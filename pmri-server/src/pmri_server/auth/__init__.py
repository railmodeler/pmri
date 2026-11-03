"""Authentication + authorization for the JSON API and web UI.

**Turn-on**: set ``JMRI_AUTH_ENABLED=1`` in the environment before
starting the server. With auth off (the default), the server behaves
as it did before -- every route open, no login required. That keeps
existing single-user-on-LAN Pi deployments working without change.

**Users** live in ``<settings-root>/users.yaml`` -- one entry per
user with a bcrypt-hashed password + a personal API token + one of
three roles (``viewer`` / ``operator`` / ``admin``). See
:class:`~pmri_server.auth.models.User`.

**Authentication mechanisms**:

* **Session cookie** (browser). Set by ``/login`` on password
  verify; carries a signed user identity. Cleared by ``/logout``.
* **Bearer token** (JSON API + WiThrottle). Long-lived per-user
  token in ``users.yaml``; the client presents it in the
  ``Authorization: Bearer <token>`` header.

**Authorization** is by role:

* ``viewer`` -- can ``get`` / ``list`` any topic. No mutations.
* ``operator`` -- everything ``viewer`` can, plus ``post`` /
  ``put`` / ``delete`` on beans, throttles, programmer, roster,
  panels, LogixNG, scripts.
* ``admin`` -- everything, plus ``configProfile`` /
  ``beanTransfer`` / (future) ``configUser``.

**Bootstrap**: with auth enabled but ``users.yaml`` missing, the
:class:`~pmri_server.auth.manager.AuthManager` mints an ``admin``
user with a random 32-byte password + token, writes the plaintext
password to ``<settings-root>/admin_password.txt`` (mode 0600), and
logs a one-time WARNING telling the operator to pick it up. On next
startup the file is left alone -- deleting it manually is fine.

For the JMRI Java-API-compatible token flow, see the docstrings on
:class:`~pmri_server.auth.manager.AuthManager` and the middleware.
"""
from pmri_server.auth.manager import (
    ADMIN_PASSWORD_FILE,
    ADMIN_TOKEN_ENV,
    EPHEMERAL_SECRET_ENV,
    SECRET_ENV,
    SESSION_MAX_AGE_SECONDS,
    USERS_FILE_NAME,
    AuthManager,
    LoginResult,
    build_auth_manager,
)
from pmri_server.auth.models import (
    AUTH_ENABLED_ENV,
    Role,
    User,
)
from pmri_server.auth.passwords import (
    hash_password,
    verify_password,
)
from pmri_server.auth.store import (
    UsersFile,
    load_users_file,
    save_users_file,
)

__all__ = [
    "ADMIN_PASSWORD_FILE",
    "ADMIN_TOKEN_ENV",
    "AUTH_ENABLED_ENV",
    "AuthManager",
    "EPHEMERAL_SECRET_ENV",
    "LoginResult",
    "Role",
    "SECRET_ENV",
    "SESSION_MAX_AGE_SECONDS",
    "USERS_FILE_NAME",
    "User",
    "UsersFile",
    "build_auth_manager",
    "hash_password",
    "load_users_file",
    "save_users_file",
    "verify_password",
]
