"""``LayoutBlock`` -- a track segment on the panel that a layout-editor
diagram references.

This is intentionally a **round-trip bean** for the current pass. A
real LayoutBlock in JMRI Java participates in occupancy propagation
across turnout paths, block-boss signalling, and layout-editor
rendering -- none of which exist in the Python rewrite yet. What we
carry here is enough to survive `PanelPro.xml` load/save and to
answer the JSON schema so JMRI mobile clients don't get 404s when
they probe the ``layoutBlock`` topic.

When Panel Editor lands (Tier 1 #3 in ``~/workitems.txt``) this file
grows real logic; today it's honest scaffolding.
"""
from __future__ import annotations

from jmri_core.bean import AbstractNamedBean
from jmri_core.manager import Manager

# Default panel colours JMRI uses when it renders a layout block.
# Strings so the wire form pins the wire form; Layout Editor is what
# consumes them later.
_DEFAULT_BLOCK_COLOR = "black"
_DEFAULT_TRACK_COLOR = "black"
_DEFAULT_OCCUPIED_COLOR = "red"
_DEFAULT_EXTRA_COLOR = "white"


class LayoutBlock(AbstractNamedBean):
    """One layout-editor block. Occupancy / adjacency logic is Panel-Editor
    territory; here we're a data holder."""

    __slots__ = (
        "block",             # underlying Block bean name (opaque string)
        "block_color",
        "extra_color",
        "memory",            # associated Memory bean name (opaque string)
        "occupancy_sensor",  # associated Sensor bean name (opaque string)
        "occupied_color",
        "occupied_sense",    # sensor state that means "occupied" (usually 2)
        "state",             # opaque int the layout-editor uses
        "track_color",
        "use_extra_color",
    )

    type_letter = "B"

    def __init__(
        self, system_name: str, user_name: str | None = None,
    ) -> None:
        super().__init__(system_name, user_name)
        self.state: int = 0
        self.block: str | None = None
        self.memory: str | None = None
        self.occupancy_sensor: str | None = None
        # JMRI Java's default: ACTIVE == 2 == occupied.
        self.occupied_sense: int = 2
        self.block_color: str = _DEFAULT_BLOCK_COLOR
        self.track_color: str = _DEFAULT_TRACK_COLOR
        self.occupied_color: str = _DEFAULT_OCCUPIED_COLOR
        self.extra_color: str = _DEFAULT_EXTRA_COLOR
        self.use_extra_color: bool = False


class LayoutBlockManager(Manager[LayoutBlock]):
    # JMRI uses ``ILB``-prefixed system names for layout blocks; the
    # type letter is ``B`` (same as Block) but the manager is distinct.
    # Callers pass fully-qualified system names -- ``normalize_name`` will
    # accept ``"ILB1"`` unchanged when we're constructed with prefix
    # ``"IL"``.
    type_letter = "B"

    def _create(self, system_name: str) -> LayoutBlock:
        return LayoutBlock(system_name)
