"""``ScriptRunner`` -- load + execute Python scripts against a JmriShim.

Each script gets:

* its own globals dict seeded from :meth:`JmriShim.globals`;
* a worker thread it runs on (so sync shim wrappers can bridge to the
  event loop without deadlocking);
* a unique name for status / stop lookup.

A script can be:

1. A plain ``.py`` file that runs top-to-bottom (statements at module level).
2. A file that defines ``class MyScript(AbstractAutomaton): ...`` and
   calls ``MyScript().start()`` -- the runner intercepts ``start`` so the
   automaton runs on the worker thread.
3. A file that defines ``def main(shim): ...`` -- called with the shim.

For (2), :func:`run_script` automatically detects ``AbstractAutomaton``
subclasses and instantiates them. The script doesn't have to import
``AbstractAutomaton`` -- it's in the injected globals.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from pmri_scripting import jython_shim
from pmri_scripting.automaton import AbstractAutomaton

if TYPE_CHECKING:
    from pmri_core.hal.connection import SystemConnection

    from pmri_scripting.shim import JmriShim


# Per-thread stdout/stderr capture. See :class:`_ThreadDispatchStream`.
_DEFAULT_OUTPUT_BUFFER_BYTES = 64 * 1024


# ``pmri-autoload: false`` (or ``no``, ``off``, ``0``) anywhere in the
# first 20 lines opts the file out of the startup autoloader while
# leaving it available for on-demand Run via the /scripts UI. Case-
# insensitive on the value; the key spelling is fixed to make grep
# straightforward.
import re as _re  # noqa: E402
_AUTOLOAD_OPTOUT_RE = _re.compile(
    r"#\s*pmri-autoload\s*:\s*(false|no|off|0)\b",
    _re.IGNORECASE,
)
_OPTOUT_SCAN_LINES = 20


def _autoload_opt_out(source: str) -> bool:
    """Return True when *source*'s header carries the opt-out marker.

    Restricted to the first :data:`_OPTOUT_SCAN_LINES` so a script
    that happens to contain the string in a large docstring or data
    block doesn't accidentally opt out."""
    head = "\n".join(source.splitlines()[:_OPTOUT_SCAN_LINES])
    return bool(_AUTOLOAD_OPTOUT_RE.search(head))


class _BoundedTextBuffer:
    """Text buffer with FIFO truncation once *max_bytes* is exceeded.

    Not thread-safe by design: one script thread owns one buffer, and
    reads come from the asyncio thread only after ``finished`` flips or
    when the caller explicitly copies :meth:`getvalue`. String concat
    at 64 KB is fine on modern hardware -- a script printing megabytes
    of chatter isn't the workload we're optimising for.
    """

    __slots__ = ("_data", "_max")

    def __init__(self, max_bytes: int = _DEFAULT_OUTPUT_BUFFER_BYTES) -> None:
        self._max = max_bytes
        self._data: str = ""

    def write(self, text: str) -> int:
        self._data += text
        overshoot = len(self._data) - self._max
        if overshoot > 0:
            # Drop the oldest bytes; keep a leading truncation marker so
            # readers can tell the buffer wrapped.
            marker = f"... ({overshoot} bytes elided) ...\n"
            self._data = marker + self._data[overshoot + len(marker):]
        return len(text)

    def flush(self) -> None:  # noqa: D401 -- file-like protocol
        return

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def getvalue(self) -> str:
        return self._data


class _ThreadDispatchStream:
    """Route ``write`` calls to a per-thread target, else to *fallback*.

    Installed once at module import time as ``sys.stdout`` (and again
    as ``sys.stderr``). When a script thread starts, the runner calls
    :meth:`register` to attach that thread's :class:`_BoundedTextBuffer`;
    on exit it calls :meth:`unregister`. Writes from other threads
    (asyncio loop, driver monitors, logging) pass through to the
    original ``sys.__stdout__`` unchanged.

    Thread-safe: the target map is guarded by a lock, and each
    ``write`` snapshots the current target before delegating so an
    unregister racing with a write can't corrupt state.
    """

    def __init__(self, fallback: TextIO) -> None:
        self._fallback = fallback
        self._targets: dict[int, TextIO] = {}
        self._lock = threading.Lock()

    def register(self, target: TextIO) -> None:
        with self._lock:
            self._targets[threading.get_ident()] = target

    def unregister(self) -> None:
        with self._lock:
            self._targets.pop(threading.get_ident(), None)

    def _pick(self) -> TextIO:
        with self._lock:
            return self._targets.get(threading.get_ident(), self._fallback)

    def write(self, data: str) -> int:
        return self._pick().write(data)

    def flush(self) -> None:
        self._pick().flush()

    def isatty(self) -> bool:
        try:
            return self._fallback.isatty()
        except (AttributeError, ValueError):
            return False

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fallback.fileno()


def _install_dispatch_streams() -> tuple[_ThreadDispatchStream, _ThreadDispatchStream]:
    """Idempotently swap :data:`sys.stdout` and :data:`sys.stderr` for
    :class:`_ThreadDispatchStream` proxies. Returns the two proxies so
    the runner can register/unregister on them.

    Called from :meth:`ScriptRunner.__init__` (not at module import)
    so that test frameworks like ``pytest`` that replace ``sys.stdout``
    per test still get their capture wrapped -- the proxy uses whatever
    ``sys.stdout`` is at install time as its fallback for non-script
    threads.
    """
    stdout = sys.stdout
    stderr = sys.stderr
    if isinstance(stdout, _ThreadDispatchStream):
        stdout_proxy = stdout
    else:
        stdout_proxy = _ThreadDispatchStream(stdout)
        sys.stdout = stdout_proxy
    if isinstance(stderr, _ThreadDispatchStream):
        stderr_proxy = stderr
    else:
        stderr_proxy = _ThreadDispatchStream(stderr)
        sys.stderr = stderr_proxy
    return stdout_proxy, stderr_proxy


@dataclass
class ScriptStatus:
    """One running (or finished) script."""

    name: str
    path: pathlib.Path | None
    started_at: float
    automaton: AbstractAutomaton | None = None
    thread: threading.Thread | None = None
    error: str | None = None
    finished: bool = False
    # Per-script captured stdout/stderr. Populated on the script's
    # worker thread via the dispatch streams above; readable from any
    # thread -- the buffer's underlying string is immutable per read
    # since ``getvalue`` returns a snapshot.
    _stdout_buffer: _BoundedTextBuffer = field(
        default_factory=_BoundedTextBuffer,
    )
    _stderr_buffer: _BoundedTextBuffer = field(
        default_factory=_BoundedTextBuffer,
    )

    @property
    def stdout(self) -> str:
        """Captured ``print()`` output. Truncated FIFO once ~64 KB."""
        return self._stdout_buffer.getvalue()

    @property
    def stderr(self) -> str:
        """Captured ``sys.stderr`` output (raised tracebacks, warnings)."""
        return self._stderr_buffer.getvalue()


class ScriptRunner:
    """Owns the script lifecycle for one ServerContext."""

    def __init__(self, shim: JmriShim) -> None:
        self._shim = shim
        self._scripts: dict[str, ScriptStatus] = {}
        self._lock = threading.Lock()
        # Install the stdout/stderr dispatch streams lazily on runner
        # construction so ``sys.stdout``'s current identity (which may
        # be pytest's capture, a systemd journal wrapper, or the real
        # terminal) becomes the fallback for non-script threads.
        self._stdout_proxy, self._stderr_proxy = _install_dispatch_streams()
        # Register the synthetic ``jmri.*`` module tree so Jython-shaped
        # scripts (``from jmri import InstanceManager``) import
        # unchanged. Idempotent -- modules are only registered once
        # per process.
        jython_shim.install()

    @classmethod
    def for_connection(
        cls, loop: asyncio.AbstractEventLoop, connection: SystemConnection
    ) -> ScriptRunner:
        from pmri_scripting.shim import JmriShim  # noqa: PLC0415
        return cls(JmriShim(loop, connection))

    @property
    def shim(self) -> JmriShim:
        return self._shim

    # ----- public API ----------------------------------------------------

    def run_file(self, path: str | pathlib.Path, *, name: str | None = None) -> ScriptStatus:
        """Compile and start *path*. Returns immediately; script runs in background."""
        p = pathlib.Path(path)
        source = p.read_text(encoding="utf-8")
        name = name or p.stem
        return self._start(name, source, p)

    def load_directory(
        self, path: str | pathlib.Path, *, glob: str = "*.py",
    ) -> list[ScriptStatus]:
        """Autoload every ``*.py`` under *path* and start it.

        Used on server startup to resume scripts the user parked in the
        profile's ``python/`` directory -- this is the profile-scoped
        persistence layer.

        **Three opt-outs from autoload**, in order of aggressiveness:

        1. **``_``-prefix** (``_helpers.py``) -- private module. Skipped
           entirely; not run and not visible under Python's ``import``
           system as a top-level module either.
        2. **``lib_``-prefix** (``lib_stuff.py``) -- importable helper.
           Skipped from auto-execute but the directory is on
           ``sys.path`` so ``main.py`` can ``import lib_stuff``.
        3. **Magic comment** (``# pmri-autoload: false`` in the first
           few lines) -- the file is a regular module, importable by
           its own name, but the autoloader leaves it alone. Ideal
           for scripts you want to keep on disk for the on-demand
           Run button but don't want firing at every server start.

        Read errors (permissions, encoding) skip the file silently.
        Scripts that fail to compile still get a status entry with
        ``error`` populated so the UI can surface them.
        """
        root = pathlib.Path(path)
        if not root.is_dir():
            return []
        # Register the directory on sys.path once so scripts can
        # ``import`` peer files as regular Python modules -- the
        # recursive-scripting affordance. Idempotent via ``in`` check.
        root_str = str(root.resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        started: list[ScriptStatus] = []
        for p in sorted(root.glob(glob)):
            if p.name.startswith("_") or not p.is_file():
                continue
            # ``lib_*.py`` are library modules for other scripts to
            # ``import``. Don't auto-execute them.
            if p.name.startswith("lib_"):
                continue
            try:
                source = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if _autoload_opt_out(source):
                # The magic-comment opt-out. File stays on disk +
                # visible to the /scripts UI's Run button, but not
                # autoexecuted at server startup.
                continue
            started.append(self._start(p.stem, source, p))
        return started

    def run_source(self, source: str, *, name: str) -> ScriptStatus:
        """Compile and start *source* (inline script body)."""
        return self._start(name, source, None)

    def stop(self, name: str) -> bool:
        """Signal the named script to stop. Returns True if it was running."""
        with self._lock:
            status = self._scripts.get(name)
            if status is None:
                return False
            if status.automaton is not None:
                status.automaton.stop()
            # Plain scripts can't be stopped mid-statement; they'll finish
            # naturally on the next handle() boundary if they're using an
            # automaton, or run to completion otherwise.
            return True

    def list(self) -> list[ScriptStatus]:
        with self._lock:
            return list(self._scripts.values())

    def get(self, name: str) -> ScriptStatus | None:
        with self._lock:
            return self._scripts.get(name)

    def stop_all(self, *, join_timeout: float = 2.0) -> None:
        with self._lock:
            statuses = list(self._scripts.values())
        for s in statuses:
            if s.automaton is not None:
                s.automaton.stop()
        for s in statuses:
            if s.thread is not None and s.thread.is_alive():
                s.thread.join(timeout=join_timeout)

    # ----- internals -----------------------------------------------------

    def _start(self, name: str, source: str, path: pathlib.Path | None) -> ScriptStatus:
        # Re-verify the dispatch streams are still installed. Some test
        # runners (pytest) and long-running processes (uvicorn --reload)
        # swap ``sys.stdout`` after our ``__init__`` first installed the
        # proxy -- re-installing here wraps whatever is current so a
        # freshly-started script still routes through us.
        if sys.stdout is not self._stdout_proxy or sys.stderr is not self._stderr_proxy:
            self._stdout_proxy, self._stderr_proxy = _install_dispatch_streams()
        status = ScriptStatus(
            name=name, path=path, started_at=time.time(),
        )
        with self._lock:
            # If a previous script with the same name is around, stop it first.
            existing = self._scripts.get(name)
            if existing and existing.automaton is not None:
                existing.automaton.stop()
            self._scripts[name] = status

        thread = threading.Thread(
            target=self._exec_script,
            args=(name, source, path, status),
            name=f"pmri-script:{name}",
            daemon=True,
        )
        status.thread = thread
        thread.start()
        return status

    def _exec_script(
        self,
        name: str,
        source: str,
        path: pathlib.Path | None,
        status: ScriptStatus,
    ) -> None:
        # Route this thread's stdout/stderr into the status buffers so
        # ``print()`` and any raised traceback surface in the web UI
        # rather than getting lost in the server's log stream. The
        # dispatch streams were installed on runner construction; here
        # we just register/unregister this thread's target.
        self._stdout_proxy.register(status._stdout_buffer)
        self._stderr_proxy.register(status._stderr_buffer)
        # Bind the shim to this thread so ``jmri.InstanceManager``
        # can find the active connection during script execution.
        jython_shim._bind_shim(self._shim)
        try:
            self._exec_script_body(name, source, path, status)
        finally:
            jython_shim._unbind_shim()
            self._stdout_proxy.unregister()
            self._stderr_proxy.unregister()

    def _exec_script_body(
        self,
        name: str,
        source: str,
        path: pathlib.Path | None,
        status: ScriptStatus,
    ) -> None:
        globals_dict = self._shim.globals() | {
            "AbstractAutomaton": AbstractAutomaton,
            "__name__": "__pmri_script__",
            "__file__": str(path) if path else f"<{name}>",
        }
        try:
            code = compile(source, str(path) if path else f"<{name}>", "exec")
            exec(code, globals_dict)  # noqa: S102 -- script execution is the feature
        except Exception:
            status.error = traceback.format_exc()
            status.finished = True
            return

        # After top-level execution, look for an automaton subclass to run.
        automaton_cls = _find_automaton_class(globals_dict)
        if automaton_cls is not None:
            automaton = automaton_cls(name=name)
            automaton.shim = self._shim
            with self._lock:
                status.automaton = automaton
            try:
                automaton.run()
            except Exception:
                status.error = traceback.format_exc()
        # Or look for a `main(shim)` callable.
        else:
            main_fn = globals_dict.get("main")
            if callable(main_fn):
                try:
                    sig = inspect.signature(main_fn)
                    if len(sig.parameters) > 0:
                        main_fn(self._shim)
                    else:
                        main_fn()
                except Exception:
                    status.error = traceback.format_exc()
        status.finished = True


def _find_automaton_class(globals_dict: dict[str, Any]) -> type[AbstractAutomaton] | None:
    """Find the first ``AbstractAutomaton`` subclass defined in the script."""
    for value in globals_dict.values():
        if (
            isinstance(value, type)
            and issubclass(value, AbstractAutomaton)
            and value is not AbstractAutomaton
            and value.__module__ in ("__pmri_script__", "<string>")
        ):
            return value
    return None
