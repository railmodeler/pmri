"""``RosterStore`` -- server-owned roster persistence.

Backs the roster service. Holds an in-memory dict of ``RosterEntry`` and
persists each entry as its own ``locomotive-config`` XML file under
``Profile.roster_dir`` (the Stage 2 directory layout).

Storage policy:

* One file per entry, named ``<id>.xml``.
* The roster index ``roster.xml`` is regenerated from the in-memory
  state on every mutation.
* In-memory state is the source of truth during a server session; the
  XML files are the source of truth at startup.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from pmri_core.beans.roster_entry import RosterEntry
from pmri_core.events import Signal
from pmri_core.persistence import (
    RosterIndex,
    load_locomotive_bytes,
    save_locomotive,
    save_roster_index,
)
from pmri_core.persistence.profile import Profile

_log = logging.getLogger(__name__)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(entry_id: str) -> str:
    """Sanitize an entry id into a filesystem-safe filename stem."""
    cleaned = _SAFE_FILENAME.sub("_", entry_id).strip("_") or "entry"
    return f"{cleaned}.xml"


def _emit_signal(sig: "Signal[Any]", payload: Any) -> None:
    """Fire *sig* from a sync context without blocking.

    ``Signal.emit`` is async; ``put`` / ``delete`` here are sync. When
    there's a running event loop, schedule the emit as a task so
    subscribers run in their natural async context. Without a loop
    (rare -- some unit tests instantiating RosterStore directly), the
    emit is dropped rather than raised, which matches how other stores
    (e.g. ConsistManager under sync test setups) behave.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(sig.emit(payload))


@dataclass
class RosterStore:
    """In-memory + on-disk roster."""

    profile: Profile
    _entries: dict[str, RosterEntry] = field(default_factory=dict)
    # Signals for downstream subscribers -- specifically the WiThrottle
    # server, which rebroadcasts a fresh RL to every connected client on
    # mutation. Signals are constructed in __post_init__ so a caller
    # that mutates during startup still has valid Signal instances.
    added: Signal[RosterEntry] = field(init=False)
    updated: Signal[RosterEntry] = field(init=False)
    removed: Signal[RosterEntry] = field(init=False)

    def __post_init__(self) -> None:
        self.added = Signal()
        self.updated = Signal()
        self.removed = Signal()
        self.profile.ensure()
        self._load_existing()

    def _load_existing(self) -> None:
        """Read every ``locomotive-config`` XML in roster_dir into memory."""
        roster_dir = self.profile.roster_dir
        if not roster_dir.is_dir():
            return
        for path in roster_dir.glob("*.xml"):
            if path.name == "roster.xml":
                continue
            try:
                entries = load_locomotive_bytes(path.read_bytes())
            except Exception as e:  # noqa: BLE001
                _log.warning("RosterStore: failed to load %s: %s", path, e)
                continue
            for entry in entries:
                if entry.id:
                    self._entries[entry.id] = entry

    # ----- read --------------------------------------------------------

    def list(self) -> list[RosterEntry]:
        return list(self._entries.values())

    def get(self, entry_id: str) -> RosterEntry | None:
        return self._entries.get(entry_id)

    def __iter__(self) -> Iterator[RosterEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    # ----- write -------------------------------------------------------

    def put(self, entry: RosterEntry) -> RosterEntry:
        """Create-or-replace by id. Persists to disk + rewrites the index.

        Fires ``added`` on the first insertion at *entry.id*, ``updated``
        on a replace. Subscribers (specifically the WiThrottle server)
        use these to rebroadcast the roster to connected clients so
        Engine Driver / WiThrottle iOS reflect roster changes without a
        reconnect.
        """
        if not entry.id:
            raise ValueError("RosterEntry.id is required")
        is_new = entry.id not in self._entries
        self._entries[entry.id] = entry
        path = self.profile.roster_dir / _safe_filename(entry.id)
        save_locomotive(path, [entry])
        # Update the file_name field so reloads find it.
        if not entry.file_name:
            entry.file_name = path.name
            save_locomotive(path, [entry])
        self._write_index()
        # Signal-emit is async elsewhere in the codebase; do it on a
        # loop-aware helper so sync callers (like the tests) still work.
        _emit_signal(self.added if is_new else self.updated, entry)
        return entry

    def delete(self, entry_id: str) -> bool:
        """Remove an entry. Returns True if it existed."""
        entry = self._entries.pop(entry_id, None)
        if entry is None:
            return False
        path = self.profile.roster_dir / _safe_filename(entry_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as e:
                _log.warning("RosterStore: failed to delete %s: %s", path, e)
        self._write_index()
        _emit_signal(self.removed, entry)
        return True

    # ----- index ------------------------------------------------------

    def _write_index(self) -> None:
        index = RosterIndex(entries=list(self._entries.values()))
        try:
            save_roster_index(self.profile.roster_index, index)
        except OSError as e:
            _log.warning("RosterStore: failed to write roster index: %s", e)
