"""Server-side "scripts output tail" watcher.

Replaces the client-side ``hx-trigger="every 2s"`` polling that fed
the scripts page's live-output area. Runs ONE task per server (not
per connected client): every second while any script is marked
``running``, hash the aggregate stdout+stderr snapshot; when the
hash changes, nudge all connected clients so their tables refetch.

Why diff-and-nudge instead of per-write callbacks: a running
automaton can emit ``print()`` hundreds of times per second. Firing
a nudge on every write would flood the WebSocket and tail the
browser with constant re-fetches. Diff-and-nudge at 1 Hz bounds
nudge traffic at one frame per second per server, which is cheaper
AND steadier than the former client-side 2s polling (which fetched
even when nothing changed).

When nothing is running, the tick interval stretches to 5 s so the
idle cost is negligible.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmri_server.app import AppHandle

    from pmri_web.nudge import NudgeHub


_log = logging.getLogger(__name__)

_ACTIVE_INTERVAL_S = 1.0
_IDLE_INTERVAL_S = 5.0


async def run_scripts_tail(server: "AppHandle", nudge: "NudgeHub") -> None:
    """Loop until cancelled; nudge ``scripts`` on output change.

    The loop is intentionally forgiving: a one-off failure (service
    not yet registered, transient exception during listing) just
    skips the tick. The loop never raises -- cancellation is the
    only normal exit.
    """
    last_hash: str | None = None
    while True:
        try:
            svc = server.registry.get("scripts")
            if svc is None:
                await asyncio.sleep(_IDLE_INTERVAL_S)
                continue
            statuses = await svc.on_list({"library": True})
            any_running = any(
                s.get("running") for s in statuses if isinstance(s, dict)
            )
            interval = _ACTIVE_INTERVAL_S if any_running else _IDLE_INTERVAL_S
            # Snapshot hash: stdout + stderr for every running row --
            # finished/errored rows don't need tailing, their final
            # output already arrived via the mutation route's nudge.
            h = hashlib.blake2b(digest_size=16)
            for s in statuses:
                if not isinstance(s, dict) or not s.get("running"):
                    continue
                h.update((s.get("name") or "").encode("utf-8"))
                h.update(b"\x00")
                h.update((s.get("stdout") or "").encode("utf-8"))
                h.update(b"\x00")
                h.update((s.get("stderr") or "").encode("utf-8"))
                h.update(b"\x01")
            digest = h.hexdigest()
            if digest != last_hash:
                last_hash = digest
                await nudge.broadcast("scripts")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.exception("scripts-tail tick failed; continuing")
            interval = _IDLE_INTERVAL_S
        await asyncio.sleep(interval)
