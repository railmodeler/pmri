"""Pane-composition tests -- the tab-order + filtering logic that drives
the DecoderPro-style programmer view.
"""
from __future__ import annotations

from pmri_core.persistence.decoder_xml import DecoderPane, DecoderVariable
from pmri_core.persistence.programmer_xml import ProgrammerConfig

from pmri_decoderpro import (
    ROSTER_ENTRY_NAME,
    ROSTER_ENTRY_SLUG,
    compose_panes,
    find_pane_by_slug,
    slugify_pane_name,
)


def _pane(name: str, items: list[str], **kwargs) -> DecoderPane:
    return DecoderPane(name=name, items=items, **kwargs)


def _var(item: str, cv: str = "1") -> DecoderVariable:
    return DecoderVariable.model_validate({"CV": cv, "item": item})


# ----- roster entry --------------------------------------------------------


def test_roster_entry_pane_is_always_first_and_synthetic() -> None:
    """No matter what the programmer or decoder declares, the synthetic
    Roster Entry tab always leads the tab list."""
    composed = compose_panes([], None, [])
    assert composed[0].name == ROSTER_ENTRY_NAME
    assert composed[0].slug == ROSTER_ENTRY_SLUG
    assert composed[0].is_synthetic is True
    assert composed[0].pane is None


def test_roster_entry_pane_is_never_empty() -> None:
    """Roster Entry has no variables but still renders (roster fields)."""
    composed = compose_panes([], None, [])
    assert composed[0].is_empty is False


# ----- standard tab order --------------------------------------------------


def test_standard_tabs_follow_spec_order() -> None:
    """When the programmer declares Basic + Motor + Sound, they appear
    in the spec-defined order regardless of declaration order."""
    programmer = ProgrammerConfig(
        name="Comprehensive",
        panes=[
            _pane("Sound", ["Volume"]),
            _pane("Motor", ["Vstart"]),
            _pane("Basic", ["Short Address"]),
        ],
    )
    decoder_vars = [_var("Short Address"), _var("Vstart"), _var("Volume")]
    composed = compose_panes(decoder_vars, programmer, [])
    names = [c.name for c in composed]
    # Roster Entry, then Basic, Motor, Sound -- the spec order.
    assert names == [ROSTER_ENTRY_NAME, "Basic", "Motor", "Sound"]


def test_dropped_tabs_never_appear_even_when_programmer_declares_them() -> None:
    """Comments and Legacy Function Map are explicitly excluded per the
    user's spec -- even if the programmer XML declares them."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[
            _pane("Basic", ["Short Address"]),
            _pane("Comments", ["some_var"]),
            _pane("Legacy Function Map", ["some_var"]),
        ],
    )
    decoder_vars = [_var("Short Address"), _var("some_var")]
    composed = compose_panes(decoder_vars, programmer, [])
    names = [c.name for c in composed]
    assert "Comments" not in names
    assert "Legacy Function Map" not in names


# ----- empty-pane filtering ------------------------------------------------


def test_empty_pane_is_hidden_by_default() -> None:
    """A pane whose items aren't in the decoder's variable table is empty
    and hidden -- matches JMRI's default ``showEmptyPanes`` behaviour."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[
            _pane("Basic", ["Short Address"]),
            _pane("Motor", ["Vstart"]),   # decoder has no Vstart
        ],
        show_empty_panes="default",
    )
    decoder_vars = [_var("Short Address")]
    composed = compose_panes(decoder_vars, programmer, [])
    names = [c.name for c in composed]
    assert "Basic" in names
    assert "Motor" not in names


def test_empty_pane_is_visible_when_programmer_says_show_empty() -> None:
    """A programmer with ``showEmptyPanes="yes"`` keeps all its tabs even
    if the decoder can't fill them."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[
            _pane("Basic", ["Short Address"]),
            _pane("Motor", ["Vstart"]),
        ],
        show_empty_panes="yes",
    )
    decoder_vars = [_var("Short Address")]
    composed = compose_panes(decoder_vars, programmer, [])
    names = [c.name for c in composed]
    assert "Basic" in names
    assert "Motor" in names


def test_cvtable_pane_never_counts_as_empty() -> None:
    """The CVs tab uses ``<cvtable/>`` -- its content isn't item-based, so
    it stays visible even when its ``items`` list is empty."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[_pane("CVs", [], has_cvtable=True)],
    )
    composed = compose_panes([_var("Whatever")], programmer, [])
    names = [c.name for c in composed]
    assert "CVs" in names


def test_dccaddress_pane_never_counts_as_empty() -> None:
    """A pane with ``<dccaddress/>`` (the Basic pane's composite address
    widget) counts as populated even without ``items``."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[_pane("Basic", [], has_dcc_address=True)],
    )
    composed = compose_panes([], programmer, [])
    names = [c.name for c in composed]
    assert "Basic" in names


# ----- decoder-declared extras --------------------------------------------


def test_decoder_declared_panes_appended_after_standard_tabs() -> None:
    """Panes the decoder file declares (e.g. Digitrax, SUSI) land after
    the standard-order block."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[_pane("Basic", ["Short Address"])],
    )
    decoder_panes = [_pane("Digitrax", ["Decoder Lock ID number"])]
    decoder_vars = [_var("Short Address"), _var("Decoder Lock ID number")]
    composed = compose_panes(decoder_vars, programmer, decoder_panes)
    names = [c.name for c in composed]
    assert names == [ROSTER_ENTRY_NAME, "Basic", "Digitrax"]


def test_decoder_declared_panes_hidden_when_programmer_disables_them() -> None:
    """``decoderFilePanes="no"`` on the programmer means: ignore the
    decoder's own panes entirely (JMRI-Java parity)."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[_pane("Basic", ["Short Address"])],
        decoder_file_panes=False,
    )
    decoder_panes = [_pane("Digitrax", ["Decoder Lock ID number"])]
    decoder_vars = [_var("Short Address"), _var("Decoder Lock ID number")]
    composed = compose_panes(decoder_vars, programmer, decoder_panes)
    names = [c.name for c in composed]
    assert "Digitrax" not in names


def test_decoder_pane_name_collision_uses_decoder_version() -> None:
    """When both sources declare a pane with the same name, the
    decoder-declared version wins. Real-world case: SoundTraxx
    Tsunami2 ships its own "Function Map" pane (Extended Function
    Map) which should replace Comprehensive.xml's generic
    ``<fnmapping/>`` matrix. The standard-tab slot is preserved so
    the Function Map still lands in its usual position, but the
    content comes from the decoder file."""
    programmer = ProgrammerConfig(
        name="Comprehensive",
        panes=[_pane("Sound", ["MasterVolume"])],
    )
    decoder_panes = [_pane("Sound", ["OtherVolume"])]
    decoder_vars = [_var("MasterVolume"), _var("OtherVolume")]
    composed = compose_panes(decoder_vars, programmer, decoder_panes)
    sound_tabs = [c for c in composed if c.name == "Sound"]
    assert len(sound_tabs) == 1
    assert sound_tabs[0].source == "decoder"


# ----- items filtering ----------------------------------------------------


def test_pane_dedupes_repeated_items() -> None:
    """JMRI's real pane fragments sometimes list the same variable
    twice -- a numeric input widget plus a slider widget both bound to
    Vstart, for example. Our HTML rendering only knows how to make
    one row per variable, so duplicates would produce two independent
    rows whose state diverges after any write. ``items_available``
    dedupes while preserving first-seen order."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[_pane(
            "Basic Speed Control",
            ["Vstart", "Vmid", "Vhigh", "Vstart", "Vmid", "Vhigh"],
        )],
    )
    decoder_vars = [_var("Vstart", "2"), _var("Vmid", "6"), _var("Vhigh", "5")]
    composed = compose_panes(decoder_vars, programmer, [])
    bsc = next(c for c in composed if c.name == "Basic Speed Control")
    assert bsc.items_available == ("Vstart", "Vmid", "Vhigh")


def test_pane_items_filtered_against_decoder_variables() -> None:
    """``items_available`` on the composed pane is the intersection of
    the pane's declared items and the decoder's actual variables -- so
    the rendering layer never tries to display a variable the decoder
    doesn't have."""
    programmer = ProgrammerConfig(
        name="Custom",
        panes=[_pane("Basic", ["Short Address", "Long Address", "Not There"])],
    )
    decoder_vars = [_var("Short Address"), _var("Long Address")]
    composed = compose_panes(decoder_vars, programmer, [])
    basic = next(c for c in composed if c.name == "Basic")
    assert basic.items_available == ("Short Address", "Long Address")


# ----- helpers -------------------------------------------------------------


def test_slugify_pane_name_produces_url_safe_ids() -> None:
    assert slugify_pane_name("Basic") == "basic"
    assert slugify_pane_name("Basic Speed Control") == "basic-speed-control"
    assert slugify_pane_name("Sound Levels") == "sound-levels"
    assert slugify_pane_name("CVs") == "cvs"
    # Edge case: only-symbol input yields a fallback slug.
    assert slugify_pane_name("!!!") == "pane"


def test_find_pane_by_slug_returns_none_for_unknown() -> None:
    programmer = ProgrammerConfig(
        name="C", panes=[_pane("Basic", ["Short Address"])],
    )
    composed = compose_panes([_var("Short Address")], programmer, [])
    assert find_pane_by_slug(composed, "basic") is not None
    assert find_pane_by_slug(composed, "no-such-tab") is None


# ----- no programmer at all -----------------------------------------------


def test_without_programmer_only_roster_entry_and_decoder_panes_appear() -> None:
    """When no programmer definition is loaded, we still get the Roster
    Entry tab + any decoder-declared panes -- the framework degrades
    gracefully instead of showing an empty tab bar."""
    decoder_panes = [_pane("SUSI Info", ["SUSI-range"])]
    composed = compose_panes([_var("SUSI-range")], None, decoder_panes)
    names = [c.name for c in composed]
    assert names == [ROSTER_ENTRY_NAME, "SUSI Info"]
