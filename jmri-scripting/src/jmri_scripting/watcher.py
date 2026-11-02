"""``ScriptWatcher`` -- poll-based hot-reload for .py files.

Watches a directory and restarts any script whose source file mtime
changes. Polls every ``interval`` seconds (default 1s) -- coarse but
zero-dependency. For production setups that need sub-second response,
swap in ``watchdog`` or ``inotify`` later.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jmri_scripting.runner import ScriptRunner

_log = logging.getLogger(__name__)


class ScriptWatcher:
    """Re-runs scripts in a directory when their .py files change."""

    def __init__(
        self,
        runner: ScriptRunner,
        directory: str | pathlib.Path,
        *,
        interval: float = 1.0,
    ) -> None:
        self._runner = runner
        self._dir = pathlib.Path(directory)
        self._interval = interval
        self._mtimes: dict[pathlib.Path, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        # Seed the initial mtimes.
        for p in self._dir.glob("*.py"):
            self._mtimes[p] = p.stat().st_mtime
        self._task = asyncio.create_task(self._poll(), name="script-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            import contextlib  # noqa: PLC0415
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _poll(self) -> None:
        import contextlib  # noqa: PLC0415
        try:
            while not self._stop.is_set():
                self._tick()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        except asyncio.CancelledError:
            return

    def _tick(self) -> None:
        if not self._dir.is_dir():
            return
        current: dict[pathlib.Path, float] = {}
        for p in self._dir.glob("*.py"):
            try:
                current[p] = p.stat().st_mtime
            except FileNotFoundError:
                continue

        # New + changed files: restart.
        for p, mtime in current.items():
            prev = self._mtimes.get(p)
            if prev is None or mtime > prev + 1e-6:
                _log.info("hot-reload: %s", p.name)
                try:
                    self._runner.run_file(p, name=p.stem)
                except Exception as e:  # noqa: BLE001
                    _log.warning("failed to re-run %s: %s", p, e)

        # Deleted files: stop the running script.
        for p in list(self._mtimes):
            if p not in current:
                _log.info("hot-reload: %s removed, stopping", p.name)
                self._runner.stop(p.stem)

        self._mtimes = current
