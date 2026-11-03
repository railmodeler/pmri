"""Tests for ``rosters/<id>.xml`` -- per-locomotive CV state file."""
from __future__ import annotations

import pathlib

import pytest

from pmri_core.persistence import (
    LocoCvState,
    load_cv_values_from_locomotive_config,
    load_loco_cv,
    load_loco_cv_bytes,
    save_cv_values_into_locomotive_config,
    save_loco_cv,
    save_loco_cv_bytes,
)


def test_empty_state_serializes_minimally() -> None:
    state = LocoCvState(loco_id="459")
    out = save_loco_cv_bytes(state)
    assert b"locomotive-cv-values" in out
    assert b'id="459"' in out
    # No <CV> children.
    assert b"<CV " not in out


def test_round_trip_preserves_cv_values() -> None:
    state = LocoCvState(loco_id="459", cv_values={"1": 3, "7": 151, "29": 38})
    out = save_loco_cv_bytes(state)
    reparsed = load_loco_cv_bytes(out)
    assert reparsed.loco_id == "459"
    assert reparsed.cv_values == {"1": 3, "7": 151, "29": 38}


def test_supports_split_cv_notation() -> None:
    """Non-integer CV names (e.g. ``17.18``) must round-trip."""
    state = LocoCvState(loco_id="X", cv_values={"17.18": 42})
    reparsed = load_loco_cv_bytes(save_loco_cv_bytes(state))
    assert reparsed.cv_values == {"17.18": 42}


def test_cv_order_is_numeric_then_other() -> None:
    """The writer sorts CVs numerically when possible."""
    state = LocoCvState(loco_id="X", cv_values={
        "29": 38, "1": 3, "17.18": 42, "8": 100,
    })
    out = save_loco_cv_bytes(state).decode()
    positions = {name: out.find(f'name="{name}"') for name in state.cv_values}
    assert positions["1"] < positions["8"] < positions["29"]
    # Non-numeric CVs come after numeric ones.
    assert positions["29"] < positions["17.18"]


def test_load_missing_file_returns_empty_state(tmp_path: pathlib.Path) -> None:
    state = load_loco_cv(tmp_path / "missing" / "459.xml")
    assert state.loco_id == "459"
    assert state.cv_values == {}


def test_save_then_load_via_path(tmp_path: pathlib.Path) -> None:
    state = LocoCvState(loco_id="42", cv_values={"1": 3})
    target = tmp_path / "rosters" / "42.xml"
    save_loco_cv(target, state)
    assert target.exists()
    reloaded = load_loco_cv(target)
    assert reloaded.cv_values == {"1": 3}


def test_rejects_wrong_root_tag() -> None:
    with pytest.raises(ValueError, match="locomotive-cv-values"):
        load_loco_cv_bytes(b"<?xml version='1.0'?><wrong/>")


def test_skips_malformed_cv_entries() -> None:
    """Files hand-edited with non-numeric values shouldn't kill the load."""
    body = b"""<?xml version='1.0' encoding='UTF-8'?>
<locomotive-cv-values id="X">
  <CV name="1" value="3" />
  <CV name="2" value="notanumber" />
  <CV name="3" value="42" />
  <CV value="42" />
</locomotive-cv-values>"""
    state = load_loco_cv_bytes(body)
    assert state.cv_values == {"1": 3, "3": 42}


# ----- save_cv_values_into_locomotive_config (JMRI-canonical writer) ------


def _write_locomotive_config(
    path: pathlib.Path, loco_id: str,
    initial_values: dict[str, int] | None = None,
) -> None:
    """Bootstrap a minimal roster/<id>.xml so the writer has a target."""
    values_block = ""
    if initial_values:
        rows = "".join(
            f'<CV name="{k}" value="{v}"/>' for k, v in initial_values.items()
        )
        values_block = f"<values>{rows}</values>"
    path.write_bytes(
        f"""<?xml version='1.0' encoding='UTF-8'?>
<locomotive-config>
  <locomotive id="{loco_id}" fileName="{loco_id}.xml">
    <decoder family="F" model="M"/>
    <locoaddress><number>3</number></locoaddress>
    {values_block}
  </locomotive>
</locomotive-config>""".encode(),
    )


def test_writer_inserts_values_block_into_bare_locomotive(
    tmp_path: pathlib.Path,
) -> None:
    """A locomotive-config file without a <values> block gets one added
    the first time CVs are written to it."""
    p = tmp_path / "L1.xml"
    _write_locomotive_config(p, "L1")
    save_cv_values_into_locomotive_config(p, {"1": 3, "29": 38})
    state = load_cv_values_from_locomotive_config(p, loco_id="L1")
    assert state is not None
    assert state.cv_values == {"1": 3, "29": 38}


def test_writer_replaces_existing_per_cv_entries(tmp_path: pathlib.Path) -> None:
    """Subsequent writes replace prior per-CV entries but don't
    duplicate them. Preserves the outer ``<values>`` block so any
    sibling children (JMRI Java's ``<decoderDef>``) survive."""
    p = tmp_path / "L1.xml"
    _write_locomotive_config(p, "L1", {"1": 3})
    save_cv_values_into_locomotive_config(p, {"7": 151, "29": 38})
    body = p.read_bytes()
    assert body.count(b"<values>") == 1
    state = load_cv_values_from_locomotive_config(p, loco_id="L1")
    assert state.cv_values == {"7": 151, "29": 38}


def test_writer_preserves_pmri_java_decoderdef_block(
    tmp_path: pathlib.Path,
) -> None:
    """JMRI Java writes a ``<decoderDef>`` inside ``<values>`` that
    tracks per-variable human values (``<varValue item= value=/>``).
    Our writer must NOT wipe that block -- otherwise the first time a
    user does a batch write via our UI, they lose everything JMRI Java
    tracked about the loco."""
    p = tmp_path / "11.xml"
    p.write_bytes(
        b"""<?xml version='1.0' encoding='UTF-8'?>
<locomotive-config>
  <locomotive id="5" fileName="11.xml">
    <decoder family="Tsunami2 Diesel" model="TSU-1100"/>
    <values>
      <decoderDef>
        <varValue item="Vstart" value="0"/>
        <varValue item="Address Format" value="0"/>
      </decoderDef>
      <CVvalue name="1" value="5"/>
      <CVvalue name="29" value="2"/>
    </values>
  </locomotive>
</locomotive-config>"""
    )
    save_cv_values_into_locomotive_config(p, {"29": 34}, loco_id="5")
    body = p.read_bytes()
    # Java's per-variable block survives.
    assert b"<decoderDef>" in body
    assert b'item="Vstart"' in body
    # New CV value present, old one replaced.
    assert b'name="29"' in body
    assert b'value="34"' in body
    # Reader picks up the new CV via the JMRI-canonical CVvalue tag.
    state = load_cv_values_from_locomotive_config(p, loco_id="5")
    assert state.cv_values["29"] == 34


def test_writer_emits_pmri_canonical_cvvalue_tag(
    tmp_path: pathlib.Path,
) -> None:
    """The wire tag is ``<CVvalue>`` -- that's what JMRI Java reads.
    Emitting ``<CV>`` (as an earlier revision did) meant our writes
    were invisible to a JMRI Java-side reload."""
    p = tmp_path / "L1.xml"
    _write_locomotive_config(p, "L1")
    save_cv_values_into_locomotive_config(p, {"1": 3})
    body = p.read_bytes()
    assert b"<CVvalue " in body
    assert b'name="1"' in body


def test_writer_puts_each_cv_entry_on_its_own_line(
    tmp_path: pathlib.Path,
) -> None:
    """lxml's ``pretty_print`` doesn't reformat siblings inside a parsed
    tree, so appending many entries into an existing ``<values>``
    element left them all on one giant line. Explicit ``.tail``
    whitespace fixes that. Regression guard: after a batch write, no
    single line should carry more than one ``<CVvalue>`` entry."""
    p = tmp_path / "L1.xml"
    p.write_bytes(
        b"""<?xml version='1.0' encoding='UTF-8'?>
<locomotive-config>
  <locomotive id="1" fileName="1.xml">
    <values>
      <decoderDef>
        <varValue item="X" value="0"/>
      </decoderDef>
    </values>
  </locomotive>
</locomotive-config>"""
    )
    save_cv_values_into_locomotive_config(
        p, {"1": 3, "7": 151, "29": 34}, loco_id="1",
    )
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    for line in lines:
        assert line.count("<CVvalue") <= 1, (
            f"multiple CVvalue entries on one line: {line[:200]}"
        )


def test_reader_accepts_pmri_java_cvvalue_tag() -> None:
    """A file that only has JMRI Java's ``<CVvalue>`` entries (no
    ``<CV>``) must still be parsed. Previously our reader only iterated
    ``<CV>`` and silently returned zero CVs on every JMRI-written file."""
    body = b"""<?xml version='1.0' encoding='UTF-8'?>
<locomotive-config>
  <locomotive id="5">
    <values>
      <CVvalue name="1" value="5"/>
      <CVvalue name="29" value="2"/>
    </values>
  </locomotive>
</locomotive-config>"""
    from pmri_core.persistence.loco_cv_xml import (  # noqa: PLC0415
        load_cv_values_from_locomotive_config_bytes,
    )
    state = load_cv_values_from_locomotive_config_bytes(body, loco_id="5")
    assert state is not None
    assert state.cv_values == {"1": 5, "29": 2}


def test_writer_drops_values_block_when_dict_is_empty(
    tmp_path: pathlib.Path,
) -> None:
    """A save with no CVs (everything matches defaults) removes the
    per-CV entries. If the ``<values>`` block ends up empty (no
    sibling ``<decoderDef>`` etc.), drop it too -- the "minimum-diff
    roster" invariant. If sibling children exist (JMRI Java's
    ``<decoderDef>``), the ``<values>`` block stays."""
    p = tmp_path / "L1.xml"
    _write_locomotive_config(p, "L1", {"1": 3, "7": 151})
    save_cv_values_into_locomotive_config(p, {})
    body = p.read_bytes()
    # <values> had no siblings -> dropped entirely.
    assert b"<values" not in body
    # Other content survives.
    assert b'family="F"' in body
    assert b"<locoaddress>" in body


def test_writer_preserves_other_locomotive_children(
    tmp_path: pathlib.Path,
) -> None:
    """The rewrite must not touch <decoder>, <locoaddress>, etc."""
    p = tmp_path / "L1.xml"
    _write_locomotive_config(p, "L1")
    save_cv_values_into_locomotive_config(p, {"1": 3})
    body = p.read_bytes()
    assert b'family="F"' in body
    assert b'model="M"' in body
    assert b"<locoaddress>" in body


def test_writer_is_a_noop_on_missing_file(tmp_path: pathlib.Path) -> None:
    """No file -> no roster entry to update. Log-only; don't crash the
    caller (batch write path relies on this)."""
    save_cv_values_into_locomotive_config(
        tmp_path / "nope.xml", {"1": 3},
    )   # should not raise
    assert not (tmp_path / "nope.xml").exists()
