"""Per-manufacturer productID probe tests.

The probes are declarative -- each one reads a specific set of CVs
per JMRI Java's ``IdentifyDecoder.java`` and packs the results.
Tests seed a fake programmer's CVs, run the probe, and verify both:

* the exact CV read sequence (so a future refactor can't quietly
  drop a page/index write), and
* the returned productID against a hand-computed expected value
  (so the bit-packing formula stays honest against the Java source).
"""
from __future__ import annotations

from jmri_core.beans.programmer import Programmer, ProgrammingMode
from jmri_core.persistence.decoder_xml import DecoderFamily, DecoderModel

from jmri_decoderpro import (
    DecoderDefinition,
    MFG_ESU,
    MFG_SOUNDTRAXX,
    MFG_TCS,
    MFG_ZIMO,
    ProgrammingSession,
    probe_esu,
    probe_soundtraxx,
    probe_tcs,
    probe_zimo,
    read_product_id,
)


class FakeProgrammer(Programmer):
    """In-memory programmer with visible read/write history."""

    def __init__(self) -> None:
        self._mode = ProgrammingMode.SERVICE_DIRECT
        self.cvs: dict[int, int] = {}
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []

    @property
    def mode(self) -> ProgrammingMode:
        return self._mode

    async def read_cv(self, cv: int) -> int:
        self.reads.append(cv)
        return self.cvs.get(cv, 0)

    async def write_cv(self, cv: int, value: int) -> None:
        self.writes.append((cv, value))
        self.cvs[cv] = value


def _session_with(programmer: FakeProgrammer) -> ProgrammingSession:
    definition = DecoderDefinition(
        family=DecoderFamily(name="Test", mfg="Test", models=[]),
        model=DecoderModel(model="X1"),
        variables=(),
    )
    return ProgrammingSession(definition=definition, programmer=programmer)


# ---- SoundTraxx --------------------------------------------------------


async def test_soundtraxx_probe_bit_packing() -> None:
    """SoundTraxx Tsunami2 productID formula:
    ``low | ((high & 0x7) << 8) | (highest << 11)``.
    Verified against IdentifyDecoder.java line 380."""
    p = FakeProgrammer()
    p.cvs[253] = 0x05    # highest -> shifts left by 11
    p.cvs[256] = 0x2A    # low -> bits 0..7
    p.cvs[255] = 0x03    # high -> bits 8..10 (mask 0x7)
    session = _session_with(p)

    got = await probe_soundtraxx(session, mfg_id=MFG_SOUNDTRAXX, model_id=71)
    assert got is not None
    expected = 0x2A | ((0x03 & 0x7) << 8) | (0x05 << 11)
    assert got == expected
    # Read order matches the Java source: 253 first, then 256, then 255.
    assert p.reads == [253, 256, 255]


async def test_soundtraxx_probe_skips_older_models() -> None:
    """model_id < 70 -> this decoder family doesn't populate the
    productID CVs. Probe returns None without touching the wire."""
    p = FakeProgrammer()
    session = _session_with(p)

    got = await probe_soundtraxx(session, mfg_id=MFG_SOUNDTRAXX, model_id=44)
    assert got is None
    assert p.reads == []


# ---- ESU ---------------------------------------------------------------


async def test_esu_probe_page_writes_then_4byte_read() -> None:
    """ESU LokPilot/LokSound v4+ productID:
    write CV31=0, write CV32=255, then read CV261..264 as
    little-endian bytes."""
    p = FakeProgrammer()
    p.cvs[261] = 0x12
    p.cvs[262] = 0x34
    p.cvs[263] = 0x56
    p.cvs[264] = 0x78
    session = _session_with(p)

    got = await probe_esu(session, mfg_id=MFG_ESU, model_id=255)
    assert got == (0x12 | (0x34 << 8) | (0x56 << 16) | (0x78 << 24))
    # Page/index writes happen before the 4 productID reads.
    assert p.writes == [(31, 0), (32, 255)]
    assert p.reads == [261, 262, 263, 264]


async def test_esu_probe_skips_non_v4_models() -> None:
    p = FakeProgrammer()
    session = _session_with(p)
    got = await probe_esu(session, mfg_id=MFG_ESU, model_id=100)
    assert got is None
    assert p.writes == []
    assert p.reads == []


# ---- Zimo --------------------------------------------------------------


async def test_zimo_probe_reads_cv250() -> None:
    p = FakeProgrammer()
    p.cvs[250] = 42
    session = _session_with(p)
    got = await probe_zimo(session, mfg_id=MFG_ZIMO, model_id=1)
    assert got == 42
    assert p.reads == [250]


# ---- TCS ---------------------------------------------------------------


async def test_tcs_probe_short_form() -> None:
    """CV249 < 129 -> single-byte productID; no further CVs read."""
    p = FakeProgrammer()
    p.cvs[249] = 25
    session = _session_with(p)
    got = await probe_tcs(session, mfg_id=MFG_TCS, model_id=3)
    assert got == 25
    assert p.reads == [249]


async def test_tcs_probe_extended_form_short_128_135_model_4() -> None:
    """CV249 in [129..135], model_id == 4 -> productID = lowest + low*256.
    Matches ``IdentifyDecoder.java`` line 422."""
    p = FakeProgrammer()
    p.cvs[249] = 130   # lowest  in  [129..135]
    p.cvs[248] = 7     # low
    p.cvs[111] = 0     # high (unused in this branch)
    p.cvs[110] = 0     # highest (unused in this branch)
    session = _session_with(p)
    got = await probe_tcs(session, mfg_id=MFG_TCS, model_id=4)
    assert got == 130 + 7 * 256
    # All four extended CVs must be read even when not all contribute
    # -- matches the Java probe's linear read pattern.
    assert p.reads == [249, 248, 111, 110]


async def test_tcs_probe_full_4byte_when_model_5() -> None:
    """model_id >= 5 with lowest >= 129 and (special-case aside) ->
    full 4-byte little-endian pack. Matches Java line 419."""
    p = FakeProgrammer()
    p.cvs[249] = 200   # lowest
    p.cvs[248] = 0x01  # low
    p.cvs[111] = 0x02  # high
    p.cvs[110] = 0x03  # highest
    session = _session_with(p)
    got = await probe_tcs(session, mfg_id=MFG_TCS, model_id=5)
    assert got == 200 + 0x01 * 256 + 0x02 * 65536 + 0x03 * 16777216


async def test_tcs_probe_special_case_180_model_5() -> None:
    """Special case: lowest == 180 and model_id == 5 -> 2-byte pack.
    Matches Java line 417."""
    p = FakeProgrammer()
    p.cvs[249] = 180
    p.cvs[248] = 0x05
    session = _session_with(p)
    got = await probe_tcs(session, mfg_id=MFG_TCS, model_id=5)
    assert got == 180 + 0x05 * 256


# ---- dispatch registry -------------------------------------------------


async def test_read_product_id_dispatches_to_soundtraxx() -> None:
    p = FakeProgrammer()
    p.cvs[253] = 1
    p.cvs[255] = 0
    p.cvs[256] = 0
    session = _session_with(p)
    got = await read_product_id(session, mfg_id=MFG_SOUNDTRAXX, model_id=71)
    assert got == 1 << 11


async def test_read_product_id_returns_none_for_unknown_mfg() -> None:
    """CV8 = 11 (NCE) -> we don't probe NCE. Return None so the
    identify path falls back to family-level CV7/CV8 filtering."""
    p = FakeProgrammer()
    session = _session_with(p)
    got = await read_product_id(session, mfg_id=11, model_id=100)
    assert got is None
    # No wire traffic for unknown mfgs -- the fallback path handles it.
    assert p.reads == []


async def test_read_product_id_swallows_probe_errors() -> None:
    """A wire error mid-probe must not kill identify; the caller
    falls back to family-level identify. Matches JMRI Java's
    ``setOptionalCv`` handling."""
    class BadProgrammer(FakeProgrammer):
        async def read_cv(self, cv: int) -> int:
            raise ConnectionError("wire error simulated")

    p = BadProgrammer()
    session = _session_with(p)
    got = await read_product_id(session, mfg_id=MFG_ZIMO, model_id=1)
    assert got is None
