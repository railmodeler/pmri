# jmri-decoderpro — Stage 8 of the JMRI Python rewrite

DCC decoder programming catalog + programming-session management.
Replaces JMRI's ``jmri.jmrit.symbolicprog.*`` Java package hierarchy
with a lean Python core: parse the 881 decoder XML files (Stage 2 work)
into a registry, expose a programming session that binds a decoder
definition to a :class:`Programmer` from any driver.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **15 / 15** passing |
| Coverage | exceeds 85% target |

## How the pieces fit

```
   ┌─────────────────────┐
   │ Decoder XML (881)   │   parsed by jmri-core.persistence.decoder_xml
   └──────────┬──────────┘
              ↓
   ┌─────────────────────┐
   │ DecoderCatalog      │   in-memory registry, find/list by mfg/family/model
   └──────────┬──────────┘
              ↓
   ┌─────────────────────┐    ┌──────────────────────┐
   │ DecoderDefinition   │ ←→ │ Programmer (any drv) │
   └──────────┬──────────┘    └──────────┬───────────┘
              ↓                          ↓
              └────── ProgrammingSession ─┘
                        read_cv / write_cv / read_all / read_identity
```

Two session shapes:

* **Service mode** — uses `connection.programmers.global_programmer()`
  (the programming-track singleton). Reads + writes work on any decoder.
* **Ops mode** — uses `connection.programmers.ops_programmer(address)`.
  Writes work on a running loco; reads require RailCom.

## Usage

```python
import asyncio
from jmri_decoderpro import DecoderCatalog, ProgrammingSession
from jmri_nce import NceConnection

async def main():
    # Load the catalog (typically pointed at JMRI's xml/decoders/).
    catalog = DecoderCatalog.from_directory("xml/decoders")
    print(f"Loaded {len(catalog)} decoder definitions")

    # Open a connection with a real programmer.
    conn = NceConnection()
    await conn.open({"kind": "serial", "port": "COM4", "auto_detect": False})

    # Pick a decoder definition.
    definition = catalog.find("ESU Roco 10745", "Roco 10745")
    if definition is None:
        raise SystemExit("decoder not found")

    # Service-mode session against the programming track.
    session = ProgrammingSession.from_global(conn, definition)
    version, mfg = await session.read_identity()
    print(f"CV7={version}, CV8={mfg}")

    # Read every variable the decoder model exposes.
    values = await session.read_all()
    for cv, val in sorted(values.items()):
        print(f"CV{cv} = {val}")

    await conn.close()

asyncio.run(main())
```

## What's intentionally deferred

| To | What |
|---|---|
| Stage 11 | **Ops-mode CV read** — requires RailCom. Writes work on a running loco; reads need the RailCom cutout channel which crosses over with the DCC-EX RailCom work. |
| Stage 11 | **Pane / row / column-level `<qualifier>` gates** — the common per-variable qualifier is fully modelled (eq/ne/gt/ge/lt/le and the fixed-up `exists` branch). Qualifiers that live directly on a `<pane>`, `<row>`, or `<column>` element (per the `panetype.xsd` schema) land with the Panel Editor port, which is where the row/column structure gets reified from `raw_element`. |
| Stage 11 | **Full JMRI mfg/family/model UI symmetry** — the identify picker groups by (mfg, family) and picks a specific model when productID narrows to one; JMRI Java's picker has additional filters (decoder revision date, connector type). Practical impact minimal. |

**Resolved since the Stage 8 initial README** (batch cleaned up in the
recent DecoderPro sprint):

* ~~Split-CV / composite variables~~ — indexed CV addressing
  (`16.1.275`), split-byte (`17:2`), and composite presets all wired
  end-to-end (read + write, CV31/CV32 bank setup, preset fan-out to
  sub-variable writes).
* ~~Variable-level field types~~ — DECIMAL, HEXADECIMAL, ENUM,
  COMPOSITE, CONSTANT, SPEED_TABLE, and SPLIT_* kinds all classify and
  render with their typed widgets.
* ~~Pane layout~~ — rich column/row/group tree, Swing GridBagLayout
  emulation (`<grid>`/`<griditem>` with anchors), `<fnmapping/>`
  checkbox matrix for the standard Function Map, SoundTraxx's
  Extended Function Map (dropdown + checkbox row per output),
  `<display format="checkbox">` widgets, per-widget dirty tracking,
  batched pane Read/Write with a thermometer + cancel.
* ~~Roster integration write-through~~ — reads and writes persist to
  `roster/<id>.xml` via `<values><CVvalue>` (JMRI-canonical shape)
  plus a legacy `rosters/<id>.xml` fallback with an ambiguity-picker
  UI when both files exist.
* ~~Auto-identify~~ — CV7 + CV8 + (for SoundTraxx) CV253/255/256
  narrow to a specific model; auto-retry once on the 0/48/255 wire-
  fail sentinels. Manufacturer table loaded from JMRI's own
  `decoderIndex.xml` (179 entries) so the mapping stays in sync.
* ~~Decoder-declared pane override~~ — when a decoder file ships its
  own pane with the same name as a standard programmer pane
  (SoundTraxx "Function Map" vs Comprehensive.xml's generic version),
  the decoder-declared variant wins in the tab slot.
* ~~ProductID filtering + `<qualifier>` runtime evaluation~~ —
  `<variable include="..."/>`/`exclude="..."` filters by the current
  model's productID; `<qualifier>` triples (eq / ne / gt / ge / lt /
  le / exists) evaluate against other variables' current CV values so
  per-model / per-Sound-Option alternates render as a single row
  instead of a pile-up. The ``exists`` relation reads JMRI Java's
  ``ArithmeticQualifier`` convention: ``<value>0</value>`` is "require
  ABSENT", any non-zero is "require PRESENT"; permissive on
  ``yes``/``no`` / ``true``/``false`` strings that show up in some
  decoder files even though the schema types the field as ``xs:int``.

## Layout

```
jmri-decoderpro/
├── pyproject.toml
├── README.md
├── src/jmri_decoderpro/
│   ├── __init__.py
│   ├── catalog.py         DecoderCatalog + DecoderDefinition + DecoderKey
│   ├── session.py         ProgrammingSession + CvCacheEntry + read_identity
│   └── py.typed
└── tests/
    └── unit/              20 tests against Stage 0 golden decoders + FakeProgrammer
```

## What it proves

1. **Decoder XML is data, not code.** The 881 JMRI decoder files describe
   CVs declaratively; once parsed (Stage 2), no Java port of `symbolicprog`
   is needed — a small registry + session covers the practical surface.
2. **The Programmer ABC works for every driver.** NCE, LocoNet, and DCC-EX
   all implement it; this package consumes it uniformly.
3. **Auto-identify works.** `read_identity` reads CV7+CV8 and
   `DecoderCatalog.identify_by_cv` returns matching definitions.
4. **End-to-end CV programming from the web works.** Open
   `/programmer` in jmri-web, point at a decoder XML directory, browse
   to a model, and the read/write buttons hit a real programmer through
   the JSON API.
