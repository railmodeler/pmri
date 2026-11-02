"""``ServiceController`` -- state transitions + the TCP ready probe.

The uvicorn integration itself is covered by
``tests/integration/test_headless_boot.py`` with a real ASGI app on
``127.0.0.1:0``. Here we focus on the state machine and the
dialable-host coercion so a ``0.0.0.0`` bind doesn't make the probe
dial the wildcard address."""
from __future__ import annotations

import socket
import threading
import time

from jmri_core.persistence import PmriConfig

from jmri_pmri.service import (
    ServiceController,
    ServiceState,
    _probe_host,
    _tcp_probe,
)

# ---- host coercion for the probe ----------------------------------------


def test_bind_0_0_0_0_coerces_to_loopback_for_probe() -> None:
    """``0.0.0.0`` means "accept on every interface"; it's not a
    destination address. The probe + browser URL must dial
    ``127.0.0.1`` instead so a listen-everywhere bind is reachable."""
    assert _probe_host("0.0.0.0") == "127.0.0.1"


def test_bind_ipv6_wildcard_coerces_to_loopback() -> None:
    """Same treatment for IPv6 ``::`` (unspecified / listen-all)."""
    assert _probe_host("::") == "127.0.0.1"


def test_empty_host_coerces_to_loopback() -> None:
    """Edge case: an empty host attribute in pmri.xml should
    not break the probe."""
    assert _probe_host("") == "127.0.0.1"


def test_specific_host_passes_through() -> None:
    """A deliberate bind (``127.0.0.1`` or a LAN address) is kept
    as-is; coercion must not surprise the operator."""
    assert _probe_host("127.0.0.1") == "127.0.0.1"
    assert _probe_host("10.0.0.5") == "10.0.0.5"


# ---- URL ----------------------------------------------------------------


def test_url_uses_probe_host() -> None:
    """The launcher's Open-UI button hits ``controller.url``. If
    the bind is wildcard, the URL must be dialable
    (``http://127.0.0.1:PORT/``), not a confusing
    ``http://0.0.0.0:PORT/``."""
    cfg = PmriConfig()
    cfg.http.host = "0.0.0.0"
    cfg.http.port = 54321
    ctrl = ServiceController(config=cfg)
    assert ctrl.url == "http://127.0.0.1:54321/"


# ---- initial state ------------------------------------------------------


def test_initial_state_is_stopped() -> None:
    """A fresh controller is STOPPED until start() is called. The
    GUI reads this to enable the Start button on first paint."""
    ctrl = ServiceController(config=PmriConfig())
    assert ctrl.state is ServiceState.STOPPED
    assert ctrl.last_error is None


def test_stop_on_stopped_controller_is_noop() -> None:
    """A redundant stop() must not raise or deadlock -- the GUI's
    close handler calls it defensively on an already-stopped service
    at exit."""
    ctrl = ServiceController(config=PmriConfig())
    ctrl.stop()
    assert ctrl.state is ServiceState.STOPPED


# ---- TCP probe ----------------------------------------------------------


def test_tcp_probe_against_listening_socket_returns_true() -> None:
    """Positive case: open a listening socket, confirm the probe
    sees it. Uses port 0 so the OS picks a free one."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _tcp_probe("127.0.0.1", port) is True
    finally:
        srv.close()


def test_tcp_probe_against_closed_port_returns_false() -> None:
    """A random high port with no listener should refuse promptly.
    We don't assert "immediate" because kernel RST timing varies,
    but it must come back False within the probe's 100 ms budget."""
    # Grab a port, close it, probe it.
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    start = time.monotonic()
    assert _tcp_probe("127.0.0.1", port) is False
    assert time.monotonic() - start < 0.5


# ---- wait_until_ready ---------------------------------------------------
#
# Driven against a hand-built listening socket on a port we pick --
# simulates uvicorn's eventual bind without the full server lifecycle.


def test_wait_until_ready_succeeds_once_socket_appears() -> None:
    """A listener appearing partway through the wait must flip the
    probe success. Simulates uvicorn's bind-after-startup race.

    ``wait_until_ready`` requires BOTH ``server.started`` to flip
    AND the TCP probe to connect (the AND catches a 'bound but
    specific interface unreachable' edge case; see
    :meth:`ServiceController._wait_for_host`). The test fakes a
    minimal server object exposing ``started`` so the AND can
    succeed without running real uvicorn."""
    cfg = PmriConfig()
    cfg.http.port = _free_port()
    cfg.http.host = "127.0.0.1"
    ctrl = ServiceController(config=cfg)
    ctrl.state = ServiceState.STARTING

    class _FakeStartedServer:
        started = True

    ctrl._slot.server = _FakeStartedServer()  # type: ignore[assignment]

    listener_opened = threading.Event()
    srv = socket.socket()

    def _late_listen() -> None:
        time.sleep(0.1)
        srv.bind(("127.0.0.1", cfg.http.port))
        srv.listen(1)
        listener_opened.set()

    t = threading.Thread(target=_late_listen, daemon=True)
    t.start()
    try:
        assert ctrl.wait_until_ready(timeout=2.0) is True
        assert ctrl.state is ServiceState.STARTED
        # ``_probed_host`` is populated so ``url`` reflects which
        # host actually accepted.
        assert ctrl._probed_host == "127.0.0.1"
    finally:
        listener_opened.wait(timeout=1.0)
        srv.close()


def test_wait_until_ready_falls_back_to_lan_ip_on_bind_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind is 0.0.0.0 (listen everywhere). The primary probe hits
    127.0.0.1 but that times out (simulated broken loopback); the
    fallback probes a monkey-patched LAN IP that DOES have a
    listener. The caller sees True + ``url`` reflects the LAN IP."""
    import jmri_pmri.service as svc  # noqa: PLC0415
    cfg = PmriConfig()
    cfg.http.port = _free_port()
    cfg.http.host = "0.0.0.0"
    ctrl = ServiceController(config=cfg)
    ctrl.state = ServiceState.STARTING

    class _FakeStartedServer:
        started = True

    ctrl._slot.server = _FakeStartedServer()  # type: ignore[assignment]

    # Open a listener on the loopback -- but monkey-patch _tcp_probe
    # so 127.0.0.1 always comes back False (simulating a broken
    # loopback) while 127.0.0.2 (our fake "LAN" IP) is reachable.
    srv = socket.socket()
    srv.bind(("127.0.0.1", cfg.http.port))  # bind here anyway
    srv.listen(1)

    real_tcp_probe = svc._tcp_probe

    def _fake_probe(host: str, port: int, *, timeout: float = 0.1) -> bool:
        if host == "127.0.0.1":
            return False
        return real_tcp_probe("127.0.0.1", port, timeout=timeout)

    monkeypatch.setattr(svc, "_tcp_probe", _fake_probe)
    monkeypatch.setattr(svc, "_lan_ip", lambda: "127.0.0.2")
    try:
        assert ctrl.wait_until_ready(timeout=1.0) is True
        assert ctrl._probed_host == "127.0.0.2"
        assert ctrl.url == f"http://127.0.0.2:{cfg.http.port}/"
    finally:
        srv.close()


def test_wait_until_ready_times_out_cleanly() -> None:
    """No listener appears within the budget -> False, state stays
    STARTING. The GUI shows the error label; the service thread may
    still be alive doing bind-retries."""
    cfg = PmriConfig()
    cfg.http.port = _free_port()
    cfg.http.host = "127.0.0.1"
    ctrl = ServiceController(config=cfg)
    ctrl.state = ServiceState.STARTING
    assert ctrl.wait_until_ready(timeout=0.2) is False


def test_wait_until_ready_returns_false_when_thread_exited() -> None:
    """If the uvicorn thread crashed (bind failure, say), state
    transitions to STOPPED before our timeout and the probe should
    return False promptly rather than wasting the full budget."""
    cfg = PmriConfig()
    ctrl = ServiceController(config=cfg)
    ctrl.state = ServiceState.STOPPED  # simulate the thread's finally block
    start = time.monotonic()
    assert ctrl.wait_until_ready(timeout=5.0) is False
    assert time.monotonic() - start < 0.1


# ---- helpers ------------------------------------------------------------


def _free_port() -> int:
    """Grab a free localhost port and release it. Race-prone with
    concurrent tests, but we don't test parallel-port acquisition --
    worst case a test flakes and reruns clean."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    return port
