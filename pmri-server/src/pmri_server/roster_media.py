"""Roster media resolver.

:class:`~pmri_core.beans.roster_entry.RosterEntry` carries two path
fields -- ``image_file_path`` (large photo) and ``icon_file_path``
(thumbnail). Both may be absolute or relative to a base directory
under the active profile. This module resolves them to an on-disk
:class:`pathlib.Path`, rejecting anything that would escape the
profile root (a scripted CV file pointing at ``../../etc/passwd``
should not become a media URL).

The actual HTTP serving lives in :mod:`pmri_server.routes` -- this
module only does the path resolution + safety check. Kept separate so
the resolver has tests independent of the FastAPI test client.
"""
from __future__ import annotations

import mimetypes
import pathlib

from pmri_core.beans.roster_entry import RosterEntry
from pmri_core.persistence.profile import Profile


def _guess_type(path: pathlib.Path) -> str:
    """Best-effort content-type for the served file. Falls back to
    ``application/octet-stream`` for unrecognized extensions -- safer
    than lying about the mime and letting the browser sniff."""
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "application/octet-stream"


def _candidate_bases(profile: Profile) -> list[pathlib.Path]:
    """Directories a relative roster path may be resolved against.

    JMRI's Java writer stores paths relative to the profile's
    ``resources/`` dir (photos) or the profile root itself (older
    layouts). We check both so pre-existing rosters imported from a
    real JMRI install work without editing paths.
    """
    return [
        profile.profile_dir / "resources",
        profile.profile_dir,
        profile.root,
    ]


def _under(root: pathlib.Path, candidate: pathlib.Path) -> bool:
    """True if *candidate* is contained under *root*. Both must be
    resolved to real absolute paths first -- otherwise a symlink or
    ``..`` component could sneak past the string prefix check."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_media_path(
    profile: Profile,
    raw: str | None,
) -> pathlib.Path | None:
    """Turn a stored ``image_file_path`` / ``icon_file_path`` into a
    real on-disk file, or ``None`` if the string is empty, points
    somewhere that doesn't exist, or would escape the profile.

    Absolute paths pass through untouched (after existence and
    containment checks). Relative paths try each candidate base in
    order; the first match wins.

    The containment check is the security-relevant bit: any resolved
    path that isn't under the profile root -- or, for absolute paths,
    isn't a normal file -- is rejected as ``None``. That prevents a
    hostile roster XML from pointing ``imageFilePath`` at
    ``/etc/passwd`` and having the server serve it.
    """
    if not raw:
        return None
    raw_str = str(raw).strip()
    if not raw_str:
        return None
    path = pathlib.Path(raw_str).expanduser()

    if path.is_absolute():
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            return None
        if not resolved.is_file():
            return None
        try:
            profile_root = profile.root.resolve()
        except OSError:
            return None
        if not _under(profile_root, resolved):
            # Absolute paths that escape the profile are refused --
            # rosters exported from a foreign JMRI install may carry
            # arbitrary absolute paths; we won't serve them.
            return None
        return resolved

    for base in _candidate_bases(profile):
        try:
            base_resolved = base.resolve()
        except OSError:
            continue
        try:
            candidate = (base_resolved / path).resolve(strict=False)
        except OSError:
            continue
        if not _under(base_resolved, candidate):
            # ``../../foo`` climbed past the base -- skip; try the
            # next base rather than trusting this resolution.
            continue
        if candidate.is_file():
            return candidate
    return None


def media_url_for(entry: RosterEntry, kind: str) -> str:
    """Build the HTTP path a client fetches to get this piece of media.

    Callers should have already confirmed the file exists via
    :func:`resolve_media_path` -- this helper just constructs the
    URL. ``kind`` must be ``"image"`` or ``"icon"``.
    """
    if kind not in ("image", "icon"):
        raise ValueError(f"unknown roster media kind: {kind!r}")
    return f"/roster/{entry.id}/{kind}"


def media_content_type(path: pathlib.Path) -> str:
    """Publicly exposed content-type guesser for the media route."""
    return _guess_type(path)
