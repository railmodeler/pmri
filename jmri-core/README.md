# jmri-core — Stages 1–2 of the JMRI Python rewrite

The headless domain model + XML persistence. No I/O over the network, no UI.
Drivers and servers depend on this; this package depends on nothing
JMRI-specific.

## Quality gates (Stage 2 close-out)

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | no issues across 32 source files |
| `pytest tests/` | **151/151 passing** |
| Branch coverage | **93.6%** (target 90%) |
| Stage 0 conformance | **174/174 supported-format tests pass**, 2 skip (signal-mast `.sgn`), 97 xfail (Stage 3 server) |

## What's shipped in Stage 2

### XML persistence

| Module | Round-trip guarantee | Schema |
|---|---|---|
| `roster_xml` | Byte-equal after two save passes | `locomotive-config.xsd` |
| `roster_index_xml` | Semantic (model-equal) | `roster.xsd` |
| `operations_xml` | Semantic (model-equal); byte-equal deferred to Stage 2.5 | `operations-*.dtd` (6 file families) |
| `decoder_xml` | **Read-only** parse (editing is Stage 8 / DecoderPro) | `decoder-4-15-2.xsd` |
| `panel_xml` | Envelope-level (tree preserved opaquely) | `layout-5-5-5.xsd` |
| `profile` | `~/JMRI/` directory layout, no XML | — |

### Operations module — 6 file families

`OperationsFile` accepts any `operations-config` document and exposes typed
accessors over the major top-level elements: `engines()`, `cars()`,
`routes()` (with embedded `RouteLocation`s), `trains()`, `locations()`,
`setup()` for the configuration block.

Common attributes (`id`, `name`, `roadName`, `roadNumber`, `type`, `length`,
`locationId`, `model`, `consist`, etc.) are typed Pydantic fields; every
other attribute lands in `.extra: dict[str, str]` so unknown attributes
survive round-trip unchanged. Tested against all five Stage 0 golden
operations files plus the setup file.

### Decoder XML — 881 files

`DecoderFile` parses any `xml/decoders/*.xml` file into a structural model:

* `family` (name, manufacturer) + list of `model` entries (connector,
  outputs, protocols, physical size)
* flat `variables` list of CV definitions (CV#, item, default, mask, label,
  min/max, opsOnly flag)
* `includes` — recorded `xi:include` hrefs for downstream resolution

`iter_decoder_files(dir_path)` walks a directory yielding one `DecoderFile`
per `.xml`, silently skipping files that fail to parse — JMRI ships a few
quirky decoders and one bad file shouldn't break enumeration of the rest.

### Profile layout

`Profile(root=…, name=…)` exposes every standard JMRI subdirectory as a
named property: `roster_dir`, `operations_dir`, `panel_dir`, `log_dir`,
`profile_dir`, `throttle_dir`, `signal_systems_dir`. `Profile.default()`
chooses the OS-appropriate root (`%userprofile%\JMRI` on Windows,
`~/Library/Preferences/JMRI` on macOS, `~/.jmri` on Linux). `ensure()`
creates every directory idempotently.

## Persistence — current status

* `roster_xml`, `decoder_xml`, `operations_xml`, and `panel` envelope all
  round-trip semantically. `RosterIndex` is byte-equal. The remaining
  byte-equality and panel-element gaps are documented in the deferred
  table below.

## What's shipped in Stage 1

### Core abstractions

| Module | What |
|---|---|
| `jmri_core.bean` | `NamedBean` Protocol + `AbstractNamedBean` mixin |
| `jmri_core.events` | `Signal[T]` — async-friendly fan-out, replaces `PropertyChangeListener` |
| `jmri_core.manager` | `Manager[T: NamedBean]` — per-type registry with system + user-name lookup |
| `jmri_core.instance` | `InstanceManager` — explicit service-locator (no global state) |
| `jmri_core.names` | `SystemName` — parse / format JMRI system names (`"LT12"` etc.) |
| `jmri_core.state` | Shared state enums — values match JMRI's Java integers byte-for-byte |
| `jmri_core.plugins` | Entry-point discovery for `jmri.connection` |

### 13 domain types

| Bean | Type letter | State enum | Driver-override hook |
|---|:-:|---|---|
| `Turnout` | T | `TurnoutState` | `set_commanded_state` |
| `Sensor` | S | `SensorState` | `_set_known_state` |
| `SignalHead` | H | `SignalAppearance` | `set_appearance`, `set_held` |
| `SignalMast` | F | aspect strings | `set_aspect`, `set_held` |
| `Block` | B | `BlockState` | `attach_sensor` / `_set_state` |
| `Memory` | M | any value | `set_value` |
| `Reporter` | R | any value | `set_report` |
| `Light` | L | `LightState` (+ intensity) | `set_state`, `set_intensity` |
| `Route` | O | `RouteState` | `fire` |
| `PowerManager` | — | `PowerState` | `set_power` |
| `Throttle` | — | speed / dir / 32 functions | `set_speed`, `set_direction`, `set_function` |
| `RosterEntry` | — | Pydantic data model | — |
| `Consist` | — | Pydantic data model | `add`, `remove` |

### HAL

`jmri_core.hal.connection.SystemConnection` — abstract base every hardware
driver implements. Default impl wires up all the standard managers; drivers
override `_make_*_manager` hooks to plug in hardware-aware subclasses.

`jmri_core.hal.internal.InternalConnection` — pure in-memory loopback. Used
by every test in this package and by the Stage 3 server's contract tests.
Registered as the `internal` entry point in `pyproject.toml`.

### Persistence

`jmri_core.persistence.roster_xml` — round-trip support for
`locomotive-config` XML (the per-loco files JMRI's roster writes). Validated
against `stage-0/contracts/xml/schema/locomotive-config.xsd`.

```powershell
cd C:\Users\jpetr\pmri\jmri-core
pip install -e .[dev]
pytest --cov=jmri_core
mypy src
ruff check src
```

## What it proves

1. **Plugin model works end-to-end.** `pip install -e .` registers the
   `internal` entry point; `jmri_core.plugins.load_connections()` finds it
   without any in-code registration.
2. **No circular dependencies.** mypy strict passes on the whole tree; the
   import graph is a DAG from `bean` ↔ `events` at the leaves up through
   `beans/*` → `hal` at the top.
3. **State enums match JMRI.** The integer values in `state.py` map 1:1 to
   the constants in JMRI's Java `Turnout.java`, `Sensor.java`, etc. — golden
   XML files load back with the same numeric state.
4. **Driver-shaped abstractions.** `set_commanded_state` / `_set_known_state`
   / `_make_*_manager` overrides are the only hooks a driver touches.
   Stage 4 (LocoNet) won't need anything new in this package; verified by
   the `InternalConnection` smoke tests.

## What's intentionally deferred

| To | What | Why |
|---|---|---|
| Stage 11 | Full panel element modeling — layout-editor positionables, signal icons, train icons, route tables | Layout Editor is its own project — see Stage 11 in the plan |
| Stage 11 | Byte-equality round-trip for operations and panel files | JMRI's Java writer has specific attribute-ordering and whitespace conventions; matching them requires a custom canonical writer |

**Resolved since Stage 2 close-out:** decoder XML XInclude resolution
now runs at load time (with per-recursion-path cycle detection so
SoundTraxx-style repeated fragment references don't get flagged) —
`DecoderPro` consumes the fully-resolved tree.

## Layout

```
jmri-core/
├── pyproject.toml                  hatchling + pytest + mypy + ruff config
├── README.md
├── src/jmri_core/
│   ├── __init__.py                 re-exports the top-level API
│   ├── bean.py                     NamedBean + AbstractNamedBean
│   ├── events.py                   Signal[T]
│   ├── instance.py                 InstanceManager
│   ├── manager.py                  Manager[T: NamedBean]
│   ├── names.py                    SystemName parse/format
│   ├── state.py                    Shared enums (JMRI-compatible values)
│   ├── plugins.py                  Entry-point discovery
│   ├── beans/                      13 bean modules
│   ├── hal/                        SystemConnection ABC + InternalConnection
│   └── persistence/                roster_xml load/save
└── tests/
    ├── conftest.py
    └── unit/                       87 tests across 12 files
```

## Stage 3 entry conditions — all green

- [x] `jmri_core` builds and passes `mypy --strict` across 32 source files
- [x] 93.6% branch coverage (target ≥90%)
- [x] All 13 standard beans implemented with JMRI-matching state enums
- [x] `RosterEntry` byte-equal round-trip + `RosterIndex` semantic round-trip
- [x] All 6 Operations module file families load + semantic round-trip
- [x] 881-decoder loader handles real JMRI install corpus
- [x] Panel XML envelope round-trip (full element modeling deferred to Stage 11)
- [x] `Profile` directory layout matches JMRI's Java behavior across platforms
- [x] `InternalConnection` HAL stub registered via entry point
- [x] No I/O, no UI, no async network code in this package
- [x] Stage 0 conformance suite: 174 supported-format round-trips pass
