"""``OBlockService`` -- JSON topic ``oblock``.

Wire shape per ``jmri/server/json/oblock/oblock-server.json``.
Round-trip only in this pass. Warrants + portals + paths come with
Panel Editor (Tier 1 #3).
"""
from __future__ import annotations

from typing import Any

from jmri_core.beans.oblock import OBlock, OBlockManager

from jmri_server.services.beans import _BeanService, _common_named_bean


class OBlockService(_BeanService):
    topic = "oblock"

    def manager(self) -> OBlockManager:
        return self.ctx.connection.oblocks

    def project(self, bean: OBlock) -> dict[str, Any]:
        return _common_named_bean(bean) | {
            "status": bean.status,
            "value": bean.value,
            "sensor": bean.sensor_name,
            "warrant": bean.warrant_name,
            "train": bean.train_name,
        }

    async def apply(self, bean: OBlock, data: dict[str, Any]) -> None:
        if "value" in data:
            v = data["value"]
            bean.value = str(v) if v is not None else None
        if "sensor" in data:
            v = data["sensor"]
            bean.sensor_name = str(v) if v is not None else None
        if "warrant" in data:
            v = data["warrant"]
            bean.warrant_name = str(v) if v is not None else None
        if "train" in data:
            v = data["train"]
            bean.train_name = str(v) if v is not None else None
        if "status" in data:
            await bean.set_status(int(data["status"]))
