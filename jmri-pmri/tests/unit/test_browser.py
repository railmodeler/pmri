"""``open_browser_when_ready`` -- probe + launch sequencing.

Covers:

* The function waits for the controller to be ready before calling
  webbrowser.open_new_tab -- probing early would get the user a "connection
  refused" page.
* A timeout on the probe skips the open and returns False.
* webbrowser.open_new_tab raising / returning False both surface as False.
"""
from __future__ import annotations

from unittest.mock import patch

from jmri_core.persistence import PmriConfig

from jmri_pmri.browser import open_browser_when_ready
from jmri_pmri.service import ServiceController, ServiceState


class _FakeController:
    """Just enough shape of ServiceController for the browser tests."""

    def __init__(self, *, ready: bool, url: str = "http://127.0.0.1:12080/") -> None:
        self._ready = ready
        self._url = url
        self.last_error: str | None = None

    def wait_until_ready(self, *, timeout: float) -> bool:
        return self._ready

    @property
    def url(self) -> str:
        return self._url


def test_opens_browser_once_service_is_ready() -> None:
    """Happy path: controller flags ready, webbrowser.open_new_tab is called
    with the controller's URL, True returned."""
    ctrl = _FakeController(ready=True, url="http://127.0.0.1:12080/")
    with patch("jmri_pmri.browser.webbrowser.open_new_tab", return_value=True) as mock_open:
        result = open_browser_when_ready(ctrl)  # type: ignore[arg-type]
    assert result is True
    mock_open.assert_called_once_with("http://127.0.0.1:12080/")


def test_timeout_skips_the_browser_call() -> None:
    """If the service never comes up, the browser must NOT be
    launched -- otherwise the user stares at a connection-refused
    page with no idea why."""
    ctrl = _FakeController(ready=False)
    with patch("jmri_pmri.browser.webbrowser.open_new_tab") as mock_open:
        result = open_browser_when_ready(ctrl)  # type: ignore[arg-type]
    assert result is False
    mock_open.assert_not_called()


def test_webbrowser_open_returning_false_surfaces_as_false() -> None:
    """webbrowser.open_new_tab returns False when no browser handler is
    registered (headless Linux without a desktop, say). The
    launcher logs and the caller treats this as a soft failure --
    the service is still up."""
    ctrl = _FakeController(ready=True)
    with patch("jmri_pmri.browser.webbrowser.open_new_tab", return_value=False):
        assert open_browser_when_ready(ctrl) is False  # type: ignore[arg-type]


def test_webbrowser_error_surfaces_as_false() -> None:
    """``webbrowser.Error`` indicates a hostile environment (no
    handler + no fallback). Logged, not raised -- the service stays
    up and the operator can click Open UI manually later."""
    import webbrowser as wb  # noqa: PLC0415
    ctrl = _FakeController(ready=True)
    with patch("jmri_pmri.browser.webbrowser.open_new_tab", side_effect=wb.Error("boom")):
        assert open_browser_when_ready(ctrl) is False  # type: ignore[arg-type]


# ---- real ServiceController integration (type matches) ------------------


def test_passes_a_real_service_controller_through_the_type_system() -> None:
    """Belt-and-braces: the signature accepts a real
    ``ServiceController`` instance (not just our fake). This catches
    an accidental signature narrowing on the controller type."""
    cfg = PmriConfig()
    ctrl = ServiceController(config=cfg)
    ctrl.state = ServiceState.STOPPED
    # Not ready, so we expect False and no browser call.
    with patch("jmri_pmri.browser.webbrowser.open_new_tab") as mock_open:
        result = open_browser_when_ready(ctrl, timeout=0.05)
    assert result is False
    mock_open.assert_not_called()
