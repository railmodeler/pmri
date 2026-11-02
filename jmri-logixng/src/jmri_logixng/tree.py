"""LogixNG tree node ABCs.

A LogixNG program is a tree of :class:`Node` instances. There are three
flavors:

* :class:`Action` -- does something (mutates layout state).
* :class:`Expression[T]` -- evaluates to a value of type *T*. The most common
  variant is ``Expression[bool]`` (a "digital expression"); analog
  expressions return floats and string expressions return strings.
* :class:`Node` -- the common base. Both Actions and Expressions are Nodes.

Containers like :class:`ConditionalNG` and :class:`LogixNG` themselves
satisfy the Action ABC so they can be nested.

Every node:
  * has a stable ``id`` used for serialization and event correlation;
  * has an optional ``user_name`` for human-readable identification;
  * declares a class-level ``type_tag`` used by the serializer.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from jmri_logixng.context import ExecutionContext


class Node(ABC):  # noqa: B024 -- abstract by intent; the type_tag rule is enforced
    """Common base for every LogixNG element."""

    type_tag: ClassVar[str] = ""   # subclasses must set

    def __init__(self, *, user_name: str = "", node_id: str | None = None) -> None:
        if not self.type_tag:
            raise TypeError(f"{type(self).__name__}.type_tag must be set on the class")
        self.user_name = user_name
        self.id = node_id or f"{type(self).__name__}-{uuid.uuid4().hex[:8]}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this node + its children to a dict suitable for YAML/JSON.

        Default implementation captures ``type``, ``id``, ``user_name``.
        Subclasses extend by overriding :meth:`extra_fields`.
        """
        out: dict[str, Any] = {"type": self.type_tag, "id": self.id}
        if self.user_name:
            out["user_name"] = self.user_name
        out.update(self.extra_fields())
        return out

    def extra_fields(self) -> dict[str, Any]:
        """Hook for subclasses to add their own fields to ``to_dict``."""
        return {}

    def __repr__(self) -> str:
        if self.user_name:
            return f"<{type(self).__name__} {self.user_name!r}>"
        return f"<{type(self).__name__} {self.id}>"


class Action(Node):
    """A node that *does* something when executed."""

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> None: ...


class Expression[T](Node):
    """A node that evaluates to a value of type *T*.

    The most common variant is ``Expression[bool]``. Concrete subclasses
    implement :meth:`evaluate`.
    """

    @abstractmethod
    async def evaluate(self, ctx: ExecutionContext) -> T: ...
