"""Configuration.xml persistence end-to-end.

These tests prove the alpha-test promise:

1. Beans created via the JSON API are persisted to Configuration.xml.
2. Restarting the server reloads those beans from disk.
3. Importing a JMRI-format Configuration.xml populates the bean managers.
4. Saving doesn't lose unknown sections (signal heads, blocks, etc.) --
   so users can edit in our UI without breaking JMRI compatibility.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from pmri_core.persistence import Profile

from pmri_server.app import build_app
from pmri_server.config_store import ConfigStore


@pytest.fixture()
def isolated_profile(tmp_path: pathlib.Path) -> Profile:
    profile = Profile(root=tmp_path, name="alpha")
    profile.ensure()
    return profile


def _client(profile: Profile):
    handle = build_app()
    # Override the lifespan's default profile + pre-attach the config store
    # so the test exercises the same path as a real server start.
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    handle.ctx.config_store = ConfigStore(profile=profile)
    return TestClient(handle.app), handle


# ----- new bean via API -> persisted to disk -------------------------------


def test_turnout_put_writes_configuration_xml(isolated_profile: Profile) -> None:
    client, _ = _client(isolated_profile)
    with client:
        client.post("/json/", json={
            "type": "turnout", "method": "put",
            "data": {"name": "IT80", "userName": "Yard Ladder", "inverted": True},
        })
    cfg_path = isolated_profile.config_file
    assert cfg_path.exists()
    raw = cfg_path.read_text(encoding="utf-8")
    assert "IT80" in raw
    assert "Yard Ladder" in raw
    assert 'inverted="true"' in raw


def test_turnout_class_attribute_is_pmri_schema_valid(isolated_profile: Profile) -> None:
    """JMRI Java's ``layout-*.xsd`` schema requires the ``class`` attribute
    on ``<turnouts>`` to match ``[a-zA-Z0-9.]*[a-z]`` (last char lowercase).
    The abstract class name we used to emit
    (``AbstractTurnoutManagerConfigXML``) ends in uppercase ``L`` and made
    the file fail to open in JMRI Java at startup.

    Uses the store directly (bypassing manager-side name normalization)
    to place beans under two different system prefixes.
    """
    import re  # noqa: PLC0415
    from pmri_core.persistence import TurnoutRow  # noqa: PLC0415

    store = ConfigStore(profile=isolated_profile)
    store.upsert_turnout(TurnoutRow(system_name="IT80"))
    store.upsert_turnout(TurnoutRow(system_name="NT40"))
    raw = isolated_profile.config_file.read_text(encoding="utf-8")

    schema_valid = re.compile(r"^[a-zA-Z0-9.]*[a-z]$")
    class_attrs = re.findall(r'<turnouts class="([^"]+)"', raw)
    assert class_attrs, "no <turnouts class=..> element found in emitted XML"
    for value in class_attrs:
        assert schema_valid.match(value), (
            f"turnout manager_class {value!r} is schema-invalid -- JMRI Java "
            f"will refuse to parse the file at startup"
        )
    # Concrete driver-specific classes emitted per prefix.
    assert any("InternalTurnoutManagerXml" in v for v in class_attrs)
    assert any("NceTurnoutManagerXml" in v for v in class_attrs)


def test_beans_are_grouped_by_system_prefix(isolated_profile: Profile) -> None:
    """Beans with different system prefixes land in *separate* sections
    so JMRI can dispatch each to the right driver's config loader."""
    from pmri_core.persistence import TurnoutRow  # noqa: PLC0415
    store = ConfigStore(profile=isolated_profile)
    store.upsert_turnout(TurnoutRow(system_name="IT80"))
    store.upsert_turnout(TurnoutRow(system_name="NT40"))
    raw = isolated_profile.config_file.read_text(encoding="utf-8")
    # Two <turnouts> blocks -- one per prefix.
    assert raw.count("<turnouts ") == 2


def test_sensor_and_light_persist(isolated_profile: Profile) -> None:
    client, _ = _client(isolated_profile)
    with client:
        client.post("/json/", json={
            "type": "sensor", "method": "put",
            "data": {"name": "IS3", "userName": "Block 3"},
        })
        client.post("/json/", json={
            "type": "light", "method": "put",
            "data": {"name": "IL1", "userName": "Yard Lamp"},
        })
    raw = isolated_profile.config_file.read_text(encoding="utf-8")
    assert "IS3" in raw and "Block 3" in raw
    assert "IL1" in raw and "Yard Lamp" in raw


def test_delete_removes_from_configuration_xml(isolated_profile: Profile) -> None:
    client, _ = _client(isolated_profile)
    with client:
        client.post("/json/", json={
            "type": "turnout", "method": "put", "data": {"name": "IT80"},
        })
        assert "IT80" in isolated_profile.config_file.read_text(encoding="utf-8")
        client.post("/json/", json={
            "type": "turnout", "method": "delete", "data": {"name": "IT80"},
        })
    assert "IT80" not in isolated_profile.config_file.read_text(encoding="utf-8")


# ----- restart restore -----------------------------------------------------


def test_beans_restored_on_restart(isolated_profile: Profile) -> None:
    """First server creates beans; second server with the same profile sees them."""
    # Session 1: create beans
    client1, _ = _client(isolated_profile)
    with client1:
        client1.post("/json/", json={
            "type": "turnout", "method": "put",
            "data": {"name": "IT80", "userName": "Yard Ladder"},
        })
        client1.post("/json/", json={
            "type": "sensor", "method": "put",
            "data": {"name": "IS3", "userName": "Block 3"},
        })

    # Session 2: fresh app, same profile -- beans should pre-populate
    client2, _ = _client(isolated_profile)
    with client2:
        r = client2.post("/json/", json={
            "type": "turnout", "method": "get", "data": {"name": "IT80"},
        })
        r.raise_for_status()
        assert r.json()["data"]["userName"] == "Yard Ladder"
        r = client2.post("/json/", json={
            "type": "sensor", "method": "get", "data": {"name": "IS3"},
        })
        r.raise_for_status()
        assert r.json()["data"]["userName"] == "Block 3"


# ----- importing an existing JMRI file -------------------------------------


def test_import_existing_pmri_config(isolated_profile: Profile) -> None:
    """Drop a real JMRI Configuration.xml in and the server reads beans from it.

    Uses the golden Configuration.xml from stage-0. Its turnouts are
    LocoNet-prefixed (LT...), but the InternalConnection has prefix "I" --
    so we only expect the (initially-empty) internal beans to be
    pre-populated. The LocoNet beans round-trip on save without being
    materialized as live beans.
    """
    # Seed the profile's config file with the golden JMRI sample.
    golden = (
        pathlib.Path(__file__).parents[3]
        / "stage-0/contracts/xml/golden/samples/Configuration.xml"
    ).resolve()
    isolated_profile.config_file.write_bytes(golden.read_bytes())

    client, _ = _client(isolated_profile)
    with client:
        # Adding a new internal turnout should not wipe the existing JMRI
        # sections -- the imported file's signalheads, blocks, memories
        # must round-trip.
        client.post("/json/", json={
            "type": "turnout", "method": "put",
            "data": {"name": "IT99", "userName": "Test"},
        })

    raw = isolated_profile.config_file.read_text(encoding="utf-8")
    # New bean is there
    assert "IT99" in raw
    # Existing JMRI sections preserved
    assert "<signalheads" in raw
    assert "<memories" in raw
    assert "<blocks" in raw
    # LocoNet-prefixed turnouts not materialized but persisted
    assert "LT200" in raw


def test_pmri_5x_systemname_extras_survive_web_edit(isolated_profile: Profile) -> None:
    """Real JMRI 5.x file: systemName as child + extra attribs (locked,
    lockMode, decoder, automate) must round-trip even when the user edits
    the bean's userName through the web UI."""
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<layout-config>
  <turnouts class="jmri.jmrix.nce.configurexml.NceTurnoutManagerXml">
    <turnout feedback="MONITORING" inverted="false" locked="false" lockMode="cab" decoder="None" automate="Off">
      <systemName>IT40</systemName>
      <userName>Old Name</userName>
    </turnout>
  </turnouts>
</layout-config>
"""
    isolated_profile.config_file.write_bytes(body)

    client, _ = _client(isolated_profile)
    with client:
        # Rename via web UI.
        client.post("/json/", json={
            "type": "turnout", "method": "post",
            "data": {"name": "IT40", "userName": "New Name"},
        })

    raw = isolated_profile.config_file.read_text(encoding="utf-8")
    assert "New Name" in raw
    assert "Old Name" not in raw
    # Extras the bean services don't know about must survive.
    assert 'locked="false"' in raw
    assert 'lockMode="cab"' in raw
    assert 'decoder="None"' in raw
    # systemName remains a child element (5.x shape preserved on write).
    assert "<systemName>IT40</systemName>" in raw
