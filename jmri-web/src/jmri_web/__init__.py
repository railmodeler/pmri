"""jmri-web -- Stage 5 of the JMRI Python rewrite.

A server-rendered web UI built on htmx + Jinja2 + FastAPI. Mounts on top
of ``jmri-server`` so a single process serves both the JSON wire API and
the human-facing pages, sharing one in-memory ``InternalConnection`` (or
a driver-backed one, once Stage 5.5 wires connection selection).
"""
from jmri_web.app import build_web_app, create_app

__version__ = "0.1.0"

__all__ = ["__version__", "build_web_app", "create_app"]
