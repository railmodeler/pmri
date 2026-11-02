"""``LogsService`` -- JSON topic ``logs``.

Tails ``JMRI_LOG_FILE`` (when configured) so scripts and the /home page
can surface recent activity without shelling into the Pi. There's no
JMRI-Java precedent; the wire shape is ours to define.

Wire shape (single-object, no ``list``)::

    {
      "path": "/var/log/jmri/jmri.log",   // or null when not configured
      "tail": 20,                          // number of lines returned
      "lines": ["2026-08-04 15:03:07 INFO  [jmri_nce.connection] ...", ...]
    }

Request-side: ``method=get``, optional ``data.tail`` (default 50,
cap 500). Missing log file returns ``lines=[]`` with a ``path=null``
so clients see the "not configured" state explicitly instead of a
generic 404.
"""
from __future__ import annotations

from typing import Any

from jmri_core.log import tail as _tail

from jmri_server.service import JsonService

_DEFAULT_TAIL = 50
_MAX_TAIL = 500


class LogsService(JsonService):
    topic = "logs"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        try:
            requested = int(data.get("tail", _DEFAULT_TAIL))
        except (TypeError, ValueError):
            requested = _DEFAULT_TAIL
        n = max(0, min(requested, _MAX_TAIL))

        path = getattr(self.ctx, "_log_file", None)
        if path is None:
            return {"path": None, "tail": n, "lines": []}
        return {
            "path": str(path),
            "tail": n,
            "lines": _tail(path, n),
        }
