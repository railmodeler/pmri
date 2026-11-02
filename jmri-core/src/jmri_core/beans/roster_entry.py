"""``RosterEntry`` -- Pydantic data model for a stored locomotive.

Mirrors the fields in ``contracts/xml/schema/roster.xsd``. XML serialization
lives in :mod:`jmri_core.persistence.roster_xml` so this module stays purely
declarative.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FunctionLabel(BaseModel):
    """One function-key label (e.g. ``F2 -> "Whistle"``)."""

    model_config = ConfigDict(populate_by_name=True)

    number: int = Field(ge=0, le=31)
    label: str = ""
    lockable: bool = True
    image: str | None = None
    selected_image: str | None = None


class SoundLabel(BaseModel):
    """One sound-event label."""

    number: int = Field(ge=0)
    label: str = ""


class RosterEntry(BaseModel):
    """A roster entry -- what JMRI's ``RosterEntry`` carries."""

    model_config = ConfigDict(populate_by_name=True)

    # Identity
    id: str
    file_name: str = ""

    # DCC
    dcc_address: str = "3"
    long_address: bool = False
    protocol: str = "dcc_short"

    # Decoder identity
    decoder_family: str = ""
    decoder_model: str = ""
    decoder_comment: str = ""

    # Display
    road_name: str = ""
    road_number: str = ""
    mfg: str = ""
    owner: str = ""
    model: str = ""
    comment: str = ""

    # Operations
    max_speed_pct: int = Field(default=100, ge=0, le=100)

    # Imagery
    image_file_path: str | None = None
    icon_file_path: str | None = None
    url: str | None = None
    date_modified: str | None = None

    # Function & sound labels
    function_labels: list[FunctionLabel] = Field(default_factory=list)
    sound_labels: list[SoundLabel] = Field(default_factory=list)

    # Free-form attributes (JMRI <attributepairs>)
    attributes: dict[str, str] = Field(default_factory=dict)

    # Unmodeled child XML -- for elements like ``<values>`` (CV state),
    # ``<dateUpdated>``, custom developer/manufacturer fields, etc. that
    # JMRI authors but we don't yet have typed bindings for. Stored as
    # serialized XML strings so the model stays JSON-friendly. Preserved
    # on round-trip so a load -> save doesn't destroy decoder CV state.
    preserved_xml: list[str] = Field(default_factory=list)
