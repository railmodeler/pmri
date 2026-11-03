"""AuthManager: password login, token login, session cookie
round-trip, bootstrap flow."""
from __future__ import annotations

import pathlib

import pytest

from pmri_server.auth import (
    ADMIN_PASSWORD_FILE,
    ADMIN_TOKEN_ENV,
    AuthManager,
    LoginResult,
    Role,
    SECRET_ENV,
    USERS_FILE_NAME,
    User,
    UsersFile,
    build_auth_manager,
    hash_password,
    load_users_file,
)


def _seeded_manager(tmp_path: pathlib.Path) -> AuthManager:
    """Manager with two users: admin (password 'ap', token 'atok')
    and viewer (password 'vp', token empty)."""
    users = UsersFile(users=[
        User(username="admin", password_hash=hash_password("ap"),
             role=Role.ADMIN, token="atok"),
        User(username="viewer", password_hash=hash_password("vp"),
             role=Role.VIEWER, token=""),
    ])
    return AuthManager(
        users,
        users_path=tmp_path / USERS_FILE_NAME,
        secret_key="test-secret-key-not-real",
    )


# ---- password login ----------------------------------------------------


def test_login_password_success(tmp_path: pathlib.Path) -> None:
    mgr = _seeded_manager(tmp_path)
    result = mgr.login_password("admin", "ap")
    assert isinstance(result, LoginResult)
    assert result.user.role == Role.ADMIN


def test_login_password_wrong_password(tmp_path: pathlib.Path) -> None:
    """Never leak "user exists" via return value -- wrong password
    and unknown user both return ``None``."""
    mgr = _seeded_manager(tmp_path)
    assert mgr.login_password("admin", "not-the-password") is None


def test_login_password_unknown_user(tmp_path: pathlib.Path) -> None:
    mgr = _seeded_manager(tmp_path)
    assert mgr.login_password("nobody", "ap") is None


# ---- token login -------------------------------------------------------


def test_login_token_success(tmp_path: pathlib.Path) -> None:
    mgr = _seeded_manager(tmp_path)
    result = mgr.login_token("atok")
    assert result and result.user.username == "admin"


def test_login_token_empty_matches_nothing(tmp_path: pathlib.Path) -> None:
    """The viewer has an empty token -- an empty-token bearer must
    NOT match them. Otherwise every unauth request would look
    like the first token-less user."""
    mgr = _seeded_manager(tmp_path)
    assert mgr.login_token("") is None


def test_login_token_unknown(tmp_path: pathlib.Path) -> None:
    mgr = _seeded_manager(tmp_path)
    assert mgr.login_token("not-a-real-token") is None


# ---- session cookies ---------------------------------------------------


def test_session_cookie_roundtrip(tmp_path: pathlib.Path) -> None:
    mgr = _seeded_manager(tmp_path)
    admin = mgr.login_password("admin", "ap").user
    cookie = mgr.mint_session_cookie(admin)
    recovered = mgr.verify_session_cookie(cookie)
    assert recovered is not None
    assert recovered.username == "admin"


def test_session_cookie_rejects_tampering(tmp_path: pathlib.Path) -> None:
    mgr = _seeded_manager(tmp_path)
    admin = mgr.users[0]
    cookie = mgr.mint_session_cookie(admin)
    # Flip the last char -- invalidates the signature.
    tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    assert mgr.verify_session_cookie(tampered) is None


def test_session_cookie_rejects_empty(tmp_path: pathlib.Path) -> None:
    mgr = _seeded_manager(tmp_path)
    assert mgr.verify_session_cookie("") is None


def test_session_cookie_deleted_user_returns_none(
    tmp_path: pathlib.Path,
) -> None:
    """Removing a user from the store immediately invalidates any
    outstanding session cookies. Otherwise a fired employee keeps
    their cookie for a week -- not acceptable."""
    mgr = _seeded_manager(tmp_path)
    viewer = mgr.users[1]
    cookie = mgr.mint_session_cookie(viewer)
    # Drop the viewer from the store (simulate an admin deletion).
    mgr._users = UsersFile(users=[u for u in mgr._users.users if u.username != "viewer"])
    assert mgr.verify_session_cookie(cookie) is None


# Note: cookie expiration behavior is provided by itsdangerous's
# URLSafeTimedSerializer -- we rely on their well-tested max_age
# check rather than duplicating a brittle real-clock test here.


# ---- bootstrap flow ----------------------------------------------------


def test_bootstrap_creates_admin_when_no_users_file(
    tmp_path: pathlib.Path,
) -> None:
    """First-run flow: empty settings root gets an admin user + a
    plaintext-credentials file. Subsequent starts leave the store
    alone."""
    mgr = build_auth_manager(tmp_path)
    assert (tmp_path / USERS_FILE_NAME).is_file()
    assert (tmp_path / ADMIN_PASSWORD_FILE).is_file()
    users = load_users_file(tmp_path / USERS_FILE_NAME)
    admin = users.by_username("admin")
    assert admin is not None
    assert admin.role == Role.ADMIN
    assert admin.token  # token is non-empty


def test_bootstrap_credentials_file_contains_password(
    tmp_path: pathlib.Path,
) -> None:
    """The plaintext file the operator retrieves must contain
    both the password and the token -- mobile clients need the
    latter."""
    build_auth_manager(tmp_path)
    content = (tmp_path / ADMIN_PASSWORD_FILE).read_text(encoding="utf-8")
    assert "admin_password:" in content
    assert "admin_token:" in content


def test_bootstrap_second_run_leaves_users_alone(
    tmp_path: pathlib.Path,
) -> None:
    """A second bootstrap on an already-populated store must not
    rewrite the file. Otherwise every restart resets the admin
    password -- catastrophic."""
    build_auth_manager(tmp_path)
    first_users = load_users_file(tmp_path / USERS_FILE_NAME)
    first_hash = first_users.by_username("admin").password_hash

    build_auth_manager(tmp_path)
    second_users = load_users_file(tmp_path / USERS_FILE_NAME)
    second_hash = second_users.by_username("admin").password_hash
    assert first_hash == second_hash


def test_bootstrap_admin_token_from_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``JMRI_AUTH_ADMIN_TOKEN`` is set, the bootstrap uses
    it as the admin's token. Useful for CI / provisioning where
    a known token needs to seed the first client."""
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "sentinel-token-from-env")
    build_auth_manager(tmp_path)
    users = load_users_file(tmp_path / USERS_FILE_NAME)
    assert users.by_username("admin").token == "sentinel-token-from-env"


def test_ephemeral_secret_rotates_between_boots(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``JMRI_AUTH_EPHEMERAL_SECRET=1`` a cookie minted in one
    boot can't verify in the next -- matches "sessions die on
    restart" contract."""
    from pmri_server.auth import EPHEMERAL_SECRET_ENV  # noqa: PLC0415
    monkeypatch.setenv(EPHEMERAL_SECRET_ENV, "1")

    mgr_boot1 = build_auth_manager(tmp_path)
    admin = mgr_boot1.users[0]
    cookie = mgr_boot1.mint_session_cookie(admin)

    # Simulated restart -- build a fresh manager. Different random
    # secret -> cookie no longer verifies.
    mgr_boot2 = build_auth_manager(tmp_path)
    assert mgr_boot2.verify_session_cookie(cookie) is None


def test_explicit_secret_env_overrides_ephemeral(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``JMRI_AUTH_SECRET`` wins over ``JMRI_AUTH_EPHEMERAL_SECRET``
    -- an operator who set both explicitly wanted the fixed value."""
    from pmri_server.auth import EPHEMERAL_SECRET_ENV  # noqa: PLC0415
    monkeypatch.setenv(SECRET_ENV, "explicit-fixed")
    monkeypatch.setenv(EPHEMERAL_SECRET_ENV, "1")

    mgr_boot1 = build_auth_manager(tmp_path)
    admin = mgr_boot1.users[0]
    cookie = mgr_boot1.mint_session_cookie(admin)

    mgr_boot2 = build_auth_manager(tmp_path)
    # Same explicit secret -> cookie still verifies.
    assert mgr_boot2.verify_session_cookie(cookie) is not None


def test_secret_from_env_overrides_derived(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cookie minted with the env-supplied secret must NOT
    verify against the derived secret and vice-versa. Otherwise
    ``JMRI_AUTH_SECRET`` provides no real protection."""
    monkeypatch.setenv(SECRET_ENV, "explicit-secret")
    mgr_with_env = build_auth_manager(tmp_path)
    admin = mgr_with_env.users[0]
    cookie = mgr_with_env.mint_session_cookie(admin)

    monkeypatch.delenv(SECRET_ENV)
    mgr_without = build_auth_manager(tmp_path)
    assert mgr_without.verify_session_cookie(cookie) is None
