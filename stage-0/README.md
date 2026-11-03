# JMRI → Python rewrite: Stage 0 deliverables

This workspace is the **contract** the Python rewrite must satisfy. Everything
in here was extracted from the installed JMRI 5.14.0 build at
`C:\Program Files (x86)\JMRI\` — no clone of the upstream Java repo was needed.

## What's here

```
stage-0/
├── README.md                          ← you are here
├── contracts/                         ← what the Python rewrite must preserve
│   ├── json-schema/                   97 JSON schemas (server + client) per topic
│   ├── xml/
│   │   ├── schema/                    37 .xsd / .dtd / .xsl files from xml/schema/
│   │   └── golden/                    38 real-world panel/roster/decoder files
│   │       └── MANIFEST.md            what each golden file exercises
│   └── script-api.md                  2118 attribute paths from 369 Jython scripts
├── inventory/
│   ├── jmrix-systems.md               52 hardware systems, class counts, sub-pkgs
│   ├── package-deps.dot               1577 package-level dependency edges
│   └── port-priority.md               6 topological layers + SCC analysis
├── tools/                             extraction scripts (re-runnable)
│   ├── extract_script_api.py
│   ├── inventory_jmrix.py
│   └── package_deps.py
└── tests/conformance/                 pytest scaffolding for Stage 1+
    ├── conftest.py
    ├── test_json_schema_present.py
    └── test_xml_golden_present.py
```

## What we learned

### 1. The JSON wire format is in the jar already

JMRI ships its API JSON Schemas as resources at `jmri/server/json/**/*.json`
inside `jmri.jar`. **All 97** are now in `contracts/json-schema/`. Each topic
has a `<topic>-server.json` (what JMRI emits) and `<topic>-client.json` (what
JMRI accepts). These are the rewrite's wire contract — Stage 3's FastAPI
server must validate against them.

### 2. The XML formats are stable and well-schema'd

37 XSD schemas cover panels, decoders, rosters, signals, operations, timetable.
The 38 golden files in `contracts/xml/golden/` are real artifacts (sample panels,
demo operations roster, 16 different manufacturer decoder defs from 2 KB to
55 KB). Stage 1's persistence layer must load + re-emit these with byte-for-byte
canonical-XML equality.

### 3. The script API surface is much smaller than feared

369 Jython scripts → only **2,118 unique attribute paths**, and the long tail
drops off fast. The top 200 paths cover the vast majority of scripts. Top hits:
`jmri.InstanceManager.getDefault` (88), `sensors.provideSensor` (68),
`turnouts.provideTurnout` (59), `AbstractAutomaton` (45). See
`contracts/script-api.md` for the ranked list.

### 4. Hardware drivers are mostly leaves — parallelizable

The dependency graph shows **most `jmrix.*` packages are at layer 0** — they
have no internal JMRI dependencies on each other. Once `pmri_core` (Stage 1)
exists, the 40+ drivers can be ported by independent contributors in parallel.

### 5. The Java core is one giant SCC, which we won't replicate

Layer 4 of the dependency graph is a single strongly-connected component of
**103 packages and 7,971 classes** — `jmri`, all of `jmrit.*`, the major
`jmrix.*` drivers, the server, swing helpers. This is the cost of JMRI's
Reflections-based plugin discovery: everything ends up tangled.

The Python rewrite breaks this cycle **by design**: `pmri_core` depends on
nothing in `jmri.jmrix.*`; drivers register themselves via
`importlib.metadata` entry points. Dependencies flow one way only:
`drivers → core`, never `core → drivers`. The SCC disappears.

### 6. High fan-in packages = Stage 1's must-have list

| JMRI package | Fan-in | Stage 1 mapping |
|---|---:|---|
| `jmri` | 154 | `pmri_core` — NamedBean, Manager, InstanceManager |
| `jmri.util` | 141 | `pmri_core.util` |
| `jmri.jmrix` | 72 | `pmri_core.hal` — connection ABC |
| `jmri.implementation` | 68 | `pmri_core.beans.*` — default bean impls |
| `jmri.configurexml` | 64 | `pmri_core.persistence` — XML load/save |
| `jmri.managers` | 49 | `pmri_core.manager` — default managers |
| `jmri.profile` | 35 | `pmri_core.persistence.profile` |
| `jmri.beans` | 29 | folded into bean abstractions (no PropertyChangeListener) |

These are what Stage 1 must deliver first; everything else builds on them.

## Re-running the extraction

Any tool in `tools/` can be re-run against a newer JMRI install:

```powershell
python tools\extract_script_api.py "<JMRI>\jython" contracts\script-api.md
python tools\inventory_jmrix.py    "<JMRI>\jmri.jar" inventory\jmrix-systems.md
python tools\package_deps.py       "<JMRI>\jmri.jar" inventory\package-deps.dot inventory\port-priority.md 3
```

## Open Stage 0 follow-ups (not blocking Stage 1)

- **Per-system port-effort ratings** in `inventory/jmrix-systems.md` — left as
  `_TODO_` until a domain pass with someone who knows the protocols.
- **JSON service behavioral notes** — the schemas tell us message shapes but
  not semantics (e.g., when does the server push a state-change message?).
  Filling these in requires reading
  `java/src/jmri/server/json/<topic>/Json*SocketService.java` in the upstream
  repo. Defer to Stage 3 when we're actually implementing the server.
- **Visualizing the .dot graph** — `inventory/package-deps.dot` opens in any
  Graphviz viewer; render with `dot -Tsvg package-deps.dot -o package-deps.svg`.

## Stage 1 entry conditions — all green

- [x] JSON API contract captured (97 schemas)
- [x] XML format contract captured (schemas + 38 golden files)
- [x] Script API surface captured (2118 paths, ranked)
- [x] Hardware-system inventory built (52 systems, class counts)
- [x] Dependency graph + port-priority hint produced
- [x] Conformance test scaffold in place — Stage 1 just needs to make the
      tests in `tests/conformance/` start passing
