"""Unit coverage for the four bean types added in the missing-JSON pass.

The bean shapes here are minimal on purpose -- Panel Editor (Tier 1 #3
in ``~/workitems.txt``) will grow ``LayoutBlock`` and ``OBlock`` real
occupancy logic later. What matters at this level: state signals fire,
managers register cleanly, and default values match the schemas.
"""
from __future__ import annotations

import pytest

from jmri_core.beans.audio import (
    AUDIO_STATE_PLAYING,
    AUDIO_STATE_STOPPED,
    Audio,
    AudioManager,
)
from jmri_core.beans.id_tag import (
    ID_TAG_STATE_SEEN,
    ID_TAG_STATE_UNKNOWN,
    ID_TAG_STATE_UNSEEN,
    IdTag,
    IdTagManager,
)
from jmri_core.beans.layout_block import LayoutBlock, LayoutBlockManager
from jmri_core.beans.oblock import (
    OBLOCK_STATUS_OCCUPIED,
    OBLOCK_STATUS_UNOCCUPIED,
    OBlock,
    OBlockManager,
)


# ---- audio ---------------------------------------------------------------


async def test_audio_defaults_to_stopped_source() -> None:
    a = Audio("IAS1")
    assert a.state == AUDIO_STATE_STOPPED
    assert a.kind == "source"
    assert a.url is None
    assert a.gain == 1.0


async def test_audio_play_emits_state_changed() -> None:
    a = Audio("IAS1")
    events: list[int] = []
    a.state_changed.connect(lambda s: events.append(s))
    await a.play()
    assert events == [AUDIO_STATE_PLAYING]
    # Idempotent: repeat play doesn't re-emit.
    await a.play()
    assert events == [AUDIO_STATE_PLAYING]


async def test_audio_stop_emits_state_changed() -> None:
    a = Audio("IAS1")
    await a.play()
    events: list[int] = []
    a.state_changed.connect(lambda s: events.append(s))
    await a.stop()
    assert events == [AUDIO_STATE_STOPPED]


async def test_audio_manager_provide_and_iter() -> None:
    m = AudioManager("I")
    a = await m.provide("IAS1")
    assert isinstance(a, Audio)
    assert list(m) == [a]


# ---- idTag ---------------------------------------------------------------


async def test_id_tag_defaults_to_unknown() -> None:
    t = IdTag("ID1")
    assert t.state == ID_TAG_STATE_UNKNOWN
    assert t.reporter_name is None
    assert t.time is None


async def test_id_tag_report_seen_updates_all_fields() -> None:
    t = IdTag("ID1")
    events: list[int] = []
    t.state_changed.connect(lambda s: events.append(s))
    await t.report_seen("IR1", "2026-08-03T12:00:00Z")
    assert t.state == ID_TAG_STATE_SEEN
    assert t.reporter_name == "IR1"
    assert t.time == "2026-08-03T12:00:00Z"
    assert events == [ID_TAG_STATE_SEEN]


async def test_id_tag_report_unseen_keeps_reporter() -> None:
    """Once a tag has been seen the reporter attribution is retained
    when the tag goes out of range -- clients render "last seen at X,
    gone since Y" from the pair."""
    t = IdTag("ID1")
    await t.report_seen("IR1", "T0")
    await t.report_unseen("T1")
    assert t.state == ID_TAG_STATE_UNSEEN
    assert t.reporter_name == "IR1"
    assert t.time == "T1"


# ---- layoutBlock ---------------------------------------------------------


def test_layout_block_defaults_match_schema() -> None:
    b = LayoutBlock("ILB1")
    assert b.state == 0
    assert b.block is None
    assert b.memory is None
    assert b.occupancy_sensor is None
    assert b.occupied_sense == 2
    assert b.block_color == "black"
    assert b.track_color == "black"
    assert b.occupied_color == "red"
    assert b.extra_color == "white"
    assert b.use_extra_color is False


async def test_layout_block_manager_provides_new() -> None:
    m = LayoutBlockManager("IL")
    b = await m.provide("ILB1")
    assert isinstance(b, LayoutBlock)
    assert b.system_name == "ILB1"


# ---- oblock --------------------------------------------------------------


async def test_oblock_defaults_and_set_status() -> None:
    o = OBlock("OB1")
    assert o.status == OBLOCK_STATUS_UNOCCUPIED
    events: list[int] = []
    o.state_changed.connect(lambda s: events.append(s))
    await o.set_status(OBLOCK_STATUS_OCCUPIED)
    assert o.status == OBLOCK_STATUS_OCCUPIED
    assert events == [OBLOCK_STATUS_OCCUPIED]
    # Idempotent: same status doesn't re-emit.
    await o.set_status(OBLOCK_STATUS_OCCUPIED)
    assert events == [OBLOCK_STATUS_OCCUPIED]


async def test_oblock_manager_provides_new() -> None:
    m = OBlockManager("O")
    o = await m.provide("OB1")
    assert isinstance(o, OBlock)
    assert o.system_name == "OB1"


@pytest.mark.parametrize("attr,value", [
    ("value", "engine 3601"),
    ("sensor_name", "IS10"),
    ("warrant_name", "IW1"),
    ("train_name", "3601"),
])
def test_oblock_scalar_fields(attr: str, value: str) -> None:
    o = OBlock("OB1")
    setattr(o, attr, value)
    assert getattr(o, attr) == value
