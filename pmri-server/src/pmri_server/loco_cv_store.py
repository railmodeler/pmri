"""``LocoCvStore`` -- lazy per-locomotive CV-state cache backed by disk.

Two on-disk layouts can carry a loco's CV state:

* **JMRI-canonical**: the ``<values>`` block embedded in
  ``<profile>/roster/<id>.xml`` (the locomotive-config file itself).
  This is what JMRI Java reads and writes.
* **Legacy proprietary**: ``<profile>/rosters/<id>.xml`` with our own
  ``<locomotive-cv-values>`` root. Used by earlier alphas.

On read we try the canonical location first; if it has no ``<values>``
we fall back to the legacy one. When *both* have CV data, we raise
:class:`AmbiguousCvSourceError` -- the UI catches that and prompts the
user to pick which source is authoritative. Once resolved (or on a fresh
loco), writes go to the location we loaded from, defaulting to the
JMRI-canonical embedded ``<values>`` block for interop.

Storage policy is otherwise unchanged:

* In-memory state is the source of truth during a session.
* Files are the source of truth across server restarts.
* Writes are atomic (tmp + rename).
* Failures are logged, not raised, so a permissions issue doesn't
  crash the programmer page mid-session.
"""
from __future__ import annotations

import logging
import pathlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pmri_core.beans.loco_cv_state import LocoCvState
from pmri_core.persistence.loco_cv_xml import (
    load_cv_values_from_locomotive_config,
    load_loco_cv,
    save_cv_values_into_locomotive_config,
)
from pmri_core.persistence.profile import Profile

# ``(loco_id) -> {cv_number_str: default_value_str}``. Consulted by
# :meth:`LocoCvStore.set_cv` when deciding what to persist: any CV whose
# in-memory value equals its catalog default is *dropped* from the
# roster ``<values>`` block on write, per the spec (roster stores only
# user customisations). Returning an empty dict disables filtering for
# that loco (catalog not yet loaded etc.).
DefaultLookup = Callable[[str], dict[str, str]]

_log = logging.getLogger(__name__)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class CvSource(StrEnum):
    """Where a loco's CV state is / should be persisted."""
    ROSTER   = "roster"    # JMRI-canonical: <profile>/roster/<id>.xml <values>
    LEGACY   = "legacy"    # our layout:      <profile>/rosters/<id>.xml


class AmbiguousCvSourceError(Exception):
    """Both source locations have CV data for the same loco.

    The UI catches this and prompts the user which source is authoritative
    before proceeding. Once chosen, the picked source is remembered on
    :class:`LocoCvStore` so future accesses don't re-prompt.
    """

    def __init__(
        self, loco_id: str,
        roster_path: pathlib.Path, legacy_path: pathlib.Path,
    ) -> None:
        super().__init__(
            f"CV state for loco {loco_id!r} exists in both "
            f"{roster_path} and {legacy_path}; caller must pick one via "
            f"LocoCvStore.set_preferred_source()",
        )
        self.loco_id = loco_id
        self.roster_path = roster_path
        self.legacy_path = legacy_path


def _safe_filename(loco_id: str) -> str:
    """Sanitize a loco id into a filesystem-safe filename stem."""
    cleaned = _SAFE_FILENAME.sub("_", loco_id).strip("_") or "entry"
    return f"{cleaned}.xml"


def _resolve_filename(loco_id: str, file_name: str | None) -> str:
    """Pick the CV-state filename for *loco_id*.

    JMRI's roster stores a ``fileName`` attribute on each locomotive
    (often a small integer like ``1.xml``) -- when set, that's the file
    JMRI uses for decoder state, and that's the file our programmer
    must read/write. Falls back to a sanitised ``<id>.xml`` when the
    roster doesn't pin a filename.
    """
    if file_name:
        # Only the basename; never let a caller traverse out of the dir.
        return pathlib.Path(file_name).name
    return _safe_filename(loco_id)


@dataclass
class LocoCvStore:
    """Lazy disk-backed map of loco-id -> :class:`LocoCvState`.

    ``default_lookup`` is optional; when supplied, writes filter out any
    CV whose value matches the catalog default so ``roster/<id>.xml``
    stays a minimal delta. See :data:`DefaultLookup` for the callable's
    signature. Set to ``None`` for legacy behaviour (write every CV).
    """

    profile: Profile
    default_lookup: DefaultLookup | None = None
    _by_id: dict[str, LocoCvState] = field(default_factory=dict)
    # loco_id -> chosen source. Set via ``set_preferred_source`` when the
    # UI resolves an ambiguity, or automatically when only one source
    # has data.
    _source_by_id: dict[str, CvSource] = field(default_factory=dict)
    # Roster files we've already backed up during this session.
    # Absolute paths so switching a loco's ``fileName`` still triggers
    # a fresh backup on the new file. See :meth:`_backup_once`.
    _backed_up_paths: set[pathlib.Path] = field(default_factory=set)

    def __post_init__(self) -> None:
        # Both locations are created lazily on first write. Not
        # pre-scanned -- the programmer opens for one loco at a time.
        self.profile.rosters_cv_dir.mkdir(parents=True, exist_ok=True)
        self.profile.roster_dir.mkdir(parents=True, exist_ok=True)

    # ----- source resolution ------------------------------------------

    def _roster_path(self, loco_id: str, file_name: str | None) -> pathlib.Path:
        """The JMRI-canonical file: ``<profile>/roster/<filename>``."""
        return self.profile.roster_dir / _resolve_filename(loco_id, file_name)

    def _legacy_path(self, loco_id: str, file_name: str | None) -> pathlib.Path:
        """Our proprietary file: ``<profile>/rosters/<filename>``."""
        return self.profile.rosters_cv_dir / _resolve_filename(loco_id, file_name)

    def path_for(
        self, loco_id: str, file_name: str | None = None,
        source: CvSource | None = None,
    ) -> pathlib.Path:
        """Resolve the file path for *loco_id* under *source*.

        Backwards-compat with older callers that assumed the legacy
        location: when *source* is None, use the loco's preferred source
        if known, else default to the legacy path.
        """
        source = source or self._source_by_id.get(loco_id, CvSource.LEGACY)
        if source is CvSource.ROSTER:
            return self._roster_path(loco_id, file_name)
        return self._legacy_path(loco_id, file_name)

    def _backup_once(self, roster_path: pathlib.Path) -> None:
        """Copy *roster_path* to ``<name>.xml.bak`` the first time the
        session touches it.

        No-op if:

        * the path has already been backed up during this store's life;
        * the source file doesn't exist (nothing to back up);
        * the copy raises :class:`OSError` (permissions / filesystem
          full) -- logged so a follow-up sync can see the failure, but
          the write itself proceeds.
        """
        abs_path = roster_path.resolve() if roster_path.exists() else roster_path
        if abs_path in self._backed_up_paths:
            return
        # Mark first so a failure to copy doesn't cause a retry storm
        # on every subsequent write.
        self._backed_up_paths.add(abs_path)
        if not roster_path.exists():
            return
        backup_path = roster_path.with_suffix(roster_path.suffix + ".bak")
        try:
            shutil.copy2(roster_path, backup_path)
            _log.info(
                "LocoCvStore: backed up %s -> %s", roster_path, backup_path,
            )
        except OSError as e:
            _log.warning(
                "LocoCvStore: backup %s -> %s failed: %s",
                roster_path, backup_path, e,
            )

    def set_preferred_source(
        self, loco_id: str, source: CvSource,
    ) -> None:
        """Record which source the UI wants us to use for this loco.

        Clears any cached in-memory state so the next :meth:`get` reads
        the freshly-picked source from disk.
        """
        self._source_by_id[loco_id] = source
        self._by_id.pop(loco_id, None)

    # ----- read --------------------------------------------------------

    def get(self, loco_id: str, file_name: str | None = None) -> LocoCvState:
        """Return the loco's CV state, loading from disk on first hit.

        Resolution order:

        1. In-memory cache
        2. Preferred source set via :meth:`set_preferred_source`
        3. JMRI-canonical ``roster/<id>.xml`` ``<values>`` block
        4. Legacy ``rosters/<id>.xml``
        5. Empty state if none of the above have CVs

        Raises :class:`AmbiguousCvSourceError` when both canonical and
        legacy locations have CV data and the preferred source hasn't
        been pinned yet.
        """
        if loco_id in self._by_id:
            return self._by_id[loco_id]

        pref = self._source_by_id.get(loco_id)
        if pref is CvSource.ROSTER:
            state = self._read_roster(loco_id, file_name) or LocoCvState(loco_id=loco_id)
        elif pref is CvSource.LEGACY:
            state = self._read_legacy(loco_id, file_name) or LocoCvState(loco_id=loco_id)
        else:
            roster_state = self._read_roster(loco_id, file_name)
            legacy_state = self._read_legacy(loco_id, file_name)
            if roster_state and legacy_state and (
                roster_state.cv_values and legacy_state.cv_values
            ):
                raise AmbiguousCvSourceError(
                    loco_id,
                    self._roster_path(loco_id, file_name),
                    self._legacy_path(loco_id, file_name),
                )
            if roster_state and roster_state.cv_values:
                self._source_by_id[loco_id] = CvSource.ROSTER
                state = roster_state
            elif legacy_state:
                self._source_by_id[loco_id] = CvSource.LEGACY
                state = legacy_state
            elif roster_state is not None:
                # Roster entry file exists but has no <values> -- write
                # to JMRI-canonical on next set_cv for best interop.
                self._source_by_id[loco_id] = CvSource.ROSTER
                state = roster_state
            else:
                # Neither exists yet -- default new locos to canonical.
                self._source_by_id[loco_id] = CvSource.ROSTER
                state = LocoCvState(loco_id=loco_id)

        # File-stored ids might not match what the caller passed (JMRI
        # ``fileName`` decouples filename from loco id). Canonicalise.
        if state.loco_id != loco_id:
            state = LocoCvState(loco_id=loco_id, cv_values=dict(state.cv_values))
        self._by_id[loco_id] = state
        return state

    def _read_roster(
        self, loco_id: str, file_name: str | None,
    ) -> LocoCvState | None:
        """Try to read CV state from ``roster/<id>.xml`` (JMRI-canonical)."""
        path = self._roster_path(loco_id, file_name)
        if not path.exists():
            return None
        try:
            return load_cv_values_from_locomotive_config(path, loco_id=loco_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("LocoCvStore: failed to read %s: %s", path, e)
            return None

    def _read_legacy(
        self, loco_id: str, file_name: str | None,
    ) -> LocoCvState | None:
        """Try to read CV state from ``rosters/<id>.xml`` (our layout)."""
        path = self._legacy_path(loco_id, file_name)
        if not path.exists():
            return None
        try:
            return load_loco_cv(path)
        except Exception as e:  # noqa: BLE001
            _log.warning("LocoCvStore: failed to read %s: %s", path, e)
            return None

    # ----- write -------------------------------------------------------

    def record_read_result(
        self, loco_id: str, cv: str, value: int, file_name: str | None = None,
    ) -> LocoCvState:
        """Record a freshly-read CV in the in-memory cache. Does NOT
        persist to disk and does NOT trigger a ``.bak``.

        JMRI Java treats reads as transient until the user explicitly
        saves. Mirroring that (rather than our older persist-every-read
        behaviour) means:

        * The ``.bak`` created by :meth:`_backup_once` captures the
          pre-modification state -- a Read pane that touches 28 CVs
          doesn't burn the backup on a read that leaves the roster
          logically unchanged.
        * SD cards on Pi setups don't take a write per CV read (the
          Read pane action does ~30 reads sequentially).
        * A restart discards the read cache -- but that's exactly the
          "reads are ephemeral" contract JMRI Java exposes.

        Callers who want the read to persist (e.g. a "Save read
        results" button, or a batch that logically commits reads) can
        call :meth:`set_cv` with the same value.
        """
        state = self.get(loco_id, file_name)
        new_cvs = dict(state.cv_values)
        new_cvs[str(cv)] = int(value)
        new_state = LocoCvState(loco_id=loco_id, cv_values=new_cvs)
        self._by_id[loco_id] = new_state
        return new_state

    def set_cv(
        self, loco_id: str, cv: str, value: int, file_name: str | None = None,
    ) -> LocoCvState:
        """Set one CV value; persist to the JMRI-canonical
        ``roster/<id>.xml`` ``<values>`` block, filtering CVs equal to
        the catalog default. Returns the updated state.

        Filtering behaviour depends on :attr:`default_lookup`:

        * When supplied and it returns a non-empty dict for this loco,
          any CV whose value matches the default is *dropped* from what
          gets written -- the roster file stays a minimal delta.
        * When missing or empty, every in-memory CV is written (backward
          compatible with the pre-refactor behaviour).

        For interop with JMRI Java, writes always target the embedded
        ``<values>`` block inside ``roster/<id>.xml``. The old
        proprietary ``rosters/<id>.xml`` remains read-only for the
        migration window; explicit ``CvSource.LEGACY`` preferences from
        the ambiguity chooser also read from there but write forward to
        the canonical location.
        """
        state = self.get(loco_id, file_name)
        new_cvs = dict(state.cv_values)
        new_cvs[str(cv)] = int(value)
        new_state = LocoCvState(loco_id=loco_id, cv_values=new_cvs)
        self._by_id[loco_id] = new_state

        # Apply the default-diff filter: drop any CV whose stringified
        # value equals the catalog default. Comparison is done on
        # strings so we sidestep numeric edge cases (e.g. "0"/"00").
        defaults: dict[str, str] = {}
        if self.default_lookup is not None:
            try:
                defaults = self.default_lookup(loco_id) or {}
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "LocoCvStore: default lookup failed for %r: %s", loco_id, e,
                )
        to_persist = {
            cv_name: cv_val for cv_name, cv_val in new_cvs.items()
            if str(cv_val) != defaults.get(cv_name)
        }

        # Write to JMRI-canonical roster/<id>.xml <values> block.
        roster_path = self._roster_path(loco_id, file_name)
        # JMRI Java behaviour: back up the pristine roster file to
        # ``<id>.xml.bak`` once, before the first write of the session.
        # Repeated writes to the same file in the same session don't
        # re-backup (otherwise the backup would race behind current
        # state and lose the pre-session baseline). Backup is
        # best-effort -- a failure here logs but doesn't block the
        # write, so a read-only ``.bak`` (e.g. permissions on an SD
        # card) can't prevent programming.
        self._backup_once(roster_path)
        try:
            save_cv_values_into_locomotive_config(
                roster_path, to_persist, loco_id=loco_id,
            )
        except OSError as e:
            _log.warning("LocoCvStore: failed to write %s: %s", roster_path, e)

        # Future reads should hit the roster path so the just-written
        # values come back on a re-open even if legacy still has stale
        # data from a previous alpha.
        self._source_by_id[loco_id] = CvSource.ROSTER
        return new_state
