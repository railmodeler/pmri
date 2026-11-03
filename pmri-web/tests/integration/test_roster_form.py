"""Tests for the extended roster add/edit form (Tier-3 follow-up).

Covers:
* the four newly-form-visible scalar fields (mfg / model / owner /
  maxSpeedPct) round-trip through create + update;
* the decoder-picker section is present when the catalog is loaded
  and hides behind a hint otherwise;
* the auto-load helper does nothing without ``JMRI_DECODER_XML_ROOT``
  and something with it.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from pmri_core.persistence.profile import Profile

from pmri_web.app import build_web_app


@pytest.fixture()
def handle_with_profile(tmp_path: pathlib.Path):
    """Web handle wired to an isolated profile so roster writes don't
    touch the developer's real ``~/JMRI``."""
    profile = Profile(root=tmp_path, name="test")
    h = build_web_app()
    h.server.ctx._roster_profile = profile  # type: ignore[attr-defined]
    return h


# ---- extended scalar fields ---------------------------------------------


def test_create_captures_new_fields(handle_with_profile) -> None:
    """Mfg / model / owner / maxSpeedPct entered on the create form
    must land on the projected entry -- previously the form dropped
    them silently."""
    with TestClient(handle_with_profile.app) as tc:
        r = tc.post("/roster/create", data={
            "id": "TEST_1",
            "dccAddress": "1234",
            "longAddress": "1",
            "roadName": "Union Pacific",
            "roadNumber": "3601",
            "mfg": "Athearn",
            "model": "Genesis SD70ACe",
            "owner": "alice",
            "maxSpeedPct": "88",
            "decoderFamily": "ESU LokSound v5",
            "decoderModel": "LokSound 5",
        })
        r.raise_for_status()
        got = tc.post("/json/", json={
            "type": "roster", "method": "get", "data": {"id": "TEST_1"},
        }).json()["data"]
        assert got["mfg"] == "Athearn"
        assert got["model"] == "Genesis SD70ACe"
        assert got["owner"] == "alice"
        assert got["maxSpeedPct"] == 88


def test_update_captures_new_fields(handle_with_profile) -> None:
    """Same fields via the edit-row POST."""
    with TestClient(handle_with_profile.app) as tc:
        tc.post("/roster/create", data={
            "id": "TEST_2", "dccAddress": "42",
        }).raise_for_status()
        r = tc.post("/roster/TEST_2/update", data={
            "dccAddress": "42",
            "longAddress": "0",
            "roadName": "BNSF",
            "roadNumber": "7000",
            "mfg": "Kato",
            "model": "SD70MAC",
            "owner": "bob",
            "maxSpeedPct": "75",
            "decoderFamily": "",
            "decoderModel": "",
        })
        r.raise_for_status()
        got = tc.post("/json/", json={
            "type": "roster", "method": "get", "data": {"id": "TEST_2"},
        }).json()["data"]
        assert got["mfg"] == "Kato"
        assert got["owner"] == "bob"
        assert got["maxSpeedPct"] == 75


# ---- decoder picker visibility -----------------------------------------


def test_picker_hidden_when_catalog_not_loaded(
    handle_with_profile,
) -> None:
    """With no catalog loaded the roster page should show the hint
    (link to Programmer page) rather than the picker dropdowns."""
    with TestClient(handle_with_profile.app) as tc:
        r = tc.get("/roster")
        r.raise_for_status()
        body = r.text
        # Hint text is present.
        assert "Load the decoder catalog on the" in body
        # Picker id is NOT present because the whole select is
        # rendered behind ``{% if catalog_loaded %}``.
        assert 'id="mfg-select"' not in body
        assert 'id="identify-result"' not in body


def test_picker_visible_when_catalog_loaded(
    handle_with_profile,
) -> None:
    """A populated catalog triggers the picker + Identify button
    rendering. We stuff a tiny in-memory catalog directly onto the
    ctx (bypassing the async loader) -- the page-side check is just
    ``len(_catalog()) > 0``."""
    from pmri_core.persistence.decoder_xml import DecoderIndexEntry  # noqa: PLC0415
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    cat = DecoderCatalog()
    cat.add_index_entry(DecoderIndexEntry(
        mfg="TestMfg", family="TestFamily", model="TestModel",
        source_file=pathlib.Path("/nowhere.xml"),
    ))
    handle_with_profile.server.ctx._decoder_catalog = cat
    with TestClient(handle_with_profile.app) as tc:
        r = tc.get("/roster")
        r.raise_for_status()
        body = r.text
        assert 'id="mfg-select"' in body
        assert "Identify decoder on program track" in body
        assert "TestMfg" in body   # option in the mfg dropdown


# ---- auto-load ----------------------------------------------------------


def test_autoload_skipped_when_env_unset(handle_with_profile) -> None:
    """No ``JMRI_DECODER_XML_ROOT`` set -> schedule_autoload returns
    None and the catalog stays empty."""
    from pmri_web.catalog_autoload import schedule_autoload  # noqa: PLC0415

    # Nothing on the env -- the helper must return None.
    assert schedule_autoload(handle_with_profile.server) is None


def test_autoload_skipped_when_dir_missing(
    handle_with_profile, monkeypatch, tmp_path,
) -> None:
    """Env set but pointing at a non-existent dir -- no task, no
    crash. Broken config shouldn't take down the app."""
    from pmri_web.catalog_autoload import schedule_autoload  # noqa: PLC0415

    monkeypatch.setenv(
        "JMRI_DECODER_XML_ROOT", str(tmp_path / "not-there"),
    )
    assert schedule_autoload(handle_with_profile.server) is None


async def test_autoload_populates_catalog(
    handle_with_profile, monkeypatch, tmp_path,
) -> None:
    """Env set + dir exists -> the worker runs and populates
    ``ctx._decoder_catalog``. Empty dir -> empty catalog, still
    non-None (proves the loader ran)."""
    from pmri_web.catalog_autoload import schedule_autoload  # noqa: PLC0415

    monkeypatch.setenv("JMRI_DECODER_XML_ROOT", str(tmp_path))
    task = schedule_autoload(handle_with_profile.server)
    assert task is not None
    await task
    catalog = handle_with_profile.server.ctx._decoder_catalog  # noqa: SLF001
    assert catalog is not None
