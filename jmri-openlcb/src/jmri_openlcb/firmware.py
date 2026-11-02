"""Firmware Upgrade orchestration (S-9.7.4.3, Apr 25 2021).

This standard defines **no new messages** -- it's pure composition
of existing protocols (Memory Configuration + PIP + Stream / Datagram
+ well-known events). :class:`FirmwareUpgradeClient` runs the full
CT-side sequence per sec.5.5:

1. Freeze the Firmware Space (0xEF) -- puts the target in Firmware
   Upgrade state. Per sec.5.5 note, "a Datagram Received OK reply
   message is not always returned to this request" -- we tolerate a
   timeout at this step.
2. Wait for a Node Initialization Complete PCER from the target,
   confirming the state transition.
3. PIP probe the target to check the Stream Protocol bit.
4. Data transfer:
   - **Stream** path if the target advertises Stream Protocol --
     one MemoryClient stream write to Firmware Space at offset 0.
   - **Datagram** path otherwise -- sequential 64-byte
     memory-config writes to Firmware Space at increasing offsets
     (per sec.5.4.2, "exactly 64 bytes of payload in each datagram
     except the last").
5. Unfreeze the Firmware Space -- returns the target to Operating
   state. Again the ack may not arrive.

Error codes (S-9.7.4.3 sec.6.2) are surfaced as :class:`DatagramRejected`
so callers can distinguish transient/permanent per the existing
error taxonomy:

* ``0x2088`` -- Transfer error (checksum failed) -- retryable
* ``0x1088`` -- Firmware incompatible with hardware -- permanent
* ``0x1089`` -- Firmware invalid / corrupted -- permanent
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from jmri_openlcb.datagrams import DatagramRejected
from jmri_openlcb.events import (
    EVENT_INIT_COMPLETE,
    EVENT_INIT_COMPLETE_SIMPLE,
    EventId,
)
from jmri_openlcb.pip import ProtocolFlag

if TYPE_CHECKING:
    from jmri_openlcb.memory import MemoryClient
    from jmri_openlcb.pip import PipClient
    from jmri_openlcb.stream import StreamClient
    from jmri_openlcb.traffic import OlcbTrafficController

_log = logging.getLogger(__name__)


# ---- constants (S-9.7.4.3 sec.6) --------------------------------------------

SPACE_FIRMWARE = 0xEF
"""Memory Space number where firmware upgrade data is written
(sec.6.1)."""

# Error codes per sec.6.2 -- reported by the target as memory-config
# datagram rejection codes or stream abort codes.
ERROR_TRANSFER_CHECKSUM = 0x2088
ERROR_INCOMPATIBLE = 0x1088
ERROR_INVALID = 0x1089

# Datagram-path chunk size per sec.5.4.2 -- "exactly 64 bytes of
# payload in each datagram except the last".
FIRMWARE_DATAGRAM_CHUNK = 64


# ---- exceptions ---------------------------------------------------------


class FirmwareUpgradeError(Exception):
    """Raised when the upgrade flow can't complete for a reason the
    caller should surface -- target didn't reach Init Complete, target
    rejected the transfer with a permanent error code, etc.

    The upstream exception (``DatagramRejected``, ``TimeoutError``)
    is chained via ``__cause__`` so callers can inspect specifics."""


# ---- helpers ------------------------------------------------------------


def _is_init_complete_event(event: EventId) -> bool:
    return event in (EVENT_INIT_COMPLETE, EVENT_INIT_COMPLETE_SIMPLE)


# Progress callback signature: (bytes_sent, total_bytes) -> None.
ProgressCallback = Callable[[int, int], None]


# ---- client -------------------------------------------------------------


class FirmwareUpgradeClient:
    """CT (Configuration Tool) side of the firmware upgrade flow.

    Constructed with the existing MemoryClient + PipClient + traffic
    controller, and optionally a StreamClient for the streaming
    transfer path. When no StreamClient is supplied, only the
    datagram transfer path is available.
    """

    def __init__(
        self,
        memory: "MemoryClient",
        pip: "PipClient",
        tc: "OlcbTrafficController",
        *,
        stream: "StreamClient | None" = None,
    ) -> None:
        self._memory = memory
        self._pip = pip
        self._tc = tc
        self._stream = stream

    async def upgrade(
        self,
        target_alias: int,
        firmware_data: bytes,
        *,
        progress: ProgressCallback | None = None,
        init_complete_timeout: float = 10.0,
        pip_timeout: float = 5.0,
        write_timeout: float = 10.0,
    ) -> None:
        """Push *firmware_data* to *target_alias*. Blocks until the
        transfer either completes and the target is unfrozen, or
        fails somewhere in the sequence (raises
        :class:`FirmwareUpgradeError` in that case, with the
        underlying exception attached).

        *progress* -- optional callback fired with ``(bytes_sent,
        total_bytes)`` after each datagram or every 64 stream bytes.
        Useful for a UI progress bar; must not block or the upgrade
        stalls."""
        if not firmware_data:
            raise ValueError("firmware_data is empty")

        # Arm the Init Complete listener BEFORE sending Freeze so a
        # fast target's PCER can't race the listener install. The
        # listener stays passive until the PCER arrives; if it's
        # already there when we start awaiting the future,
        # ``wait_for`` completes immediately.
        init_complete_fut = self._install_init_listener(target_alias)

        # ---- 1. Freeze Firmware Space ---------------------------------
        # Per sec.5.5 the ACK may or may not come back -- swallow a
        # timeout/rejection here so a strict node still upgrades.
        await self._maybe_ack(
            self._memory.freeze(
                target_alias, SPACE_FIRMWARE, timeout=write_timeout,
            ),
            step="freeze",
        )

        # ---- 2. Wait for Node Initialization Complete -----------------
        try:
            await asyncio.wait_for(
                init_complete_fut, timeout=init_complete_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise FirmwareUpgradeError(
                f"target {target_alias:#05x} did not send Node "
                "Initialization Complete after Freeze -- is the "
                "Firmware Upgrade Protocol supported?",
            ) from exc

        # ---- 3. PIP probe: decide stream vs datagram -------------------
        use_stream = False
        try:
            flags = await self._pip.probe(target_alias, timeout=pip_timeout)
            use_stream = (
                bool(flags & ProtocolFlag.STREAM)
                and self._stream is not None
            )
        except Exception:  # noqa: BLE001
            # PIP probe failure isn't fatal -- fall back to datagram
            # transfer, which every node with the Firmware Upgrade
            # Protocol bit must support.
            _log.info(
                "PIP probe of %#05x failed during firmware upgrade; "
                "falling back to datagram transfer", target_alias,
            )

        # ---- 4. Data transfer ------------------------------------------
        try:
            if use_stream:
                await self._transfer_stream(
                    target_alias, firmware_data, progress,
                )
            else:
                await self._transfer_datagram(
                    target_alias, firmware_data,
                    progress, timeout=write_timeout,
                )
        except DatagramRejected as exc:
            raise FirmwareUpgradeError(
                f"target {target_alias:#05x} rejected firmware "
                f"transfer: {exc.description}",
            ) from exc

        # ---- 5. Unfreeze -- return target to Operating state -----------
        await self._maybe_ack(
            self._memory.unfreeze(
                target_alias, SPACE_FIRMWARE, timeout=write_timeout,
            ),
            step="unfreeze",
        )

    async def _transfer_datagram(
        self,
        target_alias: int,
        firmware_data: bytes,
        progress: ProgressCallback | None,
        *,
        timeout: float,
    ) -> None:
        """sec.5.4.2: sequential memory-config Write commands, exactly
        64 bytes each except the last, offsets 0/64/128/...

        MemoryClient.write already fragments to
        :data:`~jmri_openlcb.memory.MAX_MEMORY_CHUNK` (64), so a
        single write call with the full firmware bytes satisfies
        the spec. We still loop chunk-by-chunk here so progress can
        fire after each on-wire datagram."""
        total = len(firmware_data)
        offset = 0
        while offset < total:
            chunk = firmware_data[offset:offset + FIRMWARE_DATAGRAM_CHUNK]
            await self._memory.write(
                target_alias, SPACE_FIRMWARE, offset, chunk,
                timeout=timeout,
            )
            offset += len(chunk)
            if progress is not None:
                try:
                    progress(offset, total)
                except Exception:
                    _log.exception("progress callback failed")

    async def _transfer_stream(
        self,
        target_alias: int,
        firmware_data: bytes,
        progress: ProgressCallback | None,
    ) -> None:
        """sec.5.4.1: Stream Write to Firmware Space with a starting
        offset of zero. The stream carries the exact firmware bytes,
        unmodified."""
        assert self._stream is not None
        # The Stream Protocol doesn't have a per-frame progress
        # callback (it's an all-or-nothing await); fire a synthetic
        # progress at the start and end.
        total = len(firmware_data)
        if progress is not None:
            try:
                progress(0, total)
            except Exception:
                _log.exception("progress callback failed")
        await self._stream.send(target_alias, firmware_data)
        if progress is not None:
            try:
                progress(total, total)
            except Exception:
                _log.exception("progress callback failed")

    def _install_init_listener(
        self, target_alias: int,
    ) -> asyncio.Future[None]:
        """Arm a passive listener for an init-complete PCER from
        *target_alias*. Returns a future the caller awaits after the
        Freeze upcode goes out. Registered eagerly so a fast target's
        PCER can't beat us to the connect() call."""
        from jmri_openlcb.codec import decode_pcer_if_pcer  # noqa: PLC0415

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        unsubscribe: list[Callable[[], None]] = []

        async def _on_frame(frame) -> None:  # noqa: ANN001
            report = decode_pcer_if_pcer(frame)
            if report is None:
                return
            if (
                report.source_alias == target_alias
                and _is_init_complete_event(report.event)
                and not fut.done()
            ):
                fut.set_result(None)
                unsubscribe[0]()

        unsubscribe.append(self._tc.frames.connect(_on_frame))
        fut.add_done_callback(
            lambda _: unsubscribe[0]() if unsubscribe else None,
        )
        return fut

    @staticmethod
    async def _maybe_ack(
        coro: Awaitable[None], *, step: str,
    ) -> None:
        """Await *coro*; swallow ``TimeoutError`` and
        ``DatagramRejected`` since sec.5.5 warns the ACK may not
        come. Real transport failures propagate."""
        try:
            await coro
        except (asyncio.TimeoutError, DatagramRejected) as e:
            _log.info(
                "firmware %s: ACK not received (%s) -- proceeding",
                step, e,
            )
        # Any other exception propagates -- that's a real transport
        # failure, not the spec's "ack may be missing" case.
