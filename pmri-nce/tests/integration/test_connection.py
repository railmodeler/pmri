"""End-to-end tests for NceConnection."""
from __future__ import annotations

import asyncio

from pmri_core.plugins import load_connections

from pmri_nce import NceConnection, loopback_pair
from pmri_nce.opcodes import (
    ACK_OK,
    OP_WRITE_N_MEM,
    OP_CLOCK_SET_RATIO,
    OP_CLOCK_START,
    OP_CLOCK_STOP,
    OP_READ_16_MEM,
)


async def test_entry_point_discovers_nce() -> None:
    connections = load_connections()
    assert "nce" in connections
    assert connections["nce"] is NceConnection


async def test_open_close_lifecycle() -> None:
    a, _ = loopback_pair()
    conn = NceConnection()
    assert not conn.is_open
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        # The background turnout monitor polls the CS every ~500ms and
        # would race the fake-CS handler for reply bytes. Off for these
        # tests -- the monitor has its own coverage in test_monitors.
        "disable_turnout_monitor": True,
    })
    assert conn.is_open
    assert conn.prefix == "N"
    assert conn.command_set.name == "PH-Pro 2006+"
    assert conn.turnouts is not None
    assert conn.sensors is not None
    assert conn.power is not None
    await conn.close()
    assert not conn.is_open


async def test_command_set_override() -> None:
    a, _ = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a, "command_set": "PowerCab",
    })
    assert conn.command_set.name == "PowerCab"
    await conn.close()


async def test_unknown_command_set_falls_back_to_default() -> None:
    a, _ = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a, "command_set": "no-such-thing",
        "auto_detect": False,
    })
    # Default is PH-Pro 2006+.
    assert conn.command_set.name == "PH-Pro 2006+"
    await conn.close()


async def test_unknown_transport_kind_raises() -> None:
    conn = NceConnection()
    try:
        await conn.open({"kind": "wifi"})
    except ValueError as e:
        assert "transport" in str(e).lower()
    else:
        raise AssertionError("expected ValueError")


# ----- fast clock ----------------------------------------------------------


def _clock_reply_12h(hours: int, minutes: int, seconds: int, ratio: int, *, am: bool) -> bytes:
    """Build a 16-byte CS-clock reply in 12h mode with the given time.

    Byte layout mirrors NceClockControl:
      [0] scale (250/ratio); [1] tick=1 (running); [2] ss; [3] mm; [4] hh;
      [5] AM/PM ('A' if AM else 'P'); [6] 12h/24h flag (0=12h);
      remaining bytes zero-padded to 16.
    """
    scale = 250 // ratio
    ampm = ord("A") if am else ord("P")
    body = bytes([scale, 1, seconds, minutes, hours, ampm, 0]) + bytes(9)
    assert len(body) == 16
    return body


async def test_read_hardware_clock_decodes_12h_am() -> None:
    """CS reports 07:30 AM at rate 8 -- the user's actual scenario."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        # The background turnout monitor polls the CS every ~500ms and
        # would race the fake-CS handler for reply bytes. Off for these
        # tests -- the monitor has its own coverage in test_monitors.
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        # 3-byte request: [OP_READ_16_MEM (READ16_CMD), hi, lo].
        req = await b.read_exact(3)
        assert req[0] == OP_READ_16_MEM
        assert req[1] == 0xDC and req[2] == 0x00
        await b.write(_clock_reply_12h(7, 30, 0, ratio=8, am=True))

    asyncio.create_task(fake_cs())
    state = await conn.read_hardware_clock()
    assert state.hours == 7
    assert state.minutes == 30
    assert state.rate == 8.0
    assert state.twenty_four_hour is False
    # First read has no prior seconds byte to compare against; running
    # defaults to the last-known intent (False on a fresh connection).
    # Running inference is tested separately below.
    assert state.running is False
    await conn.close()


async def test_read_hardware_clock_running_inferred_from_tick_advancement() -> None:
    """Two reads with different seconds bytes -> running=True."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        # First poll: seconds=0.
        await b.read_exact(3)
        await b.write(_clock_reply_12h(7, 30, 0, ratio=8, am=True))
        # Second poll: seconds=15 (clock advanced).
        await b.read_exact(3)
        await b.write(_clock_reply_12h(7, 30, 15, ratio=8, am=True))

    asyncio.create_task(fake_cs())
    first = await conn.read_hardware_clock()
    assert first.running is False              # no prior history
    second = await conn.read_hardware_clock()
    assert second.running is True              # seconds byte moved -> running
    await conn.close()


async def test_read_hardware_clock_stopped_when_seconds_unchanged() -> None:
    """Two consecutive reads with the same seconds byte -> running=False."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        for _ in range(2):
            await b.read_exact(3)
            await b.write(_clock_reply_12h(7, 30, 45, ratio=8, am=True))

    asyncio.create_task(fake_cs())
    await conn.read_hardware_clock()             # seed
    state = await conn.read_hardware_clock()     # same seconds -> stopped
    assert state.running is False
    await conn.close()


async def test_start_hardware_clock_marks_running_intent() -> None:
    """After Start, the next read must report running even if the CS
    hasn't ticked yet (the seconds byte may still match the pre-start
    sample). The intent flag covers that gap."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        await b.read_exact(1)                     # START_CLOCK opcode
        await b.write(ACK_OK)
        await b.read_exact(3)                     # READ16 request
        await b.write(_clock_reply_12h(7, 30, 0, ratio=8, am=True))

    asyncio.create_task(fake_cs())
    await conn.start_hardware_clock()
    state = await conn.read_hardware_clock()
    assert state.running is True
    await conn.close()


async def test_read_hardware_clock_normalises_12h_pm() -> None:
    """PM 03:15 in 12h mode -> 15:15 in the returned 24-hour state."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        # The background turnout monitor polls the CS every ~500ms and
        # would race the fake-CS handler for reply bytes. Off for these
        # tests -- the monitor has its own coverage in test_monitors.
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        await b.read_exact(3)      # 3-byte OP_READ_16_MEM request
        await b.write(_clock_reply_12h(3, 15, 0, ratio=4, am=False))

    asyncio.create_task(fake_cs())
    state = await conn.read_hardware_clock()
    assert state.hours == 15
    assert state.minutes == 15
    await conn.close()


async def test_read_hardware_clock_normalises_12h_midnight() -> None:
    """AM 12:00 in 12h mode -> 00:00."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        # The background turnout monitor polls the CS every ~500ms and
        # would race the fake-CS handler for reply bytes. Off for these
        # tests -- the monitor has its own coverage in test_monitors.
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        await b.read_exact(3)      # 3-byte OP_READ_16_MEM request
        await b.write(_clock_reply_12h(12, 0, 0, ratio=1, am=True))

    asyncio.create_task(fake_cs())
    state = await conn.read_hardware_clock()
    assert state.hours == 0
    assert state.minutes == 0
    await conn.close()


async def test_set_hardware_clock_time_writes_to_cs() -> None:
    """Verify the wire request matches JMRI's ``accMemoryWriteN`` at 0xDC02."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        # The background turnout monitor polls the CS every ~500ms and
        # would race the fake-CS handler for reply bytes. Off for these
        # tests -- the monitor has its own coverage in test_monitors.
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        # WRITE_N_CMD is padded to 20 bytes total; the CS won't ACK
        # until it has consumed all 20.
        req = await b.read_exact(20)
        captured.append(req)
        await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.set_hardware_clock_time(13, 45, 30)
    expected = (
        bytes([OP_WRITE_N_MEM, 0xDC, 0x02, 3, 30, 45, 13])
        + bytes(13)   # zero-padding out to 20 bytes total
    )
    assert captured[0] == expected
    await conn.close()


async def test_set_hardware_clock_rate_clamps_and_rounds() -> None:
    """Float rate 7.6 must arrive as integer ratio 8 on the wire."""
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        # The background turnout monitor polls the CS every ~500ms and
        # would race the fake-CS handler for reply bytes. Off for these
        # tests -- the monitor has its own coverage in test_monitors.
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        req = await b.read_exact(2)
        captured.append(req)
        await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.set_hardware_clock_rate(7.6)
    assert captured[0] == bytes([OP_CLOCK_SET_RATIO, 8])
    await conn.close()


async def test_software_consist_set_speed_fans_out_to_every_member() -> None:
    """Regression for "only the last-added member responds": the
    software-consist fan-out must send ONE NCE loco command per member,
    with each command carrying that member's address on the wire.

    Bug modes this catches:
    * ``ConsistManager`` holding a stale ``ThrottleManager`` reference
      from before the driver installed its NCE subclass -- the
      fan-out would drive placeholder beans and NO commands would
      hit the wire.
    * A subtle iteration bug that only sends the LAST member's
      command (which is what the user observed on the Pi).
    """
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        # Loco command is 5 bytes on the wire + 1 ACK back.
        for _ in range(3):
            req = await b.read_exact(5)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(10, kind="software")
    await conn.consists.add_member(10, 111)
    await conn.consists.add_member(10, 222)
    await conn.consists.add_member(10, 333)
    await conn.consists.set_speed(10, 0.5)

    assert len(captured) == 3
    # Each request begins with the loco-cmd opcode + the wire address
    # for that member. Shorts (< 128) get no offset; we passed all
    # three as short addresses.
    from pmri_nce.opcodes import OP_NCE_LOCO_CMD  # noqa: PLC0415
    addresses = [(c[1] << 8) | c[2] for c in captured]
    opcodes = [c[0] for c in captured]
    assert opcodes == [OP_NCE_LOCO_CMD, OP_NCE_LOCO_CMD, OP_NCE_LOCO_CMD]
    assert sorted(addresses) == [111, 222, 333]
    await conn.close()


async def test_cs_consist_default_kind_is_command_station() -> None:
    """After ``NceConsistManager`` was wired in, the default kind on an
    NCE connection is ``command_station`` -- not ``software`` as it was
    when we shipped only the base manager. Callers who want software
    fan-out must ask for it explicitly."""
    a, _ = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    try:
        assert conn.consists.default_kind == "command_station"
    finally:
        await conn.close()


async def test_cs_consist_add_sends_lead_rear_mid_in_order() -> None:
    """The FIRST add sends ``LOCO_SUB_CS_ADD_LEAD_FWD`` (0x0B), the
    SECOND sends ``LOCO_SUB_CS_ADD_REAR_FWD`` (0x0D), and everything
    from the THIRD onward sends ``LOCO_SUB_CS_ADD_MID_FWD`` (0x0F).
    All commands carry the *consist number* in the data byte.
    """
    from pmri_nce.opcodes import (  # noqa: PLC0415
        LOCO_SUB_CS_ADD_LEAD_FWD,
        LOCO_SUB_CS_ADD_MID_FWD,
        LOCO_SUB_CS_ADD_REAR_FWD,
        OP_NCE_LOCO_CMD,
    )

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        for _ in range(4):
            req = await b.read_exact(5)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    consist_num = 33
    await conn.consists.create(consist_num)
    await conn.consists.add_member(consist_num, 111)   # LEAD
    await conn.consists.add_member(consist_num, 222)   # REAR
    await conn.consists.add_member(consist_num, 333)   # MID #1
    await conn.consists.add_member(consist_num, 444)   # MID #2

    assert len(captured) == 4
    # Layout: [op, addr_hi, addr_lo, sub, data]
    for req in captured:
        assert req[0] == OP_NCE_LOCO_CMD
        assert req[4] == consist_num       # consist# in the data byte
    subs = [req[3] for req in captured]
    assert subs == [
        LOCO_SUB_CS_ADD_LEAD_FWD,
        LOCO_SUB_CS_ADD_REAR_FWD,
        LOCO_SUB_CS_ADD_MID_FWD,
        LOCO_SUB_CS_ADD_MID_FWD,
    ]
    addresses = [(req[1] << 8) | req[2] for req in captured]
    assert addresses == [111, 222, 333, 444]
    await conn.close()


async def test_cs_consist_add_reverse_uses_rev_variants() -> None:
    """``direction_normal=False`` sends the REV sub-command variant
    (0x0A / 0x0C / 0x0E) so the CS records the loco's orientation."""
    from pmri_nce.opcodes import (  # noqa: PLC0415
        LOCO_SUB_CS_ADD_LEAD_REV,
        LOCO_SUB_CS_ADD_MID_REV,
        LOCO_SUB_CS_ADD_REAR_REV,
    )

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        for _ in range(3):
            req = await b.read_exact(5)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(50)
    await conn.consists.add_member(50, 111, direction_normal=False)  # LEAD REV
    await conn.consists.add_member(50, 222, direction_normal=False)  # REAR REV
    await conn.consists.add_member(50, 333, direction_normal=False)  # MID REV

    subs = [req[3] for req in captured]
    assert subs == [
        LOCO_SUB_CS_ADD_LEAD_REV,
        LOCO_SUB_CS_ADD_REAR_REV,
        LOCO_SUB_CS_ADD_MID_REV,
    ]
    await conn.close()


async def test_cs_consist_add_uses_long_address_wire_offset() -> None:
    """Long-address members ride the wire as ``address + 0xC000`` so
    the CS can tell them apart from a short at the same number."""
    from pmri_nce.opcodes import LONG_ADDRESS_OFFSET  # noqa: PLC0415

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        for _ in range(1):
            req = await b.read_exact(5)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(60)
    await conn.consists.add_member(60, 3601, long_address=True)

    assert len(captured) == 1
    wire = (captured[0][1] << 8) | captured[0][2]
    assert wire == 3601 + LONG_ADDRESS_OFFSET
    await conn.close()


async def test_cs_consist_remove_mid_sends_remove_opcode() -> None:
    """Removing a MID position sends ``LOCO_SUB_CS_REMOVE`` (0x10)
    targeting that loco's address, with data byte 0."""
    from pmri_nce.opcodes import LOCO_SUB_CS_REMOVE  # noqa: PLC0415

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        # 3 adds + 1 remove = 4 round-trips.
        for _ in range(4):
            req = await b.read_exact(5)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(70)
    await conn.consists.add_member(70, 111)       # LEAD
    await conn.consists.add_member(70, 222)       # REAR
    await conn.consists.add_member(70, 333)       # MID
    await conn.consists.remove_member(70, 333)    # remove MID

    assert len(captured) == 4
    remove_req = captured[3]
    assert remove_req[3] == LOCO_SUB_CS_REMOVE
    assert remove_req[4] == 0
    address = (remove_req[1] << 8) | remove_req[2]
    assert address == 333
    await conn.close()


async def test_cs_consist_delete_sends_kill_via_lead_address() -> None:
    """Deleting an active CS consist sends ``LOCO_SUB_CS_KILL`` (0x11)
    with the LEAD's DCC address -- the CS uses that to identify which
    consist to disband. Data byte 0."""
    from pmri_nce.opcodes import LOCO_SUB_CS_KILL  # noqa: PLC0415

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        # 2 adds + 1 kill = 3.
        for _ in range(3):
            req = await b.read_exact(5)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(80)
    await conn.consists.add_member(80, 111)   # LEAD
    await conn.consists.add_member(80, 222)   # REAR
    await conn.consists.delete(80)

    kill_req = captured[2]
    assert kill_req[3] == LOCO_SUB_CS_KILL
    assert kill_req[4] == 0
    address = (kill_req[1] << 8) | kill_req[2]
    assert address == 111    # LEAD's address
    await conn.close()


async def test_cs_consist_rejects_seventh_member() -> None:
    """NCE PowerPro's CS-consist slot cap is 6 members. Enforced at
    the bean level so the UI sees a clean error instead of a silent
    drop on the seventh add."""
    import pytest  # noqa: PLC0415

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        # Six successful adds -- the seventh must not reach the wire.
        for _ in range(6):
            await b.read_exact(5)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(90)
    for addr in (11, 22, 33, 44, 55, 66):
        await conn.consists.add_member(90, addr)
    with pytest.raises(ValueError, match="6 members max"):
        await conn.consists.add_member(90, 77)
    await conn.close()


async def test_cs_consist_rejects_lead_and_rear_removal() -> None:
    """LEAD (position 0) and REAR (position 1) can't be removed from
    a CS consist individually -- the CS design requires disposing the
    whole consist. Reject at the bean level with a clear error."""
    import pytest  # noqa: PLC0415

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })

    async def fake_cs() -> None:
        for _ in range(3):
            await b.read_exact(5)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(100)
    await conn.consists.add_member(100, 111)  # LEAD
    await conn.consists.add_member(100, 222)  # REAR
    await conn.consists.add_member(100, 333)  # MID
    with pytest.raises(ValueError, match="LEAD"):
        await conn.consists.remove_member(100, 111)
    with pytest.raises(ValueError, match="REAR"):
        await conn.consists.remove_member(100, 222)
    await conn.close()


async def test_cs_consist_set_speed_targets_lead_only() -> None:
    """CS-consist fan-out: the CS handles per-loco speed distribution
    after LINK/ADD. We send exactly ONE speed command to the LEAD, not
    one per member."""
    from pmri_nce.opcodes import (  # noqa: PLC0415
        LOCO_SUB_128_FWD,
        OP_NCE_LOCO_CMD,
    )

    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        # 3 adds + 1 speed cmd = 4.
        for _ in range(4):
            req = await b.read_exact(5)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.consists.create(110)
    await conn.consists.add_member(110, 111)   # LEAD
    await conn.consists.add_member(110, 222)   # REAR
    await conn.consists.add_member(110, 333)   # MID
    await conn.consists.set_speed(110, 0.5)

    # First three requests are the adds; the fourth is the speed cmd,
    # targeted at the LEAD only.
    assert len(captured) == 4
    speed_req = captured[3]
    assert speed_req[0] == OP_NCE_LOCO_CMD
    assert speed_req[3] == LOCO_SUB_128_FWD    # 128-step forward
    address = (speed_req[1] << 8) | speed_req[2]
    assert address == 111    # LEAD only, not 222/333
    await conn.close()


async def test_start_stop_hardware_clock() -> None:
    a, b = loopback_pair()
    conn = NceConnection()
    await conn.open({
        "kind": "loopback", "loopback_port": a,
        "auto_detect": False,
        # The background turnout monitor polls the CS every ~500ms and
        # would race the fake-CS handler for reply bytes. Off for these
        # tests -- the monitor has its own coverage in test_monitors.
        "disable_turnout_monitor": True,
    })
    captured: list[bytes] = []

    async def fake_cs() -> None:
        for _ in range(2):
            req = await b.read_exact(1)
            captured.append(req)
            await b.write(ACK_OK)

    asyncio.create_task(fake_cs())
    await conn.start_hardware_clock()
    await conn.stop_hardware_clock()
    assert captured == [bytes([OP_CLOCK_START]), bytes([OP_CLOCK_STOP])]
    await conn.close()
