# jmri-loconet — LocoNet driver

A `SystemConnection` subclass that drives the LocoNet wire protocol
(Digitrax PR3/PR4, LocoBuffer-II, and any TCP-based bridge like LbServer).
Discovered by `jmri-core`'s plugin machinery via the `jmri.connection =
loconet` entry point — no glue code required in `jmri-server` or any other
consumer.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **86 / 86** passing |
| Branch coverage | ≥85% |

## What this proves

1. **The HAL abstraction holds.** No changes were needed in `jmri-core` or
   `jmri-server` to make LocoNet appear as a usable system. The
   `SystemConnection` ABC + entry-point plugin model from Stages 1–3 were
   correctly shaped.
2. **Entry-point discovery is the only wiring.** After `pip install -e .`,
   `jmri_core.plugins.load_connections()` reports
   `{'internal': InternalConnection, 'loconet': LocoNetConnection}`.
3. **Async-first design pays off.** The traffic controller is a single
   `asyncio` task — no thread management, no listener queues, no
   `PropertyChangeListener` machinery. Send/receive semantics that took
   thousands of lines in JMRI's Java implementation are ~150 lines here.

## Layout

```
jmri-loconet/
├── pyproject.toml
├── README.md
├── src/jmri_loconet/
│   ├── __init__.py
│   ├── opcodes.py            LnConstants port (opcode + slot status)
│   ├── codec.py              frame framing, checksum, async read_frame
│   ├── messages.py           typed dataclasses + encode/decode dispatch
│   ├── dcc.py                NMRA DCC function-group packets for F9..F28
│   ├── traffic.py            LocoNetTrafficController (async + Signal fan-out)
│   ├── slots.py              SlotManager (address↔slot, acquire/release)
│   ├── beans.py              LnTurnout/Sensor managers + LnPowerManager
│   ├── throttle.py           LnThrottle + LnThrottleManager (incl. slot refresh)
│   ├── programmer.py         service-mode + ops-mode CV programming
│   ├── connection.py         LocoNetConnection — the SystemConnection subclass
│   └── transport/
│       ├── port.py           LocoNetPort ABC
│       ├── loopback.py       in-memory pair for tests
│       ├── serial_port.py    pyserial-asyncio LocoBufferII / PR3 / PR4
│       └── tcp_port.py       LbServer / LNWI / any TCP bridge
└── tests/
    ├── unit/
    │   ├── test_codec.py         frame read/write + checksum
    │   ├── test_messages.py      encode/decode round-trip for every opcode
    │   ├── test_loopback.py      transport pair
    │   ├── test_traffic.py       TC over loopback
    │   ├── test_slots.py         acquire/release with simulated CS
    │   ├── test_beans.py         turnout/sensor/power end-to-end
    │   └── test_throttle.py      DIRF/SND packing + slot routing
    └── integration/
        └── test_connection.py    LocoNetConnection end-to-end + entry-point
```

## Opcode coverage (Stage 4)

All 2-byte: `OPC_GPON`, `OPC_GPOFF`, `OPC_IDLE`.

4-byte: `OPC_SW_REQ`, `OPC_SW_REP`, `OPC_INPUT_REP`, `OPC_LONG_ACK`,
`OPC_SLOT_STAT1`, `OPC_MOVE_SLOTS`, `OPC_RQ_SL_DATA`, `OPC_LOCO_ADR`,
`OPC_LOCO_SPD`, `OPC_LOCO_DIRF`, `OPC_LOCO_SND`.

Variable-length: `OPC_SL_RD_DATA` and `OPC_WR_SL_DATA` (the 14-byte slot
data exchanges).

Everything else decodes to `OpcUnknown(opcode, payload)` so listeners see
unrecognized traffic without losing it.

## Transports

| Transport | Hardware | Settings |
|---|---|---|
| `LoopbackPort` | None (tests/demos) | `{"kind": "loopback", "loopback_port": <LoopbackPort>}` |
| `SerialPort`   | LocoBuffer-II / PR3 / PR4 / any USB-serial dongle | `{"kind": "serial", "port": "COM5", "baud": 16457}` |
| `TcpPort`      | LbServer / Digitrax LNWI / JMRI-bridge | `{"kind": "tcp", "host": "...", "tcp_port": 1234}` |

Defaults match JMRI: 16,457 baud for LocoBufferII serial; 1234 for LbServer.

## Slot management

- `SlotManager.acquire(address)` → sends `OPC_LOCO_ADR`, waits for
  `OPC_SL_RD_DATA`, returns the populated `Slot`.
- `claim(slot)` — null-move to flip to `LOCO_IN_USE`.
- `release(slot)` — move to slot 0 (mark COMMON).
- Inbound `OPC_SL_RD_DATA` always updates the slot mirror.
- **Periodic refresh** — `LnThrottleManager` sends null-moves on every
  active slot ~every 90s so the CS doesn't purge them.

Consist support (`OPC_LINK_SLOTS` / `OPC_UNLINK_SLOTS`) is wired via
`LnConsistManager` -- see `jmri_loconet.consist`. Consists created via
`conn.consists` default to `kind="command_station"`: speed/direction
go to the lead slot only, the CS fans to linked followers, functions
stay per-loco. Reversed followers get an explicit inverted DIRF so
they move the right way physically.

## Test design

Every test uses the `loopback_pair()` transport: one side runs the
`LocoNetTrafficController` we're driving, the other side stands in as the
command station. The "fake command station" pattern in
`test_slots.py::_fake_command_station` shows how a few lines of listener
code simulate a real CS for the throttle / slot tests.

No real hardware is required. Hardware-specific manual smoke tests are
gated behind the `hardware` pytest marker for future use.

## Running against real hardware

```python
import asyncio
from jmri_loconet import LocoNetConnection

async def main():
    conn = LocoNetConnection()
    await conn.open({"kind": "serial", "port": "COM5", "baud": 16457})
    # ...drive turnouts/sensors/throttles via conn.turnouts, conn.sensors,
    #    conn.throttles.acquire(3601), conn.power.set_power(...)
    await conn.close()

asyncio.run(main())
```

Or via the jmri-server, just install both packages and point a JMRI JSON
client at it:

```powershell
pip install -e jmri-core jmri-server jmri-loconet
uvicorn jmri_server:create_app --factory
```

The server picks the connection via `JMRI_CONNECTION=loconet` plus a
`JMRI_CONNECTION_SETTINGS` JSON blob in the environment.

## What's intentionally deferred

| To | What |
|---|---|
