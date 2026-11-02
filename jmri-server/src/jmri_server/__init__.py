"""jmri-server -- Stage 3 of the JMRI Python rewrite.

A FastAPI + WebSocket server speaking JMRI's JSON wire protocol on top of
:mod:`jmri_core`'s in-memory `InternalConnection`. The HTTP and WebSocket
endpoints share one dispatcher; every reply validates against the JSON
schemas extracted from JMRI's jar in Stage 0.
"""
from jmri_server.app import build_app, create_app

__version__ = "0.1.0"

__all__ = ["build_app", "create_app", "__version__"]
