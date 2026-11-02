"""``LocoCvState`` -- per-locomotive CV state.

JMRI stores decoder CV values per locomotive, typically inside the
``<values>`` block of ``roster/<id>.xml`` or (in some installs) as a
separate file at ``rosters/<id>.xml``. Either way, the conceptual
model is *one decoder's CVs as a map keyed by CV number*.

We model the standalone file (``rosters/<id>.xml``) and let
:class:`~jmri_server.loco_cv_store.LocoCvStore` lazy-load it when the
programmer page opens for a specific loco. The roster page itself
doesn't read this file -- that lazy load is the point.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LocoCvState(BaseModel):
    """Decoder CV state for one locomotive.

    Keys are CV names (strings, because JMRI uses ``"17.18"`` notation
    for split CVs and similar non-integer cases). Values are byte
    integers 0..255.
    """

    model_config = ConfigDict(populate_by_name=True)

    loco_id: str
    cv_values: dict[str, int] = Field(default_factory=dict)
