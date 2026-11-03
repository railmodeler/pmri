from __future__ import annotations

from pmri_core.beans.signal_mast import SignalMast


async def test_signal_mast_set_aspect_emits_change() -> None:
    m = SignalMast("IF$shsm:basic:one-searchlight:(1)")
    events = []
    m.state_changed.connect(events.append)
    await m.set_aspect("Clear")
    assert m.aspect == "Clear"
    assert len(events) == 1


async def test_signal_mast_held_emits_even_without_aspect_change() -> None:
    m = SignalMast("IF$shsm:basic:one-searchlight:(2)")
    await m.set_aspect("Approach")
    events = []
    m.state_changed.connect(events.append)
    await m.set_held(True)
    assert m.held
    assert len(events) == 1
    assert events[0].previous == "Approach"
    assert events[0].current == "Approach"


async def test_idempotent_set_aspect_does_not_emit() -> None:
    m = SignalMast("IF$shsm:basic:(1)")
    await m.set_aspect("Clear")
    events = []
    m.state_changed.connect(events.append)
    await m.set_aspect("Clear")
    assert events == []
