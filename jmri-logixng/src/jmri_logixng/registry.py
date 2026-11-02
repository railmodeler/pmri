"""Type registry -- maps ``type_tag`` strings to concrete node classes.

Used by the serializer: a serialized tree carries ``type: <tag>`` fields,
and the deserializer looks up the class by tag, then calls
``cls.from_dict(payload, registry)`` (or a default reflection-based
constructor) to rebuild the node.

The registry is populated at import time by every action/expression
module via :func:`register`. Tests use :func:`snapshot` /
:func:`restore` to isolate registrations.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jmri_logixng.tree import Node


_REGISTRY: dict[str, type[Node]] = {}


def register(cls: type[Node]) -> type[Node]:
    """Register a node class by its ``type_tag``. Usable as a decorator.

    Raises if the tag is already registered to a different class.
    """
    tag = cls.type_tag
    if not tag:
        raise ValueError(f"{cls.__name__}.type_tag must be set before registration")
    existing = _REGISTRY.get(tag)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"type_tag {tag!r} already registered to {existing.__name__}, "
            f"can't re-register to {cls.__name__}"
        )
    _REGISTRY[tag] = cls
    return cls


def lookup(tag: str) -> type[Node]:
    """Find the class for *tag*. Raises ``KeyError`` if unknown."""
    if tag not in _REGISTRY:
        raise KeyError(f"unknown LogixNG type tag: {tag!r}")
    return _REGISTRY[tag]


def known_tags() -> list[str]:
    return sorted(_REGISTRY)


def snapshot() -> dict[str, type[Node]]:
    return dict(_REGISTRY)


def restore(snap: dict[str, type[Node]]) -> None:
    _REGISTRY.clear()
    _REGISTRY.update(snap)
