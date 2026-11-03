"""AuthManager mutation methods: add / remove / password / role / token.

Every mutation must persist to disk atomically -- the tests reload the
UsersFile after each mutation to confirm both in-memory state AND
on-disk state match.
"""
from __future__ import annotations

import pathlib

import pytest

from pmri_server.auth import (
    AuthManager,
    Role,
    USERS_FILE_NAME,
    User,
    UsersFile,
    hash_password,
    load_users_file,
    verify_password,
)


def _mgr_with(users: list[User], tmp_path: pathlib.Path) -> AuthManager:
    return AuthManager(
        UsersFile(users=users),
        users_path=tmp_path / USERS_FILE_NAME,
        secret_key="test-secret",
    )


# ---- add_user ---------------------------------------------------------


def test_add_user_creates_entry_and_persists(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("adminpass"),
             role=Role.ADMIN, token="atok"),
    ], tmp_path)
    new = mgr.add_user("alice", "alicepass", role=Role.OPERATOR)
    assert new.username == "alice"
    assert new.token  # auto-generated
    reloaded = load_users_file(tmp_path / USERS_FILE_NAME)
    assert reloaded.by_username("alice") is not None
    assert reloaded.by_username("alice").role is Role.OPERATOR


def test_add_user_hashes_password(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
    ], tmp_path)
    mgr.add_user("bob", "bobpass")
    # Round-trip through disk to ensure the hash -- not plaintext --
    # was stored.
    reloaded = load_users_file(tmp_path / USERS_FILE_NAME)
    bob = reloaded.by_username("bob")
    assert bob.password_hash != "bobpass"
    assert verify_password("bobpass", bob.password_hash)


def test_add_user_refuses_duplicate(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([
        User(username="alice", password_hash=hash_password("a")),
    ], tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        mgr.add_user("alice", "b")


def test_add_user_refuses_empty_username(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([], tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        mgr.add_user("", "p")


def test_add_user_accepts_explicit_empty_token(
    tmp_path: pathlib.Path,
) -> None:
    """A login-only account (no API token) is a legit use case for
    a viewer who only ever browses the web UI. Explicit empty
    string opts out of the auto-generated token."""
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
    ], tmp_path)
    user = mgr.add_user("viewer", "viewpass", role=Role.VIEWER, token="")
    assert user.token == ""


# ---- remove_user -----------------------------------------------------


def test_remove_user_succeeds(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
        User(username="alice", password_hash=hash_password("b"),
             role=Role.OPERATOR, token="t2"),
    ], tmp_path)
    mgr.remove_user("alice", current_username="admin")
    reloaded = load_users_file(tmp_path / USERS_FILE_NAME)
    assert reloaded.by_username("alice") is None


def test_remove_user_refuses_self(tmp_path: pathlib.Path) -> None:
    """Deleting your own account would immediately lock you out
    without an obvious recovery path (nobody else could log you
    back in). Refuse."""
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
    ], tmp_path)
    with pytest.raises(ValueError, match="own account"):
        mgr.remove_user("admin", current_username="admin")


def test_remove_user_refuses_last_admin(tmp_path: pathlib.Path) -> None:
    """Removing the only admin would leave the install with no
    admin -- no user-CRUD flow could recover from that."""
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
        User(username="another", password_hash=hash_password("b"),
             role=Role.OPERATOR, token="t2"),
    ], tmp_path)
    with pytest.raises(ValueError, match="last admin"):
        mgr.remove_user("admin", current_username="another")


def test_remove_user_allows_one_admin_when_others_exist(
    tmp_path: pathlib.Path,
) -> None:
    """Two admins -- either can be removed."""
    mgr = _mgr_with([
        User(username="admin1", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
        User(username="admin2", password_hash=hash_password("b"),
             role=Role.ADMIN, token="t2"),
    ], tmp_path)
    mgr.remove_user("admin2", current_username="admin1")
    assert mgr.by_username("admin2") is None


def test_remove_user_unknown_raises(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
    ], tmp_path)
    with pytest.raises(LookupError):
        mgr.remove_user("nobody", current_username="admin")


# ---- set_password ----------------------------------------------------


def test_set_password_persists_new_hash(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([
        User(username="alice", password_hash=hash_password("old"),
             role=Role.OPERATOR, token="t"),
    ], tmp_path)
    mgr.set_password("alice", "new")
    reloaded = load_users_file(tmp_path / USERS_FILE_NAME)
    alice = reloaded.by_username("alice")
    assert verify_password("new", alice.password_hash)
    assert not verify_password("old", alice.password_hash)


def test_set_password_unknown_user_raises(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([], tmp_path)
    with pytest.raises(LookupError):
        mgr.set_password("nobody", "x")


# ---- set_role --------------------------------------------------------


def test_set_role_promotes_viewer_to_operator(tmp_path: pathlib.Path) -> None:
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
        User(username="alice", password_hash=hash_password("b"),
             role=Role.VIEWER, token="t2"),
    ], tmp_path)
    mgr.set_role("alice", Role.OPERATOR, current_username="admin")
    assert mgr.by_username("alice").role is Role.OPERATOR


def test_set_role_refuses_self_demotion(tmp_path: pathlib.Path) -> None:
    """Demoting yourself would surprise you the next request --
    you'd lose access to /users and couldn't undo it. Refuse."""
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
    ], tmp_path)
    with pytest.raises(ValueError, match="own account"):
        mgr.set_role("admin", Role.VIEWER, current_username="admin")


def test_set_role_refuses_last_admin_demotion(
    tmp_path: pathlib.Path,
) -> None:
    """Same last-admin guard as remove_user."""
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
        User(username="op", password_hash=hash_password("b"),
             role=Role.OPERATOR, token="t2"),
    ], tmp_path)
    with pytest.raises(ValueError, match="last admin"):
        mgr.set_role("admin", Role.OPERATOR, current_username="op")


def test_set_role_allows_self_no_op(tmp_path: pathlib.Path) -> None:
    """Setting your own role to what it already is is a no-op --
    should NOT trigger the demotion guard."""
    mgr = _mgr_with([
        User(username="admin", password_hash=hash_password("a"),
             role=Role.ADMIN, token="t"),
    ], tmp_path)
    mgr.set_role("admin", Role.ADMIN, current_username="admin")
    assert mgr.by_username("admin").role is Role.ADMIN


# ---- regenerate_token ------------------------------------------------


def test_regenerate_token_replaces_and_persists(
    tmp_path: pathlib.Path,
) -> None:
    """A regenerated token invalidates the old one -- that's the
    whole point of the endpoint (leaked-token cycling)."""
    mgr = _mgr_with([
        User(username="alice", password_hash=hash_password("a"),
             role=Role.OPERATOR, token="old-token"),
    ], tmp_path)
    new = mgr.regenerate_token("alice")
    assert new != "old-token"
    assert mgr.by_username("alice").token == new
    # Old token no longer matches.
    assert mgr.login_token("old-token") is None
    assert mgr.login_token(new) is not None
    reloaded = load_users_file(tmp_path / USERS_FILE_NAME)
    assert reloaded.by_username("alice").token == new
