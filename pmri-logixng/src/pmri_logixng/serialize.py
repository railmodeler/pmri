"""YAML/JSON serialization for LogixNG trees.

The on-disk format is a nested dict tree where every node has a
``type:`` tag matched against :mod:`pmri_logixng.registry`. Each
concrete class implements :meth:`extra_fields` to declare its own
serialized fields; deserialization runs the inverse via a small,
explicit decoder per node class.

Why explicit decoders and not reflection? Because actions/expressions
hold *typed* children (``Action``, ``Expression[bool]``, ``TurnoutState``,
...) that need careful reconstruction, not blind kwarg passing.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

import yaml
from pmri_core.state import LightState, SensorState, TurnoutState

from pmri_logixng.actions.cv import ReadCV, WriteCV
from pmri_logixng.actions.digital import (
    IfThenElse,
    LogAction,
    SetLightState,
    SetMemory,
    SetMemoryFromExpression,
    SetSensorState,
    SetTurnoutState,
)
from pmri_logixng.actions.flow import Delay, Sequence
from pmri_logixng.actions.script import ScriptAction
from pmri_logixng.expressions.analog import (
    ConstantNumber,
    MemoryValueNumber,
    NumericCompare,
    NumericOp,
)
from pmri_logixng.expressions.digital import (
    And,
    ConstantBool,
    LightIsOn,
    MemoryEquals,
    Not,
    Or,
    SensorIsActive,
    TurnoutIsClosed,
)
from pmri_logixng.expressions.string import (
    ConstantString,
    MemoryValueString,
    StringCompare,
    StringConcat,
)
from pmri_logixng.logix import ConditionalNG, LogixNG
from pmri_logixng.tree import Action, Expression, Node


def to_yaml(node: Node) -> str:
    """Serialize *node* to a YAML string. Lossless."""
    return yaml.safe_dump(node.to_dict(), sort_keys=False)


def to_json(node: Node, *, indent: int | None = 2) -> str:
    """Serialize *node* to a JSON string."""
    return json.dumps(node.to_dict(), indent=indent)


def save_yaml(node: Node, path: str | pathlib.Path) -> None:
    pathlib.Path(path).write_text(to_yaml(node), encoding="utf-8")


def load_yaml(path: str | pathlib.Path) -> Node:
    return from_dict(yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")))


def from_yaml(text: str) -> Node:
    return from_dict(yaml.safe_load(text))


def from_json(text: str) -> Node:
    return from_dict(json.loads(text))


# ---- decoders --------------------------------------------------------------


def from_dict(payload: dict[str, Any]) -> Node:
    """Dispatch on ``type`` to the right decoder."""
    tag = payload.get("type")
    if not isinstance(tag, str):
        raise ValueError(f"serialized node missing 'type' tag: {payload!r}")
    decoder = _DECODERS.get(tag)
    if decoder is None:
        raise KeyError(f"no decoder for tag {tag!r}")
    node = decoder(payload)
    assert isinstance(node, Node)
    return node


def _node_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Common ``user_name`` + ``node_id`` extraction."""
    return {
        "user_name": payload.get("user_name", ""),
        "node_id": payload.get("id"),
    }


def _decode_conditional(p: dict[str, Any]) -> ConditionalNG:
    return ConditionalNG(
        actions=[_decode_action(a) for a in p.get("actions", [])],
        enabled=p.get("enabled", True),
        **_node_kwargs(p),
    )


def _decode_logixng(p: dict[str, Any]) -> LogixNG:
    return LogixNG(
        conditionals=[_decode_conditional(c) for c in p.get("conditionals", [])],
        enabled=p.get("enabled", True),
        **_node_kwargs(p),
    )


# ---- action decoders ------------------------------------------------------


def _decode_action(p: dict[str, Any]) -> Action:
    node = from_dict(p)
    if not isinstance(node, Action):
        raise TypeError(f"expected Action, got {type(node).__name__}")
    return node


def _decode_expression(p: dict[str, Any]) -> Expression[Any]:
    node = from_dict(p)
    if not isinstance(node, Expression):
        raise TypeError(f"expected Expression, got {type(node).__name__}")
    return node


def _decode_set_turnout(p: dict[str, Any]) -> SetTurnoutState:
    return SetTurnoutState(
        turnout=p["turnout"], state=TurnoutState[p["state"]],
        **_node_kwargs(p),
    )


def _decode_set_sensor(p: dict[str, Any]) -> SetSensorState:
    return SetSensorState(
        sensor=p["sensor"], state=SensorState[p["state"]],
        **_node_kwargs(p),
    )


def _decode_set_light(p: dict[str, Any]) -> SetLightState:
    return SetLightState(
        light=p["light"], state=LightState[p["state"]],
        **_node_kwargs(p),
    )


def _decode_set_memory(p: dict[str, Any]) -> SetMemory:
    return SetMemory(memory=p["memory"], value=p.get("value"), **_node_kwargs(p))


def _decode_set_memory_from_expression(p: dict[str, Any]) -> SetMemoryFromExpression:
    return SetMemoryFromExpression(
        memory=p["memory"], source=_decode_expression(p["source"]),
        **_node_kwargs(p),
    )


def _decode_if_then(p: dict[str, Any]) -> IfThenElse:
    else_payload = p.get("else")
    return IfThenElse(
        condition=_decode_expression(p["condition"]),
        then_action=_decode_action(p["then"]),
        else_action=_decode_action(else_payload) if else_payload else None,
        **_node_kwargs(p),
    )


def _decode_log(p: dict[str, Any]) -> LogAction:
    return LogAction(
        message=p.get("message", ""), level=p.get("level", "info"),
        **_node_kwargs(p),
    )


def _decode_sequence(p: dict[str, Any]) -> Sequence:
    return Sequence(
        actions=[_decode_action(a) for a in p.get("actions", [])],
        **_node_kwargs(p),
    )


def _decode_delay(p: dict[str, Any]) -> Delay:
    return Delay(seconds=float(p["seconds"]), **_node_kwargs(p))


def _decode_script(p: dict[str, Any]) -> ScriptAction:
    return ScriptAction(
        source=p.get("source", ""),
        bindings=p.get("bindings") or {},
        **_node_kwargs(p),
    )


def _decode_write_cv(p: dict[str, Any]) -> WriteCV:
    return WriteCV(
        cv=int(p["cv"]), value=int(p["value"]),
        address=p.get("address"),
        long_address=bool(p.get("long_address", False)),
        **_node_kwargs(p),
    )


def _decode_read_cv(p: dict[str, Any]) -> ReadCV:
    return ReadCV(
        cv=int(p["cv"]),
        address=p.get("address"),
        long_address=bool(p.get("long_address", False)),
        into_memory=p.get("into_memory"),
        **_node_kwargs(p),
    )


# ---- expression decoders --------------------------------------------------


def _decode_const_bool(p: dict[str, Any]) -> ConstantBool:
    return ConstantBool(value=bool(p["value"]), **_node_kwargs(p))


def _decode_sensor_active(p: dict[str, Any]) -> SensorIsActive:
    return SensorIsActive(sensor=p["sensor"], **_node_kwargs(p))


def _decode_turnout_closed(p: dict[str, Any]) -> TurnoutIsClosed:
    return TurnoutIsClosed(turnout=p["turnout"], **_node_kwargs(p))


def _decode_light_on(p: dict[str, Any]) -> LightIsOn:
    return LightIsOn(light=p["light"], **_node_kwargs(p))


def _decode_memory_equals(p: dict[str, Any]) -> MemoryEquals:
    return MemoryEquals(memory=p["memory"], value=p.get("value"), **_node_kwargs(p))


def _decode_and(p: dict[str, Any]) -> And:
    return And(
        children=[_decode_expression(c) for c in p.get("children", [])],
        **_node_kwargs(p),
    )


def _decode_or(p: dict[str, Any]) -> Or:
    return Or(
        children=[_decode_expression(c) for c in p.get("children", [])],
        **_node_kwargs(p),
    )


def _decode_not(p: dict[str, Any]) -> Not:
    return Not(child=_decode_expression(p["child"]), **_node_kwargs(p))


# ---- analog / string expression decoders -----------------------------------


def _decode_const_number(p: dict[str, Any]) -> ConstantNumber:
    return ConstantNumber(value=float(p["value"]), **_node_kwargs(p))


def _decode_memory_value_number(p: dict[str, Any]) -> MemoryValueNumber:
    return MemoryValueNumber(memory=p["memory"], **_node_kwargs(p))


def _decode_numeric_op(p: dict[str, Any]) -> NumericOp:
    return NumericOp(
        op=p["op"],
        left=_decode_expression(p["left"]),
        right=_decode_expression(p["right"]),
        **_node_kwargs(p),
    )


def _decode_numeric_compare(p: dict[str, Any]) -> NumericCompare:
    return NumericCompare(
        op=p["op"],
        left=_decode_expression(p["left"]),
        right=_decode_expression(p["right"]),
        **_node_kwargs(p),
    )


def _decode_const_string(p: dict[str, Any]) -> ConstantString:
    return ConstantString(value=str(p["value"]), **_node_kwargs(p))


def _decode_memory_value_string(p: dict[str, Any]) -> MemoryValueString:
    return MemoryValueString(memory=p["memory"], **_node_kwargs(p))


def _decode_string_concat(p: dict[str, Any]) -> StringConcat:
    return StringConcat(
        children=[_decode_expression(c) for c in p.get("children", [])],
        separator=p.get("separator", ""),
        **_node_kwargs(p),
    )


def _decode_string_compare(p: dict[str, Any]) -> StringCompare:
    return StringCompare(
        op=p["op"],
        left=_decode_expression(p["left"]),
        right=_decode_expression(p["right"]),
        case_insensitive=bool(p.get("case_insensitive", False)),
        **_node_kwargs(p),
    )


# Tag -> decoder dispatch table.
_DECODERS: dict[str, Any] = {
    "logixng:conditional": _decode_conditional,
    "logixng:root": _decode_logixng,
    "action:set-turnout": _decode_set_turnout,
    "action:set-sensor": _decode_set_sensor,
    "action:set-light": _decode_set_light,
    "action:set-memory": _decode_set_memory,
    "action:set-memory-from-expression": _decode_set_memory_from_expression,
    "action:if-then-else": _decode_if_then,
    "action:log": _decode_log,
    "action:sequence": _decode_sequence,
    "action:delay": _decode_delay,
    "action:script": _decode_script,
    "action:write-cv": _decode_write_cv,
    "action:read-cv": _decode_read_cv,
    "expr:const-bool": _decode_const_bool,
    "expr:sensor-active": _decode_sensor_active,
    "expr:turnout-closed": _decode_turnout_closed,
    "expr:light-on": _decode_light_on,
    "expr:memory-equals": _decode_memory_equals,
    "expr:and": _decode_and,
    "expr:or": _decode_or,
    "expr:not": _decode_not,
    "expr:const-num": _decode_const_number,
    "expr:memory-value-num": _decode_memory_value_number,
    "expr:num-op": _decode_numeric_op,
    "expr:num-compare": _decode_numeric_compare,
    "expr:const-str": _decode_const_string,
    "expr:memory-value-str": _decode_memory_value_string,
    "expr:string-concat": _decode_string_concat,
    "expr:string-compare": _decode_string_compare,
}
