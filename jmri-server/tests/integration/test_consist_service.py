"""ConsistService -- CRUD + fan-out actions over the JSON envelope."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import post


# ---- CRUD -----------------------------------------------------------------


def test_put_creates_consist(client: TestClient) -> None:
    resp = post(client, "consist", method="put", data={
        "address": 100, "kind": "software", "comment": "test",
    })
    data = resp["data"]
    assert data["address"] == 100
    assert data["kind"] == "software"
    assert data["comment"] == "test"
    assert data["locos"] == []


def test_put_rejects_out_of_range_address(client: TestClient) -> None:
    """Consist addresses are 1..127 short by spec (CV19)."""
    r = client.post("/json/", json={
        "type": "consist", "method": "put",
        "data": {"address": 128},
    })
    assert r.status_code == 400


def test_put_rejects_roster_collision_without_touching_roster_page(
    handle,
) -> None:
    """Bug fixed here: the roster store is built lazily on first use.
    If the user's first move is Create-consist (never visits /roster or
    /throttle first), the store wasn't on ``ctx`` yet and the conflict
    check silently returned "no conflict". The service must lazy-load
    the store itself so the collision check works from a cold start.
    """
    # Use unique ids so parallel/repeat runs don't leak state through
    # the profile's on-disk roster dir.
    entry_id = "collide_short_71"
    address = 71
    with TestClient(handle.app) as tc:
        _reset_roster(tc, [entry_id])
        tc.post("/json/", json={
            "type": "roster", "method": "put",
            "data": {"id": entry_id, "dccAddress": str(address),
                     "longAddress": False},
        })
        # Force the store back off the context so the /consists PUT
        # must lazy-load it (mimicking a fresh navigation).
        handle.ctx._roster_store = None
        try:
            r = tc.post("/json/", json={
                "type": "consist", "method": "put",
                "data": {"address": address},
            })
            assert r.status_code == 409
            assert entry_id in r.json()["detail"]
        finally:
            _reset_roster(tc, [entry_id])


def test_put_ignores_long_address_roster_entry_at_same_number(
    handle,
) -> None:
    """A long-address loco at the same numeric value doesn't conflict
    with a 1..127 short consist -- they share no wire space."""
    entry_id = "long_only_72"
    address = 72
    with TestClient(handle.app) as tc:
        _reset_roster(tc, [entry_id])
        tc.post("/json/", json={
            "type": "roster", "method": "put",
            "data": {"id": entry_id, "dccAddress": str(address),
                     "longAddress": True},
        })
        try:
            r = tc.post("/json/", json={
                "type": "consist", "method": "put",
                "data": {"address": address},
            })
            assert r.status_code == 200
        finally:
            tc.post("/json/", json={
                "type": "consist", "method": "delete",
                "data": {"address": address},
            })
            _reset_roster(tc, [entry_id])


def _reset_roster(tc: TestClient, ids: list[str]) -> None:
    """Delete each roster id, ignoring 404s. Used to clean state
    between roster-touching tests since the ``RosterStore`` writes to
    disk under ``Profile.default()``."""
    for entry_id in ids:
        tc.post("/json/", json={
            "type": "roster", "method": "delete",
            "data": {"id": entry_id},
        })


def test_put_is_idempotent(client: TestClient) -> None:
    """Repeated put at the same address doesn't clear members."""
    post(client, "consist", method="put", data={"address": 101})
    post(client, "consist", method="post", data={
        "address": 101, "action": "add_member",
        "member": {"address": 111},
    })
    # Second put must NOT drop the member.
    resp = post(client, "consist", method="put", data={"address": 101})
    assert [m["address"] for m in resp["data"]["locos"]] == [111]


def test_get_returns_specific_consist(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 102})
    resp = post(client, "consist", method="get", data={"address": 102})
    assert resp["data"]["address"] == 102


def test_get_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "consist", "method": "get", "data": {"address": 999},
    })
    assert r.status_code == 404


def test_list_returns_every_consist(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 20})
    post(client, "consist", method="put", data={"address": 21})
    resp = post(client, "consist", method="list")
    addrs = {c["address"] for c in resp["data"]}
    assert {20, 21} <= addrs


def test_delete_drops_the_consist(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 30})
    resp = post(client, "consist", method="delete", data={"address": 30})
    assert resp["data"]["deleted"] is True
    r = client.post("/json/", json={
        "type": "consist", "method": "get", "data": {"address": 30},
    })
    assert r.status_code == 404


def test_delete_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "consist", "method": "delete", "data": {"address": 999},
    })
    assert r.status_code == 404


# ---- membership ---------------------------------------------------------


def test_add_member_appears_in_get(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 40})
    post(client, "consist", method="post", data={
        "address": 40, "action": "add_member",
        "member": {"address": 111, "long_address": True,
                   "direction_normal": False},
    })
    resp = post(client, "consist", method="get", data={"address": 40})
    locos = resp["data"]["locos"]
    assert len(locos) == 1
    assert locos[0]["address"] == 111
    assert locos[0]["long_address"] is True
    assert locos[0]["direction_normal"] is False
    assert locos[0]["position"] == 0


def test_remove_member(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 41})
    post(client, "consist", method="post", data={
        "address": 41, "action": "add_member",
        "member": {"address": 111},
    })
    post(client, "consist", method="post", data={
        "address": 41, "action": "add_member",
        "member": {"address": 222},
    })
    post(client, "consist", method="post", data={
        "address": 41, "action": "remove_member",
        "member_address": 111,
    })
    resp = post(client, "consist", method="get", data={"address": 41})
    assert [m["address"] for m in resp["data"]["locos"]] == [222]


def test_remove_missing_member_returns_404(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 42})
    r = client.post("/json/", json={
        "type": "consist", "method": "post",
        "data": {"address": 42, "action": "remove_member", "member_address": 999},
    })
    assert r.status_code == 404


# ---- fan-out throttle control ------------------------------------------


def test_set_speed_reaches_member_throttles(client: TestClient) -> None:
    """set_speed fans to every member throttle. Verify via the throttle
    service -- we should see the member's throttle at the requested speed."""
    post(client, "consist", method="put", data={"address": 50})
    post(client, "consist", method="post", data={
        "address": 50, "action": "add_member",
        "member": {"address": 3601, "long_address": True},
    })
    post(client, "consist", method="post", data={
        "address": 50, "action": "set_speed", "speed": 0.42,
    })
    r = post(client, "throttle", method="get", data={"name": "3601"})
    assert r["data"]["speed"] == 0.42


def test_set_direction_via_action(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 51})
    post(client, "consist", method="post", data={
        "address": 51, "action": "add_member",
        "member": {"address": 3602},
    })
    post(client, "consist", method="post", data={
        "address": 51, "action": "set_direction", "direction": "reverse",
    })
    r = post(client, "throttle", method="get", data={"name": "3602"})
    assert r["data"]["forward"] is False


def test_set_function_reaches_members(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 52})
    post(client, "consist", method="post", data={
        "address": 52, "action": "add_member",
        "member": {"address": 3603},
    })
    post(client, "consist", method="post", data={
        "address": 52, "action": "set_function", "function": 0, "on": True,
    })
    r = post(client, "throttle", method="get", data={"name": "3603"})
    assert r["data"]["F0"] is True


def test_emergency_stop_action(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 53})
    post(client, "consist", method="post", data={
        "address": 53, "action": "add_member",
        "member": {"address": 3604},
    })
    post(client, "consist", method="post", data={
        "address": 53, "action": "set_speed", "speed": 0.6,
    })
    post(client, "consist", method="post", data={
        "address": 53, "action": "emergency_stop",
    })
    r = post(client, "throttle", method="get", data={"name": "3604"})
    assert r["data"]["speed"] == -1.0


# ---- error paths -------------------------------------------------------


def test_put_rejects_bad_kind(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "consist", "method": "put",
        "data": {"address": 60, "kind": "nonsense"},
    })
    assert r.status_code == 400


def test_post_unknown_action(client: TestClient) -> None:
    post(client, "consist", method="put", data={"address": 61})
    r = client.post("/json/", json={
        "type": "consist", "method": "post",
        "data": {"address": 61, "action": "cook_eggs"},
    })
    assert r.status_code == 400


def test_post_missing_address_400(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "consist", "method": "post",
        "data": {"action": "set_speed", "speed": 0.5},
    })
    assert r.status_code == 400


# ---- refresh_from_cs ---------------------------------------------------


def test_refresh_from_cs_unsupported_connection_returns_400(
    client: TestClient,
) -> None:
    """The default InternalConnection doesn't expose ``refresh_from_cs``.
    Callers should get a clear 400, not a 500 or AttributeError."""
    r = client.post("/json/", json={
        "type": "consist", "method": "post",
        "data": {"action": "refresh_from_cs"},
    })
    assert r.status_code == 400
    assert "refresh_from_cs" in r.json()["detail"]


def test_refresh_from_cs_calls_manager_and_returns_snapshot(handle) -> None:
    """When the driver's manager exposes ``refresh_from_cs``, the JSON
    service dispatches it and returns both the change list and a
    fresh full snapshot so the UI can re-render without a follow-up
    ``list`` round-trip."""
    calls: list[dict] = []

    async def fake_refresh(**kwargs):
        calls.append(kwargs)
        return [10, 20]

    handle.ctx.connection.consists.refresh_from_cs = fake_refresh  # type: ignore[attr-defined]
    with TestClient(handle.app) as tc:
        tc.post("/json/", json={
            "type": "consist", "method": "put", "data": {"address": 10},
        })
        r = tc.post("/json/", json={
            "type": "consist", "method": "post",
            "data": {"action": "refresh_from_cs"},
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["changed"] == [10, 20]
        # A snapshot of every consist comes along for the ride.
        addrs = {c["address"] for c in data["consists"]}
        assert 10 in addrs
        assert calls == [{}]


def test_refresh_from_cs_forwards_range_bounds(handle) -> None:
    """Optional ``min_consist`` / ``max_consist`` reach the manager
    call so operators can spot-check a slot instead of the full 127."""
    calls: list[dict] = []

    async def fake_refresh(**kwargs):
        calls.append(kwargs)
        return []

    handle.ctx.connection.consists.refresh_from_cs = fake_refresh  # type: ignore[attr-defined]
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "consist", "method": "post",
            "data": {
                "action": "refresh_from_cs",
                "min_consist": 5, "max_consist": 8,
            },
        })
        assert r.status_code == 200
        assert calls == [{"min_consist": 5, "max_consist": 8}]


def test_refresh_from_cs_rejects_non_int_bounds(handle) -> None:
    async def fake_refresh(**kwargs):
        return []

    handle.ctx.connection.consists.refresh_from_cs = fake_refresh  # type: ignore[attr-defined]
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "consist", "method": "post",
            "data": {
                "action": "refresh_from_cs",
                "min_consist": "banana",
            },
        })
        assert r.status_code == 400
