"""Auth data models -- Role enum, User record, env constant.

Kept in their own module so tests + downstream code can import them
without pulling bcrypt / YAML transitively. Password hashing lives
in :mod:`~jmri_server.auth.passwords`; the YAML file layer lives in
:mod:`~jmri_server.auth.store`.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


# Env var name kept as a module constant so tests can flip it via
# ``monkeypatch.setenv`` without importing anything heavy.
AUTH_ENABLED_ENV = "JMRI_AUTH_ENABLED"


class Role(StrEnum):
    """Access role for one user.

    Ordered by privilege (viewer < operator < admin). The
    comparison operators are defined via :meth:`_priority` so
    ``request.state.role >= Role.OPERATOR`` reads naturally on the
    dispatch path.
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    def _priority(self) -> int:
        return {"viewer": 0, "operator": 1, "admin": 2}[self.value]

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Role):
            return NotImplemented
        return self._priority() >= other._priority()

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Role):
            return NotImplemented
        return self._priority() > other._priority()

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Role):
            return NotImplemented
        return self._priority() <= other._priority()

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Role):
            return NotImplemented
        return self._priority() < other._priority()


class User(BaseModel):
    """One user record -- matches one entry in ``users.yaml``.

    ``password_hash`` is bcrypt-encoded; verify via
    :func:`~jmri_server.auth.passwords.verify_password`. ``token``
    is the long-lived personal API token -- regenerated on demand,
    never derived from the password so a password change doesn't
    invalidate mobile clients.

    ``token`` may be empty for a user that only logs in via the
    web UI. The bootstrap admin always gets one so JMRI mobile
    clients can be connected without a browser round-trip.
    """

    model_config = ConfigDict(frozen=False, extra="ignore")

    username: str
    password_hash: str
    role: Role = Role.VIEWER
    token: str = ""
