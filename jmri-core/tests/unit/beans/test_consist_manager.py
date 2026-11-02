"""ConsistManager -- software-consist CRUD + fan-out throttle control."""
from __future__ import annotations

import pytest

from jmri_core.beans.consist import Consist, ConsistLoco
from jmri_core.beans.consist_manager import ConsistManager
from jmri_core.beans.throttle_manager import ThrottleManager
from jmri_core.state import ThrottleDirection


@pytest.fixture
def throttles() -> ThrottleManager:
    return ThrottleManager("IT")


@pytest.fixture
def consists(throttles: ThrottleManager) -> ConsistManager:
    return ConsistManager(throttles, "IT")


# ---- construction & CRUD ---------------------------------------------------


def test_manager_requires_a_system_prefix(throttles: ThrottleManager) -> None:
    with pytest.raises(ValueError):
        ConsistManager(throttles, "")


async def test_create_and_get(consists: ConsistManager) -> None:
    # CV19's 7-bit range means the consist address itself is short only.
    c = await consists.create(42)
    assert isinstance(c, Consist)
    assert c.address == 42
    assert c.kind == "software"
    assert consists.get(42) is c


async def test_create_rejects_out_of_range_address(
    consists: ConsistManager,
) -> None:
    """Consist addresses are 1..127 to match the CV19 wire limit.
    Anything else is a caller bug -- surfaces as a pydantic error."""
    import pydantic  # noqa: PLC0415
    with pytest.raises(pydantic.ValidationError):
        await consists.create(128)
    with pytest.raises(pydantic.ValidationError):
        await consists.create(3601)


async def test_create_is_idempotent(consists: ConsistManager) -> None:
    """Repeated create at the same address returns the existing consist
    rather than raising -- restore paths call create liberally."""
    a = await consists.create(42)
    b = await consists.create(42)
    assert a is b


async def test_delete_returns_true_when_present(consists: ConsistManager) -> None:
    await consists.create(7)
    assert await consists.delete(7) is True
    assert await consists.delete(7) is False
    assert consists.get(7) is None


async def test_contains_and_len(consists: ConsistManager) -> None:
    await consists.create(1)
    await consists.create(2)
    assert 1 in consists
    assert 2 in consists
    assert 3 not in consists
    assert len(consists) == 2


async def test_iter_yields_all(consists: ConsistManager) -> None:
    await consists.create(1)
    await consists.create(2)
    assert {c.address for c in consists} == {1, 2}


# ---- signals --------------------------------------------------------------


async def test_created_signal_fires(consists: ConsistManager) -> None:
    seen: list[int] = []
    consists.created.connect(lambda c: seen.append(c.address))
    await consists.create(99)
    assert seen == [99]


async def test_deleted_signal_fires(consists: ConsistManager) -> None:
    await consists.create(99)
    seen: list[int] = []
    consists.deleted.connect(lambda c: seen.append(c.address))
    await consists.delete(99)
    assert seen == [99]


async def test_updated_signal_fires_on_membership_change(
    consists: ConsistManager,
) -> None:
    await consists.create(5)
    seen: list[int] = []
    consists.updated.connect(lambda c: seen.append(c.address))
    await consists.add_member(5, 3601, long_address=True)
    await consists.remove_member(5, 3601)
    assert seen == [5, 5]


# ---- membership ----------------------------------------------------------


async def test_add_member_appends_at_end(consists: ConsistManager) -> None:
    await consists.create(10)
    await consists.add_member(10, 111)
    await consists.add_member(10, 222)
    consist = consists.get(10)
    assert consist is not None
    assert [m.address for m in consist.locos] == [111, 222]
    assert [m.position for m in consist.locos] == [0, 1]


async def test_add_member_idempotent_updates_direction(
    consists: ConsistManager,
) -> None:
    """Re-adding the same address doesn't duplicate; it flips direction."""
    await consists.create(10)
    await consists.add_member(10, 111, direction_normal=True)
    await consists.add_member(10, 111, direction_normal=False)
    consist = consists.get(10)
    assert consist is not None
    assert len(consist.locos) == 1
    assert consist.locos[0].direction_normal is False


async def test_remove_member_renumbers_positions(
    consists: ConsistManager,
) -> None:
    """Positions stay contiguous after a middle removal so lead/rear
    semantics are preserved for the display side."""
    await consists.create(10)
    for addr in (111, 222, 333):
        await consists.add_member(10, addr)
    await consists.remove_member(10, 222)
    consist = consists.get(10)
    assert consist is not None
    assert [m.address for m in consist.locos] == [111, 333]
    assert [m.position for m in consist.locos] == [0, 1]


async def test_add_member_to_missing_consist_raises(
    consists: ConsistManager,
) -> None:
    with pytest.raises(KeyError):
        await consists.add_member(999, 111)


# ---- fan-out throttle control -------------------------------------------


async def test_set_speed_fans_to_every_member(
    consists: ConsistManager, throttles: ThrottleManager,
) -> None:
    await consists.create(10)
    await consists.add_member(10, 3601, long_address=True)
    await consists.add_member(10, 3602, long_address=True)
    await consists.set_speed(10, 0.42)
    assert throttles.get(3601).speed == 0.42
    assert throttles.get(3602).speed == 0.42


async def test_set_direction_honours_per_loco_orientation(
    consists: ConsistManager, throttles: ThrottleManager,
) -> None:
    """A member marked ``direction_normal=False`` gets the OPPOSITE
    direction -- this is the whole point of software consisting: two
    locos facing opposite ways in the same train move together."""
    await consists.create(10)
    await consists.add_member(10, 111, direction_normal=True)
    await consists.add_member(10, 222, direction_normal=False)
    await consists.set_direction(10, ThrottleDirection.FORWARD)
    assert throttles.get(111).direction == ThrottleDirection.FORWARD
    assert throttles.get(222).direction == ThrottleDirection.REVERSE
    # And flipping the consist flips both symmetrically.
    await consists.set_direction(10, ThrottleDirection.REVERSE)
    assert throttles.get(111).direction == ThrottleDirection.REVERSE
    assert throttles.get(222).direction == ThrottleDirection.FORWARD


async def test_set_function_fans_to_every_member(
    consists: ConsistManager, throttles: ThrottleManager,
) -> None:
    await consists.create(10)
    await consists.add_member(10, 111)
    await consists.add_member(10, 222)
    await consists.set_function(10, 0, True)   # F0 headlight on
    assert throttles.get(111).functions[0] is True
    assert throttles.get(222).functions[0] is True


async def test_emergency_stop_fans_to_every_member(
    consists: ConsistManager, throttles: ThrottleManager,
) -> None:
    await consists.create(10)
    await consists.add_member(10, 111)
    await consists.add_member(10, 222)
    # Give them some non-zero speed first so e-stop is meaningful.
    await consists.set_speed(10, 0.5)
    await consists.emergency_stop(10)
    # Throttle emergency_stop leaves speed at -1 sentinel; both should be there.
    assert throttles.get(111).speed == -1.0
    assert throttles.get(222).speed == -1.0


async def test_set_speed_on_missing_consist_raises(
    consists: ConsistManager,
) -> None:
    with pytest.raises(KeyError):
        await consists.set_speed(999, 0.5)


# ---- runtime drive state (for the /consists page UI) --------------------
#
# The /consists card needs to render the slider at its last-commanded
# position and highlight the active direction button. Without this the
# slider snaps back to 0 on every drag-release because the template
# re-render has no state to draw from. The manager keeps a per-consist
# ``_speeds`` / ``_directions`` dict for that.


async def test_set_speed_records_current_speed(
    consists: ConsistManager,
) -> None:
    await consists.create(20)
    await consists.add_member(20, 111)
    assert consists.current_speed(20) == 0.0
    await consists.set_speed(20, 0.42)
    assert consists.current_speed(20) == 0.42


async def test_set_speed_emits_updated(
    consists: ConsistManager,
) -> None:
    """The updated signal is what triggers the WebSocket push, which is
    what the /consists page listens for to refresh the card. Without
    this the slider position never comes back down the wire."""
    await consists.create(21)
    await consists.add_member(21, 111)
    seen: list[Consist] = []
    consists.updated.connect(lambda c: seen.append(c))
    await consists.set_speed(21, 0.3)
    assert len(seen) == 1
    assert seen[0].address == 21


async def test_set_direction_records_current_direction(
    consists: ConsistManager,
) -> None:
    await consists.create(22)
    await consists.add_member(22, 111)
    assert consists.current_direction(22) is ThrottleDirection.FORWARD
    await consists.set_direction(22, ThrottleDirection.REVERSE)
    assert consists.current_direction(22) is ThrottleDirection.REVERSE


async def test_set_direction_emits_updated(
    consists: ConsistManager,
) -> None:
    await consists.create(23)
    await consists.add_member(23, 111)
    seen: list[Consist] = []
    consists.updated.connect(lambda c: seen.append(c))
    await consists.set_direction(23, ThrottleDirection.REVERSE)
    assert len(seen) == 1


async def test_emergency_stop_resets_recorded_speed(
    consists: ConsistManager,
) -> None:
    """After e-stop the slider should show 0, not the last commanded
    speed -- the train has stopped."""
    await consists.create(24)
    await consists.add_member(24, 111)
    await consists.set_speed(24, 0.7)
    await consists.emergency_stop(24)
    assert consists.current_speed(24) == 0.0


async def test_rebind_throttles_swaps_the_fan_out_target(
    throttles: ThrottleManager,
) -> None:
    """The critical bug this guards: ``SystemConnection.__init__``
    builds the consist manager before drivers install their
    hardware-aware throttle manager in ``open``. Without
    :meth:`rebind_throttles`, consist fan-out silently drives the
    stale pre-open throttle manager, so the consist card looks
    responsive (bean state advances, signals fire) while the actual
    locomotive sits still.

    We assert that after rebinding, :meth:`set_speed` acquires
    against the NEW manager, not the initial one.
    """
    initial = throttles
    consists = ConsistManager(initial, "IT")
    await consists.create(30)
    await consists.add_member(30, 555)
    # A distinct manager stands in for the driver's hardware-aware
    # subclass (NceThrottleManager, LnThrottleManager, ...).
    replacement = ThrottleManager("IT")
    consists.rebind_throttles(replacement)
    await consists.set_speed(30, 0.5)
    # The replacement now holds the throttle. The initial manager was
    # never touched -- proving the fan-out routed correctly.
    assert replacement.get(555) is not None
    assert replacement.get(555).speed == 0.5
    assert initial.get(555) is None


async def test_delete_clears_drive_state(
    consists: ConsistManager,
) -> None:
    """After delete + recreate at the same address, drive state must
    start fresh -- otherwise a new consist inherits a stale slider
    position from a deleted one."""
    await consists.create(25)
    await consists.add_member(25, 111)
    await consists.set_speed(25, 0.5)
    await consists.set_direction(25, ThrottleDirection.REVERSE)
    await consists.delete(25)
    await consists.create(25)
    assert consists.current_speed(25) == 0.0
    assert consists.current_direction(25) is ThrottleDirection.FORWARD


# ---- advanced-consist CV19 auto-programming -----------------------------
#
# For kind="advanced" consists, add_member must write CV19 on the member's
# decoder in ops mode: forward=consist_address, reverse=consist_address+128.
# remove_member writes CV19=0. Fully automatic -- no user "Write" button.


class _FakeOpsProgrammer:
    """Records ops-mode CV writes so tests can assert what CV19 got."""

    def __init__(self) -> None:
        from jmri_core.beans.programmer import ProgrammingMode  # noqa: PLC0415
        self.mode = ProgrammingMode.OPS_MODE
        self.writes: list[tuple[int, int]] = []

    async def read_cv(self, cv: int) -> int:
        raise NotImplementedError

    async def write_cv(self, cv: int, value: int) -> None:
        self.writes.append((cv, value))


class _FakeProgrammerManager:
    def __init__(self) -> None:
        # keyed by (address, long_address)
        self.by_addr: dict[tuple[int, bool], _FakeOpsProgrammer] = {}

    def global_programmer(self):  # noqa: ANN201
        return None

    def ops_programmer(
        self, address: int, *, long_address: bool = False,
    ) -> _FakeOpsProgrammer:
        key = (address, long_address)
        prog = self.by_addr.get(key)
        if prog is None:
            prog = _FakeOpsProgrammer()
            self.by_addr[key] = prog
        return prog


async def test_advanced_add_member_writes_cv19_forward(
    throttles: ThrottleManager,
) -> None:
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3, direction_normal=True)
    writes = progs.ops_programmer(3, long_address=False).writes
    # Forward: CV19 = consist_address, bit 7 clear.
    assert writes == [(19, 50)]


async def test_advanced_add_member_writes_cv19_reverse(
    throttles: ThrottleManager,
) -> None:
    """Direction bit is CV19 bit 7 (per NMRA S-9.2.2). Reverse member
    gets consist_address + 128."""
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3, direction_normal=False)
    writes = progs.ops_programmer(3, long_address=False).writes
    assert writes == [(19, 50 | 0x80)]


async def test_advanced_remove_member_clears_cv19(
    throttles: ThrottleManager,
) -> None:
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3)
    prog = progs.ops_programmer(3, long_address=False)
    prog.writes.clear()      # drop the add-time CV19
    await cm.remove_member(50, 3)
    assert prog.writes == [(19, 0)]


async def test_advanced_readd_updates_cv19_direction(
    throttles: ThrottleManager,
) -> None:
    """Re-adding a member with a flipped direction rewrites CV19 with
    the new direction bit. The ``add_member`` idempotency path (loco
    already in the consist) is where this happens."""
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3, direction_normal=True)
    await cm.add_member(50, 3, direction_normal=False)  # flip
    prog = progs.ops_programmer(3, long_address=False)
    assert prog.writes == [(19, 50), (19, 50 | 0x80)]


async def test_software_and_command_station_consists_do_not_touch_cv19(
    throttles: ThrottleManager,
) -> None:
    """Only ``advanced`` triggers CV19 programming. Software fans
    commands out via throttles; command_station uses driver-specific
    group ops. Neither should touch the decoder's CV19."""
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(1, kind="software")
    await cm.create(2, kind="command_station")
    await cm.add_member(1, 3)
    await cm.add_member(2, 4)
    assert progs.by_addr == {}


async def test_cv19_write_failure_is_logged_not_raised(
    throttles: ThrottleManager,
) -> None:
    """A POM write can fail (no ack, loco not on the track). The user's
    add-member action must still succeed at the bean level -- the
    consist itself is configured, and the user can retry."""
    class _FailingProg(_FakeOpsProgrammer):
        async def write_cv(self, cv: int, value: int) -> None:
            raise TimeoutError("no ack from decoder")

    class _FailingMgr(_FakeProgrammerManager):
        def ops_programmer(self, address: int, *, long_address: bool = False):  # noqa: ANN201
            return _FailingProg()

    cm = ConsistManager(throttles, "IT", _FailingMgr())
    await cm.create(50, kind="advanced")
    # Must NOT raise.
    loco = await cm.add_member(50, 3)
    assert loco.address == 3
    assert cm.get(50).locos[0].address == 3


async def test_advanced_consist_without_programmer_is_silent_noop(
    throttles: ThrottleManager,
) -> None:
    """No programmer manager (base InternalConnection, unit test setup)
    means CV19 writes are impossible -- the consist still stores the
    membership as data. This matches how the old software fallback
    behaved for kind='advanced' before we could program at all."""
    cm = ConsistManager(throttles, "IT")  # programmers=None
    await cm.create(50, kind="advanced")
    loco = await cm.add_member(50, 3)
    assert loco.address == 3


# ---- advanced-consist DRIVE routing --------------------------------------
#
# Once CV19 is written, each member decoder responds to packets on the
# consist address, not its own. So drive commands (speed, direction,
# e-stop) must be addressed to the consist number, not fanned out to
# member addresses -- fanning out would send packets that the decoders
# now ignore. Functions are the exception: NMRA S-9.2.2 keeps them on
# the individual loco address by default (CV21/CV22 aren't touched).


async def test_advanced_consist_speed_drives_consist_address(
    throttles: ThrottleManager,
) -> None:
    """set_speed on an advanced consist sends one command to the consist
    address, not per-member commands. The user-facing bug this pins:
    after CV19 is written, per-member throttle packets are ignored by
    the decoders and the locomotives sit still."""
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3, direction_normal=True)
    await cm.add_member(50, 4, direction_normal=False)
    await cm.set_speed(50, 0.42)
    # The consist address itself is a live throttle now.
    consist_throttle = throttles.get(50)
    assert consist_throttle is not None
    assert consist_throttle.speed == 0.42
    # No throttle was acquired for the member addresses.
    assert throttles.get(3) is None
    assert throttles.get(4) is None


async def test_advanced_consist_direction_drives_consist_address(
    throttles: ThrottleManager,
) -> None:
    """Direction goes to the consist address; per-loco orientation is
    encoded in CV19 bit 7 on the decoder itself, so the CS doesn't
    need to invert per member. The decoder inverts."""
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3, direction_normal=True)
    await cm.add_member(50, 4, direction_normal=False)
    await cm.set_direction(50, ThrottleDirection.REVERSE)
    consist_throttle = throttles.get(50)
    assert consist_throttle is not None
    assert consist_throttle.direction is ThrottleDirection.REVERSE
    assert throttles.get(3) is None
    assert throttles.get(4) is None


async def test_advanced_consist_estop_drives_consist_address(
    throttles: ThrottleManager,
) -> None:
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3)
    await cm.set_speed(50, 0.5)
    await cm.emergency_stop(50)
    consist_throttle = throttles.get(50)
    assert consist_throttle is not None
    assert consist_throttle.speed == -1.0
    assert throttles.get(3) is None


async def test_advanced_consist_functions_still_fan_out(
    throttles: ThrottleManager,
) -> None:
    """F0/F1/... default to the individual loco address per NMRA S-9.2.2
    (CV21/CV22 unset). So functions fan out even for advanced consists
    -- otherwise the headlight on the trailing loco would never light
    up."""
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3)
    await cm.add_member(50, 4)
    await cm.set_function(50, 0, True)
    # Each member got a throttle and its function.
    assert throttles.get(3).functions[0] is True
    assert throttles.get(4).functions[0] is True


# ---- advanced-consist DELETE clears CV19 --------------------------------
#
# Missing this leaves the loco stuck: it's no longer in a consist in
# our tables, but its decoder still ignores its own address until
# CV19 is manually zeroed.


async def test_delete_advanced_consist_clears_cv19_on_every_member(
    throttles: ThrottleManager,
) -> None:
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="advanced")
    await cm.add_member(50, 3)
    await cm.add_member(50, 4)
    # Clear the setup CV19 writes so we can pin what delete emits.
    for prog in progs.by_addr.values():
        prog.writes.clear()

    assert await cm.delete(50) is True

    assert progs.ops_programmer(3, long_address=False).writes == [(19, 0)]
    assert progs.ops_programmer(4, long_address=False).writes == [(19, 0)]


async def test_delete_software_consist_does_not_touch_cv19(
    throttles: ThrottleManager,
) -> None:
    """Software / command-station consists never wrote CV19, so
    deletion doesn't touch it either. Regression pin: an operator
    running software mode shouldn't see mystery CV19 writes on
    consist teardown."""
    progs = _FakeProgrammerManager()
    cm = ConsistManager(throttles, "IT", progs)
    await cm.create(50, kind="software")
    await cm.add_member(50, 3)
    await cm.delete(50)
    assert progs.by_addr == {}


# ---- SystemConnection wiring --------------------------------------------


async def test_internal_connection_has_a_consist_manager() -> None:
    """The default SystemConnection wires up a ConsistManager so every
    driver (including InternalConnection) exposes ``conn.consists``."""
    from jmri_core.hal.internal import InternalConnection
    conn = InternalConnection()
    await conn.open({})
    try:
        assert isinstance(conn.consists, ConsistManager)
        # And it's a *software* consist manager for the default path.
        assert conn.consists.default_kind == "software"
        # CRUD works end-to-end.
        await conn.consists.create(50)
        await conn.consists.add_member(50, 111)
        assert len(conn.consists) == 1
        assert conn.consists.get(50).locos[0].address == 111
    finally:
        await conn.close()
