"""Decoder programmer page tests."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_programmer_page_renders(client: TestClient) -> None:
    r = client.get("/programmer")
    r.raise_for_status()
    assert "Decoder Programmer" in r.text
    assert "Connection capabilities" in r.text


def test_catalog_initially_empty(client: TestClient) -> None:
    r = client.get("/programmer")
    r.raise_for_status()
    assert "Catalog empty" in r.text


def test_catalog_load_via_form(client: TestClient) -> None:
    """Load POST returns the progress fragment (load runs in a background
    thread). Once complete, the poll endpoint returns the final
    "Catalog loaded" fragment."""
    import pathlib  # noqa: PLC0415
    import time  # noqa: PLC0415
    golden = (
        pathlib.Path(__file__).resolve().parents[3]
        / "stage-0" / "contracts" / "xml" / "golden" / "decoders"
    )
    r = client.post("/programmer/catalog/load", data={"directory": str(golden)})
    r.raise_for_status()
    # First response is the progress fragment -- thermometer + polling markup.
    assert "thermometer" in r.text
    assert "hx-get" in r.text
    assert "/programmer/catalog/progress" in r.text
    # Poll until done (the golden dir is small -- should finish in <2s).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        r = client.get("/programmer/catalog/progress")
        r.raise_for_status()
        if "Catalog loaded" in r.text:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"catalog load did not complete: {r.text}")
    assert "decoder definition" in r.text


def test_catalog_load_nonexistent_directory_shows_error(client: TestClient) -> None:
    """User's request: the load button must surface a clear error when
    the specified path doesn't exist rather than silently failing."""
    r = client.post("/programmer/catalog/load", data={"directory": "/definitely-not-there-zzz"})
    assert r.status_code == 400
    assert "does not exist" in r.text.lower()


def test_catalog_load_file_not_directory_shows_error(client: TestClient, tmp_path) -> None:
    """Same treatment when the path exists but isn't a directory."""
    f = tmp_path / "some.txt"
    f.write_text("hello")
    r = client.post("/programmer/catalog/load", data={"directory": str(f)})
    assert r.status_code == 400
    assert "not a directory" in r.text.lower()


def test_cv_read_without_programmer_returns_503(client: TestClient) -> None:
    """The InternalConnection has no programmer; reads via the programmer
    page surface the 503 from the JSON service."""
    r = client.post("/programmer/cv/read", data={"cv": "1", "mode": "service"})
    # The route returns the fragment with the error embedded.
    assert r.status_code == 503
    assert "no service-mode programmer" in r.text


def test_programmer_nav_link_visible(client: TestClient) -> None:
    r = client.get("/")
    r.raise_for_status()
    assert '>programmer</a>' in r.text
