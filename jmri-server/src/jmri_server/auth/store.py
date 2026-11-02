"""Load / save the ``users.yaml`` file at ``<settings-root>/users.yaml``.

YAML shape::

    users:
      - username: admin
        password_hash: "$2b$12$..."
        role: admin
        token: "8f3a..."   # optional; empty string when not present
      - username: alice
        password_hash: "$2b$12$..."
        role: operator
        token: ""

Load / save round-trip preserves user order so a human editor doesn't
see their carefully-organised file reshuffle on the first save. Atomic
write (temp file + replace) so a crashed save can't leave a truncated
file.
"""
from __future__ import annotations

import logging
import os
import pathlib
import tempfile
from typing import Any

import yaml
from pydantic import BaseModel, Field

from jmri_server.auth.models import User

_log = logging.getLogger(__name__)


class UsersFile(BaseModel):
    """Top-level ``users.yaml`` shape."""

    users: list[User] = Field(default_factory=list)

    def by_username(self, username: str) -> User | None:
        """Case-sensitive lookup. Returns ``None`` for unknown user.

        JMRI usernames are treated as opaque identifiers; we do not
        lowercase for lookup because doing so silently hides the
        real difference between ``Admin`` and ``admin`` and
        confuses log-line audits."""
        for u in self.users:
            if u.username == username:
                return u
        return None

    def by_token(self, token: str) -> User | None:
        """Constant-time-ish lookup by API token.

        Bounded by user count and skips empty tokens (a user with
        no token gets no matches). ``hmac.compare_digest`` guards
        against timing side channels on the compare -- matters when
        the caller can flood the endpoint with guesses."""
        if not token:
            return None
        import hmac  # noqa: PLC0415
        for u in self.users:
            if u.token and hmac.compare_digest(u.token, token):
                return u
        return None


def load_users_file(path: str | pathlib.Path) -> UsersFile:
    """Load *path* into a :class:`UsersFile`. Missing -> empty file.

    Malformed YAML raises ``yaml.YAMLError``; the caller decides
    whether to keep starting (empty users file -> bootstrap admin)
    or crash out (edited file corrupt -> refuse to start with auth
    on, since silently ignoring would be worse than a loud stop).
    """
    p = pathlib.Path(path)
    if not p.is_file():
        return UsersFile(users=[])
    raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    if raw is None:
        return UsersFile(users=[])
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: expected a mapping at the top level, got {type(raw).__name__}"
        )
    return UsersFile.model_validate(raw)


def save_users_file(path: str | pathlib.Path, file: UsersFile) -> None:
    """Atomically write *file* to *path* with mode 0600 on POSIX.

    Uses temp file + rename so a crashed save can't leave the
    file half-written. On Windows, the ``chmod`` is a no-op (the
    OS ACL isn't representable through the same mode bits); the
    settings root should be user-only anyway.
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # ``mode="json"`` converts the ``Role`` enum to its string value --
    # pyyaml's safe_dump can't represent StrEnum instances directly,
    # and even if it could, we want the on-disk shape to be plain
    # strings so a human editor doesn't see ``!!python/object:``
    # references.
    payload = {"users": [u.model_dump(mode="json") for u in file.users]}
    fd, tmp = tempfile.mkstemp(
        prefix=".users.yaml.", dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=False)
        # Best-effort mode 0600 -- matters on multi-user hosts, no-op
        # on Windows where the ACL model differs.
        try:
            os.chmod(tmp, 0o600)
        except (OSError, PermissionError) as e:  # pragma: no cover
            _log.debug("chmod on %s ignored: %s", tmp, e)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
