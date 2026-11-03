"""Tests for :mod:`pmri_core.persistence.signal_systems`.

Pins the loader's behaviour against XML shapes we've verified
against the real JMRI corpus (``xml/signals/basic``,
``xml/signals/AAR-1946``, ...). The samples inline here are the
minimal valid ``aspecttable`` documents JMRI's schema accepts.
"""
from __future__ import annotations

import pathlib

import pytest

from pmri_core.persistence.signal_systems import (
    SignalAspect,
    SignalSystem,
    SignalSystemLoader,
)


_BASIC_ASPECTS = """<?xml version="1.0" encoding="utf-8"?>
<aspecttable xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <name>Basic Model Signals</name>
  <reference>Basic signals known by many modelers</reference>
  <aspects>
    <aspect>
      <name>Clear</name>
      <indication>Proceed</indication>
      <speed>Normal</speed>
      <speed2>Normal</speed2>
    </aspect>
    <aspect>
      <name>Approach</name>
      <indication>Proceed preparing to stop at next signal</indication>
      <speed>Medium</speed>
    </aspect>
    <aspect>
      <name>Stop</name>
      <indication>Stop</indication>
      <speed>Stop</speed>
    </aspect>
    <aspect>
      <name>Diverging Clear</name>
      <indication>Proceed through diverging route</indication>
      <speed>Medium</speed>
      <route>Diverging</route>
    </aspect>
  </aspects>
</aspecttable>
"""


def _write_system(root: pathlib.Path, system_id: str, xml: str) -> None:
    system_dir = root / system_id
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "aspects.xml").write_text(xml, encoding="utf-8")


# ---- loader lifecycle ---------------------------------------------------


def test_none_root_returns_empty_tuple() -> None:
    """No configured XML root -> no systems and no error. Operators
    without the JMRI install shouldn't see a crash."""
    loader = SignalSystemLoader(None)
    assert loader.systems() == ()
    assert loader.get("basic") is None


def test_missing_root_returns_empty(tmp_path: pathlib.Path) -> None:
    loader = SignalSystemLoader(tmp_path / "not-here")
    assert loader.systems() == ()


def test_loads_single_system(tmp_path: pathlib.Path) -> None:
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    systems = loader.systems()
    assert len(systems) == 1
    system = systems[0]
    assert isinstance(system, SignalSystem)
    assert system.id == "basic"
    assert system.name == "Basic Model Signals"
    assert system.reference == "Basic signals known by many modelers"


def test_aspects_parsed_in_file_order(tmp_path: pathlib.Path) -> None:
    """Signalers expect the file order to match the picker order. If
    this test starts failing, the picker will look wrong to any
    signaller familiar with the source XML."""
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    names = [a.name for a in loader.get("basic").aspects]
    assert names == ["Clear", "Approach", "Stop", "Diverging Clear"]


def test_optional_fields_default_to_empty(
    tmp_path: pathlib.Path,
) -> None:
    """Legacy aspect files often omit ``speed2`` or ``route`` -- those
    must round-trip as empty strings, not raise."""
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    approach = next(
        a for a in loader.get("basic").aspects if a.name == "Approach"
    )
    assert approach.speed2 == ""
    assert approach.route == ""


def test_route_flag_captured_for_diverging_aspects(
    tmp_path: pathlib.Path,
) -> None:
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    diverging = next(
        a for a in loader.get("basic").aspects if a.name == "Diverging Clear"
    )
    assert diverging.route == "Diverging"


def test_multiple_systems_loaded_in_id_order(
    tmp_path: pathlib.Path,
) -> None:
    """Systems present as directories under the root; the loader
    returns them sorted so mobile pickers see a stable order across
    hosts."""
    _write_system(tmp_path, "z-system", _BASIC_ASPECTS)
    _write_system(tmp_path, "a-system", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    ids = [s.id for s in loader.systems()]
    assert ids == ["a-system", "z-system"]


# ---- error handling -----------------------------------------------------


def test_missing_aspects_xml_skipped(tmp_path: pathlib.Path) -> None:
    """A dir under the root without ``aspects.xml`` (e.g. an install
    left ``.svn`` metadata behind) must be silently ignored -- not
    an error."""
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "readme.txt").write_text("hi", encoding="utf-8")
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    ids = [s.id for s in loader.systems()]
    assert ids == ["basic"]


def test_malformed_xml_skipped_with_warning(
    tmp_path: pathlib.Path, caplog,
) -> None:
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    _write_system(tmp_path, "broken", "<not-real-xml>")
    caplog.set_level("WARNING", logger="pmri_core.persistence.signal_systems")
    loader = SignalSystemLoader(tmp_path)
    ids = [s.id for s in loader.systems()]
    assert ids == ["basic"]
    assert any("broken" in r.message for r in caplog.records)


def test_wrong_root_element_skipped(tmp_path: pathlib.Path) -> None:
    """The file exists but has a different root element -- usually
    a wrong-file-copied-here mistake. Skip, don't fail."""
    _write_system(tmp_path, "notasystem", (
        "<?xml version='1.0'?>"
        "<panelfile><panel name='test'/></panelfile>"
    ))
    loader = SignalSystemLoader(tmp_path)
    assert loader.systems() == ()


def test_aspect_without_name_skipped(tmp_path: pathlib.Path) -> None:
    """An unnamed aspect can't be selected -- drop it so we don't
    show a nameless row in the UI."""
    _write_system(tmp_path, "flawed", """<?xml version='1.0'?>
<aspecttable>
  <name>Flawed</name>
  <aspects>
    <aspect><indication>Proceed</indication></aspect>
    <aspect><name>Stop</name><indication>Stop</indication></aspect>
  </aspects>
</aspecttable>""")
    loader = SignalSystemLoader(tmp_path)
    assert [a.name for a in loader.get("flawed").aspects] == ["Stop"]


# ---- cache behaviour ----------------------------------------------------


def test_result_is_cached(tmp_path: pathlib.Path) -> None:
    """Repeated calls hit the cache -- mutating the XML on disk after
    the first read shouldn't be visible until reload()."""
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    first = loader.systems()
    # Add a system on disk.
    _write_system(tmp_path, "extra", _BASIC_ASPECTS)
    second = loader.systems()
    assert [s.id for s in first] == [s.id for s in second]


def test_reload_picks_up_disk_changes(tmp_path: pathlib.Path) -> None:
    _write_system(tmp_path, "basic", _BASIC_ASPECTS)
    loader = SignalSystemLoader(tmp_path)
    assert [s.id for s in loader.systems()] == ["basic"]
    _write_system(tmp_path, "extra", _BASIC_ASPECTS)
    loader.reload()
    assert [s.id for s in loader.systems()] == ["basic", "extra"]


# ---- dataclass shape ----------------------------------------------------


def test_aspect_is_frozen() -> None:
    """The dataclasses are immutable so callers can't accidentally
    mutate the cached representation. If a test needs a mutation,
    build a new instance."""
    aspect = SignalAspect(name="Clear")
    with pytest.raises((AttributeError, Exception)):
        aspect.name = "Stop"       # type: ignore[misc]
