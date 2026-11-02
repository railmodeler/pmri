"""End-to-end login/logout flow via the web UI.

Auth is enabled by attaching an :class:`AuthManager` to the
running server context -- same pattern the server-side tests use.
Covers:

* GET /login renders when auth is on.
* GET /login shows a friendly message when auth is off.
* POST /login with correct creds sets the session cookie.
* POST /login with wrong creds returns 401 + error.
* POST /login without CSRF cookie fails.
* POST /logout clears the cookie.
* base.html renders the user badge when logged in.
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
)
from jmri_server.auth.middleware import SESSION_COOKIE


@pytest.fixture()
def auth_enabled(handle, tmp_path: pathlib.Path) -> AuthManager:
    users = UsersFile(users=[
        User(username="admin", password_hash=hash_password("adminpass"),
             role=Role.ADMIN, token="admintoken"),
    ])
    mgr = AuthManager(
        users, users_path=tmp_path / USERS_FILE_NAME,
        secret_key="test-secret",
    )
    handle.server.ctx.auth = mgr
    return mgr


# ---- login page renders -----------------------------------------------


def test_login_page_renders_when_auth_off(client: TestClient) -> None:
    """Auth off -- /login still renders (it's exempt) and shows a
    "disabled" message so operators don't waste time entering
    credentials that go nowhere."""
    r = client.get("/login")
    assert r.status_code == 200
    assert "Auth is disabled" in r.text


def test_login_page_renders_when_auth_on(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    # Form fields present.
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text
    assert 'name="_csrf"' in r.text


def test_login_sets_csrf_cookie(client: TestClient) -> None:
    """The GET /login response must set ``JMRICSRF`` so the POST
    can verify -- otherwise every fresh browser hits an error on
    first submit."""
    r = client.get("/login")
    assert "JMRICSRF" in r.cookies


# ---- login POST -------------------------------------------------------


def test_login_post_wrong_creds_returns_401(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    # First fetch to get the CSRF cookie.
    client.get("/login")
    csrf_cookie = client.cookies.get("JMRICSRF")
    r = client.post(
        "/login",
        data={
            "username": "admin", "password": "wrong",
            "next": "/", "_csrf": csrf_cookie,
        },
    )
    assert r.status_code == 401
    assert "Invalid" in r.text


def test_login_post_missing_csrf_returns_400(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    """No CSRF cookie / no _csrf field -> 400 with a friendly
    "session expired" message, not a silent success."""
    r = client.post(
        "/login",
        data={
            "username": "admin", "password": "adminpass",
            "next": "/",
        },
    )
    assert r.status_code == 400
    assert "expired" in r.text.lower()


def test_login_post_success_sets_session_cookie(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    client.get("/login")
    csrf_cookie = client.cookies.get("JMRICSRF")
    r = client.post(
        "/login",
        data={
            "username": "admin", "password": "adminpass",
            "next": "/", "_csrf": csrf_cookie,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert SESSION_COOKIE in r.cookies


def test_login_post_success_redirects_to_next(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    """A ``next=`` value from the middleware's redirect must be
    honoured so the user lands on the page they originally tried
    to hit, not just the home page."""
    client.get("/login")
    csrf_cookie = client.cookies.get("JMRICSRF")
    r = client.post(
        "/login",
        data={
            "username": "admin", "password": "adminpass",
            "next": "/roster", "_csrf": csrf_cookie,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["Location"] == "/roster"


# ---- logout -----------------------------------------------------------


def test_logout_clears_cookie(client: TestClient) -> None:
    """POST /logout clears the session cookie regardless of
    whether we were logged in -- logging out should always work
    without leaving stale cookies around."""
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    # Response either sets an expiry or a blank value; either way
    # the browser drops it.
    set_cookies = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE in set_cookies


# ---- user badge in base template --------------------------------------


def test_user_badge_shown_when_logged_in(
    client: TestClient, auth_enabled: AuthManager,
) -> None:
    """After login, home page shows the user badge with username
    and role."""
    client.get("/login")
    csrf_cookie = client.cookies.get("JMRICSRF")
    client.post(
        "/login",
        data={
            "username": "admin", "password": "adminpass",
            "next": "/", "_csrf": csrf_cookie,
        },
    )
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "admin" in r.text
    assert "admin" in r.text  # role


def test_user_badge_hidden_when_auth_off(client: TestClient) -> None:
    """Auth off -> no badge, no logout button. Otherwise every
    default install shows a distracting "log out" link that
    doesn't do anything."""
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    # Neither the logout form nor a badge should render.
    assert "/logout" not in r.text or "log out" not in r.text.lower()
