from __future__ import annotations

import pytest

from pmri_logixng import ConstantBool, LogAction, Sequence
from pmri_logixng.tree import Node


def test_node_requires_type_tag() -> None:
    class Broken(Node):
        pass
    with pytest.raises(TypeError, match="type_tag"):
        Broken()


def test_node_id_is_generated_when_missing() -> None:
    a = LogAction(message="hi")
    b = LogAction(message="hi")
    assert a.id != b.id


def test_node_id_is_preserved_when_supplied() -> None:
    a = LogAction(message="hi", node_id="fixed-id")
    assert a.id == "fixed-id"


def test_to_dict_carries_type_and_id() -> None:
    a = LogAction(message="hi", user_name="my-log")
    d = a.to_dict()
    assert d["type"] == "action:log"
    assert "id" in d
    assert d["user_name"] == "my-log"
    assert d["message"] == "hi"


def test_user_name_omitted_when_empty() -> None:
    a = LogAction(message="hi")
    assert "user_name" not in a.to_dict()


def test_sequence_to_dict_nests_children() -> None:
    seq = Sequence([LogAction(message="a"), LogAction(message="b")])
    d = seq.to_dict()
    assert d["type"] == "action:sequence"
    assert len(d["actions"]) == 2
    assert d["actions"][0]["type"] == "action:log"


def test_repr_uses_user_name_when_set() -> None:
    a = LogAction(message="hi", user_name="hello-world")
    assert "hello-world" in repr(a)


def test_constant_bool_extra_fields() -> None:
    assert ConstantBool(value=True).to_dict()["value"] is True
