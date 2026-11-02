"""End-to-end tests for the /users page.

Attach an AuthManager with a couple of users, log in through the
web /login flow, then exercise the /users page + forms.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from jmri_server.auth import (
    AuthManager,
    Role,
    USERS_FILE_NAME,
    User,
    UsersFile,
    hash_password,
    verify_password,
)


@pytest.fixture()
def auth_enabled(handle, tmp_path: pathlib.Path) -> AuthManager:
    users = UsersFile(users=[
        User(username="admin", password_hash=hash_password("adminpass"),
             role=Role.ADMIN, token="atok"),
        User(username="alice", password_hash=hash_password("alicepass"),
             role=Role.OPERATOR, token="otok"),
    ])
    mgr = AuthManager(
        users, users_path=tmp_path / USERS_FILE_NAME,
        secret_key="test-secret",
    )
    handle.server.ctx.auth = mgr
    return mgr


def _login_as(client: TestClient, username: str, password: str) -> None:
    client.get("/login")
    csrf = client.cookies.get("JMRICSRF")
    client.post(
        "/login",
        data={
            "username": username, "password": password,
            "next": "/users", "_csrf": csrf,
        },
    )


# ---- page renders -----------------------------------------------------


def test_admin_sees_user_list(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "admin", "adminpass")
    r = client.get("/users")
    assert r.status_code == 200
    assert "admin" in r.text
    assert "alice" in r.text
    # Create-user form present.
    assert "Create new user" in r.text


def test_operator_sees_self_service_only(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    """Non-admin: self-service panel visible, admin sections
    hidden."""
    _login_as(client, "alice", "alicepass")
    r = client.get("/users")
    assert r.status_code == 200
    assert "Your account" in r.text
    # Admin CRUD headings should not render.
    assert "Create new user" not in r.text


def test_anon_bounced_to_login(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    r = client.get(
        "/users", headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ---- create user -----------------------------------------------------


def test_admin_creates_user(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "admin", "adminpass")
    csrf = client.cookies.get("JMRICSRF")
    r = client.post(
        "/users/create",
        data={
            "username": "bob", "password": "bobpass",
            "role": "viewer", "_csrf": csrf,
        },
    )
    assert r.status_code == 200
    assert "Created user bob" in r.text
    assert auth_enabled.by_username("bob") is not None


def test_operator_cannot_create_user(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "alice", "alicepass")
    csrf = client.cookies.get("JMRICSRF")
    r = client.post(
        "/users/create",
        data={
            "username": "sneaky", "password": "p",
            "role": "admin", "_csrf": csrf,
        },
    )
    # Notice comes back but the underlying dispatch fails 403,
    # so the notice reads "Create failed: ...".
    assert "failed" in r.text.lower()
    assert auth_enabled.by_username("sneaky") is None


# ---- self-service password change -----------------------------------


def test_self_service_change_password(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "alice", "alicepass")
    csrf = client.cookies.get("JMRICSRF")
    r = client.post(
        "/users/self/change-password",
        data={"new_password": "changed", "_csrf": csrf},
    )
    assert r.status_code == 200
    assert "Password updated" in r.text
    alice = auth_enabled.by_username("alice")
    assert verify_password("changed", alice.password_hash)


def test_self_service_regenerate_token(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "alice", "alicepass")
    csrf = client.cookies.get("JMRICSRF")
    r = client.post(
        "/users/self/regenerate-token",
        data={"_csrf": csrf},
    )
    assert r.status_code == 200
    # New token shown once + old token invalidated.
    assert "New API token" in r.text
    assert auth_enabled.login_token("otok") is None


# ---- admin actions --------------------------------------------------


def test_admin_regen_others_token(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "admin", "adminpass")
    csrf = client.cookies.get("JMRICSRF")
    r = client.post(
        "/users/alice/regenerate-token",
        data={"_csrf": csrf},
    )
    assert r.status_code == 200
    assert "New API token" in r.text
    assert auth_enabled.login_token("otok") is None


def test_admin_deletes_user(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "admin", "adminpass")
    csrf = client.cookies.get("JMRICSRF")
    r = client.post(
        "/users/alice/delete", data={"_csrf": csrf},
    )
    assert r.status_code == 200
    assert "Deleted user alice" in r.text
    assert auth_enabled.by_username("alice") is None


def test_admin_cannot_self_delete_via_form(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    """Self-delete surfaces the 409 as a notice -- no crash."""
    _login_as(client, "admin", "adminpass")
    csrf = client.cookies.get("JMRICSRF")
    r = client.post(
        "/users/admin/delete", data={"_csrf": csrf},
    )
    assert r.status_code == 200
    assert "failed" in r.text.lower()
    # Admin still exists.
    assert auth_enabled.by_username("admin") is not None


# ---- CSRF -----------------------------------------------------------


def test_missing_csrf_shows_notice(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "admin", "adminpass")
    r = client.post(
        "/users/create",
        data={
            "username": "x", "password": "p",
            "role": "viewer",
            # deliberately no _csrf
        },
    )
    assert "expired" in r.text.lower()
    assert auth_enabled.by_username("x") is None


# ---- nav visibility -------------------------------------------------


def test_users_nav_link_hidden_when_anon(client: TestClient) -> None:
    """No auth, no user -> nav shouldn't have a 'users' link at
    all."""
    r = client.get("/")
    assert r.status_code == 200
    # The link is behind an ``{% if request.state.user %}`` guard.
    # With auth off, ``request.state.user`` is None, so no link.
    assert '/users' not in r.text or ">users<" not in r.text


def test_users_nav_link_shown_when_logged_in(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    _login_as(client, "alice", "alicepass")
    r = client.get("/")
    assert '/users' in r.text
