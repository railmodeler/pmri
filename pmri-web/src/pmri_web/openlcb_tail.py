"""Server-side OpenLCB "state changed" watcher.

Mirrors :mod:`pmri_web.scripts_tail`: one background task per server
hashes the openlcb-state surface each tick and nudges connected
clients only when the hash moves. Replaces the former pile of
polling triggers (10 s health, 5 s node list, 2 s monitor, 3 s
per-node panes) with one topic (``openlcb``) that every openlcb
page refetches on.

Hash sources (kept intentionally shallow so the tick is cheap):

* ``openlcb.list`` -- the peer roster (count + ordered aliases).
* ``openlcb.post action=recent_frames limit=1`` -- newest frame, so
  the monitor + per-node panes refresh when traffic lands.

A no-openlcb-connection install sees the list call 400; the task
backs off to the IDLE interval and keeps running (a connection
coming up later gets picked up).
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


async def run_openlcb_tail(server: "AppHandle", nudge: "NudgeHub") -> None:
    """Loop until cancelled; nudge ``openlcb`` on state change."""
    last_hash: str | None = None
    while True:
        try:
            interval = _IDLE_INTERVAL_S
            svc = server.registry.get("openlcb")
            if svc is None:
                await asyncio.sleep(_IDLE_INTERVAL_S)
                continue
            h = hashlib.blake2b(digest_size=16)
            active = True
            try:
                nodes = await svc.on_list(None)
            except Exception:  # noqa: BLE001  -- connection not OpenLCB
                nodes = []
                active = False
            for n in nodes or []:
                if not isinstance(n, dict):
                    continue
                h.update((n.get("alias") or "").encode("utf-8"))
                h.update(b"|")
                h.update(str(n.get("nid") or "").encode("utf-8"))
                h.update(b"\x00")
            if active:
                interval = _ACTIVE_INTERVAL_S
                # Sample the newest frame -- cheap on both the service
                # (one deque peek) and the hash (bounded blob).
                try:
                    frames = await svc.on_post({
                        "action": "recent_frames", "limit": 1,
                    })
                except Exception:   # noqa: BLE001
                    frames = []
                for f in (frames or []):
                    if not isinstance(f, dict):
                        continue
                    for k in ("ts", "frame", "canhdr", "src", "mti"):
                        v = f.get(k)
                        if v is not None:
                            h.update(str(v).encode("utf-8"))
                            h.update(b"|")
            digest = h.hexdigest()
            if digest != last_hash:
                last_hash = digest
                await nudge.broadcast("openlcb")
        except asyncio.CancelledError:
            raise
        except Exception:   # noqa: BLE001
            _log.exception("openlcb-tail tick failed; continuing")
            interval = _IDLE_INTERVAL_S
        await asyncio.sleep(interval)
