"""Web /scripts page tests."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient


def test_scripts_page_renders(client: TestClient) -> None:
    r = client.get("/scripts")
    r.raise_for_status()
    assert "Run an inline script" in r.text
    assert "No scripts running yet" in r.text


def test_scripts_run_via_form(client: TestClient) -> None:
    r = client.post(
        "/scripts/run",
        data={"name": "form-test", "source": "x = 1"},
    )
    r.raise_for_status()
    assert "form-test" in r.text


def test_scripts_stop_via_form(client: TestClient) -> None:
    client.post("/scripts/run", data={"name": "stop-test", "source": "x = 1"})
    # Wait for the script to finish.
    for _ in range(20):
        time.sleep(0.05)
        check = client.post("/json/", json={
            "type": "scripts", "method": "get", "data": {"name": "stop-test"},
        })
        if check.status_code == 200 and check.json()["data"]["finished"]:
            break
    # Now the stop endpoint may fail because the script is gone -- but a
    # 404 should not crash the page; the underlying _dispatch helper
    # surfaces it as HTTP 404.
    r = client.post("/scripts/stop/stop-test")
    # Either 200 (still in table) or 404 -- both are acceptable.
    assert r.status_code in (200, 404)


def test_scripts_nav_link_visible(client: TestClient) -> None:
    r = client.get("/")
    r.raise_for_status()
    assert ">scripts</a>" in r.text
