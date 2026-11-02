"""Decoder catalog -- lightweight index + lazy full-definition loading.

Loading JMRI's 2000+ decoder XML files is too slow when done eagerly
(parsing every variable, pane, and enum choice up front). Instead the
catalog holds only a ``(family, model) -> source_file`` metadata index
built by :func:`iter_decoder_index`, and calls :func:`load_decoder` on
demand the first time a specific definition is needed.

Trade-offs:

* ``__iter__`` yields :class:`DecoderIndexEntry` metadata records --
  fast enumeration for enumerate/filter/list-models workflows.
* :meth:`find` returns a :class:`DecoderDefinition` -- triggers the
  lazy full-file parse for that entry (once; cached).
* :meth:`identify_by_cv` iterates metadata + calls ``find`` for each
  candidate. Rare user action, so the load-per-candidate is acceptable.

The ``from_files`` classmethod is retained for tests that supply
already-parsed :class:`DecoderFile` objects -- those populate the cache
immediately so no lazy load fires.
"""
from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterator
from dataclasses import dataclass

from lxml import etree

from jmri_core.persistence.decoder_xml import (
    DecoderFamily,
    DecoderFile,
    DecoderIndexEntry,
    DecoderModel,
    DecoderPane,
    DecoderVariable,
    iter_decoder_index,
    load_decoder,
)
from jmri_core.persistence.programmer_xml import (
    ProgrammerConfig,
    iter_programmer_configs,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecoderKey:
    """Family + model identity, used as a stable lookup key."""

    family: str
    model: str

    def __str__(self) -> str:
        return f"{self.family} / {self.model}"


@dataclass(frozen=True)
class DecoderDefinition:
    """One decoder model's fully parsed definition.

    Constructed on demand by :meth:`DecoderCatalog.find` from the source
    XML file recorded in the catalog's metadata index. ``panes`` carries
    the decoder-specific tabs; the standard tabs are layered on top at
    UI-render time.
    """

    family: DecoderFamily
    model: DecoderModel
    variables: tuple[DecoderVariable, ...]
    panes: tuple[DecoderPane, ...] = ()
    source_file: pathlib.Path | None = None

    @property
    def key(self) -> DecoderKey:
        return DecoderKey(family=self.family.name, model=self.model.model)

    @property
    def mfg(self) -> str:
        return self.family.mfg


class DecoderCatalog:
    """Fast metadata index + lazy full-definition loader.

    Two internal maps:

    * ``_metadata`` -- ``DecoderKey -> DecoderIndexEntry`` from the fast scan
    * ``_cache``    -- ``DecoderKey -> DecoderDefinition`` filled on demand

    ``programmers`` holds the standard programmer definitions
    (``Comprehensive.xml``, ``Basic.xml``, ...) parsed from the sibling
    ``programmers/`` dir. Those are small and load eagerly.
    """

    def __init__(self) -> None:
        self._metadata: dict[DecoderKey, DecoderIndexEntry] = {}
        self._cache: dict[DecoderKey, DecoderDefinition] = {}
        self.programmers: dict[str, ProgrammerConfig] = {}
        # NMRA CV8 manufacturer ID -> mfg name, loaded from
        # ``decoderIndex.xml``'s ``<mfgList>`` at :meth:`from_directory`.
        # Falls back to :data:`_NMRA_MFG_IDS` when the catalog wasn't
        # loaded from a JMRI xml tree (tests, standalone probes).
        self.mfg_ids: dict[int, str] = {}

    # ----- construction ---------------------------------------------------

    @classmethod
    def from_directory(cls, root: str | pathlib.Path) -> DecoderCatalog:
        """Build the catalog by scanning *root* for decoder XML files.

        Only the identity fields are extracted at this stage -- variables
        + panes are parsed lazily via :meth:`find`. The sibling
        ``programmers/`` directory (if present) is loaded eagerly since
        it's small.
        """
        catalog = cls()
        for entry in iter_decoder_index(root):
            catalog._metadata[DecoderKey(entry.family, entry.model)] = entry
        # decoderIndex.xml sits at the ``xml/`` root next to the
        # ``decoders/`` dir and carries the authoritative NMRA
        # manufacturer table (currently 179 entries). Load it if
        # present so identify can map any CV8 the decoder returns.
        index_path = pathlib.Path(root).parent / "decoderIndex.xml"
        if index_path.is_file():
            catalog.mfg_ids = _load_mfg_ids(index_path)
        programmers_dir = pathlib.Path(root).parent / "programmers"
        if programmers_dir.is_dir():
            catalog.programmers = {
                pc.name: pc for pc in iter_programmer_configs(programmers_dir)
            }
            _log.info(
                "DecoderCatalog: indexed %d entries + %d programmer configs",
                len(catalog), len(catalog.programmers),
            )
        else:
            _log.info("DecoderCatalog: indexed %d entries", len(catalog))
        return catalog

    @classmethod
    def from_files(cls, files: list[DecoderFile]) -> DecoderCatalog:
        """Build the catalog from already-parsed :class:`DecoderFile`
        objects. Used by tests + callers that want eager loading.

        Populates both the metadata index and the definition cache so
        subsequent :meth:`find` calls don't trigger any file I/O.
        """
        catalog = cls()
        for f in files:
            catalog._absorb(f)
        return catalog

    def add_index_entry(self, entry: DecoderIndexEntry) -> None:
        """Register a scanned :class:`DecoderIndexEntry` in the metadata
        index. Full-definition parse is deferred to first :meth:`find`.

        Used by the web catalog-load worker so it can drive the
        thermometer at scan time rather than parse time.
        """
        self._metadata[DecoderKey(entry.family, entry.model)] = entry

    def _absorb(self, df: DecoderFile) -> None:
        """Populate both metadata and cache from an already-parsed file."""
        if df.family is None:
            return
        for model in df.family.models:
            key = DecoderKey(df.family.name, model.model)
            self._metadata[key] = DecoderIndexEntry(
                family=df.family.name,
                mfg=df.family.mfg,
                model=model.model,
                source_file=df.path or pathlib.Path(""),
                # Propagate the ``<model productID="..."/>`` attribute
                # so identify_by_cv_extended can narrow past the
                # family level. Missing on tests / older files stays
                # empty string, which the extended-identify path
                # treats as "no productID match, keep in fallback".
                product_id=getattr(model, "product_id", ""),
            )
            self._cache[key] = DecoderDefinition(
                family=df.family,
                model=model,
                variables=tuple(df.variables),
                panes=tuple(df.panes),
                source_file=df.path,
            )

    # ----- lookup ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._metadata)

    def __iter__(self) -> Iterator[DecoderIndexEntry]:
        """Iterate metadata entries -- fast, no full parse.

        Callers that need variables/panes should use :meth:`find` on the
        entry's key.
        """
        return iter(self._metadata.values())

    def get(self, key: DecoderKey) -> DecoderDefinition | None:
        return self._lazy_load(key)

    def find(self, family: str, model: str) -> DecoderDefinition | None:
        return self._lazy_load(DecoderKey(family=family, model=model))

    def _lazy_load(self, key: DecoderKey) -> DecoderDefinition | None:
        """Return the cached definition, parsing the source file on first hit."""
        if key not in self._metadata:
            return None
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        meta = self._metadata[key]
        source = meta.source_file
        try:
            df = load_decoder(source)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "DecoderCatalog: lazy-load %s (%s/%s) failed: %s",
                source, key.family, key.model, e,
            )
            return None
        # One decoder file typically declares many models -- cache them
        # all, so a load triggered by find(F, M1) also serves find(F, M2)
        # without re-parsing the file.
        self._absorb(df)
        return self._cache.get(key)

    # ----- fast metadata queries -----------------------------------------

    def manufacturers(self) -> list[str]:
        return sorted({e.mfg for e in self._metadata.values() if e.mfg})

    def families(self, mfg: str | None = None) -> list[str]:
        return sorted({
            e.family for e in self._metadata.values()
            if e.family and (mfg is None or e.mfg == mfg)
        })

    def models(self, family: str) -> list[str]:
        return sorted({
            e.model for e in self._metadata.values() if e.family == family
        })

    # ----- identification -------------------------------------------------

    def identify_by_cv(
        self, cv7_version: int, cv8_mfg: int,
    ) -> list[DecoderIndexEntry]:
        """Return metadata entries consistent with CV7/CV8 values.

        Metadata-only (no lazy load): iterating a full catalog and
        parsing every candidate file is prohibitively slow on modest
        hardware -- the JMRI decoder tree has ~2000 files, each with
        xi:include fragments to resolve. Instead we filter by mfg alone
        (from :attr:`mfg_ids` when the catalog was loaded from a JMRI
        xml tree, or the small stopgap :data:`_NMRA_MFG_IDS` when it
        wasn't) and let the caller pick a specific entry to fully load
        via :meth:`find`.

        Raises :class:`UnknownDecoderIdentityError` when *cv8_mfg* isn't
        a known NMRA manufacturer ID -- that includes the NCE NoAck
        sentinel byte (``0x30`` = 48) that a failed programming-track
        read returns. Surfacing the error is much friendlier than
        silently returning "no matches" or, worse, iterating the whole
        catalog when the read failed.
        """
        mfg_name = self.mfg_ids.get(cv8_mfg) or _NMRA_MFG_IDS.get(cv8_mfg)
        if mfg_name is None:
            raise UnknownDecoderIdentityError(cv7_version, cv8_mfg)
        # Match case-insensitively AND on the leading token so
        # "SoundTraxx (Throttle-Up)" matches entries stored as either
        # form -- decoderIndex.xml uses the parenthesised name; some
        # older decoder files use the short form.
        needle = mfg_name.split(" (")[0].lower()
        # Filter by CV7 within the model's declared version range.
        # JMRI decoder XMLs use ``lowVersionID`` / ``highVersionID`` on
        # ``<family>`` (and less often on ``<model>``) to gate which
        # decoder revisions a definition applies to. Models with no
        # declared range stay wildcard (0..255) so old files without
        # the attributes aren't hidden from the identify list.
        return [
            entry for entry in self._metadata.values()
            if needle in entry.mfg.lower()
            and entry.low_version_id <= cv7_version <= entry.high_version_id
        ]

    def identify_by_cv_extended(
        self, cv7_version: int, cv8_mfg: int, product_id: int | None,
    ) -> list[DecoderIndexEntry]:
        """Same as :meth:`identify_by_cv`, then narrow by *product_id*.

        When *product_id* is ``None`` (no probe run, or the probe
        determined the model doesn't populate the productID CVs),
        this returns the same list as :meth:`identify_by_cv` --
        the caller shows the family-level picker and the user
        chooses the model.

        When *product_id* is an int, entries whose
        :attr:`~jmri_core.persistence.decoder_xml.DecoderIndexEntry.product_id`
        (the ``<model productID="..."/>`` XML attribute) matches are
        preferred. JMRI stores comma-separated productID lists on
        one model when the same XML applies to several variants --
        we split and match against each. Entries without a
        productID attribute are always kept in the fallback list
        so no CV7/CV8 match is ever lost.

        If any entry has a productID match, only those matches are
        returned. If none do, we fall back to the CV7/CV8-only list
        -- this handles cases like a catalog scan that missed newer
        productIDs the operator's decoder actually reports.
        """
        base = self.identify_by_cv(cv7_version, cv8_mfg)
        if product_id is None:
            return base
        product_id_str = str(product_id)
        matches = [
            entry for entry in base
            if _model_matches_product_id(entry.product_id, product_id_str)
        ]
        return matches if matches else base


def _model_matches_product_id(entry_field: str, needle: str) -> bool:
    """Return True when *needle* matches any productID token in
    *entry_field*. JMRI decoder XML stores multi-productID models as
    a comma-separated list (``"12345,12346"``); a single-token match
    is enough."""
    if not entry_field:
        return False
    return any(
        token.strip() == needle for token in entry_field.split(",")
    )


class UnknownDecoderIdentityError(Exception):
    """CV7/CV8 read back values we can't map to a known decoder.

    Distinct from "we know the mfg but have no matching decoder in the
    catalog" (which returns an empty list). This is raised when we
    can't even interpret the identity bytes -- most often because the
    read failed on the wire and the CS returned its NoAck sentinel.
    """

    # CV8 values that almost always mean "the physical read failed"
    # rather than a real (but unrecognised) mfg ID:
    #   0   -- NMRA reserved / decoder didn't respond
    #   48  -- NCE PowerPro NoAck sentinel byte (0x30)
    #   255 -- 0xFF, common "read timed out" fill
    _WIRE_FAIL_SENTINELS = {0, 48, 255}

    def __init__(self, cv7: int, cv8: int) -> None:
        if cv8 in self._WIRE_FAIL_SENTINELS:
            hint = (
                f"CV8={cv8} looks like a failed wire read (0/48/255 are the "
                "common no-decoder / no-ack sentinels). Retry, or verify the "
                "decoder is on the programming track and powered."
            )
        else:
            hint = (
                f"CV8={cv8} is not a known NMRA manufacturer ID. The catalog "
                "may not be loaded (Load catalog above), or this is a very "
                "new / obscure decoder."
            )
        super().__init__(f"CV7={cv7} CV8={cv8} -- {hint}")
        self.cv7 = cv7
        self.cv8 = cv8


def _matches_cv7(defn: DecoderDefinition, target: int) -> bool:
    """Return True if the decoder's CV7 'Decoder Version' default == *target*.

    Many decoder definitions don't pin CV7 (they declare it as a
    read-only identity variable); in those cases we accept the match
    rather than excluding the candidate. The CV8 manufacturer match is
    the more reliable signal.
    """
    for variable in defn.variables:
        if variable.cv != "7":
            continue
        if variable.default is None:
            return True
        return variable.default == target
    return True   # CV7 unspecified -- leave open


# Top-N NMRA manufacturer IDs from the public registry. Used only as a
# best-effort filter; missing IDs just fall through to the unfiltered
# (CV7-only) match. When the catalog is loaded from a JMRI xml tree
# via :meth:`DecoderCatalog.from_directory`, the authoritative table
# (179 manufacturers) is loaded from ``decoderIndex.xml`` and used in
# preference to this stopgap. IDs and names below match the JMRI
# ``<mfgList>`` -- do not "correct" without cross-checking that file.
_NMRA_MFG_IDS: dict[int, str] = {
    1:   "CML Systems",
    11:  "North Coast Engineering",
    54:  "TCH Technology",
    97:  "Doehler und Haass",
    99:  "Lenz",
    101: "Bachmann Trains",
    113: "QSIndustries",
    127: "Atlas",
    129: "Digitrax",
    131: "Trix Modelleisenbahn",
    141: "SoundTraxx (Throttle-Up)",
    143: "MRC",
    145: "Zimo",
    151: "Electronic Solutions Ulm GmbH",
    153: "Train Control Systems",
    161: "Roco Modelspielwaren",
    165: "MERG",
}


def _load_mfg_ids(index_path: pathlib.Path) -> dict[int, str]:
    """Parse the ``<mfgList>`` block of ``decoderIndex.xml``.

    Returns ``{mfgID -> mfg name}`` for every ``<manufacturer>`` entry.
    Malformed IDs are skipped rather than raised -- JMRI's file has
    been solid for years, but keeping the loader tolerant means a
    single bad entry can't blank the whole identify feature.
    """
    try:
        root = etree.parse(str(index_path)).getroot()
    except (etree.XMLSyntaxError, OSError) as e:
        _log.warning("failed to load %s: %s", index_path, e)
        return {}
    ids: dict[int, str] = {}
    for entry in root.iter("manufacturer"):
        raw_id = entry.get("mfgID", "")
        name = entry.get("mfg", "")
        if not name:
            continue
        try:
            ids[int(raw_id)] = name
        except ValueError:
            continue
    _log.info(
        "DecoderCatalog: loaded %d NMRA manufacturer entries from %s",
        len(ids), index_path.name,
    )
    return ids
