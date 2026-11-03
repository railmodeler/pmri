"""NCE CV programmer -- ops-mode (main track) and direct-mode (programming track).

Two methods of CV access:

* **Ops mode** (``write_cv_ops`` / ``read_cv_ops``) talks to a specific loco
  on the main track. Reads require RailCom-capable hardware downstream
  (NCE BD20, etc.); without it the read times out.
* **Direct mode** (``write_cv_direct`` / ``read_cv_direct``) is the
  programming-track service mode. The CS pauses normal layout traffic
  while the operation is in progress.

The programmer exposes a "programming in progress" event so the
:class:`~pmri_nce.monitors.NceTurnoutMonitor` can pause its memory polls
during direct-mode sessions (otherwise the CS may queue reads behind the
programming wait).

This module also adapts the NCE-specific programmer to the generic
:class:`pmri_core.beans.programmer.Programmer` ABC via
:class:`NceServiceProgrammer` and :class:`NceOpsProgrammer`, which is
what the :class:`ProgrammerManager` hands out for DecoderPro use.
"""
from __future__ import annotations

import asyncio
import logging

from pmri_core.beans.programmer import (
    Programmer,
    ProgrammerManager,
    ProgrammingMode,
)

from pmri_nce.messages import (
    NceCommandError,
    direct_mode_read_cv,
    direct_mode_write_cv,
    enter_programming_mode,
    exit_programming_mode,
    ops_mode_write_cv,
)
from pmri_nce.traffic import NceTrafficController

_log = logging.getLogger(__name__)


class NceProgrammer:
    """CV access against an NCE command station."""

    def __init__(self, tc: NceTrafficController) -> None:
        self._tc = tc
        self._programming_in_progress = asyncio.Event()

    @property
    def is_programming(self) -> bool:
        """True while a direct-mode session is in flight."""
        return self._programming_in_progress.is_set()

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Wait for any in-progress direct-mode session to finish."""
        if not self._programming_in_progress.is_set():
            return
        # ``wait`` returns True when set; we want to wait until it's clear.
        # asyncio.Event has no direct "wait clear" -- poll briefly.
        deadline = asyncio.get_event_loop().time() + timeout
        while self._programming_in_progress.is_set():
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("programming did not complete in time")
            await asyncio.sleep(0.05)

    # ----- ops mode (Programming On the Main) ----------------------------
    #
    # Rebuilt on the 2026-07-14 audit path: raw NMRA DCC packet wrapped
    # in NCE ``QUEUEn_TMP_CMD`` (opcodes 0x93..0x96). See
    # :func:`pmri_nce.messages.ops_mode_write_cv` for the packet layout.
    # NCE PowerPro has no ops-mode CV read (RailCom is out of scope), so
    # only ``write_cv_ops`` exists -- reads must go via direct mode.

    async def write_cv_ops(
        self, loco_address: int, cv: int, value: int,
        *, long_address: bool = False,
    ) -> None:
        """Ops-mode CV write (POM). Fires a single DCC packet on the main.

        Unlike ``write_cv_direct``, this does *not* pause layout traffic:
        the CS just queues a service-mode packet in-between throttle
        packets. Result reaches the loco during normal operation.

        There's no wire-level "did the decoder accept it?" -- POM is
        inherently unverified without RailCom. The returned ACK confirms
        the CS queued the packet, not that the decoder wrote the CV.
        """
        await self._tc.send_command(
            ops_mode_write_cv(
                loco_address, cv, value, long_address=long_address,
            ),
        )

    # ----- direct mode (programming track) -------------------------------
    #
    # NCE PowerPro's direct-mode CV opcodes (``0xA8`` / ``0xA9``) require
    # the CS to be in *programming mode* -- a persistent state entered
    # via ``ENTER_PROG_CMD`` (0x9E) and exited via ``EXIT_PROG_CMD``
    # (0x9F). Outside programming mode the CS immediately NoAcks the
    # command with ``'0'`` (0x30) *without* running any DCC packets on
    # the track. That looks like an "instant NoAck" on the wire (~ms,
    # not the 2-6 s a real programming attempt takes).
    #
    # Java JMRI's ``NceProgrammer`` wraps every direct-mode read/write
    # with enter/exit. We mirror that here in :meth:`_prog_op` -- the
    # write and read helpers both go through it so exit always happens
    # even when the operation raises.

    async def _prog_op(
        self, op, *, consume_trailer: bool = False,
    ) -> "NceReply":  # noqa: F821
        """Run *op* (an NceCommand) wrapped in ENTER_PROG / EXIT_PROG.

        The enter and exit commands are short (single-byte requests,
        1-byte ACK reply) so the wrapper adds ~10 ms of latency, dwarfed
        by the 2-6 s of any real direct-mode CV op. Best-effort exit:
        if the wrapped op raises, we still send EXIT_PROG so the CS
        returns to normal operation and layout traffic resumes. The
        exit's own reply is swallowed on failure -- we don't want it
        masking the original error.

        *consume_trailer*: for RD_CV_DIRECT (and any future command
        where NCE PowerPro appends a trailing ACK byte after the
        payload), set True so the trailer is eaten before EXIT_PROG
        runs. Without this, the trailer can land inside EXIT_PROG's
        expected-1-byte reply window and cascade into stream
        desynchronization.
        """
        await self._tc.send_command(enter_programming_mode())
        try:
            reply = await self._tc.send_command(op)
            if consume_trailer:
                await self._tc.consume_trailer(max_wait_s=0.1)
            return reply
        finally:
            try:
                await self._tc.send_command(exit_programming_mode())
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "NCE: EXIT_PROG failed after %s: %s", op.name, e,
                )

    async def write_cv_direct(self, cv: int, value: int) -> None:
        """Service-mode write. Pauses layout traffic for ~2-6 s.

        Wraps the write with :meth:`_prog_op` so the CS is in
        programming mode when the ``WRITE_DIR_CV_CMD`` (0xA8) opcode
        arrives -- a hard requirement of NCE PowerPro firmware.
        """
        self._programming_in_progress.set()
        try:
            await self._prog_op(direct_mode_write_cv(cv, value))
        finally:
            self._programming_in_progress.clear()

    async def read_cv_direct(self, cv: int) -> int:
        """Service-mode read. Same caveats as :meth:`write_cv_direct`.

        Wrapped in ENTER_PROG / EXIT_PROG for the same reason as writes.
        Raises :class:`NceCommandError` when the decoder doesn't
        acknowledge -- NCE signals that by sending the single byte
        ``0x30`` (ASCII ``'0'``). JMRI Java's ``NceProgrammer`` treats
        byte ``'0'`` as ``NoAck`` regardless of whether the actual CV
        happens to hold value 48; we mirror that here since the
        alternative (a silent "value 48 always") is worse than the rare
        false negative when a CV genuinely equals 48.

        Firmware quirk on successful reads: NCE PowerPro sends *two*
        bytes back -- the CV value plus a trailing ``!`` ACK. Our
        command spec asks for ``reply_len=1`` so the trailer lands in
        the serial buffer. If the next command's drain catches it,
        fine; if it doesn't (race), the trailer contaminates the next
        reply. We proactively consume it here with a short blocking
        wait, closing that race window.
        """
        self._programming_in_progress.set()
        try:
            reply = await self._prog_op(
                direct_mode_read_cv(cv), consume_trailer=True,
            )
            byte = reply.data[0]
            if byte == 0x30:   # '0' = NCE NoAck sentinel
                # NoAck is a single byte on the wire; the trailer eater
                # in ``_prog_op`` times out silently in that case,
                # which is fine.
                raise NceCommandError(
                    reply.command, b"0",
                    b"decoder did not ack read (no decoder on prog track?)",
                )
            return byte
        except NceCommandError:
            raise
        finally:
            self._programming_in_progress.clear()


# ---- adapters to the pmri-core Programmer ABC ----------------------------


class NceServiceProgrammer(Programmer):
    """Service-mode adapter -- programming-track CV access."""

    def __init__(self, inner: NceProgrammer) -> None:
        self._inner = inner

    @property
    def mode(self) -> ProgrammingMode:
        return ProgrammingMode.SERVICE_DIRECT

    async def read_cv(self, cv: int) -> int:
        return await self._inner.read_cv_direct(cv)

    async def write_cv(self, cv: int, value: int) -> None:
        await self._inner.write_cv_direct(cv, value)


class NceOpsProgrammer(Programmer):
    """Ops-mode adapter -- Programming On the Main for one specific loco.

    Constructed via :meth:`NceProgrammerManager.ops_programmer`; carries
    the target DCC address so callers can use the same ``read_cv`` /
    ``write_cv`` surface as service mode.

    ``read_cv`` raises :class:`NotImplementedError` -- NCE PowerPro has
    no ops-mode CV read opcode (no RailCom). Callers that need a verified
    read must switch to service mode on the programming track.
    """

    def __init__(
        self, inner: NceProgrammer, address: int, *, long_address: bool = False,
    ) -> None:
        self._inner = inner
        self._address = address
        self._long_address = long_address

    @property
    def mode(self) -> ProgrammingMode:
        return ProgrammingMode.OPS_MODE

    @property
    def address(self) -> int:
        return self._address

    @property
    def long_address(self) -> bool:
        return self._long_address

    async def read_cv(self, cv: int) -> int:
        raise NotImplementedError(
            "NCE has no ops-mode CV read (RailCom is not decoded by the "
            "PowerPro command station). Use service mode via "
            "NceServiceProgrammer for a verified read.",
        )

    async def write_cv(self, cv: int, value: int) -> None:
        await self._inner.write_cv_ops(
            self._address, cv, value, long_address=self._long_address,
        )


class NceProgrammerManager(ProgrammerManager):
    """Hands out NCE service and ops programmers backed by one underlying
    :class:`NceProgrammer`."""

    def __init__(self, inner: NceProgrammer) -> None:
        self._inner = inner
        self._service_singleton: NceServiceProgrammer | None = None

    def global_programmer(self) -> NceServiceProgrammer:
        if self._service_singleton is None:
            self._service_singleton = NceServiceProgrammer(self._inner)
        return self._service_singleton

    def ops_programmer(
        self, address: int, *, long_address: bool = False,
    ) -> NceOpsProgrammer:
        """Return an ops-mode programmer bound to *address*.

        Fresh instance per call -- the caller usually owns one per loco
        currently in the batch queue. Cheap: no wire traffic until the
        first ``write_cv``.
        """
        return NceOpsProgrammer(
            self._inner, address, long_address=long_address,
        )

    @property
    def inner(self) -> NceProgrammer:
        """Direct access to the underlying NCE-specific programmer."""
        return self._inner
