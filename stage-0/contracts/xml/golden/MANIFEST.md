# Golden XML samples — round-trip conformance fixtures

Each file under this directory is a real JMRI artifact captured from the
v5.14.0 install. Stage 1's persistence layer must load and re-emit each one
such that `lxml.etree.canonicalize(load(save(load(f))))` equals
`lxml.etree.canonicalize(load(f))` — i.e., byte-for-byte after canonicalization.

## Coverage matrix

| Category | Files | Schema(s) exercised | What it stresses |
|---|---|---|---|
| Panel files | `samples/*.xml`, `samples/javaone/*.xml` | layout-5-5-5.xsd, panelfile.xsd | Layout editor geometry, icons, sensor/turnout bindings |
| Logix / state machines | `samples/LogixStateMachine.xml`, `samples/TwoColumnMachine.xml` | layout, conditional logic | Embedded Logix tree, conditional expressions |
| Warrants | `samples/Warrant.xml` (392 KB) | layout, warrant elements | Largest realistic file — performance smoke test |
| Operations rosters | `demoOperations/Operations*.xml` | locomotive-config, operations XSDs | Cars, engines, locations, routes, trains, kernels |
| Operations panel | `demoOperations/Operations Demo Panel.xml` | layout + operations cross-refs | Cross-file references between panel and operations |
| Timetable | `demoTimetable/TimeTableData.xml` | timetable.xsd | Train schedules, layovers, station stops |
| Signal mast logic | `demoTimetable/TestSample.sgn` | aspecttable / appearancetable | Signal aspect lookup tables |
| Decoder defs | `decoders/*.xml` (16 manufacturers) | decoder.xsd, decoder-4-15-2.xsd | CV definition variety across DCC vendors |
| Locomotive-config | `locomotives/*.xml` | locomotive-config.xsd | Synthetic representative — JMRI generates these at runtime, no real samples ship in the install. Stage 1 round-trip target. |

## Manufacturer coverage in decoders/

Picked the smallest representative file per manufacturer to keep golden tests fast:

- **Digitrax** — Kato_EM13 (4.3 KB) — board replacement, minimal CVs
- **ESU** — Roco_10745_old (5.3 KB) — old-style ESU LokPilot
- **TCS** — Fn_FW01 (2.8 KB) — function-only decoder
- **Zimo** — Unified_software_MX634Cv31 (4.6 KB) — modern sound-capable
- **NCE** — Light-It_signal (4.1 KB) — accessory/signal decoder
- **Lenz** — Gold (6.9 KB) — flagship Lenz mobile decoder
- **SoundTraxx** — Blu_Electric (8.7 KB) — sound decoder
- **MRC** — 14step (2.2 KB) — 14-speed-step legacy
- **Uhlenbrock** — 32500 (2.7 KB) — DE/EU manufacturer
- **QSI** — Stock (54.9 KB) — **largest realistic decoder file**, stress-test
- **Public Domain** — Fucik_1 (2 KB) — community decoder def
- **Hornby** — DigitalFnOnly (2.1 KB) — UK manufacturer
- **TAMS** — LD-G-21 (15.9 KB) — German manufacturer, mid-size
- **Doehler & Haass** — susi_sd_fw1.14_SH05-10A (4.4 KB) — SUSI interface
- **Trix** — Snd_Glasscar (5.5 KB) — sound carriage decoder
- **Atlas** — VO1000 (4.5 KB) — locomotive-specific

## Adding to the corpus

When a real-world bug surfaces from a user, drop their (sanitized) panel/roster file
into the appropriate subdirectory and add a row above. The golden test suite
automatically picks it up by glob.
