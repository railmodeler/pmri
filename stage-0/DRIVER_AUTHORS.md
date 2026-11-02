# Driver author's guide — Stage 10 prep

How to write a new hardware driver for the JMRI Python rewrite. Distilled
from the five drivers shipped so far (`internal`, `loconet`, `nce`,
`ncemonitor`, `dccex`, `openlcb`) plus three real-hardware debugging
sessions on NCE PowerPro 6.2.3.

## The contract

A driver is a Python package that:

1. Subclasses `jmri_core.hal.connection.SystemConnection`.
2. Sets two class vars: `name` (vendor label like `"NCE"`) and `prefix`
   (single-letter JMRI system-name prefix like `"N"`).
3. Registers an entry point in `pyproject.toml`:

   ```toml
   [project.entry-points."jmri.connection"]
   yourdriver = "jmri_yourdriver:YourdriverConnection"
   ```

That's the wire. `jmri_core.plugins.load_connections()` discovers the
driver and `jmri_server.app.build_app(connection_name="yourdriver")`
opens it. No glue code in `jmri-core` or `jmri-server` is required.

## Lifecycle

```python
class YourdriverConnection(SystemConnection):
    name: ClassVar[str] = "Yourdriver"
    prefix: ClassVar[str] = "Y"

    async def open(self, settings: ConnectionSettings | None = None) -> None:
        # 1. Build the transport (serial/TCP/loopback).
        # 2. Construct the traffic controller.
        # 3. Replace base managers with hardware-aware subclasses:
        #      self.turnouts = YourdriverTurnoutManager(...)
        # 4. Start any background polling/listening tasks.
        # 5. Mark opened — last step.
        await super().open(settings)

    async def close(self) -> None:
        # Cancel background tasks, close transport, then call super.
        await super().close()
```

## Bean manager overrides

`SystemConnection.__init__` creates in-memory base managers for every
bean type. Driver overrides happen in one of two places:

- **`_make_*_manager()` hooks** — cleanest for drivers that always want a
  custom manager (used by `openlcb`).
- **Reassignment in `open()`** — necessary when the manager needs the
  traffic controller, which can't exist before `open()` (used by `nce`,
  `loconet`, `ncemonitor`). Pattern: `self.turnouts = MyTurnoutManager(...)`.

Both patterns are supported. Pick the one that matches your driver's
constraints. The base ABC accepts either.

## Transports

The convention across existing drivers:

- A `transport/port.py` module defines an ABC with `write`, `read_exact`,
  `read_pending`, `aclose`.
- Concrete implementations for `serial`, `tcp`, and `loopback`.
- `LoopbackPort` is paired (two halves connected by `asyncio.Queue`) so
  the test suite can stand up a fake CS without real hardware.

Stick to this pattern unless your protocol genuinely doesn't fit.

## Lessons from real hardware

Three NCE hardware debugging sessions surfaced patterns every new driver
should be aware of.

### 1. Never ship an opcode that wasn't verified on real hardware

Bug #1 in the NCE driver: a speculative `OP_QUERY_COMMAND_VER = 0x87` was
defined and wired into an opt-in auto-detect path. On the real CS, 0x87 is
interpreted as a multi-byte command that waits for more arguments —
**every subsequent byte is consumed as a fake argument until the CS is
power-cycled**. The bus was wedged solid.

Rule: opcodes go into `opcodes.py` only after you can demonstrate they do
the documented thing on real hardware. If you can't test it, leave it out
or gate it behind an explicit opt-in with a loud warning.

### 2. Reply shapes are command-specific, not protocol-wide

The same NCE driver assumed every command returned `N data bytes + 1 ACK
byte`. On real hardware:

- Some commands return just `!` (1 byte ACK)
- Some commands return `N` data bytes with no trailing ACK
- Some commands echo the request opcode as the "ACK" byte

The fix was an `is_read: bool` flag on the command record and a relaxed
ACK validation (accept anything except explicit `b'0'` NACK).

Rule: don't assume reply framing is uniform. Read JMRI Java's source for
each opcode you implement and copy the reply-length contract verbatim.

### 3. Opcodes collide across modes

NCE's `0x90..0x97` are CV-programming opcodes **and** the operands of the
legacy "send raw DCC packet" command (`0x90 + packet_length`). Modern
firmware uses `0xA2 nceLocoCmd` for throttle traffic; sending the wrong
opcode puts the CS into a different mode entirely.

Rule: when two paths share opcodes, document which firmware/mode each
path is meant for. The Python labels you pick (`OP_*`) should encode the
*meaning*, not the byte. If the byte value is reused in another context,
add a `NOTE:` comment with cross-references.

### 4. Buffer alignment matters when reading

Stale bytes from the CS (startup chatter, late echo bytes, response
trailers) accumulate in the serial buffer and get consumed as the *next*
command's reply. Either:

- Drain pending bytes at the start of every `send_command` (NCE's
  approach — `BytePort.read_pending()`), or
- Use a JMRI-style continuous reader thread that buffers and dispatches
  by context.

The first is simpler and works for sync request/response protocols. The
second is more robust but requires careful task design.

### 5. Mirror commanded → known for UI feedback

When a user clicks "throw", they want the UI to reflect the new state
immediately, not after the next poll cycle. JMRI does this; mirror in
`set_commanded_state` after sending the wire command. If the layout
disagrees (motor stall, no decoder), the monitor poll will correct the
known state back.

## Testing pattern

Every existing driver follows the same test recipe:

1. `loopback_pair()` creates two connected `LoopbackPort` halves.
2. One half wires the real traffic controller.
3. The other half runs a "fake CS" coroutine that reads request bytes,
   computes replies from canned state, and writes them back.
4. Tests mutate the canned state between operations to simulate layout
   events (cab throws a turnout, AIU goes high, etc.).
5. Assertions confirm beans get the right state changes.

Reference: `jmri-nce/tests/unit/test_monitors.py::FakeCS` is the most
useful template — a small CS simulator with `address → byte` memory and
`aiu → word` dicts that answers reads from that state.

Hardware-specific tests go behind the `hardware` pytest marker so CI
can skip them. Smoke tests against a real CS belong in a markdown
runbook, not in pytest.

## Quality gates per driver

Match the existing drivers:

| Gate | Required |
|---|---|
| `ruff check src` | clean |
| `mypy --strict src` | clean |
| `pytest tests/` | ≥85% branch coverage on protocol/bean code |
| `loopback` test fixture | works without real hardware |
| Entry-point discovery | `jmri_core.plugins.load_connections()` finds the driver |
| `SystemConnection.open(loopback_settings)` end-to-end | turnouts/sensors/power testable in unit tests |

## Known abstraction gaps (will not block Stage 10)

These are recognized limitations of the current `SystemConnection` ABC.
They haven't blocked any driver yet, but new authors should know about
them:

- **No capability discovery.** A passive driver (`ncemonitor`) accepts
  throttle commands silently — they just don't reach hardware. Today
  drivers document their read/write semantics; future ABC may add a
  `capabilities` set.
- **No connection failure events.** If the transport dies mid-session,
  the driver can't signal "broken" to the server. The server only sees a
  hung command. Workaround: drivers should raise from `send_command` on
  transport errors so the caller sees the failure.
- **No driver metadata channel.** Firmware version, hardware variant,
  etc. live in driver-specific properties (`NceConnection.command_set`).
  Web UIs can't introspect this uniformly.
- **One connection instance per `name`.** Running two LocoNet
  connections (e.g., two layouts) requires distinct entry-point names.

If your driver needs any of the above to work at all, raise it before
spending time on the implementation — the ABC might need a careful
addition.

## Reference drivers, by shape

When in doubt, look at the closest-shaped existing driver:

| Your protocol shape | Look at |
|---|---|
| Async broadcast bus (LocoNet, MERG/CBUS) | `jmri-loconet` |
| Sync request/response + polling (NCE, MRC, Lenz) | `jmri-nce` |
| Text protocol (DCC-EX, SRCP, ECoS, Marklin) | `jmri-dccex` |
| Event-driven (OpenLCB/LCC, MQTT) | `jmri-openlcb` |
| Passive observer (any sniffer/monitor) | `jmri-ncemonitor` |
