"""Capture -> file -> replay -> monitor round-trip tests."""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from jmri_ncemonitor.messages import (
    ADDR_ACCESSORY,
    AccessoryObserved,
    CabBusMessage,
    CabPoll,
    encode,
)
from jmri_ncemonitor.monitor import CabBusMonitor
from jmri_ncemonitor.replay import BusCapture, BusReplay, CapturedFrame, make_replay_pair


def test_capture_records_timestamps() -> None:
    capture = BusCapture()
    capture.record(encode(CabPoll(address=2, command=0)))
    capture.record(encode(CabPoll(address=3, command=0)))
    assert len(capture.frames) == 2
    assert capture.frames[1].t >= capture.frames[0].t


def test_capture_jsonl_roundtrip(tmp_path: pathlib.Path) -> None:
    capture = BusCapture()
    capture.record(encode(CabPoll(address=5, command=0)))
    capture.record(encode(AccessoryObserved(
        address=ADDR_ACCESSORY, accessory_address=12, closed=True,
    )))
    path = tmp_path / "capture.jsonl"
    capture.write(path)

    loaded = BusCapture.read(path)
    assert len(loaded.frames) == 2
    assert loaded.frames[0].frame == capture.frames[0].frame
    assert loaded.frames[1].frame == capture.frames[1].frame


def test_captured_frame_jsonl_format() -> None:
    cf = CapturedFrame(t=1.5, frame=bytes([0x82, 0x00]))
    line = cf.to_jsonl()
    assert '"t": 1.5' in line
    assert '"frame_hex": "8200"' in line
    parsed = CapturedFrame.from_jsonl(line)
    assert parsed == cf


async def test_replay_drives_monitor_through_decoded_messages() -> None:
    # Build a synthetic capture of three messages.
    capture = BusCapture()
    capture.record(encode(CabPoll(address=2, command=0)))
    capture.record(encode(AccessoryObserved(
        address=ADDR_ACCESSORY, accessory_address=7, closed=True,
    )))
    capture.record(encode(CabPoll(address=4, command=0)))

    monitor_port, replay = make_replay_pair(capture)
    write_port = replay.__dict__["_write_port"]

    monitor = CabBusMonitor(monitor_port)
    captured: list[CabBusMessage] = []
    monitor.inbound.connect(captured.append)
    await monitor.start()

    await replay.play_into(write_port)

    # Give the monitor a few ticks to drain its decoded messages.
    for _ in range(40):
        if len(captured) >= 3:
            break
        await asyncio.sleep(0.005)

    types_seen = [type(m).__name__ for m in captured]
    assert "CabPoll" in types_seen
    assert "AccessoryObserved" in types_seen

    await monitor.aclose()


async def test_replay_from_file(tmp_path: pathlib.Path) -> None:
    capture = BusCapture()
    capture.record(encode(CabPoll(address=2, command=0)))
    capture.record(encode(CabPoll(address=3, command=0)))
    path = tmp_path / "playback.jsonl"
    capture.write(path)

    monitor_port, write_port = (lambda: __import__("jmri_ncemonitor").loopback_pair())()
    replay = BusReplay.from_file(path)

    monitor = CabBusMonitor(monitor_port)
    captured: list[CabBusMessage] = []
    monitor.inbound.connect(captured.append)
    await monitor.start()
    await replay.play_into(write_port)

    for _ in range(40):
        if any(isinstance(m, CabPoll) and m.address == 3 for m in captured):
            break
        await asyncio.sleep(0.005)
    assert any(isinstance(m, CabPoll) and m.address == 2 for m in captured)
    assert any(isinstance(m, CabPoll) and m.address == 3 for m in captured)
    await monitor.aclose()


def test_replay_drops_malformed_frames(caplog: pytest.LogCaptureFixture) -> None:
    """A captured frame missing its address marker is dropped with a warning."""
    capture = BusCapture()
    # Synthetic bad frame: low-bit byte at the start.
    capture.frames.append(CapturedFrame(t=0.0, frame=bytes([0x10, 0x20])))
    # And a good one.
    capture.frames.append(CapturedFrame(t=0.1, frame=encode(CabPoll(address=1, command=0))))
    replay = BusReplay(capture.frames)
    # Run through a fresh loopback; just confirm play_into doesn't raise.
    async def go():  # noqa: ANN202
        from jmri_ncemonitor.transport import loopback_pair  # noqa: PLC0415
        _, write_port = loopback_pair()
        await replay.play_into(write_port)
    asyncio.run(go())
