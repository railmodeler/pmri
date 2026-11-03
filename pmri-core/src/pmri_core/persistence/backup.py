"""``write_with_backup`` -- atomic file write with a rolling .bak series.

The convention matches JMRI Java's for its own XML state files: before
overwriting ``file.xml``, rename the existing one to ``file.xml.bak``;
if a prior ``.bak`` exists, roll it to ``.bak1``; and so on, keeping
*N* generations. ``.bak1`` through ``.bak<N-1>`` sit alongside the
primary file for easy discovery and manual recovery.

The write itself is atomic via a temp-file + rename: content lands in
``<file>.tmp`` first, then renames onto the final name. A crash
mid-write leaves the previous version intact; a crash between the
backup-rotate step and the final rename leaves a complete ``.bak``
with the old content so the operator can rename it back.

Sequence (``keep=4``, meaning: current + ``.bak``, ``.bak1``, ``.bak2``,
``.bak3`` -- four generations of history):

1. Rotate up: drop ``.bak3`` if present; ``.bak2`` -> ``.bak3``;
   ``.bak1`` -> ``.bak2``; ``.bak`` -> ``.bak1``.
2. Rename ``file`` -> ``file.bak`` (if ``file`` exists).
3. Write new content to ``file.tmp`` and rename it to ``file``.

If step 1 or 2 fails (file locked, permission denied), the exception
propagates and the new content is NOT written. If step 3's temp-write
fails after step 2 succeeded, the operator still has the previous
content as ``.bak``; the GUI surfaces the exception and the file is
in a recoverable state.
"""
from __future__ import annotations

import os
import pathlib

DEFAULT_KEEP = 4
"""JMRI's default history depth. One primary + three ``.bakN`` makes
four distinct states of the file reachable without going to a
version-control backup."""


def write_with_backup(
    path: str | pathlib.Path,
    data: bytes,
    *,
    keep: int = DEFAULT_KEEP,
) -> None:
    """Overwrite *path* with *data*, rotating the previous version into
    a ``.bak`` series.

    *keep* is the total number of generations (including the primary);
    ``keep=4`` means the primary plus three rolls (``.bak``, ``.bak1``,
    ``.bak2``). ``keep`` must be >= 1 (just the primary, no history),
    though 0 is accepted and treated as 1.

    No-op when *data* is identical to the current file contents --
    skips the rotation. This keeps "save every edit" GUI flows from
    churning the backup series when the user hasn't actually changed
    anything.
    """
    p = pathlib.Path(path)
    keep = max(1, int(keep))
    # Short-circuit: if the file already matches the new content,
    # don't disturb the backup chain. This matters for the GUI "save
    # on every tab change" pattern -- without this the user's actual
    # last change would walk itself off the end of the ``keep`` window.
    try:
        if p.exists() and p.read_bytes() == data:
            return
    except OSError:
        # Transient read failure -- fall through and do the normal
        # rotate-and-write. Worst case is a spurious backup roll.
        pass

    p.parent.mkdir(parents=True, exist_ok=True)
    _rotate_bak_series(p, keep)
    _atomic_write(p, data)


def _rotate_bak_series(primary: pathlib.Path, keep: int) -> None:
    """Shift the ``.bak`` series up one slot, dropping the one that
    would overflow the ``keep`` window, then promote ``primary`` to
    ``.bak``.

    With *keep* total generations (primary included), the valid
    ``.bak`` indexes are 0 (``.bak``) through ``keep - 2`` (so
    ``keep=4`` -> ``.bak``, ``.bak1``, ``.bak2`` -- three numbered
    slots alongside the primary). The sequence is:

    1. Drop the slot that would overflow: ``.bak{keep-2}``.
    2. Shift middles up: ``.bak{keep-3}`` -> ``.bak{keep-2}``, down
       to ``.bak1`` -> ``.bak2``.
    3. ``.bak`` -> ``.bak1`` (when a ``.bak1`` slot exists, i.e.
       keep >= 3).
    4. Primary -> ``.bak``.

    ``keep <= 1`` short-circuits -- no history, primary only.
    """
    if keep <= 1:
        return
    # (1) Oldest final-state slot is .bak{keep-2}; drop whatever
    # occupies it so step (2)'s rename into that slot doesn't fail
    # (on Windows ``rename`` to an existing target raises).
    oldest = _bak_path(primary, keep - 2)
    if oldest.exists():
        oldest.unlink()
    # (2) Shift the numbered middles. For keep=4 the loop is just
    # i=1 (.bak1 -> .bak2); for keep=5 it's i=2, i=1; etc.
    for i in range(keep - 3, 0, -1):
        src = _bak_path(primary, i)
        if src.exists():
            src.rename(_bak_path(primary, i + 1))
    # (3) .bak -> .bak1 when a .bak1 slot exists.
    current_bak = _bak_path(primary, 0)
    if keep >= 3 and current_bak.exists():
        current_bak.rename(_bak_path(primary, 1))
    # (4) Promote the primary to .bak. The primary is unlinked
    # above only when it was itself .bak{keep-2} (not possible for
    # the primary), so this rename always has a free target.
    if primary.exists():
        primary.rename(current_bak)


def _bak_path(primary: pathlib.Path, index: int) -> pathlib.Path:
    """Return the ``.bak`` sibling at *index*: 0 -> ``.bak``, 1 ->
    ``.bak1``, 2 -> ``.bak2``, etc."""
    suffix = ".bak" if index == 0 else f".bak{index}"
    return primary.with_name(primary.name + suffix)


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    """Write *data* to ``path`` via a sibling tempfile + rename.

    The tempfile lives at ``path.tmp`` to keep the write on the same
    filesystem (rename is only atomic across the same mount). Any
    pre-existing ``path.tmp`` from a prior crashed write is clobbered.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    # os.replace is atomic + overwrites the target. ``pathlib.rename``
    # on Windows raises if the destination exists; replace is the
    # cross-platform shape.
    os.replace(tmp, path)
