"""``LocoCvStore`` integration -- lazy per-loco CV state, JMRI-shaped."""
from __future__ import annotations

import pathlib

import pytest
from jmri_core.persistence import Profile

from jmri_server.loco_cv_store import (
    AmbiguousCvSourceError,
    CvSource,
    LocoCvStore,
)


def test_first_get_returns_empty_state(tmp_path: pathlib.Path) -> None:
    """Loco with no on-disk file -> empty state, not an error."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    store = LocoCvStore(profile=profile)
    state = store.get("459")
    assert state.loco_id == "459"
    assert state.cv_values == {}


def test_set_cv_persists_to_jmri_canonical_values_block(tmp_path: pathlib.Path) -> None:
    """Writes go into the ``<values>`` block of ``roster/<id>.xml`` -- the
    JMRI-canonical location. Requires an existing locomotive-config file
    (the roster service creates it on entry-creation)."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {})
    store = LocoCvStore(profile=profile)
    store.set_cv("459", "29", 38)
    target = profile.roster_dir / "459.xml"
    assert target.exists()
    raw = target.read_text(encoding="utf-8")
    assert "<values>" in raw
    assert 'name="29"' in raw
    assert 'value="38"' in raw


def test_subsequent_get_hits_cache(tmp_path: pathlib.Path) -> None:
    """Once loaded, get() returns the same in-memory state."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    store = LocoCvStore(profile=profile)
    store.set_cv("459", "1", 3)
    s1 = store.get("459")
    s2 = store.get("459")
    # The dataclass is recreated on set, but multiple get() calls share it.
    assert s1.cv_values == s2.cv_values == {"1": 3}


def test_existing_file_is_loaded_on_first_get(tmp_path: pathlib.Path) -> None:
    """When ``rosters/<id>.xml`` already exists on disk, get() picks it up."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    profile.rosters_cv_dir.mkdir(parents=True)
    (profile.rosters_cv_dir / "459.xml").write_bytes(
        b"""<?xml version='1.0' encoding='UTF-8'?>
<locomotive-cv-values id="459">
  <CV name="1" value="3" />
  <CV name="29" value="38" />
</locomotive-cv-values>"""
    )
    store = LocoCvStore(profile=profile)
    state = store.get("459")
    assert state.cv_values == {"1": 3, "29": 38}


def test_set_cv_appends_to_existing_state(tmp_path: pathlib.Path) -> None:
    """Setting one CV must not wipe the others already in the file.

    Existing state lives in ``roster/<id>.xml <values>`` (the
    JMRI-canonical location). After the write, both CVs must be there.
    """
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {"1": 3})
    store = LocoCvStore(profile=profile)
    store.set_cv("459", "29", 38)
    raw = (profile.roster_dir / "459.xml").read_text()
    assert 'name="1"' in raw
    assert 'name="29"' in raw


def test_safe_filename_handles_punctuation(tmp_path: pathlib.Path) -> None:
    """Loco ids with characters like ``UP/3601`` must produce safe filenames.

    The sanitisation happens the same way for the JMRI-canonical
    ``roster/`` target as for the legacy layout.
    """
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    # The roster service normally writes the locomotive-config file with
    # a safe name; mirror that here so the CV write has a target.
    from jmri_server.loco_cv_store import _safe_filename  # noqa: PLC0415
    profile.roster_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename("UP/3601")
    (profile.roster_dir / stem).write_bytes(
        b"""<?xml version='1.0'?>
<locomotive-config><locomotive id="UP/3601" dccAddress="3601">
<decoder model="M" family="F"/></locomotive></locomotive-config>"""
    )
    store = LocoCvStore(profile=profile)
    store.set_cv("UP/3601", "1", 3)
    files = list(profile.roster_dir.glob("*.xml"))
    assert len(files) == 1
    # Punctuation collapsed to underscores; the file is still recoverable.
    assert "_" in files[0].name


def test_filename_argument_overrides_default(tmp_path: pathlib.Path) -> None:
    """JMRI's roster ``fileName`` attribute decouples loco-id from filename.

    Example: loco id="459" with fileName="1.xml" -> CVs go into the
    ``<values>`` block of ``roster/1.xml``, not ``roster/459.xml``.
    """
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {}, file_name="1.xml")
    store = LocoCvStore(profile=profile)
    store.set_cv("459", "29", 38, file_name="1.xml")
    assert (profile.roster_dir / "1.xml").exists()
    assert not (profile.roster_dir / "459.xml").exists()
    raw = (profile.roster_dir / "1.xml").read_text()
    assert 'name="29"' in raw


def test_get_with_filename_loads_correct_file(tmp_path: pathlib.Path) -> None:
    """A loco with ``fileName="1.xml"`` must load from that file."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    profile.rosters_cv_dir.mkdir(parents=True)
    (profile.rosters_cv_dir / "1.xml").write_bytes(
        b"""<?xml version='1.0' encoding='UTF-8'?>
<locomotive-cv-values id="459">
  <CV name="29" value="38" />
</locomotive-cv-values>"""
    )
    store = LocoCvStore(profile=profile)
    state = store.get("459", file_name="1.xml")
    assert state.loco_id == "459"
    assert state.cv_values == {"29": 38}
    # Repeated get without filename still finds the cached entry.
    assert store.get("459").cv_values == {"29": 38}


def test_filename_basename_only(tmp_path: pathlib.Path) -> None:
    """A malicious ``file_name="../escape.xml"`` must not escape roster/."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    # Bootstrap the target under the sanitised name so the write has a
    # locomotive-config to update.
    _write_locomotive_config_with_values(profile, "X", {}, file_name="escape.xml")
    store = LocoCvStore(profile=profile)
    store.set_cv("X", "1", 3, file_name="../escape.xml")
    # File stays inside roster_dir (basename stripped path components).
    assert (profile.roster_dir / "escape.xml").exists()
    assert not (tmp_path / "escape.xml").exists()


# ----- JMRI-canonical roster/<id>.xml <values> source -----


def _write_locomotive_config_with_values(
    profile: Profile, loco_id: str, cv_values: dict[str, int],
    *, file_name: str | None = None,
) -> pathlib.Path:
    """Write a JMRI-canonical ``roster/<id>.xml`` (locomotive-config) file
    with an embedded ``<values>`` block."""
    stem = (file_name or f"{loco_id}.xml")
    path = profile.roster_dir / pathlib.Path(stem).name
    profile.roster_dir.mkdir(parents=True, exist_ok=True)
    cv_rows = "\n".join(
        f'<CV name="{k}" value="{v}" />' for k, v in cv_values.items()
    )
    path.write_bytes(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<locomotive-config>
  <locomotive id="{loco_id}" dccAddress="{loco_id}">
    <decoder model="M" family="F" />
    <values><CVvalue>{cv_rows}</CVvalue></values>
  </locomotive>
</locomotive-config>""".encode()
    )
    return path


def test_get_reads_from_jmri_canonical_roster_values_block(tmp_path: pathlib.Path) -> None:
    """The primary CV state lives inside ``<profile>/roster/<id>.xml``'s
    ``<values>`` block on real JMRI installs -- the store must read from
    there, not just from our proprietary ``rosters/`` layout."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {"1": 3, "29": 38})
    store = LocoCvStore(profile=profile)
    state = store.get("459")
    assert state.cv_values == {"1": 3, "29": 38}


def test_get_prefers_canonical_when_only_canonical_present(tmp_path: pathlib.Path) -> None:
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {"1": 3})
    # No legacy file at all.
    store = LocoCvStore(profile=profile)
    state = store.get("459")
    assert state.cv_values == {"1": 3}


def test_get_falls_back_to_legacy_when_canonical_has_no_values(tmp_path: pathlib.Path) -> None:
    """Roster entry exists but with no ``<values>`` yet, and legacy has
    data -> legacy wins so users don't lose CV state we already wrote."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    profile.roster_dir.mkdir(parents=True)
    (profile.roster_dir / "459.xml").write_bytes(
        b"""<?xml version="1.0"?>
<locomotive-config><locomotive id="459" dccAddress="459">
<decoder model="M" family="F" /></locomotive></locomotive-config>"""
    )
    profile.rosters_cv_dir.mkdir(parents=True)
    (profile.rosters_cv_dir / "459.xml").write_bytes(
        b"""<?xml version="1.0"?>
<locomotive-cv-values id="459"><CV name="1" value="7" /></locomotive-cv-values>"""
    )
    store = LocoCvStore(profile=profile)
    state = store.get("459")
    assert state.cv_values == {"1": 7}


def test_get_raises_ambiguous_when_both_sources_have_cvs(tmp_path: pathlib.Path) -> None:
    """A loco with CV state in *both* JMRI-canonical AND legacy locations
    forces the UI to ask the user which is authoritative."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {"1": 3})
    profile.rosters_cv_dir.mkdir(parents=True)
    (profile.rosters_cv_dir / "459.xml").write_bytes(
        b"""<?xml version="1.0"?>
<locomotive-cv-values id="459"><CV name="1" value="99" /></locomotive-cv-values>"""
    )
    store = LocoCvStore(profile=profile)
    with pytest.raises(AmbiguousCvSourceError) as exc:
        store.get("459")
    assert exc.value.loco_id == "459"
    assert "roster" in str(exc.value.roster_path)
    assert "rosters" in str(exc.value.legacy_path)


def test_set_preferred_source_resolves_ambiguity(tmp_path: pathlib.Path) -> None:
    """After the UI asks the user which source to use, the store honours
    the pick on the next read -- no more ``AmbiguousCvSourceError``."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {"1": 3})
    profile.rosters_cv_dir.mkdir(parents=True)
    (profile.rosters_cv_dir / "459.xml").write_bytes(
        b"""<?xml version="1.0"?>
<locomotive-cv-values id="459"><CV name="1" value="99" /></locomotive-cv-values>"""
    )
    store = LocoCvStore(profile=profile)
    store.set_preferred_source("459", CvSource.LEGACY)
    assert store.get("459").cv_values == {"1": 99}
    # And switching preference re-reads the other source.
    store.set_preferred_source("459", CvSource.ROSTER)
    assert store.get("459").cv_values == {"1": 3}


def test_default_lookup_drops_cvs_matching_the_default(tmp_path: pathlib.Path) -> None:
    """When the catalog default matches the in-memory value, the CV
    must NOT be written to ``roster/<id>.xml <values>`` -- that's how
    the roster file stays a minimal delta per the spec."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {})
    # CV1 default = 3, CV29 default = 6. Write both -- only CV29 differs.
    store = LocoCvStore(
        profile=profile,
        default_lookup=lambda _id: {"1": "3", "29": "6"},
    )
    store.set_cv("459", "1", 3)     # equals default -> drop
    store.set_cv("459", "29", 38)   # differs -> keep
    raw = (profile.roster_dir / "459.xml").read_text()
    assert 'name="29"' in raw
    assert 'value="38"' in raw
    # CV1 equals its default -> filtered out.
    assert 'name="1"' not in raw


def test_record_read_result_updates_memory_but_does_not_touch_disk(
    tmp_path: pathlib.Path,
) -> None:
    """Reads must land in memory (so the pane re-render sees the fresh
    value) but must NOT persist to disk. Otherwise a Read pane over
    28 CVs would trigger 28 atomic disk writes on the roster file,
    beating up SD cards on Pi setups without a corresponding change to
    logical state -- the read result would already be there next time
    the decoder was queried."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "5", {"1": 5, "29": 2})
    baseline = (profile.roster_dir / "5.xml").read_bytes()

    store = LocoCvStore(profile=profile)
    store.record_read_result("5", "29", 99)

    # In-memory state reflects the read.
    assert store.get("5").cv_values["29"] == 99
    # Disk is untouched.
    assert (profile.roster_dir / "5.xml").read_bytes() == baseline
    # No .bak -- that's reserved for the first *user write*.
    assert not (profile.roster_dir / "5.xml.bak").exists()


def test_first_write_after_reads_still_creates_bak(
    tmp_path: pathlib.Path,
) -> None:
    """A session that reads many CVs then writes one: the reads don't
    trigger the ``.bak``, but the first user write does. The backup
    captures the pre-*modification* state, not the pre-*read* state
    (they're the same on disk, since reads don't persist)."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "5", {"1": 5})
    pristine = (profile.roster_dir / "5.xml").read_bytes()

    store = LocoCvStore(profile=profile)
    store.record_read_result("5", "29", 34)
    store.record_read_result("5", "17", 192)
    # No backup yet -- nothing on disk changed.
    assert not (profile.roster_dir / "5.xml.bak").exists()

    # First user write triggers the backup.
    store.set_cv("5", "1", 7)
    backup = profile.roster_dir / "5.xml.bak"
    assert backup.exists()
    assert backup.read_bytes() == pristine


def test_first_write_creates_bak_of_pre_session_file(
    tmp_path: pathlib.Path,
) -> None:
    """JMRI Java saves ``<id>.xml.bak`` before the first modification
    to a roster file per session; we mirror that. The .bak content is
    the byte-for-byte pre-write file so the user can roll back if the
    session corrupts state."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "5", {"1": 5, "29": 2})
    pristine = (profile.roster_dir / "5.xml").read_bytes()

    store = LocoCvStore(profile=profile)
    store.set_cv("5", "1", 7)

    backup = profile.roster_dir / "5.xml.bak"
    assert backup.exists()
    assert backup.read_bytes() == pristine


def test_subsequent_writes_do_not_overwrite_bak(
    tmp_path: pathlib.Path,
) -> None:
    """Only the FIRST write per session creates a .bak. Subsequent
    writes must not touch it -- otherwise the backup would race behind
    the current state and lose the pre-session baseline the user cares
    about rolling back to."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "5", {"1": 5})
    store = LocoCvStore(profile=profile)

    store.set_cv("5", "1", 7)
    backup = profile.roster_dir / "5.xml.bak"
    first_bak = backup.read_bytes()

    # Second write -- file has changed; .bak must NOT reflect that.
    store.set_cv("5", "29", 34)
    assert backup.read_bytes() == first_bak


def test_no_bak_when_source_file_does_not_exist(
    tmp_path: pathlib.Path,
) -> None:
    """A brand-new loco with no pre-existing roster file has nothing to
    back up. The write proceeds and the ``.bak`` doesn't materialise."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    # No _write_locomotive_config_with_values call -- the file just
    # doesn't exist yet.
    profile.roster_dir.mkdir(parents=True, exist_ok=True)
    store = LocoCvStore(profile=profile)
    store.set_cv("5", "1", 7)
    assert not (profile.roster_dir / "5.xml.bak").exists()


def test_bak_uses_fresh_source_when_filename_changes(
    tmp_path: pathlib.Path,
) -> None:
    """A store tracks backups per absolute path, so opening a second
    loco (different fileName) in the same session triggers a fresh
    ``.bak`` for that file."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "5", {"1": 5}, file_name="a.xml")
    _write_locomotive_config_with_values(profile, "6", {"1": 6}, file_name="b.xml")
    store = LocoCvStore(profile=profile)
    store.set_cv("5", "1", 7, file_name="a.xml")
    store.set_cv("6", "1", 8, file_name="b.xml")
    assert (profile.roster_dir / "a.xml.bak").exists()
    assert (profile.roster_dir / "b.xml.bak").exists()


def test_default_lookup_composed_default_byte_matches_no_write(
    tmp_path: pathlib.Path,
) -> None:
    """When the actual composed default byte (from combining several
    bit-mask variables) matches the current value, the roster must not
    persist that CV. Real-world example: CV 29 = 2 is the NMRA default
    for a stock decoder (Speed Steps=1, everything else 0). The
    programmer service composes this via ``variable_apply_to_state``
    and passes the resulting byte string to the store."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "L1", {})
    # Composed default byte for CV 29 = 2 (Speed Steps bit 1 set).
    store = LocoCvStore(
        profile=profile,
        default_lookup=lambda _id: {"29": "2"},
    )
    store.set_cv("L1", "29", 2)   # matches default byte -> skip write
    body = (profile.roster_dir / "L1.xml").read_bytes()
    assert b"<values>" not in body


def test_default_lookup_composed_default_byte_differs_writes_new_value(
    tmp_path: pathlib.Path,
) -> None:
    """When one bit-mask variable flips (Address Format 0->1), the
    composed CV 29 byte goes from 2 to 34. That differs from the
    catalog composed default (2), so persistence kicks in."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "L1", {})
    store = LocoCvStore(
        profile=profile,
        default_lookup=lambda _id: {"29": "2"},
    )
    store.set_cv("L1", "29", 34)   # user flipped Address Format bit
    body = (profile.roster_dir / "L1.xml").read_text()
    assert 'name="29"' in body
    assert 'value="34"' in body


def test_default_lookup_none_writes_every_cv(tmp_path: pathlib.Path) -> None:
    """When no default_lookup is configured (or it returns {}), we fall
    back to the pre-refactor behaviour: every in-memory CV gets written.
    Preserves correctness for setups where the catalog isn't loaded."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(profile, "459", {})
    store = LocoCvStore(profile=profile, default_lookup=None)
    store.set_cv("459", "1", 3)     # would-be-default but we don't know
    raw = (profile.roster_dir / "459.xml").read_text()
    assert 'name="1"' in raw


def test_get_honours_filename_attribute_for_roster_lookup(tmp_path: pathlib.Path) -> None:
    """JMRI's ``fileName`` attribute decouples loco-id from disk name;
    the canonical-source lookup honours it for the roster/ tree too."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    _write_locomotive_config_with_values(
        profile, "459", {"29": 38}, file_name="1.xml",
    )
    store = LocoCvStore(profile=profile)
    state = store.get("459", file_name="1.xml")
    assert state.cv_values == {"29": 38}
