"""``time`` JSON service -- Fast Clock wire interface."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pmri_core.beans.fast_clock import HardwareClockState
from pmri_server.app import AppHandle

from tests.conftest import post


def test_get_returns_default_state(client: TestClient) -> None:
    r = post(client, "time", method="get")
    assert r["data"]["time"] == "00:00"
    assert r["data"]["rate"] == 1.0
    assert r["data"]["running"] is False


def test_post_sets_time(client: TestClient) -> None:
    r = post(client, "time", method="post", data={"time": "08:30"})
    assert r["data"]["time"] == "08:30"


def test_post_sets_rate(client: TestClient) -> None:
    r = post(client, "time", method="post", data={"rate": 4.0})
    assert r["data"]["rate"] == 4.0


def test_post_starts_and_stops(client: TestClient) -> None:
    r = post(client, "time", method="post", data={"running": True})
    assert r["data"]["running"] is True
    r = post(client, "time", method="post", data={"running": False})
    assert r["data"]["running"] is False


def test_post_combined_set(client: TestClient) -> None:
    r = post(client, "time", method="post", data={
        "time": "12:00", "rate": 2.0, "running": True,
    })
    assert r["data"]["time"] == "12:00"
    assert r["data"]["rate"] == 2.0
    assert r["data"]["running"] is True


def test_state_persists_across_requests(client: TestClient) -> None:
    """The clock is a singleton on ctx, so subsequent gets see prior posts."""
    post(client, "time", method="post", data={"time": "09:15", "rate": 8.0})
    r = post(client, "time", method="get")
    assert r["data"]["time"] == "09:15"
    assert r["data"]["rate"] == 8.0


def test_invalid_time_format_returns_400(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "time", "method": "post", "data": {"time": "not-a-time"},
    })
    assert r.status_code == 400


def test_out_of_range_rate_returns_400(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "time", "method": "post", "data": {"rate": 500.0},
    })
    assert r.status_code == 400


def test_out_of_range_hours_returns_400(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "time", "method": "post", "data": {"time": "25:00"},
    })
    assert r.status_code == 400


# ----- hardware clock (CS-driven fast clock) -------------------------------


class _FakeHardwareConn:
    """Stand-in ``connection`` that satisfies TimeService's duck-typing.

    The service checks for ``is_open`` + ``read_hardware_clock`` and treats
    that as authoritative. This fake records every call the service makes
    so the tests can assert both wire-through and cache behaviour.
    """

    def __init__(self, state: HardwareClockState) -> None:
        self._state = state
        self.is_open = True
        self.reads = 0
        self.time_writes: list[tuple[int, int, int]] = []
        self.rate_writes: list[float] = []
        self.starts = 0
        self.stops = 0

    async def read_hardware_clock(self) -> HardwareClockState:
        self.reads += 1
        return self._state

    async def set_hardware_clock_time(self, h: int, m: int, s: int = 0) -> None:
        self.time_writes.append((h, m, s))
        # Reflect the write in the next read so GETs converge on the change.
        self._state = HardwareClockState(
            hours=h, minutes=m, seconds=s,
            rate=self._state.rate,
            running=self._state.running,
            twenty_four_hour=self._state.twenty_four_hour,
        )

    async def set_hardware_clock_rate(self, rate: float) -> None:
        self.rate_writes.append(rate)
        self._state = HardwareClockState(
            hours=self._state.hours, minutes=self._state.minutes,
            seconds=self._state.seconds,
            rate=rate,
            running=self._state.running,
            twenty_four_hour=self._state.twenty_four_hour,
        )

    async def start_hardware_clock(self) -> None:
        self.starts += 1
        self._state = HardwareClockState(
            hours=self._state.hours, minutes=self._state.minutes,
            seconds=self._state.seconds, rate=self._state.rate,
            running=True, twenty_four_hour=self._state.twenty_four_hour,
        )

    async def stop_hardware_clock(self) -> None:
        self.stops += 1
        self._state = HardwareClockState(
            hours=self._state.hours, minutes=self._state.minutes,
            seconds=self._state.seconds, rate=self._state.rate,
            running=False, twenty_four_hour=self._state.twenty_four_hour,
        )


@pytest.fixture()
def hw_client(handle: AppHandle) -> tuple[TestClient, _FakeHardwareConn]:
    """Swap in a fake CS-driven connection before wiring the client."""
    fake = _FakeHardwareConn(HardwareClockState(
        hours=7, minutes=30, seconds=0,
        rate=8.0, running=True, twenty_four_hour=False,
    ))
    handle.ctx.connection = fake  # type: ignore[assignment]
    with TestClient(handle.app) as tc:
        yield tc, fake


def test_get_prefers_hardware_clock(hw_client: tuple[TestClient, _FakeHardwareConn]) -> None:
    """The user's real-world scenario: CS at 07:30 AM, rate 8.0 -- GET must
    surface that, not the software bean's default 00:00 / 1.0."""
    client, fake = hw_client
    r = post(client, "time", method="get")
    assert r["data"]["time"] == "07:30"
    assert r["data"]["rate"] == 8.0
    assert r["data"]["running"] is True
    assert fake.reads >= 1


def test_post_time_writes_to_hardware(
    hw_client: tuple[TestClient, _FakeHardwareConn],
) -> None:
    client, fake = hw_client
    r = post(client, "time", method="post", data={"time": "10:15"})
    assert fake.time_writes == [(10, 15, 0)]
    # Response reflects the CS state after write.
    assert r["data"]["time"] == "10:15"


def test_post_rate_writes_to_hardware(
    hw_client: tuple[TestClient, _FakeHardwareConn],
) -> None:
    client, fake = hw_client
    post(client, "time", method="post", data={"rate": 4.0})
    assert fake.rate_writes == [4.0]


def test_post_running_writes_to_hardware(
    hw_client: tuple[TestClient, _FakeHardwareConn],
) -> None:
    client, fake = hw_client
    post(client, "time", method="post", data={"running": False})
    post(client, "time", method="post", data={"running": True})
    assert fake.stops == 1
    assert fake.starts == 1


def test_hardware_reads_are_cached_across_polls(
    hw_client: tuple[TestClient, _FakeHardwareConn],
) -> None:
    """The 1 Hz widget poll must not hit the CS every request; a fresh
    cache lets the software bean cover the between-CS-reads window and
    keeps the turnout monitor's serial airtime intact."""
    client, fake = hw_client
    # First GET -> one CS read.
    post(client, "time", method="get")
    baseline = fake.reads
    # A burst of polls within the TTL must not add reads.
    for _ in range(5):
        post(client, "time", method="get")
    assert fake.reads == baseline


def test_post_invalidates_cache(
    hw_client: tuple[TestClient, _FakeHardwareConn],
) -> None:
    """A write must force a fresh CS read so the caller sees the effect."""
    client, fake = hw_client
    post(client, "time", method="get")            # seed cache
    reads_before = fake.reads
    post(client, "time", method="post", data={"running": False})
    # POST does a read after the write, so we should see at least one more.
    assert fake.reads > reads_before


def test_hardware_write_timeout_returns_504_not_500(
    hw_client: tuple[TestClient, _FakeHardwareConn],
) -> None:
    """A write that times out at the CS must surface as a graceful 504,
    not a bare 500 with a stack trace. The UI can then say "CS didn't
    respond" and let the user retry."""
    client, fake = hw_client

    async def boom(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise TimeoutError("CS didn't answer")
    fake.set_hardware_clock_time = boom  # type: ignore[assignment]

    r = client.post("/json/", json={
        "type": "time", "method": "post", "data": {"time": "10:15"},
    })
    assert r.status_code == 504


def test_hardware_read_failure_falls_back_to_bean(
    hw_client: tuple[TestClient, _FakeHardwareConn],
) -> None:
    """A timed-out CS read must not 500 the request -- surface the bean instead."""
    client, fake = hw_client

    async def boom() -> HardwareClockState:
        raise TimeoutError("CS didn't answer")
    fake.read_hardware_clock = boom  # type: ignore[assignment]

    r = post(client, "time", method="get")
    # Bean defaults survive: midnight, 1x, stopped.
    assert r["data"]["time"] == "00:00"
    assert r["data"]["rate"] == 1.0
    assert r["data"]["running"] is False
