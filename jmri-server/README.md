# jmri-server — FastAPI server speaking JMRI's JSON wire protocol

FastAPI + WebSocket server on top of any installed `SystemConnection`
(internal, LocoNet, NCE, DCC-EX, OpenLCB). Every response validates
against the JSON schemas extracted from JMRI's jar in Stage 0.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest` (from `jmri-server/`) | **99 / 100** passing (1 schema-coverage skip) |
| Branch coverage | ≥85% |
| Stage 0 conformance | 14 topics validated against their JMRI schemas |

```powershell
cd C:\Users\jpetr\pmri\jmri-server
pip install -e .[dev]
pytest --cov=jmri_server
mypy src
ruff check src
```

## What's implemented

| Topic | Service | Methods | Notes |
|---|---|---|---|
| `hello` | `HelloService` | get | JMRI/protocol version, heartbeat, railroad, node |
| `ping` | `PingService` | get | round-trip latency check |
| `version` | `VersionService` | get | JMRI + JSON protocol versions |
| `railroad` | `RailroadService` | get | railroad name |
| `node` | `NodeService` | get | node identity + former nodes |
| `power` | `PowerService` | get/list/post/put | track power ON/OFF/IDLE |
| `turnout` | `TurnoutService` | get/list/put/post/delete | + WS push on state change |
| `sensor` | `SensorService` | get/list/put/post/delete | + WS push |
| `light` | `LightService` | get/list/put/post/delete | + WS push |
| `signalHead` | `SignalHeadService` | get/list/put/post/delete | held/appearance + WS push |
| `signalMast` | `SignalMastService` | get/list/put/post/delete | aspect/held + WS push |
| `memory` | `MemoryService` | get/list/put/post/delete | any-value + WS push |
| `reporter` | `ReporterService` | get/list/put/post/delete | report/lastReport + WS push |
| `block` | `BlockService` | get/list/put/post/delete | sensor attach + WS push |
| `route` | `RouteService` | get/list/put/post/delete | fire via state≠0 |
| `roster` | `RosterService` | list/get/put/post/delete | full CRUD; profile-scoped store |
| `throttle` | `ThrottleService` | get/list/put/post/delete | acquire/release, speed/dir/functions, e-stop |
| `programmer` | `ProgrammerService` | get/list/put/post | service + ops mode CV read/write; batched writes; indexed CV setup |
| `time` | `TimeService` | get/list/put/post | fast-clock reading, rate, start/stop |
| `scripts` | `ScriptsService` | get/list/put/post | run inline / from file, stop, list active |

Companion protocol servers (mounted alongside `/json`):

| Package | Protocol | Default port |
|---|---|---|
| `jmri-withrottle` | WiThrottle TCP (Engine Driver / WiThrottle iOS) | 12090 |

## What it proves

1. **Wire-format compatibility.** Every server response is validated against
   the JMRI JSON schema for its topic. Existing JMRI JSON clients (mobile
   throttle apps, JMRI's web UI) can talk to this server without changes.
2. **Push semantics.** `PushManager` subscribes to every bean's
   `state_changed` Signal and broadcasts the bean's current data to all
   connected WebSocket clients on any change. New beans created via
   `Manager.added` automatically gain push listeners.
3. **HTTP + WebSocket share one dispatcher.** Same handlers run for both
   transports; clients can choose whichever fits their needs.
4. **No hardware required.** The server runs entirely on
   `jmri_core.hal.InternalConnection`, making it deployable for tests,
   demos, and CI gates without a DCC layout.

## Wire format

The JMRI envelope is symmetric between HTTP and WebSocket:

**Request:**
```json
{ "type": "turnout", "method": "post", "id": 7,
  "data": { "name": "IT3", "state": 4 } }
```

**Response:**
```json
{ "type": "turnout", "method": "post", "id": 7,
  "data": { "name": "IT3", "userName": null, "comment": null,
            "properties": [], "inverted": false,
            "state": 4, "feedbackMode": 1,
            "feedbackModes": [1, 2, 4, 8, 16, 32, 64, 128, 256] } }
```

**WebSocket push** (unsolicited, no `id`):
```json
{ "type": "turnout", "data": { "name": "IT3", ..., "state": 4 } }
```

**List shorthand:**
```json
{ "type": "list", "list": "turnout" }
```
returns `{ "type": "list", "data": [...], "method": "list" }`.

## State translation

JMRI's JSON wire format encodes `UNKNOWN` as `0`, while
`jmri_core.state` keeps the internal `NamedBean.UNKNOWN = 1` to match
JMRI's Java integer constants. The `jmri_server.wire_states` module
isolates this translation so service code reads/writes the wire form
unambiguously.

## Running the server

```powershell
# Dev: hot-reload, single InternalConnection.
uvicorn jmri_server:create_app --factory --reload --host 0.0.0.0 --port 12080

# With a custom connection (e.g. a real LocoNet driver once Stage 4 lands):
python -c "
from jmri_server.app import build_app
import uvicorn
# build_app(connection=…) takes a SystemConnection subclass.
handle = build_app()
uvicorn.run(handle.app, host='0.0.0.0', port=12080)
"
```

## What's intentionally deferred

| To | What |
|---|---|
| Stage 11 | Full layout-editor port -- the ``panel`` service lists panels and serves their XML verbatim; a client that wants to render them still has to parse the layout XML themselves. ``layoutBlock`` and ``oblock`` services round-trip the beans but their rich semantics (occupancy propagation, warrants, portals, paths) land with the Panel Editor port. |

## Layout

```
jmri-server/
├── pyproject.toml
├── README.md
├── src/jmri_server/
│   ├── __init__.py
│   ├── app.py                 FastAPI app factory + lifespan
│   ├── context.py             ServerContext + ServerMetadata
│   ├── envelope.py            JmriRequest / JmriResponse Pydantic models
│   ├── push.py                PushManager — WebSocket fan-out + listener wiring
│   ├── registry.py            ServiceRegistry / dispatch
│   ├── routes.py              HTTP POST /json + WS /json
│   ├── service.py             JsonService base class + JsonServiceError
│   ├── wire_states.py         Internal ↔ wire state translation
│   └── services/
│       ├── __init__.py        register_all() + topic list
│       ├── meta.py            hello / ping / version / railroad / node / power
│       ├── beans.py           turnout / sensor / light / signal*/ memory / …
│       └── roster.py          roster (list-only for Stage 3)
└── tests/
    ├── conftest.py            TestClient + schema fixtures
    ├── unit/test_envelope.py
    └── integration/
        ├── test_meta_services.py
        ├── test_bean_services.py    # parametrized over 9 bean topics
        └── test_websocket.py        # connect, dispatch, push, batched req
```

## Stage 4 entry conditions — all green

- [x] FastAPI server passes JMRI JSON schemas for 14 implemented topics
- [x] WebSocket push delivers state changes to every connected client
- [x] HTTP + WebSocket share one dispatcher
- [x] All gates green (ruff, mypy --strict, 88% coverage, 56 tests passing)
- [x] Stage 0 conformance: 187 server-response validations pass
- [x] No hardware required to run or test
