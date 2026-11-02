"""``ScriptsService`` -- JSON wire interface for the scripting host.

JMRI's stock JSON API doesn't expose script management; this is a
rewrite-specific extension. Wire shapes:

* ``method=list`` -> ``[{name, started_at, finished, error, stdout,
  stderr, running}]``.
* ``method=get``, ``data={"name": ...}`` -> one status entry.
* ``method=put``, ``data={"name": ..., "source": "...", "save": bool}``
  -> start a script from inline source; when ``save`` is true, also
  write it to ``<profile>/python/<name>.py`` so it autoloads next boot.
* ``method=post``, ``data={"name": ..., "action": ...}`` -- verbs:
    - ``"stop"`` (default) -- signal the automaton to stop
    - ``"delete"`` -- stop AND remove the on-disk file. Refuses when
      the file lives under the shared ``<settings-root>/python/`` dir
      because those are operator-managed globals; the UI only
      manages per-profile scripts.
* ``method=list``, ``data={"library": true}`` -> list scripts from
  the shared and per-profile python dirs so the UI can render the
  library table. Each entry carries a ``source`` tag (``"shared"``
  or ``"profile"``) so the UI can lock down the actions for shared
  scripts.

Autoload / library scan sequence -- same for both:

1. ``<settings-root>/python/`` -- cross-profile shared helpers.
2. ``<profile>/python/`` -- per-profile scripts.

The service uses one :class:`ScriptRunner` per server context, lazily
constructed on first use. The runner's event loop is the FastAPI app's
event loop, so the sync shim wrappers correctly bridge.
"""
from __future__ import annotations

import asyncio
import pathlib
from typing import Any

from jmri_core.persistence.profile import Profile
from jmri_scripting import ScriptRunner

from jmri_server.auth.models import Role
from jmri_server.service import JsonService, JsonServiceError


def _status_to_dict(s: Any) -> dict[str, Any]:
    return {
        "name": s.name,
        "started_at": s.started_at,
        "finished": s.finished,
        "error": s.error,
        "running": s.thread is not None and s.thread.is_alive(),
        # Captured per-script stdout/stderr. Bounded (~64 KB with FIFO
        # truncation and a leading marker) so a runaway loop doesn't
        # OOM the response. Empty string if the script produced none.
        "stdout": s.stdout,
        "stderr": s.stderr,
    }


_LIBRARY_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9_.-]+$")


def _sanitize_library_name(name: str) -> str:
    """Reject anything that isn't a plain filename component.

    Prevents path traversal (``../../etc/passwd``), absolute paths, and
    other tricks against the profile scripts directory. The rule is
    intentionally strict -- script names live in the URL space and get
    stored as filenames, so restricting to ``[A-Za-z0-9_.-]`` costs
    nothing and avoids edge cases.
    """
    if not name or not _LIBRARY_NAME_RE.match(name) or ".." in name:
        raise JsonServiceError(400, f"invalid script name: {name!r}")
    return name


class ScriptsService(JsonService):
    topic = "scripts"
    # Scripts execute arbitrary Python on the server -- that's
    # remote-code-execution surface. Restrict mutations to admin
    # so a compromised operator token can't drop a script that
    # steals the settings dir or reads users.yaml. Reads (list /
    # get) are fine for operators -- they may want to inspect
    # what's running.
    REQUIRED_ROLE = {
        "list": Role.OPERATOR,
        "get": Role.OPERATOR,
        "put": Role.ADMIN,
        "post": Role.ADMIN,
    }

    @property
    def _runner(self) -> ScriptRunner:
        # Cache the runner on the context. Lazy because the event loop
        # isn't running at app-construction time.
        cached = getattr(self.ctx, "_script_runner", None)
        if cached is None:
            loop = asyncio.get_event_loop()
            cached = ScriptRunner.for_connection(loop, self.ctx.connection)
            self.ctx._script_runner = cached  # type: ignore[attr-defined]
        return cached

    def _python_dirs(self) -> list[tuple[pathlib.Path, str]]:
        """The two autoload/library dirs, in scan order, tagged by
        source (``"shared"`` for the settings-root dir, ``"profile"``
        for the active profile's dir). Missing profile -> empty list."""
        profile = getattr(self.ctx, "_roster_profile", None)
        if not isinstance(profile, Profile):
            return []
        return [
            (profile.shared_python_dir, "shared"),
            (profile.python_dir, "profile"),
        ]

    def _profile_python_dir(self) -> pathlib.Path | None:
        """Save target for user-submitted scripts. Shared scripts are
        operator-managed; the UI writes to the per-profile dir only."""
        profile = getattr(self.ctx, "_roster_profile", None)
        return profile.python_dir if isinstance(profile, Profile) else None

    def _find_script_on_disk(self, name: str) -> pathlib.Path | None:
        """Locate ``<name>.py`` under the per-profile or shared dir.

        Per-profile wins when both exist -- matches the autoload
        order that shipped in the python-dir migration. Returns
        None when neither dir has the file; the caller surfaces
        that as 404 so the UI can distinguish "script not on
        disk" from "run failed"."""
        for dir_path, _tag in self._python_dirs():
            candidate = dir_path / f"{name}.py"
            if candidate.is_file():
                return candidate
        return None

    async def on_list(self, data: dict[str, Any] | None) -> Any:
        data = data or {}
        if data.get("library"):
            # Library shape: everything the runner *knows about* (either
            # running or persisted on disk) so the UI can render a
            # single table. Scripts are keyed by name -- a per-profile
            # file with the same name as a shared one wins (last write
            # in scan order), matching the runner's autoload behaviour.
            statuses = {s.name: _status_to_dict(s) for s in self._runner.list()}
            for dir_path, source_tag in self._python_dirs():
                if not dir_path.is_dir():
                    continue
                for p in sorted(dir_path.glob("*.py")):
                    if p.name.startswith("_"):
                        continue
                    entry = statuses.setdefault(p.stem, {
                        "name": p.stem, "started_at": None,
                        "finished": False, "error": None, "running": False,
                        "stdout": "", "stderr": "",
                    })
                    entry["path"] = str(p)
                    entry["source"] = source_tag
            return list(statuses.values())
        return [_status_to_dict(s) for s in self._runner.list()]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        if not isinstance(name, str):
            raise JsonServiceError(400, "scripts.get requires 'name'")
        status = self._runner.get(name)
        if status is None:
            raise JsonServiceError(404, f"no script named {name!r}")
        return _status_to_dict(status)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        source = data.get("source")
        save = bool(data.get("save", False))
        if not isinstance(name, str) or not isinstance(source, str):
            raise JsonServiceError(400, "scripts.put requires 'name' and 'source'")
        if save:
            # Persist to <profile>/python/<name>.py so the script
            # autoloads on next boot. Requires an active profile.
            # Shared scripts under <settings>/python/ are not writable
            # via the API -- operator drops them by hand.
            _sanitize_library_name(name)
            pdir = self._profile_python_dir()
            if pdir is None:
                raise JsonServiceError(
                    409,
                    "cannot save: no active profile has a python dir",
                )
            pdir.mkdir(parents=True, exist_ok=True)
            path = pdir / f"{name}.py"
            path.write_text(source, encoding="utf-8")
            status = self._runner.run_file(path, name=name)
        else:
            status = self._runner.run_source(source, name=name)
        return _status_to_dict(status)

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        action = data.get("action", "stop")
        if not isinstance(name, str):
            raise JsonServiceError(400, "scripts.post requires 'name'")
        if action == "delete":
            _sanitize_library_name(name)
            # Stop any running instance, then remove the on-disk file.
            # Only per-profile scripts are removable via the API --
            # shared scripts under <settings>/python/ are operator
            # artefacts. A shared file with the same name blocks the
            # delete with 409 so the caller sees why.
            pdir = self._profile_python_dir()
            profile_path = (pdir / f"{name}.py") if pdir else None
            shared_dir = None
            profile = getattr(self.ctx, "_roster_profile", None)
            if isinstance(profile, Profile):
                shared_dir = profile.shared_python_dir
            shared_path = (
                shared_dir / f"{name}.py" if shared_dir else None
            )
            if (
                (profile_path is None or not profile_path.is_file())
                and shared_path is not None
                and shared_path.is_file()
            ):
                raise JsonServiceError(
                    409,
                    f"script {name!r} lives under the shared python dir; "
                    f"remove it from disk manually",
                )
            self._runner.stop(name)
            if profile_path is not None and profile_path.is_file():
                profile_path.unlink()
            status = self._runner.get(name)
            return _status_to_dict(status) if status else {"name": name, "deleted": True}
        if action == "run_file":
            # Re-read the on-disk file (per-profile first, then
            # shared) and hand it to the runner. This is the "debug
            # loop" primitive -- edit the file externally, click
            # Run in the UI, see fresh output. The runner replaces
            # any existing status entry with the same name so
            # re-runs stack cleanly (previous automatons get
            # stopped first).
            _sanitize_library_name(name)
            path = self._find_script_on_disk(name)
            if path is None:
                raise JsonServiceError(
                    404,
                    f"no script named {name!r} on disk under the "
                    "profile or shared python dirs",
                )
            status = self._runner.run_file(path, name=name)
            return _status_to_dict(status)
        if action != "stop":
            raise JsonServiceError(400, f"unknown scripts action: {action!r}")
        if not self._runner.stop(name):
            raise JsonServiceError(404, f"no script named {name!r}")
        status = self._runner.get(name)
        return _status_to_dict(status) if status else {"name": name}
