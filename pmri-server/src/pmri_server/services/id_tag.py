"""``IdTagService`` -- JSON topic ``idTag``.

Wire shape per ``jmri/server/json/idtag/idTag-server.json``. Read-only
from the layout's perspective: the ``report_seen`` / ``report_unseen``
transitions are driven by reporter beans, not by JSON post. But the
JSON contract does support a post that sets the whole state manually
(scripts occasionally do this for simulation), so we honour it.
"""
from __future__ import annotations

from typing import Any

from pmri_core.beans.id_tag import (
    ID_TAG_STATE_SEEN,
    ID_TAG_STATE_UNKNOWN,
    ID_TAG_STATE_UNSEEN,
    IdTag,
    IdTagManager,
)

from pmri_server.service import JsonServiceError
from pmri_server.services.beans import _BeanService, _common_named_bean


class IdTagService(_BeanService):
    topic = "idTag"

    def manager(self) -> IdTagManager:
        return self.ctx.connection.id_tags

    def project(self, bean: IdTag) -> dict[str, Any]:
        return _common_named_bean(bean) | {
            "state": bean.state,
            "reporter": bean.reporter_name,
            "time": bean.time,
        }

    async def apply(self, bean: IdTag, data: dict[str, Any]) -> None:
        # Scripts + tests can set state + reporter + time directly. The
        # reporter reference is opaque (a bean name string); the JSON
        # layer doesn't cross-check against the reporter manager
        # because a report can name a reporter on a different profile.
        if "state" in data:
            state = int(data["state"])
            if state not in (
                ID_TAG_STATE_UNKNOWN,
                ID_TAG_STATE_SEEN,
                ID_TAG_STATE_UNSEEN,
            ):
                raise JsonServiceError(
                    400, f"invalid idTag state: {state}",
                )
            when = str(data.get("time") or "")
            reporter = data.get("reporter")
            if state == ID_TAG_STATE_SEEN:
                if not isinstance(reporter, str) or not reporter:
                    raise JsonServiceError(
                        400,
                        "idTag.state=SEEN requires a 'reporter' name",
                    )
                await bean.report_seen(reporter, when)
            elif state == ID_TAG_STATE_UNSEEN:
                await bean.report_unseen(when)
            else:
                # UNKNOWN: reset everything.
                bean._state = state  # noqa: SLF001 -- internal reset
                bean._reporter_name = None  # noqa: SLF001
                bean._time = None  # noqa: SLF001
                await bean.state_changed.emit(state)
