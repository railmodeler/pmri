"""Unit coverage for the ``writable`` capability flag in the shared
NamedBean payload builder.

The flag's job is to let a UI (``_turnout_row.html`` etc.) hide
command controls on observe-only beans without extending JMRI's
canonical JSON schema. We omit the key for the default-writable case
(``additionalProperties: false`` schemas would reject it) and emit
``writable: false`` only when a driver flips the flag.

See ``jmri-ncemonitor``'s passive beans for the one real production
caller; this unit tests the pure projection logic in isolation.
"""
from __future__ import annotations

from jmri_core.bean import AbstractNamedBean

from jmri_server.services.beans import _common_named_bean


def test_writable_default_bean_omits_the_key() -> None:
    """A normal bean has ``writable = True`` on its class; the
    projector must not put the key on the wire."""
    bean = AbstractNamedBean("LT1")
    payload = _common_named_bean(bean)
    assert "writable" not in payload


def test_passive_bean_emits_writable_false() -> None:
    """A subclass that marks ``writable = False`` surfaces as
    ``writable: false`` on the payload. jmri-ncemonitor's passive
    beans ride this path."""
    class _ObservedOnly(AbstractNamedBean):
        writable = False

    bean = _ObservedOnly("MT1")
    payload = _common_named_bean(bean)
    assert payload["writable"] is False


def test_duck_typed_object_without_attribute_defaults_writable() -> None:
    """Defensive: an object missing the attribute entirely is treated
    as writable (the common case for every bean written before the
    flag existed). We route through getattr() so a legacy custom
    bean class can't trip us."""
    class _NoAttr:
        system_name = "X1"
        user_name = None
        comment = None
        properties: dict = {}

    payload = _common_named_bean(_NoAttr())  # type: ignore[arg-type]
    assert "writable" not in payload
