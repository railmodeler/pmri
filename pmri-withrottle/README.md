# pmri-withrottle — WiThrottle TCP server (Engine Driver / WiThrottle iOS)

A TCP server speaking JMRI's line-based ASCII **WiThrottle** protocol, so
handheld throttles — Engine Driver on Android, WiThrottle on iOS — can
connect to the Python JMRI rewrite as-is. No app changes on the phone
side.

The server holds its own `asyncio` listener (independent of FastAPI) but
is designed to be mounted inside the same process as `pmri-server` so it
shares the active connection's `ThrottleManager`, the roster store, and
the fast-clock — a loco acquired over WiThrottle is the *same* bean the
JSON API and the web UI drive.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **91 / 91** passing |
| Coverage | exceeds 85% target |
| Hardware-verified | Engine Driver (Android) + WiThrottle (iOS) against NCE and DCC-EX layouts on a Raspberry Pi |

## What this proves

1. **The HAL abstraction holds for driver-agnostic throttle clients.**
   Engine Driver sees the same throttle beans whether the layout is
   driven by LocoNet, NCE, DCC-EX, or the internal loopback. No
   protocol-specific glue in this package.
2. **Multi-client throttle coherence.** Every WiThrottle client + the
   web throttle + the JSON API acquire and release through one
   `ThrottleManager`. State changes from *any* source (physical cab,
   another WiThrottle, a script, the web) are visible to every other
   client.
3. **Two-way turnout sync.** Turnouts thrown from a WiThrottle client
   propagate to layout hardware; turnouts thrown from a physical cab or
   the web page push a `PTA<state><sysName>` line back to every
   connected WiThrottle. `state_changed` Signal wiring lives inside the
   server's start method so it attaches to the driver-swapped manager.
4. **Fast-clock sync without a separate transport.** The current PFT is
   piggybacked on every heartbeat ack, plus a periodic broadcast every
   `fast_clock_broadcast_s` (default 5 s), so client clocks stay
   aligned within one fast-second at 8× rate.

## Wire protocol summary

WiThrottle is a line-oriented ASCII protocol on TCP port **12090**.

```
                          client → server                server → client
Greeting / heartbeat      *                              *              (echo)
                                                         VN2.0
                                                         HT<railroad>
                                                         *<n>          request n-second heartbeat
                                                         PPA<0|1>      initial track power

Hello                     N<name>                        ─
                          HU<hardware-id>                ─

Quit                      Q                              ─

Throttle acquire          M<t>+<addr><;><label>          M<t>+<addr><;>
                                                         M<t>L<addr><;>]\[F0]\[F1]…]\[F28    labels
                                                         M<t>A<addr><;>F<state><fn>          × 29
                                                         M<t>A<addr><;>V<0..126>
                                                         M<t>A<addr><;>R<0|1>
                                                         M<t>A<addr><;>s<1|2|4|8|16>         speed-step

Throttle release          M<t>-<addr><;>                 M<t>-<addr><;>

Throttle action           M<t>A<addr><;>V<0..126>        (echo)   speed
                          M<t>A<addr><;>R<0|1>           (echo)   direction
                          M<t>A<addr><;>F<state><fn>     (echo)   function
                          M<t>A<addr><;>X                V1       e-stop
                          M<t>A<addr><;>I                V0       idle

Broadcast address         M<t>A*<;>V<n>                  per-throttle echo
                                                                  Engine Driver's slider drag path

Turnouts                  PTA<T|C|2><sysName>            PTA<state><sysName>
                                                         PTT…]\[Closed}|{2]\[Thrown}|{4…    once
                                                         PTL]\[<sys>}|{<user>}|{<state>…    once

Routes                    PRA<2><sysName>                PRA<state><sysName>              fire + broadcast
                                                         PRT…]\[Active}|{2]\[Inactive}|{4…  once
                                                         PRL]\[<sys>}|{<user>}|{<state>…    once

Fast clock                ─                              PFT<local-unix>;<rate>
Roster                    ─                              RL<n>]\[<id>}|{<addr>}|{<L|S>…
```

`address_token` is the loco address in the WiThrottle wire form:
`L3601` (long 3601), `S3` (short 3), or bare digits (NMRA convention:
1..127 = short, ≥128 = long). Address token `*` on an action broadcasts
to every throttle acquired on that mux character — critical for Engine
Driver's slider-drag path.

Full JMRI protocol reference:
<https://www.jmri.org/help/en/package/jmri/jmrit/withrottle/Protocol.shtml>

## Usage

### Standalone (no pmri-server)

```python
import asyncio
from pmri_core.hal.internal import InternalConnection
from pmri_withrottle import WiThrottleServer

async def main():
    conn = InternalConnection()
    await conn.open({})
    server = WiThrottleServer(
        connection=conn,
        railroad="Basement Layout",
    )
    await server.start(port=12090)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await conn.close()

asyncio.run(main())
```

### Mounted inside pmri-server (typical)

`pmri-server` picks up the package automatically when it's installed:

```powershell
pip install -e pmri-core pmri-server pmri-withrottle
uvicorn pmri_server:create_app --factory --port 12080
```

`attach_withrottle_server(handle)` runs in the FastAPI lifespan and:

* Wires `roster_provider` to the active profile's roster store — new
  entries appear on Engine Driver's next connect without a restart.
* Wires `turnout_provider` + `turnout_action_handler` to the connection
  so the Turnouts tab renders and its buttons drive real hardware.
* Wires `fast_clock_provider` to any fast clock attached to the
  server context, folding today's date into the HH:MM:SS reading and
  biasing by the local timezone (matches JMRI Java's
  `(fastClockTime + timeZoneOffset) / 1000L`).
* Subscribes to every turnout's `state_changed` Signal so external
  changes broadcast to all connected clients via `PTA`.

Environment overrides:

* `JMRI_WITHROTTLE_HOST` — bind address (default `0.0.0.0`).
* `JMRI_WITHROTTLE_PORT` — bind port (default `12090`).

## Client compatibility notes

Two client quirks that took hardware time to find:

1. **Function bar hides on empty labels.** Engine Driver hides the
   `F0..F28` bar entirely if the `M<t>L<addr>` labels line is missing
   *or* contains empty labels. The server always sends the line and
   fills unspecified slots with `F0..F28` defaults so both roster and
   manual acquires render the bar.
2. **Slider drag uses the broadcast address token.** Engine Driver's
   slider drag sends `M0A*<;>V<n>` (throttle `0`, address `*`) rather
   than the acquired address. Without `*` fan-out the throttle bean
   never sees the speed change even though direction / function
   commands work. Handled in `WiThrottleSession._action`.

## Test design

Every test drives the server end-to-end through `asyncio` streams —
`test_server.py` opens a real TCP connection to a `WiThrottleServer`
listening on `127.0.0.1:0`, sends encoded WiThrottle lines, and asserts
on the framed replies. No mock throttle manager: the tests use
`InternalConnection`'s real one, so the throttle beans exercised are
exactly the shape drivers implement.

`test_protocol.py` covers the line encoder/decoder in isolation
(round-trip per message type, address token parsing edge cases).
`test_session.py` covers session dispatch against a fake `send_line`
recorder, verifying acquire/release ordering, broadcast token
fan-out, heartbeat piggybacking, and turnout action forwarding.

## Layout

```
pmri-withrottle/
├── pyproject.toml
├── README.md
└── src/pmri_withrottle/
    ├── __init__.py               re-exports WiThrottleServer
    ├── protocol.py               line codec + typed inbound messages + encoders
    ├── session.py                per-connection state + dispatch
    └── server.py                 asyncio TCP listener + broadcast helpers
tests/
├── unit/
│   ├── test_protocol.py          decode/encode + address-token parsing
│   └── test_session.py           dispatch + acquire/release + broadcast
└── integration/
    └── test_server.py            end-to-end over a real TCP socket
```

## What's intentionally deferred

| To | What |
|---|---|
| — | **Auth** — WiThrottle historically has no auth; clients on the same LAN are trusted. When `pmri-web` gains auth, this server will need a matching token or IP-allowlist mechanism. JMRI itself has no auth for WiThrottle either. |

### Not part of the wire protocol

* **Block occupancy / layout state push.** JMRI's own WiThrottle package
  has no `BlockController` — the protocol has throttle, consist, turnout,
  route, fast-clock, and track-power controllers but no block one, so
  there is no wire format Engine Driver / WiThrottle iOS would consume.
  Block state in JMRI reaches clients via the HTML panel view, which in
  this rewrite is `/panels/{name}/svg` (served by `pmri-web`, with
  `data-sysname` / `data-kind` hooks on every drawn icon for client-side
  live updates).

### Shipped during stage 11

| Feature | Wire shape | Notes |
|---|---|---|
| Route control | `PRT` / `PRL` / `PRA` | Mirrors turnout controller shape; action `2` fires a `Route` bean's `fire()`. |
| Consist building | `M<t>+<addr><;>` × N on the same multiplex char | JMRI builds MUs by repeat-acquire on the same throttle slot; there is **no** separate `+MU` opcode. Wildcard throttle actions (`M<t>A*<;>…`) fan to every acquired loco. Targeted release drops one member; wildcard release drops all. |
