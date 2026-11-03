"""``NceConnection`` -- the SystemConnection subclass for NCE.

Construct -> ``open(settings)`` -> bean managers and the background poller
tasks are wired and start running. ``close()`` cancels the pollers and
shuts down the traffic controller.

Settings:
    {
        "kind": "loopback" | "serial",
        "command_set": "PH-Pro 2006+" | "PowerCab" | "Smart Booster SB5",
        "aiu_addresses": [1, 2, 3],     # AIUs to poll (default: empty)
        # for serial:
        "port": "COM7",
        "baud": 9600,
        # for loopback:
        "loopback_port": LoopbackPort,
    }
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar

from pmri_core.beans.fast_clock import HardwareClockState
from pmri_core.hal.connection import ConnectionSettings, SystemConnection

from pmri_nce import messages as nce_messages
from pmri_nce.beans import NcePowerManager, NceSensorManager, NceTurnoutManager
from pmri_nce.command_set import COMMAND_SETS, NCE_PH_PRO_2006, NceCommandSet
from pmri_nce.detect import detect_command_set
from pmri_nce.monitors import NceAiuPoller, NceTurnoutMonitor
from pmri_nce.programmer import NceProgrammer, NceProgrammerManager
from pmri_nce.throttle import NceThrottleManager
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import BytePort, LoopbackPort, SerialPort

_log = logging.getLogger(__name__)


class NceConnection(SystemConnection):
    """NCE hardware connection. Prefix ``N``, name ``NCE``."""

    name: ClassVar[str] = "NCE"
    prefix: ClassVar[str] = "N"

    def __init__(self) -> None:
        super().__init__()
        self._port: BytePort | None = None
        self._tc: NceTrafficController | None = None
        self._cmd_set: NceCommandSet | None = None
        self._monitor: NceTurnoutMonitor | None = None
        self._aiu_poller: NceAiuPoller | None = None
        self._programmer: NceProgrammer | None = None
        self._tasks: list[asyncio.Task[None]] = []
        # Fast-clock running state -- inferred from tick advancement across
        # consecutive reads (JMRI does the same). See ``read_hardware_clock``.
        self._last_clock_seconds: int | None = None
        self._last_clock_running: bool = False

    # ----- lifecycle --------------------------------------------------------

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        s = settings or {}
        explicit = s.get("command_set")
        # Convenience: set ``JMRI_NCE_DEBUG=1`` to turn on wire-level
        # DEBUG for the ``pmri_nce`` logger without having to write a
        # separate uvicorn --log-config file. Uvicorn's own --log-level
        # only affects uvicorn's loggers; our module loggers inherit
        # from root, so we need to boost the module logger explicitly.
        # Go through :mod:`pmri_core.env` so the truthy/whitespace
        # semantics match every other JMRI_* var.
        from pmri_core.env import nce_debug  # noqa: PLC0415
        if nce_debug():
            _nce_root = logging.getLogger("pmri_nce")
            _nce_root.setLevel(logging.DEBUG)
            # Make sure there's a handler that will actually print the
            # DEBUG records. Uvicorn installs a root-level handler at
            # startup that emits INFO+; adding a StreamHandler here
            # ensures our DEBUG lines aren't swallowed.
            if not any(
                isinstance(h, logging.StreamHandler)
                for h in _nce_root.handlers
            ):
                h = logging.StreamHandler()
                h.setLevel(logging.DEBUG)
                h.setFormatter(logging.Formatter(
                    "%(levelname)s:%(name)s:%(message)s",
                ))
                _nce_root.addHandler(h)
                _nce_root.propagate = False
            _log.warning("pmri_nce DEBUG logging enabled via JMRI_NCE_DEBUG=1")
        _log.warning(
            "NCE connection opening: kind=%s port=%s command_set=%s",
            s.get("kind"), s.get("port"), explicit or "(default)",
        )
        self._port = await _build_port(s)
        self._tc = NceTrafficController(self._port)

        # Command-set selection:
        #
        # 1. Explicit ``command_set`` setting wins -- production users should
        #    just specify which CS they have.
        # 2. ``auto_detect`` is OPT-IN (default False). The version probe
        #    opcode isn't verified across all firmwares; on some
        #    PowerCab/PowerPro variants it leaves the CS waiting for
        #    additional argument bytes, wedging the serial bus until the CS
        #    is power-cycled. Don't enable auto-detect on production hardware
        #    until the opcode is verified against your specific firmware.
        # 3. Otherwise default to PH-Pro 2006+ (works on the largest set of
        #    deployed NCE command stations).
        if explicit and explicit in COMMAND_SETS:
            self._cmd_set = COMMAND_SETS[explicit]
        elif s.get("auto_detect", False):
            self._cmd_set = await detect_command_set(self._tc)
        else:
            self._cmd_set = NCE_PH_PRO_2006

        # Replace the base in-memory managers with NCE-aware ones.
        nce_turnouts = NceTurnoutManager(self.prefix, tc=self._tc)
        nce_sensors = NceSensorManager(self.prefix)
        self.turnouts = nce_turnouts                      # type: ignore[assignment]
        self.sensors = nce_sensors                        # type: ignore[assignment]
        self.throttles = NceThrottleManager(self.prefix, tc=self._tc)
        self.power = NcePowerManager(self._tc)
        self._programmer = NceProgrammer(self._tc)
        self.programmers = NceProgrammerManager(self._programmer)
        # Replace the base placeholder ``ConsistManager`` (built in
        # ``SystemConnection.__init__`` before drivers had a chance to
        # install hardware-aware managers) with the NCE-aware one. This
        # brings CS-managed (``command_station``) consists to life via
        # 0xA2 sub-commands; software and advanced/CV19 consists still
        # work because the subclass falls through to the base for those
        # kinds. The rebind hooks used elsewhere aren't enough here --
        # the fan-out overrides only live on ``NceConsistManager``.
        from pmri_nce.consist import NceConsistManager  # noqa: PLC0415
        self.consists = NceConsistManager(
            self.throttles, self.prefix, self.programmers, tc=self._tc,
            consist_head_addr=self._cmd_set.consist_head_addr,
            consist_tail_addr=self._cmd_set.consist_tail_addr,
            consist_mid_addr=self._cmd_set.consist_mid_addr,
            consist_mid_entries=self._cmd_set.consist_mid_entries,
        )

        # Start polling tasks (turnout monitor + optional AIU poller).
        # The turnout monitor can be opted out via ``disable_turnout_monitor``
        # -- useful when the memory base for a CS firmware variant isn't
        # known yet, or when the poll is failing for some other reason and
        # the user just wants the rest of the driver to function.
        if not s.get("disable_turnout_monitor", False):
            self._monitor = NceTurnoutMonitor(self._tc, nce_turnouts, self._cmd_set)
            self._tasks.append(
                asyncio.create_task(self._monitor.run(), name="nce-turnout-monitor"),
            )

        aius: list[int] = list(s.get("aiu_addresses", []) or [])
        if aius and self._cmd_set.aiu_polling_supported:
            self._aiu_poller = NceAiuPoller(self._tc, nce_sensors, aius)
            self._tasks.append(asyncio.create_task(self._aiu_poller.run(), name="nce-aiu-poller"))

        # CS reachability monitor. Polls the traffic controller's
        # ``last_success_at`` timestamp once a second; if we haven't
        # heard a completed round-trip in more than 3 seconds, mark
        # the CS unreachable -- the turnout monitor (1.5s cadence) or
        # AIU poller (250ms) would have brought that number forward if
        # the CS were still answering. Any fresh success flips it
        # back on immediately.
        #
        # Start pessimistic: the base class defaults ``_cs_reachable``
        # to True (matches ``is_open`` for drivers with no real
        # detection), but because we DO have detection, we don't yet
        # know if the CS is answering. Home-page renders in the first
        # ~3 s would otherwise show "On" even against a dark CS; the
        # monitor's first True-transition flips the badge live via
        # the existing WebSocket push once we hear from the CS.
        self._cs_reachable = False
        self._tasks.append(asyncio.create_task(
            self._cs_reachability_loop(), name="nce-cs-reachability",
        ))

        await super().open(settings)

    async def _cs_reachability_loop(self) -> None:
        """Flip ``_cs_reachable`` on the 3-second silence rule.

        Runs until cancelled by :meth:`close`. Uses ``time.monotonic()``
        to avoid wall-clock jumps confusing the window.
        """
        import contextlib  # noqa: PLC0415
        try:
            # Give the CS 3 seconds of grace on open -- the first
            # turnout-monitor poll or AIU read is what generates our
            # first ``last_success_at`` value, and it can take a
            # couple of seconds on a slow serial adapter.
            await asyncio.sleep(3.0)
            while True:
                await asyncio.sleep(1.0)
                if self._tc is None:
                    return
                last = self._tc.last_success_at
                if last is None:
                    # No successful round-trip has happened yet. Treat
                    # as "not reachable" so the UI doesn't lie during
                    # a stuck-open serial hang.
                    await self._set_cs_reachable(False)
                    continue
                age = time.monotonic() - last
                await self._set_cs_reachable(age <= 3.0)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                return

    async def close(self) -> None:
        if self._monitor is not None:
            self._monitor.stop()
        if self._aiu_poller is not None:
            self._aiu_poller.stop()
        for t in self._tasks:
            t.cancel()
        import contextlib  # noqa: PLC0415
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self._tc is not None:
            await self._tc.aclose()
            self._tc = None
        self._port = None
        await super().close()

    # ----- accessors --------------------------------------------------------

    @property
    def command_set(self) -> NceCommandSet:
        if self._cmd_set is None:
            raise RuntimeError("connection not open")
        return self._cmd_set

    @property
    def traffic(self) -> NceTrafficController:
        if self._tc is None:
            raise RuntimeError("connection not open")
        return self._tc

    @property
    def programmer(self) -> NceProgrammer:
        if self._programmer is None:
            raise RuntimeError("connection not open")
        return self._programmer

    # ----- hardware clock ---------------------------------------------------

    async def read_hardware_clock(self) -> HardwareClockState:
        """Read the CS's fast-clock state.

        Talks to command-station memory at ``CS_CLOCK_ADDR`` and decodes
        the 16-byte reply into a :class:`HardwareClockState`. In 12h mode
        the CS reports hours 1..12 with an AM/PM flag; we normalise to
        24-hour before returning so the JSON/web layer never has to care.
        """
        cmd = nce_messages.read_clock()
        reply = await self.traffic.send_command(cmd)
        data = reply.data
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug(
                "NCE clock read bytes: %s",
                " ".join(f"{b:02X}" for b in data),
            )
        scale = data[0]
        seconds = data[2] & 0xFF
        minutes = data[3] & 0xFF
        hours_raw = data[4] & 0xFF
        ampm_flag = data[5]              # ASCII 'A' (65) = AM, else PM
        twenty_four = (data[6] & 0xFF) == 1
        # In 12h mode, hours cycle 12,1..11,12,1..11 with AM flipping to PM
        # at each 12. Normalise: AM 12 -> 0, PM 12 -> 12, PM 1..11 -> 13..23.
        if twenty_four:
            hours = hours_raw % 24
        else:
            is_am = ampm_flag == ord("A")
            h = hours_raw % 12               # 12 -> 0
            hours = h if is_am else h + 12
        # Scale of 0 would divide-by-zero; treat as "unknown, don't touch".
        rate = float(250 // scale) if scale else 1.0
        # The CS's 16-byte poll has no clean "running" bit -- byte [1] is a
        # scale-prescaler tick that stays nonzero even when stopped, so
        # decoding it as a run flag reports running=True for a stopped
        # clock. JMRI's ``NceClockControl`` infers running by watching
        # tick advancement across reads instead; we do the same:
        # if this read's seconds byte differs from the last one, the CS
        # advanced -> running. If identical, -> stopped. On the very first
        # read we have no history and fall back to whatever the last
        # explicit start/stop button intent was.
        if self._last_clock_seconds is None:
            running = self._last_clock_running
        else:
            running = seconds != self._last_clock_seconds
        self._last_clock_seconds = seconds
        self._last_clock_running = running
        return HardwareClockState(
            hours=hours,
            minutes=minutes,
            seconds=seconds,
            rate=rate,
            running=running,
            twenty_four_hour=twenty_four,
        )

    async def set_hardware_clock_time(
        self, hours: int, minutes: int, seconds: int = 0,
    ) -> None:
        """Write hh:mm:ss to the CS clock."""
        await self.traffic.send_command(
            nce_messages.write_clock_time(hours, minutes, seconds),
        )

    async def set_hardware_clock_rate(self, rate: float) -> None:
        """Set the CS fast-clock ratio. NCE only stores integer ratios 1..15;
        we round and clamp to that range so the user's float input doesn't
        drop a command."""
        r = max(1, min(15, int(round(rate))))
        await self.traffic.send_command(nce_messages.set_clock_ratio(r))

    async def start_hardware_clock(self) -> None:
        await self.traffic.send_command(nce_messages.start_clock())
        # Mark the run intent so the *next* read (which may sample the
        # seconds byte before the CS has ticked) doesn't wrongly report
        # stopped. Clear the last-seconds cache so tick-advancement
        # inference restarts on the second post-start read.
        self._last_clock_running = True
        self._last_clock_seconds = None

    async def stop_hardware_clock(self) -> None:
        await self.traffic.send_command(nce_messages.stop_clock())
        # Explicit stop overrides tick-advancement inference for the next
        # read; the last-seconds cache stays valid because a stopped clock
        # will read the same seconds byte again.
        self._last_clock_running = False


# ----- transport construction ----------------------------------------------


async def _build_port(settings: dict[str, Any]) -> BytePort:
    kind = settings.get("kind", "loopback")
    if kind == "serial":
        port = settings.get("port")
        if not port:
            raise ValueError("serial transport requires 'port'")
        baud = int(settings.get("baud", 9600))
        return await SerialPort.open(port=port, baud=baud)
    if kind == "loopback":
        loop_port = settings.get("loopback_port")
        if not isinstance(loop_port, LoopbackPort):
            raise ValueError(
                "loopback transport requires 'loopback_port' to be a LoopbackPort"
            )
        return loop_port
    raise ValueError(f"unknown NCE transport kind: {kind!r}")
