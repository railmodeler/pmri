"""ScriptWatcher tests."""
from __future__ import annotations

import asyncio
import pathlib

from jmri_scripting import ScriptRunner, ScriptWatcher


async def _wait_for(predicate, timeout: float = 2.0) -> bool:  # noqa: ANN001
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_watcher_picks_up_existing_files(
    runner: ScriptRunner, tmp_path: pathlib.Path
) -> None:
    (tmp_path / "a.py").write_text(
        "sensors.provideSensor('IS_A').setKnownState(ACTIVE)"
    )
    watcher = ScriptWatcher(runner, tmp_path, interval=0.05)
    await watcher.start()
    # Wait for the initial mtime snapshot to settle, then bump the file
    # to trigger a "change" detected on the next tick.
    await asyncio.sleep(0.1)
    (tmp_path / "a.py").touch()
    assert await _wait_for(lambda: runner.get("a") is not None and runner.get("a").finished)
    await watcher.stop()


async def test_watcher_detects_new_files(
    runner: ScriptRunner, tmp_path: pathlib.Path
) -> None:
    watcher = ScriptWatcher(runner, tmp_path, interval=0.05)
    await watcher.start()
    await asyncio.sleep(0.1)
    (tmp_path / "newone.py").write_text("pass")
    assert await _wait_for(lambda: runner.get("newone") is not None)
    await watcher.stop()


async def test_watcher_stop_idempotent(
    runner: ScriptRunner, tmp_path: pathlib.Path
) -> None:
    watcher = ScriptWatcher(runner, tmp_path, interval=0.05)
    await watcher.start()
    await watcher.stop()
    await watcher.stop()   # second call must not raise


async def test_watcher_handles_missing_directory(runner: ScriptRunner) -> None:
    watcher = ScriptWatcher(runner, "/nonexistent/whatever", interval=0.05)
    await watcher.start()
    await asyncio.sleep(0.1)
    await watcher.stop()
