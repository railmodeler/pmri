"""EX-RAIL manager -- introspection request/response pairing + control
command encodings.

Validated against:
https://dcc-ex.com/exrail/exrail-command-reference.html
https://dcc-ex.com/throttles/tech-reference.html
"""
from __future__ import annotations

import asyncio

import pytest

from pmri_dccex.codec import DccexMessage, encode
from pmri_dccex.exrail import DccexExRailManager, ExRailAspect, ExRailKind
from pmri_dccex.traffic import DccexTrafficController
from pmri_dccex.transport import loopback_pair


def _setup():  # type: ignore[no-untyped-def]
    """Build a loopback transport, a TC on one end, and the second
    port the test uses as the fake command station."""
    a, b = loopback_pair()
    tc = DccexTrafficController(a)
    mgr = DccexExRailManager(tc)
    return tc, mgr, b


# ---- introspection: list_ids ----------------------------------------------


async def test_list_ids_sends_JA_and_parses_jA_reply() -> None:
    """``list_ids()`` sends ``<JA>`` and resolves on the first
    ``<jA ...>`` whose args all parse as ints -- distinguishing it
    from the ``<jA id R|A|X ...>`` detail form."""
    tc, mgr, b = _setup()
    await tc.start()

    async def ack() -> None:
        await asyncio.sleep(0.01)
        # Simulate the CS replying by writing into the second end of
        # the loopback. The TC's read-loop will pick it up.
        await b.write(encode(DccexMessage(command="jA", args=("13", "16", "23"))))

    asyncio.create_task(ack())
    try:
        ids = await mgr.list_ids(timeout=2.0)
    finally:
        await tc.aclose()
    assert ids == (13, 16, 23)


async def test_list_ids_empty_firmware_returns_empty_tuple() -> None:
    """A CS without EX-RAIL compiled in replies with a bare
    ``<jA>`` (no ids). Must come back as ``()``, not an error."""
    tc, mgr, b = _setup()
    await tc.start()

    async def ack() -> None:
        await asyncio.sleep(0.01)
        await b.write(encode(DccexMessage(command="jA", args=())))

    asyncio.create_task(ack())
    try:
        ids = await mgr.list_ids(timeout=2.0)
    finally:
        await tc.aclose()
    assert ids == ()


async def test_list_ids_ignores_detail_shaped_jA_replies() -> None:
    """A ``<jA 7 R "name">`` arriving before the list reply must NOT
    satisfy ``list_ids`` -- the second arg ``R`` fails int-parse, so
    the listener loops. The test proves the discriminator holds."""
    tc, mgr, b = _setup()
    await tc.start()

    async def ack() -> None:
        await asyncio.sleep(0.01)
        # Detail-shaped reply first; list-shaped reply second.
        await b.write(encode(DccexMessage(
            command="jA", args=("7", "R", '"Yard entry"'),
        )))
        await asyncio.sleep(0.01)
        await b.write(encode(DccexMessage(
            command="jA", args=("13", "16"),
        )))

    asyncio.create_task(ack())
    try:
        ids = await mgr.list_ids(timeout=2.0)
    finally:
        await tc.aclose()
    assert ids == (13, 16)


async def test_list_ids_timeout_raises() -> None:
    tc, mgr, _ = _setup()
    await tc.start()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await mgr.list_ids(timeout=0.05)
    finally:
        await tc.aclose()


# ---- introspection: detail ------------------------------------------------


async def test_detail_route_parses_description_with_space() -> None:
    """The description payload is quoted on the wire; our codec
    whitespace-splits so a multi-word quoted description arrives as
    multiple args. The manager must rejoin them before stripping the
    quotes. Pin the full round-trip here."""
    tc, mgr, b = _setup()
    await tc.start()

    async def ack() -> None:
        await asyncio.sleep(0.01)
        # The CS emits a single bracketed message with the whole
        # quoted description inside. DccexMessage.raw joins args with
        # a single space; the receiver's codec splits on whitespace
        # and the manager rejoins + strips.
        await b.write(encode(DccexMessage(
            command="jA", args=("13", "R", '"Yard', 'entry"'),
        )))

    asyncio.create_task(ack())
    try:
        entry = await mgr.detail(13, timeout=2.0)
    finally:
        await tc.aclose()
    assert entry is not None
    assert entry.id == 13
    assert entry.kind is ExRailKind.ROUTE
    assert entry.description == "Yard entry"


async def test_detail_automation_decodes_A_kind() -> None:
    tc, mgr, b = _setup()
    await tc.start()

    async def ack() -> None:
        await asyncio.sleep(0.01)
        await b.write(encode(DccexMessage(
            command="jA", args=("4", "A", '"Shunt"'),
        )))

    asyncio.create_task(ack())
    try:
        entry = await mgr.detail(4, timeout=2.0)
    finally:
        await tc.aclose()
    assert entry is not None
    assert entry.kind is ExRailKind.AUTOMATION
    assert entry.description == "Shunt"


async def test_detail_not_found_returns_none() -> None:
    """``<jA id X>`` means "no such id" -- the manager returns None
    rather than raising, so a caller can distinguish "firmware never
    had this id" from a transport error."""
    tc, mgr, b = _setup()
    await tc.start()

    async def ack() -> None:
        await asyncio.sleep(0.01)
        await b.write(encode(DccexMessage(command="jA", args=("99", "X"))))

    asyncio.create_task(ack())
    try:
        entry = await mgr.detail(99, timeout=2.0)
    finally:
        await tc.aclose()
    assert entry is None


async def test_detail_ignores_jA_for_other_id() -> None:
    """A ``<jA 4 R "...">`` arriving while we're waiting for id=7 must
    be ignored -- otherwise concurrent calls would cross-contaminate."""
    tc, mgr, b = _setup()
    await tc.start()

    async def ack() -> None:
        await asyncio.sleep(0.01)
        await b.write(encode(DccexMessage(
            command="jA", args=("4", "R", '"Wrong"'),
        )))
        await asyncio.sleep(0.01)
        await b.write(encode(DccexMessage(
            command="jA", args=("7", "A", '"Right"'),
        )))

    asyncio.create_task(ack())
    try:
        entry = await mgr.detail(7, timeout=2.0)
    finally:
        await tc.aclose()
    assert entry is not None
    assert entry.id == 7
    assert entry.description == "Right"


# ---- control commands (fire-and-forget wire encoding) ---------------------


async def _collect_outbound(b, count: int, *, timeout: float = 1.0) -> list[bytes]:
    """Drain *count* messages from the loopback's CS-facing side."""
    from pmri_dccex.codec import read_message  # noqa: PLC0415
    deadline = asyncio.get_event_loop().time() + timeout
    out: list[bytes] = []
    for _ in range(count):
        remain = max(0.01, deadline - asyncio.get_event_loop().time())
        try:
            text = await asyncio.wait_for(
                read_message(b.read_byte), timeout=remain,
            )
        except TimeoutError:
            break
        out.append(text.encode())
    return out


async def test_start_route_sends_slash_START_id() -> None:
    """``</ START 7>`` -- no cab assignment for a plain route."""
    tc, mgr, b = _setup()
    try:
        await mgr.start_route(7)
        [out] = await _collect_outbound(b, 1)
        assert out == b"/ START 7"
    finally:
        await tc.aclose()


async def test_start_automation_sends_slash_START_cab_id() -> None:
    """``</ START 3 7>`` -- cab goes BEFORE the id per the EX-RAIL
    command reference's ``</ START loco_addr route_id>`` form."""
    tc, mgr, b = _setup()
    try:
        await mgr.start_automation(id=7, cab=3)
        [out] = await _collect_outbound(b, 1)
        assert out == b"/ START 3 7"
    finally:
        await tc.aclose()


async def test_kill_by_id_sends_slash_KILL_id() -> None:
    tc, mgr, b = _setup()
    try:
        await mgr.kill(42)
        [out] = await _collect_outbound(b, 1)
        assert out == b"/ KILL 42"
    finally:
        await tc.aclose()


async def test_kill_all_normalises_to_uppercase() -> None:
    """``kill("all")`` must emit ``</ KILL ALL>`` -- the firmware
    matches on the exact token so case-fold is critical."""
    tc, mgr, b = _setup()
    try:
        await mgr.kill("all")
        [out] = await _collect_outbound(b, 1)
        assert out == b"/ KILL ALL"
    finally:
        await tc.aclose()


async def test_pause_and_resume_emit_bare_slash_verbs() -> None:
    """``</ PAUSE>`` and ``</ RESUME>`` take no args. PAUSE also
    E-STOPs every active loco per the EX-RAIL docs."""
    tc, mgr, b = _setup()
    try:
        await mgr.pause()
        await mgr.resume()
        outs = await _collect_outbound(b, 2)
        assert outs == [b"/ PAUSE", b"/ RESUME"]
    finally:
        await tc.aclose()


async def test_set_signal_emits_aspect_slash_command() -> None:
    tc, mgr, b = _setup()
    try:
        await mgr.set_signal(ExRailAspect.RED, 42)
        await mgr.set_signal(ExRailAspect.AMBER, 42)
        await mgr.set_signal(ExRailAspect.GREEN, 42)
        outs = await _collect_outbound(b, 3)
        assert outs == [b"/ RED 42", b"/ AMBER 42", b"/ GREEN 42"]
    finally:
        await tc.aclose()


async def test_reserve_and_free_block() -> None:
    tc, mgr, b = _setup()
    try:
        await mgr.reserve_block(5)
        await mgr.free_block(5)
        outs = await _collect_outbound(b, 2)
        assert outs == [b"/ RESERVE 5", b"/ FREE 5"]
    finally:
        await tc.aclose()


async def test_latch_and_unlatch() -> None:
    tc, mgr, b = _setup()
    try:
        await mgr.latch(100)
        await mgr.unlatch(100)
        outs = await _collect_outbound(b, 2)
        assert outs == [b"/ LATCH 100", b"/ UNLATCH 100"]
    finally:
        await tc.aclose()


async def test_set_logging_toggles_D_EXRAIL() -> None:
    """``<D EXRAIL ON>`` / ``<D EXRAIL OFF>`` -- diagnostic form, not
    a slash command."""
    tc, mgr, b = _setup()
    try:
        await mgr.set_logging(True)
        await mgr.set_logging(False)
        outs = await _collect_outbound(b, 2)
        assert outs == [b"D EXRAIL ON", b"D EXRAIL OFF"]
    finally:
        await tc.aclose()
