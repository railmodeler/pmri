"""``write_with_backup`` -- rolling .bak rotation + atomic write.

Pins the JMRI-shaped backup convention:

* First write to a path creates just the primary (no .bak sibling).
* Subsequent writes roll the previous content to .bak -> .bak1 -> .bak2.
* The series tops out at ``keep - 1`` numbered rolls (keep=4 means
  .bak, .bak1, .bak2); older generations are dropped.
* Same-content writes don't disturb the backup series.
* Mid-write safety: the primary is renamed to .bak BEFORE the new
  content lands, so a crash between the rename and the final rename
  leaves the operator a recoverable .bak.
"""
from __future__ import annotations

import pathlib

from jmri_core.persistence.backup import write_with_backup


def test_first_write_creates_primary_only(tmp_path: pathlib.Path) -> None:
    """A fresh file has no history, so no .bak is created."""
    f = tmp_path / "data.xml"
    write_with_backup(f, b"<v>1</v>")
    assert f.read_bytes() == b"<v>1</v>"
    assert not (tmp_path / "data.xml.bak").exists()
    assert not (tmp_path / "data.xml.bak1").exists()


def test_second_write_creates_bak(tmp_path: pathlib.Path) -> None:
    """Second write: previous content lands in .bak."""
    f = tmp_path / "data.xml"
    write_with_backup(f, b"<v>1</v>")
    write_with_backup(f, b"<v>2</v>")
    assert f.read_bytes() == b"<v>2</v>"
    assert (tmp_path / "data.xml.bak").read_bytes() == b"<v>1</v>"


def test_four_writes_fill_the_series(tmp_path: pathlib.Path) -> None:
    """Four writes (default keep=4) populate: primary + .bak + .bak1 + .bak2.
    Each slot carries the content from exactly one write prior."""
    f = tmp_path / "data.xml"
    for n in (1, 2, 3, 4):
        write_with_backup(f, f"<v>{n}</v>".encode())
    assert f.read_bytes() == b"<v>4</v>"
    assert (tmp_path / "data.xml.bak").read_bytes() == b"<v>3</v>"
    assert (tmp_path / "data.xml.bak1").read_bytes() == b"<v>2</v>"
    assert (tmp_path / "data.xml.bak2").read_bytes() == b"<v>1</v>"
    # .bak3 should not exist yet (keep=4 means 3 numbered rolls max,
    # and the oldest content only reaches .bak2 on the fourth write).
    assert not (tmp_path / "data.xml.bak3").exists()


def test_fifth_write_drops_oldest(tmp_path: pathlib.Path) -> None:
    """keep=4: write #5 pushes the oldest off the end. The content
    from write #1 is lost; the window now holds writes 2..5."""
    f = tmp_path / "data.xml"
    for n in (1, 2, 3, 4, 5):
        write_with_backup(f, f"<v>{n}</v>".encode())
    assert f.read_bytes() == b"<v>5</v>"
    assert (tmp_path / "data.xml.bak").read_bytes() == b"<v>4</v>"
    assert (tmp_path / "data.xml.bak1").read_bytes() == b"<v>3</v>"
    assert (tmp_path / "data.xml.bak2").read_bytes() == b"<v>2</v>"
    # The oldest (write #1) is gone -- it would have landed in .bak3
    # on the fifth write but keep=4 drops it.
    assert not (tmp_path / "data.xml.bak3").exists()


def test_identical_content_skips_rotation(tmp_path: pathlib.Path) -> None:
    """A no-op save shouldn't walk the backup chain. Without this a
    'save on every tab change' GUI flow would blow the user's actual
    last-change history off the keep window within four edits."""
    f = tmp_path / "data.xml"
    write_with_backup(f, b"<v>1</v>")
    write_with_backup(f, b"<v>2</v>")
    # .bak now holds <v>1</v>. Re-saving the same <v>2</v> must not
    # rotate .bak into .bak1 and clobber the real history.
    write_with_backup(f, b"<v>2</v>")
    assert f.read_bytes() == b"<v>2</v>"
    assert (tmp_path / "data.xml.bak").read_bytes() == b"<v>1</v>"
    assert not (tmp_path / "data.xml.bak1").exists()


def test_keep_1_disables_history(tmp_path: pathlib.Path) -> None:
    """keep=1 (primary only, no history) is a valid shape and
    produces no .bak files even after many writes."""
    f = tmp_path / "data.xml"
    for n in (1, 2, 3):
        write_with_backup(f, f"<v>{n}</v>".encode(), keep=1)
    assert f.read_bytes() == b"<v>3</v>"
    assert not (tmp_path / "data.xml.bak").exists()


def test_keep_0_treated_as_1(tmp_path: pathlib.Path) -> None:
    """``keep=0`` is nonsensical (no file at all would be weird) --
    we treat it as 1 to keep the invariant 'the primary always
    exists after a successful write.'"""
    f = tmp_path / "data.xml"
    write_with_backup(f, b"<v>1</v>", keep=0)
    assert f.read_bytes() == b"<v>1</v>"


def test_sparse_series_still_rotates_correctly(tmp_path: pathlib.Path) -> None:
    """If the operator deletes .bak1 manually, a later write must not
    rename a non-existent file (which would raise on some
    filesystems). Rotation should just skip missing slots."""
    f = tmp_path / "data.xml"
    write_with_backup(f, b"<v>1</v>")
    write_with_backup(f, b"<v>2</v>")
    write_with_backup(f, b"<v>3</v>")
    # Now have .bak (<v>2</v>) and .bak1 (<v>1</v>). Delete .bak1
    # manually -- simulating an operator cleanup.
    (tmp_path / "data.xml.bak1").unlink()
    # Next write must not raise.
    write_with_backup(f, b"<v>4</v>")
    assert f.read_bytes() == b"<v>4</v>"
    assert (tmp_path / "data.xml.bak").read_bytes() == b"<v>3</v>"
    assert (tmp_path / "data.xml.bak1").read_bytes() == b"<v>2</v>"


def test_creates_parent_dir(tmp_path: pathlib.Path) -> None:
    """A write to a path whose parent doesn't exist should create the
    parent -- so a fresh install's first ``pmri.xml`` save works
    without an explicit ``mkdir`` beforehand."""
    f = tmp_path / "nested" / "deep" / "data.xml"
    write_with_backup(f, b"<v>1</v>")
    assert f.read_bytes() == b"<v>1</v>"


def test_leaves_no_tmp_file_on_success(tmp_path: pathlib.Path) -> None:
    """The atomic-write staging file (``data.xml.tmp``) must not
    survive a successful write. Operators seeing a stale ``.tmp``
    file would reasonably assume a crash."""
    f = tmp_path / "data.xml"
    write_with_backup(f, b"<v>1</v>")
    assert not (tmp_path / "data.xml.tmp").exists()
