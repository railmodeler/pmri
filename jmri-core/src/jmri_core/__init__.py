"""jmri_core -- Python rewrite of JMRI's core domain model.

This package is **pure logic with no I/O**. It contains:

- :mod:`jmri_core.bean`     -- :class:`NamedBean` protocol and
  :class:`AbstractNamedBean` mixin.
- :mod:`jmri_core.events`   -- :class:`Signal`, the async-friendly event bus
  that replaces JMRI's ``PropertyChangeListener``.
- :mod:`jmri_core.manager`  -- :class:`Manager` generic, the per-type registry
  underlying every JMRI ``*Manager``.
- :mod:`jmri_core.instance` -- :class:`InstanceManager`, the service-locator
  container.
- :mod:`jmri_core.names`    -- system-name parsing (``"LT12"`` -> connection
  prefix ``"L"``, type letter ``"T"``, address ``"12"``).
- :mod:`jmri_core.state`    -- shared state enums.
- :mod:`jmri_core.beans`    -- concrete bean classes (Turnout, Sensor, ...).
- :mod:`jmri_core.hal`      -- :class:`SystemConnection` ABC + internal stub.
- :mod:`jmri_core.persistence` -- XML load/save for roster and config files.
- :mod:`jmri_core.plugins` -- entry-point discovery for drivers and extensions.

Hardware drivers and UI live in separate packages that depend on this one;
this package never depends on them.
"""
from jmri_core.bean import AbstractNamedBean, NamedBean
from jmri_core.events import Signal
from jmri_core.instance import InstanceManager
from jmri_core.manager import Manager
from jmri_core.names import SystemName

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
