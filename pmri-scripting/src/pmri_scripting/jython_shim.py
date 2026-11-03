"""Synthetic ``jmri`` namespace so real JMRI Jython scripts import unchanged.

JMRI's Jython environment exposes classes and utilities under the
``jmri.*`` package tree -- ``jmri.InstanceManager``, ``jmri.Path``,
``jmri.jmrit.automat.AbstractAutomaton``, and so on. Our runtime's
injected globals cover the *most* common access pattern (``sensors``,
``turnouts``, ``ACTIVE``, ``CLOSED``), but scripts that reach in via the
JMRI namespace fail with ``ImportError: No module named 'jmri'`` before
they even start.

This module builds a synthetic module tree and registers it in
``sys.modules`` on first :func:`install` call. Attribute coverage
targets the top-200 paths from ``stage-0/contracts/script-api.md`` --
the tail below rank 200 is a long list of one-off references not worth
the maintenance cost.

Design:

* Each synthetic module has ``__all__`` for the attributes we actually
  populate. Attribute misses fall through :func:`_missing` which raises
  a clear ``AttributeError`` naming the shim's coverage rank + a hint.
* Bean classes (:class:`jmri.Turnout`, :class:`jmri.Sensor`, ...) are
  re-exports of :mod:`pmri_core.beans.*` so ``isinstance`` checks work.
* :class:`jmri.InstanceManager` is a static-method class whose
  ``getDefault(cls)`` maps a JMRI Java class object (or one of our
  shim classes) to the corresponding shim manager on the running
  :class:`JmriShim` instance. The shim is picked up from a
  thread-local set by the runner just before ``exec()``.
* Modules under ``jmri.jmrix.*`` and the deep ``jmri.jmrit.*`` sub-trees
  are stubs -- scripts that touch them get a helpful error instead of a
  bare ``ImportError``.
"""
from __future__ import annotations

import types
from typing import TYPE_CHECKING, Any

from pmri_core.beans.block import Block
from pmri_core.beans.light import Light
from pmri_core.beans.memory import Memory
from pmri_core.beans.reporter import Reporter
from pmri_core.beans.route import Route
from pmri_core.beans.sensor import Sensor
from pmri_core.beans.signal_head import SignalHead
from pmri_core.beans.signal_mast import SignalMast
from pmri_core.beans.throttle import Throttle
from pmri_core.beans.turnout import Turnout
from pmri_core.state import LightState, PowerState, SensorState, TurnoutState

from pmri_scripting.automaton import AbstractAutomaton

if TYPE_CHECKING:
    from pmri_scripting.shim import JmriShim


# Thread-local slot the runner fills right before exec() so
# ``InstanceManager.getDefault(...)`` can find the active shim.
import threading  # noqa: E402
_active_shim: threading.local = threading.local()


def _get_active_shim() -> JmriShim:
    shim = getattr(_active_shim, "value", None)
    if shim is None:
        raise RuntimeError(
            "jmri.InstanceManager accessed outside a running script; the "
            "runtime shim is only available on script worker threads.",
        )
    return shim


def _bind_shim(shim: JmriShim) -> None:
    _active_shim.value = shim


def _unbind_shim() -> None:
    if hasattr(_active_shim, "value"):
        del _active_shim.value


# ----- jmri.Path -----------------------------------------------------------


class Path:
    """Layout Path constants and helpers.

    JMRI's ``jmri.Path`` is a compass-direction bit field used by block
    tracking and route logic. We expose the constants and
    :meth:`decodeDirection`; anything that needs the full path/turnout
    linkage falls back to a clear AttributeError.
    """

    NONE = 0
    NORTH = 1
    SOUTH = 2
    EAST = 4
    WEST = 8
    CW = 16      # clockwise
    CCW = 32     # counter-clockwise
    LEFT = 64
    RIGHT = 128
    UP = 256
    DOWN = 512

    _NAMES = {
        NONE: "None", NORTH: "North", SOUTH: "South", EAST: "East",
        WEST: "West", CW: "CW", CCW: "CCW", LEFT: "Left",
        RIGHT: "Right", UP: "Up", DOWN: "Down",
    }

    @staticmethod
    def decodeDirection(direction: int) -> str:  # noqa: N802 -- JMRI casing
        """Return the human-readable name for a Path direction bit."""
        return Path._NAMES.get(direction, f"Unknown({direction})")


# ----- jmri.DccLocoAddress -------------------------------------------------


class DccLocoAddress:
    """Address container matching JMRI's ``jmri.DccLocoAddress`` shape.

    Constructor + ``getNumber`` + ``isLongAddress`` cover every use in
    the top-200 scripts. Equality follows JMRI's semantics: two
    addresses are equal iff both number and long-address flag match.
    """

    __slots__ = ("_num", "_long")

    def __init__(self, number: int, long_address: bool) -> None:
        self._num = int(number)
        self._long = bool(long_address)

    def getNumber(self) -> int:  # noqa: N802
        return self._num

    def isLongAddress(self) -> bool:  # noqa: N802
        return self._long

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DccLocoAddress):
            return NotImplemented
        return self._num == other._num and self._long == other._long

    def __hash__(self) -> int:
        return hash((self._num, self._long))

    def __repr__(self) -> str:
        return f"DccLocoAddress({self._num}, long={self._long})"


# ----- jmri.PowerManager ---------------------------------------------------


class PowerManager:
    """Static holder for JMRI PowerManager constants + instance methods.

    The class itself doubles as the type key for
    ``InstanceManager.getDefault(PowerManager)``; instances of it (rare
    in scripts) fall through to the active shim's PowerShim.
    """

    ON = PowerState.ON
    OFF = PowerState.OFF
    IDLE = PowerState.IDLE
    UNKNOWN = PowerState.UNKNOWN


# ---- InstanceManager.getDefault type keys --------------------------------

# These are placeholder classes used as class keys -- scripts do
# ``InstanceManager.getDefault(ConsistManager)`` and we route by
# ``is`` identity to the shim's manager. Kept as bare classes (not
# instances) to match how JMRI Java Jython scripts pass the class
# object itself as the lookup key.


class ConsistManager:
    """Type key for :meth:`_InstanceManager.getDefault` -- resolves to
    the shim's :class:`~pmri_scripting.shim._ConsistShim`. See the
    active shim's ``consists`` attribute for the runtime object."""


class ProgrammerManager:
    """Type key for :meth:`_InstanceManager.getDefault` -- resolves to
    the shim's :class:`~pmri_scripting.shim._ProgrammerShim`."""


# ----- jmri.InstanceManager -----------------------------------------------


class _InstanceManager:
    """Service locator matching JMRI's ``jmri.InstanceManager``.

    ``getDefault(cls)`` returns the shim manager that services *cls* on
    the currently running script's :class:`JmriShim`. Recognised
    classes:

    * :class:`Sensor`, :class:`Turnout`, :class:`Light`, :class:`Memory`,
      :class:`Reporter`, :class:`Block`, :class:`Route`,
      :class:`SignalHead`, :class:`SignalMast` -- return the matching
      manager.
    * :class:`PowerManager` -- return the shim's power manager.

    Unknown types raise a clear AttributeError so a script author can
    tell it's a shim-coverage gap, not a JMRI runtime bug.
    """

    @staticmethod
    def getDefault(cls: Any) -> Any:  # noqa: N802 -- JMRI casing
        shim = _get_active_shim()
        # Map class -> shim manager. Uses identity comparison because
        # scripts import the same class objects we exported.
        if cls is Sensor:
            return shim.sensors
        if cls is Turnout:
            return shim.turnouts
        if cls is Light:
            return shim.lights
        if cls is Memory:
            return shim.memories
        if cls is Reporter:
            return shim.reporters
        if cls is Block:
            return shim.blocks
        if cls is Route:
            return shim.routes
        if cls is SignalHead:
            return shim.signals
        if cls is SignalMast:
            return shim.masts
        if cls is PowerManager:
            return shim.powermanager
        if cls is ConsistManager:
            return shim.consists
        if cls is ProgrammerManager:
            return shim.programmers
        raise AttributeError(
            f"InstanceManager.getDefault({cls!r}) is not shimmed. Common "
            "shimmed types: Sensor, Turnout, Light, Memory, Reporter, "
            "Block, Route, SignalHead, SignalMast, PowerManager, "
            "ConsistManager, ProgrammerManager.",
        )

    @staticmethod
    def getList(cls: Any) -> list[Any]:  # noqa: N802
        """Return all instances of the manager's beans as a plain list.

        JMRI's version returns a Java List; here we hand back a Python
        list of shim-wrapped beans. Scripts that call ``len(...)`` or
        iterate over the return value work either way.
        """
        mgr = _InstanceManager.getDefault(cls)
        return list(mgr)


# ----- jmri.util.FileUtil --------------------------------------------------


class _FileUtil:
    """Subset of ``jmri.util.FileUtil`` used by the top-200 scripts.

    JMRI's version implements a portable ``preference:``, ``profile:``,
    ``resource:`` URL scheme. Our lightweight version maps the same
    prefixes onto the current profile's directories where possible,
    and returns raw paths otherwise so most script uses ``open(...)``
    calls still work.
    """

    @staticmethod
    def getExternalFilename(name: str) -> str:  # noqa: N802
        """Resolve a JMRI-style filename to a native path.

        Supported prefixes:

        * ``profile:X`` -> ``<profile>/X`` when a profile is bound.
        * ``preference:X`` -> same as profile (JMRI conflates them at
          this layer).
        * Anything else -> passthrough (assumed already-native).
        """
        for prefix in ("profile:", "preference:"):
            if name.startswith(prefix):
                try:
                    shim = _get_active_shim()
                    profile_dir = getattr(shim, "profile_dir", None)
                    if profile_dir is not None:
                        return str(profile_dir / name[len(prefix):])
                except RuntimeError:
                    pass
                return name[len(prefix):]
        return name


# ----- module tree construction --------------------------------------------


def _missing(module_name: str, attr: str) -> "AttributeError":
    """Build a helpful AttributeError for an un-shimmed attribute."""
    return AttributeError(
        f"module {module_name!r} has no attribute {attr!r}. "
        "The Jython namespace shim covers the top-200 paths from "
        "stage-0/contracts/script-api.md; this one falls outside. "
        "Either use the shim's injected globals (sensors, turnouts, "
        "...) or file an issue if this path is important.",
    )


def _make_module(name: str, **attrs: Any) -> types.ModuleType:
    """Create a synthetic ModuleType with attribute-level fallthrough."""
    mod = types.ModuleType(name)
    mod.__all__ = list(attrs.keys())
    for k, v in attrs.items():
        setattr(mod, k, v)

    def _module_getattr(item: str) -> Any:
        raise _missing(name, item)

    mod.__getattr__ = _module_getattr  # type: ignore[method-assign]
    return mod


def _build_tree() -> dict[str, types.ModuleType]:
    """Assemble the ``jmri.*`` module tree without registering yet."""
    # jmri.jmrit.automat -- the AbstractAutomaton lives here in JMRI Java.
    pmri_jmrit_automat = _make_module(
        "jmri.jmrit.automat", AbstractAutomaton=AbstractAutomaton,
    )
    # Populated stubs for the sub-namespaces most scripts probe by
    # existence check; each raises via _missing on unknown attribute
    # access.
    pmri_jmrit_roster = _make_module("jmri.jmrit.roster")
    pmri_jmrit_operations = _make_module("jmri.jmrit.operations")
    pmri_jmrit_operations_trains = _make_module(
        "jmri.jmrit.operations.trains",
    )
    pmri_jmrit_operations_rollingstock = _make_module(
        "jmri.jmrit.operations.rollingstock",
    )
    pmri_jmrit_display = _make_module("jmri.jmrit.display")
    pmri_jmrit_catalog = _make_module("jmri.jmrit.catalog")
    pmri_jmrit_tracker = _make_module("jmri.jmrit.tracker")

    pmri_jmrit = _make_module(
        "jmri.jmrit",
        automat=pmri_jmrit_automat,
        roster=pmri_jmrit_roster,
        operations=pmri_jmrit_operations,
        display=pmri_jmrit_display,
        catalog=pmri_jmrit_catalog,
        tracker=pmri_jmrit_tracker,
    )

    pmri_jmrix = _make_module("jmri.jmrix")
    pmri_jmrix_loconet = _make_module("jmri.jmrix.loconet")
    pmri_jmrix_mqtt = _make_module("jmri.jmrix.mqtt")

    pmri_util = _make_module(
        "jmri.util",
        FileUtil=_FileUtil,
    )
    pmri_implementation = _make_module("jmri.implementation")

    jmri = _make_module(
        "jmri",
        # Service locator + constants used by the vast majority of scripts.
        InstanceManager=_InstanceManager,
        Path=Path,
        PowerManager=PowerManager,
        ConsistManager=ConsistManager,
        ProgrammerManager=ProgrammerManager,
        DccLocoAddress=DccLocoAddress,
        # Bean class re-exports -- used for isinstance and as
        # InstanceManager.getDefault keys.
        Sensor=Sensor,
        Turnout=Turnout,
        Light=Light,
        Memory=Memory,
        Reporter=Reporter,
        Block=Block,
        Route=Route,
        SignalHead=SignalHead,
        SignalMast=SignalMast,
        Throttle=Throttle,
        # Wire-value enums, so a script that does
        # ``jmri.Turnout.CLOSED`` still works via the class attribute
        # even though our Turnout doesn't define a nested constant.
        # We expose them as sibling attributes so both forms resolve.
        SensorState=SensorState,
        TurnoutState=TurnoutState,
        LightState=LightState,
        PowerState=PowerState,
        # Submodules -- must be settable so ``from jmri import jmrit`` works.
        jmrit=pmri_jmrit,
        jmrix=pmri_jmrix,
        util=pmri_util,
        implementation=pmri_implementation,
    )

    return {
        "jmri": jmri,
        "jmri.jmrit": pmri_jmrit,
        "jmri.jmrit.automat": pmri_jmrit_automat,
        "jmri.jmrit.roster": pmri_jmrit_roster,
        "jmri.jmrit.operations": pmri_jmrit_operations,
        "jmri.jmrit.operations.trains": pmri_jmrit_operations_trains,
        "jmri.jmrit.operations.rollingstock": pmri_jmrit_operations_rollingstock,
        "jmri.jmrit.display": pmri_jmrit_display,
        "jmri.jmrit.catalog": pmri_jmrit_catalog,
        "jmri.jmrit.tracker": pmri_jmrit_tracker,
        "jmri.jmrix": pmri_jmrix,
        "jmri.jmrix.loconet": pmri_jmrix_loconet,
        "jmri.jmrix.mqtt": pmri_jmrix_mqtt,
        "jmri.util": pmri_util,
        "jmri.implementation": pmri_implementation,
    }


_INSTALLED = False


def install() -> None:
    """Register the synthetic ``jmri.*`` modules in :data:`sys.modules`.

    Idempotent -- invoked at module import time (see the bottom of this
    file) so ``import jmri`` works immediately, and again from
    :meth:`ScriptRunner.__init__` as a defensive belt. After this runs,
    scripts can do ``from jmri import InstanceManager`` and
    ``from jmri.jmrit.automat import AbstractAutomaton`` without
    triggering the real Python import machinery.

    ``sys.modules.setdefault`` is used so that if someone has a
    legitimate ``jmri`` package pip-installed (rare but possible), we
    don't clobber it -- the real package wins.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    import sys  # noqa: PLC0415
    for name, mod in _build_tree().items():
        sys.modules.setdefault(name, mod)
    _INSTALLED = True


# Install eagerly. Importing this module means the caller wants the
# ``jmri.*`` names available; running the registration at load time
# keeps ``import jmri`` working from any code path that touches
# pmri_scripting, including tests that inspect the shim without
# constructing a ScriptRunner.
install()
