"""``NceTrafficController`` -- sync request/response over a byte stream.

NCE is single-master: the PC sends a command, the command station replies
with ``reply_len`` data bytes followed by one ACK byte. Multiple commands
are serialized through one :class:`asyncio.Lock` because the command station
processes them one at a time.

This is the structural opposite of :mod:`pmri_loconet.traffic`, which has
no concept of "in flight" -- every LocoNet frame is broadcast and any
listener may decode it. The :class:`SystemConnection` ABC abstracts both
shapes without forcing either onto the other; that this driver dropped in
without changes to ``pmri-core`` is the validation that the abstraction
holds.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pmri_nce.messages import NceCommand, NceCommandError, NceReply
from pmri_nce.opcodes import ACK_ERROR
from pmri_nce.transport.port import BytePort

_log = logging.getLogger(__name__)


class NceTrafficController:
    """One command in flight at a time. All sends serialize through ``_lock``."""

    # Fast-fail behaviour: once the CS has been silent for more than
    # ``SILENCE_THRESHOLD_S`` (matching the reachability monitor's
    # window plus a small margin so the two don't fight for control),
    # ``send_command`` refuses to touch the serial port and raises
    # ``TimeoutError`` immediately. Otherwise the browser's 1-Hz
    # fast-clock polls + the turnout monitor's 1.5 s poll cadence
    # queue up behind the ``_lock`` and each pays the full 500 ms
    # timeout -- a home-page render or profile-load can stall for
    # 5-10 seconds even though we already know the CS is off.
    #
    # Every ``PROBE_INTERVAL_S`` one command IS let through to probe
    # whether the CS has come back. On success the timestamp gets
    # updated, silence drops below threshold, and normal operation
    # resumes.
    SILENCE_THRESHOLD_S = 4.0
    PROBE_INTERVAL_S = 5.0

    def __init__(self, port: BytePort) -> None:
        self._port = port
        self._lock = asyncio.Lock()
        self._closed = False
        # Timestamp (``time.monotonic()`` scale) of the most recent
        # completed round-trip with the CS. ``None`` before any
        # command has been sent. Read by :class:`NceConnection`'s
        # reachability monitor to flip the CS-power state; write-only
        # here -- updates on every success in :meth:`send_command` and
        # in :meth:`consume_trailer` when the trailer arrives.
        self.last_success_at: float | None = None
        # Fallback reference for the fast-fail silence calculation.
        # If the CS has been off since server start, ``last_success_at``
        # is always ``None`` and would defeat the short-circuit --
        # every command would still pay its own serial timeout. Using
        # ``max(start_time, last_success_at)`` means an always-off CS
        # is detected the moment startup grace elapses, and a CS that
        # went off after some traffic is measured from its last reply.
        self._start_time: float = time.monotonic()
        # Next moment at which a probe attempt is allowed while the CS
        # is known silent. See the class-level fast-fail comment.
        self._next_probe_at: float | None = None

    async def aclose(self) -> None:
        self._closed = True
        await self._port.aclose()

    async def send_command(self, cmd: NceCommand) -> NceReply:
        """Write *cmd*, await its reply with timeout, return the reply data.

        Raises:
          - :class:`asyncio.TimeoutError` if the command station doesn't
            answer within ``cmd.timeout_s``;
          - :class:`NceCommandError` if the ACK byte indicates failure.
        """
        if self._closed:
            raise RuntimeError("traffic controller is closed")
        # Fast-fail if the CS has been silent long enough that the
        # reachability monitor has already flipped it to unreachable.
        # Without this, a browser page-load can queue ~10 commands (fast
        # clock + turnout monitor + user actions) that each pay 500 ms
        # of serial timeout in series -- 5-9 s of hang on GET / and
        # POST /profiles/load. Let one probe through every
        # ``PROBE_INTERVAL_S`` so recovery is detected.
        #
        # ``reference`` is the later of first-open and last successful
        # round-trip. If the CS has been off since startup,
        # ``last_success_at`` is ``None`` -- measuring silence from
        # ``_start_time`` still trips the short-circuit once startup
        # grace elapses.
        now = time.monotonic()
        reference = (
            self.last_success_at
            if self.last_success_at is not None
            else self._start_time
        )
        silence = now - reference
        if silence > self.SILENCE_THRESHOLD_S:
            if self._next_probe_at is not None and now < self._next_probe_at:
                raise TimeoutError(
                    f"NCE CS silent for {silence:.1f}s; "
                    f"{cmd.name} short-circuited"
                )
            # Let this one through as a probe; re-arm for the next
            # window so only one attempt hits serial per interval.
            self._next_probe_at = now + self.PROBE_INTERVAL_S
        async with self._lock:
            # Drain any bytes the CS left in the read buffer before the
            # request. Without this, leftover bytes -- CS startup chatter,
            # or extras the CS sends after a documented reply -- are read
            # as the *current* command's ACK on the next send. (Action
            # commands then fail with random non-``!`` ACKs.)
            await self._drain_pending()
            _log.debug("NCE send %s: %s", cmd.name, cmd.request.hex())
            await self._port.write(cmd.request)
            # Read commands return exactly ``reply_len`` data bytes (no ACK).
            # Action commands return ``reply_len`` data bytes + 1 ACK byte.
            n = cmd.reply_len if cmd.is_read else cmd.reply_len + 1
            try:
                full = await asyncio.wait_for(
                    self._port.read_exact(n),
                    timeout=cmd.timeout_s,
                )
            except TimeoutError:
                _log.warning("NCE command %s timed out", cmd.name)
                raise
            _log.debug("NCE recv %s: %s", cmd.name, full.hex())
            # Any completed round-trip counts as evidence the CS is on.
            self.last_success_at = time.monotonic()
            if cmd.is_read:
                return NceReply(command=cmd, data=full)
            data = full[: cmd.reply_len]
            ack = full[cmd.reply_len : cmd.reply_len + 1]
            # NCE's reply byte semantics vary by command and firmware: some
            # commands return ``!`` (ACK_OK), others echo the opcode or
            # respond with a status byte that's command-specific. JMRI Java
            # treats action commands as fire-and-forget (null listener,
            # no value check). The one signal we *do* care about is the
            # explicit-NACK byte ``b'0'`` (ACK_ERROR) -- anything else is
            # accepted as success.
            if ack == ACK_ERROR:
                raise NceCommandError(cmd, ack, data)
            return NceReply(command=cmd, data=data)

    async def _drain_pending(self) -> None:
        """Discard any bytes already sitting in the read buffer.

        Non-blocking: returns immediately if the buffer is empty. Avoids
        the ~5ms-per-command timeout penalty of a polling loop.
        """
        stray = await self._port.read_pending()
        if stray:
            _log.debug("NCE drained %d stale byte(s): %s", len(stray), stray.hex())

    async def consume_trailer(self, max_wait_s: float = 0.1) -> bytes:
        """Wait briefly for a trailing byte and consume it if it arrives.

        NCE PowerPro firmware quirk: some commands (notably
        RD_CV_DIRECT on successful decoder ack) send TWO bytes back --
        the payload byte plus a trailing ``!`` ACK. Our command spec
        for RD_CV_DIRECT expects ``reply_len=1``, so the second byte
        lands in the serial buffer. Usually the next command's drain
        catches it, but occasionally it arrives *after* the drain
        window closes but *before* the next command's expected reply
        -- corrupting the reply as though we'd shifted by one byte.

        Callers who know their command has an optional trailer should
        call this immediately after :meth:`send_command`. Holds no
        lock (the caller's ``send_command`` call already released it),
        which is fine because we're only READING from the port, not
        writing -- a concurrent write can't interleave.

        Returns the trailer byte if seen, empty bytes otherwise.
        """
        try:
            trailer = await asyncio.wait_for(
                self._port.read_exact(1),
                timeout=max_wait_s,
            )
        except TimeoutError:
            return b""
        _log.debug("NCE consumed trailer byte: %02x", trailer[0])
        return trailer
