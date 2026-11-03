"""Open the user's default browser once the uvicorn service is ready.

Python's stdlib :mod:`webbrowser` already has the "detect running /
launch if not" behaviour we want on all three platforms: Windows uses
``start``, macOS uses ``open``, Linux uses ``xdg-open``. All three
reuse an already-running browser (new tab) and launch one if not.

The one piece worth getting right is timing: uvicorn's bind is async
and the service doesn't accept connections the instant we schedule
the server. Firing ``webbrowser.open`` too early gets the user a
confusing "connection refused" page. This module couples the probe
and the open: wait for TCP accept, then open.
"""
from __future__ import annotations

import logging
import webbrowser

from pmri_pmri.service import ServiceController

_log = logging.getLogger(__name__)


def open_browser_when_ready(
    controller: ServiceController,
    *,
    timeout: float = 5.0,
) -> bool:
    """Wait for *controller* to accept, then open its URL in the
    default browser.

    Returns True on success. False means the ready-probe timed out
    (service didn't come up within *timeout*) OR the browser call
    returned False (no handler registered -- rare on desktop OSes,
    common on headless systems). The caller surfaces the failure in
    the GUI with the specific reason from ``controller.last_error``
    if the service didn't come up.
    """
    if not controller.wait_until_ready(timeout=timeout):
        _log.warning(
            "uvicorn didn't become ready in %.1fs; skipping browser open",
            timeout,
        )
        return False
    url = controller.url
    _log.info("opening browser tab: %s", url)
    try:
        # Use ``open_new_tab`` rather than ``open`` so an
        # already-running browser gets a new TAB rather than just
        # being pinged (which some controllers interpret as
        # "focus"). On every platform the launcher cares about,
        # new-tab semantics also degrade gracefully to "new window"
        # when no browser is running yet, so this call covers both
        # the already-running and cold-start cases.
        opened = webbrowser.open_new_tab(url)
    except webbrowser.Error as e:
        _log.warning("webbrowser.open_new_tab(%s) raised: %s", url, e)
        return False
    if not opened:
        _log.warning(
            "webbrowser.open_new_tab(%s) returned False -- no browser "
            "handler registered on this system, or the handler can't "
            "message a running instance. Operator can hit \"Open UI\" "
            "in the GUI to launch manually.", url,
        )
    return opened
