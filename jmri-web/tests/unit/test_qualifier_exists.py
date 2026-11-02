"""``_qualifier_exists_want_present`` -- coerce an ``<qualifier
relation=exists><value>…</value></qualifier>`` payload to the
"require present" / "require absent" flag that the pane renderer's
qualifier gate uses.

Before this helper landed, the renderer silently passed every
``exists`` qualifier. That meant ``exists=yes`` on a variable that
got filtered out by productID showed the host variable anyway, and
``exists=no`` on a present variable also leaked. The helper
restores JMRI Java's ``ArithmeticQualifier`` semantics.
"""
from __future__ import annotations

import pytest
from jmri_core.persistence.decoder_xml import DecoderVariable

from jmri_web.routes import (
    _qualifier_exists_want_present as _want,
)
from jmri_web.routes import (
    _variable_qualifiers_pass,
)

# ---- schema-faithful int path ----------------------------------------------


def test_int_zero_wants_absent() -> None:
    """JMRI's canonical form: ``<value>0</value>`` means
    "require ABSENT". Matches ArithmeticQualifier.java."""
    assert _want("0") is False


def test_int_one_wants_present() -> None:
    assert _want("1") is True


def test_int_nonzero_wants_present() -> None:
    """Any non-zero int is "require present". The schema permits any
    xs:int here, so cover a few shapes."""
    assert _want("42") is True
    assert _want("-1") is True


# ---- permissive string synonyms (decoder files in the wild) ----------------
#
# A handful of decoder XML files use ``yes``/``no`` or ``true``/``false``
# in the ``<value>`` element even though the schema types it as xs:int.
# JMRI tolerates that; so do we.


@pytest.mark.parametrize("raw", ["no", "No", "NO", "false", "False", "FALSE"])
def test_string_falsey_wants_absent(raw: str) -> None:
    assert _want(raw) is False


@pytest.mark.parametrize("raw", ["yes", "Yes", "YES", "true", "True", "TRUE"])
def test_string_truthy_wants_present(raw: str) -> None:
    assert _want(raw) is True


def test_whitespace_tolerated() -> None:
    """Surrounding whitespace from pretty-printed XML shouldn't flip
    the meaning."""
    assert _want("  0  ") is False
    assert _want("  yes  ") is True


# ---- defaults for malformed / missing input --------------------------------


def test_empty_defaults_to_present() -> None:
    """A bare ``<value/>`` with no body reads as "require present" --
    matches the natural reading of a lone ``<qualifier relation=exists/>``
    with no value attribute."""
    assert _want("") is True


def test_none_defaults_to_present() -> None:
    assert _want(None) is True  # type: ignore[arg-type]


def test_garbage_string_defaults_to_present() -> None:
    """Anything else that isn't recognisably a 0/no/false falls back
    to "require present" rather than raising -- JMRI's tolerant
    behaviour for decoder files that share qualifier snippets across
    models."""
    assert _want("maybe") is True
    assert _want("0.0.0") is True


# ---- end-to-end filter behaviour -------------------------------------------
#
# Exercises ``_variable_qualifiers_pass`` directly with hand-built
# DecoderVariable instances. Covers the pane-render filter without
# the full catalog / HTTP stack.


def _mk_var(item: str, cv: str = "1", **extra: object) -> DecoderVariable:
    """A minimal DecoderVariable usable as a ref for qualifier tests.

    All variables in the harness are single-byte DECIMAL (``mask=
    VVVVVVVV``) rooted at a distinct CV so the pane filter can
    extract their values from the store.
    """
    return DecoderVariable(
        item=item,
        label=item,
        cv=cv,
        mask="VVVVVVVV",
        default=0,
        min_value=0,
        max_value=255,
        **extra,  # type: ignore[arg-type]
    )


# ---- `exists` relation --------------------------------------------


def test_exists_yes_passes_when_ref_is_present() -> None:
    """``exists`` with any non-zero value requires the ref to be
    PID-applicable. Ref is present -> gate passes -> host is kept."""
    ref = _mk_var("Ref", cv="2")
    host = _mk_var("Host", cv="3", qualifiers=[
        {"variableref": "Ref", "relation": "exists", "value": "1"},
    ])
    pid_by_item = {"Ref": ref, "Host": host}
    assert _variable_qualifiers_pass(host, pid_by_item, {}) is True


def test_exists_yes_fails_when_ref_is_absent() -> None:
    """The regression: before the fix, ``exists`` always passed, so
    a host conditional on a PID-filtered-out ref would show anyway.
    Now the gate correctly fails."""
    host = _mk_var("Host", cv="3", qualifiers=[
        {"variableref": "MissingRef", "relation": "exists", "value": "1"},
    ])
    pid_by_item = {"Host": host}  # MissingRef not here
    assert _variable_qualifiers_pass(host, pid_by_item, {}) is False


def test_exists_no_passes_when_ref_is_absent() -> None:
    """``value=0`` on ``exists`` is "require ABSENT" -- symmetric to
    the yes case. Covers the second half of the bug."""
    host = _mk_var("Host", cv="3", qualifiers=[
        {"variableref": "MissingRef", "relation": "exists", "value": "0"},
    ])
    pid_by_item = {"Host": host}
    assert _variable_qualifiers_pass(host, pid_by_item, {}) is True


def test_exists_no_fails_when_ref_is_present() -> None:
    ref = _mk_var("Ref", cv="2")
    host = _mk_var("Host", cv="3", qualifiers=[
        {"variableref": "Ref", "relation": "exists", "value": "0"},
    ])
    pid_by_item = {"Ref": ref, "Host": host}
    assert _variable_qualifiers_pass(host, pid_by_item, {}) is False


# ---- numeric relations keep JMRI's tolerant behaviour -------------


def test_numeric_relation_against_missing_ref_passes_permissively() -> None:
    """A numeric relation whose ref got PID-filtered out is *skipped*
    (not failed) -- JMRI tolerates qualifier snippets that reference
    variables defined in sibling decoder files. The exists-fix must
    not accidentally tighten this path."""
    host = _mk_var("Host", cv="3", qualifiers=[
        {"variableref": "MissingRef", "relation": "eq", "value": "5"},
    ])
    pid_by_item = {"Host": host}
    assert _variable_qualifiers_pass(host, pid_by_item, {}) is True


def test_numeric_eq_passes_when_ref_matches() -> None:
    """Store key for a plain short-CV variable is ``str(cv)`` -- see
    :func:`jmri_core.persistence.decoder_xml.variable_extract_from_store`."""
    ref = _mk_var("Ref", cv="2")
    host = _mk_var("Host", cv="3", qualifiers=[
        {"variableref": "Ref", "relation": "eq", "value": "7"},
    ])
    pid_by_item = {"Ref": ref, "Host": host}
    store = {"2": 7}
    assert _variable_qualifiers_pass(host, pid_by_item, store) is True


def test_numeric_eq_fails_when_ref_mismatches() -> None:
    ref = _mk_var("Ref", cv="2")
    host = _mk_var("Host", cv="3", qualifiers=[
        {"variableref": "Ref", "relation": "eq", "value": "7"},
    ])
    pid_by_item = {"Ref": ref, "Host": host}
    store = {"2": 3}
    assert _variable_qualifiers_pass(host, pid_by_item, store) is False


def test_no_qualifiers_always_passes() -> None:
    """A variable with no qualifiers is always visible (modulo
    productID, which the caller handled before invoking us)."""
    v = _mk_var("Plain", cv="1")
    assert _variable_qualifiers_pass(v, {"Plain": v}, {}) is True
