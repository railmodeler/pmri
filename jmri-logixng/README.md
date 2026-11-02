# jmri-logixng — Stage 7 of the JMRI Python rewrite

Tree-and-interpreter implementation of JMRI's LogixNG automation engine.
Replaces ~700 Java classes in the original `jmri.jmrit.logixng.*` package
hierarchy with a lean Python core: visitor-based execution, async-first,
YAML-driven authoring, type-tagged serialization.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **160 / 160** passing |
| Coverage | exceeds 85% target |

## The model

A LogixNG program is a tree of three node kinds:

* **Action** — does something (mutates layout state). Examples:
  `SetTurnoutState`, `SetLightState`, `SetMemory`, `Delay`, `LogAction`.
* **Expression[T]** — evaluates to a value. The common variant is
  `Expression[bool]` (a "digital expression"). Examples:
  `SensorIsActive`, `TurnoutIsClosed`, `And`/`Or`/`Not`.
* **ConditionalNG / LogixNG** — containers. `ConditionalNG` is a
  sequence of root actions; `LogixNG` groups conditionals.

Triggers wire conditionals to bean state changes via the
`AutomationEngine`: when a watched sensor/turnout/memory changes, an
optional guard expression is evaluated and the conditional runs if the
guard returns true.

## Actions

| Category | Type tag | Class | What it does |
|---|---|---|---|
| digital | `action:set-turnout` | `SetTurnoutState` | Set turnout commanded state |
| digital | `action:set-sensor` | `SetSensorState` | Set sensor known state (for simulation) |
| digital | `action:set-light` | `SetLightState` | Set light state |
| digital | `action:set-memory` | `SetMemory` | Write a value into a memory |
| digital | `action:set-memory-from-expression` | `SetMemoryFromExpression` | Evaluate an expression; write result into memory |
| digital | `action:if-then-else` | `IfThenElse` | Conditional execution |
| digital | `action:log` | `LogAction` | Emit a log message |
| flow | `action:sequence` | `Sequence` | Run a list of actions in order |
| flow | `action:delay` | `Delay` | Sleep for *N* seconds |
| script | `action:script` | `ScriptAction` | Run an inline async Python snippet (globals: `ctx`, `bean(..)`, `asyncio`, `TurnoutState`, …) |
| cv | `action:write-cv` | `WriteCV` | Write one CV via the programmer service (service- or ops-mode, logs on failure) |
| cv | `action:read-cv` | `ReadCV` | Read one CV; optional `into_memory` bean target |

## Expressions

| Category | Type tag | Class | Returns |
|---|---|---|---|
| digital | `expr:const-bool` | `ConstantBool` | Fixed boolean |
| digital | `expr:sensor-active` | `SensorIsActive` | true ↔ sensor ACTIVE |
| digital | `expr:turnout-closed` | `TurnoutIsClosed` | true ↔ turnout known CLOSED |
| digital | `expr:light-on` | `LightIsOn` | true ↔ light ON |
| digital | `expr:memory-equals` | `MemoryEquals` | true ↔ memory == value |
| digital | `expr:and` / `expr:or` / `expr:not` | `And` / `Or` / `Not` | Boolean algebra (short-circuits) |
| analog | `expr:const-number` | `ConstantNumber` | Fixed `float` |
| analog | `expr:memory-value-number` | `MemoryValueNumber` | Memory bean's value coerced to `float` |
| analog | `expr:numeric-op` | `NumericOp` | Binary op (`+ - * / min max`) over two analog children |
| analog | `expr:numeric-compare` | `NumericCompare` | Analog comparison (`< ≤ = ≥ > ≠`) → `bool` |
| string | `expr:const-string` | `ConstantString` | Fixed `str` |
| string | `expr:memory-value-string` | `MemoryValueString` | Memory bean's value stringified |
| string | `expr:string-concat` | `StringConcat` | Concatenate child strings with a separator |
| string | `expr:string-compare` | `StringCompare` | Comparisons (`eq ne starts-with ends-with contains regex`) → `bool` |

## On-disk format

Each node serializes to a dict with a ``type`` tag matched against the
type registry. Example (`examples/station-light.yaml`):

```yaml
type: logixng:root
user_name: Station platform light
conditionals:
  - type: logixng:conditional
    actions:
      - type: action:if-then-else
        condition:
          type: expr:and
          children:
            - type: expr:sensor-active
              sensor: IS1
            - type: expr:turnout-closed
              turnout: IT1
        then:
          type: action:set-light
          light: IL1
          state: ON
        else:
          type: action:set-light
          light: IL1
          state: OFF
```

Round-trip: ``save_yaml(tree, path)`` ↔ ``load_yaml(path)``. JSON via
``to_json`` / ``from_json``. The serializer uses explicit per-class
decoders rather than reflection, so children with rich types
(``TurnoutState``, sub-``Action``, sub-``Expression``) reconstruct
cleanly.

## Usage

```python
import asyncio
from jmri_core.hal.internal import InternalConnection
from jmri_logixng import (
    AutomationEngine, ExecutionContext, ConditionalNG, TriggerBinding,
    SensorIsActive, SetLightState, load_yaml,
)
from jmri_core.state import LightState

async def main():
    conn = InternalConnection()
    await conn.open({})
    # Pre-create the beans the YAML references.
    await conn.sensors.provide("IS1")
    await conn.lights.provide("IL1")

    # Load the automation tree.
    tree = load_yaml("examples/station-light.yaml")

    # Wire its first conditional as a trigger watching IS1.
    ctx = ExecutionContext(connection=conn)
    engine = AutomationEngine(ctx)
    for c in tree.conditionals:
        engine.add(TriggerBinding(conditional=c, watches=[("sensor", "IS1")]))
    engine.start()

    # ...drive the layout; engine fires conditionals on every IS1 change...

    engine.stop()
    await conn.close()

asyncio.run(main())
```

## JMRI XML importer

`jmri_importer.import_jmri_logixng_xml(path)` reads a JMRI
PanelPro.xml (or standalone LogixNG XML) and reifies its flat forest
into our nested tree. JMRI's XML persists LogixNGs, ConditionalNGs,
DigitalActions, and DigitalExpressions as four manager-scoped lists
cross-referenced by system name; the importer indexes every node once
then walks from each `<LogixNG>` root through its ConditionalNGs,
resolving child sockets by system-name lookup.

Covered JMRI node tags (~20): `ActionTurnout`, `ActionSensor`,
`ActionLight`, `ActionMemory`, `ActionLogItem`, `IfThenElse`,
`DigitalMany`, `DigitalTimer`, `ExpressionSensor`, `ExpressionTurnout`,
`ExpressionLight`, `ExpressionMemory`, `True`, `False`, `And`, `Or`,
`Not`. Unknown tags surface as `Logxxx` log messages so an operator
can see what's missing instead of silently losing nodes.

Round-trip pattern: `import_jmri_logixng_xml(path)` → native tree →
`save_yaml(tree, out_path)`.

## Layout

```
jmri-logixng/
├── pyproject.toml
├── README.md
├── examples/station-light.yaml
├── src/jmri_logixng/
│   ├── __init__.py
│   ├── tree.py              Node / Action / Expression ABCs
│   ├── tree_ops.py          node-id structural edits (slot catalog + splice)
│   ├── context.py           ExecutionContext
│   ├── registry.py          type-tag → class registry
│   ├── logix.py             ConditionalNG + LogixNG
│   ├── manager.py           LogixNG manager (name/profile wiring)
│   ├── triggers.py          AutomationEngine + TriggerBinding
│   ├── serialize.py         YAML/JSON load/save + decoders
│   ├── jmri_importer.py     JMRI PanelPro.xml → native tree
│   ├── actions/
│   │   ├── digital.py       set-turnout/sensor/light/memory + if-then + log + set-memory-from-expr
│   │   ├── flow.py          sequence + delay
│   │   ├── script.py        inline async Python snippet (ScriptAction)
│   │   └── cv.py            WriteCV + ReadCV (via programmer service)
│   └── expressions/
│       ├── digital.py       sensor/turnout/light/memory state + and/or/not + const
│       ├── analog.py        const + memory + numeric-op + numeric-compare
│       └── string.py        const + memory + concat + compare (eq/contains/regex/…)
└── tests/
    ├── unit/                tree, tree-ops, actions, cv/script actions, digital/analog/string expressions, serialization, manager
    └── integration/         trigger end-to-end + JMRI XML importer
```
