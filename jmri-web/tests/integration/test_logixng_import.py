"""End-to-end test for the /logixng/import-xml upload path.

Uploads a synthetic JMRI XML, verifies:
* Response reports the imported programs.
* Programs land in the manager (visible via logixng.list).
* Unsupported-tag warnings surface in the response body.
"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient


_SAMPLE_XML = (
    "<layout-config>"
    "<LogixNGs>"
    "  <LogixNG>"
    "    <systemName>IQ:IMPORTED:1</systemName>"
    "    <userName>morning startup</userName>"
    "    <ConditionalNGs>"
    "      <systemName>IQC:IMPORTED:1</systemName>"
    "    </ConditionalNGs>"
    "  </LogixNG>"
    "</LogixNGs>"
    "<LogixNGConditionalNGs>"
    "  <ConditionalNG>"
    "    <systemName>IQC:IMPORTED:1</systemName>"
    "    <Socket>"
    "      <socketName>A1</socketName>"
    "      <systemName>IQDA:IMPORTED:1</systemName>"
    "    </Socket>"
    "  </ConditionalNG>"
    "</LogixNGConditionalNGs>"
    "<LogixNGDigitalActions>"
    "  <ActionTurnout>"
    "    <systemName>IQDA:IMPORTED:1</systemName>"
    "    <namedBean>"
    "      <addressing>Direct</addressing>"
    "      <systemName>IT42</systemName>"
    "    </namedBean>"
    "    <state>"
    "      <addressing>Direct</addressing>"
    "      <enum>THROWN</enum>"
    "    </state>"
    "  </ActionTurnout>"
    "</LogixNGDigitalActions>"
    "</layout-config>"
)


def test_upload_imports_programs_into_manager(
    client: TestClient,
) -> None:
    """The imported program shows up in ``logixng.list`` right after
    the upload, without a full page reload."""
    files = {"file": ("panel.xml", io.BytesIO(_SAMPLE_XML.encode("utf-8")), "text/xml")}
    r = client.post("/logixng/import-xml", files=files)
    assert r.status_code == 200
    assert "Imported 1 program" in r.text
    # Colons in the imported name get sanitized to dots for
    # Windows-portable persistence.
    assert "IQ.IMPORTED.1" in r.text

    # Verify it's actually in the manager via the JSON API.
    r2 = client.post("/json/", json={
        "type": "logixng", "method": "list",
    })
    r2.raise_for_status()
    names = {p["name"] for p in r2.json()["data"]}
    assert "IQ.IMPORTED.1" in names


def test_upload_empty_file_returns_400(client: TestClient) -> None:
    files = {"file": ("empty.xml", io.BytesIO(b""), "text/xml")}
    r = client.post("/logixng/import-xml", files=files)
    assert r.status_code == 400
    assert "empty file" in r.text.lower()


def test_upload_bad_xml_returns_400(client: TestClient) -> None:
    files = {"file": ("bad.xml", io.BytesIO(b"not xml at all"), "text/xml")}
    r = client.post("/logixng/import-xml", files=files)
    assert r.status_code == 400
    assert "failed to parse" in r.text.lower()


def test_upload_surfaces_warnings(client: TestClient) -> None:
    """Uploading an XML with an unsupported tag succeeds but reports
    the warning in the response body."""
    xml = (
        "<layout-config>"
        "<LogixNGs>"
        "  <LogixNG>"
        "    <systemName>IQ:UNK:1</systemName>"
        "    <userName>with unknown</userName>"
        "    <ConditionalNGs><systemName>IQC:UNK:1</systemName></ConditionalNGs>"
        "  </LogixNG>"
        "</LogixNGs>"
        "<LogixNGConditionalNGs>"
        "  <ConditionalNG>"
        "    <systemName>IQC:UNK:1</systemName>"
        "    <Socket>"
        "      <socketName>A1</socketName>"
        "      <systemName>IQDA:UNK:1</systemName>"
        "    </Socket>"
        "  </ConditionalNG>"
        "</LogixNGConditionalNGs>"
        "<LogixNGDigitalActions>"
        "  <ActionRunScript>"
        "    <systemName>IQDA:UNK:1</systemName>"
        "  </ActionRunScript>"
        "</LogixNGDigitalActions>"
        "</layout-config>"
    )
    files = {"file": ("unk.xml", io.BytesIO(xml.encode("utf-8")), "text/xml")}
    r = client.post("/logixng/import-xml", files=files)
    assert r.status_code == 200
    assert "warning" in r.text.lower()
    assert "unknown-tag" in r.text
