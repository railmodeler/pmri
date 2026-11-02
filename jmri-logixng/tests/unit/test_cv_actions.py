"""WriteCV / ReadCV actions."""
from __future__ import annotations

import asyncio
import logging

import pytest
from jmri_core.beans.programmer import ProgrammingMode

from jmri_logixng import (
    ExecutionContext,
    ReadCV,
    WriteCV,
    from_yaml,
    to_yaml,
)


# ---- fakes --------------------------------------------------------------


class _FakeGlobalProgrammer:
    def __init__(self) -> None:
        self.mode = ProgrammingMode.SERVICE_DIRECT
        self.writes: list[tuple[int, int]] = []
        self.reads: list[int] = []
        self.values: dict[int, int] = {}       # cv -> value seed for reads
        self.raise_on_read: Exception | None = None
        self.raise_on_write: Exception | None = None

    async def read_cv(self, cv: int) -> int:
        self.reads.append(cv)
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self.values.get(cv, 0)

    async def write_cv(self, cv: int, value: int) -> None:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.writes.append((cv, value))


class _FakeOpsProgrammer(_FakeGlobalProgrammer):
    def __init__(self) -> None:
        super().__init__()
        self.mode = ProgrammingMode.OPS_MODE


class _FakeProgrammerManager:
    def __init__(
        self, *, has_global: bool = True, has_ops: bool = True,
    ) -> None:
        self._global = _FakeGlobalProgrammer() if has_global else None
        self.ops_by_addr: dict[tuple[int, bool], _FakeOpsProgrammer] = {}
        self._has_ops = has_ops

    def global_programmer(self) -> _FakeGlobalProgrammer | None:
        return self._global

    def ops_programmer(
        self, address: int, *, long_address: bool = False,
    ) -> _FakeOpsProgrammer | None:
        if not self._has_ops:
            return None
        key = (address, long_address)
        prog = self.ops_by_addr.get(key)
        if prog is None:
            prog = _FakeOpsProgrammer()
            self.ops_by_addr[key] = prog
        return prog


@pytest.fixture()
def with_programmer(ctx: ExecutionContext) -> _FakeProgrammerManager:
    pm = _FakeProgrammerManager()
    ctx.connection.programmers = pm   # type: ignore[assignment]
    return pm


@pytest.fixture()
def no_programmer(ctx: ExecutionContext) -> _FakeProgrammerManager:
    pm = _FakeProgrammerManager(has_global=False, has_ops=False)
    ctx.connection.programmers = pm   # type: ignore[assignment]
    return pm


# ---- WriteCV -------------------------------------------------------------


async def test_write_cv_service_mode(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
) -> None:
    await WriteCV(cv=29, value=6).execute(ctx)
    assert with_programmer._global is not None
    assert with_programmer._global.writes == [(29, 6)]


async def test_write_cv_ops_mode(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
) -> None:
    await WriteCV(cv=17, value=200, address=3601, long_address=True).execute(ctx)
    prog = with_programmer.ops_by_addr[(3601, True)]
    assert prog.writes == [(17, 200)]


async def test_write_cv_no_programmer(
    ctx: ExecutionContext, no_programmer: _FakeProgrammerManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="jmri_logixng"):
        await WriteCV(cv=29, value=6).execute(ctx)   # must not raise
    assert any("no programmer" in r.getMessage() for r in caplog.records)


async def test_write_cv_timeout_logs(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert with_programmer._global is not None
    with_programmer._global.raise_on_write = asyncio.TimeoutError()
    with caplog.at_level(logging.WARNING, logger="jmri_logixng"):
        await WriteCV(cv=29, value=6).execute(ctx)
    assert any("timed out" in r.getMessage() for r in caplog.records)


async def test_write_cv_generic_error_logs(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert with_programmer._global is not None
    with_programmer._global.raise_on_write = RuntimeError("no ack")
    with caplog.at_level(logging.ERROR, logger="jmri_logixng"):
        await WriteCV(cv=29, value=6).execute(ctx)
    assert any("raised" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad_cv", [0, -1, 1025, "29", 1.5])
def test_write_cv_rejects_bad_cv(bad_cv: object) -> None:
    with pytest.raises(ValueError, match="CV must be an int"):
        WriteCV(cv=bad_cv, value=6)   # type: ignore[arg-type]


@pytest.mark.parametrize("bad_val", [-1, 256, "6"])
def test_write_cv_rejects_bad_value(bad_val: object) -> None:
    with pytest.raises(ValueError, match="CV value must be an int"):
        WriteCV(cv=29, value=bad_val)   # type: ignore[arg-type]


# ---- ReadCV --------------------------------------------------------------


async def test_read_cv_into_memory(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
) -> None:
    assert with_programmer._global is not None
    with_programmer._global.values[29] = 42
    mem = await ctx.connection.memories.provide("IM_CV")
    await ReadCV(cv=29, into_memory="IM_CV").execute(ctx)
    assert mem.value == 42


async def test_read_cv_ops_mode_into_memory(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
) -> None:
    mem = await ctx.connection.memories.provide("IM_OPS")
    await ReadCV(cv=17, address=3, into_memory="IM_OPS").execute(ctx)
    prog = with_programmer.ops_by_addr[(3, False)]
    assert prog.reads == [17]
    assert mem.value == 0   # seeded default


async def test_read_cv_timeout_writes_none(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
) -> None:
    assert with_programmer._global is not None
    with_programmer._global.raise_on_read = asyncio.TimeoutError()
    mem = await ctx.connection.memories.provide("IM_R")
    await mem.set_value(999)
    await ReadCV(cv=29, into_memory="IM_R").execute(ctx)
    assert mem.value is None


async def test_read_cv_no_memory_specified(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
) -> None:
    """Fire-and-forget: read but don't store. Should still perform the read."""
    assert with_programmer._global is not None
    await ReadCV(cv=8).execute(ctx)
    assert with_programmer._global.reads == [8]


async def test_read_cv_no_programmer_writes_none(
    ctx: ExecutionContext, no_programmer: _FakeProgrammerManager,
) -> None:
    mem = await ctx.connection.memories.provide("IM_R")
    await mem.set_value(999)
    await ReadCV(cv=29, into_memory="IM_R").execute(ctx)
    assert mem.value is None


async def test_read_cv_missing_target_memory_logs(
    ctx: ExecutionContext, with_programmer: _FakeProgrammerManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="jmri_logixng"):
        await ReadCV(cv=8, into_memory="IM_missing").execute(ctx)
    assert any("no memory" in r.getMessage() for r in caplog.records)


# ---- round-trip ----------------------------------------------------------


def test_write_cv_round_trip_yaml() -> None:
    original = WriteCV(cv=29, value=6, address=3, long_address=False,
                       user_name="Set CV29")
    text = to_yaml(original)
    restored = from_yaml(text)
    assert isinstance(restored, WriteCV)
    assert restored.cv == 29
    assert restored.value == 6
    assert restored.address == 3
    assert restored.long_address is False
    assert restored.user_name == "Set CV29"


def test_write_cv_service_mode_round_trip_omits_address() -> None:
    """Service-mode (address=None) shouldn't clutter the YAML with
    address / long_address fields."""
    original = WriteCV(cv=1, value=3)
    text = to_yaml(original)
    assert "address:" not in text
    restored = from_yaml(text)
    assert isinstance(restored, WriteCV)
    assert restored.address is None


def test_read_cv_round_trip_yaml() -> None:
    original = ReadCV(cv=8, address=3601, long_address=True,
                      into_memory="IM_OUT")
    restored = from_yaml(to_yaml(original))
    assert isinstance(restored, ReadCV)
    assert restored.cv == 8
    assert restored.address == 3601
    assert restored.long_address is True
    assert restored.into_memory == "IM_OUT"
