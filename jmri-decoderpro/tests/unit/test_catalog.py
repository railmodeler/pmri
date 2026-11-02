"""Catalog tests against the Stage 0 golden decoder XML files."""
from __future__ import annotations

import pathlib

import pytest

from jmri_decoderpro import DecoderCatalog, DecoderKey

GOLDEN_DECODERS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "stage-0"
    / "contracts"
    / "xml"
    / "golden"
    / "decoders"
)


@pytest.fixture(scope="module")
def catalog() -> DecoderCatalog:
    return DecoderCatalog.from_directory(GOLDEN_DECODERS)


def test_catalog_loads_definitions(catalog: DecoderCatalog) -> None:
    assert len(catalog) > 0
    assert len(catalog) >= 16   # at least one per manufacturer file


def test_catalog_lists_manufacturers(catalog: DecoderCatalog) -> None:
    mfgs = catalog.manufacturers()
    assert "Atlas" in mfgs
    assert "Digitrax" in mfgs


def test_catalog_lookup_by_key(catalog: DecoderCatalog) -> None:
    families = catalog.families()
    assert families
    family = families[0]
    models = catalog.models(family)
    assert models
    defn = catalog.find(family, models[0])
    assert defn is not None
    assert defn.key == DecoderKey(family=family, model=models[0])


def test_catalog_filters_families_by_mfg(catalog: DecoderCatalog) -> None:
    # ``iter(catalog)`` yields :class:`DecoderIndexEntry` metadata now
    # (lazy-load refactor) -- ``entry.family`` is the family name string,
    # ``entry.mfg`` is the mfg string. No full parse triggered here.
    for mfg in catalog.manufacturers():
        families = catalog.families(mfg)
        assert all(
            e.family in families for e in catalog if e.mfg == mfg
        )


def test_empty_catalog_handles_unknown_lookups() -> None:
    cat = DecoderCatalog()
    assert cat.find("nope", "nada") is None
    assert cat.manufacturers() == []
    assert cat.families() == []


def test_catalog_from_files_skips_decoderless() -> None:
    """A DecoderFile whose .family is None is silently skipped."""
    from jmri_core.persistence.decoder_xml import DecoderFile  # noqa: PLC0415
    cat = DecoderCatalog.from_files([DecoderFile()])
    assert len(cat) == 0


def test_definition_has_variables(catalog: DecoderCatalog) -> None:
    """At least one definition in our golden corpus has variables.

    Iteration yields metadata (post-lazy-load refactor) -- resolve each
    to a full definition via ``find`` to check the variables list.
    """
    has_vars = False
    for entry in catalog:
        defn = catalog.find(entry.family, entry.model)
        if defn is not None and defn.variables:
            has_vars = True
            break
    assert has_vars, "expected at least one decoder with variables"


def test_definition_carries_panes_from_decoder_file() -> None:
    """``<pane>`` elements declared in a decoder XML file flow through
    the catalog onto each :class:`DecoderDefinition` so downstream code
    (change tracking, tab UI, batch queues) can iterate them without
    reaching back into the raw ``DecoderFile``."""
    from jmri_core.persistence.decoder_xml import load_decoder_bytes  # noqa: PLC0415
    df = load_decoder_bytes(
        b'<?xml version="1.0"?>'
        b'<decoder-config><decoder>'
        b'<family name="F" mfg="M"><model model="M1"/></family>'
        b'</decoder>'
        b'<pane><name>Custom</name>'
        b'<column><display item="X"/><display item="Y"/></column>'
        b'</pane>'
        b'</decoder-config>'
    )
    cat = DecoderCatalog.from_files([df])
    defn = cat.find("F", "M1")
    assert defn is not None
    assert len(defn.panes) == 1
    assert defn.panes[0].name == "Custom"
    assert defn.panes[0].items == ["X", "Y"]
