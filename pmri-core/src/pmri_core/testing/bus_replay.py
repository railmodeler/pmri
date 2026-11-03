"""Recorded-bus replay format shared by driver test suites.

The Stage-0 scoping doc called for "capture real LocoNet traffic
with wireshark/JMRI's monitor, replay against the Python TC, assert
state matches." This module ships the on-disk file shape both the
LocoNet and NCE test suites use.

Wire shape (JSONL -- one JSON object per line)::

    {"t": 0.000000, "dir": "in",  "frame_hex": "e703bf..."}
    {"t": 0.043500, "dir": "out", "frame_hex": "8000..."}

Fields:

* ``t`` -- monotonic timestamp in seconds, relative to the first frame.
* ``dir`` -- ``"in"`` = from command station to us, ``"out"`` = from us
  to CS. Direction matters because a driver's assertion order depends
  on which side spoke first.
* ``frame_hex`` -- the frame bytes as hex, including any framing bytes
  (length prefix, address byte). No checksum stripping here -- the
  wire form is the canonical form.

The format is line-oriented and plain-text on purpose: easy to diff,
grep, and hand-edit into synthetic fixtures when you don't have real
traffic to work with. Trailing whitespace and blank lines are
ignored on read; comments (``#`` at column 0) are permitted so
test-authors can annotate captures.

Chose to live under ``pmri_core.testing`` rather than
``tests/_replay.py`` in each driver package because the format is
one shared contract -- a divergent LocoNet-only shape would defeat
the point of the driver-agnostic capture tooling that would use
this.
"""
from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal

Direction = Literal["in", "out"]


@dataclass(frozen=True, slots=True)
class BusFrame:
    """One captured frame."""

    t: float
    direction: Direction
    frame: bytes

    def to_jsonl(self) -> str:
        return json.dumps({
            "t": round(self.t, 6),
            "dir": self.direction,
            "frame_hex": self.frame.hex(),
        })

    @classmethod
    def from_jsonl(cls, line: str) -> BusFrame:
        obj = json.loads(line)
        direction = obj.get("dir", "in")
        if direction not in ("in", "out"):
            raise ValueError(
                f"invalid direction {direction!r}; must be 'in' or 'out'"
            )
        return cls(
            t=float(obj["t"]),
            direction=direction,   # type: ignore[arg-type]
            frame=bytes.fromhex(obj["frame_hex"]),
        )


def write_capture(path: str | pathlib.Path, frames: Iterable[BusFrame]) -> None:
    """Serialize *frames* to *path* as JSONL. Overwrites."""
    p = pathlib.Path(path)
    with p.open("w", encoding="utf-8") as f:
        for frame in frames:
            f.write(frame.to_jsonl() + "\n")


def read_capture(path: str | pathlib.Path) -> list[BusFrame]:
    """Load a capture. Blank lines and ``#``-prefixed comments are
    silently skipped so hand-authored fixtures can carry notes."""
    p = pathlib.Path(path)
    out: list[BusFrame] = []
    with p.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(BusFrame.from_jsonl(line))
    return out


def iter_direction(
    frames: Iterable[BusFrame], direction: Direction,
) -> Iterator[BusFrame]:
    """Filter capture frames by direction. Handy when a test only
    cares about one side of the conversation (e.g. "assert every
    ``in`` frame decodes cleanly")."""
    for f in frames:
        if f.direction == direction:
            yield f
