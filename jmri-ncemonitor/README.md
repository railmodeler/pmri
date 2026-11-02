# jmri-ncemonitor — passive NCE Cab Bus monitor driver

The HAL stress test. Where `jmri-loconet` is **async broadcast** and
`jmri-nce` is **synchronous request/response**, this package is
**purely passive** — read-only observation of the NCE Cab Bus traffic
between the command station and physical cab throttles. No outbound
writes; commands raise.

That this drops into the same `SystemConnection` ABC as the other two
drivers — with no changes required in `jmri-core` or `jmri-server` — is
the validation that the abstraction supports the full read/write/observe
spectrum, not just bidirectional drivers.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | no issues across 10 source files |
| `pytest tests/` | **52 / 52** passing |
| Branch coverage | **89.3%** (target 85%) |

The serial transport is hardware-only and excluded from coverage —
manual smoke tests gated behind the `hardware` pytest marker.

## What gets observed

| Message | Source | Decoded fields |
|---|---|---|
| `CabPoll` | master → cab | cab ID, poll command |
| `CabResponse` | cab → master | key, speed, direction, F0..F8 |
| `FastClockUpdate` | master | hour, minute |
| `AccessoryObserved` | master | accessory address, closed/thrown |
| `AiuUpdate` | master | AIU module #, 16-bit input word |
| `ConsistObserved` | master | consist address, member locos |
| `MacroObserved` | master | macro number + accessory steps |
| `MasterAck` | master | opcode-tagged acknowledgement |
| `ProgrammingResponse` | master | CV value + address + ack flag |
| `ThrottleObserved` | master | ad-hoc throttle traffic on the bus |
| `CabBusUnknown` | anything else | raw bytes preserved |

When `AccessoryObserved` arrives, `PassiveTurnoutManager` updates the
matching turnout's *known state*. When `AiuUpdate` arrives,
`PassiveSensorManager` decodes the 14 input bits and updates the matching
sensors. There is no commanded state — `set_commanded_state` raises with a
clear message.

## Layout

```
jmri-ncemonitor/
├── pyproject.toml
├── README.md
├── src/jmri_ncemonitor/
│   ├── __init__.py
│   ├── codec.py              9-bit-marker framing + PeekableReader
│   ├── messages.py           typed observation messages + decode/encode
│   ├── monitor.py            CabBusMonitor — passive read loop + Signal
│   ├── beans.py              PassiveSensor/Turnout + raising PowerManager (writable=False)
│   ├── parity9bit.py         Parity9BitDecoder — mark/space-parity -> high-bit shim
│   ├── replay.py             BusCapture + BusReplay (.cabbus log format)
│   ├── connection.py         NceMonitorConnection (SystemConnection)
│   └── transport/
│       ├── port.py           BytePort ABC (read-only by convention)
│       ├── loopback.py       in-memory pair for tests
│       └── serial_port.py    pyserial-asyncio (write() raises)
└── tests/
    ├── unit/
    │   ├── test_codec.py        framing, pushback, EOF
    │   ├── test_messages.py     encode/decode round-trip per type
    │   ├── test_new_messages.py consist/macro/programming/throttle/ack
    │   ├── test_monitor.py      end-to-end read-loop over loopback
    │   ├── test_parity9bit.py   parity-tagged source -> high-bit stream
    │   ├── test_replay.py       capture + replay round-trips
    │   └── test_beans.py        passive bean semantics + writable=False
    └── integration/
        └── test_connection.py   entry-point + observation
```

## Cab Bus framing convention

The real NCE Cab Bus is 9600-baud RS-485 with a 9-bit byte where the 9th
bit marks address bytes. Many USB-serial adapters can't deliver the 9th
bit directly; both JMRI's reference adapter and this package use the
"high bit set = address byte" convention. Bytes with the high bit set
start a new message; following bytes are 7-bit data until the next
address byte appears.

Production hardware that delivers true 9-bit serial via mark/space parity
needs a thin adapter layer mapping parity-marked bytes to high-bit-marked
bytes; that adapter is *not* in scope here.

## Why a fully-passive driver matters

Some real use cases:

- **Diagnostic monitor** — sniff Cab Bus traffic while JMRI's command-side
  driver (`jmri-nce`) talks to the command station. Two connections on
  two different transports, observing each other.
- **Read-only sensor bridge** — pipe AIU input changes into a server
  without giving anyone write access to the layout.
- **Bus capture for replay** — record traffic with a passive driver,
  later play it back through `jmri-server` for testing without hardware.

In all three cases, `set_commanded_state` raising is the *correct*
behavior — the driver should fail loudly rather than silently dropping
writes that have no path to the layout.

## Running against real hardware

```python
import asyncio
from jmri_ncemonitor import NceMonitorConnection

async def main():
    conn = NceMonitorConnection()
    await conn.open({"kind": "serial", "port": "COM7", "baud": 9600})
    # Subscribe to every message decoded off the bus:
    conn.monitor.inbound.connect(lambda m: print(m))
    await asyncio.sleep(60)
    await conn.close()

asyncio.run(main())
```

Or through `jmri-server` — `pip install` of this package is the only
wiring, then select it via `JMRI_CONNECTION=ncemonitor`.

## Observed-only UI affordance

Passive beans carry a `writable = False` class attribute (defined
default-True on `AbstractNamedBean` / `PowerManager`), surfaced by the
server as a `"writable": false` field *only when it's false* — so the
canonical JMRI JSON schemas (`additionalProperties: false`) stay happy
on the writable common case, and the handful of ncemonitor rows show
up differently in the web UI.

`jmri-web`'s `_turnout_row.html` / `_sensor_row.html` / `_power_panel.html`
check the flag and:

* add an "observed" badge next to the state badge,
* stamp `data-observed="true"` on the row for CSS / client scripts,
* hide the state-change buttons (Close/Throw, Active/Inactive, ON/OFF),
* keep Edit + Delete reachable (those don't touch the layout).

Clients that don't know the field default to writable=True when it's
absent, so the change is backward-compatible with every pre-existing
WebSocket subscriber and REST client.

## HAL validation

No changes to `jmri-core` or `jmri-server` were required to land *any* of
the drivers (loconet, nce, ncemonitor, dccex, openlcb). **The HAL holds.**
