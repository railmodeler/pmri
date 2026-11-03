"""Golden-file XML round-trip suite.

Loads every JMRI-shipped panel sample from ``stage-0/contracts/xml/
samples/`` through our persistence layer, saves it back, and
compares the c14n-canonicalized source vs output. Serves two roles:

1. **Regression guard.** Any change to
   :mod:`pmri_core.persistence.config_xml` that silently corrupts a
   previously-parseable file will fail here.

2. **Divergence log.** Files that don't round-trip byte-for-byte
   today get :func:`pytest.xfail` with the reason recorded inline.
   The xfail list is the punch list for the "byte-equality
   round-trip" deferred item -- each entry names one specific gap
   between our persistence layer and JMRI Java's.

Canonicalization uses lxml's built-in XML c14n so attribute
ordering, whitespace variations, and empty-element style
(``<foo/>`` vs ``<foo></foo>``) don't count as diffs. Only
*semantic* structural differences fail.
"""
from __future__ import annotations

import io
import pathlib

import pytest
from lxml import etree

from pmri_core.persistence.config_xml import load_config_bytes, save_config_bytes

_SAMPLES_DIR = (
    pathlib.Path(__file__).resolve().parents[3]
    / "stage-0" / "contracts" / "xml" / "samples"
)


# Files that don't round-trip byte-equal today. Each entry names
# what specifically differs so a future pass can track progress
# closing them out. Files not in this map must round-trip cleanly.
KNOWN_DIVERGENCES: dict[str, str] = {
    "CrossoverExample.xml": "signalheads / blocks / logix sections preserved opaquely, not modelled",
    "LogixStateMachine.xml": "logix + signalheads sections preserved opaquely",
    "MemoryIconExample.xml": "memory bean modelling incomplete on load path",
    "ReporterIconExample.xml": "reporter icon layout section not modelled",
    "RouteExample.xml": "route bean modelling incomplete on load path",
    "TwoColumnMachine.xml": "logix + panel positionables preserved opaquely",
    "Warrant.xml": "oblock + warrant sections preserved opaquely",
    "javaone/Configuration.xml": "operations config section preserved opaquely",
    "javaone/MainPanel.xml": "panel positionables preserved opaquely",
    "javaone/MonitorPanel.xml": "panel positionables preserved opaquely",
    "javaone/Throttles.xml": "throttle-config XML shape not modelled",
}


def _c14n(xml_bytes: bytes) -> bytes:
    """Canonicalize *xml_bytes* -- normalizes attribute order,
    whitespace between elements, and empty-element style. What
    remains is the semantic structure only."""
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
    tree = etree.parse(io.BytesIO(xml_bytes), parser)
    out = io.BytesIO()
    tree.write_c14n(out)
    return out.getvalue()


def _iter_samples() -> list[pathlib.Path]:
    if not _SAMPLES_DIR.is_dir():
        return []
    return sorted(_SAMPLES_DIR.rglob("*.xml"))


@pytest.mark.parametrize(
    "sample",
    _iter_samples(),
    ids=lambda p: str(p.relative_to(_SAMPLES_DIR)) if p else "no-samples",
)
def test_sample_roundtrips_byte_equal_after_c14n(sample: pathlib.Path) -> None:
    """Load the sample, save it back, canonicalize both, diff."""
    key = str(sample.relative_to(_SAMPLES_DIR)).replace("\\", "/")
    if key in KNOWN_DIVERGENCES:
        pytest.xfail(f"documented divergence: {KNOWN_DIVERGENCES[key]}")

    original = sample.read_bytes()
    cfg = load_config_bytes(original)
    round_tripped = save_config_bytes(cfg)

    original_c14n = _c14n(original)
    round_c14n = _c14n(round_tripped)

    if original_c14n != round_c14n:
        # Emit the diff in the failure message so the operator can
        # audit what changed without hunting for two big files.
        raise AssertionError(
            f"c14n diverges for {key}:\n"
            f"first 500 bytes of original c14n:\n"
            f"{original_c14n[:500]!r}\n\n"
            f"first 500 bytes of round-trip c14n:\n"
            f"{round_c14n[:500]!r}"
        )


def test_samples_dir_populated() -> None:
    """Sanity: the extracted samples must be present. If this test
    fails on a fresh checkout, run the extraction step from
    ``stage-0/README.md`` (copy JMRI sample XMLs into
    ``stage-0/contracts/xml/samples/``)."""
    assert _iter_samples(), (
        f"no XML samples under {_SAMPLES_DIR}; "
        "populate them from JMRI's installed xml/samples/ directory"
    )
