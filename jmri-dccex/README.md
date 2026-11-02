# jmri-dccex — DCC-EX driver

The bracket-delimited ASCII protocol driver. DCC-EX (formerly DCC++) is
the most-used open-source DCC command station — designed for hobbyist
hardware, simple text protocol, modern Wi-Fi/Ethernet/USB transports.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **34 / 34** |
| Coverage | ≥85% |

## Wire protocol summary

```
PC → CS                CS → PC
─────                  ─────
<1>      power on      <p 1>
<0>      power off     <p 0>
<T 12 1> close T#12    <H 12 1>
<T 12 0> throw T#12    <H 12 0>
                       <Q 5>   sensor 5 active
                       <q 5>   sensor 5 inactive
<t cab loco s d>       throttle (speed/dir/cab functions)
<W cv v>               write CV (service mode)
<R cv>                 read CV  (service mode)
<w cab cv v>           write CV (ops mode)
```

## Transports

- **Serial** (USB to DCC-EX command station, default 115200 baud)
- **TCP** (DCC-EX Wi-Fi / Ethernet command stations)
- **Loopback** (tests)

## Settings

```python
{"kind": "serial", "port": "COM4", "baud": 115200}
{"kind": "tcp", "host": "dccex.local", "tcp_port": 2560}
{"kind": "loopback", "loopback_port": LoopbackPort(...)}
```

## EX-RAIL automation

EX-RAIL is DCC-EX's embedded automation DSL -- routes and automations
are compiled into the command station's firmware (`myAutomation.h`).
A client can't upload new ones, but can introspect the compiled list
and drive runtime control. Available on an open `DccexConnection` as
`conn.exrail`.

Introspection (request + wait for reply, 2 s default timeout):

| Call | Wire |
|---|---|
| `exrail.list_ids()` -> `tuple[int, ...]` | `<JA>` -> `<jA id0 id1 …>` |
| `exrail.detail(id)` -> `ExRailEntry \| None` | `<JA id>` -> `<jA id R\|A "desc">`; returns None on `<jA id X>` |
| `exrail.refresh()` -> `tuple[ExRailEntry, …]` | list + per-id detail |

Control (fire-and-forget; the CS doesn't ack):

| Call | Wire |
|---|---|
| `exrail.start_route(id)` | `</ START id>` |
| `exrail.start_automation(id, cab)` | `</ START cab id>` |
| `exrail.kill(id)` / `exrail.kill("ALL")` | `</ KILL id>` / `</ KILL ALL>` |
| `exrail.pause()` / `exrail.resume()` | `</ PAUSE>` / `</ RESUME>` (PAUSE also E-STOPs all locos) |
| `exrail.set_signal(aspect, signal_id)` | `</ RED signal_id>` / `</ AMBER …>` / `</ GREEN …>` |
| `exrail.reserve_block(block)` / `exrail.free_block(block)` | `</ RESERVE id>` / `</ FREE id>` |
| `exrail.latch(vpin)` / `exrail.unlatch(vpin)` | `</ LATCH id>` / `</ UNLATCH id>` |
| `exrail.set_logging(on)` | `<D EXRAIL ON\|OFF>` |

Wire shapes verified against the DCC-EX
[EX-RAIL command reference](https://dcc-ex.com/exrail/exrail-command-reference.html)
and the [throttles tech reference](https://dcc-ex.com/throttles/tech-reference.html).

## What's intentionally deferred

| To | What |
|---|---|
| Stage 11 | **Traction sound triggers** — upstream decoder feature set, not part of the core DCC-EX protocol. |

Ops-mode CV read via RailCom (`<r cab cv>` -> `<r cab cv value>`) is
wired in `DccexProgrammer.read_cv_ops`. It raises `RuntimeError` with
a clear "verify RAILCOM enabled in the firmware" message when the CS
times out (which is what you see on a CS built without RailCom
support -- the DCC-EX firmware silently drops the request).
