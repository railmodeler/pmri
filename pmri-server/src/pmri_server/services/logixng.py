"""``LogixNGService`` -- JSON topic ``logixng``.

Exposes the :class:`~pmri_logixng.LogixNGManager` registered on the
server context. There's no JMRI-Java precedent for this topic; the
wire shape follows the pattern of the other bean-backed services
(``name`` / ``userName`` / ``comment`` at the top plus type-specific
fields) so consumers can reuse the same envelope handling.

Wire shape::

    {
      "name": "IQ1",
      "userName": "Yard automation",
      "enabled": true,
      "conditionals": [
        {
          "id": "conditional-abc",
          "user_name": "Turn on light when track occupied",
          "enabled": true,
          "actions": [
            {"type": "action:set-turnout", "turnout": "IT1",
             "state": "CLOSED"},
            ...
          ]
        }
      ]
    }

Methods:

* ``list`` -> array of the above.
* ``get`` -> one by ``name``.
* ``put`` -> create-or-replace; the request body carries the whole
  tree under ``tree`` (a nested dict matching the LogixNG YAML shape).
* ``delete`` -> drop by ``name``; also removes the on-disk YAML.
* ``post`` -> verbs on ``command``:
    - ``enable`` / ``disable`` -- flip the ``enabled`` flag on the
      whole program.
    - ``run`` -- execute every conditional in order (fire-and-forget
      for the caller; the service awaits so failures surface).
    - ``run_conditional`` with ``conditional_id`` -- run one.
"""
from __future__ import annotations

import logging
from typing import Any

from pmri_logixng import (
    ExecutionContext,
    LogixNG,
    LogixNGManager,
    append_child,
    delete_node,
    find_node,
    set_single_slot,
    single_slot_names,
)
from pmri_logixng.serialize import from_dict

from pmri_server.service import JsonService, JsonServiceError

_log = logging.getLogger(__name__)


def _preserve_child_slots(old: Any, new: Any) -> None:
    """Carry *old*'s nested-node slots over to *new*.

    When the visual editor edits a container's leaf fields (user_name,
    plus any scalar on the container itself like NumericOp.op), the
    client only re-serializes the LEAF fields -- not the sub-tree. We
    then build *new* from the decoded dict, which starts with empty /
    placeholder children. Copy *old*'s actual children across so the
    tree keeps its shape.

    Called from :meth:`LogixNGService._update_node`. Introspects every
    slot catalogued in :mod:`pmri_logixng.tree_ops`.
    """
    from pmri_logixng.tree_ops import list_slot_names, single_slot_names  # noqa: PLC0415

    for slot in list_slot_names(old):
        existing = getattr(old, slot, None)
        # Only carry over when the caller didn't supply the slot
        # explicitly in the serialized fields (i.e. new has an empty
        # list from the decoder's default).
        if existing is not None and not getattr(new, slot, None):
            setattr(new, slot, list(existing))
    for slot in single_slot_names(old):
        if getattr(new, slot, None) is None:
            carry = getattr(old, slot, None)
            if carry is not None:
                setattr(new, slot, carry)


def _splice_in_place(root: Any, node_id: str, new_node: Any) -> None:
    """Replace the node currently sitting at *node_id* with *new_node*.

    Walks the tree looking for the parent of *node_id* and sets the
    slot to *new_node*. If *node_id* IS the root, mutates the root's
    user_name + any leaf attributes in place; the tree reference
    itself is immutable across replace because manager uses it as the
    key for persistence.
    """
    from pmri_logixng.tree_ops import find_parent, iter_children  # noqa: PLC0415

    if root.id == node_id:
        # Root replacement: just copy the user_name + enabled onto the
        # existing root in place. We can't swap the whole LogixNG
        # instance because the manager holds a reference to it.
        root.user_name = getattr(new_node, "user_name", root.user_name)
        if hasattr(new_node, "enabled"):
            root.enabled = new_node.enabled
        return
    ref = find_parent(root, node_id)
    if ref is None:
        return
    parent = ref.parent
    if ref.index is not None:
        getattr(parent, ref.slot)[ref.index] = new_node
    else:
        setattr(parent, ref.slot, new_node)


def _project(system_name: str, prog: LogixNG) -> dict[str, Any]:
    """Full-tree projection so a mobile client / web page can render
    the whole program without a follow-up call. Matches the tree the
    YAML serializer emits + a ``name`` slot for the system name."""
    return {
        "name": system_name,
        "userName": prog.user_name or None,
        "enabled": prog.enabled,
        "conditionals": [c.to_dict() for c in prog.conditionals],
    }


def _require_manager(ctx: Any) -> LogixNGManager:
    mgr = getattr(ctx, "logix_ng", None)
    if mgr is None:
        raise JsonServiceError(
            503, "LogixNG service unavailable (pmri-logixng not installed)",
        )
    return mgr


class LogixNGService(JsonService):
    topic = "logixng"

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        mgr = _require_manager(self.ctx)
        return [
            _project(name, mgr.get(name))       # type: ignore[arg-type]
            for name in mgr.names()
        ]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        mgr = _require_manager(self.ctx)
        name = self._require_name(data or {})
        prog = mgr.get(name)
        if prog is None:
            raise JsonServiceError(404, f"no logixng named {name!r}")
        return _project(name, prog)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        """Create-or-replace by name. Body may be:

        * an empty program: only ``name`` + optional ``userName``.
        * a full tree: ``tree`` field carrying the nested dict the
          YAML serializer produced.
        """
        mgr = _require_manager(self.ctx)
        data = data or {}
        name = self._require_name(data)
        tree = data.get("tree")
        if tree is None:
            prog = await mgr.create(
                name,
                user_name=str(data.get("userName") or ""),
                enabled=bool(data.get("enabled", True)),
            )
        else:
            if not isinstance(tree, dict):
                raise JsonServiceError(
                    400, "logixng.put tree must be a mapping",
                )
            try:
                node = from_dict(tree)
            except (ValueError, KeyError, TypeError) as e:
                raise JsonServiceError(
                    400, f"logixng tree decode failed: {e}",
                ) from None
            if not isinstance(node, LogixNG):
                raise JsonServiceError(
                    400, "logixng tree must have type=logixng:root",
                )
            prog = await mgr.put(name, node)
        self._persist(mgr, name)
        return _project(name, prog)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        mgr = _require_manager(self.ctx)
        name = self._require_name(data or {})
        if not await mgr.delete(name):
            raise JsonServiceError(404, f"no logixng named {name!r}")
        self._persist_delete(name)
        return {"name": name, "deleted": True}

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        mgr = _require_manager(self.ctx)
        data = data or {}
        name = self._require_name(data)
        prog = mgr.get(name)
        if prog is None:
            raise JsonServiceError(404, f"no logixng named {name!r}")
        command = data.get("command")
        if command == "enable":
            prog.enabled = True
            await mgr.updated.emit(prog)
            self._persist(mgr, name)
        elif command == "disable":
            prog.enabled = False
            await mgr.updated.emit(prog)
            self._persist(mgr, name)
        elif command == "run":
            await self._run(prog)
        elif command == "run_conditional":
            target_id = data.get("conditional_id")
            if not isinstance(target_id, str):
                raise JsonServiceError(
                    400,
                    "logixng.run_conditional requires 'conditional_id'",
                )
            await self._run_one(prog, target_id)
        elif command in ("conditional_enable", "conditional_disable"):
            target_id = data.get("conditional_id")
            if not isinstance(target_id, str):
                raise JsonServiceError(
                    400,
                    f"logixng.{command} requires 'conditional_id'",
                )
            target = next(
                (c for c in prog.conditionals if c.id == target_id),
                None,
            )
            if target is None:
                raise JsonServiceError(
                    404,
                    f"no conditional {target_id!r} in this logixng",
                )
            target.enabled = (command == "conditional_enable")
            await mgr.updated.emit(prog)
            self._persist(mgr, name)
        elif command == "replace_yaml":
            text = data.get("yaml")
            if not isinstance(text, str):
                raise JsonServiceError(
                    400, "logixng.replace_yaml requires 'yaml' (string)",
                )
            # Any parse-time exception surfaces as a 400 to keep the
            # editor UX predictable; the previous tree stays intact
            # because ``load_yaml_text`` builds the new one before the
            # atomic ``put()``.
            try:
                prog = await mgr.load_yaml_text(name, text)
            except Exception as e:  # noqa: BLE001
                raise JsonServiceError(
                    400, f"logixng YAML parse failed: {e}",
                ) from None
            self._persist(mgr, name)
        elif command == "update_node":
            self._update_node(prog, data)
            await mgr.updated.emit(prog)
            self._persist(mgr, name)
        elif command == "add_child":
            self._add_child(prog, data)
            await mgr.updated.emit(prog)
            self._persist(mgr, name)
        elif command == "delete_node":
            self._delete_node(prog, data)
            await mgr.updated.emit(prog)
            self._persist(mgr, name)
        else:
            raise JsonServiceError(400, f"unknown logixng command: {command!r}")
        return _project(name, prog)

    # ---- structural-edit helpers --------------------------------------
    #
    # The visual tree editor in pmri-web drives these via node_id.
    # Each verb goes through the shared from_dict/find_node pipeline so
    # the backing YAML stays authoritative -- after any edit the manager
    # re-saves the whole program, matching how the YAML-editor path
    # works today.

    def _update_node(self, prog: LogixNG, data: dict[str, Any]) -> None:
        """Replace the node at *node_id* with a decoded ``fields`` dict.

        Rather than hand-wire per-type field setters, we rebuild the
        node from a serialized dict via :func:`from_dict` and splice it
        back into the tree at the same slot. That reuses every
        type-specific validation the decoders already encode.
        """
        node_id = data.get("node_id")
        fields = data.get("fields")
        if not isinstance(node_id, str) or not isinstance(fields, dict):
            raise JsonServiceError(
                400,
                "logixng.update_node requires 'node_id' and 'fields'",
            )
        target = find_node(prog, node_id)
        if target is None:
            raise JsonServiceError(
                404, f"no node {node_id!r} in logixng {prog.id!r}",
            )
        # Carry the id across so the UI's node_id references survive.
        new_fields = dict(fields)
        new_fields.setdefault("id", node_id)
        try:
            new_node = from_dict(new_fields)
        except (ValueError, KeyError, TypeError) as e:
            raise JsonServiceError(
                400, f"logixng update_node decode failed: {e}",
            ) from None
        if new_node.type_tag != target.type_tag:
            raise JsonServiceError(
                400,
                f"update_node must preserve type_tag; got "
                f"{new_node.type_tag!r}, expected {target.type_tag!r}. "
                "Use delete + add_child to change a node's kind.",
            )
        # Preserve existing children when the client doesn't resupply
        # them -- the visual editor edits leaf fields in isolation.
        _preserve_child_slots(target, new_node)
        _splice_in_place(prog, node_id, new_node)

    def _add_child(self, prog: LogixNG, data: dict[str, Any]) -> None:
        """Append (list slot) or replace (single slot) a child under
        the node at *parent_id*.

        * ``parent_id``: required.
        * ``slot``: required; must be a known list OR single slot
          name on the parent's type.
        * ``child``: required; a serialized dict decodable by
          :func:`from_dict`.
        """
        parent_id = data.get("parent_id")
        slot = data.get("slot")
        child_fields = data.get("child")
        if (
            not isinstance(parent_id, str)
            or not isinstance(slot, str)
            or not isinstance(child_fields, dict)
        ):
            raise JsonServiceError(
                400,
                "logixng.add_child requires 'parent_id', 'slot', 'child'",
            )
        parent = find_node(prog, parent_id)
        if parent is None:
            raise JsonServiceError(
                404, f"no node {parent_id!r} in logixng {prog.id!r}",
            )
        try:
            child = from_dict(child_fields)
        except (ValueError, KeyError, TypeError) as e:
            raise JsonServiceError(
                400, f"logixng add_child decode failed: {e}",
            ) from None
        if slot in single_slot_names(parent):
            try:
                set_single_slot(parent, slot, child)
            except ValueError as e:
                raise JsonServiceError(400, str(e)) from None
        else:
            try:
                append_child(parent, slot, child)
            except ValueError as e:
                raise JsonServiceError(400, str(e)) from None

    def _delete_node(self, prog: LogixNG, data: dict[str, Any]) -> None:
        node_id = data.get("node_id")
        if not isinstance(node_id, str):
            raise JsonServiceError(
                400, "logixng.delete_node requires 'node_id'",
            )
        try:
            removed = delete_node(prog, node_id)
        except ValueError as e:
            raise JsonServiceError(400, str(e)) from None
        if not removed:
            raise JsonServiceError(
                404, f"no node {node_id!r} in logixng {prog.id!r}",
            )

    # ---- helpers -------------------------------------------------------

    async def _run(self, prog: LogixNG) -> None:
        exec_ctx = ExecutionContext(connection=self.ctx.connection)
        try:
            await prog.execute_all(exec_ctx)
        except Exception as e:  # noqa: BLE001
            _log.exception("LogixNG %s run failed", prog.user_name or "?")
            raise JsonServiceError(500, f"logixng run failed: {e}") from None

    async def _run_one(self, prog: LogixNG, conditional_id: str) -> None:
        target = next(
            (c for c in prog.conditionals if c.id == conditional_id),
            None,
        )
        if target is None:
            raise JsonServiceError(
                404, f"no conditional {conditional_id!r} in this logixng",
            )
        exec_ctx = ExecutionContext(connection=self.ctx.connection)
        try:
            await target.execute(exec_ctx)
        except Exception as e:  # noqa: BLE001
            _log.exception("LogixNG conditional %s run failed", conditional_id)
            raise JsonServiceError(
                500, f"conditional run failed: {e}",
            ) from None

    def _require_name(self, data: dict[str, Any]) -> str:
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise JsonServiceError(400, "logixng request requires 'name'")
        return name

    def _persist(self, mgr: LogixNGManager, name: str) -> None:
        root = getattr(self.ctx, "_logixng_dir", None)
        if root is None:
            return
        mgr.save_one(root, name)

    def _persist_delete(self, name: str) -> None:
        root = getattr(self.ctx, "_logixng_dir", None)
        mgr = _require_manager(self.ctx)
        if root is not None:
            mgr.delete_on_disk(root, name)
