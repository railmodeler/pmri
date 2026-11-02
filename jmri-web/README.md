# jmri-web — Stage 5 of the JMRI Python rewrite

Server-rendered web UI built on **htmx + Jinja2 + FastAPI** atop the
Stage 3 JSON server. One Python process serves both the JMRI JSON wire
API at `/json` and the human-facing pages at `/`, `/throttle`,
`/turnouts`, `/sensors`, `/roster`, `/panel`.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | no issues across 3 source files |
| `pytest tests/` | **20 / 20** passing |
| Branch coverage | **98.1%** (target 85%) |

## Pages

| Path | What |
|---|---|
| `/` | Home — connection info, active connection status, track power toggle, fast-clock strip, topic list |
| `/throttle` | Throttle UI — DCC address, speed slider, direction, F0..F28 buttons |
| `/turnouts` | Turnouts table — add, throw/close, inline edit user-name, live state via WebSocket |
| `/sensors` | Sensors table — add, simulate active/inactive, inline edit, live state |
| `/lights` | Lights table — add, on/off, intensity slider, inline edit, live state |
| `/roster` | Roster list + full CRUD (create/edit/delete), profile-scoped, edit inline |
| `/programmer` | Decoder programmer — catalog load, browse mfg/family/model dropdowns, identify (CV7/CV8 + SoundTraxx productID), single-CV read/write |
| `/programmer/loco/<id>` | Per-loco programmer — DecoderPro-style tabbed pane view: Basic / Motor / Speed Table / Function Map / Lights / Consist / Sound / … / CVs, with per-variable widgets (enum dropdowns, checkbox rows, indexed CVs, composite presets, `<fnmapping/>` matrix, SoundTraxx Extended Function Map grid), dirty tracking, batched pane read/write with progress + cancel |
| `/scripts` | Scripts UI — inline editor, table of running scripts, per-row stop |
| `/panel` | Panel XML viewer — upload + parse high-level structure |
| `/json` | JSON wire API (HTTP POST + WebSocket) — unchanged from jmri-server |

## Architecture

```
   ┌─────────────────────────────────────────────────────────────┐
   │                  Single FastAPI process                     │
   │                                                             │
   │   HTML / htmx / JS              JSON envelope               │
   │   ─────────────────             ───────────────             │
   │      ↓ form POST                   ↓ POST/WS                │
   │   ┌──────────┐                  ┌──────────┐                │
   │   │ jmri-web │ ───┐         ┌─→ │jmri-server│               │
   │   │ templates│    │         │   │ /json     │               │
   │   └──────────┘    │         │   └────┬─────┘                │
   │                   ↓         ↑        ↓                      │
   │              ┌─────────────────────────────────┐            │
   │              │  ServiceRegistry (in-process)   │            │
   │              └─────────────┬───────────────────┘            │
   │                            ↓                                │
   │              ┌─────────────────────────────────┐            │
   │              │  jmri-core (HAL: bean + manager)│            │
   │              └─────────────┬───────────────────┘            │
   │                            ↓                                │
   │              ┌─────────────────────────────────┐            │
   │              │  Driver: internal/loconet/nce/… │            │
   │              └─────────────────────────────────┘            │
   └─────────────────────────────────────────────────────────────┘
```

Key design choices:

* **One process** — templates call `registry.dispatch()` directly, not
  over HTTP. No serialization overhead, no port juggling.
* **htmx for actions** — form posts return small HTML fragments that
  replace just the affected row or status block. No SPA framework.
* **WebSocket for push** — `/static/app.js` opens one WebSocket on page
  load, exposes a `jmriBus` singleton, and individual page scripts
  subscribe to topic types to update DOM in place. State changes from
  *any* source (web, another HTTP client, the driver's own monitor)
  reach every connected page.

## Layout

```
jmri-web/
├── pyproject.toml
├── README.md
├── src/jmri_web/
│   ├── __init__.py
│   ├── app.py                build_web_app() — mounts jmri-server + templates
│   ├── routes.py             every template route + htmx action handler
│   ├── static/
│   │   ├── style.css         minimal modern dark stylesheet
│   │   └── app.js            jmriBus WebSocket singleton
│   └── templates/
│       ├── base.html         shared layout + nav
│       ├── home.html
│       ├── throttle.html
│       ├── turnouts.html
│       ├── sensors.html
│       ├── roster.html
│       ├── panel.html
│       └── _*.html           htmx-swappable fragments
└── tests/
    └── integration/
        ├── test_pages.py     every page renders
        ├── test_actions.py   every htmx mutation returns the right fragment
        └── test_websocket.py state-change push reaches WS clients
```

## Running it

```powershell
pip install -e jmri-core jmri-server jmri-web    # + any drivers you want
uvicorn jmri_web:create_app --factory --host 0.0.0.0 --port 12080
```

Then open <http://localhost:12080>. The internal connection is active by
default; pick a real driver via the `JMRI_CONNECTION` and
`JMRI_CONNECTION_SETTINGS` environment variables (see `jmri-server`'s
README for the env-var contract).

## One small change in jmri-server

This stage moved jmri-server's status endpoint from `/` to `/api` so the
web UI's home page isn't shadowed when both packages mount on the same
app. `jmri-server` standalone is unchanged otherwise.

## What's intentionally deferred

| To | What |
|---|---|
| Stage 11 | **Full panel editing** — the panel viewer renders a decent set of elements (sensoricon, turnouticon, signal mast / head icons, memory, text labels, track segments, positionable points, layout turnouts) as SVG with live bean-state colouring, but editing (dragging icons around, authoring new panels) is still part of the full layout-editor port. Unknown element types are counted and surfaced in a footnote. |

Auth is already in place: `/login`, `/logout`, CSRF-protected forms,
session cookies minted via `jmri_server.auth` (manager / middleware /
models / passwords / store), hashed-password storage, and the
`password` login flow all ship. See `test_auth_flow.py` for the
end-to-end coverage.
- [x] No real hardware required to run or test the UI
