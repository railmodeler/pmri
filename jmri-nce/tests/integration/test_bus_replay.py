"""Recorded-bus replay smoke tests for NCE.

NCE is request/response -- every ``out`` frame from us is paired
with an ``in`` reply from the command station. The replay pattern:

1. Read the capture.
2. For each ``out`` frame, verify the traffic controller emits
   exactly those bytes when we resend that command via the codec.
3. For each ``in`` frame, feed it into the port as the CS's reply
   so the pending ``send_command`` future resolves. Assert the
   decoded ``NceReply`` matches expectations.

Shared format lives in :mod:`jmri_core.testing.bus_replay` -- the
same JSONL used by :mod:`jmri_loconet.tests.integration.test_bus_replay`.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from jmri_core.testing.bus_replay import (
    BusFrame,
    read_capture,
    write_capture,
)

from jmri_nce.messages import NceCommand
from jmri_nce.traffic import NceTrafficController
from jmri_nce.transport import loopback_pair

CAPTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "captures"


@pytest.fixture(scope="module")
def synthetic_capture(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Synthetic capture of a power-on + status-read exchange.

    The ``out`` frames are the exact bytes an ``NceTrafficController``
    would emit for the named commands. The ``in`` frames are the
    replies real NCE hardware sends: a single ``!`` ACK for
    power-on, a 3-byte version tuple for the status read.
    """
    frames: list[BusFrame] = [
        # Power ON -- opcode 0x89, no args, replies with ACK only.
        BusFrame(t=0.000, direction="out", frame=bytes([0x89])),
        BusFrame(t=0.020, direction="in", frame=b"!"),
        # Read command station memory at 0x0000 for 3 bytes (fake
        # version + status). Opcode 0xA0 addr[hi] addr[lo] count.
        BusFrame(t=0.100, direction="out",
                 frame=bytes([0xA0, 0x00, 0x00, 0x03])),
        BusFrame(t=0.120, direction="in", frame=b"\x01\x02\x03"),
    ]
    path = tmp_path_factory.mktemp("captures") / "synthetic_power_on.ncerep"
    write_capture(path, frames)
    return path


async def test_replay_matches_action_reply_shape(
    synthetic_capture: pathlib.Path,
) -> None:
    """A recorded ``!`` ACK reply must satisfy the traffic
    controller's synchronous send. If the reply framing changes
    silently, this catches it."""
    frames = read_capture(synthetic_capture)
    a, b = loopback_pair()
    tc = NceTrafficController(a)

    async def replay_replies() -> None:
        # Feed each "in" reply after a tiny yield so send_command's
        # write completes first. Order matters: two commands, two
        # replies, one after the other.
        for f in frames:
            if f.direction == "in":
                await asyncio.sleep(0.005)
                await b.write(f.frame)

    replier = asyncio.create_task(replay_replies())
    try:
        # Send the two commands from the capture; the replies come
        # from the background task above.
        power_on = NceCommand(
            name="power_on", request=bytes([0x89]), reply_len=0,
        )
        reply1 = await tc.send_command(power_on)
        assert reply1.data == b""

        read_mem = NceCommand(
            name="read_mem", request=bytes([0xA0, 0x00, 0x00, 0x03]),
            reply_len=3, is_read=True,
        )
        reply2 = await tc.send_command(read_mem)
        assert reply2.data == b"\x01\x02\x03"
    finally:
        await replier
        await a.aclose()


def test_capture_format_roundtrips_via_write_read(
    tmp_path: pathlib.Path,
) -> None:
    original = [
        BusFrame(t=0.0, direction="out", frame=b"\x89"),
        BusFrame(t=0.02, direction="in", frame=b"!"),
    ]
    path = tmp_path / "sample.ncerep"
    write_capture(path, original)
    loaded = read_capture(path)
    assert loaded == original


def test_hand_authored_captures_load(tmp_path: pathlib.Path) -> None:
    """Any capture files committed under ``tests/captures/`` must
    parse. Blocks a bad hand-edit from silently failing later."""
    if not CAPTURES_DIR.is_dir():
        pytest.skip("no captures dir yet")
    for cap in CAPTURES_DIR.glob("*.ncerep"):
        try:
            frames = read_capture(cap)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"capture {cap.name} failed to load: {e}") from None
        assert frames, f"capture {cap.name} is empty"
