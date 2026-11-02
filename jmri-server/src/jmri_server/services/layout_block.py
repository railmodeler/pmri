"""``LayoutBlockService`` -- JSON topic ``layoutBlock``.

Wire shape per ``jmri/server/json/layoutblock/layoutBlock-server.json``.
Round-trip only in this pass -- Panel Editor (Tier 1 #3) will layer
real occupancy logic on top of these beans.
"""
from __future__ import annotations

from typing import Any

from jmri_core.beans.layout_block import LayoutBlock, LayoutBlockManager

from jmri_server.services.beans import _BeanService, _common_named_bean


class LayoutBlockService(_BeanService):
    topic = "layoutBlock"

    def manager(self) -> LayoutBlockManager:
        return self.ctx.connection.layout_blocks

    def project(self, bean: LayoutBlock) -> dict[str, Any]:
        return _common_named_bean(bean) | {
            "state": bean.state,
            "block": bean.block,
            "memory": bean.memory,
            "occupancySensor": bean.occupancy_sensor,
            "occupiedSense": bean.occupied_sense,
            "blockColor": bean.block_color,
            "trackColor": bean.track_color,
            "occupiedColor": bean.occupied_color,
            "extraColor": bean.extra_color,
            "useExtraColor": bean.use_extra_color,
        }

    async def apply(self, bean: LayoutBlock, data: dict[str, Any]) -> None:
        # Every field is a straight assignment -- no signal fires here
        # because Panel Editor isn't wired yet. When it lands, this
        # method grows a "notify state_changed" branch for occupancy
        # transitions.
        for wire_field, attr in (
            ("block", "block"),
            ("memory", "memory"),
            ("occupancySensor", "occupancy_sensor"),
            ("blockColor", "block_color"),
            ("trackColor", "track_color"),
            ("occupiedColor", "occupied_color"),
            ("extraColor", "extra_color"),
        ):
            if wire_field in data:
                val = data[wire_field]
                setattr(bean, attr, str(val) if val is not None else None)
        if "occupiedSense" in data:
            bean.occupied_sense = int(data["occupiedSense"])
        if "useExtraColor" in data:
            bean.use_extra_color = bool(data["useExtraColor"])
        if "state" in data:
            bean.state = int(data["state"])
