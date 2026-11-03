"""Auto-identify decoder tests.

Post-2026-07-16 refactor: ``identify_by_cv`` returns metadata entries
(:class:`DecoderIndexEntry`) rather than lazy-loading full definitions.
Full identify was iterating the whole catalog and parsing every
candidate XML file on-demand -- prohibitive on a Pi with the real 2000+
file JMRI tree. Metadata-only filtering avoids that.
"""
from __future__ import annotations

import pytest
from pmri_core.persistence.decoder_xml import (
    DecoderFamily,
    DecoderFile,
    DecoderModel,
    DecoderVariable,
)

from pmri_decoderpro import DecoderCatalog, UnknownDecoderIdentityError


def _make_catalog() -> DecoderCatalog:
    """Synthetic catalog with a couple of mfgs to exercise filtering.

    Uses the exact ``mfg`` strings JMRI's decoderIndex.xml declares --
    the identity lookup compares NMRA mfg names case-insensitively
    against ``entry.mfg``, and real decoder files use the JMRI form.
    """
    files = [
        DecoderFile(
            family=DecoderFamily(
                name="ESU Test", mfg="Electronic Solutions Ulm GmbH",
            ),
            variables=[DecoderVariable(CV="7", item="Version", default="151")],
        ),
        DecoderFile(
            family=DecoderFamily(
                name="ESU Test 2", mfg="Electronic Solutions Ulm GmbH",
            ),
            variables=[DecoderVariable(CV="7", item="Version", default="151")],
        ),
        DecoderFile(
            family=DecoderFamily(name="Digitrax Test", mfg="Digitrax"),
            variables=[DecoderVariable(CV="7", item="Version", default="33")],
        ),
    ]
    for f in files:
        f.family.models.append(DecoderModel(model=f.family.name))
    return DecoderCatalog.from_files(files)


def test_identify_returns_all_entries_for_matching_mfg() -> None:
    """Every metadata entry whose mfg matches the NMRA-id lookup is
    returned. CV7 is no longer used to narrow -- the user picks from
    the returned list."""
    cat = _make_catalog()
    # CV8 = 151 = Electronic Solutions Ulm GmbH (ESU) per JMRI's
    # decoderIndex.xml mfgList.
    matches = cat.identify_by_cv(cv7_version=151, cv8_mfg=151)
    families = {m.family for m in matches}
    assert "ESU Test" in families
    assert "ESU Test 2" in families
    assert "Digitrax Test" not in families


def test_identify_filters_by_manufacturer() -> None:
    cat = _make_catalog()
    # CV8 = 129 = Digitrax per JMRI's mfgList.
    matches = cat.identify_by_cv(cv7_version=33, cv8_mfg=129)
    families = {m.family for m in matches}
    assert families == {"Digitrax Test"}


def test_identify_raises_when_cv8_is_the_nce_no_ack_sentinel() -> None:
    """CV8 = 48 (``0x30``) is the NCE NoAck byte a failed programming-
    track read returns. Old code interpreted 48 as an unknown mfg,
    skipped the mfg filter, then iterated the whole catalog lazy-
    loading every file -- effectively hanging the server. Now we raise
    a clear error so the UI can prompt for a real programming session."""
    cat = _make_catalog()
    with pytest.raises(UnknownDecoderIdentityError):
        cat.identify_by_cv(cv7_version=48, cv8_mfg=48)


def test_identify_raises_for_any_unknown_mfg_id() -> None:
    """Same treatment for any CV8 value we can't map to a manufacturer.
    Returning the full catalog silently was too dangerous -- the caller
    can't distinguish "no match" from "unknown identity"."""
    cat = _make_catalog()
    with pytest.raises(UnknownDecoderIdentityError) as exc:
        cat.identify_by_cv(cv7_version=42, cv8_mfg=200)
    assert exc.value.cv7 == 42
    assert exc.value.cv8 == 200


def test_identify_returns_empty_when_mfg_known_but_no_catalog_entry() -> None:
    """CV8 = 11 (North Coast Engineering / NCE) is a known ID but our
    synthetic catalog has no NCE entries -- legitimately empty result."""
    cat = _make_catalog()
    matches = cat.identify_by_cv(cv7_version=100, cv8_mfg=11)
    assert matches == []
