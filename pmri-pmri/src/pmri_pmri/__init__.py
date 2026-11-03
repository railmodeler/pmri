"""pmri-pmri -- PMRI launcher for the Python Model Railroad Interface.

Single launch point for the whole web stack (pmri-server + pmri-web +
pmri-withrottle). Replaces the ``uvicorn pmri_server:create_app --factory``
command line with a customtkinter GUI that owns the uvicorn lifecycle:

* Start / Stop buttons for the web service.
* "Open UI" opens the user's default browser to the running service.
* Edit tabs for ``pmri.xml`` (install-level config) and ``profile.xml``
  (per-profile connections + script list).
* ``--headless`` CLI flag skips the GUI for Pi / systemd installs.

Entry point: ``pmri`` on PATH (see pyproject's console_scripts) or
``python -m pmri_pmri``.
"""
from pmri_pmri.browser import open_browser_when_ready
from pmri_pmri.service import ServiceController, ServiceState

__version__ = "0.1.0"
__all__ = [
    "ServiceController",
    "ServiceState",
    "__version__",
    "open_browser_when_ready",
]
