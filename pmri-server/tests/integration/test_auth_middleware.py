"""End-to-end auth middleware tests.

Drives the full FastAPI app through TestClient with auth enabled
via :attr:`ServerContext.auth`. Covers:

* Auth off -> every route open.
* Auth on, no credentials -> 401 on JSON, 302 redirect on HTML.
* Auth on, valid bearer -> passes.
* Auth on, valid cookie -> passes.
* Role gate on JSON topics (viewer blocked from mutations).
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from pmri_server.auth import (
    AuthManager,
    Role,
    User,
    UsersFile,
    USERS_FILE_NAME,
    hash_password,
)
from pmri_server.auth.middleware import SESSION_COOKIE


@pytest.fixture()
def auth_enabled_client(
    handle, tmp_path: pathlib.Path,
) -> TestClient:
    """Attach an AuthManager with two users to the running app.

    Returns a TestClient. Users:
    * ``admin`` -- password ``adminpass``, token ``admintoken``, role admin.
    * ``viewer`` -- password ``viewpass``, token ``viewtoken``, role viewer.
    """
    users = UsersFile(users=[
        User(username="admin", password_hash=hash_password("adminpass"),
             role=Role.ADMIN, token="admintoken"),
        User(username="viewer", password_hash=hash_password("viewpass"),
             role=Role.VIEWER, token="viewtoken"),
    ])
    handle.ctx.auth = AuthManager(
        users, users_path=tmp_path / USERS_FILE_NAME,
        secret_key="test-secret",
    )
    with TestClient(handle.app) as tc:
        yield tc


# ---- auth off (default) -----------------------------------------------


def test_auth_off_json_open(client: TestClient) -> None:
    """Without auth attached the JSON API stays open."""
    r = client.post("/json/", json={"type": "power", "method": "get"})
    assert r.status_code == 200


# ---- 401 without credentials ------------------------------------------


def test_auth_on_json_without_credentials_401(
    auth_enabled_client: TestClient,
) -> None:
    r = auth_enabled_client.post(
        "/json/", json={"type": "power", "method": "get"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"


def test_auth_on_html_without_credentials_redirects_to_login(
    auth_enabled_client: TestClient,
) -> None:
    """A browser-accepting client with no cookie gets bounced to
    /login with a next= hint so it lands back on the intended
    page after auth."""
    r = auth_enabled_client.get(
        "/api", headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ---- bearer token ------------------------------------------------------


def test_bearer_admin_token_grants_access(
    auth_enabled_client: TestClient,
) -> None:
    r = auth_enabled_client.post(
        "/json/", json={"type": "power", "method": "get"},
        headers={"Authorization": "Bearer admintoken"},
    )
    assert r.status_code == 200


def test_bearer_viewer_token_blocked_from_mutation(
    auth_enabled_client: TestClient,
) -> None:
    """Viewer can read but can't drive."""
    r = auth_enabled_client.post(
        "/json/", json={"type": "power", "method": "get"},
        headers={"Authorization": "Bearer viewtoken"},
    )
    assert r.status_code == 200

    r2 = auth_enabled_client.post(
        "/json/",
        json={"type": "power", "method": "post", "data": {"state": 2}},
        headers={"Authorization": "Bearer viewtoken"},
    )
    assert r2.status_code == 403


def test_bearer_bad_token_returns_401(
    auth_enabled_client: TestClient,
) -> None:
    r = auth_enabled_client.post(
        "/json/", json={"type": "power", "method": "get"},
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    assert r.status_code == 401


# ---- session cookie ---------------------------------------------------


def test_session_cookie_grants_access(
    auth_enabled_client: TestClient, handle,
) -> None:
    """Mint a session cookie the way /login would, then use it on
    a JSON call. Sidesteps the /login form since we're testing
    the cookie mechanism itself."""
    admin = handle.ctx.auth.users[0]
    cookie = handle.ctx.auth.mint_session_cookie(admin)
    r = auth_enabled_client.post(
        "/json/", json={"type": "power", "method": "get"},
        cookies={SESSION_COOKIE: cookie},
    )
    assert r.status_code == 200


def test_tampered_cookie_returns_401(
    auth_enabled_client: TestClient, handle,
) -> None:
    admin = handle.ctx.auth.users[0]
    cookie = handle.ctx.auth.mint_session_cookie(admin)
    tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    r = auth_enabled_client.post(
        "/json/", json={"type": "power", "method": "get"},
        cookies={SESSION_COOKIE: tampered},
    )
    assert r.status_code == 401


# ---- exempt paths -----------------------------------------------------


def test_login_path_exempt(auth_enabled_client: TestClient) -> None:
    """The /login page must render without any credentials --
    otherwise there's no way to get in."""
    r = auth_enabled_client.get("/login", headers={"Accept": "text/html"})
    # 200 (form rendered) or 404 (pmri-web not mounted in this
    # test) both prove the middleware let it through. Real /login
    # coverage lives in pmri-web tests.
    assert r.status_code in (200, 404)


def test_static_path_exempt(auth_enabled_client: TestClient) -> None:
    """Static assets like ``/static/style.css`` must serve without
    a cookie. Otherwise the login page loads unstyled and looks
    broken."""
    r = auth_enabled_client.get("/static/style.css")
    assert r.status_code in (200, 404)
