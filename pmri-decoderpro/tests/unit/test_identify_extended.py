"""Extended-identify tests -- CV7 + CV8 + productID narrowing.

The catalog's :meth:`identify_by_cv_extended` should narrow the
family-level list from :meth:`identify_by_cv` when a productID
matches. When no productID matches (or productID is None), it
falls back to the full family-level list so we never lose a
legitimate CV7/CV8 match.
"""
from __future__ import annotations

from pmri_core.beans.programmer import Programmer, ProgrammingMode
from pmri_core.persistence.decoder_xml import (
    DecoderFamily,
    DecoderFile,
    DecoderModel,
    DecoderVariable,
)

from pmri_decoderpro import (
    DecoderCatalog,
    DecoderDefinition,
    MFG_SOUNDTRAXX,
    ProgrammingSession,
)


def _catalog() -> DecoderCatalog:
    """Two SoundTraxx models sharing a family, with distinct
    productIDs -- the identify picker should be able to narrow to
    one when the probe returns a matching productID."""
    files = [
        DecoderFile(
            family=DecoderFamily(
                name="Tsunami2 Diesel", mfg="SoundTraxx (Throttle-Up)",
            ),
            variables=[
                DecoderVariable(CV="7", item="Version", default="71"),
            ],
        ),
        DecoderFile(
            family=DecoderFamily(
                name="Tsunami2 Steam", mfg="SoundTraxx (Throttle-Up)",
            ),
            variables=[
                DecoderVariable(CV="7", item="Version", default="71"),
            ],
        ),
    ]
    # Attach one distinctive productID per family so identify can
    # pick between them.
    files[0].family.models.append(
        DecoderModel(model="Diesel-A", product_id="4096"),
    )
    files[1].family.models.append(
        DecoderModel(model="Steam-A", product_id="8192"),
    )
    return DecoderCatalog.from_files(files)


def test_extended_identify_narrows_to_matching_product_id() -> None:
    cat = _catalog()
    matches = cat.identify_by_cv_extended(
        cv7_version=71, cv8_mfg=141, product_id=4096,
    )
    assert {m.model for m in matches} == {"Diesel-A"}


def test_extended_identify_falls_back_when_no_product_id_match() -> None:
    """If no entry's productID matches, we return the family-level
    list rather than an empty result. Catalogs that predate a new
    productID must not hide entries that CV7/CV8 already narrowed."""
    cat = _catalog()
    matches = cat.identify_by_cv_extended(
        cv7_version=71, cv8_mfg=141, product_id=9999,
    )
    assert {m.model for m in matches} == {"Diesel-A", "Steam-A"}


def test_extended_identify_none_product_id_returns_full_family() -> None:
    """No probe run (product_id=None) -> identical to identify_by_cv."""
    cat = _catalog()
    matches = cat.identify_by_cv_extended(
        cv7_version=71, cv8_mfg=141, product_id=None,
    )
    assert {m.model for m in matches} == {"Diesel-A", "Steam-A"}


def test_extended_identify_matches_comma_separated_product_ids() -> None:
    """JMRI stores multi-productID models as a comma-separated
    list. A match against any token wins."""
    file = DecoderFile(
        family=DecoderFamily(name="Multi", mfg="SoundTraxx (Throttle-Up)"),
        variables=[DecoderVariable(CV="7", item="Version", default="71")],
    )
    file.family.models.append(
        DecoderModel(model="Multi-Variant", product_id="100,200,300"),
    )
    cat = DecoderCatalog.from_files([file])
    matches = cat.identify_by_cv_extended(
        cv7_version=71, cv8_mfg=141, product_id=200,
    )
    assert {m.model for m in matches} == {"Multi-Variant"}


# ---- read_identity_extended integration --------------------------------


class FakeProgrammer(Programmer):
    def __init__(self) -> None:
        self._mode = ProgrammingMode.SERVICE_DIRECT
        self.cvs: dict[int, int] = {}

    @property
    def mode(self) -> ProgrammingMode:
        return self._mode

    async def read_cv(self, cv: int) -> int:
        return self.cvs.get(cv, 0)

    async def write_cv(self, cv: int, value: int) -> None:
        self.cvs[cv] = value


async def test_read_identity_extended_returns_triple() -> None:
    """A full read_identity_extended returns (cv7, cv8, product_id)
    with the productID coming from the mfg-specific probe."""
    p = FakeProgrammer()
    p.cvs[7] = 71                        # SoundTraxx Tsunami2
    p.cvs[8] = MFG_SOUNDTRAXX            # 141
    # SoundTraxx bit layout: low | ((high & 0x7) << 8) | (highest << 11).
    # highest=2 << 11 = 4096; low=0, high=0 -> productID = 4096.
    p.cvs[253] = 2                       # highest -> bit 11+
    p.cvs[255] = 0                       # high (& 0x7) -> bits 8..10
    p.cvs[256] = 0                       # low -> bits 0..7

    session = ProgrammingSession(
        definition=DecoderDefinition(
            family=DecoderFamily(name="Test", mfg="Test", models=[]),
            model=DecoderModel(model="X"),
            variables=(),
        ),
        programmer=p,
    )
    cv7, cv8, pid = await session.read_identity_extended()
    assert cv7 == 71
    assert cv8 == 141
    assert pid == 4096   # bit layout: low=0 | (high & 0x7)<<8=0x1000 | highest<<11=0


async def test_read_identity_extended_none_for_unknown_mfg() -> None:
    """CV8 not in the probe registry -> productID is None (identify
    falls back to family-level filtering)."""
    p = FakeProgrammer()
    p.cvs[7] = 33
    p.cvs[8] = 129   # Digitrax -- no probe registered
    session = ProgrammingSession(
        definition=DecoderDefinition(
            family=DecoderFamily(name="Test", mfg="Test", models=[]),
            model=DecoderModel(model="X"),
            variables=(),
        ),
        programmer=p,
    )
    _, _, pid = await session.read_identity_extended()
    assert pid is None
