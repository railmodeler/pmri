"""``JmriShim`` -- the JMRI-style globals injected into every script.

JMRI scripts (the 369 Jython files we inventoried in Stage 0) call APIs
like ``sensors.provideSensor("LS5")`` synchronously and get a Sensor
back. Our core is async, so the shim bridges via
:func:`asyncio.run_coroutine_threadsafe` against the event loop the
:class:`ScriptRunner` retains a reference to.

Every shim method runs *on the script's worker thread* and blocks until
the corresponding async coroutine completes on the event-loop thread.
Scripts therefore feel like the original JMRI experience: ``sensor =
sensors.provideSensor("LS5"); sensor.setKnownState(ACTIVE)``.

Top-N coverage targets the most-used paths from Stage 0's
``contracts/script-api.md`` ranking.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, cast

from jmri_core.beans.sensor import Sensor
from jmri_core.beans.turnout import Turnout
from jmri_core.state import (
    LightState,
    PowerState,
    SensorState,
    ThrottleDirection,
    TurnoutState,
)

if TYPE_CHECKING:
    from jmri_core.beans.block import Block
    from jmri_core.beans.light import Light
    from jmri_core.beans.memory import Memory
    from jmri_core.beans.reporter import Reporter
    from jmri_core.hal.connection import SystemConnection

_log = logging.getLogger(__name__)


def _block(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """Run *coro* on *loop* from a different thread and wait for the result."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


class _ManagerShim[B, W]:
    """Sync facade over a :class:`jmri_core.manager.Manager`.

    Wraps every bean it hands back with *wrap*, so scripts get the
    JMRI-style camelCase methods on the wrapper rather than the raw
    async-native bean.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        manager: Any,
        wrap: Callable[[B], W],
    ) -> None:
        self._loop = loop
        self._mgr = manager
        self._wrap = wrap

    def provide(self, name: str) -> W:
        bean: B = _block(self._loop, self._mgr.provide(name))
        return self._wrap(bean)

    def get(self, name: str) -> W | None:
        bean = self._mgr.get(name)
        return None if bean is None else self._wrap(bean)

    def get_by_system_name(self, name: str) -> W | None:
        bean = self._mgr.get_by_system_name(name)
        return None if bean is None else self._wrap(bean)

    def get_by_user_name(self, name: str) -> W | None:
        bean = self._mgr.get_by_user_name(name)
        return None if bean is None else self._wrap(bean)

    def get_named_bean_set(self) -> list[W]:
        return [self._wrap(b) for b in self._mgr.named_bean_set()]

    def __iter__(self) -> Iterator[W]:
        return iter(self._wrap(b) for b in self._mgr)

    def __len__(self) -> int:
        return len(self._mgr)

    # JMRI-style camelCase aliases. Each manager only ever uses one
    # of provideX/getX so multiple aliases on the same class is fine.
    provideSensor = provide
    provideTurnout = provide
    provideLight = provide
    provideMemory = provide
    provideReporter = provide
    provideBlock = provide
    provideRoute = provide
    getSensor = get
    getTurnout = get
    getLight = get
    getMemory = get
    getReporter = get
    getBlock = get
    getSignalHead = get
    getSignalMast = get
    getByUserName = get_by_user_name
    getBySystemName = get_by_system_name
    getNamedBeanSet = get_named_bean_set


# --- bean shims --------------------------------------------------------------


class _SensorShim:
    """Sync wrapper over a real Sensor."""

    __slots__ = ("_bean", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, bean: Sensor) -> None:
        self._loop = loop
        self._bean = bean

    @property
    def system_name(self) -> str:
        return self._bean.system_name

    @property
    def user_name(self) -> str | None:
        return self._bean.user_name

    @property
    def known_state(self) -> SensorState:
        return self._bean.known_state

    @property
    def isActive(self) -> bool:  # noqa: N802 -- JMRI casing
        return self._bean.is_active

    def setKnownState(self, state: SensorState) -> None:  # noqa: N802
        _block(self._loop, self._bean._set_known_state(state))

    def getKnownState(self) -> SensorState:  # noqa: N802
        return self._bean.known_state


class _TurnoutShim:
    """Sync wrapper over a Turnout."""

    __slots__ = ("_bean", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, bean: Turnout) -> None:
        self._loop = loop
        self._bean = bean

    @property
    def system_name(self) -> str:
        return self._bean.system_name

    @property
    def user_name(self) -> str | None:
        return self._bean.user_name

    @property
    def commanded_state(self) -> TurnoutState:
        return self._bean.commanded_state

    @property
    def known_state(self) -> TurnoutState:
        return self._bean.known_state

    def setCommandedState(self, state: TurnoutState) -> None:  # noqa: N802
        _block(self._loop, self._bean.set_commanded_state(state))

    def getCommandedState(self) -> TurnoutState:  # noqa: N802
        return self._bean.commanded_state

    def getKnownState(self) -> TurnoutState:  # noqa: N802
        return self._bean.known_state


class _LightShim:
    __slots__ = ("_bean", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, bean: Light) -> None:
        self._loop = loop
        self._bean = bean

    @property
    def system_name(self) -> str:
        return self._bean.system_name

    @property
    def state(self) -> LightState:
        return self._bean.state

    def setState(self, state: LightState) -> None:  # noqa: N802
        _block(self._loop, self._bean.set_state(state))

    def getState(self) -> LightState:  # noqa: N802
        return self._bean.state


class _MemoryShim:
    __slots__ = ("_bean", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, bean: Memory) -> None:
        self._loop = loop
        self._bean = bean

    @property
    def system_name(self) -> str:
        return self._bean.system_name

    @property
    def value(self) -> Any:
        return self._bean.value

    def setValue(self, value: Any) -> None:  # noqa: N802
        _block(self._loop, self._bean.set_value(value))

    def getValue(self) -> Any:  # noqa: N802
        return self._bean.value


class _ReporterShim:
    __slots__ = ("_bean", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, bean: Reporter) -> None:
        self._loop = loop
        self._bean = bean

    @property
    def system_name(self) -> str:
        return self._bean.system_name

    @property
    def current_report(self) -> Any:
        return self._bean.current_report

    def getCurrentReport(self) -> Any:  # noqa: N802
        return self._bean.current_report

    def getLastReport(self) -> Any:  # noqa: N802
        return self._bean.last_report


class _BlockShim:
    __slots__ = ("_bean", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, bean: Block) -> None:
        self._loop = loop
        self._bean = bean

    @property
    def system_name(self) -> str:
        return self._bean.system_name

    @property
    def state(self) -> Any:
        return self._bean.state

    @property
    def value(self) -> Any:
        return self._bean.value

    def setValue(self, value: Any) -> None:  # noqa: N802
        _block(self._loop, self._bean.set_value(value))


class _PowerShim:
    """Sync wrapper over the connection's PowerManager.

    JMRI Java's Jython bridge exposes ``powermanager.getPower()`` which
    returns an integer state code -- usable but you have to remember to
    compare against ``powermanager.ON``. The Python rewrite adds
    friendlier accessors as first-class properties (:attr:`state`,
    :attr:`isOn`, :attr:`isOff`, :attr:`isIdle`, :attr:`isUnknown`) so
    scripts can write ``if powermanager.isOn: ...`` directly. ``getPower``
    is retained verbatim for JMRI-Jython script compatibility.

    Two independent power questions matter to scripts:

    * **Track power** -- is the DCC signal live on the rails? Controlled
      by :meth:`setPower` / :meth:`getPower` and the ``isOn`` /
      ``isOff`` / ``isIdle`` properties. This is what the CS sends to
      the layout and what most scripts check ("don't try to move locos
      when the track power is off").
    * **Command-station power** -- is the CS itself talking to us at
      all? Exposed as :attr:`isCommandStationOn` /
      :meth:`getCommandStationPower`. Always ``True`` when the
      connection is open under the default driver semantics; some
      drivers with heartbeat detection can flip it False mid-session
      when the CS goes down.
    """

    __slots__ = ("_conn", "_loop", "_pm")

    ON = PowerState.ON
    OFF = PowerState.OFF
    IDLE = PowerState.IDLE
    UNKNOWN = PowerState.UNKNOWN

    def __init__(
        self, loop: asyncio.AbstractEventLoop, pm: Any, connection: Any = None,
    ) -> None:
        self._loop = loop
        self._pm = pm
        self._conn = connection

    def setPower(self, state: PowerState | int) -> None:  # noqa: N802
        if isinstance(state, int) and not isinstance(state, PowerState):
            state = PowerState(state)
        _block(self._loop, self._pm.set_power(state))

    def getPower(self) -> PowerState:  # noqa: N802
        return cast("PowerState", self._pm.state)

    @property
    def state(self) -> PowerState:
        """Current track power state as a :class:`PowerState` enum.

        Pythonic alternative to :meth:`getPower` -- the enum's
        ``.name`` (``"ON"`` / ``"OFF"`` / ``"IDLE"`` / ``"UNKNOWN"``)
        formats nicely in logs and ``str()`` calls.
        """
        return cast("PowerState", self._pm.state)

    @property
    def isOn(self) -> bool:  # noqa: N802
        """``True`` iff track power is currently ON.

        The most common script query, elevated to a one-name check.
        """
        return self._pm.state == PowerState.ON

    @property
    def isOff(self) -> bool:  # noqa: N802
        """``True`` iff track power is currently OFF."""
        return self._pm.state == PowerState.OFF

    @property
    def isIdle(self) -> bool:  # noqa: N802
        """``True`` iff track power is IDLE (some CS report this after e-stop)."""
        return self._pm.state == PowerState.IDLE

    @property
    def isUnknown(self) -> bool:  # noqa: N802
        """``True`` when we haven't yet observed a power state.

        Fresh boot before the driver has heard from the CS, or a driver
        that doesn't push status changes and hasn't been polled yet.
        """
        return self._pm.state == PowerState.UNKNOWN

    # ----- command-station power -----------------------------------------
    #
    # Track power (above) is DCC-on-rails. CS power is "is the command
    # station itself talking to us at all?" -- a strict bool since the
    # only visible states are On (open connection) and Off (no CS).

    @property
    def isCommandStationOn(self) -> bool:  # noqa: N802
        """``True`` when the command station itself is powered / reachable.

        Base drivers tie this to the connection being open; drivers with
        heartbeat detection may flip this False without closing the
        connection. Scripts wanting "should I even try to talk to the
        CS right now?" should check this before ``isOn``.
        """
        if self._conn is None:
            return False
        return bool(getattr(self._conn, "is_command_station_on", False))

    def getCommandStationPower(self) -> bool:  # noqa: N802
        """JMRI-Java-shaped accessor returning ``True`` iff the CS is
        powered. Mirrors :meth:`getPower` for track power."""
        return self.isCommandStationOn


class _ConsistShim:
    """Sync wrapper over the connection's ConsistManager.

    JMRI Java's ``ConsistManager`` API used by Jython scripts::

        cm = InstanceManager.getDefault(ConsistManager)
        c = cm.getConsist(dcc_address)
        c.add(loco_address, direction_normal=True)
        c.remove(loco_address)
        cm.delConsist(dcc_address)

    We match those names -- ``getConsist`` auto-creates a new consist
    if one doesn't exist yet at that address (matches JMRI Java's
    behavior; ``newConsist`` is an alias for the same). Speed /
    function / direction fan-out at the consist level is handled by
    the underlying async manager; the shim delegates through the
    manager rather than the individual Consist bean so per-driver
    dispatch (LocoNet advanced, NCE memory-backed, DCC-EX) still
    fires.
    """

    __slots__ = ("_loop", "_mgr")

    def __init__(self, loop: asyncio.AbstractEventLoop, mgr: Any) -> None:
        self._loop = loop
        self._mgr = mgr

    def getConsist(self, address: int) -> "_ConsistProxy":  # noqa: N802
        """Return the consist at *address*, creating it if absent."""
        existing = self._mgr.get(address)
        if existing is None:
            _block(self._loop, self._mgr.create(address))
        return _ConsistProxy(self._loop, self._mgr, address)

    def newConsist(self, address: int) -> "_ConsistProxy":  # noqa: N802
        """Alias for :meth:`getConsist` -- JMRI Java scripts use both."""
        return self.getConsist(address)

    def delConsist(self, address: int) -> None:  # noqa: N802
        """Delete the consist at *address*."""
        _block(self._loop, self._mgr.delete(address))

    def getConsistList(self) -> list[int]:  # noqa: N802
        """Return the DCC addresses of every registered consist."""
        return [c.address for c in self._mgr]


class _ConsistProxy:
    """Per-consist facade returned from :meth:`_ConsistShim.getConsist`.

    Kept thin: every mutation goes through the manager so hardware
    dispatch happens uniformly. Read state via :attr:`members` /
    :attr:`getConsistAddress`.
    """

    __slots__ = ("_loop", "_mgr", "_address")

    def __init__(
        self, loop: asyncio.AbstractEventLoop, mgr: Any, address: int,
    ) -> None:
        self._loop = loop
        self._mgr = mgr
        self._address = address

    def getConsistAddress(self) -> int:  # noqa: N802
        return self._address

    def add(self, loco: int, direction_normal: bool = True) -> None:
        """Add *loco* to the consist. ``direction_normal=False`` for a
        loco pointing backwards relative to the consist lead."""
        _block(self._loop, self._mgr.add_member(
            self._address, loco, direction_normal=direction_normal,
        ))

    def remove(self, loco: int) -> None:
        _block(self._loop, self._mgr.remove_member(self._address, loco))

    @property
    def members(self) -> list[int]:
        consist = self._mgr.get(self._address)
        if consist is None:
            return []
        # The Consist model stores members under ``locos`` (per JMRI
        # Java's field name); expose the more script-friendly
        # ``members`` for a caller iterating over addresses.
        return [m.address for m in consist.locos]


class _ProgrammerShim:
    """Sync wrapper over the connection's ProgrammerManager.

    JMRI Java's Jython usage::

        pm = InstanceManager.getDefault(ProgrammerManager)
        prog = pm.getGlobalProgrammer()          # service mode
        prog = pm.getAddressedProgrammer(True, addr)  # ops mode

    Then on the returned programmer::

        value = prog.readCV(29)         # sync in JMRI Java
        prog.writeCV(29, value)

    CV reads block for 1-3s on service mode. Ops-mode reads require
    RailCom + a driver that supports it (currently DCC-EX; NCE / LocoNet
    don't).
    """

    __slots__ = ("_loop", "_mgr")

    def __init__(self, loop: asyncio.AbstractEventLoop, mgr: Any) -> None:
        self._loop = loop
        self._mgr = mgr

    def getGlobalProgrammer(self) -> "_ProgrammerProxy | None":  # noqa: N802
        prog = self._mgr.global_programmer()
        return _ProgrammerProxy(self._loop, prog) if prog is not None else None

    def getAddressedProgrammer(  # noqa: N802
        self, long_address: bool, address: int,
    ) -> "_ProgrammerProxy | None":
        prog = self._mgr.ops_programmer(address, long_address=long_address)
        return _ProgrammerProxy(self._loop, prog) if prog is not None else None


class _ProgrammerProxy:
    """Sync facade over a single :class:`~jmri_core.beans.programmer.Programmer`.

    Exposes ``readCV(cv)`` and ``writeCV(cv, value)`` -- the two calls
    every JMRI Jython programming script uses. Errors from the
    async layer (timeouts, driver-specific failures) propagate as
    the same exception type the async call raised.
    """

    __slots__ = ("_loop", "_prog")

    def __init__(
        self, loop: asyncio.AbstractEventLoop, prog: Any,
    ) -> None:
        self._loop = loop
        self._prog = prog

    def readCV(self, cv: int) -> int:  # noqa: N802
        return _block(self._loop, self._prog.read_cv(cv))

    def writeCV(self, cv: int, value: int) -> None:  # noqa: N802
        _block(self._loop, self._prog.write_cv(cv, value))


# ---- top-level shim --------------------------------------------------------


class JmriShim:
    """Bundle of script globals -- the namespace every script sees.

    A script's globals dict is populated from :meth:`globals`. JMRI-style
    bare names (``sensors``, ``turnouts``, ``powermanager``, ``ACTIVE``,
    ``CLOSED``, ``ON``, ...) work without imports, matching the existing
    Jython convention.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, connection: SystemConnection
    ) -> None:
        self._loop = loop
        self.connection = connection

        self.sensors: _ManagerShim[Any, _SensorShim] = _ManagerShim(
            loop, connection.sensors, lambda b: _SensorShim(loop, b),
        )
        self.turnouts: _ManagerShim[Any, _TurnoutShim] = _ManagerShim(
            loop, connection.turnouts, lambda b: _TurnoutShim(loop, b),
        )
        self.lights: _ManagerShim[Any, _LightShim] = _ManagerShim(
            loop, connection.lights, lambda b: _LightShim(loop, b),
        )
        self.memories: _ManagerShim[Any, _MemoryShim] = _ManagerShim(
            loop, connection.memories, lambda b: _MemoryShim(loop, b),
        )
        self.reporters: _ManagerShim[Any, _ReporterShim] = _ManagerShim(
            loop, connection.reporters, lambda b: _ReporterShim(loop, b),
        )
        self.blocks: _ManagerShim[Any, _BlockShim] = _ManagerShim(
            loop, connection.blocks, lambda b: _BlockShim(loop, b),
        )
        # Routes/signals are less commonly mutated from scripts; expose
        # them as raw managers so users can fall through to async if needed.
        self.routes: _ManagerShim[Any, Any] = _ManagerShim(
            loop, connection.routes, lambda b: b,
        )
        self.signals: _ManagerShim[Any, Any] = _ManagerShim(
            loop, connection.signal_heads, lambda b: b,
        )
        self.masts: _ManagerShim[Any, Any] = _ManagerShim(
            loop, connection.signal_masts, lambda b: b,
        )

        self.powermanager = _PowerShim(loop, connection.power, connection)
        # Consists + programmers -- both were on the deferred list
        # for the Jython shim through beta 3. Wire them here so
        # ``InstanceManager.getDefault(ConsistManager)`` and the
        # ``ProgrammerManager`` equivalent resolve for scripts
        # migrating from JMRI Java.
        self.consists = _ConsistShim(loop, connection.consists)
        self.programmers = _ProgrammerShim(loop, connection.programmers)

        # State constants -- JMRI scripts use bare ``ACTIVE``, ``CLOSED``, etc.
        self.ACTIVE = SensorState.ACTIVE
        self.INACTIVE = SensorState.INACTIVE
        self.CLOSED = TurnoutState.CLOSED
        self.THROWN = TurnoutState.THROWN
        self.ON = LightState.ON
        self.OFF = LightState.OFF
        self.FORWARD = ThrottleDirection.FORWARD
        self.REVERSE = ThrottleDirection.REVERSE

    def globals(self) -> dict[str, Any]:
        """Return the globals dict to inject into a script's ``exec`` namespace."""
        return {
            "sensors": self.sensors,
            "turnouts": self.turnouts,
            "lights": self.lights,
            "memories": self.memories,
            "reporters": self.reporters,
            "blocks": self.blocks,
            "routes": self.routes,
            "signals": self.signals,
            "masts": self.masts,
            "powermanager": self.powermanager,
            "consists": self.consists,
            "programmers": self.programmers,
            "ACTIVE": self.ACTIVE,
            "INACTIVE": self.INACTIVE,
            "CLOSED": self.CLOSED,
            "THROWN": self.THROWN,
            "ON": self.ON,
            "OFF": self.OFF,
            "FORWARD": self.FORWARD,
            "REVERSE": self.REVERSE,
            "SensorState": SensorState,
            "TurnoutState": TurnoutState,
            "LightState": LightState,
            "PowerState": PowerState,
            "shim": self,
        }
