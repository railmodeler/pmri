"""Capture observed Cab Bus traffic to a file and replay it back.

Capture format is JSON Lines -- one JSON object per observed message:

    {"t": 12345.678, "frame_hex": "806c..."}

* ``t`` -- monotonic timestamp in seconds (relative to capture start).
* ``frame_hex`` -- the raw frame bytes (including the address byte) as
  hex; replay reconstructs the bytes verbatim.

The format is line-oriented + plain-text so it's easy to diff, grep,
and edit by hand for test fixtures. :class:`BusReplay` reads such a file
and feeds the bytes into a :class:`LoopbackPort` so the same
:class:`CabBusMonitor` decode pipeline runs unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from pmri_ncemonitor.codec import is_address_byte
from pmri_ncemonitor.transport import LoopbackPort, loopback_pair

_log = logging.getLogger(__name__)


@dataclass
class CapturedFrame:
    """One captured frame: timestamp + raw bytes."""

    t: float
    frame: bytes

    def to_jsonl(self) -> str:
        return json.dumps({"t": round(self.t, 6), "frame_hex": self.frame.hex()})

    @classmethod
    def from_jsonl(cls, line: str) -> CapturedFrame:
        obj = json.loads(line)
        return cls(t=float(obj["t"]), frame=bytes.fromhex(obj["frame_hex"]))


@dataclass
class BusCapture:
    """Accumulate observed frames; write them out as JSONL on demand."""

    frames: list[CapturedFrame] = field(default_factory=list)
    _started_at: float = field(default_factory=time.monotonic)

    def record(self, frame: bytes) -> None:
        """Record one decoded frame (raw bytes including the address byte)."""
        self.frames.append(CapturedFrame(
            t=time.monotonic() - self._started_at,
            frame=frame,
        ))

    def write(self, path: str | pathlib.Path) -> None:
        """Persist the capture as JSONL."""
        p = pathlib.Path(path)
        with p.open("w", encoding="utf-8") as f:
            for cf in self.frames:
                f.write(cf.to_jsonl() + "\n")

    @classmethod
    def read(cls, path: str | pathlib.Path) -> BusCapture:
        """Load a previously-written capture."""
        p = pathlib.Path(path)
        out = cls()
        out._started_at = 0.0  # consumed; only frame timestamps matter
        with p.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                out.frames.append(CapturedFrame.from_jsonl(line))
        return out


class BusReplay:
    """Feed a captured stream into a :class:`LoopbackPort` for testing.

    Construct with ``BusReplay(frames)`` (or via :meth:`from_file`); call
    :meth:`run` to start a background task that writes frames to the
    *write* side of a loopback pair. The *read* side is returned so callers
    can hand it to a :class:`CabBusMonitor`.

    Timing modes:

    * ``preserve_timing=False`` (default) -- emit all frames immediately,
      one after the other. Good for unit tests.
    * ``preserve_timing=True`` -- sleep between frames according to the
      captured timestamps. Useful when validating real-time behaviour.

    The replay always appends a trailing address byte after the last
    frame so the monitor's "read until next address" loop completes.
    """

    def __init__(
        self,
        frames: Iterable[CapturedFrame],
        *,
        preserve_timing: bool = False,
    ) -> None:
        self._frames = list(frames)
        self._preserve_timing = preserve_timing
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_file(
        cls, path: str | pathlib.Path, *, preserve_timing: bool = False
    ) -> BusReplay:
        capture = BusCapture.read(path)
        return cls(capture.frames, preserve_timing=preserve_timing)

    async def play_into(self, write_port: LoopbackPort) -> None:
        """Write every captured frame into *write_port*."""
        last_t: float | None = None
        for cf in self._frames:
            if self._preserve_timing and last_t is not None:
                delay = cf.t - last_t
                if delay > 0:
                    await asyncio.sleep(delay)
            last_t = cf.t
            if not cf.frame or not is_address_byte(cf.frame[0]):
                _log.warning("dropping malformed captured frame: %s", cf.frame.hex())
                continue
            await write_port.write(cf.frame)
        # Tail boundary byte so the monitor's "read until next addr" returns.
        await write_port.write(bytes([0x80]))

    def start(self, write_port: LoopbackPort) -> asyncio.Task[None]:
        """Kick off the replay as a background task. Returns the task."""
        self._task = asyncio.create_task(self.play_into(write_port), name="cabbus-replay")
        return self._task


def make_replay_pair(
    capture: BusCapture, *, preserve_timing: bool = False
) -> tuple[LoopbackPort, BusReplay]:
    """Convenience helper: return ``(monitor_port, replay)``.

    The caller passes ``monitor_port`` to :class:`CabBusMonitor` and calls
    ``replay.start(write_port)`` where ``write_port`` is the other half of
    a fresh ``loopback_pair()``.
    """
    monitor_port, write_port = loopback_pair()
    replay = BusReplay(capture.frames, preserve_timing=preserve_timing)
    # Stash write_port on the replay so the test can drive it.
    replay.__dict__["_write_port"] = write_port
    return monitor_port, replay
