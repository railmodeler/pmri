"""``LogixNGManager`` -- profile-scoped registry of :class:`LogixNG` programs.

Programs live on disk as YAML files under ``<profile>/logixng/``.
Filenames are the program's system name plus ``.yaml``. The manager
provides:

* ``list()`` / iteration
* ``get(system_name)``
* ``create(system_name, ...)``
* ``put(system_name, logixng)`` -- replace by name
* ``delete(system_name)``
* ``save_all(root)`` / ``load_all(root)``

Signals ``added`` / ``removed`` / ``updated`` fire on mutations so the
JSON push side and any future UI can react without polling.
"""
from __future__ import annotations

import logging
import pathlib
import re
from collections.abc import Iterator

from pmri_core.events import Signal

from pmri_logixng.logix import LogixNG
from pmri_logixng.serialize import from_yaml, load_yaml, save_yaml

_log = logging.getLogger(__name__)

# JMRI Java's convention for LogixNG system names is ``IQ`` (LogixNG
# root) + digits, distinct from the ``IX`` Logix (legacy) prefix. We
# follow it so system names round-trip against real JMRI XML. Callers
# using scripts can also pass any string -- validation is deliberately
# lax (a user-friendly ``IQ:MyRoutine`` is fine).
_DEFAULT_PREFIX = "IQ"

# Filenames are only allowed the same character class as JMRI system
# names to keep the ``<system_name>.yaml`` mapping unambiguous. Slashes
# and other separators would be a path-traversal risk on disk. Colons
# in particular are excluded because Windows won't allow them as file
# names -- the save silently truncates ``"IQ:x.yaml"`` to ``"IQ"``.
# Colons are permitted because JMRI Java's LogixNG system names use
# them (``IQ:AUTO:0001``); a name imported from a JMRI XML would be
# rejected without them. Slashes and spaces still excluded -- those
# would break file-per-program persistence.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class LogixNGManager:
    """Registry of :class:`LogixNG` programs, one per profile.

    Not a :class:`~pmri_core.manager.Manager` -- LogixNG isn't a
    NamedBean and the system-name shape is program-oriented rather
    than address-oriented. The signal / index shape mirrors Manager
    closely so callers can subscribe in the same pattern.
    """

    def __init__(self, prefix: str = _DEFAULT_PREFIX) -> None:
        self.prefix = prefix
        self._by_name: dict[str, LogixNG] = {}
        self.added: Signal[LogixNG] = Signal()
        self.removed: Signal[LogixNG] = Signal()
        self.updated: Signal[LogixNG] = Signal()

    # ---- read ------------------------------------------------------------

    def __iter__(self) -> Iterator[LogixNG]:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def get(self, system_name: str) -> LogixNG | None:
        return self._by_name.get(system_name)

    def names(self) -> list[str]:
        return list(self._by_name)

    # ---- mutate ---------------------------------------------------------

    async def put(self, system_name: str, logixng: LogixNG) -> LogixNG:
        """Insert or replace *logixng* under *system_name*. Fires
        ``added`` on a new entry and ``updated`` on a replace."""
        if not _SAFE_NAME_RE.match(system_name):
            raise ValueError(
                f"logixng system name {system_name!r} must match "
                f"[A-Za-z0-9_.:-]+"
            )
        existing = self._by_name.get(system_name)
        self._by_name[system_name] = logixng
        if existing is None:
            await self.added.emit(logixng)
        else:
            await self.updated.emit(logixng)
        return logixng

    async def create(
        self, system_name: str, *, user_name: str = "",
        enabled: bool = True,
    ) -> LogixNG:
        """Create an empty LogixNG program at *system_name*. Idempotent
        by name -- returns the existing program if one already lives
        there (matches how the base bean managers behave)."""
        if system_name in self._by_name:
            return self._by_name[system_name]
        prog = LogixNG(user_name=user_name, enabled=enabled)
        return await self.put(system_name, prog)

    async def delete(self, system_name: str) -> bool:
        """Remove the program at *system_name*. Returns True if one
        was found. Fires ``removed``."""
        prog = self._by_name.pop(system_name, None)
        if prog is None:
            return False
        await self.removed.emit(prog)
        return True

    # ---- disk ----------------------------------------------------------

    def save_all(self, root: pathlib.Path) -> None:
        """Write every program to ``root/<system_name>.yaml``.

        Creates ``root`` if absent. Overwrites any existing files;
        stale files (programs deleted since the last save) are
        removed so the on-disk set matches memory.
        """
        root.mkdir(parents=True, exist_ok=True)
        keep: set[pathlib.Path] = set()
        for system_name, prog in self._by_name.items():
            path = root / f"{system_name}.yaml"
            save_yaml(prog, path)
            keep.add(path)
        for existing in root.glob("*.yaml"):
            if existing not in keep:
                try:
                    existing.unlink()
                except OSError as e:
                    _log.warning(
                        "logixng: couldn't remove stale %s (%s)", existing, e,
                    )

    def save_one(self, root: pathlib.Path, system_name: str) -> None:
        """Persist a single program without touching siblings. Cheaper
        than :meth:`save_all` when only one entry changed."""
        prog = self._by_name.get(system_name)
        if prog is None:
            return
        root.mkdir(parents=True, exist_ok=True)
        save_yaml(prog, root / f"{system_name}.yaml")

    def delete_on_disk(self, root: pathlib.Path, system_name: str) -> None:
        """Remove ``<root>/<system_name>.yaml`` if it exists. Used by
        the service's ``delete`` handler so the file goes away when the
        in-memory entry does."""
        path = root / f"{system_name}.yaml"
        if path.exists():
            try:
                path.unlink()
            except OSError as e:
                _log.warning(
                    "logixng: couldn't unlink %s (%s)", path, e,
                )

    async def load_all(self, root: pathlib.Path) -> int:
        """Populate the manager from ``root/*.yaml``.

        Returns the number of programs loaded. Files that fail to
        parse are logged and skipped -- one broken file doesn't
        prevent the rest from loading.
        """
        if not root.is_dir():
            return 0
        count = 0
        for yaml_path in sorted(root.glob("*.yaml")):
            system_name = yaml_path.stem
            if not _SAFE_NAME_RE.match(system_name):
                _log.warning(
                    "logixng: skipping %s (unsafe filename)", yaml_path,
                )
                continue
            try:
                node = load_yaml(yaml_path)
            except Exception as e:  # noqa: BLE001 -- one bad file mustn't block the rest
                # YAML parse errors, decoder failures, IO errors -- all
                # get logged and skipped. The manager keeps loading
                # siblings so one broken file doesn't nuke the whole
                # set.
                _log.warning(
                    "logixng: skipping %s (%s)", yaml_path, e,
                )
                continue
            if not isinstance(node, LogixNG):
                _log.warning(
                    "logixng: %s isn't a LogixNG root, skipping", yaml_path,
                )
                continue
            await self.put(system_name, node)
            count += 1
        return count

    async def load_yaml_text(
        self, system_name: str, text: str,
    ) -> LogixNG:
        """Parse *text* as a LogixNG YAML program and register it at
        *system_name*. Convenient for JSON-service uploads that ship
        the tree inline."""
        node = from_yaml(text)
        if not isinstance(node, LogixNG):
            raise ValueError("payload is not a LogixNG root")
        return await self.put(system_name, node)
