# jmri-openlcb — OpenLCB/LCC driver (Stage 6)

Event-driven peer-to-peer driver. OpenLCB (Open Layout Command Control) is
the standards-track CAN-bus protocol used by the NMRA's LCC products and
the broader OpenLCB community. Unlike DCC command-station protocols
there's no master/slave shape — every node both **produces** and
**consumes** 64-bit *event IDs* on a shared bus.

This driver demonstrates that the HAL handles event-bound beans without
strain. Where LocoNet and DCC-EX address turnouts by integer, OpenLCB
binds them to two 64-bit event IDs (one per state). The
`SystemConnection` ABC accommodates both via the same `Manager[T]`
shape — bean construction takes the event IDs at `configure()` time
instead of being derived from a system name.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **29 / 29** |
| Coverage | exceeds 85% target |

## Wire protocol summary

```
GridConnect ASCII frame:
    :X<8-hex header>N<0..16 hex data>;

Header (29 bits):
    bit 28 — extended frame flag (always 1 for OpenLCB)
    bits 12..23 — MTI (Message Type Indicator)
    bits 0..11 — source node alias (12-bit)

PCER (Producer/Consumer Event Report) — MTI 0x5B4:
    Data is the 8-byte event ID.
    Sent by producers when "this event happened" on their node.
    Every consumer that cares about this event ID reacts.
```

We decode every frame the bus carries; PCER frames also surface on the
`OlcbTrafficController.events` Signal as typed `EventReport` instances.
Non-PCER MTIs reach listeners via the `OlcbTrafficController.frames`
Signal preserved verbatim.

## Bean configuration

```python
import asyncio
from jmri_openlcb import OlcbConnection, EventId, loopback_pair

async def main():
    a, _ = loopback_pair()  # or a real transport
    conn = OlcbConnection()
    await conn.open({"kind": "loopback", "loopback_port": a, "source_alias": 0x123})

    turnout = await conn.turnouts.configure(
        "XT1",
        closed_event=EventId.parse("05.01.01.01.4C.43.00.01"),
        thrown_event=EventId.parse("05.01.01.01.4C.43.00.02"),
    )
    sensor = await conn.sensors.configure(
        "XS5",
        active_event=EventId.parse("05.01.01.01.4C.43.10.01"),
        inactive_event=EventId.parse("05.01.01.01.4C.43.10.02"),
    )
    # ... drive turnout, observe sensor ...
    await conn.close()

asyncio.run(main())
```

Note: `provide()` raises `NotImplementedError` — OpenLCB beans require
explicit event-ID binding, so callers must use `configure()`.

## Transports

| Transport | Hardware | Settings |
|---|---|---|
| Loopback | None (tests/demos) | `{"kind": "loopback", "loopback_port": LoopbackPort}` |
| Serial | USB-to-CAN GridConnect bridges | `{"kind": "serial", "port": "COM5", "baud": 333333}` |
| TCP | Network-attached LCC bridges | `{"kind": "tcp", "host": "lcc.local", "tcp_port": 12021}` |

## Stage-6.5 features (shipped)

The five items originally scoped as Stage-6.5 follow-ons are all in
place; the openlcb suite is 512 tests passing.

| Feature | Module(s) | Notes |
|---|---|---|
| Verify Node discovery | `verify.py` | `VerifyNodeGlobal` / `VerifyNodeAddressed` request + `VerifiedNodeID` response. Driver participates — doesn't just forward. |
| Identify Producers / Consumers | `event_ident.py` | Full `IdentifyEventsGlobal` / `IdentifyProducers` / `IdentifyConsumers` cycle with `ProducerIdentified` + `ConsumerIdentified` replies. |
| CDI (Configuration Description Information) | `cdi.py` | Full XML tree model + wire codec, SNIP editor path (`snip.py` + `snip_editor` tests), FDI loco-function descriptors (`fdi.py`). |
| Datagrams + stream protocol | `datagrams.py` + `stream.py` | Core transport for memory config access (CV reads/writes via OpenLCB). |
| Well-known power events | `power.py` | `WellKnownEventPowerManager` wires `power.set_power()` to the reserved EStop / Track-Power event IDs. |
| Node alias allocation (CID/RID) | `alias.py` + `duplicate_nid.py` + `stale.py` | Full CID/RID negotiation, duplicate-NID detection, stale-alias reclaim. |
