# Dispatcher Pro Migration — Design Spec

**Status:** Deferred. Not scheduled. Captured here for reference so work
can start cold whenever a user requests it.

**Source material:** `C:\Users\jpetr\Documents\jmri.jar.src\jmri\jmrit\dispatcher\`
(21 Java files) plus `apps\DispatcherPro\DispatcherPro.java` and
`jmri\jmrit\logixng\util\DispatcherActiveTrainManager.java`.

**Decisions locked in** (see "Scoping decisions" below for each):

| # | Question | Decision |
|---|----------|----------|
| 1 | AutoActiveTrain in initial port? | **No — phase 3.** |
| 2 | Signal types in phase 1? | **`SECTIONSALLOCATED` + `SIGNALHEAD` only.** `SIGNALMAST` deferred with AutoActiveTrain. |
| 3 | Ad-hoc routing in phase 1? | **No. Users hand-build Transits.** |
| 4 | Operations ↔ Dispatcher integration depth? | **Wait for the Operations enhancement spec.** |
| 5 | Dispatcher instances per profile? | **Single instance per profile.** |
| 6 | AutoAllocate thread model? | **Port Java's `Thread` + blocking queue as `asyncio.Task` + `asyncio.Queue`.** |
| 7 | TrainInfo file layout? | **Single `dispatcher.xml` per profile** (not per-train XMLs). |
| 8 | LayoutEditor panel paint of allocated Sections? | **Deferred.** |

---

## 1. What Dispatcher Pro is

The dispatcher desk that runs scheduled trains across the layout **one
`Section` at a time**. It sits between Operations Pro (which decides
*what* trains exist and *where they go*) and the live hardware
(blocks / sensors / signals, telling you *where the train actually
is*).

Two coexisting modes in the original:

- **Manual** — operator clicks Allocate on each pending request; the
  human drives the throttle. Dispatcher enforces turnout alignment,
  stop-signal hold, and Section hand-off as the train advances.
- **AutoActiveTrain** — JMRI drives a throttle directly, watches
  SignalMast aspects, ramps speed to profile, and terminates at end
  of transit.

Phase 1 ports **manual mode only**. AutoActiveTrain is phase 3.

### Primary actor flow (manual mode)

1. User defines Sections (ordered Blocks with EntryPoints) and
   Transits (ordered list of Sections).
2. User creates an `ActiveTrain` from a TrainInfo (or roster entry +
   Transit).
3. Dispatcher allocates the starting Section; the train is "on the
   railroad".
4. As the train's lead Block occupies the next Section's forward-entry
   Block, Dispatcher raises an `AllocationRequest` for the next
   Section in the Transit.
5. Operator clicks **Allocate**. Dispatcher aligns turnouts, allocates
   the Section, releases the section behind (based on train length +
   trailing Section's release policy).
6. Signals held at the far end of each un-allocated Section stop the
   train.
7. At end of Transit the train is **terminated**; everything is
   released.

---

## 2. Hard prerequisites not yet in the port

Dispatcher is built on bean types we haven't ported:

| Concept | Where it lives in JMRI | Status |
|---------|------------------------|--------|
| **Section** (`Section`, `SectionManager`) | `jmri.Section`, `jmri.managers.DefaultSectionManager` | **Missing** |
| **Transit** (`Transit`, `TransitManager`, `TransitSection`, `TransitSectionAction`) | `jmri.Transit*` | **Missing** |
| **EntryPoint** | `jmri.EntryPoint` (lives on Section) | **Missing** |
| **SignalMastLogic** | `jmri.SignalMastLogic*` | **Missing** (only needed for AutoActiveTrain) |

What IS already in the port and can be relied on:

- `Block` / `BlockManager` (occupancy tracking)
- `LayoutBlock` (adjacency graph)
- `SignalHead` / `SignalHeadManager` (Red/Green/Yellow aspects)
- `SignalMast` (bean exists; aspect-to-speed mapping does not)
- `Turnout` / `TurnoutManager` (align via SET)
- `Sensor` / `SensorManager`
- `Throttle` / `ThrottleManager` (needed for AutoActiveTrain, not manual)
- `FastClock` (needed for scheduled starts)
- `Signal[T]` (event bus — AutoAllocate's queue model maps cleanly to this)

### 2.1 `Section`

**JMRI model.** An ordered sequence of Blocks that acts as the atomic
allocation unit. Each Section has:

- An **ordered Block list** — what the train traverses.
- **Forward EntryPoints** and **Reverse EntryPoints** — which
  neighbouring Blocks lead into which end.
- Two **directional modes**: `FORWARD` and `REVERSE`. A Section's
  "forward" is defined by its Block order.
- An **occupation state**: `UNOCCUPIED` / `OCCUPIED`.
- Optional **stopping sensors** (for AutoActiveTrain to detect "train
  has passed the stop line").
- Optional **forward/reverse SignalMasts** at each end.

**Port shape.**

```python
# pmri_core/beans/section.py
class Section(NamedBean):
    user_name: str
    forward_blocks: list[Block]        # the ordered traversal
    forward_entry_points: list[EntryPoint]
    reverse_entry_points: list[EntryPoint]
    forward_stopping_sensor: Sensor | None
    reverse_stopping_sensor: Sensor | None
    forward_signal_head: SignalHead | None    # phase 1
    reverse_signal_head: SignalHead | None
    forward_signal_mast: SignalMast | None    # phase 3
    reverse_signal_mast: SignalMast | None

    @property
    def state(self) -> Literal["UNOCCUPIED", "OCCUPIED"]: ...
    # emits occupancy_changed: Signal[SectionState] fed by Block occupancy
```

```python
class EntryPoint:
    from_block: Block                  # block outside the Section
    to_block:   Block                  # block inside the Section
    direction:  Literal["FORWARD", "REVERSE", "UNKNOWN"]
    fixed:      bool                   # unchangeable by user
```

```python
class SectionManager(Manager[Section]):
    def provide(self, system_name: str) -> Section: ...
    def iter(self) -> Iterable[Section]: ...
    def get(self, system_name: str) -> Section | None: ...
    # Standard bean events on sections_changed: Signal[SectionChange]
```

### 2.2 `Transit`

**JMRI model.** An ordered list of Sections (plus per-section
direction) describing a train's route from origin to destination.
Also carries optional TransitSectionActions (fire-when-entering,
set-sensor, etc. — **out of scope for phase 1**).

**Port shape.**

```python
# pmri_core/beans/transit.py
class Transit(NamedBean):
    user_name: str
    sections: list[TransitSection]       # ordered

    def first(self) -> TransitSection: ...
    def last(self) -> TransitSection: ...
    def contains(self, sec: Section) -> bool: ...
    def index_of(self, sec: Section) -> int: ...

class TransitSection:
    section: Section
    direction: Literal["FORWARD", "REVERSE"]
    sequence_number: int
    actions: list[TransitSectionAction] = []    # phase 3+
```

```python
class TransitManager(Manager[Transit]):
    def provide(self, system_name: str) -> Transit: ...
    def iter(self) -> Iterable[Transit]: ...
    # transits_changed: Signal[TransitChange]
```

### 2.3 EntryPoint discovery

JMRI's EntryPoint auto-builder walks `LayoutBlock` adjacency. **Phase
1 skips the auto-builder** — EntryPoints are specified explicitly
when a Section is created. This is consistent with "hand-build
Transits".

### 2.4 Persistence

Sections and Transits live in `PanelPro.xml` alongside every other
bean, via two new `*ManagerXml` codecs:

```
pmri_core/persistence/xml/section_xml.py
pmri_core/persistence/xml/transit_xml.py
```

Schema-valid against the JMRI `layout-section-2-9-6.xsd` and
`layout-transit-2-9-6.xsd` so hand-edited JMRI panels round-trip.

---

## 3. Dispatcher domain model

New top-level package **`pmri_dispatcher`** (peer of
`pmri_decoderpro`, `pmri_withrottle`).

### 3.1 Package layout

```
pmri-dispatcher/
└─ src/pmri_dispatcher/
   ├─ __init__.py
   ├─ models/
   │  ├─ train_info.py        # TrainInfo + XML codec
   │  ├─ active_train.py      # ActiveTrain state machine
   │  ├─ allocated_section.py # AllocatedSection record
   │  ├─ allocation_request.py
   │  └─ enums.py             # modes, signal-type, release-policy
   ├─ session.py              # DispatcherSession singleton
   ├─ allocator.py            # manual-mode allocator
   ├─ auto_allocator.py       # phase 2 — AutoAllocate loop
   ├─ auto_turnouts.py        # turnout alignment for an AllocatedSection
   ├─ signal_control.py       # phase 1: SIGNALHEAD + SECTIONSALLOCATED
   ├─ persistence/
   │  ├─ dispatcher_xml.py    # dispatcher.xml codec
   │  └─ paths.py             # <profile>/dispatcher.xml
   ├─ sources/
   │  ├─ roster_source.py     # roster → ActiveTrain seed
   │  └─ user_source.py       # UI-entered trains
   ├─ auto_run/               # phase 3 — AutoActiveTrain
   │  └─ ...
   └─ service.py              # pmri_server wiring + topic handlers
```

### 3.2 `TrainInfo`

Serialisable description of what to run. Pydantic model with camelCase
aliases matching JMRI's `TrainInfo.java` field names.

```python
class TrainInfo(BaseModel):
    transit_name: str
    train_name: str
    dcc_address: int | None
    roster_entry_id: str | None
    train_type: str = "LOCAL_FREIGHT"    # for UI colour
    train_from_roster: bool = True
    train_from_trains: bool = False      # operations integration — phase 4
    train_from_user: bool = False
    priority: int = 5
    allocation_method: Literal["MANUAL", "AUTO"] = "MANUAL"
    delayed_start: Literal["NONE", "TIMED", "SENSOR"] = "NONE"
    departure_time_hr: int | None = None
    departure_time_min: int | None = None
    delay_sensor_name: str | None = None
    reset_when_done: bool = False
    terminate_when_done: bool = True
    allocate_all_the_way: bool = False
    # — AutoActiveTrain fields (phase 3) kept in model, ignored in phase 1 —
    auto_run: bool = False
    speed_factor: float = 1.0
    max_speed: float = 0.6
    ramp_rate: Literal["RAMP_NONE","RAMP_FAST","RAMP_MEDIUM","RAMP_SLOW"] = "RAMP_NONE"
    resistance_wheels: bool = True
    run_in_reverse: bool = False
    sound_decoder: bool = False
    max_train_length: float = 100.0
    use_speed_profile: bool = False
    stop_by_speed_profile: bool = False
    stop_by_speed_profile_adjust: float = 1.0
```

Phase 1 only honours the manual-mode fields. The AutoActiveTrain
fields are present so the XML schema is stable across phases.

### 3.3 `ActiveTrain`

State machine for a train currently on the railroad.

```python
class ActiveTrainStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING = "WAITING"             # held for signal/section
    WORKING = "WORKING"             # operations-driven stop (phase 4)
    READY = "READY"                 # pre-start, delayed
    STOPPED = "STOPPED"             # terminated
    DONE = "DONE"

class ActiveTrainMode(StrEnum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"                   # phase 3
    DISPATCHED = "DISPATCHED"       # paused by dispatcher button

class ActiveTrain:
    id: str                         # dispatcher-assigned, e.g. "AT-0007"
    train_info: TrainInfo
    transit: Transit
    start_block: Block
    end_block: Block
    status: ActiveTrainStatus
    mode: ActiveTrainMode
    allocated_sections: list[AllocatedSection]      # ordered, oldest-first
    last_allocated_section: AllocatedSection | None
    next_section_seq_number: int                    # position in Transit
    priority: int
    restart_sensor: Sensor | None
    delay_sensor: Sensor | None
    delay_start_time: datetime | None

    # signals
    status_changed: Signal[ActiveTrainStatus]
    section_allocated: Signal[AllocatedSection]
    section_released: Signal[AllocatedSection]
```

**Lifecycle** (phase 1):

```
   create ──► READY ──► RUNNING ──► WAITING ──► RUNNING ... ──► DONE
                 │                       │
                 └──► PAUSED ◄───────────┘
```

Transitions:

- `create_active_train(train_info)` → READY.
- `start()` → RUNNING, allocates starting Section.
- Block-occupancy change → emits `AllocationRequest` for next section
  when the lead block hits a specific trigger block in the current
  AllocatedSection.
- Operator Allocate → next section becomes `AllocatedSection`; may
  trigger a release at the tail.
- Last section allocated + train has reached its end-block → `DONE`.
  If `reset_when_done`, flip direction and start over; if
  `terminate_when_done`, release everything.

### 3.4 `AllocatedSection`

```python
class AllocatedSection:
    section: Section
    direction: Literal["FORWARD", "REVERSE"]
    active_train: ActiveTrain
    sequence_number: int                 # position in the Transit
    allocated_at: datetime
    released: bool = False
    # phase 1: used by signal_control to hold the opposing head red
    # phase 3: used by AutoActiveTrain to pick next stopping sensor
```

### 3.5 `AllocationRequest`

Pending request for the allocator to decide on.

```python
class AllocationRequest:
    active_train: ActiveTrain
    section: Section
    direction: Literal["FORWARD", "REVERSE"]
    sequence_number: int
    created_at: datetime
    priority: int                        # inherited from the ActiveTrain
```

Requests live on the `DispatcherSession`'s request queue (dict +
ordered-key, deterministic). Manual mode: the UI lists them and
offers Allocate. Auto mode (phase 2): the AutoAllocator loop
drains them.

### 3.6 `DispatcherSession` (singleton per profile)

```python
class DispatcherSession:
    profile: Profile
    active_trains: list[ActiveTrain]
    allocated_sections: list[AllocatedSection]    # all-trains, newest-first
    pending_requests: list[AllocationRequest]
    train_info_library: dict[str, TrainInfo]      # keyed by train_name

    signal_type: Literal["SECTIONSALLOCATED", "SIGNALHEAD"] = "SECTIONSALLOCATED"
    allocation_method: Literal["MANUAL", "AUTO"] = "MANUAL"
    auto_allocate: bool = False                   # phase 2 toggle
    use_connectivity: bool = False                # phase 1: False, means "hand-built transits"
    auto_turnouts: bool = True                    # align turnouts on allocation

    # events
    active_trains_changed: Signal[ActiveTrain]
    allocations_changed: Signal[AllocatedSection]
    requests_changed: Signal[AllocationRequest]

    # commands (API surface)
    def create_active_train(self, train_info: TrainInfo) -> ActiveTrain: ...
    def start_active_train(self, at_id: str) -> None: ...
    def pause_active_train(self, at_id: str) -> None: ...
    def resume_active_train(self, at_id: str) -> None: ...
    def terminate_active_train(self, at_id: str) -> None: ...
    def allocate(self, request_id: str) -> AllocatedSection: ...
    def release(self, allocated_id: str) -> None: ...
    def extend_allocation(self, at_id: str) -> None: ...
```

**Single instance per profile** — the session is created when the
profile is loaded and destroyed when the profile is unloaded.
Attempting to instantiate a second time raises.

---

## 4. Allocator (phase 1 — manual)

`pmri_dispatcher/allocator.py`. Pure function style: given the
session state, request, and current occupancy, produce an
`AllocatedSection` or a refusal reason.

```python
def allocate(session: DispatcherSession, req: AllocationRequest) -> AllocationResult:
    # 1. Section must be in the ActiveTrain's Transit, at the expected seq
    # 2. Section must be UNOCCUPIED (unless it's the ActiveTrain's starting section)
    # 3. Section must not already be AllocatedSection for another train
    # 4. Opposing entry points must not be in-use by a train moving opposite
    # 5. If auto_turnouts: align every turnout between from-EntryPoint and the
    #    Section's exit block (phase 1: along the forward_blocks path only)
    # 6. Create AllocatedSection, append to session.allocated_sections
    # 7. Update ActiveTrain.last_allocated_section + next_section_seq_number
    # 8. Signal section_allocated
    # 9. If any tail sections can now be released, release them
    # 10. Update signal_control
```

Release policy (phase 1 — simple):

- When the train's lead block is in Section N+1, release Section N-1
  (keep the trailing-but-adjacent section until the whole train has
  cleared, approximated as "lead block is 2 sections ahead of the
  tail allocation").
- A hold-the-section-for-caboose flag on ActiveTrain can defer
  release by one more block.

### 4.1 Auto-turnouts

`pmri_dispatcher/auto_turnouts.py`. Walks the `forward_blocks` of the
Section being allocated (or reverse, for reverse direction) and, for
each pair of adjacent LayoutBlocks that share a Turnout, calls
`turnout.set_state(CLOSED | THROWN)` to route through. **No
occupancy checks on individual blocks within the Section** — that's
Section-level by construction.

### 4.2 Signal control (phase 1: SECTIONSALLOCATED + SIGNALHEAD)

`pmri_dispatcher/signal_control.py`.

- **SECTIONSALLOCATED mode:** for every Section that is not currently
  allocated to a running ActiveTrain, hold its forward_signal_head
  and reverse_signal_head at `RED`. When a Section is allocated,
  clear the head at its entry end to `GREEN` (or `YELLOW` if the
  next Section ahead is not allocated).
- **SIGNALHEAD mode:** same behaviour but respects the SignalHead's
  existing appearance logic — Dispatcher only *holds* red, it never
  forces green over a user-defined red.
- **SIGNALMAST mode: deferred.** Rejected at the API layer with 400
  in phase 1.

---

## 5. AutoAllocate (phase 2)

`pmri_dispatcher/auto_allocator.py`. Backgrounded `asyncio.Task` that
drains the request queue when `session.auto_allocate` is `True`.

**Thread-model mapping from Java.** The Java code uses a dedicated
`Thread` with a blocking `LinkedBlockingQueue<AllocationRequest>`.
Port it as:

```python
class AutoAllocator:
    def __init__(self, session: DispatcherSession):
        self.session = session
        self._queue: asyncio.Queue[AllocationRequest] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _loop(self) -> None:
        while True:
            req = await self._queue.get()
            # evaluate same safety checks as the manual allocator
            # if ok: allocate
            # if blocked by conflicting ActiveTrain: requeue with backoff
            # yield to let occupancy events settle

    def submit(self, req: AllocationRequest) -> None:
        self._queue.put_nowait(req)
```

The session wires `requests_changed` → `AutoAllocator.submit` when
`auto_allocate` is enabled.

Fairness / priority: in Java it's a simple priority scan of the queue
each wakeup. Replicate: on every `_loop` turn, drain the queue into a
list, sort by (priority desc, created_at asc), process one, re-emit
unprocessed ones.

---

## 6. Persistence — `dispatcher.xml`

**One file per profile**, at
`<settings-root>/<profile>/dispatcher.xml`. Loaded at profile-open,
saved on every mutation (atomic write, same pattern as
`PanelPro.xml`).

Schema (version 1.0):

```xml
<dispatcher version="1">
  <options
      signal-type="SECTIONSALLOCATED"
      allocation-method="MANUAL"
      auto-allocate="false"
      use-connectivity="false"
      auto-turnouts="true"
      train-info-file-history-size="10"/>

  <train-infos>
    <train-info
        train-name="Westbound Local"
        transit-name="IT:WB-LOCAL"
        dcc-address="4703"
        roster-entry-id="RGS-20"
        priority="5"
        allocation-method="MANUAL"
        ...>
      <!-- camelCase attrs matching JMRI TrainInfo -->
    </train-info>
    ...
  </train-infos>

  <active-trains>
    <!-- active trains are NOT persisted on clean shutdown by default
         (matches JMRI). Persist only if `ResumeAutomaticTrains` set. -->
  </active-trains>
</dispatcher>
```

ActiveTrains are not persisted by default. The user always re-creates
them on startup from a saved TrainInfo. (JMRI parity, and simpler.)

---

## 7. Server wiring & JSON API

### 7.1 Service registration

In `pmri_server`:

```python
# pmri_server/services/dispatcher_service.py
class DispatcherService:
    def __init__(self, profile: Profile):
        self.session = DispatcherSession(profile)
        self._codec = DispatcherXmlCodec(profile.root / "dispatcher.xml")

    async def start(self) -> None:
        await self._codec.load_into(self.session)
        # subscribe to Block occupancy signals from the active SystemConnection
        # ...

    async def stop(self) -> None:
        await self._codec.save(self.session)
```

Registered in the service registry at profile-open; destroyed at
profile-close.

### 7.2 JSON topics

Topics follow the existing `operations-*` / `roster-*` idiom:

| Topic | Payload |
|-------|---------|
| `dispatcher-options` | GET → options block; POST → update |
| `dispatcher-train-info` | GET list / create / update / delete TrainInfo entries |
| `dispatcher-active-train` | GET list / create / terminate ActiveTrains |
| `dispatcher-allocated-section` | GET list; stream via WS on `allocations_changed` |
| `dispatcher-allocation-request` | GET pending; POST `{allocate: id}` or `{reject: id}` |
| `dispatcher-section` | CRUD on Section beans (lives on pmri-core bean topics; listed here for completeness) |
| `dispatcher-transit` | CRUD on Transit beans |

WebSocket pushes: `dispatcher-options`, `dispatcher-active-train`,
`dispatcher-allocated-section`, `dispatcher-allocation-request` each
mirror their `Signal[T]` events to connected clients.

---

## 8. Web UI (phase 1)

New route: **`/dispatcher`**.

Single page, four live tables (htmx-driven, mirrored from WS events):

1. **Options bar** — signal type radio, allocation method toggle,
   auto-turnouts checkbox.
2. **Active Trains** — Train | Transit | Status | Mode | Priority |
   Last allocated | Buttons: Pause / Resume / Terminate / Allocate next.
3. **Allocation Requests** — Train | Section | Direction | Priority |
   Age | Button: Allocate.
4. **Allocated Sections** — Section | Train | Direction | Allocated
   at | Button: Release.

Secondary routes:

- `/dispatcher/train-infos` — CRUD list for TrainInfo library.
- `/dispatcher/train-infos/new` — form that picks a Transit,
  optional roster entry or DCC address, priority, delay-start
  options.
- `/dispatcher/new-active-train` — "run this TrainInfo now" page.

Sections and Transits get their own CRUD pages under a new top-level
**Beans** area (or add to the existing bean admin), independent of
Dispatcher.

---

## 9. LogixNG hook

Port `jmri.jmrit.logixng.util.DispatcherActiveTrainManager` as a new
LogixNG action node:

```yaml
- action:
    type: dispatcher-active-train
    verb: create | terminate | allocate-next | pause | resume
    train_name: "Westbound Local"     # resolves against TrainInfo library
```

This lands in phase 3 alongside AutoActiveTrain (the two share most
plumbing), but can land earlier if a user needs it.

---

## 10. AutoActiveTrain (phase 3) — sketch only

Deferred. When started, add:

- `pmri_dispatcher/auto_run/auto_active_train.py` — per-ActiveTrain
  task that owns a throttle, drives speed, tracks the signal-ahead
  logic.
- `pmri_dispatcher/auto_run/auto_trains_frame.py` — "Automatic Trains"
  live table.
- SignalMastLogic subsystem under `pmri_core/beans/signal_mast_logic/`
  (new prerequisite track).
- SpeedProfile reader (already partially in `pmri_core` for roster).
- Stop-by-speed-profile math, ramp rates, reverse running.

---

## 11. Operations ↔ Dispatcher integration (phase 4)

**Blocked on the upcoming Operations enhancement spec.** When that
spec is written, this section fills in with:

- Which Operations train corresponds to which Dispatcher TrainInfo.
- Whether a TrainInfo can be auto-created from an Operations train's
  route + schedule.
- How manifest switch-list stops become TransitSectionActions
  ("hold here until `station-done` sensor goes active").
- Whether terminating an ActiveTrain marks the Operations train as
  "run" automatically.

No code lands for this in phases 1-3.

---

## 12. Testing strategy

Per-layer unit tests in each package, following existing conventions:

- `pmri-core/tests/beans/test_section.py` — ordered blocks, entry
  point lookup, occupancy derived from Block signals, round-trip
  through section_xml.
- `pmri-core/tests/beans/test_transit.py` — ordering, index-of,
  contains, round-trip.
- `pmri-dispatcher/tests/test_allocator.py` — parametric allocate /
  reject scenarios, release policy edge cases.
- `pmri-dispatcher/tests/test_auto_turnouts.py` — walks a fixture
  Section and asserts correct turnout SETs.
- `pmri-dispatcher/tests/test_signal_control.py` — SECTIONSALLOCATED
  and SIGNALHEAD behaviour on a fixture layout.
- `pmri-dispatcher/tests/test_session.py` — full lifecycle: create
  TrainInfo → create ActiveTrain → start → simulate occupancy →
  allocate requests → terminate.
- `pmri-dispatcher/tests/test_dispatcher_xml.py` — round-trip
  dispatcher.xml with options + TrainInfo library.
- Integration tests in `pmri-web/tests/integration/`:
  `test_dispatcher_pages.py` — the four live tables render, HTMX
  partial swaps on WS events, Allocate / Terminate buttons POST and
  redirect.

---

## 13. Work breakdown

Sizes: **S** = half-day, **M** = 1-2 days, **L** = 3-5 days, **XL** = 1-2 weeks.

### Phase 1 — Manual dispatcher (locked-in scope)

| ID | Task | Size | Depends on |
|----|------|------|------------|
| A1 | `Section` + `SectionManager` + XML codec | L | — |
| A2 | `Transit` + `TransitManager` + `TransitSection` + XML codec | M | A1 |
| A3 | Section & Transit CRUD JSON topics + web pages | M | A1, A2 |
| B1 | `TrainInfo` Pydantic model + camelCase aliases | S | — |
| B2 | `dispatcher.xml` codec | M | B1 |
| C1 | `DispatcherSession` singleton + lifecycle | M | A2 |
| C2 | `ActiveTrain` state machine (manual mode) | L | C1 |
| C3 | `AllocatedSection` + `AllocationRequest` records | S | C1 |
| C4 | Manual allocator (`allocator.py`) | L | C2, C3 |
| C5 | Release policy (phase 1 — simple) | M | C4 |
| D1 | `DispatcherService` wiring in `pmri_server` | M | C1 |
| D2 | JSON topics for options / active-trains / requests / allocations | M | D1 |
| D3 | WS push plumbing for the four tables | S | D2 |
| D4 | `/dispatcher` web page — four live tables | L | D2 |
| D5 | TrainInfo library CRUD pages | M | D2 |
| D6 | New-active-train form | S | D5 |
| E1 | `auto_turnouts.py` | M | C4 |
| E2 | `signal_control.py` — SECTIONSALLOCATED + SIGNALHEAD | M | C4 |
| E3 | End-to-end integration test on a fixture layout | M | D4, E1, E2 |

Rough phase-1 estimate: ~4-5 weeks of focused work.

### Phase 2 — AutoAllocate

| F1 | `AutoAllocator` asyncio task + queue | L |
| F2 | Options wiring + UI toggle | S |
| F3 | Priority scan + fairness tests | M |
| F4 | Backoff / requeue on conflicts | M |

### Phase 3 — AutoActiveTrain + LogixNG

| G1 | `SignalMastLogic` subsystem (prerequisite) | XL |
| G2 | `AutoActiveTrain` task + throttle wiring | XL |
| G3 | Speed profile / ramp-rate logic | L |
| G4 | Automatic Trains table in web UI | M |
| G5 | SIGNALMAST signal-control mode | M |
| H1 | LogixNG `dispatcher-active-train` action | S |

### Phase 4 — Operations integration

Blocked on the Operations enhancement spec. Shape fills in later.

---

## 14. Scoping decisions — the "why"

**#1 AutoActiveTrain deferred.** Phase 1 unblocks the dispatcher
desk for human operators — the use case most layouts actually need.
AutoActiveTrain is the largest chunk by far (XL + SignalMastLogic
prerequisite); gating phase 1 on it would multiply time-to-value
with no gain for the manual dispatcher's users.

**#2 SECTIONSALLOCATED + SIGNALHEAD only.** These are the two modes
every manual-mode JMRI dispatcher install uses. SIGNALMAST adds the
whole signal-mast-logic layer (aspect decoding, speed tables, logic
expressions) which is really an AutoActiveTrain prerequisite.

**#3 Hand-built Transits.** Punts on porting
`LayoutBlock.getLayoutBlocks()` + `Section.checkValidDest()` graph
walks. Those matter when the UI offers "pick origin and
destination, auto-build a Transit"; phase 1 ships without that
button. Users define Transits explicitly, same as users already do
for routes in Operations.

**#4 Wait for Operations spec.** No TransitSection ↔ RouteStop
mapping exists in the current Operations model; inventing it now
would freeze decisions the enhancement spec needs to make freely.

**#5 Single instance per profile.** Matches JMRI. A multi-user
dispatcher is interesting but requires a conflict-resolution
protocol (two dispatchers click Allocate on the same request) which
isn't in scope.

**#6 Port the Java thread + queue.** The Java design is correct —
blocking queue is a natural event loop. `asyncio.Queue` + a single
task is the one-to-one port. No need to re-architect.

**#7 One `dispatcher.xml` per profile.** Rest of the port follows
the "one XML per feature per profile" pattern (`PanelPro.xml`,
`roster.xml`, `logixng.yaml`). JMRI's per-TrainInfo file layout
exists because it was the easiest thing to add to Java's FileChooser
UI; it has no advantage server-side.

**#8 Panel-paint deferred.** The web UI has no LayoutEditor panel
paint hook yet. The dispatcher's four tables provide the live
information that paint provides in JMRI; adding coloured block
fills to a panel editor is a separate project.

---

## 15. Open items for the next pass

- Confirm Section's forward/reverse directional semantics match our
  LayoutBlock graph direction (JMRI's Section has a `direction` enum
  **separate from** block ordering — needs a small test fixture to
  verify round-trip).
- Decide whether `Transit` CRUD belongs in a new Beans admin area
  or under `/dispatcher`. Phase 1 can go either way; current lean is
  Beans admin, since Section/Transit are useful outside Dispatcher
  (block-boundary visualisation, LogixNG conditions).
- Decide the exact release-policy knob set. JMRI has 4 knobs
  (`reset_when_done`, `terminate_when_done`, `allocate_all_the_way`,
  `train_detection`); phase 1 may only need 2.
