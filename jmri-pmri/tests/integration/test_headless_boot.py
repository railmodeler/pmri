"""End-to-end --headless smoke test: boot a trivial ASGI app via
``ServiceController``, probe the port, stop cleanly.

Does NOT use ``jmri_server:create_app`` -- that would pull the full
server dependency tree and take seconds to start. A stub ASGI
callable is enough to verify the controller's lifecycle.
"""
from __future__ import annotations

import socket
import time

import httpx
from jmri_core.persistence import PmriConfig

from jmri_pmri.service import ServiceController, ServiceState


async def _stub_app(scope, receive, send) -> None:
    """Minimal ASGI 3 callable -- responds 200/OK to any request so
    the ready probe + an HTTP smoke call both succeed."""
    if scope["type"] != "http":
        return
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"OK"})


def _free_port() -> int:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    return port


def test_start_wait_ready_stop_cycle() -> None:
    """Positive integration: start the controller, confirm it
    accepts, hit the port with httpx, stop cleanly. Takes ~1 s."""
    cfg = PmriConfig()
    cfg.http.host = "127.0.0.1"
    cfg.http.port = _free_port()
    cfg.log.level = "WARNING"  # don't flood test output
    ctrl = ServiceController(
        config=cfg,
        app_import="tests.integration.test_headless_boot:_stub_app",
        factory=False,
    )
    ctrl.start()
    try:
        assert ctrl.wait_until_ready(timeout=10.0), (
            f"service didn't become ready: {ctrl.last_error}"
        )
        assert ctrl.state is ServiceState.STARTED
        # Smoke-call the running service.
        response = httpx.get(ctrl.url, timeout=5.0)
        assert response.status_code == 200
        assert response.text == "OK"
    finally:
        ctrl.stop(join_timeout=5.0)
    assert ctrl.state is ServiceState.STOPPED


def test_double_start_is_idempotent() -> None:
    """Calling start() twice while already running must not spawn a
    second uvicorn thread or raise. Matches the "window owns the
    service, user may click Start twice" GUI pattern."""
    cfg = PmriConfig()
    cfg.http.host = "127.0.0.1"
    cfg.http.port = _free_port()
    cfg.log.level = "WARNING"
    ctrl = ServiceController(
        config=cfg,
        app_import="tests.integration.test_headless_boot:_stub_app",
        factory=False,
    )
    ctrl.start()
    try:
        ctrl.wait_until_ready(timeout=10.0)
        first_thread = ctrl._slot.thread
        ctrl.start()  # should be a no-op
        assert ctrl._slot.thread is first_thread
    finally:
        ctrl.stop(join_timeout=5.0)


def test_startup_error_surfaces_last_error() -> None:
    """A failing uvicorn startup (invalid app import) must leave the
    controller STOPPED with ``last_error`` populated so the GUI can
    display it. We use a bogus app path rather than a port collision
    because Windows's TCP stack is loose about shared-port binds
    without ``SO_EXCLUSIVEADDRUSE`` -- the port-in-use scenario is
    hard to reproduce portably."""
    cfg = PmriConfig()
    cfg.http.host = "127.0.0.1"
    cfg.http.port = _free_port()
    cfg.log.level = "CRITICAL"
    ctrl = ServiceController(
        config=cfg,
        app_import="jmri_pmri.does_not_exist:nonexistent",
        factory=False,
    )
    ctrl.start()
    # wait_until_ready returns False once the thread flips to STOPPED
    # or times out. Both outcomes populate last_error.
    assert ctrl.wait_until_ready(timeout=5.0) is False
    for _ in range(50):
        if ctrl.state is ServiceState.STOPPED:
            break
        time.sleep(0.05)
    assert ctrl.state is ServiceState.STOPPED
    assert ctrl.last_error is not None
