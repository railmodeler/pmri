"""``SystemConnection`` -- the abstract base every hardware driver implements.

Each driver builds the per-bean managers, exposes a :class:`PowerManager`,
and (where relevant) the throttle/programmer interfaces. The base class
gives every connection a uniform shape: prefix + name + lifecycle hooks +
a uniform set of bean managers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pmri_core.beans.audio import AudioManager
from pmri_core.beans.block import BlockManager
from pmri_core.events import Signal
from pmri_core.beans.consist_manager import ConsistManager
from pmri_core.beans.id_tag import IdTagManager
from pmri_core.beans.layout_block import LayoutBlockManager
from pmri_core.beans.light import LightManager
from pmri_core.beans.memory import MemoryManager
from pmri_core.beans.oblock import OBlockManager
from pmri_core.beans.power import PowerManager
from pmri_core.beans.programmer import ProgrammerManager
from pmri_core.beans.reporter import ReporterManager
from pmri_core.beans.route import RouteManager
from pmri_core.beans.sensor import SensorManager
from pmri_core.beans.signal_head import SignalHeadManager
from pmri_core.beans.signal_mast import SignalMastManager
from pmri_core.beans.throttle_manager import ThrottleManager
from pmri_core.beans.turnout import TurnoutManager

ConnectionSettings = dict[str, Any]


class SystemConnection(ABC):
    """One hardware connection to a layout.

    Subclasses set ``name`` (vendor-friendly label) and ``prefix`` (the JMRI
    system-name prefix). After :meth:`open` returns, all the bean managers
    must be populated; closed connections deregister themselves.
    """

    name: ClassVar[str] = ""
    prefix: ClassVar[str] = ""

    def __init__(self) -> None:
        if not self.name:
            raise TypeError(f"{type(self).__name__}.name must be set")
        if not self.prefix:
            raise TypeError(f"{type(self).__name__}.prefix must be set")

        # Build the default managers. Drivers override these in open() with
        # connection-specific subclasses that talk to hardware.
        self.turnouts: TurnoutManager       = self._make_turnout_manager()
        self.sensors: SensorManager         = self._make_sensor_manager()
        self.signal_heads: SignalHeadManager = self._make_signal_head_manager()
        self.signal_masts: SignalMastManager = self._make_signal_mast_manager()
        self.blocks: BlockManager           = self._make_block_manager()
        self.memories: MemoryManager        = self._make_memory_manager()
        self.reporters: ReporterManager     = self._make_reporter_manager()
        self.lights: LightManager           = self._make_light_manager()
        self.routes: RouteManager           = self._make_route_manager()
        self.throttles: ThrottleManager     = self._make_throttle_manager()
        self.programmers: ProgrammerManager = self._make_programmer_manager()
        self.consists: ConsistManager       = self._make_consist_manager()
        # The four beans added in the "missing JSON topics" pass. These
        # are software-only for every driver -- no wire ops attached to
        # the audio state, no reporter-side write to IdTag, no
        # occupancy logic on layout blocks or oblocks. Managers are
        # here so scripts and the JSON layer can register beans on any
        # connection uniformly; real hardware integration lands with
        # Panel Editor (Tier 1 #3) and any future audio subsystem.
        self.audios: AudioManager           = self._make_audio_manager()
        self.id_tags: IdTagManager          = self._make_id_tag_manager()
        self.layout_blocks: LayoutBlockManager = self._make_layout_block_manager()
        self.oblocks: OBlockManager         = self._make_oblock_manager()
        self.power: PowerManager            = PowerManager()
        self._opened: bool = False
        # Command-station reachability. Optimistic default: drivers
        # that don't implement heartbeat detection stay at True while
        # the connection is open, so the base behaviour matches
        # ``is_open``. Drivers with real detection call
        # :meth:`_set_cs_reachable` from their traffic controller to
        # flip this in response to actual bus activity.
        self._cs_reachable: bool = True
        self.command_station_power_changed: Signal[bool] = Signal()

    # ---- lifecycle ---------------------------------------------------------

    @abstractmethod
    async def open(self, settings: ConnectionSettings | None = None) -> None:
        """Connect to hardware and start any IO loops.

        Concrete drivers acquire serial/TCP ports here, install protocol
        handlers, and start any required background tasks. The base
        implementation should be called via ``await super().open(settings)``
        to mark the connection opened.
        """
        self._opened = True

    @abstractmethod
    async def close(self) -> None:
        """Disconnect from hardware. Idempotent. Base impl flips the flag."""
        self._opened = False

    @property
    def is_open(self) -> bool:
        return self._opened

    @property
    def is_command_station_on(self) -> bool:
        """True when the command station itself has power.

        Distinct from :attr:`PowerManager.state` (which is the DCC
        *track* power -- the CS driving DCC on the rails or not).

        Value is ``is_open AND _cs_reachable`` -- so a closed
        connection is always Off, and a driver with real detection
        (NCE, LocoNet, DCC-EX) can drop the reachable flag when it
        loses contact with the CS without touching ``is_open``.
        Drivers that don't implement detection leave ``_cs_reachable``
        at its optimistic True default, so their behaviour matches
        ``is_open`` exactly -- no regression.
        """
        return self._opened and self._cs_reachable

    async def _set_cs_reachable(self, reachable: bool) -> None:
        """Driver hook -- call from your traffic controller when you
        detect a CS-on <-> CS-off transition.

        Debounces: no-op when the state hasn't changed. Emits
        :attr:`command_station_power_changed` with the new
        :attr:`is_command_station_on` value (which folds in the
        ``is_open`` gate) so subscribers always see the effective
        power state.
        """
        if self._cs_reachable == reachable:
            return
        self._cs_reachable = reachable
        await self.command_station_power_changed.emit(self.is_command_station_on)

    # ---- manager construction hooks ---------------------------------------
    # Drivers override these to return their hardware-aware subclasses.

    def _make_turnout_manager(self) -> TurnoutManager:
        return TurnoutManager(self.prefix)

    def _make_sensor_manager(self) -> SensorManager:
        return SensorManager(self.prefix)

    def _make_signal_head_manager(self) -> SignalHeadManager:
        return SignalHeadManager(self.prefix)

    def _make_signal_mast_manager(self) -> SignalMastManager:
        return SignalMastManager(self.prefix)

    def _make_block_manager(self) -> BlockManager:
        return BlockManager(self.prefix)

    def _make_memory_manager(self) -> MemoryManager:
        return MemoryManager(self.prefix)

    def _make_reporter_manager(self) -> ReporterManager:
        return ReporterManager(self.prefix)

    def _make_light_manager(self) -> LightManager:
        return LightManager(self.prefix)

    def _make_route_manager(self) -> RouteManager:
        return RouteManager(self.prefix)

    def _make_throttle_manager(self) -> ThrottleManager:
        return ThrottleManager(self.prefix)

    def _make_programmer_manager(self) -> ProgrammerManager:
        return ProgrammerManager()

    def _make_audio_manager(self) -> AudioManager:
        return AudioManager(self.prefix)

    def _make_id_tag_manager(self) -> IdTagManager:
        return IdTagManager(self.prefix)

    def _make_layout_block_manager(self) -> LayoutBlockManager:
        # JMRI Java uses a fixed "IL" prefix for layout blocks
        # regardless of the driver. We follow that so system-name
        # tables round-trip against real JMRI XML.
        return LayoutBlockManager("IL")

    def _make_oblock_manager(self) -> OBlockManager:
        # JMRI Java uses "O" as the prefix for OBlocks (system names
        # like "OB1"). Fixed regardless of driver.
        return OBlockManager("O")

    def _make_consist_manager(self) -> ConsistManager:
        # Default is the software-fan-out impl. Drivers override to
        # install a hardware-consist subclass (LnConsistManager,
        # NceConsistManager, DccexConsistManager) that talks to the
        # CS's group memory / opcodes. The programmer manager is
        # passed so ``advanced`` (CV19) consists can auto-write on
        # add/remove; drivers that replace ``self.programmers`` in
        # ``open`` must call ``self.consists.rebind_programmers`` too.
        return ConsistManager(self.throttles, self.prefix, self.programmers)

    # ---- diagnostics ------------------------------------------------------

    def __repr__(self) -> str:
        state = "open" if self._opened else "closed"
        return f"<{type(self).__name__} prefix={self.prefix!r} {state}>"
