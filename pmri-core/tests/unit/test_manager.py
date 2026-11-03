from __future__ import annotations

import pytest

from pmri_core.beans.turnout import Turnout, TurnoutManager
from pmri_core.manager import Manager


def test_subclass_without_type_letter_raises() -> None:
    class BadMgr(Manager[Turnout]):
        pass  # forgot to set type_letter

    with pytest.raises(TypeError, match="type_letter"):
        BadMgr("L")


def test_empty_prefix_rejected() -> None:
    with pytest.raises(ValueError, match="system_prefix"):
        TurnoutManager("")


async def test_provide_creates_bean_by_full_system_name() -> None:
    m = TurnoutManager("L")
    t = await m.provide("LT12")
    assert t.system_name == "LT12"
    assert len(m) == 1
    assert "LT12" in m


async def test_provide_creates_bean_by_bare_address() -> None:
    m = TurnoutManager("L")
    t = await m.provide("42")
    assert t.system_name == "LT42"


async def test_provide_returns_existing_bean_idempotently() -> None:
    m = TurnoutManager("L")
    a = await m.provide("LT12")
    b = await m.provide("LT12")
    assert a is b


async def test_provide_finds_by_user_name() -> None:
    m = TurnoutManager("L")
    t = await m.provide("LT12")
    m._set_user_name(t, "Main Street")
    found = await m.provide("Main Street")
    assert found is t


async def test_register_duplicate_system_name_raises() -> None:
    m = TurnoutManager("L")
    a = Turnout("LT12")
    b = Turnout("LT12")
    await m.register(a)
    with pytest.raises(ValueError, match="already registered"):
        await m.register(b)


async def test_register_duplicate_user_name_raises() -> None:
    m = TurnoutManager("L")
    a = Turnout("LT12", "Main Street")
    b = Turnout("LT13", "Main Street")
    await m.register(a)
    with pytest.raises(ValueError, match="user_name"):
        await m.register(b)


async def test_deregister_removes_from_both_indexes() -> None:
    m = TurnoutManager("L")
    t = Turnout("LT12", "Main Street")
    await m.register(t)
    await m.deregister(t)
    assert len(m) == 0
    assert m.get_by_system_name("LT12") is None
    assert m.get_by_user_name("Main Street") is None


async def test_deregister_unknown_bean_is_noop() -> None:
    m = TurnoutManager("L")
    t = Turnout("LT12")
    await m.deregister(t)  # must not raise


async def test_added_signal_fires_on_registration() -> None:
    m = TurnoutManager("L")
    seen: list[Turnout] = []
    m.added.connect(seen.append)
    t = await m.provide("LT12")
    assert seen == [t]


async def test_removed_signal_fires_on_deregistration() -> None:
    m = TurnoutManager("L")
    t = await m.provide("LT12")
    seen: list[Turnout] = []
    m.removed.connect(seen.append)
    await m.deregister(t)
    assert seen == [t]


async def test_iter_yields_all_beans() -> None:
    m = TurnoutManager("L")
    await m.provide("LT12")
    await m.provide("LT13")
    names = {t.system_name for t in m}
    assert names == {"LT12", "LT13"}


async def test_to_dict_snapshot() -> None:
    m = TurnoutManager("L")
    t = await m.provide("LT12")
    t.user_name = "Main Street"
    snap = m.to_dict()
    assert snap["type_letter"] == "T"
    assert snap["system_prefix"] == "L"
    assert snap["beans"][0]["system_name"] == "LT12"
