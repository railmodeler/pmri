"""Public script-authoring API -- the shim as an importable module.

The runtime shim's :meth:`JmriShim.globals` injects ``sensors``,
``turnouts``, ``powermanager``, etc. into every script's namespace, so
scripts can use them without any import. That's the JMRI-Jython
convention and it's covered.

This module is the *authored* alternative: scripts (or small helper
modules a user writes) can ``from pmri_scripting.api import turnouts,
powermanager`` and reference the same shim managers explicitly. The
extra clarity helps when:

* one script factors reusable code into a helper module and the helper
  can't rely on the injected globals (module top-level code runs at
  import time, not exec time);
* an IDE / type-checker resolves imports and needs a real symbol;
* a user prefers explicit-over-implicit imports.

Semantics: each name is a **module-level attribute proxy** that
resolves via :data:`jython_shim._active_shim` on every access. So the
same import works across scripts on the same worker thread -- each
script binds its shim before ``exec()`` and unbinds on teardown.
"""
from __future__ import annotations

from typing import Any

from pmri_scripting import jython_shim


class _ShimProxy:
    """Attribute proxy that reads *attr* off the active :class:`JmriShim`.

    A single proxy instance is installed at module import time as one
    of ``sensors``, ``turnouts``, etc. Every attribute access on the
    proxy is forwarded to the active shim's matching attribute -- so a
    helper module can capture ``turnouts`` at import time and every
    call still runs against the currently-executing script's shim.

    Trying to use a proxy outside a running script raises the same
    clear error as :meth:`jython_shim._get_active_shim`.
    """

    __slots__ = ("_attr",)

    def __init__(self, attr: str) -> None:
        self._attr = attr

    def _target(self) -> Any:
        return getattr(jython_shim._get_active_shim(), self._attr)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __iter__(self) -> Any:
        return iter(self._target())

    def __len__(self) -> int:
        return len(self._target())

    def __repr__(self) -> str:
        try:
            return f"<api.{self._attr} -> {self._target()!r}>"
        except RuntimeError:
            return f"<api.{self._attr} (no active script)>"


sensors = _ShimProxy("sensors")
turnouts = _ShimProxy("turnouts")
lights = _ShimProxy("lights")
memories = _ShimProxy("memories")
reporters = _ShimProxy("reporters")
blocks = _ShimProxy("blocks")
routes = _ShimProxy("routes")
signals = _ShimProxy("signals")
masts = _ShimProxy("masts")
powermanager = _ShimProxy("powermanager")
consists = _ShimProxy("consists")
programmers = _ShimProxy("programmers")


# The state-constant enums are just re-exports -- they aren't
# per-script.
from pmri_core.state import (  # noqa: E402
    LightState,
    PowerState,
    SensorState,
    ThrottleDirection,
    TurnoutState,
)


# Bare state constants, matching the injected globals namespace.
# JMRI Jython scripts commonly do ``t.setCommandedState(THROWN)``
# with no explicit import; exposing the same names here means a
# script that opts into ``from pmri_scripting.api import ...`` can
# still write the same code.
ACTIVE = SensorState.ACTIVE
INACTIVE = SensorState.INACTIVE
CLOSED = TurnoutState.CLOSED
THROWN = TurnoutState.THROWN
ON = LightState.ON
OFF = LightState.OFF
FORWARD = ThrottleDirection.FORWARD
REVERSE = ThrottleDirection.REVERSE


__all__ = [
    "ACTIVE",
    "CLOSED",
    "FORWARD",
    "INACTIVE",
    "LightState",
    "OFF",
    "ON",
    "PowerState",
    "REVERSE",
    "SensorState",
    "THROWN",
    "ThrottleDirection",
    "TurnoutState",
    "blocks",
    "consists",
    "lights",
    "masts",
    "memories",
    "powermanager",
    "programmers",
    "reporters",
    "routes",
    "sensors",
    "signals",
    "turnouts",
]
