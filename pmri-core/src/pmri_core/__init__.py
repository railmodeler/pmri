"""pmri_core -- Python rewrite of JMRI's core domain model.

This package is **pure logic with no I/O**. It contains:

- :mod:`pmri_core.bean`     -- :class:`NamedBean` protocol and
  :class:`AbstractNamedBean` mixin.
- :mod:`pmri_core.events`   -- :class:`Signal`, the async-friendly event bus
  that replaces JMRI's ``PropertyChangeListener``.
- :mod:`pmri_core.manager`  -- :class:`Manager` generic, the per-type registry
  underlying every JMRI ``*Manager``.
- :mod:`pmri_core.instance` -- :class:`InstanceManager`, the service-locator
  container.
- :mod:`pmri_core.names`    -- system-name parsing (``"LT12"`` -> connection
  prefix ``"L"``, type letter ``"T"``, address ``"12"``).
- :mod:`pmri_core.state`    -- shared state enums.
- :mod:`pmri_core.beans`    -- concrete bean classes (Turnout, Sensor, ...).
- :mod:`pmri_core.hal`      -- :class:`SystemConnection` ABC + internal stub.
- :mod:`pmri_core.persistence` -- XML load/save for roster and config files.
- :mod:`pmri_core.plugins` -- entry-point discovery for drivers and extensions.

Hardware drivers and UI live in separate packages that depend on this one;
this package never depends on them.
"""
from pmri_core.bean import AbstractNamedBean, NamedBean
from pmri_core.events import Signal
from pmri_core.instance import InstanceManager
from pmri_core.manager import Manager
from pmri_core.names import SystemName

__version__ = "0.1.0"

__all__ = [
    "AbstractNamedBean",
    "InstanceManager",
    "Manager",
    "NamedBean",
    "Signal",
    "SystemName",
    "__version__",
]
