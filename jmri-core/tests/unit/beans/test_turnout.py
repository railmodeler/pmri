from __future__ import annotations

from jmri_core.beans.turnout import Turnout, TurnoutStateChange
from jmri_core.state import TurnoutFeedback, TurnoutState


async def test_turnout_initial_state_is_unknown() -> None:
    t = Turnout("LT1")
    assert t.commanded_state == TurnoutState.UNKNOWN
    assert t.known_state == TurnoutState.UNKNOWN


async def test_set_commanded_updates_both_states_in_direct_feedback() -> None:
    t = Turnout("LT1")
    assert t.feedback_mode == TurnoutFeedback.DIRECT
    await t.set_commanded_state(TurnoutState.THROWN)
    assert t.commanded_state == TurnoutState.THROWN
    # Direct feedback: known mirrors commanded.
    assert t.known_state == TurnoutState.THROWN


async def test_set_commanded_emits_state_changes() -> None:
    t = Turnout("LT1")
    events: list[TurnoutStateChange] = []
    t.state_changed.connect(events.append)
    await t.set_commanded_state(TurnoutState.THROWN)
    assert [e.kind for e in events] == ["commanded", "known"]
    assert events[0].previous == TurnoutState.UNKNOWN
    assert events[0].current == TurnoutState.THROWN


async def test_idempotent_set_commanded_does_not_emit() -> None:
    t = Turnout("LT1")
    await t.set_commanded_state(TurnoutState.THROWN)
    events: list[TurnoutStateChange] = []
    t.state_changed.connect(events.append)
    await t.set_commanded_state(TurnoutState.THROWN)  # same value
    assert events == []


async def test_indirect_feedback_keeps_known_separate() -> None:
    t = Turnout("LT1")
    t.feedback_mode = TurnoutFeedback.ONESENSOR
    await t.set_commanded_state(TurnoutState.THROWN)
    # No automatic known-state update with indirect feedback.
    assert t.commanded_state == TurnoutState.THROWN
    assert t.known_state == TurnoutState.UNKNOWN
    # Driver explicitly reports the layout state.
    await t._set_known_state(TurnoutState.THROWN)
    assert t.known_state == TurnoutState.THROWN
