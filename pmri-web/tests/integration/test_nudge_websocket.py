"""Integration tests for the ``/ws/nudge`` push mechanism."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from pmri_web.app import build_web_app


def test_nudge_websocket_accepts_connection() -> None:
    """A client can connect to ``/ws/nudge`` and the server stays open."""
    handle = build_web_app()
    with TestClient(handle.app) as tc:
        with tc.websocket_connect("/ws/nudge") as ws:
            # The nudge socket doesn't greet, so we don't try to
            # receive -- just confirm the connect handshake succeeded
            # by closing cleanly.
            ws.close()


def test_scripts_mutation_fires_nudge_to_ws_client() -> None:
    """Posting to a scripts mutation route broadcasts a ``scripts``
    topic frame on every attached nudge client. Verifies the
    web-route -> NudgeHub -> WebSocket -> JSON frame pipeline end-to-end.
    """
    handle = build_web_app()
    with TestClient(handle.app) as tc:
        with tc.websocket_connect("/ws/nudge") as ws:
            # Fire a run via the REPL route (no file I/O, no sleep)
            # -- any mutation route that triggers ``_nudge("scripts")``
            # would do; the scripts/run route is the one touched by
            # the migration.
            tc.post("/scripts/run", data={
                "name": "nudgeprobe",
                "source": "print('hi')",
            })
            # The nudge is broadcast asynchronously; receive_text has
            # the TestClient event loop drain pending tasks before
            # returning so we get the frame on this call.
            frame = json.loads(ws.receive_text())
    assert frame["topic"] == "scripts"


def test_scripts_table_hx_trigger_uses_nudge_not_polling() -> None:
    """Regression guard: the scripts table triggers on the ``pmri:scripts``
    custom event, not a fixed-interval polling loop."""
    handle = build_web_app()
    with TestClient(handle.app) as tc:
        r = tc.get("/scripts/refresh")
        r.raise_for_status()
        body = r.text
    assert "pmri:scripts from:body" in body
    assert "every 2s" not in body


async def test_scripts_tail_task_nudges_on_output_change() -> None:
    """Server-side tail task nudges the ``scripts`` topic when running
    script output changes. Drives :mod:`pmri_web.scripts_tail` with a
    stubbed registry so the test is hermetic + deterministic."""
    import asyncio

    from pmri_web.nudge import NudgeHub
    from pmri_web.scripts_tail import run_scripts_tail

    class _StubService:
        def __init__(self) -> None:
            self.state: list[dict] = []

        async def on_list(self, _data):
            return list(self.state)

    class _StubRegistry:
        def __init__(self) -> None:
            self.svc = _StubService()

        def get(self, topic: str):
            return self.svc if topic == "scripts" else None

    class _StubServer:
        def __init__(self) -> None:
            self.registry = _StubRegistry()

    received: list[tuple[str, dict]] = []

    class _RecordingHub(NudgeHub):
        async def broadcast(self, topic, data=None):  # type: ignore[override]
            received.append((topic, data or {}))

    server = _StubServer()
    hub = _RecordingHub()
    # Seed with a running script BEFORE launching the task so the first
    # tick sees the ACTIVE-interval cadence (1 s), not the IDLE one (5 s).
    server.registry.svc.state = [
        {"name": "A", "running": True, "stdout": "hello", "stderr": ""},
    ]
    task = asyncio.create_task(run_scripts_tail(server, hub))  # type: ignore[arg-type]
    try:
        # One active-interval tick after launch -> first nudge arrives.
        await asyncio.sleep(1.2)
        before = len(received)
        # Mutate output; next tick should nudge again on hash change.
        server.registry.svc.state[0]["stdout"] = "hello world"
        await asyncio.sleep(1.2)
        after = len(received)
        assert after > before, "tail should have nudged on output change"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
