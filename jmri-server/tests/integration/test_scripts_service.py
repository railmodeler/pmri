"""ScriptsService tests via TestClient."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient


def test_put_then_list_shows_script(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "scripts", "method": "put",
        "data": {"name": "hello", "source": "x = 1"},
    })
    r.raise_for_status()
    assert r.json()["data"]["name"] == "hello"

    # Give the worker thread a moment.
    time.sleep(0.1)

    r = client.post("/json/", json={"type": "scripts", "method": "list"})
    r.raise_for_status()
    names = [s["name"] for s in r.json()["data"]]
    assert "hello" in names


def test_put_runs_real_script_against_internal(client: TestClient) -> None:
    """Put a script that mutates layout state; confirm via the turnout service."""
    r = client.post("/json/", json={
        "type": "scripts", "method": "put",
        "data": {"name": "make-turnout",
                 "source": "turnouts.provideTurnout('IT77').setCommandedState(CLOSED)"},
    })
    r.raise_for_status()
    # Wait for execution.
    for _ in range(20):
        time.sleep(0.05)
        r2 = client.post("/json/", json={
            "type": "scripts", "method": "get", "data": {"name": "make-turnout"},
        })
        if r2.status_code == 200 and r2.json()["data"]["finished"]:
            break

    # Turnout exists and is CLOSED (wire state 2).
    r = client.post("/json/", json={
        "type": "turnout", "method": "get", "data": {"name": "IT77"},
    })
    r.raise_for_status()
    assert r.json()["data"]["state"] == 2


def test_get_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "scripts", "method": "get", "data": {"name": "nope"},
    })
    assert r.status_code == 404


def test_put_requires_name_and_source(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "scripts", "method": "put", "data": {"name": "x"},
    })
    assert r.status_code == 400


def test_stop_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "scripts", "method": "post", "data": {"name": "nope"},
    })
    assert r.status_code == 404


def test_captured_stdout_appears_on_status(client: TestClient) -> None:
    """print() in a script populates status.stdout so the /scripts UI
    can display it -- this is the beta-milestone user-visibility fix."""
    r = client.post("/json/", json={
        "type": "scripts", "method": "put",
        "data": {"name": "printy", "source": "print('hi'); print('there')"},
    })
    r.raise_for_status()
    for _ in range(20):
        time.sleep(0.05)
        r2 = client.post("/json/", json={
            "type": "scripts", "method": "get", "data": {"name": "printy"},
        })
        if r2.status_code == 200 and r2.json()["data"]["finished"]:
            break
    body = r2.json()["data"]
    assert "hi" in body["stdout"]
    assert "there" in body["stdout"]
    assert body["stderr"] == ""


def test_put_save_persists_to_jython_dir(
    client: TestClient, tmp_path, handle,
) -> None:
    """put with save=true writes the source to <profile>/python/<name>.py
    so it autoloads on the next boot."""
    from jmri_core.persistence.profile import Profile
    handle.ctx._roster_profile = Profile(root=tmp_path, name="test")

    r = client.post("/json/", json={
        "type": "scripts", "method": "put",
        "data": {"name": "persisted", "source": "x = 1", "save": True},
    })
    r.raise_for_status()
    on_disk = tmp_path / "test" / "python" / "persisted.py"
    assert on_disk.is_file()
    assert on_disk.read_text() == "x = 1"


def test_post_delete_removes_file_and_stops(
    client: TestClient, tmp_path, handle,
) -> None:
    """action=delete stops the running script AND removes the on-disk
    file so it doesn't come back on the next autoload."""
    from jmri_core.persistence.profile import Profile
    handle.ctx._roster_profile = Profile(root=tmp_path, name="test")

    # Save + start a script.
    client.post("/json/", json={
        "type": "scripts", "method": "put",
        "data": {"name": "toremove", "source": "x = 1", "save": True},
    }).raise_for_status()
    on_disk = tmp_path / "test" / "python" / "toremove.py"
    assert on_disk.is_file()

    r = client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "toremove", "action": "delete"},
    })
    r.raise_for_status()
    assert not on_disk.exists()


def test_put_save_rejects_path_traversal(
    client: TestClient, tmp_path, handle,
) -> None:
    """save=true guards against '..' and slashes in the name so a client
    can't escape the profile's python/ dir."""
    from jmri_core.persistence.profile import Profile
    handle.ctx._roster_profile = Profile(root=tmp_path, name="test")

    r = client.post("/json/", json={
        "type": "scripts", "method": "put",
        "data": {"name": "../../etc/passwd", "source": "x = 1", "save": True},
    })
    assert r.status_code == 400


def test_list_library_shows_unrun_on_disk_scripts(
    client: TestClient, tmp_path, handle,
) -> None:
    """list with library=true merges runner status with on-disk files so
    the UI can show scripts that exist but haven't been run yet."""
    from jmri_core.persistence.profile import Profile
    handle.ctx._roster_profile = Profile(root=tmp_path, name="test")

    pdir = tmp_path / "test" / "python"
    pdir.mkdir(parents=True)
    (pdir / "on_disk_only.py").write_text("x = 1")

    r = client.post("/json/", json={
        "type": "scripts", "method": "list", "data": {"library": True},
    })
    r.raise_for_status()
    names = {s["name"]: s for s in r.json()["data"]}
    assert "on_disk_only" in names
    entry = names["on_disk_only"]
    assert entry["path"].endswith("on_disk_only.py")
    assert entry["running"] is False
    assert entry["source"] == "profile"


def test_list_library_tags_shared_scripts(
    client: TestClient, tmp_path, handle,
) -> None:
    """Scripts under ``<settings-root>/python/`` come back tagged
    ``source: "shared"`` so the UI can lock down their actions."""
    from jmri_core.persistence.profile import Profile
    handle.ctx._roster_profile = Profile(root=tmp_path, name="test")

    shared_dir = tmp_path / "python"
    shared_dir.mkdir()
    (shared_dir / "helpers.py").write_text("pass")

    r = client.post("/json/", json={
        "type": "scripts", "method": "list", "data": {"library": True},
    })
    r.raise_for_status()
    names = {s["name"]: s for s in r.json()["data"]}
    assert "helpers" in names
    assert names["helpers"]["source"] == "shared"


def test_delete_refuses_shared_script(
    client: TestClient, tmp_path, handle,
) -> None:
    """A script whose only on-disk file is under the shared dir must
    not be removable through the API -- those are operator-managed."""
    from jmri_core.persistence.profile import Profile
    handle.ctx._roster_profile = Profile(root=tmp_path, name="test")

    shared_dir = tmp_path / "python"
    shared_dir.mkdir()
    shared_file = shared_dir / "protected.py"
    shared_file.write_text("pass")

    r = client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "protected", "action": "delete"},
    })
    assert r.status_code == 409
    assert shared_file.is_file()


def test_delete_removes_profile_shadow_leaves_shared(
    client: TestClient, tmp_path, handle,
) -> None:
    """When both dirs carry a same-named file, delete removes only
    the per-profile copy; the shared original stays put."""
    from jmri_core.persistence.profile import Profile
    handle.ctx._roster_profile = Profile(root=tmp_path, name="test")

    shared_dir = tmp_path / "python"
    shared_dir.mkdir()
    shared_file = shared_dir / "shadow.py"
    shared_file.write_text("shared")
    pdir = tmp_path / "test" / "python"
    pdir.mkdir(parents=True)
    profile_file = pdir / "shadow.py"
    profile_file.write_text("profile")

    r = client.post("/json/", json={
        "type": "scripts", "method": "post",
        "data": {"name": "shadow", "action": "delete"},
    })
    r.raise_for_status()
    assert not profile_file.exists()
    assert shared_file.is_file()


def test_failing_script_records_error(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "scripts", "method": "put",
        "data": {"name": "bad", "source": "raise RuntimeError('nope')"},
    })
    r.raise_for_status()
    # Wait for execution.
    for _ in range(20):
        time.sleep(0.05)
        r2 = client.post("/json/", json={
            "type": "scripts", "method": "get", "data": {"name": "bad"},
        })
        if r2.status_code == 200 and r2.json()["data"]["finished"]:
            break
    assert r2.status_code == 200
    body = r2.json()["data"]
    assert body["finished"] is True
    assert body["error"] is not None
    assert "RuntimeError" in body["error"]
