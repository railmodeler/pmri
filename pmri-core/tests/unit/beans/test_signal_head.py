from __future__ import annotations

from pmri_core.beans.signal_head import SignalHead, SignalHeadChange
from pmri_core.state import SignalAppearance


async def test_default_appearance_is_dark() -> None:
    h = SignalHead("LH1")
    assert h.appearance == SignalAppearance.DARK
    assert h.commanded_appearance == SignalAppearance.DARK
    assert not h.held


async def test_set_appearance_emits_change() -> None:
    h = SignalHead("LH1")
    events: list[SignalHeadChange] = []
    h.state_changed.connect(events.append)
    await h.set_appearance(SignalAppearance.GREEN)
    assert h.appearance == SignalAppearance.GREEN
    assert events == [SignalHeadChange(SignalAppearance.DARK, SignalAppearance.GREEN)]


async def test_held_forces_red_in_appearance_property() -> None:
    h = SignalHead("LH1")
    await h.set_appearance(SignalAppearance.GREEN)
    await h.set_held(True)
    assert h.appearance == SignalAppearance.RED
    assert h.commanded_appearance == SignalAppearance.GREEN


async def test_release_held_restores_previous_appearance() -> None:
    h = SignalHead("LH1")
    await h.set_appearance(SignalAppearance.GREEN)
    await h.set_held(True)
    await h.set_held(False)
    assert h.appearance == SignalAppearance.GREEN
