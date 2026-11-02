"""Unit tests for :mod:`jmri_server.roster_media`.

The path resolver is the security-critical piece -- a bad roster
XML shouldn't turn into an arbitrary-file-read endpoint. Tests here
pin the containment checks specifically.
"""
from __future__ import annotations

import pathlib

import pytest

from jmri_core.persistence.profile import Profile

from jmri_server.roster_media import (
    media_content_type,
    media_url_for,
    resolve_media_path,
)


@pytest.fixture()
def profile(tmp_path: pathlib.Path) -> Profile:
    p = Profile(root=tmp_path, name="test")
    p.ensure()
    return p


def _touch(path: pathlib.Path, content: bytes = b"data") -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---- happy paths --------------------------------------------------------


def test_relative_path_under_resources_dir(profile: Profile) -> None:
    """The canonical JMRI location -- ``<profile>/resources/img/*``."""
    _touch(profile.profile_dir / "resources" / "img" / "loco.png")
    resolved = resolve_media_path(profile, "img/loco.png")
    assert resolved is not None
    assert resolved.name == "loco.png"


def test_relative_path_under_profile_dir(profile: Profile) -> None:
    """Older layouts store images directly under the profile root."""
    _touch(profile.profile_dir / "photos" / "unit1.jpg")
    resolved = resolve_media_path(profile, "photos/unit1.jpg")
    assert resolved is not None


def test_absolute_path_inside_profile_root_accepted(
    profile: Profile,
) -> None:
    """Absolute paths are OK as long as they're inside the profile
    root -- the settings-root umbrella lets shared assets live at
    ``<settings>/shared/loco.png`` referenced by any profile."""
    target = _touch(profile.root / "shared" / "loco.png")
    resolved = resolve_media_path(profile, str(target))
    assert resolved == target


# ---- security / rejection paths -----------------------------------------


def test_empty_string_returns_none(profile: Profile) -> None:
    assert resolve_media_path(profile, "") is None
    assert resolve_media_path(profile, "   ") is None


def test_none_returns_none(profile: Profile) -> None:
    assert resolve_media_path(profile, None) is None


def test_missing_file_returns_none(profile: Profile) -> None:
    """The path exists in the roster but the underlying file doesn't --
    common after copying a profile without its ``resources/`` tree.
    We surface that as "no media", not a 500."""
    assert resolve_media_path(profile, "img/nope.png") is None


def test_absolute_path_outside_profile_rejected(
    profile: Profile, tmp_path: pathlib.Path,
) -> None:
    """A roster XML pointing ``imageFilePath`` at ``/etc/passwd`` (or
    an equivalent file outside the settings umbrella) must NOT
    resolve -- the server would otherwise become a general-purpose
    file-read gateway."""
    outside = _touch(tmp_path.parent / "OUTSIDE.png")
    try:
        assert resolve_media_path(profile, str(outside)) is None
    finally:
        outside.unlink(missing_ok=True)


def test_relative_traversal_rejected(profile: Profile) -> None:
    """``../../etc/passwd`` climbs above the resolution base -- must
    be refused even if such a file existed."""
    # We don't need the target to exist; the containment check
    # rejects before ``is_file`` fires.
    assert resolve_media_path(profile, "../../../etc/passwd") is None


def test_directory_not_a_file_rejected(profile: Profile) -> None:
    """A path that points at a directory (not a file) must return
    None -- content-type / streaming would blow up."""
    d = profile.profile_dir / "resources" / "notafile"
    d.mkdir(parents=True, exist_ok=True)
    assert resolve_media_path(profile, "notafile") is None


# ---- helpers ------------------------------------------------------------


def test_media_url_for_kind_validation() -> None:
    class _FakeEntry:
        id = "UP_3601"
    assert media_url_for(_FakeEntry(), "image") == "/roster/UP_3601/image"
    assert media_url_for(_FakeEntry(), "icon") == "/roster/UP_3601/icon"
    with pytest.raises(ValueError):
        media_url_for(_FakeEntry(), "video")


def test_media_content_type_infers_from_extension(
    tmp_path: pathlib.Path,
) -> None:
    """PNG / JPG / GIF each get their standard image types; an
    unknown extension falls back to octet-stream so browsers don't
    treat unrelated content as an image."""
    png = tmp_path / "x.png"
    png.write_bytes(b"")
    assert media_content_type(png) == "image/png"
    jpg = tmp_path / "x.jpg"
    jpg.write_bytes(b"")
    assert media_content_type(jpg) in ("image/jpeg", "image/jpg")
    unknown = tmp_path / "x.weird"
    unknown.write_bytes(b"")
    assert media_content_type(unknown) == "application/octet-stream"
