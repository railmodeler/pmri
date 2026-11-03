"""EX-RAIL automation client surface.

EX-RAIL is DCC-EX's embedded automation DSL -- a script compiled into
the command station's firmware (``myAutomation.h``). The routes and
automations are authored on the firmware side, so a client cannot
upload new ones, but a client CAN:

* introspect the compiled list,
* trigger a specific route or automation at runtime,
* kill a running task,
* set a virtual signal's aspect.

Wire protocol (verified against
https://dcc-ex.com/throttles/tech-reference.html and the DCC-EX
Native Commands Summary):

Introspection (requests + responses):

* ``<JA>``                     -> ``<jA id0 id1 id2 ...>``
                                    list of all defined route +
                                    automation ids.
* ``<JA id>``                  -> ``<jA id R "description">``   (route)
                                    ``<jA id A "description">`` (automation)
                                    ``<jA id X>``               (not found)

Control (fire-and-forget; the CS does not reply):

* ``</ START id>``             start a route (no loco assignment).
* ``</ START cab id>``         start an automation with loco *cab*
                               bound to the task.
* ``</ KILL id>``              kill the task identified by *id*.
* ``</ KILL ALL>``             kill every running task.
* ``</ PAUSE>``                pause every automation (sends E-STOP
                               to every loco under EX-RAIL control).
* ``</ RESUME>``               resume previously paused tasks.
* ``</ RED signalId>``         set virtual signal to RED.
* ``</ AMBER signalId>``       set virtual signal to AMBER.
* ``</ GREEN signalId>``       set virtual signal to GREEN.
* ``</ RESERVE block_id>``     reserve a virtual block (0..255).
* ``</ FREE block_id>``        release a reserved virtual block.
* ``</ LATCH vpin>``           force a sensor ON regardless of input.
* ``</ UNLATCH vpin>``         release the latch; sensor follows input.

Diagnostic (also fire-and-forget):

* ``<D EXRAIL ON>`` / ``<D EXRAIL OFF>``   enable/disable EX-RAIL
                                           sequence logging on the CS.

Note the slash commands use a literal ``/`` as the DCC-EX opcode with
the EX-RAIL verb as the first arg, so e.g.
:class:`~pmri_dccex.codec.DccexMessage` ``("/" , ("START", "7"))``
renders as ``</ START 7>`` on the wire.
"""
from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass

from pmri_dccex.codec import DccexMessage
from pmri_dccex.traffic import DccexTrafficController


class ExRailKind(enum.StrEnum):
    """Kind of EX-RAIL entry: a ``ROUTE`` runs stand-alone while an
    ``AUTOMATION`` binds to a loco cab passed in via ``</START cab id>``.
    Wire-level discriminator from the ``<jA ... R|A ...>`` response."""

    ROUTE = "R"
    AUTOMATION = "A"


class ExRailAspect(enum.StrEnum):
    """EX-RAIL virtual signal aspect. The wire opcode doubles as the
    aspect name (``RED`` / ``AMBER`` / ``GREEN``) in the ``</ ... id>``
    slash command."""

    RED = "RED"
    AMBER = "AMBER"
    GREEN = "GREEN"


@dataclass(frozen=True, slots=True)
class ExRailEntry:
    """One route or automation defined in the command station's firmware.

    ``id`` is the integer identifier the firmware author assigned in
    ``myAutomation.h``. ``description`` is the free-form string the
    firmware emits in the ``<jA>`` detail response -- typically what
    the author wrote in a ``ROUTE(id, "description")`` /
    ``AUTOMATION(id, "description")`` macro.
    """

    id: int
    kind: ExRailKind
    description: str


class DccexExRailManager:
    """Client-side EX-RAIL control bound to a DCC-EX traffic controller.

    Request/response correlation reuses the pattern established by
    :mod:`pmri_dccex.programmer`: subscribe a one-shot listener to
    :attr:`DccexTrafficController.inbound` before sending the request,
    filter responses by opcode + payload shape, resolve the future,
    unsubscribe.

    Control calls (:meth:`start_route`, :meth:`kill`, :meth:`set_signal`,
    ...) are fire-and-forget. DCC-EX does not emit an ack for the
    slash-command verbs, so a successful ``send()`` is the only
    confirmation available at this layer. State changes surface later
    (if at all) via other channels -- the EX-RAIL script itself can
    write to turnouts / signals / memories whose beans are already
    observed.
    """

    def __init__(self, tc: DccexTrafficController) -> None:
        self._tc = tc

    # ---- introspection ----------------------------------------------------

    async def list_ids(self, *, timeout: float = 2.0) -> tuple[int, ...]:
        """Send ``<JA>`` and await the ``<jA id0 id1 ...>`` reply.

        The reply form is distinguished from the ``<JA id>`` detail
        response by having every arg parse as an int (details carry
        an ``R``/``A``/``X`` type token in position 1). Returns the
        parsed id tuple -- empty when the firmware has no EX-RAIL
        script compiled in. Raises :exc:`asyncio.TimeoutError` on
        silence.
        """
        future: asyncio.Future[tuple[int, ...]] = (
            asyncio.get_event_loop().create_future()
        )

        async def listener(msg: DccexMessage) -> None:
            if future.done() or msg.command != "jA":
                return
            try:
                ids = tuple(int(a) for a in msg.args)
            except ValueError:
                # Looks like a <JA id> detail response -- not for us.
                return
            future.set_result(ids)

        disconnect = self._tc.inbound.connect(listener)
        try:
            await self._tc.send(DccexMessage(command="JA", args=()))
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            disconnect()

    async def detail(
        self, id: int, *, timeout: float = 2.0,
    ) -> ExRailEntry | None:
        """Send ``<JA id>`` and await the detail reply.

        Returns :class:`ExRailEntry` on success, ``None`` when the
        firmware answers ``<jA id X>`` (id not defined). Raises
        :exc:`asyncio.TimeoutError` if no matching reply arrives.

        The description payload is quoted by the firmware
        (``<jA 7 R "Yard entry">``); we strip the wrapping quotes
        before returning. Descriptions containing embedded spaces
        reassemble correctly because the codec's whitespace split
        over-tokenises and we rejoin via :meth:`str.join`.
        """
        future: asyncio.Future[ExRailEntry | None] = (
            asyncio.get_event_loop().create_future()
        )

        async def listener(msg: DccexMessage) -> None:
            if future.done() or msg.command != "jA":
                return
            if not msg.args or msg.args[0] != str(id):
                return
            # <jA id X>              -- not found
            # <jA id R|A "desc...">  -- found
            if len(msg.args) == 2 and msg.args[1] == "X":
                future.set_result(None)
                return
            if len(msg.args) >= 2 and msg.args[1] in ("R", "A"):
                kind = ExRailKind(msg.args[1])
                desc = " ".join(msg.args[2:])
                if len(desc) >= 2 and desc.startswith('"') and desc.endswith('"'):
                    desc = desc[1:-1]
                future.set_result(ExRailEntry(id=id, kind=kind, description=desc))

        disconnect = self._tc.inbound.connect(listener)
        try:
            await self._tc.send(DccexMessage(command="JA", args=(str(id),)))
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            disconnect()

    async def refresh(
        self, *, timeout: float = 2.0,
    ) -> tuple[ExRailEntry, ...]:
        """Fetch every id with :meth:`list_ids` then its detail. Returns
        the entries in id-emission order; ids that resolve to ``X``
        (not found -- a race if the firmware is being reflashed) are
        silently dropped."""
        ids = await self.list_ids(timeout=timeout)
        entries: list[ExRailEntry] = []
        for i in ids:
            entry = await self.detail(i, timeout=timeout)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    # ---- control (fire-and-forget) ----------------------------------------

    async def start_route(self, id: int) -> None:
        """Trigger ``</ START id>`` with no cab assignment -- for routes
        (``ROUTE(id, ...)`` in EX-RAIL). An automation macro (``AUTOMATION``)
        started this way runs with no loco bound; use
        :meth:`start_automation` to bind a cab."""
        await self._tc.send(DccexMessage(command="/", args=("START", str(id))))

    async def start_automation(self, id: int, cab: int) -> None:
        """Trigger ``</ START cab id>`` -- the cab-aware form that
        binds loco *cab* to the automation's loco slot. Required for
        automations whose EX-RAIL script references the implicit loco
        (``SPEED``, ``FORWARD``, etc.)."""
        await self._tc.send(DccexMessage(
            command="/", args=("START", str(cab), str(id)),
        ))

    async def kill(self, id: int | str) -> None:
        """Kill task *id*. Pass the literal string ``"ALL"`` (any case)
        to kill every running task. Fire-and-forget: EX-RAIL does not
        ack the kill; if the task was already finished the command is
        a no-op on the firmware side."""
        token = str(id).upper() if isinstance(id, str) else str(id)
        await self._tc.send(DccexMessage(command="/", args=("KILL", token)))

    async def set_signal(
        self, aspect: ExRailAspect, signal_id: int,
    ) -> None:
        """Set virtual signal *signal_id* to *aspect*. Equivalent to
        ``</ RED id>`` / ``</ AMBER id>`` / ``</ GREEN id>``. Fire-and-forget.

        EX-RAIL virtual signals are identified by the "red Vpin" the
        author declared in the ``SIGNAL(redPin, amberPin, greenPin)``
        macro; the aspect verb drives the matching pin high."""
        await self._tc.send(DccexMessage(
            command="/", args=(aspect.value, str(signal_id)),
        ))

    async def pause(self) -> None:
        """``</ PAUSE>`` -- halt every automation. Per the EX-RAIL
        command reference, PAUSE also issues E-STOP to every loco
        currently under EX-RAIL control, so this is more than just a
        scheduler pause -- any script that was commanding speed
        drops its loco to zero. Pair with :meth:`resume` to restart."""
        await self._tc.send(DccexMessage(command="/", args=("PAUSE",)))

    async def resume(self) -> None:
        """``</ RESUME>`` -- resume automations that :meth:`pause`
        suspended. Locos stay at zero speed until their script
        commands them otherwise -- RESUME doesn't restore the pre-pause
        speeds."""
        await self._tc.send(DccexMessage(command="/", args=("RESUME",)))

    async def reserve_block(self, block_id: int) -> None:
        """``</ RESERVE block_id>`` -- mark a virtual block held. Valid
        block ids are 0..255. Used in cooperation with EX-RAIL scripts
        that pattern on ``IF_RESERVE``/``WITH_RESERVE`` guards."""
        await self._tc.send(DccexMessage(
            command="/", args=("RESERVE", str(block_id)),
        ))

    async def free_block(self, block_id: int) -> None:
        """``</ FREE block_id>`` -- release a reserved virtual block
        (0..255). Mirror of :meth:`reserve_block`."""
        await self._tc.send(DccexMessage(
            command="/", args=("FREE", str(block_id)),
        ))

    async def latch(self, vpin: int) -> None:
        """``</ LATCH vpin>`` -- force a sensor ON regardless of its
        physical input. Useful for EX-RAIL scripts that gate on sensor
        state and want to override from a remote console. :meth:`unlatch`
        releases the override."""
        await self._tc.send(DccexMessage(
            command="/", args=("LATCH", str(vpin)),
        ))

    async def unlatch(self, vpin: int) -> None:
        """``</ UNLATCH vpin>`` -- release a :meth:`latch` so the sensor
        follows its physical input again."""
        await self._tc.send(DccexMessage(
            command="/", args=("UNLATCH", str(vpin)),
        ))

    async def set_logging(self, on: bool) -> None:
        """``<D EXRAIL ON|OFF>`` -- toggle EX-RAIL sequence logging on
        the CS console. Lines appear on the CS's diagnostic stream and
        on inbound traffic as ``<* ... *>`` (DCC-EX diagnostic form)
        if the CS is configured to echo diagnostics."""
        await self._tc.send(DccexMessage(
            command="D", args=("EXRAIL", "ON" if on else "OFF"),
        ))
