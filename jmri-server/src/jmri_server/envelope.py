"""The JMRI JSON message envelope.

Wire format (see ``contracts/json-schema/schema/json-client.json``):

* Client -> server: ``{type, data?, list?, method?, id?}``
* Server -> client: ``{type, data, method?, id?}``

Where ``method``  in  ``{"get", "put", "post", "delete", "list"}``. ``id`` is
echoed unchanged on responses. The ``list`` shorthand ``{type: "list",
list: "<topic>"}`` is equivalent to ``{type: "<topic>", method: "list"}``.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Method = Literal["get", "put", "post", "delete", "list"]


class JmriRequest(BaseModel):
    """A single client -> server message."""
    model_config = ConfigDict(extra="forbid")

    type: str
    data: dict[str, Any] | None = None
    method: Method | None = None
    list: str | None = None
    id: int | None = None


class JmriResponse(BaseModel):
    """A single server -> client message with payload."""
    model_config = ConfigDict(extra="forbid")

    type: str
    data: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)
    method: Method | None = None
    id: int | None = None


def normalize_request(req: JmriRequest) -> tuple[str, Method, dict[str, Any] | None]:
    """Resolve the ``list`` shorthand and apply the default method.

    Returns ``(type, method, data)`` ready for dispatch.
    """
    if req.type == "list" and req.list is not None:
        # ``{type: "list", list: "<topic>"}`` shorthand -> list of the topic.
        return req.list, "list", req.data
    method: Method = req.method if req.method is not None else "get"
    return req.type, method, req.data
