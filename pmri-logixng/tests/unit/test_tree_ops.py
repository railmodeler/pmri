"""tree_ops -- node-id-targeted structural edits on a LogixNG tree."""
from __future__ import annotations

import pytest

from pmri_logixng import (
    And,
    ConditionalNG,
    ConstantBool,
    IfThenElse,
    LogAction,
    LogixNG,
    Not,
    Sequence,
    SensorIsActive,
    SetTurnoutState,
)
from pmri_logixng.tree_ops import (
    append_child,
    delete_node,
    find_node,
    find_parent,
    is_container,
    iter_children,
    list_slot_names,
    set_single_slot,
    single_slot_names,
)
from pmri_core.state import TurnoutState


def _tree() -> LogixNG:
    """Representative tree with container variety:

    root
      cond-1
        if-then-else "it1"
          cond: And "a1"
            SensorIsActive "s1"
            SensorIsActive "s2"
          then: Sequence "seq"
            SetTurnoutState "st1"
            LogAction "log1"
          else: LogAction "log2"
      cond-2
        LogAction "log3"
    """
    s1 = SensorIsActive(sensor="IS1", node_id="s1")
    s2 = SensorIsActive(sensor="IS2", node_id="s2")
    a1 = And([s1, s2], node_id="a1")
    st1 = SetTurnoutState(
        turnout="IT1", state=TurnoutState.CLOSED, node_id="st1",
    )
    log1 = LogAction(message="in then", node_id="log1")
    seq = Sequence([st1, log1], node_id="seq")
    log2 = LogAction(message="in else", node_id="log2")
    it1 = IfThenElse(
        condition=a1, then_action=seq, else_action=log2, node_id="it1",
    )
    cond1 = ConditionalNG([it1], node_id="cond-1")
    log3 = LogAction(message="in cond2", node_id="log3")
    cond2 = ConditionalNG([log3], node_id="cond-2")
    return LogixNG([cond1, cond2], node_id="root")


# ---- walking ------------------------------------------------------------


def test_find_node_hits_every_depth() -> None:
    root = _tree()
    for node_id in ("root", "cond-1", "it1", "a1", "s1", "s2",
                    "seq", "st1", "log1", "log2", "cond-2", "log3"):
        found = find_node(root, node_id)
        assert found is not None and found.id == node_id


def test_find_node_misses_return_none() -> None:
    assert find_node(_tree(), "no-such-id") is None


def test_find_parent_locates_list_index() -> None:
    root = _tree()
    ref = find_parent(root, "s2")
    assert ref is not None
    assert ref.parent.id == "a1"
    assert ref.slot == "children"
    assert ref.index == 1


def test_find_parent_locates_single_slot() -> None:
    root = _tree()
    ref = find_parent(root, "a1")
    assert ref is not None
    assert ref.parent.id == "it1"
    assert ref.slot == "condition"
    assert ref.index is None


def test_find_parent_of_root_is_none() -> None:
    assert find_parent(_tree(), "root") is None


# ---- slot catalog ------------------------------------------------------


def test_is_container_identifies_the_right_set() -> None:
    root = _tree()
    assert is_container(root)                            # logixng:root
    assert is_container(find_node(root, "cond-1"))       # conditional
    assert is_container(find_node(root, "it1"))          # if-then-else
    assert is_container(find_node(root, "seq"))          # sequence
    assert is_container(find_node(root, "a1"))           # expr:and
    # Leaves
    assert not is_container(find_node(root, "s1"))
    assert not is_container(find_node(root, "st1"))
    assert not is_container(find_node(root, "log1"))


def test_slot_names_match_execution_order() -> None:
    root = _tree()
    assert list_slot_names(root) == ("conditionals",)
    assert single_slot_names(root) == ()
    it1 = find_node(root, "it1")
    assert single_slot_names(it1) == (
        "condition", "then_action", "else_action",
    )


def test_iter_children_yields_lists_then_singles_in_order() -> None:
    """``iter_children`` must preserve declaration order so UIs rendering
    the tree see the parent's slots in a predictable sequence."""
    root = _tree()
    it1 = find_node(root, "it1")
    seq = [(slot, idx, child.id) for slot, idx, child in iter_children(it1)]
    assert seq == [
        ("condition", None, "a1"),
        ("then_action", None, "seq"),
        ("else_action", None, "log2"),
    ]


# ---- deletions ---------------------------------------------------------


def test_delete_from_list_slot_renumbers_remainder() -> None:
    root = _tree()
    assert delete_node(root, "s1") is True
    a1 = find_node(root, "a1")
    assert [c.id for c in a1.children] == ["s2"]


def test_delete_optional_single_slot_clears_it() -> None:
    """``else_action`` is the only optional single slot in the catalog."""
    root = _tree()
    assert delete_node(root, "log2") is True
    it1 = find_node(root, "it1")
    assert it1.else_action is None


def test_delete_required_single_slot_raises() -> None:
    """Deleting ``condition`` or ``then_action`` would leave the
    if-then-else invalid at evaluation time; refuse."""
    root = _tree()
    with pytest.raises(ValueError, match="required slot"):
        delete_node(root, "a1")   # if-then-else.condition
    with pytest.raises(ValueError, match="required slot"):
        delete_node(root, "seq")  # if-then-else.then_action


def test_delete_missing_node_returns_false() -> None:
    assert delete_node(_tree(), "no-such-id") is False


# ---- additions ---------------------------------------------------------


def test_append_child_lands_at_end_of_list_slot() -> None:
    root = _tree()
    a1 = find_node(root, "a1")
    new = ConstantBool(value=True, node_id="s3-new")
    idx = append_child(a1, "children", new)
    assert idx == 2
    assert [c.id for c in a1.children] == ["s1", "s2", "s3-new"]


def test_append_child_rejects_unknown_slot() -> None:
    root = _tree()
    a1 = find_node(root, "a1")
    with pytest.raises(ValueError, match="no list slot"):
        append_child(a1, "actions", ConstantBool(value=True))


def test_set_single_slot_replaces_existing() -> None:
    root = _tree()
    it1 = find_node(root, "it1")
    new_cond = ConstantBool(value=False, node_id="replacement-cond")
    set_single_slot(it1, "condition", new_cond)
    assert it1.condition.id == "replacement-cond"


def test_set_single_slot_rejects_clearing_required() -> None:
    root = _tree()
    it1 = find_node(root, "it1")
    with pytest.raises(ValueError, match="required"):
        set_single_slot(it1, "condition", None)


def test_set_single_slot_clears_optional() -> None:
    root = _tree()
    it1 = find_node(root, "it1")
    set_single_slot(it1, "else_action", None)
    assert it1.else_action is None


def test_delete_round_trips_through_find() -> None:
    """After delete, find_node must no longer locate the dropped id."""
    root = _tree()
    delete_node(root, "log1")
    assert find_node(root, "log1") is None
    seq = find_node(root, "seq")
    assert [c.id for c in seq.actions] == ["st1"]
