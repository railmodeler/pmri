"""Integration tests for the ``configUser`` JSON topic."""
from __future__ import annotations

import pathlib

import pytest

from jmri_server.auth import (
    AuthManager,
    Role,
    USERS_FILE_NAME,
    User,
    UsersFile,
    hash_password,
    verify_password,
)
from jmri_server.envelope import JmriRequest


@pytest.fixture()
def auth_enabled(handle, tmp_path: pathlib.Path) -> AuthManager:
    """Attach a manager with two users:
    * ``admin`` -- Role.ADMIN, token ``atok``.
    * ``alice`` -- Role.OPERATOR, token ``otok``.
    """
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
    handle.ctx.auth = mgr
    return mgr


async def _dispatch(
    handle, *, username: str, role: Role, **kwargs,
):
    return await handle.registry.dispatch(
        JmriRequest.model_validate(kwargs),
        role=role, username=username,
    )


# ---- list / get ------------------------------------------------------


async def test_admin_can_list_users(handle, auth_enabled) -> None:
    resp = await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="list",
    )
    data = resp.data
    usernames = {u["username"] for u in data}
    assert usernames == {"admin", "alice"}
    # Never expose the hash.
    assert all("password_hash" not in u for u in data)
    # ``self`` flag correctly marks the caller.
    admin_row = next(u for u in data if u["username"] == "admin")
    assert admin_row["self"] is True


async def test_operator_blocked_from_list(handle, auth_enabled) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="alice", role=Role.OPERATOR,
            type="configUser", method="list",
        )
    assert exc.value.code == 403


async def test_get_returns_projection(handle, auth_enabled) -> None:
    resp = await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="get", data={"username": "alice"},
    )
    assert resp.data["username"] == "alice"
    assert resp.data["role"] == "operator"
    assert resp.data["has_token"] is True
    assert resp.data["self"] is False


# ---- put (create + update) -------------------------------------------


async def test_put_creates_new_user(handle, auth_enabled) -> None:
    resp = await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="put",
        data={"username": "bob", "password": "bobpass", "role": "viewer"},
    )
    assert resp.data["username"] == "bob"
    assert auth_enabled.by_username("bob").role is Role.VIEWER


async def test_put_missing_password_on_create_400s(
    handle, auth_enabled,
) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="admin", role=Role.ADMIN,
            type="configUser", method="put",
            data={"username": "bob"},
        )
    assert exc.value.code == 400


async def test_put_updates_existing_user_role(handle, auth_enabled) -> None:
    """Update an existing user's role without touching password."""
    original_hash = auth_enabled.by_username("alice").password_hash
    resp = await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="put",
        data={"username": "alice", "role": "viewer"},
    )
    assert resp.data["role"] == "viewer"
    # Password hash unchanged.
    assert auth_enabled.by_username("alice").password_hash == original_hash


async def test_put_invalid_role_400s(handle, auth_enabled) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="admin", role=Role.ADMIN,
            type="configUser", method="put",
            data={"username": "bob", "password": "b", "role": "bogus"},
        )
    assert exc.value.code == 400


async def test_put_demoting_last_admin_returns_409(
    handle, auth_enabled,
) -> None:
    """The last-admin guard surfaces as 409 (conflict) since the
    caller supplied a valid request that can't be satisfied
    given current state."""
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        # alice tries to demote admin (from her operator role, so
        # she'd hit 403 role gate first). Use admin caller instead.
        await _dispatch(
            handle, username="alice", role=Role.ADMIN,
            type="configUser", method="put",
            data={"username": "admin", "role": "operator"},
        )
    assert exc.value.code == 409


# ---- delete ----------------------------------------------------------


async def test_delete_succeeds(handle, auth_enabled) -> None:
    resp = await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="delete", data={"username": "alice"},
    )
    assert resp.data["deleted"] is True
    assert auth_enabled.by_username("alice") is None


async def test_delete_self_returns_409(handle, auth_enabled) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="admin", role=Role.ADMIN,
            type="configUser", method="delete",
            data={"username": "admin"},
        )
    assert exc.value.code == 409


async def test_delete_last_admin_returns_409(handle, auth_enabled) -> None:
    """Delete alice first (promote isn't quite right for this test --
    what we want is a state with one admin), then try to delete
    the admin. Result: 409, not 500."""
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="delete", data={"username": "alice"},
    )
    # Now admin is the only admin (and only user). Any other user
    # trying to delete would fail; test the last-admin path
    # specifically by adding a non-admin then having them try.
    await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="put",
        data={"username": "op", "password": "p", "role": "operator"},
    )
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="op", role=Role.ADMIN,
            type="configUser", method="delete",
            data={"username": "admin"},
        )
    assert exc.value.code == 409


async def test_delete_unknown_returns_404(handle, auth_enabled) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="admin", role=Role.ADMIN,
            type="configUser", method="delete",
            data={"username": "nobody"},
        )
    assert exc.value.code == 404


# ---- change_password action ------------------------------------------


async def test_self_can_change_own_password(handle, auth_enabled) -> None:
    """Any authenticated user can change their own password without
    needing admin. Post is gated at viewer for exactly this reason."""
    await _dispatch(
        handle, username="alice", role=Role.OPERATOR,
        type="configUser", method="post",
        data={
            "action": "change_password",
            "username": "alice", "new_password": "brandnew",
        },
    )
    alice = auth_enabled.by_username("alice")
    assert verify_password("brandnew", alice.password_hash)


async def test_operator_cannot_change_others_password(
    handle, auth_enabled,
) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="alice", role=Role.OPERATOR,
            type="configUser", method="post",
            data={
                "action": "change_password",
                "username": "admin", "new_password": "hacked",
            },
        )
    assert exc.value.code == 403


async def test_admin_can_change_others_password(handle, auth_enabled) -> None:
    await _dispatch(
        handle, username="admin", role=Role.ADMIN,
        type="configUser", method="post",
        data={
            "action": "change_password",
            "username": "alice", "new_password": "adminreset",
        },
    )
    assert verify_password(
        "adminreset", auth_enabled.by_username("alice").password_hash,
    )


# ---- regenerate_token action -----------------------------------------


async def test_self_can_regenerate_own_token(handle, auth_enabled) -> None:
    resp = await _dispatch(
        handle, username="alice", role=Role.OPERATOR,
        type="configUser", method="post",
        data={"action": "regenerate_token", "username": "alice"},
    )
    new_token = resp.data["token"]
    assert new_token != "otok"
    # Old token no longer works.
    assert auth_enabled.login_token("otok") is None
    assert auth_enabled.login_token(new_token) is not None


async def test_operator_cannot_regenerate_others_token(
    handle, auth_enabled,
) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="alice", role=Role.OPERATOR,
            type="configUser", method="post",
            data={"action": "regenerate_token", "username": "admin"},
        )
    assert exc.value.code == 403


# ---- error cases -----------------------------------------------------


async def test_auth_off_returns_503(handle) -> None:
    """Without auth attached, configUser has nothing to do --
    return 503 with an explicit message so an operator debugging
    "why does this endpoint 500?" sees the reason right away."""
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username=None, role=Role.ADMIN,
            type="configUser", method="list",
        )
    assert exc.value.code == 503


async def test_unknown_action_400s(handle, auth_enabled) -> None:
    from jmri_server.service import JsonServiceError  # noqa: PLC0415
    with pytest.raises(JsonServiceError) as exc:
        await _dispatch(
            handle, username="admin", role=Role.ADMIN,
            type="configUser", method="post",
            data={"action": "wat", "username": "admin"},
        )
    assert exc.value.code == 400
