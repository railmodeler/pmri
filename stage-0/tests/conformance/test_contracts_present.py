"""Stage 0 sanity tests -- every contract file is present and parseable.

These are the bedrock tests Stage 1 will build on. Future Stage 1+ tests in
this directory will additionally exercise ``pmri_core`` against each contract.
"""
from __future__ import annotations

import json
import pathlib

import pytest

# These imports are deferred until Stage 1 introduces them.
# jsonschema = pytest.importorskip("jsonschema")
# from lxml import etree


def test_json_schema_count(json_schema_dir: pathlib.Path) -> None:
    files = list(json_schema_dir.rglob("*.json"))
    assert len(files) == 97, f"expected 97 JSON schemas, got {len(files)}"


def test_xml_schema_count(xml_schema_dir: pathlib.Path) -> None:
    files = list(xml_schema_dir.glob("*.xsd"))
    # 33 .xsd + 4 non-xsd files (README, JMRIschema.xsd-skipped variants, etc.)
    assert len(files) >= 30, f"expected at least 30 XSDs, got {len(files)}"


def test_golden_xml_count(golden_xml_dir: pathlib.Path) -> None:
    files = list(golden_xml_dir.rglob("*.xml"))
    assert len(files) >= 30, f"expected at least 30 golden files, got {len(files)}"


def test_json_schema_parses(load_json_schema: object) -> None:
    """Every schema file is valid JSON.

    Most files are JSON Schema objects ({"$schema": ..., "type": ...}); a few
    are resolver manifests (a JSON array mapping public URLs to local paths).
    Both are valid -- we only assert the file parses successfully.
    """
    assert isinstance(load_json_schema, (dict, list))
    if isinstance(load_json_schema, dict):
        assert (
            "$schema" in load_json_schema
            or "type" in load_json_schema
            or "properties" in load_json_schema
            or "$ref" in load_json_schema
        )


def test_golden_xml_parses(golden_xml_file: pathlib.Path) -> None:
    """Every golden file parses as XML (catches truncation / corruption early)."""
    from xml.etree import ElementTree as ET  # noqa: PLC0415
    try:
        ET.parse(golden_xml_file)
    except ET.ParseError as e:
        pytest.fail(f"{golden_xml_file.name} failed to parse: {e}")


def test_golden_xml_roundtrip(golden_xml_file: pathlib.Path) -> None:
    """Round-trip golden files through ``pmri_core.persistence``.

    Supported as of Stage 2:
      * ``locomotive-config`` -- full lossless (byte-equal after two saves)
      * ``operations-config`` -- semantic (model-equal; byte-equal is Stage 2.5)
      * ``layout-config``     -- envelope-level (tree preserved)
      * ``decoder-config``    -- read-only (parse-only)

    Unsupported formats skip with a clear marker.
    """
    from xml.etree import ElementTree as ET  # noqa: PLC0415

    root_tag = ET.parse(golden_xml_file).getroot().tag

    if root_tag == "locomotive-config":
        from pmri_core.persistence import (  # noqa: PLC0415
            load_locomotive_bytes,
            save_locomotive_bytes,
        )
        original = golden_xml_file.read_bytes()
        once = load_locomotive_bytes(original)
        emitted = save_locomotive_bytes(once)
        twice = load_locomotive_bytes(emitted)
        assert once == twice, "second-pass load must equal first-pass load"
        emitted2 = save_locomotive_bytes(twice)
        assert emitted == emitted2, "two save passes must produce identical bytes"
        return

    if root_tag == "operations-config":
        from pmri_core.persistence import (  # noqa: PLC0415
            load_operations_bytes,
            save_operations_bytes,
        )
        once = load_operations_bytes(golden_xml_file.read_bytes())
        emitted = save_operations_bytes(once)
        twice = load_operations_bytes(emitted)
        # Semantic equality: same kind + same counts of major elements.
        assert once.kind == twice.kind
        assert len(once.engines()) == len(twice.engines())
        assert len(once.cars()) == len(twice.cars())
        assert len(once.routes()) == len(twice.routes())
        assert len(once.trains()) == len(twice.trains())
        assert len(once.locations()) == len(twice.locations())
        return

    if root_tag == "layout-config":
        from pmri_core.persistence import (  # noqa: PLC0415
            load_panel_bytes,
            save_panel_bytes,
        )
        once = load_panel_bytes(golden_xml_file.read_bytes())
        emitted = save_panel_bytes(once)
        twice = load_panel_bytes(emitted)
        assert once.root_tag == twice.root_tag
        assert len(list(once.root)) == len(list(twice.root))
        return

    if root_tag == "decoder-config":
        # Read-only support -- parsing without error is the contract.
        from pmri_core.persistence import load_decoder_bytes  # noqa: PLC0415
        load_decoder_bytes(golden_xml_file.read_bytes())
        return

    pytest.skip(f"Format <{root_tag}> not yet supported (Stage 2.5+)")


def test_server_response_matches_schema(
    load_json_schema: object, json_schema_file: pathlib.Path
) -> None:
    """Stage 3: requesting the topic against pmri-server emits a response whose
    data validates against the corresponding JMRI JSON schema.

    For each ``-server.json`` schema we exercise the topic; for ``-client.json``
    schemas we skip -- they describe what the server *accepts*, not what it emits.
    """
    name = json_schema_file.name
    if not name.endswith("-server.json"):
        pytest.skip("client-side schemas describe inputs, not server responses")

    topic = name.removesuffix("-server.json")

    # Topics implemented in Stage 3. Anything else: skip until Stage 3.5+.
    IMPLEMENTED_TOPICS = {
        "hello", "version", "railroad", "node", "power",
        "turnout", "sensor", "light", "signalHead", "signalMast",
        "memory", "reporter", "block", "route",
    }
    if topic not in IMPLEMENTED_TOPICS:
        pytest.skip(f"topic {topic!r} not implemented in Stage 3")

    # Skip non-data envelope schemas (`json-server`, `schema-server`, etc.).
    if not isinstance(load_json_schema, dict) or "properties" not in load_json_schema:
        pytest.skip("envelope-level schema, not a data payload")

    # Build a fresh server and ask the topic for a representative response.
    from fastapi.testclient import TestClient  # noqa: PLC0415
    import jsonschema  # noqa: PLC0415

    from pmri_server.app import build_app  # noqa: PLC0415

    handle = build_app()
    with TestClient(handle.app) as client:
        # For bean topics we need an existing bean before `get` returns data.
        if topic in {"turnout", "sensor", "light", "signalHead", "signalMast",
                     "memory", "reporter", "block", "route"}:
            sample_name = f"I{_TYPE_LETTER[topic]}1"
            client.post("/json/", json={
                "type": topic, "method": "put", "data": {"name": sample_name},
            })
            r = client.post("/json/", json={
                "type": topic, "method": "get", "data": {"name": sample_name},
            })
        else:
            r = client.post("/json/", json={"type": topic, "method": "get"})

    r.raise_for_status()
    data = r.json()["data"]
    jsonschema.validate(data, load_json_schema)


_TYPE_LETTER = {
    "turnout": "T", "sensor": "S", "light": "L", "signalHead": "H",
    "signalMast": "F", "memory": "M", "reporter": "R", "block": "B", "route": "O",
}
