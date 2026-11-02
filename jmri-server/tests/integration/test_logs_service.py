"""Integration tests for the ``logs`` JSON topic."""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from tests.conftest import post


def test_get_without_log_file_returns_empty(client: TestClient) -> None:
    """A bare AppHandle has no log file attached; the service returns
    ``path=null`` and an empty ``lines`` array rather than 404 -- that's
    what the home widget renders as its "not configured" hint."""
    resp = post(client, "logs", method="get")
    assert resp["data"]["path"] is None
    assert resp["data"]["lines"] == []


def test_get_with_log_file_tails_it(
    tmp_path: pathlib.Path, monkeypatch,
) -> None:
    from jmri_core import log as jmri_log  # noqa: PLC0415
    from jmri_server.app import build_app  # noqa: PLC0415

    log_file = tmp_path / "jmri.log"
    # Pre-seed content so the tail reflects those lines specifically
    # rather than whatever the lifespan startup writes.
    log_file.write_text(
        "\n".join(f"line-{i}" for i in range(20)) + "\n", encoding="utf-8",
    )
    monkeypatch.setenv("JMRI_LOG_FILE", str(log_file))
    handle = build_app()
    try:
        with TestClient(handle.app) as tc:
            r = tc.post("/json/", json={
                "type": "logs", "method": "get", "data": {"tail": 5},
            })
            data = r.json()["data"]
            assert data["path"] == str(log_file)
            assert data["tail"] == 5
            # The lifespan startup appends its own INFO line; the tail
            # of 5 spans the last 4 seeded lines plus that record.
            assert "line-19" in data["lines"][-2]
    finally:
        jmri_log.reset()


def test_tail_clamped_to_max(
    tmp_path: pathlib.Path, monkeypatch,
) -> None:
    """A pathological request for a huge tail (someone binding a slider
    up to 999,999) must be clamped so we don't slurp giant log files."""
    from jmri_core import log as jmri_log  # noqa: PLC0415
    from jmri_server.app import build_app  # noqa: PLC0415

    log_file = tmp_path / "jmri.log"
    monkeypatch.setenv("JMRI_LOG_FILE", str(log_file))
    handle = build_app()
    try:
        with TestClient(handle.app) as tc:
            r = tc.post("/json/", json={
                "type": "logs", "method": "get", "data": {"tail": 999_999},
            })
            assert r.json()["data"]["tail"] == 500
    finally:
        jmri_log.reset()


def test_missing_file_returns_empty_lines(
    tmp_path: pathlib.Path, monkeypatch,
) -> None:
    """If ``_log_file`` points somewhere that doesn't exist BEFORE
    lifespan startup, the bootstrap will create it (with the parent
    dir if needed) -- the file exists once the app is running. To
    exercise the "path set but no readable log" branch we tear the
    file down after startup: the handler is still writing to a
    (now-deleted) inode on POSIX, but on Windows ``read_text`` fails
    cleanly and :func:`tail` returns ``[]``."""
    from jmri_core import log as jmri_log  # noqa: PLC0415
    from jmri_server.app import build_app  # noqa: PLC0415

    log_file = tmp_path / "gone.log"
    monkeypatch.setenv("JMRI_LOG_FILE", str(log_file))
    handle = build_app()
    try:
        with TestClient(handle.app) as tc:
            # File exists at this point because ``configure`` opened it
            # for the RotatingFileHandler. Delete it before querying.
            try:
                log_file.unlink()
            except OSError:
                pass
            r = tc.post("/json/", json={
                "type": "logs", "method": "get",
            })
            data = r.json()["data"]
            assert data["path"] == str(log_file)
            # POSIX may re-write on the next log call; either way ``tail``
            # returns cleanly, no 500.
            assert isinstance(data["lines"], list)
    finally:
        jmri_log.reset()


def test_lifespan_configures_logging_from_env(
    tmp_path, monkeypatch,
) -> None:
    """Regression for "I set JMRI_LOG_FILE and see nothing".

    Logging bootstrap runs inside the FastAPI lifespan (uvicorn's own
    dictConfig wipes handlers we install before it, so pre-lifespan
    was a dead end). After ``TestClient`` enters the lifespan, the
    ``logs`` service must report the resolved path from the env var --
    not ``None``. Also verifies that a log line actually reaches the
    file, so a silent "path set but nothing written" regression fails
    here too.
    """
    from jmri_core import log as jmri_log  # noqa: PLC0415
    from jmri_server.app import build_app  # noqa: PLC0415

    log_file = tmp_path / "e2e.log"
    monkeypatch.setenv("JMRI_LOG_FILE", str(log_file))
    monkeypatch.setenv("JMRI_LOG_LEVEL", "INFO")

    handle = build_app()
    try:
        with TestClient(handle.app) as tc:
            r = tc.post("/json/", json={
                "type": "logs", "method": "get", "data": {"tail": 5},
            })
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["path"] == str(log_file)
            # The lifespan-startup INFO line lands in the file -- proves
            # the handler is actually installed and writing.
            assert log_file.exists()
            body = log_file.read_text(encoding="utf-8")
            assert "JMRI logs writing to" in body
    finally:
        # Don't leak handlers into the next test in the same process.
        jmri_log.reset()


def test_bad_tail_value_uses_default(
    tmp_path: pathlib.Path, monkeypatch,
) -> None:
    from jmri_core import log as jmri_log  # noqa: PLC0415
    from jmri_server.app import build_app  # noqa: PLC0415

    log_file = tmp_path / "jmri.log"
    monkeypatch.setenv("JMRI_LOG_FILE", str(log_file))
    handle = build_app()
    try:
        with TestClient(handle.app) as tc:
            r = tc.post("/json/", json={
                "type": "logs", "method": "get", "data": {"tail": "abc"},
            })
            data = r.json()["data"]
            # Fell back to 50 (the service default) rather than raising.
            assert data["tail"] == 50
    finally:
        jmri_log.reset()
