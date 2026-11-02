"""``AuthManager`` -- the runtime auth surface.

Wraps the :class:`~jmri_server.auth.store.UsersFile` behind a small
API the middleware + FastAPI routes call into. Adds:

* Password login (returns matched user or None).
* Bearer-token login (constant-time compare against every user's
  personal token).
* Signed-session-cookie mint + verify -- uses
  :class:`~itsdangerous.URLSafeTimedSerializer` so a tampered
  cookie can't impersonate a user and expired cookies fall out on
  verify. The signing key is derived from the settings-root path
  (stable across restarts) unless the operator sets
  ``JMRI_AUTH_SECRET`` explicitly.
* Bootstrap: with auth enabled + no ``users.yaml`` present, mint an
  ``admin`` account with a random password + token, persist the
  password to ``<settings-root>/admin_password.txt`` (mode 0600),
  and log a WARNING pointing the operator at the file. This is the
  path a fresh install takes -- subsequent starts leave it alone.

The manager is deliberately synchronous: password hashing is CPU
work, not I/O, and putting it under ``async`` would just hide the
CPU spin behind a threadpool anyway. Callers await into the
middleware, then the middleware calls into these plain methods.
"""
from __future__ import annotations

import logging
import os
import pathlib
import secrets
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from jmri_server.auth.models import Role, User
from jmri_server.auth.passwords import hash_password, verify_password
from jmri_server.auth.store import UsersFile, load_users_file, save_users_file

_log = logging.getLogger(__name__)

USERS_FILE_NAME = "users.yaml"
ADMIN_PASSWORD_FILE = "admin_password.txt"
ADMIN_TOKEN_ENV = "JMRI_AUTH_ADMIN_TOKEN"
SECRET_ENV = "JMRI_AUTH_SECRET"
EPHEMERAL_SECRET_ENV = "JMRI_AUTH_EPHEMERAL_SECRET"

# One week -- matches JMRI mobile clients that stay logged in
# between operating sessions but forces re-auth after long gaps.
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A successful login carries the matched user.

    Kept as a distinct type so ``if login(...):`` reads naturally
    without confusing ``User``-vs-``None`` at the call site."""
    user: User


class AuthManager:
    """Load users, verify credentials, mint session cookies.

    One instance per server lifetime. Constructed by
    :func:`build_auth_manager` which handles the bootstrap +
    users-file discovery -- direct instantiation is fine for tests
    that seed a specific set of users.
    """

    def __init__(
        self,
        users: UsersFile,
        *,
        users_path: pathlib.Path,
        secret_key: str,
    ) -> None:
        self._users = users
        self._users_path = users_path
        self._serializer = URLSafeTimedSerializer(
            secret_key, salt="jmri-session-v1",
        )

    # ---- user lookup + password flow ----------------------------------

    def login_password(
        self, username: str, password: str,
    ) -> LoginResult | None:
        """Verify *password* for *username*. Returns the login
        result on success, ``None`` on any failure (unknown user,
        wrong password, malformed hash). Never leaks which of the
        two failed via return value -- the log line records it for
        the operator's audit."""
        user = self._users.by_username(username)
        if user is None:
            _log.info("auth: login failed for unknown user %r", username)
            return None
        if not verify_password(password, user.password_hash):
            _log.info("auth: bad password for user %r", username)
            return None
        return LoginResult(user=user)

    def login_token(self, token: str) -> LoginResult | None:
        """Match a Bearer token against every user's personal
        token. Returns the login result on success, ``None`` on
        no match. Empty token -> ``None`` without querying the
        store."""
        user = self._users.by_token(token)
        if user is None:
            return None
        return LoginResult(user=user)

    # ---- session cookies ----------------------------------------------

    def mint_session_cookie(self, user: User) -> str:
        """Encode *user* as a signed cookie value. The value round-
        trips through :meth:`verify_session_cookie`; anything else
        (tampered value, expired timestamp) returns ``None`` there."""
        return self._serializer.dumps({"username": user.username})

    def verify_session_cookie(self, cookie_value: str) -> User | None:
        """Decode + validate. Returns the current :class:`User` for
        the encoded username, or ``None`` for tampered / expired /
        deleted-user cases.

        Deleted-user handling matters: a user removed from
        ``users.yaml`` should be locked out immediately, not on
        session expiry. Re-checking the store on every verify is
        cheap (dict lookup) and closes that hole."""
        if not cookie_value:
            return None
        try:
            payload = self._serializer.loads(
                cookie_value, max_age=SESSION_MAX_AGE_SECONDS,
            )
        except SignatureExpired:
            return None
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        username = payload.get("username")
        if not isinstance(username, str):
            return None
        # Re-fetch so a role change (or deletion) after cookie
        # mint takes effect immediately.
        return self._users.by_username(username)

    # ---- introspection -------------------------------------------------

    @property
    def users(self) -> list[User]:
        """Read-only snapshot of the current user list. Mutations
        go through :meth:`add_user` / :meth:`remove_user` so the
        on-disk file stays in sync."""
        return list(self._users.users)

    def has_users(self) -> bool:
        return bool(self._users.users)

    def by_username(self, username: str) -> User | None:
        return self._users.by_username(username)

    # ---- mutations ------------------------------------------------------

    def add_user(
        self,
        username: str,
        password: str,
        *,
        role: Role = Role.VIEWER,
        token: str | None = None,
    ) -> User:
        """Create a new user. Persists to disk atomically.

        ``token`` defaults to a fresh random one -- a client (mobile
        throttle, etc.) can start using it immediately. Pass an
        explicit empty string to create a login-only account.
        """
        if not username:
            raise ValueError("username must be non-empty")
        if self._users.by_username(username) is not None:
            raise ValueError(f"user {username!r} already exists")
        actual_token = (
            token if token is not None else secrets.token_urlsafe(32)
        )
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            token=actual_token,
        )
        self._users.users.append(user)
        self._persist()
        return user

    def remove_user(self, username: str, *, current_username: str) -> None:
        """Delete *username*. Two guards:

        * Cannot delete yourself -- refuses when
          ``username == current_username`` so an admin can't
          accidentally lock the whole install out.
        * Cannot delete the last admin -- refuses when *username*
          is the sole entry with :data:`Role.ADMIN`. Otherwise a
          botched cleanup would leave the install with no admin
          user, which no future user-CRUD flow could recover from.
        """
        if username == current_username:
            raise ValueError("cannot delete your own account")
        target = self._users.by_username(username)
        if target is None:
            raise LookupError(f"no user {username!r}")
        if target.role is Role.ADMIN:
            admins = [u for u in self._users.users if u.role is Role.ADMIN]
            if len(admins) <= 1:
                raise ValueError(
                    "cannot delete the last admin -- promote someone else first",
                )
        self._users.users = [
            u for u in self._users.users if u.username != username
        ]
        self._persist()

    def set_password(self, username: str, new_password: str) -> None:
        """Update *username*'s password hash. Existing session
        cookies stay valid (they encode username only) -- a
        password change without a forced-logout is the JMRI
        Java convention and matches "remember me" behaviour."""
        user = self._users.by_username(username)
        if user is None:
            raise LookupError(f"no user {username!r}")
        user.password_hash = hash_password(new_password)
        self._persist()

    def set_role(
        self, username: str, new_role: Role, *, current_username: str,
    ) -> None:
        """Change *username*'s role. Same last-admin guard as
        :meth:`remove_user` -- demoting the only admin would
        strand the install."""
        if username == current_username and new_role is not Role.ADMIN:
            raise ValueError("cannot demote your own account")
        user = self._users.by_username(username)
        if user is None:
            raise LookupError(f"no user {username!r}")
        if user.role is Role.ADMIN and new_role is not Role.ADMIN:
            admins = [u for u in self._users.users if u.role is Role.ADMIN]
            if len(admins) <= 1:
                raise ValueError(
                    "cannot demote the last admin -- promote someone else first",
                )
        user.role = new_role
        self._persist()

    def regenerate_token(self, username: str) -> str:
        """Mint a new token for *username*, replacing the old.
        Returns the new value. Any mobile client using the old
        token is immediately locked out -- that's the point of
        this endpoint (a leaked token gets cycled)."""
        user = self._users.by_username(username)
        if user is None:
            raise LookupError(f"no user {username!r}")
        user.token = secrets.token_urlsafe(32)
        self._persist()
        return user.token

    def _persist(self) -> None:
        """Write the current users file to disk. Called after
        every mutation so an operator hand-editing users.yaml
        alongside the running server sees a coherent file."""
        save_users_file(self._users_path, self._users)


# ---- bootstrap ----------------------------------------------------------


def build_auth_manager(settings_root: pathlib.Path) -> AuthManager:
    """Load ``<settings-root>/users.yaml`` -- or mint a default
    admin if missing -- and return a fully-configured
    :class:`AuthManager`.

    Called once at server startup when
    ``JMRI_AUTH_ENABLED=1``. With auth off, callers should not
    invoke this; the server sets ``ctx.auth = None`` instead and
    the middleware becomes a no-op.
    """
    users_path = settings_root / USERS_FILE_NAME
    users = load_users_file(users_path)
    if not users.users:
        users = _bootstrap_default_admin(settings_root)
        save_users_file(users_path, users)

    secret = _resolve_secret(settings_root)
    return AuthManager(users, users_path=users_path, secret_key=secret)


def _resolve_secret(settings_root: pathlib.Path) -> str:
    """Determine the session signing key. Precedence:

    1. ``JMRI_AUTH_SECRET`` -- explicit operator override. Always wins.
       Rotating this value invalidates every outstanding session.
    2. ``JMRI_AUTH_EPHEMERAL_SECRET=1`` -- mint a fresh random secret
       at every server boot. All sessions die on restart, which is
       what you want in dev / when testing auth flows / on a shared
       machine where you don't want the "convenience persistence"
       across reboots.
    3. Derived stable key from the settings root -- the default.
       Sessions survive restarts (matches "remember me" semantics).
    """
    explicit = os.environ.get(SECRET_ENV)
    if explicit:
        return explicit
    if _get_bool_env(EPHEMERAL_SECRET_ENV):
        _log.info(
            "auth: %s=1 -- sessions will invalidate on every restart",
            EPHEMERAL_SECRET_ENV,
        )
        return secrets.token_hex(32)
    return _derive_secret_from_root(settings_root)


def _get_bool_env(name: str) -> bool:
    """Same truthy set as :mod:`jmri_core.env` -- kept local to avoid
    a circular import when the auth module is used standalone."""
    raw = os.environ.get(name, "")
    return raw.lower() in ("1", "true", "yes", "on")


def _bootstrap_default_admin(settings_root: pathlib.Path) -> UsersFile:
    """Mint an ``admin`` user with random password + token; write
    the plaintext password to ``<settings-root>/admin_password.txt``
    (mode 0600) so the operator can retrieve it on first start.

    On subsequent starts the users file already has entries, so
    this path never runs -- the ``admin_password.txt`` sticks
    around as a record of the initial secret until the operator
    deletes it or changes the admin password."""
    password = secrets.token_urlsafe(24)
    token = os.environ.get(ADMIN_TOKEN_ENV) or secrets.token_urlsafe(32)
    admin = User(
        username="admin",
        password_hash=hash_password(password),
        role=Role.ADMIN,
        token=token,
    )
    _write_admin_password_file(settings_root, password, token)
    _log.warning(
        "auth bootstrap: no users.yaml found. Default admin user created; "
        "password written to %s (mode 0600). Log in as 'admin' and change "
        "the password via a future user CRUD flow, or hand-edit users.yaml.",
        settings_root / ADMIN_PASSWORD_FILE,
    )
    return UsersFile(users=[admin])


def _write_admin_password_file(
    settings_root: pathlib.Path, password: str, token: str,
) -> None:
    """Persist the bootstrap admin credentials so the operator can
    read them on first login. Includes the API token too -- mobile
    clients need it to connect without a browser round-trip."""
    settings_root.mkdir(parents=True, exist_ok=True)
    path = settings_root / ADMIN_PASSWORD_FILE
    body = (
        "# JMRI auth bootstrap credentials -- first-run only.\n"
        "# Delete this file after you've copied the values into a\n"
        "# password manager. Regeneration requires deleting the users\n"
        "# entry as well (there's no re-bootstrap flow yet).\n"
        f"admin_password: {password}\n"
        f"admin_token: {token}\n"
    )
    path.write_text(body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except (OSError, PermissionError) as e:  # pragma: no cover
        _log.debug("chmod on %s ignored: %s", path, e)


def _derive_secret_from_root(settings_root: pathlib.Path) -> str:
    """Derive a stable session-signing secret from the settings
    root path + host hostname when ``JMRI_AUTH_SECRET`` isn't set.

    Stable across restarts (so sessions survive), unique per
    install (so a leaked cookie from one Pi doesn't unlock
    another). For a production deployment with real users the
    operator should set ``JMRI_AUTH_SECRET`` explicitly -- this
    fallback exists so single-Pi hobby installs don't need an
    extra config step."""
    import hashlib  # noqa: PLC0415
    import socket  # noqa: PLC0415
    seed = f"{settings_root.resolve()}::{socket.gethostname()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()
