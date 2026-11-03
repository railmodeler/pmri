"""Step-2 tests: typed variable parsing + encode/decode round-trips.

Covers the four JMRI variable shapes the spec called out:

* choice / enum encoding (``<enumVal>``)
* bitmask packing (``mask="XXVXXXXX"``)
* composite meta-choices (``<compositeVal>``)
* split-CV assembly (``<splitVal>`` and the CV17/CV18 long-address pair)
"""
from __future__ import annotations

import pytest

from pmri_core.persistence import (
    CompositeSetting,
    CvBitMask,
    SplitCv,
    UnsupportedVariableKind,
    VariableKind,
    load_decoder_bytes,
    resolve_composite_choice,
    variable_apply_to_state,
    variable_cv_list,
    variable_extract_from_state,
    variable_primary_cv,
)


# ----- helpers -----------------------------------------------------------


def _load_variables(inner_xml: bytes):
    """Wrap *inner_xml* (which contains ``<variable>...</variable>`` rows)
    into a full ``<decoder-config>`` shell so we can parse a fragment."""
    blob = (
        b'<?xml version="1.0"?><decoder-config>'
        b'<decoder>'
        b'<family name="F" mfg="M"><model model="M1"/></family>'
        b'<variables>' + inner_xml + b'</variables>'
        b'</decoder></decoder-config>'
    )
    return load_decoder_bytes(blob).variables


# ----- mask parsing ------------------------------------------------------


@pytest.mark.parametrize(
    ("mask", "shift", "width", "mask_value"),
    [
        ("VVVVVVVV", 0, 8, 0xFF),
        ("XVVVVVVV", 0, 7, 0x7F),
        ("XXVXXXXX", 5, 1, 0b0010_0000),
        ("XXVVXXXX", 4, 2, 0b0011_0000),
        ("VXXXXXXX", 7, 1, 0b1000_0000),
        ("", 0, 8, 0xFF),
        ("XXXXXXXX", 0, 8, 0xFF),   # no V -> treat as full byte
    ],
)
def test_cv_bit_mask_parse(mask: str, shift: int, width: int, mask_value: int) -> None:
    m = CvBitMask.parse(mask)
    assert m.shift == shift
    assert m.width == width
    assert m.mask_value == mask_value


def test_cv_bit_mask_extract_and_apply_preserves_other_bits() -> None:
    """The classic read-modify-write path: read CV29, flip the speed-step
    bit, write CV29 back -- the other bits round-trip untouched."""
    m = CvBitMask.parse("XXXXXXVX")     # CV29 bit 1 (0b00000010)
    byte = 0b10101011
    assert m.extract(byte) == 1
    assert m.apply(byte, 0) == 0b10101001
    # And writing back the same value is a no-op.
    assert m.apply(byte, 1) == byte


def test_cv_bit_mask_apply_truncates_wide_values() -> None:
    """Values wider than the V region are clipped to the region size."""
    m = CvBitMask.parse("XXVXXXXX")   # 1 bit at shift 5
    assert m.apply(0, 3) == 0b0010_0000  # bit 5 set, extra bits dropped


# ----- split-CV parsing --------------------------------------------------


@pytest.mark.parametrize(
    ("cv_attr", "primary", "count", "bank", "index"),
    [
        ("1", 1, 1, None, None),
        ("29", 29, 1, None, None),
        ("17.18", 17, 2, None, None),
        ("0.255.261", 261, 1, 0, 255),
        ("0.255.261:4", 261, 4, 0, 255),
    ],
)
def test_split_cv_parse(
    cv_attr: str, primary: int, count: int,
    bank: int | None, index: int | None,
) -> None:
    s = SplitCv.parse(cv_attr)
    assert s.primary_cv == primary
    assert s.byte_count == count
    assert s.bank == bank
    assert s.index == index


def test_split_cv_list_is_low_address_first_ascending() -> None:
    """``cv_list`` gives consecutive CVs starting at the primary -- the
    read/write path iterates them in ascending CV order."""
    assert SplitCv.parse("100:3").cv_list == [100, 101, 102]


# ----- indexed CV plumbing (bank.index.base notation) --------------------


def test_variable_is_indexed_only_when_bank_index_present() -> None:
    from pmri_core.persistence import variable_is_indexed  # noqa: PLC0415
    vs = _load_variables(
        b'<variable CV="16.1.275" item="Sound Setting 42">'
        b'<decVal min="0" max="255"/></variable>'
        b'<variable CV="275" item="Plain 275">'
        b'<decVal min="0" max="255"/></variable>'
        b'<variable CV="17.18" item="Long Addr">'
        b'<longAddressVal/></variable>',
    )
    assert variable_is_indexed(vs[0]) is True     # indexed
    assert variable_is_indexed(vs[1]) is False    # plain
    assert variable_is_indexed(vs[2]) is False    # long-addr split, no bank


def test_variable_store_key_shape() -> None:
    """Roster ``<CVvalue name="...">`` key: bank.index.base for indexed
    variables, plain str(cv) for everything else. Lets ``16.1.275``
    coexist with ``275`` in the roster file without collision -- they
    address different decoder pages."""
    from pmri_core.persistence import (  # noqa: PLC0415
        variable_store_key,
    )
    vs = _load_variables(
        b'<variable CV="16.1.275" item="Sound Setting 42">'
        b'<decVal min="0" max="255"/></variable>'
        b'<variable CV="275" item="Plain 275">'
        b'<decVal min="0" max="255"/></variable>',
    )
    assert variable_store_key(vs[0], 275) == "16.1.275"
    assert variable_store_key(vs[1], 275) == "275"


def test_variable_wire_write_plan_prepends_cv31_cv32_for_indexed() -> None:
    """Every indexed CV write needs bank+index setup FIRST -- otherwise
    the decoder writes to the CV under whatever page it happens to be
    on, corrupting unrelated state. Setup ops carry ``store_key=None``
    so they don't leak into the roster's per-CV tracking."""
    from pmri_core.persistence import (  # noqa: PLC0415
        variable_wire_write_plan,
    )
    vs = _load_variables(
        b'<variable CV="16.2.507" item="Sound Setting">'
        b'<decVal min="0" max="255"/></variable>',
    )
    plan = variable_wire_write_plan(vs[0], 42, {})
    assert plan[0].wire_cv == 31
    assert plan[0].byte_value == 16
    assert plan[0].store_key is None
    assert plan[1].wire_cv == 32
    assert plan[1].byte_value == 2
    assert plan[1].store_key is None
    assert plan[2].wire_cv == 507
    assert plan[2].byte_value == 42
    assert plan[2].store_key == "16.2.507"


def test_variable_wire_write_plan_leaves_plain_writes_alone() -> None:
    """Non-indexed variables produce the same wire ops they always did,
    without spurious CV31/CV32 traffic."""
    from pmri_core.persistence import (  # noqa: PLC0415
        variable_wire_write_plan,
    )
    vs = _load_variables(
        b'<variable CV="2" item="Vstart">'
        b'<decVal min="0" max="255"/></variable>',
    )
    plan = variable_wire_write_plan(vs[0], 12, {})
    assert len(plan) == 1
    assert plan[0].wire_cv == 2
    assert plan[0].byte_value == 12
    assert plan[0].store_key == "2"


def test_variable_wire_write_plan_multi_byte_indexed() -> None:
    """A multi-byte indexed variable (``16.1.275:4``) writes CV31/CV32
    once, then all four base bytes without repeating setup (they all
    share the same page)."""
    from pmri_core.persistence import (  # noqa: PLC0415
        variable_wire_write_plan,
    )
    vs = _load_variables(
        b'<variable CV="16.1.275:4" item="Wide">'
        b'<splitVal/></variable>',
    )
    plan = variable_wire_write_plan(vs[0], 0x11223344, {})
    setup = [op for op in plan if op.store_key is None]
    payload = [op for op in plan if op.store_key is not None]
    assert [op.wire_cv for op in setup] == [31, 32]
    assert [op.wire_cv for op in payload] == [275, 276, 277, 278]
    assert [op.store_key for op in payload] == [
        "16.1.275", "16.1.276", "16.1.277", "16.1.278",
    ]


def test_variable_wire_read_plan_prepends_cv31_cv32_for_indexed() -> None:
    from pmri_core.persistence import (  # noqa: PLC0415
        variable_wire_read_plan,
    )
    vs = _load_variables(
        b'<variable CV="16.2.507" item="Sound Setting">'
        b'<decVal min="0" max="255"/></variable>',
    )
    plan = variable_wire_read_plan(vs[0])
    assert [op.wire_cv for op in plan] == [31, 32, 507]
    assert plan[0].store_key is None
    assert plan[1].store_key is None
    assert plan[2].store_key == "16.2.507"


def test_variable_label_prefers_unlabelled_english_over_localized_variants() -> None:
    """JMRI ships variables with multiple ``<label>`` children -- one
    unqualified (English) and several ``xml:lang="..."`` variants. Naive
    ``elem.find('label')`` picks whichever appears first in document
    order, which is why F13-F28 labels were coming out Italian in the
    UI. The parser must prefer the unqualified (or ``xml:lang="en"``)
    variant."""
    vs = _load_variables(
        b'<variable CV="1" item="Short Address">'
        b'<label xml:lang="it">Indirizzo Breve</label>'
        b'<label>Short Address</label>'
        b'<label xml:lang="de">Kurzadresse</label>'
        b'<decVal min="0" max="127"/>'
        b'</variable>',
    )
    assert vs[0].label == "Short Address"


def test_variable_label_picks_explicit_en_when_no_unqualified_present() -> None:
    """Some JMRI files use ``xml:lang="en"`` explicitly for the English
    label instead of leaving it unqualified. Preferred just as strongly
    -- same result as unqualified."""
    vs = _load_variables(
        b'<variable CV="1" item="Short Address">'
        b'<label xml:lang="it">Indirizzo Breve</label>'
        b'<label xml:lang="en">Short Address</label>'
        b'<decVal min="0" max="127"/>'
        b'</variable>',
    )
    assert vs[0].label == "Short Address"


def test_variable_tooltip_preserves_inline_html_markup() -> None:
    """JMRI tooltips routinely contain inline HTML -- ``<br/>`` for line
    breaks, ``<b>...</b>`` for emphasis. The parser must serialize them
    back as-is so the pane can render them as intended. Escaping (or
    dropping) them would render literal ``&lt;br/&gt;`` in the UI or
    lose the emphasis entirely."""
    vs = _load_variables(
        b'<variable CV="2" item="Vstart">'
        b'<tooltip>Kick-start value.<br/>Set higher for stubborn motors.'
        b'<br/>Range: <b>0-255</b>.</tooltip>'
        b'<decVal min="0" max="255"/>'
        b'</variable>',
    )
    assert "<br/>" in vs[0].tooltip
    assert "<b>0-255</b>" in vs[0].tooltip


def test_find_composite_current_choice_matches_when_all_settings_agree() -> None:
    """A composite variable's ``current choice`` is whichever preset's
    ``<compositeSetting>`` values all match the corresponding
    sub-variables' present state. Lets the UI pre-select the right
    option so a user opening a pane sees "Fast" instead of a blank
    dropdown when the sub-variables are already in the Fast preset."""
    from pmri_core.persistence import find_composite_current_choice  # noqa: PLC0415
    vs = _load_variables(
        b'<variable CV="10" item="Chuff Rate">'
        b'<decVal min="0" max="255"/></variable>'
        b'<variable CV="0" item="Chuff Style">'
        b'<compositeVal>'
        b'  <compositeChoice choice="Off">'
        b'    <compositeSetting label="Chuff Rate" value="0"/>'
        b'  </compositeChoice>'
        b'  <compositeChoice choice="Slow">'
        b'    <compositeSetting label="Chuff Rate" value="10"/>'
        b'  </compositeChoice>'
        b'  <compositeChoice choice="Fast">'
        b'    <compositeSetting label="Chuff Rate" value="25"/>'
        b'  </compositeChoice>'
        b'</compositeVal>'
        b'</variable>',
    )
    by_item = {v.item: v for v in vs}
    composite = by_item["Chuff Style"]

    # CV10 = 25 -> Chuff Rate is 25 -> composite matches "Fast".
    assert find_composite_current_choice(
        composite, by_item, {"10": 25},
    ) == "Fast"
    assert find_composite_current_choice(
        composite, by_item, {"10": 10},
    ) == "Slow"


def test_find_composite_current_choice_returns_none_when_no_match() -> None:
    """If the sub-variables are in an intermediate state (matches no
    preset), the UI shows a blank / 'custom' option instead of
    silently picking the first preset."""
    from pmri_core.persistence import find_composite_current_choice  # noqa: PLC0415
    vs = _load_variables(
        b'<variable CV="10" item="Chuff Rate">'
        b'<decVal min="0" max="255"/></variable>'
        b'<variable CV="0" item="Chuff Style">'
        b'<compositeVal>'
        b'  <compositeChoice choice="Slow">'
        b'    <compositeSetting label="Chuff Rate" value="10"/>'
        b'  </compositeChoice>'
        b'  <compositeChoice choice="Fast">'
        b'    <compositeSetting label="Chuff Rate" value="25"/>'
        b'  </compositeChoice>'
        b'</compositeVal>'
        b'</variable>',
    )
    by_item = {v.item: v for v in vs}
    composite = by_item["Chuff Style"]
    assert find_composite_current_choice(
        composite, by_item, {"10": 17},   # not 10 or 25
    ) is None


def test_variable_extract_from_store_uses_compound_key_for_indexed() -> None:
    """Indexed variables must look up by ``bank.index.cv`` in the store
    -- otherwise a plain CV 275 would collide with any indexed CV whose
    base is 275, and each render would show the wrong value."""
    from pmri_core.persistence import (  # noqa: PLC0415
        variable_extract_from_store,
    )
    vs = _load_variables(
        b'<variable CV="16.1.275" item="Indexed 275">'
        b'<decVal min="0" max="255"/></variable>'
        b'<variable CV="275" item="Plain 275">'
        b'<decVal min="0" max="255"/></variable>',
    )
    store = {"16.1.275": 42, "275": 99}
    assert variable_extract_from_store(vs[0], store) == 42
    assert variable_extract_from_store(vs[1], store) == 99


# ----- variable parsing --------------------------------------------------


def test_variable_decVal_is_kind_decimal() -> None:
    vs = _load_variables(
        b'<variable CV="2" item="Vstart"><decVal min="0" max="255"/>'
        b'<label>Vstart</label></variable>'
    )
    assert vs[0].kind is VariableKind.DECIMAL
    assert vs[0].bit_mask.mask_value == 0xFF   # unmask -> owns whole byte


def test_variable_hexVal_is_kind_hexadecimal() -> None:
    vs = _load_variables(
        b'<variable CV="7" item="Version"><hexVal/></variable>'
    )
    assert vs[0].kind is VariableKind.HEXADECIMAL


def test_variable_enumVal_captures_choices_positionally_when_value_omitted() -> None:
    """The classic 0NMRA speed-step ``<enumVal>``: two choices, no
    explicit ``value`` attributes -> positional 0 / 1."""
    vs = _load_variables(
        b'<variable CV="29" item="Address Format" mask="XXVXXXXX">'
        b'<enumVal>'
        b'<enumChoice choice="Short (one byte) address"/>'
        b'<enumChoice choice="Long (two byte) address"/>'
        b'</enumVal></variable>'
    )
    v = vs[0]
    assert v.kind is VariableKind.ENUM
    assert [c.value for c in v.enum_choices] == [0, 1]
    assert v.enum_choices[0].choice == "Short (one byte) address"
    assert v.bit_mask.shift == 5    # "XXVXXXXX" -- bit 5
    assert v.bit_mask.width == 1


def test_variable_enumVal_respects_explicit_value_attribute() -> None:
    """Digitrax logic-level style: choices carry explicit ``value`` attrs.
    Positions are ignored -- the wire value is what the XML says."""
    vs = _load_variables(
        b'<variable CV="64" item="Logic Level"><enumVal>'
        b'<enumChoice value="0"><choice>Logic Level</choice></enumChoice>'
        b'<enumChoice value="1"><choice>12 Volts</choice></enumChoice>'
        b'</enumVal></variable>'
    )
    v = vs[0]
    assert [c.value for c in v.enum_choices] == [0, 1]
    assert v.enum_choices[0].choice == "Logic Level"


def test_variable_enumChoice_captures_i18n_variants() -> None:
    """xml:lang variants land in ``choice_by_lang`` for a future locale
    picker; the default ``choice`` attribute stays in ``.choice``."""
    vs = _load_variables(
        '<variable CV="29" item="X"><enumVal>'
        '<enumChoice choice="Off">'
        '<choice xml:lang="fr">Arrêt</choice>'
        '<choice xml:lang="it">Spento</choice>'
        '</enumChoice>'
        '</enumVal></variable>'.encode()
    )
    v = vs[0]
    assert v.enum_choices[0].choice == "Off"
    assert v.enum_choices[0].choice_by_lang == {"fr": "Arrêt", "it": "Spento"}


def test_variable_shortAddressVal_and_longAddressVal_classify() -> None:
    vs = _load_variables(
        b'<variable CV="1" item="Short Address"><shortAddressVal/></variable>'
        b'<variable CV="17" item="Long Address"><longAddressVal/></variable>'
    )
    assert vs[0].kind is VariableKind.SHORT_ADDRESS
    assert vs[1].kind is VariableKind.LONG_ADDRESS
    # long-address split defaults to CV17+CV18.
    assert variable_cv_list(vs[1]) == [17, 18]


def test_variable_splitVal_family_captures_multi_byte_layout() -> None:
    """The three ``split*Val`` shapes all classify + carry the parsed
    CV layout."""
    vs = _load_variables(
        b'<variable CV="0.255.261:4" item="ProId 2"><splitVal/></variable>'
        b'<variable CV="0.255.265:4" item="SerNo"><splitHexVal case="upper"/></variable>'
        b'<variable CV="0.255.269:4" item="PDate">'
        b'<splitDateTimeVal display="dateOnly" base="2000-01-01T00:00:00"/>'
        b'</variable>'
    )
    assert vs[0].kind is VariableKind.SPLIT
    assert vs[0].split is not None
    assert vs[0].split.byte_count == 4
    assert vs[0].split.bank == 0 and vs[0].split.index == 255
    assert vs[1].kind is VariableKind.SPLIT_HEX
    assert vs[2].kind is VariableKind.SPLIT_DATETIME


def test_variable_splitDateTimeVal_captures_base_factor_unit_display() -> None:
    """SPLIT_DATETIME carries JMRI's ``base`` / ``factor`` / ``unit`` /
    ``display`` extras into the parsed DecoderVariable so the pane
    widget can format the stored integer as a timestamp."""
    vs = _load_variables(
        b'<variable CV="275:4" item="Last Run"><splitDateTimeVal '
        b'base="2000-01-01T00:00:00" factor="60" unit="SECONDS" '
        b'display="dateOnly"/></variable>'
    )
    assert vs[0].kind is VariableKind.SPLIT_DATETIME
    assert vs[0].dt_base == "2000-01-01T00:00:00"
    assert vs[0].dt_factor == 60
    assert vs[0].dt_unit == "SECONDS"
    assert vs[0].dt_display == "dateOnly"


def test_variable_splitDateTimeVal_defaults_when_attrs_absent() -> None:
    """Missing attributes degrade to safe defaults (empty strings,
    factor=1) rather than crashing. The pane widget then falls back
    to showing the raw integer."""
    vs = _load_variables(
        b'<variable CV="275:4" item="Odd"><splitDateTimeVal/></variable>'
    )
    assert vs[0].kind is VariableKind.SPLIT_DATETIME
    assert vs[0].dt_base == ""
    assert vs[0].dt_factor == 1
    assert vs[0].dt_unit == ""
    assert vs[0].dt_display == ""


def test_variable_splitDateTime_extract_assembles_from_cv_bytes() -> None:
    """``variable_extract_from_store`` must handle SPLIT_DATETIME:
    assemble the stored integer from the CV byte range the same way
    SPLIT does. Previously this raised ``UnsupportedVariableKind``,
    which blocked the pane layer from ever showing a value."""
    from pmri_core.persistence.decoder_xml import variable_extract_from_store  # noqa: PLC0415
    vs = _load_variables(
        b'<variable CV="275:2" item="Last"><splitDateTimeVal '
        b'base="2000-01-01T00:00:00" factor="1" unit="SECONDS"/>'
        b'</variable>'
    )
    # Store keys are string-encoded CV numbers for plain (non-indexed)
    # variables. Big-endian assembly: CV275 is high byte, 276 is low.
    assert variable_extract_from_store(
        vs[0], {"275": 0x12, "276": 0x34},
    ) == 0x1234


def test_variable_compositeVal_captures_choices_and_settings() -> None:
    """The Digitrax DCS OpSw meta-variable: one visible choice fans out
    to writes on multiple sub-variables. Step 2 models the structure;
    the write path lands in step 4."""
    vs = _load_variables(
        b'<variable item="Speed Mode"><compositeVal>'
        b'<compositeChoice choice="28 Speed-steps">'
        b'<compositeSetting label="OpSw 021" value="1"/>'
        b'<compositeSetting label="OpSw 022" value="1"/>'
        b'<compositeSetting label="OpSw 023" value="0"/>'
        b'</compositeChoice>'
        b'<compositeChoice choice="128 Speed-steps">'
        b'<compositeSetting label="OpSw 021" value="0"/>'
        b'<compositeSetting label="OpSw 022" value="0"/>'
        b'<compositeSetting label="OpSw 023" value="0"/>'
        b'</compositeChoice>'
        b'</compositeVal></variable>'
    )
    v = vs[0]
    assert v.kind is VariableKind.COMPOSITE
    assert [c.choice for c in v.composite_choices] == ["28 Speed-steps", "128 Speed-steps"]
    assert v.composite_choices[0].settings == [
        CompositeSetting(label="OpSw 021", value=1),
        CompositeSetting(label="OpSw 022", value=1),
        CompositeSetting(label="OpSw 023", value=0),
    ]


def test_variable_constant_keeps_value() -> None:
    vs = _load_variables(
        b'<variable item="Type"><constant value="42"/></variable>'
    )
    assert vs[0].kind is VariableKind.CONSTANT
    assert vs[0].constant_value == 42


def test_variable_read_only_and_write_only_flags() -> None:
    vs = _load_variables(
        b'<variable CV="7" item="Version" readOnly="yes"><decVal/></variable>'
        b'<variable CV="8" item="Mfg" writeOnly="yes"><decVal/></variable>'
    )
    assert vs[0].read_only is True
    assert vs[0].write_only is False
    assert vs[1].read_only is False
    assert vs[1].write_only is True


def test_variable_tooltip_captured_from_element_or_attribute() -> None:
    vs = _load_variables(
        b'<variable CV="1" item="A" tooltip="attr-tt"><decVal/></variable>'
        b'<variable CV="2" item="B"><decVal/><tooltip>elem-tt</tooltip></variable>'
    )
    assert vs[0].tooltip == "attr-tt"
    assert vs[1].tooltip == "elem-tt"


# ----- encode / decode round-trips --------------------------------------


def test_extract_and_apply_masked_enum_round_trip() -> None:
    """A masked enum (CV29 bit 5 = long-address mode) round-trips."""
    vs = _load_variables(
        b'<variable CV="29" item="Address Format" mask="XXVXXXXX"><enumVal>'
        b'<enumChoice choice="short"/><enumChoice choice="long"/>'
        b'</enumVal></variable>'
    )
    v = vs[0]
    state = {29: 0b00000110}   # bit 5 clear = short, other bits set
    assert variable_extract_from_state(v, state) == 0
    # Apply "long" (value 1) -- bit 5 gets set, everything else preserved.
    writes = variable_apply_to_state(v, 1, state)
    assert writes == {29: 0b00100110}
    # And extracting from the resulting state yields 1.
    state.update(writes)
    assert variable_extract_from_state(v, state) == 1


def test_short_address_round_trip() -> None:
    vs = _load_variables(
        b'<variable CV="1" item="Short Address"><shortAddressVal/></variable>'
    )
    v = vs[0]
    writes = variable_apply_to_state(v, 42, {})
    assert writes == {1: 42}
    assert variable_extract_from_state(v, writes) == 42


def test_long_address_round_trip_uses_nmra_high_bits() -> None:
    """NMRA CV17 carries the ``0b11000000`` marker bits; the low 6 bits +
    all 8 bits of CV18 form the 14-bit address."""
    vs = _load_variables(
        b'<variable CV="17" item="Long Address"><longAddressVal/></variable>'
    )
    v = vs[0]
    writes = variable_apply_to_state(v, 3601, {})
    assert writes == {17: 0xC0 | ((3601 >> 8) & 0x3F), 18: 3601 & 0xFF}
    assert variable_extract_from_state(v, writes) == 3601


def test_split_cv_big_endian_round_trip() -> None:
    """Split-CV assembly is high-byte first: the primary CV holds the
    most significant byte."""
    vs = _load_variables(
        b'<variable CV="100:3" item="Big"><splitVal/></variable>'
    )
    v = vs[0]
    assert variable_cv_list(v) == [100, 101, 102]
    writes = variable_apply_to_state(v, 0x123456, {})
    assert writes == {100: 0x12, 101: 0x34, 102: 0x56}
    assert variable_extract_from_state(v, writes) == 0x123456


def test_constant_never_writes() -> None:
    vs = _load_variables(
        b'<variable item="Type"><constant value="7"/></variable>'
    )
    v = vs[0]
    assert variable_apply_to_state(v, 99, {}) == {}
    assert variable_extract_from_state(v, {}) == 7


def test_read_only_variable_never_writes() -> None:
    """The ``readOnly`` flag suppresses writes -- the write path returns
    an empty dict so the batch queue doesn't schedule a write."""
    vs = _load_variables(
        b'<variable CV="7" item="Version" readOnly="yes"><decVal/></variable>'
    )
    assert variable_apply_to_state(vs[0], 5, {7: 3}) == {}


def test_composite_choice_resolves_to_settings() -> None:
    vs = _load_variables(
        b'<variable item="Speed Mode"><compositeVal>'
        b'<compositeChoice choice="A">'
        b'<compositeSetting label="OpSw 1" value="0"/>'
        b'<compositeSetting label="OpSw 2" value="1"/>'
        b'</compositeChoice>'
        b'<compositeChoice choice="B">'
        b'<compositeSetting label="OpSw 1" value="1"/>'
        b'<compositeSetting label="OpSw 2" value="0"/>'
        b'</compositeChoice>'
        b'</compositeVal></variable>'
    )
    settings = resolve_composite_choice(vs[0], "B")
    assert settings == [
        CompositeSetting(label="OpSw 1", value=1),
        CompositeSetting(label="OpSw 2", value=0),
    ]


def test_composite_choice_unknown_raises() -> None:
    vs = _load_variables(
        b'<variable item="X"><compositeVal>'
        b'<compositeChoice choice="A"/>'
        b'</compositeVal></variable>'
    )
    with pytest.raises(KeyError):
        resolve_composite_choice(vs[0], "not-a-choice")


def test_composite_encode_unsupported_via_apply() -> None:
    """``variable_apply_to_state`` explicitly rejects COMPOSITE -- the
    caller must go through ``resolve_composite_choice`` and apply each
    referenced sub-variable individually."""
    vs = _load_variables(
        b'<variable item="X"><compositeVal>'
        b'<compositeChoice choice="A"/>'
        b'</compositeVal></variable>'
    )
    with pytest.raises(UnsupportedVariableKind):
        variable_apply_to_state(vs[0], 0, {})


def test_speed_table_extract_is_unsupported() -> None:
    """SPEED_TABLE has custom widget semantics -- the flat-value API
    surfaces a clear error rather than silently returning garbage."""
    vs = _load_variables(
        b'<variable CV="67" item="Speed Table"><speedTable/></variable>'
    )
    with pytest.raises(UnsupportedVariableKind):
        variable_extract_from_state(vs[0], {})


def test_variable_primary_cv_and_cv_list_for_simple_variable() -> None:
    vs = _load_variables(
        b'<variable CV="29" item="X"><decVal/></variable>'
    )
    assert variable_primary_cv(vs[0]) == 29
    assert variable_cv_list(vs[0]) == [29]
