# pmri-nce — NCE hardware driver

NCE driver for the Python JMRI rewrite. Where `pmri-loconet` exercises the
HAL with an **async broadcast** protocol, this package exercises it with a
**synchronous request/response + polling** protocol — the structural
opposite. Both drivers plug into the same `SystemConnection` ABC without
any changes to `pmri-core` or `pmri-server`.

The opcode set and message layouts below are verified against JMRI Java's
`NceBinaryCommand` and exercised on real PowerPro 6.2.3 hardware over RS-232
with a USB-serial adapter.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **66 / 66** passing |
| Hardware-verified | turnouts (throw/close + feedback), throttle (speed/dir/F0..F28), monitor (accessory-state poll) |

The serial transport (`SerialPort`) is hardware-only and excluded from
coverage; the test suite drives a loopback transport with a fake CS.

## Why this driver matters

This package is the **HAL validation test** — building two drivers in
Stage 4 verifies the abstraction wasn't accidentally LocoNet-shaped.

| Dimension | LocoNet | NCE |
|---|---|---|
| Topology | Multi-master bus broadcast | Single-master, PC ↔ CS |
| Sync model | Async fire-and-forget | Synchronous req/resp |
| Sensor input | Pushed via `OPC_INPUT_REP` | **Polled** from AIU modules |
| Turnout state | Pushed via `OPC_SW_REP` | **Polled** from CS memory |
| Firmware variation | Minimal | **3 distinct command sets** |
| Checksum | Per-frame XOR | None (USB CRC + ACK byte) |

If the `SystemConnection` ABC needed to change to accommodate NCE, the
abstraction was wrong. It didn't.

## Three command-set variants

The `NceCommandSet` strategy captures per-firmware differences as data:

| Variant | Hardware | Memory base | AIUs |
|---|---|---|---|
| `NCE_PH_PRO_2006` | PowerPro, post-2006 firmware | 0xEC00 | Yes |
| `NCE_POWERCAB` | PowerCab | 0xEC00 (smaller table) | No |
| `NCE_SB5` | Smart Booster SB5 | 0xEC00 | Yes |

Selected via the `command_set` connection setting; defaults to
`PH-Pro 2006+` and falls back to it for unrecognized names.

The `auto_detect` setting (default **off**) probes the firmware via
`OP_QUERY_COMMAND_VER` (0x87). **Don't enable this on production hardware
without verifying the opcode for your firmware first** — 0x87 was
speculative and wedges some variants. The verified-safe version-read is
JMRI's 0xAA (`getNceEpromRev`); see the warning in `opcodes.py`.

## Polling architecture

NCE doesn't push state changes — clients must poll. Two background tasks
own this:

* **`NceTurnoutMonitor`** — reads accessory-state memory in 16-byte chunks
  every 1.5s via `OP_READ_ACCY_MEM` (0x8F), diffs against the previous
  snapshot, emits state changes through registered turnouts. One bit per
  turnout, 8 turnouts per byte, 256 bytes covers 2048 turnouts.
* **`NceAiuPoller`** — reads each configured AIU module's 16-bit input
  word at 250ms cadence via `OP_AIU_BASE_QUERY` (0x9B).

Both tasks live for the connection's lifetime — started in `open()` and
cancelled cleanly in `close()`. The monitor can be disabled with
`disable_turnout_monitor: true` in connection settings for firmwares
where the 0x8F read isn't supported.

## Wire shape

NCE has **two reply shapes**, command-dependent:

* **Action commands** (power, accessory, throttle, CV write) — reply is
  one ACK byte. May be `!` (ACK_OK) or an echo of the request opcode;
  both are treated as success. Only `0` (ACK_ERROR) raises.
* **Read commands** (memory, AIU, accessory state, CV read) — reply is
  exactly `reply_len` data bytes, **no** trailing ACK.

`NceTrafficController` serializes every request through one `asyncio.Lock`.
Before each command, the controller drains any stray bytes left in the
serial buffer — important because some opcodes echo back the request
bytes alongside the ACK.

## Opcode coverage

Exported from `opcodes.py` and wired through `messages.py` factories:

| Opcode | Function | Wire form |
|---|---|---|
| `0x88` `OP_POWER_OFF` | track off | 1 byte |
| `0x89` `OP_POWER_ON` | track on | 1 byte |
| `0x85` `OP_WRITE_N_MEM` | write N bytes CS memory | `[op, hi, lo, len, ...data]` |
| `0xA0` `OP_READ_N_MEM` | read N bytes CS memory | `[op, hi, lo, len]` → N bytes |
| `0x8F` `OP_READ_ACCY_MEM` | read 16 bytes accessory state | `[op, hi, lo]` → 16 bytes |
| `0x9B` `OP_AIU_BASE_QUERY` | read AIU input word | `[op, addr]` → 2 bytes |
| `0xAD` `OP_ACCY_DECODER_OPS` | accessory throw/close | `[op, hi, lo, dir, 0]` |
| `0xA2` `OP_NCE_LOCO_CMD` | throttle speed/dir/functions | `[op, hi, lo, sub, data]` |
| `0x90` `OP_RD_CV_OPS_MODE` | read CV ops mode (RailCom) | 5 bytes |
| `0x91` `OP_WR_CV_OPS_MODE` | write CV ops mode | 6 bytes |
| `0x94` `OP_WR_CV_DIRECT_MODE` | write CV service track | 4 bytes |
| `0x97` `OP_RD_CV_DIRECT_MODE` | read CV service track | 3 bytes |

`OP_QUERY_COMMAND_VER` (0x87) is defined but documented as unsafe.

### Opcode collisions worth knowing

`0x90..0x97` are CV-programming opcodes **and** the operands of the
legacy "send raw DCC packet" command (`0x90 + packet_length`). Modern
firmware uses `OP_NCE_LOCO_CMD` (0xA2) for throttle traffic, not the
legacy `0x9X` send-packet path — sending the latter is interpreted as a
CV mode command, putting the CS into a state that corrupts subsequent
reads. Same lesson for `0x8F` / `0x9D`: those are accessory-state memory
ops, **not** "macro on" / "write 1 high mem" as some older references
say.

## Settings example

```python
import asyncio
from pmri_nce import NceConnection

async def main():
    conn = NceConnection()
    await conn.open({
        "kind": "serial",
        "port": "/dev/ttyUSB0",
        "baud": 9600,
        "command_set": "PH-Pro 2006+",
        "aiu_addresses": [1, 2, 3],
    })
    # Throw turnout 12
    from pmri_core.state import TurnoutState
    t = await conn.turnouts.provide("NT12")
    await t.set_commanded_state(TurnoutState.THROWN)
    # Throttle a long-address loco
    throttle = await conn.throttles.acquire(3601, long_address=True)
    await throttle.set_speed(0.25)
    await asyncio.sleep(5)
    await throttle.emergency_stop()
    await conn.close()

asyncio.run(main())
```

## Test design

Every test uses `loopback_pair()` + a small "fake command station" coroutine
that reads requests and writes back synthetic replies. The `FakeCS` class
in `test_monitors.py` is the most useful pattern: it hosts an
`address → byte` memory dict and an `aiu → word` dict, and answers reads
from that state. Mutating the dict between polls simulates a layout cab
flipping a turnout — the monitor must detect the diff and update the bean.

## Layout

```
pmri-nce/
├── pyproject.toml
├── README.md
├── src/pmri_nce/
│   ├── __init__.py
│   ├── opcodes.py            verified opcodes + sub-commands + ACK bytes
│   ├── messages.py           NceCommand factories
│   ├── command_set.py        per-firmware strategy
│   ├── traffic.py            sync request/response (single in-flight)
│   ├── monitors.py           NceTurnoutMonitor + NceAiuPoller
│   ├── beans.py              NceTurnout/Sensor + NcePowerManager
│   ├── throttle.py           NceThrottle (0xA2 modern-firmware path)
│   ├── programmer.py         service- and ops-mode CV programming
│   ├── detect.py             opt-in firmware probe (currently unsafe)
│   ├── connection.py         NceConnection — SystemConnection subclass
│   └── transport/
│       ├── port.py           BytePort ABC (with read_pending for drain)
│       ├── loopback.py       in-memory pair for tests
│       └── serial_port.py    pyserial-asyncio
└── tests/
    ├── unit/                 message encoding, traffic, monitors, throttle, programmer
    └── integration/          open/close + entry point + connection cleanup
```
