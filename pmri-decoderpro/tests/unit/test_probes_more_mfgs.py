"""Tests for the seven productID probes added in beta 4+:
Hornby, Dietz, Harman, DIY, Doehler, Trainomatic, QSI.

Each test mirrors the pattern from :mod:`test_probes`:
* seed the fake programmer's CVs to specific bytes;
* run the probe;
* assert both the returned productID (hand-computed against the
  Java source's packing formula) and the exact CV read/write
  sequence (so a refactor can't quietly drop a page-index write
  or reorder bytes).
"""
from __future__ import annotations

from pmri_core.beans.programmer import Programmer, ProgrammingMode
from pmri_core.persistence.decoder_xml import DecoderFamily, DecoderModel

from pmri_decoderpro import (
    DecoderDefinition,
    MFG_DIETZ,
    MFG_DIY,
    MFG_DOEHLER,
    MFG_HARMAN,
    MFG_HORNBY,
    MFG_QSI,
    MFG_TRAINOMATIC,
    ProgrammingSession,
    probe_dietz,
    probe_diy,
    probe_doehler,
    probe_harman,
    probe_hornby,
    probe_qsi,
    probe_trainomatic,
    read_product_id,
)


class FakeProgrammer(Programmer):
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


# ---- Hornby ------------------------------------------------------------


async def test_hornby_model_254_uses_cv200_cv201() -> None:
    """Newer Hornby decoders (model_id==254) read CV200/CV201 as
    a little-endian 2-byte productID. Verified against
    IdentifyDecoder.java line 312 (``productIDlow + productIDhigh * 256``)."""
    p = FakeProgrammer()
    p.cvs[200] = 0x12   # high
    p.cvs[201] = 0x34   # low
    session = _session_with(p)
    got = await probe_hornby(session, MFG_HORNBY, 254)
    assert got == 0x34 + 0x12 * 256
    assert p.reads == [200, 201]


async def test_hornby_non_254_reads_cv159_directly() -> None:
    """The older-model branch: CV159 IS the productID unless it
    equals 143 (the continuation marker)."""
    p = FakeProgrammer()
    p.cvs[159] = 42
    session = _session_with(p)
    got = await probe_hornby(session, MFG_HORNBY, 100)
    assert got == 42
    assert p.reads == [159]


async def test_hornby_non_254_with_cv159_143_reads_cv158() -> None:
    """CV159==143 is the "continued in CV158" marker; productID
    becomes ``(CV158 << 8) | 143`` per Java line 317."""
    p = FakeProgrammer()
    p.cvs[159] = 143
    p.cvs[158] = 0x22
    session = _session_with(p)
    got = await probe_hornby(session, MFG_HORNBY, 100)
    assert got == (0x22 << 8) | 143
    assert p.reads == [159, 158]


# ---- Dietz -------------------------------------------------------------


async def test_dietz_reads_cv128() -> None:
    p = FakeProgrammer()
    p.cvs[128] = 77
    session = _session_with(p)
    got = await probe_dietz(session, MFG_DIETZ, 1)
    assert got == 77
    assert p.reads == [128]


# ---- Harman ------------------------------------------------------------


async def test_harman_packs_cv112_cv113_big_endian() -> None:
    p = FakeProgrammer()
    p.cvs[112] = 0xAB
    p.cvs[113] = 0xCD
    session = _session_with(p)
    got = await probe_harman(session, MFG_HARMAN, 1)
    assert got == (0xAB << 8) | 0xCD
    assert p.reads == [112, 113]


# ---- DIY ---------------------------------------------------------------


async def test_diy_packs_cv47_50_big_endian_4_bytes() -> None:
    """DIY packs CV47..CV50 as a 4-byte big-endian productID with
    CV47 the highest byte. Java line 411: ``((((h << 8) | h) << 8)
    | l) << 8 | l``. Uses distinctive bytes so a swapped order
    would jump out."""
    p = FakeProgrammer()
    p.cvs[47] = 0x11
    p.cvs[48] = 0x22
    p.cvs[49] = 0x33
    p.cvs[50] = 0x44
    session = _session_with(p)
    got = await probe_diy(session, MFG_DIY, 1)
    assert got == 0x11223344
    assert p.reads == [47, 48, 49, 50]


# ---- Doehler -----------------------------------------------------------


async def test_doehler_reads_cv261() -> None:
    p = FakeProgrammer()
    p.cvs[261] = 55
    session = _session_with(p)
    got = await probe_doehler(session, MFG_DOEHLER, 1)
    assert got == 55
    assert p.reads == [261]


async def test_doehler_missing_cv261_returns_none_via_dispatch() -> None:
    """A Doehler decoder without CV261 (older models) -- the read
    raises, the outer read_product_id() swallows to None. Matches
    JMRI Java's ``setOptionalCv(true)`` semantics."""
    class BadProgrammer(FakeProgrammer):
        async def read_cv(self, cv: int) -> int:
            raise ConnectionError("CV not present")

    p = BadProgrammer()
    session = _session_with(p)
    got = await read_product_id(session, MFG_DOEHLER, 1)
    assert got is None


# ---- Train-O-Matic -----------------------------------------------------


async def test_trainomatic_packs_cv510_509_508() -> None:
    """3-byte big-endian: CV510 << 16 | CV509 << 8 | CV508."""
    p = FakeProgrammer()
    p.cvs[510] = 0x01
    p.cvs[509] = 0x02
    p.cvs[508] = 0x03
    session = _session_with(p)
    got = await probe_trainomatic(session, MFG_TRAINOMATIC, 1)
    assert got == 0x010203
    # Read order matters -- Java reads 510, then 509, then 508.
    assert p.reads == [510, 509, 508]


# ---- QSI ---------------------------------------------------------------


async def test_qsi_page_indexes_and_reads_cv56_twice() -> None:
    """QSI's PI/SI (CV49/CV50) dance: page-index to 254, sub-index
    4 -> CV56 = high; sub-index 5 -> CV56 = low. productID = high*256 + low."""
    p = FakeProgrammer()
    # The fake returns 0 for the first CV56 read (after PI/SI to
    # 4), then we need a way to have the second read return
    # something different. Since our fake stores a single dict
    # entry per CV, use a sequence-based fake here.

    class SeqProgrammer(FakeProgrammer):
        def __init__(self) -> None:
            super().__init__()
            self.cv56_values = [0xAA, 0xBB]   # first read, second read
            self._cv56_idx = 0

        async def read_cv(self, cv: int) -> int:
            self.reads.append(cv)
            if cv == 56:
                v = self.cv56_values[self._cv56_idx]
                self._cv56_idx += 1
                return v
            return self.cvs.get(cv, 0)

    sp = SeqProgrammer()
    session = _session_with(sp)
    got = await probe_qsi(session, MFG_QSI, 1)
    assert got == 0xAA * 256 + 0xBB
    # Writes: PI=254, SI=4, then SI=5 between the two CV56 reads.
    assert sp.writes == [(49, 254), (50, 4), (50, 5)]
    # Reads: CV56 twice.
    assert sp.reads == [56, 56]


# ---- registry dispatch smoke -------------------------------------------


async def test_read_product_id_covers_all_new_mfgs() -> None:
    """Every new mfg id resolves to a probe (not None-returning
    for-lack-of-registration). Guards against a future refactor
    dropping one from the registry table."""
    from pmri_decoderpro import PROBES  # noqa: PLC0415
    for mfg_id in [
        MFG_HORNBY, MFG_DIETZ, MFG_HARMAN, MFG_DIY,
        MFG_DOEHLER, MFG_TRAINOMATIC, MFG_QSI,
    ]:
        assert mfg_id in PROBES, f"mfg {mfg_id} missing from PROBES"
