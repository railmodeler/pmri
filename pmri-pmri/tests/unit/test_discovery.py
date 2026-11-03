"""Active-profile discovery -- the Case-1 flow fix.

Covers the five-step priority chain documented in
:mod:`pmri_pmri.discovery`. The Case-1 regression (feedback item 4)
is explicitly pinned: a user with an existing JMRI install and a
``.active_profile`` marker gets their actual profile picked up, not
the Chunk-B naive "default" string.
"""
from __future__ import annotations

import pathlib
from typing import Any

from pmri_core.persistence import (
    PmriConfig,
    save_pmri_config,
)

from pmri_pmri.discovery import (
    ProfileSource,
    discover_active_profile,
)

# ---- fixtures -----------------------------------------------------------


def _write_profiles_xml(root: pathlib.Path, entries: list[tuple[str, str]]) -> None:
    """Write a profiles.xml with the given (id, dirname) list.

    Each dirname is also created as an empty directory so
    ``Profile.profile_dir.is_dir()`` succeeds in subsequent reads."""
    body = "".join(
        f'<profile id="{pid}" path="settings:{dirname}/"/>'
        for pid, dirname in entries
    )
    (root / "profiles.xml").write_text(
        f'<?xml version="1.0"?><profileConfig><profiles>{body}</profiles></profileConfig>',
        encoding="utf-8",
    )
    for _, dirname in entries:
        (root / dirname).mkdir(parents=True, exist_ok=True)


def _no_pick_called(_choices: list[Any]) -> Any:
    """Sentinel pick callback -- fails if invoked. Used in tests that
    assert the chain resolved WITHOUT prompting."""
    raise AssertionError("picker was called but discovery should have short-circuited")


# ---- step 1: pmri.xml override -----------------------------------------


def test_pmri_xml_override_wins_over_marker(tmp_path: pathlib.Path) -> None:
    """pmri.xml names ``DRGW`` -- that beats a ``.active_profile``
    marker pointing at ``UP``. The explicit operator override is the
    top rule in the chain."""
    _write_profiles_xml(tmp_path, [
        ("DRGW.xyz", "DRGW.jmri"),
        ("UP.xyz", "UP.jmri"),
    ])
    (tmp_path / ".active_profile").write_text("UP.xyz", encoding="utf-8")
    cfg = PmriConfig()
    cfg.active_profile = "DRGW.jmri"
    disc = discover_active_profile(tmp_path, cfg, pick=_no_pick_called)
    assert disc is not None
    assert disc.profile.name == "DRGW.jmri"
    assert disc.source is ProfileSource.PMRI_CONFIG
    assert disc.should_persist is False


def test_pmri_xml_override_missing_falls_through_to_marker(
    tmp_path: pathlib.Path,
) -> None:
    """pmri.xml names a profile that doesn't exist; the chain
    falls through to the ``.active_profile`` marker rather than
    aborting."""
    _write_profiles_xml(tmp_path, [("UP.xyz", "UP.jmri")])
    (tmp_path / ".active_profile").write_text("UP.xyz", encoding="utf-8")
    cfg = PmriConfig()
    cfg.active_profile = "GhostProfile"
    disc = discover_active_profile(tmp_path, cfg, pick=_no_pick_called)
    assert disc is not None
    assert disc.profile.name == "UP.jmri"
    assert disc.source is ProfileSource.MARKER


# ---- step 2: .active_profile marker (Case 1) ---------------------------


def test_case_1_marker_resolves_without_pmri_override(
    tmp_path: pathlib.Path,
) -> None:
    """The Case-1 regression: existing JMRI install with a
    ``.active_profile`` marker and NO pmri.xml should pick up the
    profile the marker names, not the Chunk-B naive "default"."""
    _write_profiles_xml(tmp_path, [
        ("DRGW.xyz", "DRGW.jmri"),
        ("UP.xyz", "UP.jmri"),
    ])
    (tmp_path / ".active_profile").write_text("DRGW.xyz", encoding="utf-8")
    # pmri.xml doesn't exist -- fresh PMRI install over a JMRI install.
    cfg = PmriConfig()  # default: active_profile == ""
    disc = discover_active_profile(tmp_path, cfg, pick=_no_pick_called)
    assert disc is not None
    assert disc.profile.name == "DRGW.jmri"
    assert disc.source is ProfileSource.MARKER
    assert disc.should_persist is False  # marker already exists


def test_marker_resolves_by_name_not_just_id(tmp_path: pathlib.Path) -> None:
    """JMRI stores the id in the marker, but some operators / tools
    write a bare name. Both resolve."""
    _write_profiles_xml(tmp_path, [("UP.xyz", "UP.jmri")])
    (tmp_path / ".active_profile").write_text("UP.jmri", encoding="utf-8")
    cfg = PmriConfig()
    disc = discover_active_profile(tmp_path, cfg, pick=_no_pick_called)
    assert disc is not None
    assert disc.profile.name == "UP.jmri"


def test_marker_pointing_at_nothing_falls_through(
    tmp_path: pathlib.Path,
) -> None:
    """A stale marker (profile was deleted) falls through to the
    single-entry / prompt / fallback chain rather than aborting."""
    _write_profiles_xml(tmp_path, [("UP.xyz", "UP.jmri")])
    (tmp_path / ".active_profile").write_text("DeletedProfile.xyz", encoding="utf-8")
    cfg = PmriConfig()
    disc = discover_active_profile(tmp_path, cfg, pick=_no_pick_called)
    # Only one profile in profiles.xml -- single-entry rule wins.
    assert disc is not None
    assert disc.profile.name == "UP.jmri"
    assert disc.source is ProfileSource.SINGLE_PROFILE


# ---- step 3: single-entry auto-pick ------------------------------------


def test_single_profile_auto_picks_and_persists_marker(
    tmp_path: pathlib.Path,
) -> None:
    """profiles.xml has exactly one entry and no marker exists ->
    auto-pick it, record to .active_profile so next launch is
    marker-driven (step 2)."""
    _write_profiles_xml(tmp_path, [("Only.xyz", "Only.jmri")])
    cfg = PmriConfig()
    disc = discover_active_profile(tmp_path, cfg, pick=_no_pick_called)
    assert disc is not None
    assert disc.profile.name == "Only.jmri"
    assert disc.source is ProfileSource.SINGLE_PROFILE
    assert disc.should_persist is True
    # Marker was written during discovery.
    assert (tmp_path / ".active_profile").read_text() == "Only.xyz"


# ---- step 4: multi-entry prompt ---------------------------------------


def test_multiple_profiles_prompts_and_persists_pick(
    tmp_path: pathlib.Path,
) -> None:
    """>1 profiles.xml entries and no marker + no override -> prompt.
    The pick is recorded to .active_profile."""
    _write_profiles_xml(tmp_path, [
        ("A.xyz", "A.jmri"),
        ("B.xyz", "B.jmri"),
    ])
    cfg = PmriConfig()
    picked_ref = []

    def _pick(choices):  # type: ignore[no-untyped-def]
        picked_ref.append(choices)
        # Return the second one so we know the pick threaded through.
        return choices[1]

    disc = discover_active_profile(tmp_path, cfg, pick=_pick)
    assert disc is not None
    assert disc.profile.name == "B.jmri"
    assert disc.source is ProfileSource.PICKED
    assert disc.should_persist is True
    assert (tmp_path / ".active_profile").read_text() == "B.xyz"
    # The picker was handed the full choice list, in profiles.xml order.
    assert len(picked_ref) == 1
    assert [c.name for c in picked_ref[0]] == ["A.jmri", "B.jmri"]


def test_multiple_profiles_cancelled_pick_returns_none(
    tmp_path: pathlib.Path,
) -> None:
    """Operator cancelled the prompt -> discovery returns None so
    the launcher aborts (vs. silently falling through to the
    wrong profile)."""
    _write_profiles_xml(tmp_path, [
        ("A.xyz", "A.jmri"), ("B.xyz", "B.jmri"),
    ])
    cfg = PmriConfig()
    disc = discover_active_profile(tmp_path, cfg, pick=lambda _choices: None)
    assert disc is None
    # Nothing was persisted.
    assert not (tmp_path / ".active_profile").exists()


def test_multiple_profiles_no_picker_supplied_aborts(
    tmp_path: pathlib.Path,
) -> None:
    """Test-harness guardrail: if nobody supplies a picker and the
    chain hits step 4, return None rather than hang or silently
    pick arbitrarily."""
    _write_profiles_xml(tmp_path, [
        ("A.xyz", "A.jmri"), ("B.xyz", "B.jmri"),
    ])
    cfg = PmriConfig()
    disc = discover_active_profile(tmp_path, cfg, pick=None)
    assert disc is None


# ---- step 5: fallback -------------------------------------------------


def test_fresh_install_falls_back_to_default_profile(
    tmp_path: pathlib.Path,
) -> None:
    """No profiles.xml, no marker, no override: Fresh install.
    Discovery returns a bare ``Profile(name="default")`` so the
    launcher boots -- fresh users don't need to see a prompt
    before seeing the GUI."""
    # tmp_path is empty.
    cfg = PmriConfig()
    disc = discover_active_profile(tmp_path, cfg, pick=_no_pick_called)
    assert disc is not None
    assert disc.profile.name == "default"
    assert disc.source is ProfileSource.FALLBACK
    assert disc.should_persist is False


# ---- integration with saved pmri.xml ---------------------------------


def test_saved_pmri_config_round_trips_through_discovery(
    tmp_path: pathlib.Path,
) -> None:
    """End-to-end: save a pmri.xml with an active_profile, round
    through load_pmri_config + discover_active_profile, confirm
    the override wins."""
    _write_profiles_xml(tmp_path, [
        ("DRGW.xyz", "DRGW.jmri"),
        ("UP.xyz", "UP.jmri"),
    ])
    cfg = PmriConfig()
    cfg.active_profile = "UP.jmri"
    save_pmri_config(tmp_path, cfg)
    # Re-load as the launcher would.
    from pmri_core.persistence import load_pmri_config  # noqa: PLC0415
    reloaded = load_pmri_config(tmp_path)
    disc = discover_active_profile(tmp_path, reloaded, pick=_no_pick_called)
    assert disc is not None
    assert disc.profile.name == "UP.jmri"
    assert disc.source is ProfileSource.PMRI_CONFIG
