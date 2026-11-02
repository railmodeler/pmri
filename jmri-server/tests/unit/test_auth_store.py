"""Auth models + password hashing + users.yaml store."""
from __future__ import annotations

import pathlib

import pytest

from jmri_server.auth import (
    Role,
    User,
    UsersFile,
    hash_password,
    load_users_file,
    save_users_file,
    verify_password,
)


# ---- Role ordering -----------------------------------------------------


def test_role_priority_ordering() -> None:
    """viewer < operator < admin -- the dispatch path relies on this."""
    assert Role.VIEWER < Role.OPERATOR < Role.ADMIN
    assert Role.ADMIN > Role.OPERATOR > Role.VIEWER
    assert Role.OPERATOR >= Role.OPERATOR
    assert not (Role.VIEWER > Role.OPERATOR)


# ---- password hash/verify ----------------------------------------------


def test_hash_and_verify_password_roundtrip() -> None:
    h = hash_password("s3cr3t!")
    assert verify_password("s3cr3t!", h)
    assert not verify_password("wrong", h)


def test_verify_password_rejects_empty_hash() -> None:
    """A user with no hash on file has no valid password. Empty
    string should be a fast, unambiguous ``False`` -- not an
    exception or an accidental match."""
    assert not verify_password("anything", "")


def test_verify_password_rejects_malformed_hash() -> None:
    """Hand-edit accident: garbage in the hash field must fail
    verify cleanly, not crash. Otherwise a botched users.yaml
    edit takes the server down."""
    assert not verify_password("password", "not-a-bcrypt-hash")


def test_hash_password_refuses_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        hash_password("")


def test_different_hashes_for_same_password() -> None:
    """bcrypt salts every hash -- two calls with the same input
    must produce different outputs but both verify."""
    a = hash_password("same")
    b = hash_password("same")
    assert a != b
    assert verify_password("same", a)
    assert verify_password("same", b)


# ---- UsersFile ---------------------------------------------------------


def test_by_username_returns_matching_user() -> None:
    uf = UsersFile(users=[
        User(username="alice", password_hash="x", role=Role.OPERATOR),
        User(username="bob", password_hash="y", role=Role.VIEWER),
    ])
    assert uf.by_username("alice").role == Role.OPERATOR
    assert uf.by_username("bob").role == Role.VIEWER
    assert uf.by_username("missing") is None


def test_by_username_is_case_sensitive() -> None:
    """Case-sensitive lookup -- matches JMRI convention of treating
    usernames as opaque identifiers. Otherwise ``Admin`` silently
    logs in as ``admin`` and audit logs mislead."""
    uf = UsersFile(users=[User(username="admin", password_hash="x")])
    assert uf.by_username("admin") is not None
    assert uf.by_username("Admin") is None


def test_by_token_finds_user_with_matching_token() -> None:
    uf = UsersFile(users=[
        User(username="a", password_hash="x", token="tok-a"),
        User(username="b", password_hash="y", token="tok-b"),
    ])
    assert uf.by_token("tok-b").username == "b"


def test_by_token_ignores_empty_tokens() -> None:
    """A user with no token (empty string) doesn't match a
    ``by_token("")`` query. Otherwise the first token-less user
    would match every empty-token bearer probe."""
    uf = UsersFile(users=[
        User(username="a", password_hash="x", token=""),
        User(username="b", password_hash="y", token="tok-b"),
    ])
    assert uf.by_token("") is None
    assert uf.by_token("tok-b").username == "b"


# ---- YAML round-trip ---------------------------------------------------


def test_yaml_roundtrip(tmp_path: pathlib.Path) -> None:
    """The load/save pair preserves user order + all fields."""
    path = tmp_path / "users.yaml"
    original = UsersFile(users=[
        User(username="admin", password_hash=hash_password("adminpass"),
             role=Role.ADMIN, token="admintoken"),
        User(username="viewer1", password_hash=hash_password("view"),
             role=Role.VIEWER, token=""),
    ])
    save_users_file(path, original)
    reloaded = load_users_file(path)
    assert [u.username for u in reloaded.users] == ["admin", "viewer1"]
    assert reloaded.by_username("admin").role == Role.ADMIN
    assert verify_password("adminpass",
                           reloaded.by_username("admin").password_hash)


def test_load_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    uf = load_users_file(tmp_path / "does_not_exist.yaml")
    assert uf.users == []


def test_load_rejects_non_mapping_top_level(tmp_path: pathlib.Path) -> None:
    """A YAML list at the top level is a shape error, not an
    empty-users file. Raise so the operator sees the mistake."""
    path = tmp_path / "users.yaml"
    path.write_text("- oops\n")
    with pytest.raises(ValueError, match="mapping"):
        load_users_file(path)


def test_atomic_save_leaves_no_temp_files(tmp_path: pathlib.Path) -> None:
    """The atomic-write pattern must clean up its temp file even
    on successful writes, so ``ls`` on the settings dir stays
    tidy."""
    path = tmp_path / "users.yaml"
    save_users_file(path, UsersFile(users=[
        User(username="x", password_hash="y"),
    ]))
    stray = [
        f for f in tmp_path.iterdir()
        if f.name.startswith(".users.yaml.") or f.name.endswith(".tmp")
    ]
    assert stray == []
