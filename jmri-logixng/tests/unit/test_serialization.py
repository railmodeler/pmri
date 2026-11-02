"""Round-trip tests for the YAML/JSON serializer."""
from __future__ import annotations

import pathlib

import yaml
from jmri_core.state import LightState, SensorState, TurnoutState

from jmri_logixng import (
    And,
    ConditionalNG,
    ConstantBool,
    Delay,
    IfThenElse,
    LogAction,
    LogixNG,
    MemoryEquals,
    Not,
    Or,
    Sequence,
    SensorIsActive,
    SetLightState,
    SetMemory,
    SetSensorState,
    SetTurnoutState,
    TurnoutIsClosed,
    from_dict,
    from_yaml,
    load_yaml,
    save_yaml,
    to_json,
    to_yaml,
)


def _sample_tree() -> LogixNG:
    return LogixNG(
        user_name="example",
        conditionals=[
            ConditionalNG(
                user_name="when occupied",
                actions=[
                    IfThenElse(
                        condition=And([
                            SensorIsActive(sensor="IS1"),
                            Not(TurnoutIsClosed(turnout="IT1")),
                            Or([
                                MemoryEquals(memory="IM1", value="rush-hour"),
                                ConstantBool(value=False),
                            ]),
                        ]),
                        then_action=Sequence([
                            SetTurnoutState(turnout="IT1", state=TurnoutState.CLOSED),
                            SetLightState(light="IL1", state=LightState.ON),
                            Delay(seconds=0.5),
                            SetSensorState(sensor="IS9", state=SensorState.ACTIVE),
                            SetMemory(memory="IM2", value=42),
                            LogAction(message="route set", level="info"),
                        ]),
                        else_action=LogAction(message="no route", level="debug"),
                    ),
                ],
            ),
        ],
    )


def test_yaml_round_trip_preserves_structure() -> None:
    tree = _sample_tree()
    text = to_yaml(tree)
    loaded = from_yaml(text)
    assert isinstance(loaded, LogixNG)
    assert loaded.user_name == "example"
    assert len(loaded.conditionals) == 1
    conditional = loaded.conditionals[0]
    assert conditional.user_name == "when occupied"
    assert len(conditional.actions) == 1
    if_then = conditional.actions[0]
    assert isinstance(if_then, IfThenElse)


def test_json_round_trip() -> None:
    tree = _sample_tree()
    text = to_json(tree)
    loaded = from_dict(__import__("json").loads(text))
    re_emitted = to_json(loaded)
    # Same structure -> same JSON.
    assert text == re_emitted


def test_save_and_load_yaml(tmp_path: pathlib.Path) -> None:
    tree = _sample_tree()
    path = tmp_path / "tree.yaml"
    save_yaml(tree, path)
    loaded = load_yaml(path)
    assert isinstance(loaded, LogixNG)
    assert to_yaml(loaded) == to_yaml(tree)


def test_yaml_uses_descriptive_tags() -> None:
    """The on-disk YAML mentions the human-readable type tags, not class names."""
    text = to_yaml(_sample_tree())
    assert "logixng:root" in text
    assert "action:set-turnout" in text
    assert "expr:and" in text


def test_load_unknown_tag_raises() -> None:
    import pytest  # noqa: PLC0415
    with pytest.raises(KeyError, match="no decoder"):
        from_dict({"type": "action:made-up", "id": "x"})


def test_load_missing_type_raises() -> None:
    import pytest  # noqa: PLC0415
    with pytest.raises(ValueError, match="'type' tag"):
        from_dict({"id": "x"})


def test_state_enum_round_trip() -> None:
    """TurnoutState/SensorState/LightState round-trip through name strings."""
    original = SetTurnoutState(turnout="IT1", state=TurnoutState.THROWN)
    loaded = from_yaml(to_yaml(original))
    assert isinstance(loaded, SetTurnoutState)
    assert loaded.state == TurnoutState.THROWN


def test_yaml_is_human_writable() -> None:
    """Confirm the YAML output round-trips through PyYAML cleanly."""
    text = to_yaml(_sample_tree())
    parsed = yaml.safe_load(text)
    assert parsed["type"] == "logixng:root"
