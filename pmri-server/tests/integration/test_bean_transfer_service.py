"""Integration tests for the ``beanTransfer`` JSON topic.

Sets up an isolated settings root with two profiles + a
``profiles.xml``, seeds the source profile's ``PanelPro.xml`` with
a few turnout / sensor / light rows via the existing bean services,
then exercises the preview + apply paths.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from pmri_core.persistence.profile import Profile
from pmri_core.persistence.profile_registry import ProfileRef, ProfileRegistry

from pmri_server.app import build_app
from pmri_server.config_store import ConfigStore


def _make_registry(settings_root: pathlib.Path) -> ProfileRegistry:
    """Two-profile registry: ``dev`` (active) + ``prod``. Directories
    live at ``<settings_root>/dev`` and ``<settings_root>/prod`` so
    the ProfileRef ``path`` values resolve cleanly."""
    (settings_root / "dev").mkdir(parents=True, exist_ok=True)
    (settings_root / "prod").mkdir(parents=True, exist_ok=True)
    registry = ProfileRegistry(
        settings_root=settings_root,
        profiles=[
            ProfileRef(id="dev-1", path="settings:dev/", name="dev"),
            ProfileRef(id="prod-1", path="settings:prod/", name="prod"),
        ],
    )
    return registry


@pytest.fixture()
def handle_with_profiles(tmp_path: pathlib.Path):
    """Wire the handle to a two-profile registry with dev active,
    and pre-populate dev's PanelPro.xml with a few bean rows."""
    from pmri_core.persistence import TurnoutRow, SensorRow, LightRow  # noqa: PLC0415

    settings_root = tmp_path
    registry = _make_registry(settings_root)
    handle = build_app()
    handle.ctx.profile_registry = registry

    dev_profile = Profile(root=settings_root, name="dev")
    dev_store = ConfigStore(profile=dev_profile)
    dev_store.upsert_turnout(TurnoutRow(system_name="IT1", user_name="Main"))
    dev_store.upsert_turnout(TurnoutRow(system_name="IT2", user_name="Yard"))
    dev_store.upsert_sensor(SensorRow(system_name="IS10", user_name="Block 10"))
    dev_store.upsert_light(LightRow(system_name="IL5", user_name="Signal 5"))

    # Point the running server at dev (so target-is-active checks work).
    handle.ctx.config_store = dev_store

    return handle, registry, settings_root


# ---- list ----------------------------------------------------------------


def test_list_returns_profiles_with_counts(handle_with_profiles) -> None:
    handle, _, _ = handle_with_profiles
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "list",
        })
        assert r.status_code == 200
        data = r.json()["data"]
        ids = {p["id"] for p in data["profiles"]}
        assert ids == {"dev-1", "prod-1"}
        dev = next(p for p in data["profiles"] if p["id"] == "dev-1")
        prod = next(p for p in data["profiles"] if p["id"] == "prod-1")
        assert dev["counts"]["turnout"] == 2
        assert dev["counts"]["sensor"] == 1
        assert dev["counts"]["light"] == 1
        assert dev["counts"]["roster"] == 0
        assert prod["counts"] == {
            "turnout": 0, "sensor": 0, "light": 0, "roster": 0,
        }
        assert data["types"] == ["turnout", "sensor", "light", "roster"]


def test_list_empty_when_no_registry(handle_with_profiles) -> None:
    """Bare context (no profiles.xml discovered) -> empty profile
    list, no crash."""
    handle, _, _ = handle_with_profiles
    handle.ctx.profile_registry = None
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "list",
        })
        assert r.status_code == 200
        assert r.json()["data"]["profiles"] == []


# ---- preview -------------------------------------------------------------


def test_preview_reports_counts_and_overwrites(
    handle_with_profiles,
) -> None:
    handle, _, _ = handle_with_profiles
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "preview",
                "from": "dev-1", "to": "prod-1",
                "types": ["turnout", "sensor"],
            },
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["counts"] == {"turnout": 2, "sensor": 1}
        # No overlaps because prod is empty.
        assert data["willOverwrite"] == {"turnout": 0, "sensor": 0}


def test_preview_counts_overwrites_when_target_has_same_names(
    handle_with_profiles, tmp_path,
) -> None:
    """Seed prod with a same-named row so the overwrite count > 0."""
    from pmri_core.persistence import TurnoutRow  # noqa: PLC0415

    handle, _, settings_root = handle_with_profiles
    prod_profile = Profile(root=settings_root, name="prod")
    prod_store = ConfigStore(profile=prod_profile)
    prod_store.upsert_turnout(TurnoutRow(
        system_name="IT1", user_name="Overwritten"
    ))
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "preview",
                "from": "dev-1", "to": "prod-1",
                "types": ["turnout"],
            },
        })
        data = r.json()["data"]
        assert data["counts"]["turnout"] == 2
        assert data["willOverwrite"]["turnout"] == 1


def test_preview_rejects_same_source_and_target(
    handle_with_profiles,
) -> None:
    handle, _, _ = handle_with_profiles
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "preview",
                "from": "dev-1", "to": "dev-1",
                "types": ["turnout"],
            },
        })
        assert r.status_code == 400


def test_preview_rejects_unknown_type(handle_with_profiles) -> None:
    handle, _, _ = handle_with_profiles
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "preview",
                "from": "dev-1", "to": "prod-1",
                "types": ["turnout", "signalMast"],
            },
        })
        assert r.status_code == 400


def test_preview_rejects_unknown_profile_id(
    handle_with_profiles,
) -> None:
    handle, _, _ = handle_with_profiles
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "preview",
                "from": "no-such", "to": "prod-1",
                "types": ["turnout"],
            },
        })
        assert r.status_code == 404


# ---- apply ---------------------------------------------------------------


def test_apply_upserts_into_target(handle_with_profiles) -> None:
    """After apply, the target's PanelPro.xml carries the source's
    rows. Reload the target's store from disk to verify the
    persistence (not just the in-memory copy)."""
    handle, _, settings_root = handle_with_profiles
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "apply",
                "from": "dev-1", "to": "prod-1",
                "types": ["turnout", "sensor", "light"],
            },
        })
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["copied"] == {"turnout": 2, "sensor": 1, "light": 1}
        # Target is prod, dev is active -> not active.
        assert data["targetIsActive"] is False

    # Fresh store from disk sees the newly-written rows.
    prod_profile = Profile(root=settings_root, name="prod")
    reloaded = ConfigStore(profile=prod_profile)
    assert {r.system_name for r in reloaded.turnouts()} == {"IT1", "IT2"}
    assert {r.system_name for r in reloaded.sensors()} == {"IS10"}
    assert {r.system_name for r in reloaded.lights()} == {"IL5"}


def test_apply_into_active_reports_target_is_active(
    handle_with_profiles, tmp_path,
) -> None:
    """Copy from prod -> dev (active). Even though prod is empty
    right now, the apply must report ``targetIsActive: True`` so the
    UI knows to expect the live-manager refresh."""
    from pmri_core.persistence import TurnoutRow  # noqa: PLC0415

    handle, _, settings_root = handle_with_profiles
    # Seed prod with something to copy.
    prod_profile = Profile(root=settings_root, name="prod")
    prod_store = ConfigStore(profile=prod_profile)
    prod_store.upsert_turnout(TurnoutRow(
        system_name="IT99", user_name="From prod"
    ))
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "apply",
                "from": "prod-1", "to": "dev-1",
                "types": ["turnout"],
            },
        })
        data = r.json()["data"]
        assert data["targetIsActive"] is True
        assert data["copied"]["turnout"] == 1


def test_unknown_action_400s(handle_with_profiles) -> None:
    handle, _, _ = handle_with_profiles
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {"action": "delete_everything"},
        })
        assert r.status_code == 400


# ---- roster --------------------------------------------------------------
#
# The roster kind lives in a separate on-disk store (RosterStore) with
# ``entry.id`` playing the "system name" role. Regression pin: an earlier
# version of the service only handled turnout/sensor/light, so a user
# who set up a fresh DCC-EX profile found they had no way to bring their
# roster over from an existing profile short of copying files by hand.


def _seed_roster(profile: Profile, ids: list[str]) -> None:
    from pmri_core.beans.roster_entry import RosterEntry  # noqa: PLC0415

    from pmri_server.roster_store import RosterStore  # noqa: PLC0415
    store = RosterStore(profile=profile)
    for eid in ids:
        store.put(RosterEntry(id=eid, dcc_address="3", long_address=False))


def test_roster_counted_in_list(handle_with_profiles) -> None:
    """A profile with N roster entries reports counts['roster'] == N."""
    handle, _, settings_root = handle_with_profiles
    dev_profile = Profile(root=settings_root, name="dev")
    _seed_roster(dev_profile, ["UP_3601", "SP_5"])
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "list",
        })
        data = r.json()["data"]
        dev = next(p for p in data["profiles"] if p["id"] == "dev-1")
        assert dev["counts"]["roster"] == 2


def test_preview_reports_roster_counts_and_overwrites(
    handle_with_profiles,
) -> None:
    handle, _, settings_root = handle_with_profiles
    _seed_roster(Profile(root=settings_root, name="dev"),
                 ["UP_3601", "SP_5"])
    _seed_roster(Profile(root=settings_root, name="prod"),
                 ["UP_3601"])   # id collision -> overwrite
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "preview",
                "from": "dev-1", "to": "prod-1",
                "types": ["roster"],
            },
        })
        data = r.json()["data"]
        assert data["counts"]["roster"] == 2
        assert data["willOverwrite"]["roster"] == 1


def test_apply_copies_roster_between_profiles(handle_with_profiles) -> None:
    """After apply, prod's roster dir carries dev's entries. The file
    is loadable by a fresh RosterStore -- proves the transfer wrote
    both the per-entry XML and the ``roster.xml`` index."""
    from pmri_server.roster_store import RosterStore  # noqa: PLC0415

    handle, _, settings_root = handle_with_profiles
    _seed_roster(Profile(root=settings_root, name="dev"),
                 ["UP_3601", "SP_5"])
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "apply",
                "from": "dev-1", "to": "prod-1",
                "types": ["roster"],
            },
        })
        assert r.status_code == 200
        assert r.json()["data"]["copied"]["roster"] == 2

    reloaded = RosterStore(profile=Profile(root=settings_root, name="prod"))
    assert {e.id for e in reloaded} == {"UP_3601", "SP_5"}


def test_apply_roster_into_active_uses_shared_store(
    handle_with_profiles,
) -> None:
    """Roster copied INTO the active profile must land in the
    ctx-shared RosterStore so any WiThrottle rebroadcast subscribers
    (attached via ``_attach_roster_signals``) see the mutation.
    Pin: without shared-store handling, only the on-disk copy would
    change and Engine Driver would miss the update until reconnect."""
    from pmri_server.roster_store import RosterStore  # noqa: PLC0415

    handle, _, settings_root = handle_with_profiles
    # Seed prod (source) with an entry to move into the active dev.
    _seed_roster(Profile(root=settings_root, name="prod"), ["ATSF_42"])
    # Prime the active-profile shared store on ctx.
    handle.ctx._roster_store = RosterStore(
        profile=Profile(root=settings_root, name="dev"),
    )
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={
            "type": "beanTransfer", "method": "post",
            "data": {
                "action": "apply",
                "from": "prod-1", "to": "dev-1",
                "types": ["roster"],
            },
        })
        assert r.status_code == 200
        assert r.json()["data"]["targetIsActive"] is True
    # Same instance shows the new entry -- proves the copy went through
    # the shared store, not a throwaway.
    assert "ATSF_42" in {e.id for e in handle.ctx._roster_store}
