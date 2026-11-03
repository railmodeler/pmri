"""Recorded-bus replay smoke tests for LocoNet.

Uses :mod:`pmri_core.testing.bus_replay` -- the JSONL format is
shared with NCE (and any future driver). The workflow:

1. Author a capture with :func:`write_capture` (either synthetically
   from :func:`encode` output, or from real hardware -- someday).
2. Load it with :func:`read_capture`.
3. Feed the ``in`` frames into a :class:`LocoNetTrafficController`'s
   port and let it decode.
4. Assert the resulting typed messages match the frames the capture
   claimed were on the wire.

The ``captures/`` directory below holds hand-authored synthetic
fixtures for now -- enough to prove the framework works. Real
hardware captures replace them piecemeal as they're taken.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from pmri_core.testing.bus_replay import (
    BusFrame,
    read_capture,
    write_capture,
)

from pmri_loconet.messages import (
    LocoNetMessage,
    OpcInputRep,
    OpcLocoAdr,
    OpcSwRep,
    OpcSwReq,
    encode,
)
from pmri_loconet.traffic import LocoNetTrafficController
from pmri_loconet.transport import loopback_pair

CAPTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "captures"


@pytest.fixture(scope="module")
def synthetic_capture(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A synthetic capture built from :func:`encode` output.

    Content: 4 frames covering the workflow types every driver test
    cares about -- throttle acquire, turnout throw, turnout feedback,
    sensor input. Timestamps roughly match a real-time throttle
    session pace.
    """
    frames: list[BusFrame] = [
        BusFrame(t=0.000, direction="out",
                 frame=encode(OpcLocoAdr(address=3601))),
        BusFrame(t=0.050, direction="out",
                 frame=encode(OpcSwReq(address=17, closed=True, output_on=True))),
        BusFrame(t=0.120, direction="in",
                 frame=encode(OpcSwRep(address=17, closed=True, thrown=False))),
        BusFrame(t=0.300, direction="in",
                 frame=encode(OpcInputRep(address=42, active=True))),
    ]
    path = tmp_path_factory.mktemp("captures") / "synthetic_throttle_session.lnrep"
    write_capture(path, frames)
    return path


async def test_replay_decodes_every_in_frame(
    synthetic_capture: pathlib.Path,
) -> None:
    """Every ``in`` frame in the capture must decode into a typed
    message via the standard traffic controller path. Missing
    decoders would silently drop frames on a real bus -- this is the
    canary."""
    frames = read_capture(synthetic_capture)
    a, b = loopback_pair()
    tc = LocoNetTrafficController(a)
    seen: list[LocoNetMessage] = []
    tc.inbound.connect(lambda msg: seen.append(msg))
    await tc.start()
    try:
        # Feed all "in" frames as if the CS emitted them.
        for f in frames:
            if f.direction == "in":
                await b.write_frame(f.frame)
        # Give the read loop a moment to decode.
        for _ in range(40):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.005)
        assert any(isinstance(m, OpcSwRep) for m in seen)
        assert any(isinstance(m, OpcInputRep) for m in seen)
    finally:
        await tc.aclose()


def test_capture_format_roundtrips_via_write_read(
    tmp_path: pathlib.Path,
) -> None:
    """Format-level regression: write a small capture, read it back,
    every field survives. Guards against a JSONL schema change
    silently corrupting saved captures."""
    original = [
        BusFrame(t=0.0, direction="out", frame=b"\x80\x7f"),
        BusFrame(t=0.5, direction="in", frame=b"\xb2\x2a\x60\x0d"),
    ]
    path = tmp_path / "sample.lnrep"
    write_capture(path, original)
    loaded = read_capture(path)
    assert loaded == original


def test_hand_authored_captures_load(tmp_path: pathlib.Path) -> None:
    """Any capture files committed under ``tests/captures/`` must
    parse. Blocks a bad hand-edit from silently failing later when
    a real hardware session is added."""
    if not CAPTURES_DIR.is_dir():
        pytest.skip("no captures dir yet")
    for cap in CAPTURES_DIR.glob("*.lnrep"):
        try:
            frames = read_capture(cap)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"capture {cap.name} failed to load: {e}") from None
        assert frames, f"capture {cap.name} is empty -- did the format break?"
