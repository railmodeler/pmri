"""Node-id-targeted structural edits on a LogixNG tree.

The YAML / JSON serialization layer in :mod:`jmri_logixng.serialize`
round-trips a whole tree. The visual editor in :mod:`jmri_web` needs
finer-grained mutations keyed by :attr:`Node.id`: edit a single node's
fields, append a child under a specific container slot, delete a
single node from its parent.

Rather than put the walker on :class:`Node` (which keeps Node's API
narrow and matches JMRI Java's separation between the ``LogixNG``
model and the ``LogixNG`` editor), the walker lives here and uses
only the public attributes of each node type.

The container slots we model, keyed by ``type_tag``:

* ``logixng:root`` -> ``conditionals`` (list)
* ``logixng:conditional`` -> ``actions`` (list)
* ``action:sequence`` -> ``actions`` (list)
* ``action:if-then-else`` -> ``condition`` (single Expression),
  ``then_action`` (single Action, required),
  ``else_action`` (single Action, optional)
* ``action:set-memory-from-expression`` -> ``source`` (single Expression)
* ``expr:and`` / ``expr:or`` -> ``children`` (list)
* ``expr:not`` -> ``child`` (single Expression)
* ``expr:num-op`` / ``expr:num-compare`` -> ``left``, ``right`` (single)
* ``expr:string-compare`` -> ``left``, ``right`` (single)
* ``expr:string-concat`` -> ``children`` (list)

Every other registered type is treated as a leaf (no children).
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from jmri_logixng.tree import Node


@dataclass(frozen=True)
class SlotRef:
    """A location where a child Node sits inside its parent."""

    parent: Node
    slot: str
    # For list-shaped slots: the 0-based index into the list.
    # For single-shaped slots: None.
    index: int | None = None


# ---- slot catalog ---------------------------------------------------------
#
# Each entry is (list_slots, single_slots) describing a parent type.
# ``list_slots`` is a tuple of attribute names that hold ordered lists of
# children. ``single_slots`` is a tuple of attribute names that hold a
# single child; attribute may be ``None`` for optional single slots.


_LIST_SLOTS: dict[str, tuple[str, ...]] = {
    "logixng:root": ("conditionals",),
    "logixng:conditional": ("actions",),
    "action:sequence": ("actions",),
    "expr:and": ("children",),
    "expr:or": ("children",),
    "expr:string-concat": ("children",),
}

_SINGLE_SLOTS: dict[str, tuple[str, ...]] = {
    "action:if-then-else": ("condition", "then_action", "else_action"),
    "action:set-memory-from-expression": ("source",),
    "expr:not": ("child",),
    "expr:num-op": ("left", "right"),
    "expr:num-compare": ("left", "right"),
    "expr:string-compare": ("left", "right"),
}


def list_slot_names(node: Node) -> tuple[str, ...]:
    return _LIST_SLOTS.get(node.type_tag, ())


def single_slot_names(node: Node) -> tuple[str, ...]:
    return _SINGLE_SLOTS.get(node.type_tag, ())


def is_container(node: Node) -> bool:
    return bool(list_slot_names(node) or single_slot_names(node))


# ---- walking --------------------------------------------------------------


def iter_children(node: Node) -> Iterator[tuple[str, int | None, Node]]:
    """Yield ``(slot_name, list_index_or_None, child_node)`` triples.

    Order mirrors execution order: list slots are iterated in list
    order; multi-single slots are iterated in the declaration order
    above. Skips ``None`` single slots.
    """
    for slot in list_slot_names(node):
        children = getattr(node, slot, []) or []
        for i, child in enumerate(children):
            yield slot, i, child
    for slot in single_slot_names(node):
        child = getattr(node, slot, None)
        if child is not None:
            yield slot, None, child


def find_node(root: Node, node_id: str) -> Node | None:
    """Depth-first search for the node with ``id == node_id``.

    Returns ``None`` if no match; the caller is expected to 404 or
    log depending on context.
    """
    if root.id == node_id:
        return root
    for _slot, _idx, child in iter_children(root):
        found = find_node(child, node_id)
        if found is not None:
            return found
    return None


def find_parent(root: Node, node_id: str) -> SlotRef | None:
    """Locate *node_id*'s parent and the slot it occupies.

    Returns ``None`` when *node_id* is the root itself or isn't in
    the tree.
    """
    for slot, idx, child in iter_children(root):
        if child.id == node_id:
            return SlotRef(parent=root, slot=slot, index=idx)
        inner = find_parent(child, node_id)
        if inner is not None:
            return inner
    return None


# ---- mutations ------------------------------------------------------------


def delete_node(root: Node, node_id: str) -> bool:
    """Remove *node_id* from its parent. Returns True if removed.

    Required single slots that would leave the parent invalid (e.g.
    ``action:if-then-else.condition`` or ``.then_action``) are
    rejected -- the caller should ask the user to replace, not
    delete. Optional single slots (``else_action``) are nulled out.
    List slots just drop the index.
    """
    ref = find_parent(root, node_id)
    if ref is None:
        return False
    parent = ref.parent
    if ref.index is not None:
        getattr(parent, ref.slot).pop(ref.index)
        return True
    # Single slot. Only ``else_action`` on if-then-else is optional
    # in our model; every other single slot is required for
    # evaluation to succeed.
    if parent.type_tag == "action:if-then-else" and ref.slot == "else_action":
        parent.else_action = None
        return True
    raise ValueError(
        f"cannot delete node {node_id!r}: it occupies required slot "
        f"{ref.slot!r} on {parent.type_tag!r}; replace it instead",
    )


def append_child(parent: Node, slot: str, child: Node) -> int:
    """Append *child* at the end of *parent*'s list slot *slot*.

    Returns the index at which the child was inserted. Raises
    :class:`ValueError` for an unknown slot or if *slot* is a single
    slot (use :func:`set_single_slot` instead).
    """
    if slot not in list_slot_names(parent):
        raise ValueError(
            f"{parent.type_tag!r} has no list slot {slot!r}; "
            f"available: {list_slot_names(parent)}",
        )
    children = getattr(parent, slot)
    children.append(child)
    return len(children) - 1


def set_single_slot(parent: Node, slot: str, child: Node | None) -> None:
    """Replace the single-slot *slot* on *parent* with *child*.

    ``None`` clears the slot; the method raises if the slot isn't
    optional on this parent (same rule as :func:`delete_node`).
    """
    if slot not in single_slot_names(parent):
        raise ValueError(
            f"{parent.type_tag!r} has no single slot {slot!r}; "
            f"available: {single_slot_names(parent)}",
        )
    if child is None:
        if parent.type_tag == "action:if-then-else" and slot == "else_action":
            parent.else_action = None
            return
        raise ValueError(
            f"single slot {slot!r} on {parent.type_tag!r} is "
            "required; cannot clear it",
        )
    setattr(parent, slot, child)


__all__ = [
    "SlotRef",
    "append_child",
    "delete_node",
    "find_node",
    "find_parent",
    "is_container",
    "iter_children",
    "list_slot_names",
    "set_single_slot",
    "single_slot_names",
]
