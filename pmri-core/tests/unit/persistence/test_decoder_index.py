"""Decoder-index scanner tests -- the fast path used at catalog-load time.

The full :func:`iter_decoder_files` path parses variables + panes for
every file which is too slow with 2000+ decoder XMLs on a Pi. The
scanner in this module only extracts the (family, mfg, model) tuple + a
path back to the source file; :func:`load_decoder` is called lazily
later, only for the specific loco a user opens.
"""
from __future__ import annotations

import pathlib
import time

from pmri_core.persistence.decoder_xml import (
    DecoderIndexEntry,
    iter_decoder_files,
    iter_decoder_index,
)

GOLDEN_DECODERS = (
    pathlib.Path(__file__).resolve().parents[4]
    / "stage-0" / "contracts" / "xml" / "golden" / "decoders"
)


def test_index_yields_family_and_model_for_every_golden_file() -> None:
    entries = list(iter_decoder_index(GOLDEN_DECODERS))
    assert entries, "expected at least some entries from the golden dir"
    # Every entry carries all three identity fields -- an empty family or
    # model would poison a dict lookup, so those get filtered upstream.
    for e in entries:
        assert isinstance(e, DecoderIndexEntry)
        assert e.family
        assert e.model
        assert e.source_file.is_file()


def test_index_covers_all_families_from_the_full_parse() -> None:
    """Sanity-check that the fast scan doesn't miss models the eager
    parser sees. Every (family, model) found by :func:`iter_decoder_files`
    must appear in the index -- otherwise a loco whose decoder is in the
    catalog would silently 404 after this refactor."""
    full = {
        (df.family.name, m.model)
        for df in iter_decoder_files(GOLDEN_DECODERS)
        if df.family is not None
        for m in df.family.models
    }
    fast = {(e.family, e.model) for e in iter_decoder_index(GOLDEN_DECODERS)}
    missing = full - fast
    assert not missing, f"fast scan missed: {missing}"


def test_index_ignores_non_decoder_xml(tmp_path: pathlib.Path) -> None:
    """The decoders/ tree contains index files, NMRA lists, pane
    fragments etc. The scanner must not emit entries for them."""
    (tmp_path / "not-a-decoder.xml").write_bytes(
        b"<?xml version='1.0'?><some-other-root/>",
    )
    # A malformed XML file should also be skipped without raising.
    (tmp_path / "broken.xml").write_bytes(b"<<< not xml >>>")
    entries = list(iter_decoder_index(tmp_path))
    assert entries == []


def test_index_emits_one_entry_per_model(tmp_path: pathlib.Path) -> None:
    """A single decoder XML with N models yields N index entries -- the
    (family, model) lookup key is per-model, not per-file."""
    (tmp_path / "family.xml").write_bytes(b"""<?xml version="1.0"?>
        <decoder-config>
          <decoder>
            <family name="TestFamily" mfg="TestMfg">
              <model model="M1"/>
              <model model="M2"/>
              <model model="M3"/>
            </family>
          </decoder>
        </decoder-config>
    """)
    entries = list(iter_decoder_index(tmp_path))
    assert {e.model for e in entries} == {"M1", "M2", "M3"}
    assert {e.family for e in entries} == {"TestFamily"}
    assert {e.mfg for e in entries} == {"TestMfg"}


def test_index_skips_empty_model_or_family_names(tmp_path: pathlib.Path) -> None:
    """Missing name/model attributes would create an unlookupable index
    key. Skip rather than raise so a partly-malformed file doesn't kill
    the whole scan."""
    (tmp_path / "bad.xml").write_bytes(b"""<?xml version="1.0"?>
        <decoder-config>
          <decoder>
            <family name="" mfg="M">
              <model model="Ok"/>
            </family>
          </decoder>
        </decoder-config>
    """)
    (tmp_path / "bad2.xml").write_bytes(b"""<?xml version="1.0"?>
        <decoder-config>
          <decoder>
            <family name="F" mfg="M">
              <model model=""/>
              <model model="Ok2"/>
            </family>
          </decoder>
        </decoder-config>
    """)
    entries = list(iter_decoder_index(tmp_path))
    assert {e.model for e in entries} == {"Ok2"}


def test_index_is_measurably_faster_than_full_parse() -> None:
    """This test isn't strict-timing; it just guards against a
    regression where iter_decoder_index accidentally starts doing the
    full parse. Full parse over the golden dir should be at least 2x
    the time of the scan."""
    t0 = time.perf_counter()
    _ = list(iter_decoder_index(GOLDEN_DECODERS))
    fast = time.perf_counter() - t0
    t0 = time.perf_counter()
    _ = list(iter_decoder_files(GOLDEN_DECODERS))
    full = time.perf_counter() - t0
    # Loose bound -- golden dir is small; on the real 2000-file tree the
    # ratio is much larger. If this ever regresses to fast >= full we
    # know the scanner is doing too much work.
    assert fast < full, f"fast scan {fast:.3f}s slower than full {full:.3f}s"
