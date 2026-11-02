"""jmri-decoderpro -- Stage 8 of the JMRI Python rewrite.

Decoder programming catalog + session management. The 881 JMRI decoder
XML files are parsed via :mod:`jmri_core.persistence.decoder_xml` (Stage
2) and exposed through :class:`DecoderCatalog`. A
:class:`ProgrammingSession` binds a decoder definition to a
:class:`Programmer` (from the active SystemConnection) and reads/writes
CVs.
"""
from jmri_core.persistence.programmer_xml import ProgrammerConfig

from jmri_decoderpro.catalog import (
    DecoderCatalog,
    DecoderDefinition,
    DecoderKey,
    UnknownDecoderIdentityError,
)
from jmri_decoderpro.panes import (
    ROSTER_ENTRY_NAME,
    ROSTER_ENTRY_SLUG,
    ComposedPane,
    compose_panes,
    find_pane_by_slug,
    slugify_pane_name,
)
from jmri_decoderpro.probes import (
    MFG_DIETZ,
    MFG_DIY,
    MFG_DOEHLER,
    MFG_ESU,
    MFG_HARMAN,
    MFG_HORNBY,
    MFG_QSI,
    MFG_SOUNDTRAXX,
    MFG_TCS,
    MFG_TRAINOMATIC,
    MFG_ZIMO,
    PROBES,
    probe_dietz,
    probe_diy,
    probe_doehler,
    probe_esu,
    probe_harman,
    probe_hornby,
    probe_qsi,
    probe_soundtraxx,
    probe_tcs,
    probe_trainomatic,
    probe_zimo,
    read_product_id,
)
from jmri_decoderpro.session import CvCacheEntry, ProgrammingSession

__version__ = "0.1.0"

__all__ = [
    "MFG_DIETZ",
    "MFG_DIY",
    "MFG_DOEHLER",
    "MFG_ESU",
    "MFG_HARMAN",
    "MFG_HORNBY",
    "MFG_QSI",
    "MFG_SOUNDTRAXX",
    "MFG_TCS",
    "MFG_TRAINOMATIC",
    "MFG_ZIMO",
    "PROBES",
    "ROSTER_ENTRY_NAME",
    "ROSTER_ENTRY_SLUG",
    "ComposedPane",
    "CvCacheEntry",
    "DecoderCatalog",
    "DecoderDefinition",
    "DecoderKey",
    "ProgrammerConfig",
    "ProgrammingSession",
    "UnknownDecoderIdentityError",
    "__version__",
    "compose_panes",
    "find_pane_by_slug",
    "probe_dietz",
    "probe_diy",
    "probe_doehler",
    "probe_esu",
    "probe_harman",
    "probe_hornby",
    "probe_qsi",
    "probe_soundtraxx",
    "probe_tcs",
    "probe_trainomatic",
    "probe_zimo",
    "read_product_id",
    "slugify_pane_name",
]
