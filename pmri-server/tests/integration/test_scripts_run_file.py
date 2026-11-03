"""scripts.post ``action=run_file`` -- re-run an on-disk script by name.

The debug loop primitive the user requested: an existing file on
disk gets a Run button that re-reads it and executes fresh. Runs
independent of whether the script was ever previously executed
(covers the "drop a .py into python/ after startup" case).
"""
from __future__ import annotations

import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from pmri_core.persistence.profile import Profile


@pytest.fixture()
def profile_with_python_dir(handle, tmp_path: pathlib.Path) -> Profile:
    """A test-scoped profile with a python/ dir the scripts service
    will read from."""
    profile = Profile(root=tmp_path, name="test")
    profile.ensure()
    # ``Profile.ensure()`` doesn't touch the python dirs -- the
    # scripts service tolerates them being absent -- so create the
    # per-profile python dir here for tests that need to write
    # files into it.
    profile.python_dir.mkdir(parents=True, exist_ok=True)
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    return profile


def _wait(check, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_run_file_reads_from_disk_and_runs(
    client: TestClient, profile_with_python_dir: Profile,
) -> None:
    """Drop a file into python/, list to confirm it's picked up,
    then post action=run_file to execute it. Output must show up
    in the status."""
    script_path = profile_with_python_dir.python_dir / "hello.py"
    script_path.write_text("print('hello from disk')", encoding="utf-8")

    r = client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "hello", "action": "run_file"},
    })
    assert r.status_code == 200

    # Poll list until the finished status carries the print output.
    def has_output() -> bool:
        rr = client.post("/json/", json={
            "type": "scripts", "method": "get",
            "data": {"name": "hello"},
        })
        if rr.status_code != 200:
            return False
        return "hello from disk" in (rr.json()["data"].get("stdout") or "")

    assert _wait(has_output), "print output never appeared in status"


def test_run_file_reads_fresh_source_on_rerun(
    client: TestClient, profile_with_python_dir: Profile,
) -> None:
    """The debug loop: edit the file externally between runs,
    click Run again, see the updated output. Any caching would
    break this -- the runner MUST re-read the file every call."""
    script_path = profile_with_python_dir.python_dir / "edit.py"
    script_path.write_text("print('version 1')", encoding="utf-8")

    client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "edit", "action": "run_file"},
    })
    assert _wait(lambda: _get_stdout(client, "edit") == "version 1\n")

    # Edit the file, run again, verify fresh output.
    script_path.write_text("print('version 2')", encoding="utf-8")
    client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "edit", "action": "run_file"},
    })
    assert _wait(lambda: _get_stdout(client, "edit") == "version 2\n")


def test_run_file_unknown_name_returns_404(
    client: TestClient, profile_with_python_dir: Profile,
) -> None:
    r = client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "does_not_exist", "action": "run_file"},
    })
    assert r.status_code == 404


def test_run_file_picks_up_files_dropped_after_startup(
    client: TestClient, profile_with_python_dir: Profile,
) -> None:
    """The autoload bug the user hit: dropping a .py file into
    python/ after the server started didn't make it runnable.
    With run_file, it does -- no restart needed."""
    # List first (mimics the user visiting /scripts before dropping
    # the file). No scripts on disk yet.
    r = client.post("/json/", json={
        "type": "scripts", "method": "list",
        "data": {"library": True},
    })
    names = {s["name"] for s in r.json()["data"]}
    assert "post_startup" not in names

    # Drop the file.
    script_path = profile_with_python_dir.python_dir / "post_startup.py"
    script_path.write_text("print('appeared')", encoding="utf-8")

    # The library list picks it up automatically because the service
    # rescans on every list call.
    r2 = client.post("/json/", json={
        "type": "scripts", "method": "list",
        "data": {"library": True},
    })
    names = {s["name"] for s in r2.json()["data"]}
    assert "post_startup" in names

    # And run_file can execute it.
    r3 = client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "post_startup", "action": "run_file"},
    })
    assert r3.status_code == 200


def _get_stdout(client: TestClient, name: str) -> str:
    rr = client.post("/json/", json={
        "type": "scripts", "method": "get", "data": {"name": name},
    })
    if rr.status_code != 200:
        return ""
    return rr.json()["data"].get("stdout") or ""
