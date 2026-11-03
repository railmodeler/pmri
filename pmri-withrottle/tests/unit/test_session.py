"""``WiThrottleSession`` -- per-connection state and command dispatch."""
from __future__ import annotations

from pmri_core.beans.throttle_manager import ThrottleManager
from pmri_core.state import ThrottleDirection

from pmri_withrottle.session import WiThrottleSession


def _make_session() -> tuple[WiThrottleSession, list[str], ThrottleManager]:
    """Build a session with an isolated ThrottleManager + line capture."""
    mgr = ThrottleManager(system_prefix="N")
    sent: list[str] = []

    async def send(line: str) -> None:
        sent.append(line)

    sess = WiThrottleSession(throttle_mgr=mgr, send_line=send)
    return sess, sent, mgr


# ----- hello / quit --------------------------------------------------------


async def test_hello_name_captures_client_name() -> None:
    sess, _, _ = _make_session()
    await sess.dispatch("NEngineDriver")
    assert sess.client_name == "EngineDriver"


async def test_quit_returns_false() -> None:
    sess, _, _ = _make_session()
    keep = await sess.dispatch("Q")
    assert keep is False


async def test_heartbeat_is_echoed_to_client() -> None:
    """Engine Driver expects ``*`` pings to come back as ``*`` -- without
    the echo it eventually decides the link is dead and reconnects."""
    sess, sent, _ = _make_session()
    sent.clear()
    keep = await sess.dispatch("*")
    assert keep is True
    assert sent == ["*"]


async def test_heartbeat_piggybacks_fast_clock_sync() -> None:
    """The heartbeat carries a ``PFT`` sync line when the server has a
    fast-clock provider. Without the piggyback, the client's on-screen
    fast clock sits at 12:00 PM waiting for the sync that never arrives."""
    mgr = ThrottleManager(system_prefix="N")

    async def send(line: str) -> None:
        sent.append(line)

    sent: list[str] = []
    sess = WiThrottleSession(
        throttle_mgr=mgr,
        send_line=send,
        fast_clock_provider=lambda: "PFT1700000000<;>4.0",
    )
    await sess.dispatch("*")
    assert "*" in sent
    assert "PFT1700000000<;>4.0" in sent


# ----- acquire / release ---------------------------------------------------


async def test_acquire_creates_throttle_and_acks() -> None:
    sess, sent, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>label")
    # ThrottleManager has the loco.
    bean = mgr.get(3601)
    assert bean is not None
    assert bean.long_address is True
    # And the ack was sent back to the client.
    assert "MT+L3601<;>" in sent


async def test_acquire_broadcasts_initial_state() -> None:
    """After acquire, the server sends the labels line with non-empty
    defaults, then F0..F28 states, then speed, direction, speed-step.

    The labels line matters:
    * Roster acquires hide the function bar without it.
    * Manual acquires hide the bar if labels are empty.

    So we always send it populated with F0..F28 defaults.
    """
    sess, sent, _ = _make_session()
    sent.clear()
    await sess.dispatch("MT+L3601<;>label")
    # Ack first.
    assert sent[0] == "MT+L3601<;>"
    # Labels line uses cosmetic defaults matching Engine Driver's own
    # manual-acquire labels (Light/Bell/Horn) so roster + manual paths
    # look the same.
    labels_line = next(l for l in sent if l.startswith("MTLL3601<;>"))
    assert "Light" in labels_line
    assert "Bell" in labels_line
    assert "Horn" in labels_line
    assert "F28" in labels_line
    # No empty label slots on the wire.
    assert "]\\[]\\[" not in labels_line
    # Function states, speed, direction, speed-step all present.
    assert "MTAL3601<;>F00" in sent
    assert "MTAL3601<;>F028" in sent
    assert "MTAL3601<;>V0" in sent
    assert "MTAL3601<;>R1" in sent
    assert "MTAL3601<;>s1" in sent


async def test_release_drops_throttle_and_acks() -> None:
    sess, sent, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    sent.clear()
    await sess.dispatch("MT-L3601<;>")
    assert mgr.get(3601) is None
    assert "MT-L3601<;>" in sent


async def test_wildcard_release_drops_every_throttle_on_multiplex() -> None:
    """Engine Driver's Release button sends ``M{T,S}-*<;>`` (wildcard
    address). If we match only the exact token we silently leave the
    previously-acquired throttle in ``_acquired``, and the next
    acquire on the same slot coexists with the ghost -- every
    broadcast throttle-action then fans to BOTH locos. Verified on
    NCE hardware 2026-09-18: acquire 5, release with wildcard,
    acquire 463, drive 463 -> 5 moves too because it was never
    dropped."""
    sess, sent, mgr = _make_session()
    await sess.dispatch("MT+S5<;>")
    await sess.dispatch("MT+L3601<;>")   # two locos on multiplex T
    sent.clear()
    await sess.dispatch("MT-*<;>")       # wildcard release
    # Both are gone from the manager AND the session.
    assert mgr.get(5) is None
    assert mgr.get(3601) is None
    # Server acked both -- clients rely on the ack to update UI.
    assert "MT-S5<;>" in sent
    assert "MT-L3601<;>" in sent


async def test_wildcard_release_does_not_touch_other_multiplex_slots() -> None:
    """Release on multiplex T must leave throttles on S alone. Otherwise
    releasing "primary" would also drop the "secondary" throttle."""
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+S5<;>")
    await sess.dispatch("MS+L3601<;>")   # secondary throttle
    await sess.dispatch("MT-*<;>")
    assert mgr.get(5) is None
    assert mgr.get(3601) is not None     # S-slot throttle untouched


async def test_action_after_wildcard_release_only_hits_new_acquire() -> None:
    """End-to-end pin for the reported bug: after wildcard release +
    new acquire, a broadcast throttle command must NOT drive the
    previously-acquired loco. Without the wildcard-release fix the
    old bean picks up commands too."""
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+S5<;>")
    await sess.dispatch("MT-*<;>")
    await sess.dispatch("MT+L463<;>")
    await sess.dispatch("MTA*<;>V64")    # broadcast speed on T
    # 463 got the speed; 5 is gone.
    assert mgr.get(5) is None
    assert mgr.get(463) is not None
    assert 0.4 < mgr.get(463).speed < 0.6


async def test_multi_unit_consist_via_repeat_acquire_on_same_throttle() -> None:
    """WiThrottle's multi-unit consist building IS the acquire path:
    the client calls ``M<id>+`` multiple times with different
    addresses on the same throttle character, and subsequent
    wildcard throttle actions fan to every acquired loco.

    This is JMRI's documented MU-building shape -- there's no separate
    MU opcode. Pinning here so the "throttle multiplex" semantics
    don't regress."""
    sess, _, mgr = _make_session()
    # Acquire two locos on multiplex slot 0 (Engine Driver's primary).
    await sess.dispatch("M0+L1234<;>Fred")
    await sess.dispatch("M0+L5678<;>Wilma")
    # Broadcast speed to the whole multiplex -> both move.
    await sess.dispatch("M0A*<;>V64")
    assert mgr.get(1234) is not None
    assert mgr.get(5678) is not None
    assert 0.4 < mgr.get(1234).speed < 0.6
    assert 0.4 < mgr.get(5678).speed < 0.6


async def test_drop_one_member_of_mu_leaves_other_running() -> None:
    """Releasing a specific MU member leaves siblings on the same
    throttle slot acquired. The wildcard release path (b13 fix)
    drops *every* throttle on a slot; targeted release by token
    drops exactly one."""
    sess, _, mgr = _make_session()
    await sess.dispatch("M0+L1234<;>Fred")
    await sess.dispatch("M0+L5678<;>Wilma")
    await sess.dispatch("M0A*<;>V64")
    assert mgr.get(1234).speed > 0.4
    assert mgr.get(5678).speed > 0.4
    # Drop just 1234. 5678 keeps running.
    await sess.dispatch("M0-L1234<;>")
    assert mgr.get(1234) is None
    assert mgr.get(5678) is not None
    # Next wildcard speed only reaches 5678.
    await sess.dispatch("M0A*<;>V32")
    assert 0.2 < mgr.get(5678).speed < 0.3


async def test_route_action_dispatches_to_handler() -> None:
    """A ``PRA2IO1`` from the client reaches ``route_action_handler``
    with the parsed system_name + action verb."""
    mgr = ThrottleManager(system_prefix="N")
    sent: list[str] = []
    calls: list[tuple[str, str]] = []

    async def send(line: str) -> None:
        sent.append(line)

    async def handler(sysname: str, action: str) -> None:
        calls.append((sysname, action))

    sess = WiThrottleSession(
        throttle_mgr=mgr, send_line=send, route_action_handler=handler,
    )
    await sess.dispatch("PRA2IO1")
    assert calls == [("IO1", "2")]


async def test_route_action_without_handler_is_silently_dropped() -> None:
    """When no route handler is wired we must not crash. Mirrors the
    turnout path's forgiving behaviour."""
    mgr = ThrottleManager(system_prefix="N")

    async def send(_: str) -> None:
        pass

    sess = WiThrottleSession(throttle_mgr=mgr, send_line=send)
    # Must not raise.
    await sess.dispatch("PRA2IO1")


async def test_release_zeros_speed_before_dropping() -> None:
    """Release must stop the loco before returning the throttle to the
    pool. Without this the driver's command station keeps refreshing
    the last commanded speed to the released address, and the operator
    sees the loco keep moving after Release + Select-different-loco.
    Verified on NCE hardware 2026-09-18."""
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.dispatch("MTAL3601<;>V64")
    bean = mgr.get(3601)
    assert bean is not None
    assert bean.speed > 0.4    # driven up
    # Release. Keep a ref so we can inspect state after mgr forgets it.
    released_bean = bean
    await sess.dispatch("MT-L3601<;>")
    assert mgr.get(3601) is None
    # The core :meth:`_release` set speed to 0 before removing the
    # throttle, so the driver's wire path already emitted a
    # speed-zero packet for this address.
    assert released_bean.speed == 0.0


async def test_release_of_consist_zeros_the_consist() -> None:
    """A consist WiThrottle release must fan zero-speed to every
    member via the consist manager, not just the lead throttle -- else
    trailing units in a software consist keep running after release."""
    from pmri_core.beans.consist_manager import ConsistManager  # noqa: PLC0415
    from pmri_core.state import ThrottleDirection  # noqa: PLC0415

    mgr = ThrottleManager(system_prefix="N")
    consists = ConsistManager(mgr, "N")
    await consists.create(50)
    await consists.add_member(50, 100)
    await consists.add_member(50, 101)

    sent: list[str] = []

    async def send(line: str) -> None:
        sent.append(line)

    sess = WiThrottleSession(
        throttle_mgr=mgr, send_line=send, consist_mgr=consists,
    )
    # Acquire the consist address on the primary throttle slot.
    await sess.dispatch("MT+S50<;>")
    # Drive it -- both members reach a positive speed.
    await sess.dispatch("MTAS50<;>V64")
    assert mgr.get(100).speed > 0.4
    assert mgr.get(101).speed > 0.4
    # Release. Every member speed drops to 0.
    a = mgr.get(100)
    b = mgr.get(101)
    await sess.dispatch("MT-S50<;>")
    assert a.speed == 0.0
    assert b.speed == 0.0


async def test_quit_releases_all_acquired() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.dispatch("MS+S3<;>")
    await sess.dispatch("Q")
    assert mgr.get(3601) is None
    assert mgr.get(3) is None


# ----- actions -------------------------------------------------------------


async def test_speed_action_maps_to_throttle_bean() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.dispatch("MTAL3601<;>V64")
    bean = mgr.get(3601)
    assert bean is not None
    # 64 of 126 is about half speed.
    assert 0.4 < bean.speed < 0.6


async def test_action_with_wildcard_address_broadcasts_to_all_on_multiplex() -> None:
    """Engine Driver sends speed changes with the ``*`` wildcard address
    token to mean "every throttle on this multiplex character". Without
    ``*`` support, the direction-uses-explicit-token / speed-uses-wildcard
    split makes direction work but speed silently drop."""
    sess, _, mgr = _make_session()
    # Engine Driver uses "0" as the throttle multiplex character, not "T".
    await sess.dispatch("M0+L463<;>label")
    await sess.dispatch("M0A*<;>V64")
    bean = mgr.get(463)
    assert bean is not None
    assert 0.4 < bean.speed < 0.6


async def test_action_with_wildcard_targets_only_matching_multiplex() -> None:
    """``M0A*`` must not touch throttles acquired on ``MT``."""
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")   # on T multiplex
    await sess.dispatch("M0+L463<;>")    # on 0 multiplex
    await sess.dispatch("M0A*<;>V64")     # 0-only wildcard
    # 463 (0-multiplex) moved; 3601 (T-multiplex) didn't.
    assert 0.4 < mgr.get(463).speed < 0.6
    assert mgr.get(3601).speed == 0.0


async def test_direction_action_sets_reverse() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.dispatch("MTAL3601<;>R0")
    bean = mgr.get(3601)
    assert bean.direction == ThrottleDirection.REVERSE


async def test_function_press_then_release() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.dispatch("MTAL3601<;>F18")    # F8 press
    bean = mgr.get(3601)
    assert bean.functions[8] is True
    await sess.dispatch("MTAL3601<;>F08")    # F8 release
    assert bean.functions[8] is False


async def test_estop_action_zeroes_speed() -> None:
    sess, sent, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.dispatch("MTAL3601<;>V100")
    sent.clear()
    await sess.dispatch("MTAL3601<;>X")
    bean = mgr.get(3601)
    # emergency_stop puts speed at -1.0 (the e-stop sentinel)
    assert bean.speed == -1.0
    # And the wire response uses the e-stop speed sentinel ``V1``.
    assert any(line.endswith("V1") for line in sent)


async def test_idle_action_zero_speed() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.dispatch("MTAL3601<;>V100")
    await sess.dispatch("MTAL3601<;>I")
    bean = mgr.get(3601)
    assert bean.speed == 0.0


async def test_unknown_action_silently_ignored() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    # An action we don't model (e.g. "L" speed-step mode).
    await sess.dispatch("MTAL3601<;>L128")
    # Throttle still acquired, no exception, no state change.
    assert mgr.get(3601) is not None


# ----- MU / consist integration -------------------------------------------


def _make_session_with_consist() -> tuple[
    WiThrottleSession, list[str], ThrottleManager,
    "ConsistManager",  # noqa: F821 -- forward ref
]:
    """Session backed by both a ThrottleManager AND a ConsistManager
    so consist-address acquires route through the fan-out."""
    from pmri_core.beans.consist_manager import ConsistManager  # noqa: PLC0415
    mgr = ThrottleManager(system_prefix="N")
    cm = ConsistManager(mgr, system_prefix="N")
    sent: list[str] = []

    async def send(line: str) -> None:
        sent.append(line)

    sess = WiThrottleSession(
        throttle_mgr=mgr, consist_mgr=cm, send_line=send,
    )
    return sess, sent, mgr, cm


async def test_acquire_consist_address_binds_to_consist() -> None:
    """Acquiring an address that matches a server-side consist stores
    the consist address on the _Acquired record. Subsequent throttle
    actions must fan through the consist manager."""
    sess, sent, mgr, cm = _make_session_with_consist()
    # Set up a two-loco software consist at address 100.
    await cm.create(100)
    await cm.add_member(100, 111)
    await cm.add_member(100, 222)
    sent.clear()
    # Client acquires the consist address.
    await sess.dispatch("MT+S100<;>label")
    # Ack came back.
    assert any(s.startswith("MT+S100") for s in sent)
    # And the acquired record remembers the consist.
    key = ("T", "S100")
    assert key in sess._acquired
    assert sess._acquired[key].consist_address == 100


async def test_speed_on_consist_fans_to_every_member() -> None:
    """A ``V<n>`` action on a consist-bound throttle drives every
    member's throttle via ``ConsistManager.set_speed`` rather than the
    lead's throttle alone."""
    sess, _, mgr, cm = _make_session_with_consist()
    await cm.create(100)
    await cm.add_member(100, 111)
    await cm.add_member(100, 222)
    await sess.dispatch("MT+S100<;>label")
    await sess.dispatch("MTAS100<;>V64")
    # Both members' throttles reached ~50% (wire 64 -> fraction (64-1)/125).
    t1 = mgr.get(111)
    t2 = mgr.get(222)
    assert t1 is not None and t2 is not None
    assert abs(t1.speed - t2.speed) < 1e-6
    assert 0.4 < t1.speed < 0.6


async def test_direction_on_consist_fans_with_inversion() -> None:
    """Direction commands fan to every member, respecting per-loco
    ``direction_normal`` -- a reverse-oriented member gets the opposite
    direction so the whole train moves together."""
    sess, _, mgr, cm = _make_session_with_consist()
    await cm.create(20)
    await cm.add_member(20, 111, direction_normal=True)
    await cm.add_member(20, 222, direction_normal=False)
    await sess.dispatch("MT+S20<;>label")
    # Client picks forward.
    await sess.dispatch("MTAS20<;>R1")
    assert mgr.get(111).direction == ThrottleDirection.FORWARD
    # Follower is oriented backwards -> gets REVERSE.
    assert mgr.get(222).direction == ThrottleDirection.REVERSE


async def test_estop_on_consist_stops_every_member() -> None:
    sess, _, mgr, cm = _make_session_with_consist()
    await cm.create(30)
    await cm.add_member(30, 111)
    await cm.add_member(30, 222)
    await sess.dispatch("MT+S30<;>label")
    # First give them speed so e-stop is meaningful.
    await sess.dispatch("MTAS30<;>V64")
    await sess.dispatch("MTAS30<;>X")
    assert mgr.get(111).speed == -1.0
    assert mgr.get(222).speed == -1.0


async def test_function_on_consist_fans_to_every_member() -> None:
    sess, _, mgr, cm = _make_session_with_consist()
    await cm.create(40)
    await cm.add_member(40, 111)
    await cm.add_member(40, 222)
    await sess.dispatch("MT+S40<;>label")
    # F0 headlight on. Wire form is ``F10`` (state 1, function 0).
    await sess.dispatch("MTAS40<;>F10")
    assert mgr.get(111).functions[0] is True
    assert mgr.get(222).functions[0] is True


async def test_acquire_of_non_consist_address_still_normal() -> None:
    """Regression: a plain address (not a consist) must acquire a
    single throttle exactly as before -- the MU wiring is transparent."""
    sess, _, mgr, cm = _make_session_with_consist()
    # No consist at 999.
    await sess.dispatch("MT+L999<;>label")
    key = ("T", "L999")
    assert key in sess._acquired
    assert sess._acquired[key].consist_address is None
    await sess.dispatch("MTAL999<;>V64")
    t = mgr.get(999)
    assert t is not None
    assert t.speed > 0


async def test_action_on_unacquired_throttle_is_noop() -> None:
    """If the client somehow sends a V before M+, we don't blow up."""
    sess, _, mgr = _make_session()
    await sess.dispatch("MTAL3601<;>V50")
    # No bean was created.
    assert mgr.get(3601) is None


async def test_bad_address_token_doesnt_acquire() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+Lnotanumber<;>")
    # No bean for the malformed address.
    assert len(list(mgr)) == 0


# ----- session close -------------------------------------------------------


async def test_turnout_action_routes_to_handler() -> None:
    """``PTATNT12`` dispatches to the turnout action handler with the
    parsed ``(system_name, action)`` pair."""
    mgr = ThrottleManager(system_prefix="N")
    calls: list[tuple[str, str]] = []

    async def send(_line: str) -> None:
        pass

    async def handler(sys_name: str, action: str) -> None:
        calls.append((sys_name, action))

    sess = WiThrottleSession(
        throttle_mgr=mgr, send_line=send, turnout_action_handler=handler,
    )
    await sess.dispatch("PTATNT12")
    await sess.dispatch("PTACNT13")
    await sess.dispatch("PTA2NT14")
    assert calls == [("NT12", "T"), ("NT13", "C"), ("NT14", "2")]


async def test_turnout_action_without_handler_is_silent() -> None:
    """Sessions without a turnout handler silently drop PTA actions
    rather than raising."""
    sess, _, _ = _make_session()
    keep = await sess.dispatch("PTATNT12")
    assert keep is True


async def test_close_releases_everything() -> None:
    sess, _, mgr = _make_session()
    await sess.dispatch("MT+L3601<;>")
    await sess.close()
    assert mgr.get(3601) is None
