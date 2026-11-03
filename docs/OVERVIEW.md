# JMRI Python Rewrite — Overview

This is a from-scratch Python port of the JMRI (Java Model Railroad
Interface) server. The goal is feature parity for the JSON API,
throttle protocol, roster / catalog / programming pipeline, and the
main hardware connections (NCE, LocoNet, DCC-EX, OpenLCB), running
native on a Raspberry Pi without the Java runtime.

## Package layout

Twelve packages, layered so drivers depend on `pmri_core` and
`pmri_server` orchestrates the JSON API + WebSocket surface:

| Package              | Role |
|----------------------|------|
| `pmri_core`          | Domain model — `NamedBean`, `Manager`, `Signal`, `SystemConnection`, `Profile`, persistence + XML codecs |
| `pmri_scripting`     | Async script runner — CPython 3 replacement for JMRI's Jython 2.7 host |
| `pmri_logixng`       | LogixNG (rules + actions) engine + YAML persistence |
| `pmri_nce`           | NCE PowerPro / SB5 driver (RS-232 / USB serial) |
| `pmri_loconet`       | Digitrax LocoNet driver (LocoBuffer USB, LnTcpServer) |
| `pmri_dccex`         | DCC-EX text-protocol driver (serial + TCP) |
| `pmri_openlcb`       | OpenLCB / LCC driver (GridConnect, datagrams, memory config, well-known power events) |
| `pmri_ncemonitor`    | NCE Cab Bus passive monitor (RS-485, 9-bit framing) |
| `pmri_withrottle`    | WiThrottle server (Engine Driver, WiThrottle Lite) |
| `pmri_decoderpro`    | Decoder catalog, programming sessions, per-mfg productID probes |
| `pmri_server`        | JSON API dispatcher, WebSocket push, service registry, profile persistence, config store |
| `pmri_web`           | FastAPI web UI, Jinja2 templates, htmx-driven mutations |

## The core patterns

### `NamedBean` + `Manager`

Every layout object with a system name (turnouts, sensors, lights,
signal heads / masts, memories, reporters, blocks, routes, oblocks,
layout blocks, id tags, audio sources) is a `NamedBean`. A
`Manager[T]` is the per-type registry — `TurnoutManager`,
`SensorManager`, etc. Managers live on the active
`SystemConnection` and expose the same three verbs everywhere:
`provide(system_name)`, `iter()`, `get(system_name)`.

### `SystemConnection`

The `SystemConnection` ABC binds one hardware driver (or the
`internal` fake) to its managers. Attributes:

- `turnouts`, `sensors`, `lights`, `signal_heads`, ...
- `power` — a `PowerManager`
- `throttles` — a `ThrottleManager`
- `programmers` — a `ProgrammerManager`
- `consists` — a `ConsistManager`

Each driver package (`pmri_nce`, `pmri_loconet`, `pmri_dccex`,
`pmri_openlcb`) implements this class once. The `internal`
connection lives in `pmri_core.hal.internal` and provides
software-only beans for scripting / test scenarios.

### `Signal[T]` — the async event bus

`pmri_core.events.Signal` is the async fan-out primitive. Every
manager, connection, and background task publishes events via
signals; the JSON API's push layer subscribes to them and mirrors
onto the WebSocket. Signals are ordinary Python objects, not tied
to any particular threading or asyncio primitive — they work in
tests without an event loop.

### `Profile`

`pmri_core.persistence.Profile` mirrors JMRI Java's per-profile
layout: `<settings-root>/<name>/PanelPro.xml`, `roster.xml`,
`logixng/*.yaml`, `python/*.py`, plus the shared
`<settings-root>/python/` for cross-profile scripts. Profiles are
managed at runtime via the `configProfile` JSON topic.

### `ConfigStore` + `PanelPro.xml`

`pmri_server.config_store.ConfigStore` holds the current
`PanelPro.xml` in memory + syncs to disk on every bean mutation
(atomic write). Bean tables (turnouts / sensors / lights) and
preserved unknown children round-trip losslessly so a `<jmri>` XML
edited by hand keeps its comments and non-modelled sections.

## How a request flows

A typical WebSocket request goes through:

1. **`pmri_web` routes** — Jinja2 templates render HTML; htmx POSTs
   dispatch to the server via `handle.registry.dispatch()`.
2. **`pmri_server.registry.ServiceRegistry`** — maps topic strings
   to `JsonService` subclasses. `dispatch()` normalises the JSON
   envelope, resolves the topic, calls the right method
   (`on_list` / `on_get` / `on_put` / `on_post` / `on_delete`), wraps
   the response.
3. **`JsonService` subclass** — e.g. `ThrottleService`, does its
   business logic. May call driver methods on the active connection
   or read from the config store.
4. **`ServerContext`** — the shared bag of things the services need
   (active `SystemConnection`, `ConfigStore`, `ProfileRegistry`,
   metadata, roster store, decoder catalog).

Push messages go the reverse way: manager `Signal[T].emit()` fires,
a `PushSubscription` (in `pmri_server.push`) formats it into a
JSON envelope, and every connected WebSocket receives it.

## The topics served

The home tab under "Topics available on the JSON API" lists every
registered topic with an accordion help panel. The full method
reference for every service class is at [/docs/](/docs/).

Rewrite-only topics (not in JMRI Java's original API): `logs`,
`configProfile`, `beanTransfer`, `scripts`.

## Where major flows live

| Flow | Code |
|------|------|
| Throttle acquire / speed / release | `pmri_server.services.throttle` → connection's `ThrottleManager` → driver-specific `Throttle` |
| CV programming (service + ops mode) | `pmri_server.services.programmer` → `pmri_decoderpro.session.ProgrammingSession` → connection's `ProgrammerManager` |
| Decoder identify | `ProgrammingSession.read_identity_extended()` → `pmri_decoderpro.probes` → `DecoderCatalog.identify_by_cv_extended` |
| Roster CRUD | `pmri_server.services.roster` → `pmri_server.roster.RosterStore` → `<profile>/roster.xml` |
| Profile switch | `pmri_server.app.switch_active_profile` → rebuild `ConfigStore`, refresh managers from `PanelPro.xml`, rerun script autoload |
| Consist ops | `pmri_server.services.consist` → connection's `ConsistManager` (LocoNet `OpcConsistFunc`, NCE consist opcodes, DCC-EX `<M>`) |
| LogixNG | `pmri_server.services.logixng` → `pmri_logixng.engine` → per-profile `<profile>/logixng/*.yaml` |
| Signalling | `pmri_server.services.signal_system` + `signal_head` + `signal_mast` → `pmri_core.signalling.system.SignalSystem` |
| Fast clock | `pmri_server.services.fast_clock.TimeService` → `pmri_core.fast_clock.FastClock` |

## Reading the reference

The auto-generated pdoc reference at [`packages.html`](packages.html)
mirrors the source tree. Every class + method carries its Python
docstring. When a class's docstring points at a wire format or a
JMRI Java source file, the pointer is expected to stay accurate —
protocol layers cite the upstream vendor tables (NCE, LocoNet,
DCC-EX, OpenLCB) and the JMRI decompiled sources under
`~/Documents/jmri.jar.src/` where relevant.

## Getting started (as a developer)

1. Install packages in dev mode (editable):
   ```
   pip install -e pmri-core -e pmri-server -e pmri-web -e pmri-scripting \
                -e pmri-logixng -e pmri-nce -e pmri-loconet -e pmri-dccex \
                -e pmri-openlcb -e pmri-ncemonitor -e pmri-withrottle \
                -e pmri-decoderpro
   ```
2. Set `JMRI_SETTINGS_DIR` (default `~/JMRI` on Windows,
   `~/Library/Preferences/JMRI` on macOS, `~/.jmri` on Linux).
3. Run the web server:
   ```
   uvicorn pmri_web:create_app --factory --host 0.0.0.0 --port 12080
   ```
4. Open `http://localhost:12080/` to see the home tab.

Regenerate this reference:
```
pip install pdoc
python tools/build_docs.py
```
