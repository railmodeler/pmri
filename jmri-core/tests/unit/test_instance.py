from __future__ import annotations

import pytest

from jmri_core.instance import InstanceManager


class Iface:
    pass


class Impl(Iface):
    def __init__(self, label: str = "default") -> None:
        self.label = label


def test_register_then_get_returns_instance() -> None:
    im = InstanceManager()
    impl = Impl()
    im.register(Iface, impl)
    assert im.get(Iface) is impl


def test_get_unregistered_raises() -> None:
    im = InstanceManager()
    with pytest.raises(LookupError, match="Iface"):
        im.get(Iface)


def test_try_get_returns_none_when_absent() -> None:
    im = InstanceManager()
    assert im.try_get(Iface) is None


def test_factory_lazily_builds() -> None:
    im = InstanceManager()
    calls = 0

    def factory() -> Impl:
        nonlocal calls
        calls += 1
        return Impl("from-factory")

    im.register_factory(Iface, factory)
    assert calls == 0
    a = im.get(Iface)
    b = im.get(Iface)
    assert calls == 1, "factory must only fire once"
    assert a is b, "subsequent gets must return the cached instance"
    assert isinstance(a, Impl) and a.label == "from-factory"


def test_register_replaces_factory_binding() -> None:
    im = InstanceManager()
    im.register_factory(Iface, lambda: Impl("factory"))
    direct = Impl("direct")
    im.register(Iface, direct)
    assert im.get(Iface) is direct


def test_contains_reports_both_modes() -> None:
    im = InstanceManager()
    assert not im.contains(Iface)
    im.register_factory(Iface, lambda: Impl())
    assert im.contains(Iface)
    im.remove(Iface)
    assert not im.contains(Iface)


def test_remove_is_noop_when_absent() -> None:
    im = InstanceManager()
    im.remove(Iface)
