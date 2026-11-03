"""Per-manufacturer productID probes.

CV7 (version) + CV8 (mfg) alone typically narrow a decoder identity
to a family, not a specific model. Each manufacturer that ships more
than a couple of decoders per family encodes a **productID** across
one or more extra CVs -- the exact set differs per manufacturer, and
sometimes per model within a manufacturer. This module implements
the same probe logic JMRI Java's
``jmri.jmrit.decoderdefn.IdentifyDecoder`` runs, translated to the
async programmer API here.

Verified against::

    ~/Documents/jmri.jar.src/jmri/jmrit/decoderdefn/IdentifyDecoder.java

**NMRA manufacturer IDs** used below (from that file's
:class:`Manufacturer` enum, lines 105..117 in the decompiled source)::

    DIETZ        = 115
    DIY          =  13
    DOEHLER      =  97
    ESU          = 151
    HARMAN       =  98
    HORNBY       =  48
    QSI          = 113
    SOUNDTRAXX   = 141
    TCS          = 153
    TRAINOMATIC  =  78
    ZIMO         = 145

**What this module currently implements** (highest-value set for
North-American users, matching what real decoders on the workbench
here can exercise):

* :func:`probe_soundtraxx` (CV8 = 141) -- CV253/255/256 bit-packing.
* :func:`probe_esu`        (CV8 = 151) -- CV31/32 page/index writes
  plus 4-byte little-endian read from CV261..264.
* :func:`probe_zimo`       (CV8 = 145) -- single CV250 read.
* :func:`probe_tcs`        (CV8 = 153) -- CV249 with branching to
  248/111/110 for extended IDs.

Others (Hornby, Dietz, Harman, DIY, Doehler, Trainomatic, QSI) are
tracked in ``README.md`` as future adds. NCE + Digitrax are NOT
probed even in JMRI Java -- CV7/CV8 alone identifies them.

Probes are registered in :data:`PROBES` keyed by the CV8 mfg-id;
dispatch happens via :func:`read_product_id`. Adding a new probe is
a one-line registry entry plus a standalone async function; the
identify flow doesn't need to change.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmri_decoderpro.session import ProgrammingSession

_log = logging.getLogger(__name__)


# Mfg IDs -- kept as module-level constants so tests and future probes
# can reference them without magic numbers.
MFG_SOUNDTRAXX = 141
MFG_ESU = 151
MFG_ZIMO = 145
MFG_TCS = 153
MFG_HORNBY = 48
MFG_DIETZ = 115
MFG_HARMAN = 98
MFG_DIY = 13
MFG_DOEHLER = 97
MFG_TRAINOMATIC = 78
MFG_QSI = 113


ProbeFn = Callable[
    ["ProgrammingSession", int, int],
    Awaitable[int | None],
]


# ---- SoundTraxx --------------------------------------------------------


async def probe_soundtraxx(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """SoundTraxx productID probe.

    Only fires for Tsunami2 family (``model_id`` in 70..72); older
    Tsunami / Econami decoders don't populate the productID CVs.

    Byte-packing (from ``IdentifyDecoder.java`` lines 262..380)::

        highest = CV253
        low     = CV256
        high    = CV255
        productID = low | ((high & 0x7) << 8) | (highest << 11)
    """
    if not (70 <= model_id <= 72):
        return None
    highest = await session.programmer.read_cv(253)
    low = await session.programmer.read_cv(256)
    high = await session.programmer.read_cv(255)
    return low | ((high & 0x7) << 8) | (highest << 11)


# ---- ESU ---------------------------------------------------------------


async def probe_esu(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """ESU (Lok family) productID probe.

    Only fires for LokPilot v4+ / LokSound v4+ (``model_id == 255``);
    older ESU decoders don't have the extended-ID CVs.

    Two index CVs (CV31 / CV32) select the "extended-identifier"
    page, then 4 bytes at CV261..264 make up a little-endian
    productID (from ``IdentifyDecoder.java`` lines 331..458)::

        write CV31 = 0
        write CV32 = 255
        productID = CV261 | (CV262 << 8) | (CV263 << 16) | (CV264 << 24)
    """
    if model_id != 255:
        return None
    # These writes route subsequent reads through the extended
    # identifier page. Failing writes are non-fatal -- some ops-mode
    # programmers can't do the page-switch and the caller falls
    # back to family-level ID.
    await session.programmer.write_cv(31, 0)
    await session.programmer.write_cv(32, 255)
    b0 = await session.programmer.read_cv(261)
    b1 = await session.programmer.read_cv(262)
    b2 = await session.programmer.read_cv(263)
    b3 = await session.programmer.read_cv(264)
    return b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)


# ---- Zimo --------------------------------------------------------------


async def probe_zimo(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Zimo productID probe -- CV250 holds a single-byte decoder ID."""
    return await session.programmer.read_cv(250)


# ---- TCS ---------------------------------------------------------------


async def probe_tcs(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Train Control Systems (TCS) productID probe.

    TCS uses CV249 as the primary ID; older decoders (< 129) stop
    there. Newer decoders extend the ID across CV248 / CV111 / CV110
    with a family-specific packing formula (from
    ``IdentifyDecoder.java`` lines 227..426).

    Short form (CV249 < 129)::

        productID = CV249

    Extended form (CV249 >= 129)::

        lowest = CV249; low = CV248; high = CV111; highest = CV110
        # Which packing applies depends on ``model_id`` (from CV7) and
        # the observed lowest byte -- matches the branching in
        # ``IdentifyDecoder.test7`` verbatim.
    """
    lowest = await session.programmer.read_cv(249)
    if lowest < 129:
        return lowest

    low = await session.programmer.read_cv(248)
    high = await session.programmer.read_cv(111)
    highest = await session.programmer.read_cv(110)

    # See ``IdentifyDecoder.test7`` (Java lines 414..425) for the
    # authoritative branching. Kept as a nested if/else to make the
    # correspondence with the Java source obvious to a reader who
    # cross-checks; a table-driven form would obscure the mapping.
    if (
        (129 <= lowest <= 135 and low == 5)
        or model_id >= 5
    ):
        if lowest == 180 and model_id == 5:
            return lowest + low * 256
        return (
            lowest + low * 256 + high * 65536 + highest * 16777216
        )
    if (
        (129 <= lowest <= 135)
        or (170 <= lowest <= 172)
        or lowest == 180
    ) and model_id == 4:
        return lowest + low * 256
    return lowest


# ---- Hornby ------------------------------------------------------------


async def probe_hornby(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Hornby (mfg 48) productID probe.

    Two branches keyed on ``model_id`` (from ``IdentifyDecoder.java``
    lines 167..317):

    * ``model_id == 254`` -- Hornby's newer decoders use CV200/CV201
      as a 2-byte little-endian productID: ``low + high*256``.
    * All other model IDs -- optionally read CV159. If that CV
      doesn't exist (older decoders), no productID is available.
      If CV159 reads back as ``143``, that's a "productID
      continued in CV158" marker: ``productID = (CV158 << 8) | 143``.
      Any other value is the productID directly.
    """
    if model_id == 254:
        high = await session.programmer.read_cv(200)
        low = await session.programmer.read_cv(201)
        return low + high * 256

    # Non-254 branch. CV159 may not exist; a raised exception here
    # propagates up to read_product_id() which swallows to None.
    cv159 = await session.programmer.read_cv(159)
    if cv159 == 143:
        cv158 = await session.programmer.read_cv(158)
        return (cv158 << 8) | 143
    return cv159


# ---- Dietz -------------------------------------------------------------


async def probe_dietz(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Dietz (mfg 115) productID probe -- single byte at CV128.
    Straight from ``IdentifyDecoder.java`` lines 211..214, 293..294."""
    return await session.programmer.read_cv(128)


# ---- Harman ------------------------------------------------------------


async def probe_harman(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Harman (mfg 98) productID probe -- CV112 high byte, CV113
    low byte, packed as ``(high << 8) | low`` (big-endian 2 bytes).
    ``IdentifyDecoder.java`` lines 189..191, 269..272, 327..328."""
    high = await session.programmer.read_cv(112)
    low = await session.programmer.read_cv(113)
    return (high << 8) | low


# ---- DIY ---------------------------------------------------------------


async def probe_diy(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """DIY (mfg 13, the NMRA "do-it-yourself" hobbyist ID)
    productID probe -- 4 bytes at CV47..CV50, big-endian:
    ``(CV47 << 24) | (CV48 << 16) | (CV49 << 8) | CV50``.

    From ``IdentifyDecoder.java`` lines 199..201, 278..280,
    335..337, 373..375, 410..411 -- the read order is 47, 48, 49,
    50 with CV47 as the highest-order byte.
    """
    b47 = await session.programmer.read_cv(47)
    b48 = await session.programmer.read_cv(48)
    b49 = await session.programmer.read_cv(49)
    b50 = await session.programmer.read_cv(50)
    return ((((b47 << 8) | b48) << 8) | b49) << 8 | b50


# ---- Doehler -----------------------------------------------------------


async def probe_doehler(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Doehler & Haass (mfg 97) productID probe -- optional read
    of CV261. In JMRI Java this uses the ``setOptionalCv(true)``
    hint so a read failure yields "no productID" rather than an
    error. Our wrapper achieves the same via the read_product_id
    outer try/except: a raise here means CV261 doesn't exist and
    the outer catch returns None, matching JMRI's "no ID" outcome.
    From ``IdentifyDecoder.java`` lines 203..205, 282..287.
    """
    return await session.programmer.read_cv(261)


# ---- Train-O-Matic -----------------------------------------------------


async def probe_trainomatic(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Train-O-Matic (mfg 78) productID probe -- 3 bytes at
    CV510 (high), CV509 (mid), CV508 (low). Packed as
    ``(CV510 << 16) | (CV509 << 8) | CV508``.

    From ``IdentifyDecoder.java`` lines 208..210, 289..292,
    344..347, 388..390.
    """
    cv510 = await session.programmer.read_cv(510)
    cv509 = await session.programmer.read_cv(509)
    cv508 = await session.programmer.read_cv(508)
    return (cv510 << 16) | (cv509 << 8) | cv508


# ---- QSI ---------------------------------------------------------------


async def probe_qsi(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """QSI (mfg 113) productID probe -- indexed access through
    CV49 (page index) and CV50 (sub-index), reading CV56 twice
    to get the two productID bytes. Packed as
    ``(high << 8) | low``.

    Sequence (from ``IdentifyDecoder.java`` lines 159..161,
    224..225, 306..307, 358..362, 401..402, 437..438):

        write CV49 = 254   (set PI to product-ID page)
        write CV50 = 4     (set SI to high-byte position)
        read  CV56         -> productID high byte
        write CV50 = 5     (set SI to low-byte position)
        read  CV56         -> productID low byte
        productID = high * 256 + low

    Two writes per read make this the most expensive probe; a
    real programmer takes ~1-3s per CV write on service mode, so
    this can be 4+ seconds. Callers see it via the normal timeout
    contract of the underlying read_cv/write_cv calls.
    """
    await session.programmer.write_cv(49, 254)
    await session.programmer.write_cv(50, 4)
    high = await session.programmer.read_cv(56)
    await session.programmer.write_cv(50, 5)
    low = await session.programmer.read_cv(56)
    return high * 256 + low


# ---- registry + dispatch -----------------------------------------------


PROBES: dict[int, ProbeFn] = {
    MFG_SOUNDTRAXX: probe_soundtraxx,
    MFG_ESU: probe_esu,
    MFG_ZIMO: probe_zimo,
    MFG_TCS: probe_tcs,
    MFG_HORNBY: probe_hornby,
    MFG_DIETZ: probe_dietz,
    MFG_HARMAN: probe_harman,
    MFG_DIY: probe_diy,
    MFG_DOEHLER: probe_doehler,
    MFG_TRAINOMATIC: probe_trainomatic,
    MFG_QSI: probe_qsi,
}


async def read_product_id(
    session: ProgrammingSession, mfg_id: int, model_id: int,
) -> int | None:
    """Look up the probe for *mfg_id* and run it.

    Returns ``None`` when:

    * no probe is registered for the manufacturer, or
    * the probe determined the model doesn't populate productID CVs
      (e.g. older SoundTraxx decoders), or
    * a wire-level error prevented any CV from being read -- the
      caller falls back to family-level identify in that case.

    The error path treats a raised exception during a probe as a
    non-fatal miss: identify continues with just CV7+CV8. That
    matches JMRI Java's behaviour of continuing after an optional
    CV read fails.
    """
    probe = PROBES.get(mfg_id)
    if probe is None:
        return None
    try:
        return await probe(session, mfg_id, model_id)
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "productID probe for mfg=%d model=%d failed: %s",
            mfg_id, model_id, e,
        )
        return None
