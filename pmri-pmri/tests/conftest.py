"""Shared fixtures.

The tests avoid customtkinter entirely -- a tk mainloop needs a
display and the GUI module exercises widgets, not logic worth
retesting here. Launcher pieces that DO have logic (service
controller, picker, CLI arg parsing, browser timing) live in
plain modules with no tk dependency, and that's what we cover.
"""
from __future__ import annotations
