"""ProgrammingSession tests with a fake Programmer."""
from __future__ import annotations

import pytest
from pmri_core.beans.programmer import Programmer, ProgrammerManager, ProgrammingMode
from pmri_core.hal.internal import InternalConnection
from pmri_core.persistence.decoder_xml import DecoderFamily, DecoderModel, DecoderVariable

from pmri_decoderpro import DecoderDefinition, ProgrammingSession


class FakeProgrammer(Programmer):
    """In-memory programmer for tests."""

    def __init__(self, mode: ProgrammingMode = ProgrammingMode.SERVICE_DIRECT) -> None:
        self._mode = mode
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


class FakeProgrammerManager(ProgrammerManager):
    def __init__(self) -> None:
        self.service = FakeProgrammer(ProgrammingMode.SERVICE_DIRECT)
        self.ops_by_address: dict[int, FakeProgrammer] = {}

    def global_programmer(self) -> FakeProgrammer:
        return self.service

    def ops_programmer(
        self, address: int, *, long_address: bool = False
    ) -> FakeProgrammer:
        if address not in self.ops_by_address:
            self.ops_by_address[address] = FakeProgrammer(ProgrammingMode.OPS_MODE)
        return self.ops_by_address[address]


def _sample_definition() -> DecoderDefinition:
    return DecoderDefinition(
        family=DecoderFamily(name="Test", mfg="Test Co", models=[]),
        model=DecoderModel(model="X1"),
        variables=(
            DecoderVariable(CV="1", item="Short Address", default="3"),
            DecoderVariable(CV="29", item="Decoder Config", mask="XXXXXXXV"),
            DecoderVariable(CV="17", item="Long Address"),
            DecoderVariable(CV="17.18", item="Composite"),   # non-integer CV
        ),
    )


async def test_read_cv_populates_cache() -> None:
    pm = FakeProgrammerManager()
    pm.service.cvs[1] = 42
    conn = InternalConnection()
    conn.programmers = pm     # type: ignore[assignment]
    await conn.open({})

    session = ProgrammingSession.from_global(conn, _sample_definition())
    value = await session.read_cv(1)
    assert value == 42
    assert 1 in session.cv_cache
    assert session.cv_cache[1].value == 42
    await conn.close()


async def test_write_cv_updates_cache() -> None:
    pm = FakeProgrammerManager()
    conn = InternalConnection()
    conn.programmers = pm     # type: ignore[assignment]
    await conn.open({})

    session = ProgrammingSession.from_global(conn, _sample_definition())
    await session.write_cv(29, 6)
    assert pm.service.cvs[29] == 6
    assert session.cv_cache[29].value == 6
    await conn.close()


async def test_write_cv_validates_range() -> None:
    pm = FakeProgrammerManager()
    conn = InternalConnection()
    conn.programmers = pm     # type: ignore[assignment]
    await conn.open({})
    session = ProgrammingSession.from_global(conn, _sample_definition())
    with pytest.raises(ValueError):
        await session.write_cv(1, 256)
    await conn.close()


async def test_read_all_skips_split_cvs() -> None:
    pm = FakeProgrammerManager()
    pm.service.cvs.update({1: 3, 29: 6, 17: 192})
    conn = InternalConnection()
    conn.programmers = pm     # type: ignore[assignment]
    await conn.open({})

    session = ProgrammingSession.from_global(conn, _sample_definition())
    out = await session.read_all()
    # 1, 29, 17 are integer CVs; "17.18" is skipped.
    assert set(out.keys()) == {1, 29, 17}
    await conn.close()


async def test_read_identity_uses_cv7_and_cv8() -> None:
    pm = FakeProgrammerManager()
    pm.service.cvs.update({7: 99, 8: 153})
    conn = InternalConnection()
    conn.programmers = pm     # type: ignore[assignment]
    await conn.open({})
    session = ProgrammingSession.from_global(conn, _sample_definition())
    version, mfg = await session.read_identity()
    assert version == 99
    assert mfg == 153
    await conn.close()


async def test_from_ops_creates_address_scoped_programmer() -> None:
    pm = FakeProgrammerManager()
    conn = InternalConnection()
    conn.programmers = pm     # type: ignore[assignment]
    await conn.open({})
    session = ProgrammingSession.from_ops(conn, _sample_definition(), address=3601)
    assert session.mode == ProgrammingMode.OPS_MODE
    await session.write_cv(1, 7)
    assert pm.ops_by_address[3601].cvs[1] == 7
    await conn.close()


async def test_global_session_without_programmer_raises() -> None:
    conn = InternalConnection()
    await conn.open({})   # Internal has no programmer
    with pytest.raises(RuntimeError, match="global programmer"):
        ProgrammingSession.from_global(conn, _sample_definition())
    await conn.close()


async def test_ops_session_without_programmer_raises() -> None:
    conn = InternalConnection()
    await conn.open({})
    with pytest.raises(RuntimeError, match="ops-mode programmer"):
        ProgrammingSession.from_ops(conn, _sample_definition(), address=3)
    await conn.close()
