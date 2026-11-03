from __future__ import annotations

import pytest

from pmri_core.bean import AbstractNamedBean, NamedBean


def test_construct_with_system_name_only() -> None:
    b = AbstractNamedBean("LT12")
    assert b.system_name == "LT12"
    assert b.user_name is None
    assert b.comment is None
    assert b.properties == {}
    assert b.display_name == "LT12"


def test_display_name_prefers_user_name() -> None:
    b = AbstractNamedBean("LT12", "Main Street")
    assert b.display_name == "Main Street"


def test_empty_system_name_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        AbstractNamedBean("")


def test_repr_shows_type_and_system_name() -> None:
    b = AbstractNamedBean("LT12")
    assert "AbstractNamedBean" in repr(b)
    assert "'LT12'" in repr(b)


def test_writable_defaults_true_as_class_attribute() -> None:
    """``AbstractNamedBean.writable`` is a class-level capability flag
    defaulting True. Observe-only drivers (``pmri-ncemonitor``'s passive
    beans) override to False; the server reads the flag to decide whether
    to emit the client-facing ``writable`` field in JSON payloads."""
    assert AbstractNamedBean.writable is True
    b = AbstractNamedBean("LT12")
    assert b.writable is True


def test_writable_can_be_overridden_at_subclass() -> None:
    """A subclass flipping ``writable = False`` must not need to touch
    ``__slots__`` -- the flag is a ClassVar, not a per-instance slot."""
    class _Observed(AbstractNamedBean):
        writable = False

    inst = _Observed("LS1")
    assert inst.writable is False


def test_satisfies_namedbean_protocol() -> None:
    b = AbstractNamedBean("LT12")
    assert isinstance(b, NamedBean)


def test_equality_by_type_and_system_name() -> None:
    a = AbstractNamedBean("LT12")
    b = AbstractNamedBean("LT12")
    c = AbstractNamedBean("LT13")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert hash(a) != hash(c)


def test_equality_distinguishes_subclasses() -> None:
    class Special(AbstractNamedBean):
        pass

    a = AbstractNamedBean("LT12")
    b = Special("LT12")
    assert a != b
