"""GridConnect ASCII codec for CAN-over-text.

OpenLCB messages travel as CAN 2.0B extended frames; many bridges convert
them to GridConnect ASCII for serial/TCP transport:

    :X<8-hex-digit header>N<0..16 hex digit data>;

Header layout (29-bit CAN ID), per OpenLCB Standard S-9.7.0.1 sec.3.1::

    bit  28  27..24        23..12          11..0
         1   Frame Type    Variable Field  Source Alias

The 4-bit Frame Type decomposes as ``bit 27`` (the "OpenLCB frame"
flag) plus a 3-bit sub-type. The named nibbles we care about:

* ``0x0`` -- CAN control frame (bit 27=0). Variable field distinguishes:
  ``0x700`` = RID, ``0x701`` = AMD, ``0x702`` = AME, ``0x703`` = AMR.
* ``0x4..0x7`` -- CID 4, 3, 2, 1 (bit 27=0). Variable field carries a
  12-bit slice of the 48-bit Node ID.
* ``0x9`` -- Global/Addressed OpenLCB message with 12-bit MTI (bit 27=1).
  PCER lives here with MTI = ``0x5B4``.
* ``0xA..0xD`` -- Datagram frames (only / first / middle / last).
* ``0xF`` -- Stream.

The lower 12 bits of the header are always the **source node alias**.

MTIs we care about at the codec layer:

  0x5B4 = Producer/Consumer Event Report (PCER) -- the all-important
          event-emit message.

Other MTIs (verify node, identify producers/consumers, datagrams) are
preserved as :class:`OlcbUnknown` so the monitor sees them but the
driver doesn't act on them.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pmri_openlcb.events import EventId, EventReport

# ---- frame-type nibbles (bits 27..24 of the CAN header) -------------------

FT_CONTROL = 0x0     # variable field = 0x700 (RID) / 0x701 (AMD)
                     # / 0x702 (AME) / 0x703 (AMR)
FT_CID_1 = 0x7       # variable field = NID bits 47..36
FT_CID_2 = 0x6       # variable field = NID bits 35..24
FT_CID_3 = 0x5       # variable field = NID bits 23..12
FT_CID_4 = 0x4       # variable field = NID bits 11..0
FT_MTI = 0x9         # variable field = 12-bit MTI (OpenLCB data frame)
FT_DATAGRAM_ONLY = 0xA
FT_DATAGRAM_FIRST = 0xB
FT_DATAGRAM_MIDDLE = 0xC
FT_DATAGRAM_LAST = 0xD
FT_STREAM = 0xF

# ---- CAN control-frame variable-field constants ---------------------------

CTRL_RID = 0x700     # Reserved ID (this node claims the alias)
CTRL_AMD = 0x701     # Alias Map Definition (announces alias <-> NID)
CTRL_AME = 0x702     # Alias Map Enquiry (asks who has an alias, or all)
CTRL_AMR = 0x703     # Alias Map Reset (releasing alias)

# MTI constant for Producer/Consumer Event Report.
MTI_PCER = 0x5B4


@dataclass(frozen=True, slots=True)
class OlcbFrame:
    """One GridConnect frame, before MTI-level decoding."""

    header: int       # 29-bit extended CAN ID
    data: bytes       # 0..8 bytes


@dataclass(frozen=True, slots=True)
class OlcbUnknown:
    """A well-formed CAN frame whose MTI we don't have a typed wrapper for."""

    header: int
    data: bytes


def encode_frame(frame: OlcbFrame) -> bytes:
    """Encode an :class:`OlcbFrame` to GridConnect ASCII (with trailing newline)."""
    body = f":X{frame.header:08X}N{frame.data.hex().upper()};"
    return body.encode("ascii")


def decode_frame(text: str) -> OlcbFrame:
    """Parse one GridConnect message into an :class:`OlcbFrame`.

    Accepts either ``:X<hex>N<hex>;`` (newer LCC format) or ``:X<hex>;`` for
    data-less frames.
    """
    text = text.strip()
    if not text.startswith(":X") or not text.endswith(";"):
        raise ValueError(f"not a GridConnect frame: {text!r}")
    body = text[2:-1]
    if "N" in body:
        head_hex, _, data_hex = body.partition("N")
    else:
        head_hex, data_hex = body, ""
    try:
        header = int(head_hex, 16)
        data = bytes.fromhex(data_hex) if data_hex else b""
    except ValueError as e:
        raise ValueError(f"invalid GridConnect frame {text!r}: {e}") from None
    return OlcbFrame(header=header, data=data)


def extract_mti(header: int) -> int:
    """Pull the 12-bit MTI out of an OpenLCB data-frame header (bits
    12..23). Only meaningful when :func:`extract_frame_type` returns
    :data:`FT_MTI` -- for other frame types the same bits carry a
    different variable-field value."""
    return (header >> 12) & 0xFFF


def extract_source_alias(header: int) -> int:
    """Pull the 12-bit source node alias from the header low bits."""
    return header & 0x0FFF


def extract_frame_type(header: int) -> int:
    """Pull the 4-bit Frame Type nibble from the CAN header (bits
    24..27). Match against the ``FT_*`` constants to route a frame."""
    return (header >> 24) & 0xF


def extract_variable_field(header: int) -> int:
    """Pull the 12-bit variable field (bits 12..23). Same bits as the
    MTI; the name is a hint about intent for control/CID frames."""
    return (header >> 12) & 0xFFF


def _pack_header(
    frame_type: int, variable_field: int, source_alias: int,
) -> int:
    """Assemble a 29-bit CAN header from its three OpenLCB fields.
    Bit 28 (extended-frame) is always set."""
    if not 0 <= source_alias <= 0xFFF:
        raise ValueError(f"source alias must be 12-bit, got {source_alias:#x}")
    if not 0 <= frame_type <= 0xF:
        raise ValueError(f"frame type must be 4-bit, got {frame_type:#x}")
    if not 0 <= variable_field <= 0xFFF:
        raise ValueError(
            f"variable field must be 12-bit, got {variable_field:#x}"
        )
    return (
        (1 << 28)
        | ((frame_type & 0xF) << 24)
        | ((variable_field & 0xFFF) << 12)
        | (source_alias & 0xFFF)
    )


def make_pcer_header(source_alias: int) -> int:
    """Compose the 29-bit CAN header for a PCER frame from *source_alias*.

    Emits ``0x195B4XXX`` -- bit 28 (extended) + bit 27 (OpenLCB) +
    frame type 001 (Global/Addressed) + MTI 0x5B4 (PCER) + alias.
    The historical shape (missing bit 27) was silently invalid on
    the wire; real nodes would classify it as a CAN control frame
    with an unknown variable field."""
    return _pack_header(FT_MTI, MTI_PCER, source_alias)


def make_cid_header(
    seq: int, node_id_slice: int, source_alias: int,
) -> int:
    """Compose a CID (Check ID) frame header. *seq* is 1..4 selecting
    which 12-bit slice of the 48-bit Node ID is carried in the
    variable field:

    * CID 1 (frame type 0x7): NID bits 47..36
    * CID 2 (frame type 0x6): NID bits 35..24
    * CID 3 (frame type 0x5): NID bits 23..12
    * CID 4 (frame type 0x4): NID bits 11..0
    """
    seq_to_ft = {1: FT_CID_1, 2: FT_CID_2, 3: FT_CID_3, 4: FT_CID_4}
    if seq not in seq_to_ft:
        raise ValueError(f"CID seq must be 1..4, got {seq}")
    return _pack_header(seq_to_ft[seq], node_id_slice & 0xFFF, source_alias)


def make_rid_header(source_alias: int) -> int:
    """Reserve ID -- declares that *source_alias* is now taken by
    this node. Sent at the end of a successful alias claim."""
    return _pack_header(FT_CONTROL, CTRL_RID, source_alias)


def make_amd_header(source_alias: int) -> int:
    """Alias Map Definition -- announces the alias<->NID mapping. Sent
    after RID as the second half of the claim, and again in reply to
    any AME probe from another node."""
    return _pack_header(FT_CONTROL, CTRL_AMD, source_alias)


def make_ame_header(source_alias: int) -> int:
    """Alias Map Enquiry -- asks nodes to publish their AMD. With
    an empty data field the probe is global; with a 6-byte NID it
    targets a specific node."""
    return _pack_header(FT_CONTROL, CTRL_AME, source_alias)


def make_amr_header(source_alias: int) -> int:
    """Alias Map Reset -- releases *source_alias*. Sent on shutdown
    (rarely; the bus recovers even if a node vanishes)."""
    return _pack_header(FT_CONTROL, CTRL_AMR, source_alias)


def is_cid(header: int) -> bool:
    """True iff *header* is a CID 1..4 frame (frame type 0x4..0x7)."""
    ft = extract_frame_type(header)
    return FT_CID_4 <= ft <= FT_CID_1


def cid_seq(header: int) -> int:
    """Return the CID sequence number (1..4) for a CID header. Only
    valid when :func:`is_cid` is True."""
    ft = extract_frame_type(header)
    return {FT_CID_1: 1, FT_CID_2: 2, FT_CID_3: 3, FT_CID_4: 4}[ft]


def is_control(header: int, variable_field: int) -> bool:
    """True iff *header* is a CAN control frame whose variable field
    matches *variable_field* (e.g. :data:`CTRL_RID`)."""
    return (
        extract_frame_type(header) == FT_CONTROL
        and extract_variable_field(header) == variable_field
    )


def encode_pcer(event: EventId, source_alias: int) -> bytes:
    """Encode an event report as a GridConnect frame."""
    return encode_frame(OlcbFrame(
        header=make_pcer_header(source_alias),
        data=event.bytes,
    ))


def decode_pcer_if_pcer(frame: OlcbFrame) -> EventReport | None:
    """Return an :class:`EventReport` iff *frame* is a PCER, else None."""
    if extract_mti(frame.header) != MTI_PCER:
        return None
    if len(frame.data) != 8:
        return None
    return EventReport(
        event=EventId(int.from_bytes(frame.data, "big")),
        source_alias=extract_source_alias(frame.header),
    )


async def read_frame(read_byte: Callable[[], Awaitable[int]]) -> str:
    """Read one GridConnect frame (terminated by ``;``)."""
    # Skip until ':X' -- the frame start.
    out = bytearray()
    while True:
        b = await read_byte()
        if b == ord(":"):
            out = bytearray([b])
            break
    while True:
        b = await read_byte()
        out.append(b)
        if b == ord(";"):
            return out.decode("ascii", errors="replace")
