"""PMRI -- Python Model Railroad Interface, meta-package.

Installing ``pmri`` pulls in the full PMRI stack: the launcher
(``pmri-pmri``), web service (``pmri-web`` + ``pmri-server``), and
hardware drivers (NCE, LocoNet, DCC-EX, OpenLCB, and the Internal
loopback). Launch the GUI with the installed ``pmri`` console
script, or ``python -m pmri_pmri`` as a fallback.

This module itself is intentionally empty -- the meta-package exists
only to declare its dependencies and ship a single installable
version number. Operators do not import ``pmri`` directly.

Stub driver packages ``pmri-lenz`` and ``pmri-mrc`` are published on
PyPI but are NOT pulled in by the meta-package; install those
explicitly when they move out of stub status.
"""
from __future__ import annotations

__version__ = "0.1.2"
__all__ = ["__version__"]
