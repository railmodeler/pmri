# jmri-scripting — Stage 9 of the JMRI Python rewrite

Native Python scripting host. Replaces JMRI's Jython runtime with
CPython 3.12+, while preserving the script API users actually use:
``sensors.provideSensor("LS5")``, ``turnout.setCommandedState(CLOSED)``,
``class MyScript(AbstractAutomaton): ...``.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `mypy --strict src` | clean |
| `pytest tests/` | **94 / 94** passing |
| Coverage | exceeds 85% target |

## The bridge

JMRI Jython scripts are synchronous; jmri-core is async-native. The
shim resolves this by running every script on a **worker thread** with
sync wrapper methods that bridge to the event loop via
``asyncio.run_coroutine_threadsafe(...).result()``:

```
   script thread                event-loop thread
   ─────────────                ──────────────────
   sensor.setKnownState(ACTIVE) ──→ submit coroutine
   <blocks>                          ↓
                                set_known_state runs
                                ↓
   sensor.setKnownState returns ←── result
```

Scripts feel exactly like their Jython predecessors. No async/await
required. No deadlocks (the script never runs on the event-loop thread).

## Top-N API coverage

From Stage 0's `contracts/script-api.md` — the 200 most-used attribute
paths across 369 Jython scripts. All of the top 50:

| JMRI script API | Shim equivalent |
|---|---|
| `sensors.provideSensor("LS5")` | ✓ (returns `_SensorShim`) |
| `sensor.setKnownState(ACTIVE)` | ✓ |
| `sensor.known_state` / `.isActive` | ✓ |
| `turnouts.provideTurnout("LT1")` | ✓ |
| `turnout.setCommandedState(CLOSED)` | ✓ |
| `lights.provideLight(...)`/`setState(ON)` | ✓ |
| `memories.provideMemory(...)`/`setValue(...)` | ✓ |
| `powermanager.setPower(ON/OFF/IDLE)` | ✓ |
| `class X(AbstractAutomaton): def handle(self)` | ✓ |
| `jmri.InstanceManager.getDefault(...)` | via `shim` accessor |
| `signals.getSignalHead(...)` / `masts.getSignalMast(...)` | ✓ (raw access) |
| Bare constants `ACTIVE`, `CLOSED`, `ON`, `FORWARD` | ✓ |

## Usage

```python
import asyncio
from jmri_core.hal.internal import InternalConnection
from jmri_scripting import ScriptRunner

async def main():
    conn = InternalConnection()
    await conn.open({})
    runner = ScriptRunner.for_connection(asyncio.get_event_loop(), conn)

    # Run an inline script.
    runner.run_source(
        "sensors.provideSensor('IS5').setKnownState(ACTIVE)",
        name="hello",
    )

    # Or load a .py file.
    runner.run_file("automations/yard_sequencer.py")

    # ... let the scripts run ...
    runner.stop_all()
    await conn.close()

asyncio.run(main())
```

## AbstractAutomaton — the classic JMRI script shape

```python
class BlinkLight(AbstractAutomaton):
    def init(self):
        self.light = lights.provideLight("IL5")
        self.state_on = True

    def handle(self):
        self.light.setState(ON if self.state_on else OFF)
        self.state_on = not self.state_on
        import time
        time.sleep(1.0)
        return True   # keep running until stop()

# Just define the class — the runner detects it automatically.
```

The runner finds the `AbstractAutomaton` subclass after exec'ing the
file and calls `.run()` on it, which handles the init → handle-loop →
cleanup lifecycle.

## Hot-reload

```python
from jmri_scripting import ScriptRunner, ScriptWatcher

runner = ScriptRunner.for_connection(loop, conn)
watcher = ScriptWatcher(runner, "scripts/")
await watcher.start()
# Edit any .py file in scripts/ — it re-runs automatically.
```

Poll-based (default 1s interval). No watchdog dependency; stdlib only.

## Jython namespace shim

`jmri_scripting.jython_shim` installs a synthetic `jmri.*` module tree at
import time, so JMRI Jython scripts port with their imports intact:

```python
from jmri import InstanceManager, Path, DccLocoAddress
from jmri.jmrit.automat import AbstractAutomaton
from jmri.util import FileUtil

tm = InstanceManager.getDefault(Turnout)          # -> shim.turnouts
addr = DccLocoAddress(3601, True)                 # long address
path = FileUtil.getExternalFilename("profile:x")  # -> <profile>/x
```

Coverage targets the top-200 attribute paths from
`stage-0/contracts/script-api.md`. Attributes outside that set raise a
clear `AttributeError` naming the shim rather than failing as a bare
`ImportError`. See `jython_shim.py` for the full module tree.

## Explicit-import API

`from jmri_scripting.api import turnouts, powermanager, CLOSED` works
from any helper module, not just the main script body. Each name is a
thread-local proxy that resolves to the currently executing script's
shim on every access — so a helper file imported at module load time
still sees the right managers at call time.

## Captured stdout / stderr

`print()` and raised tracebacks from a script are captured on a
per-script bounded (~64 KB, FIFO-trimmed) buffer and surfaced on
`ScriptStatus.stdout` / `.stderr`. The runner swaps `sys.stdout` /
`sys.stderr` for a `_ThreadDispatchStream` so output from one script
never bleeds into another. Non-script threads still write to the real
`sys.__stdout__`, and the proxy is pytest-safe (re-installs on top of
whatever pytest's capture layer put in place).

## Autoload + persistence

`ScriptRunner.load_directory(path)` runs every `*.py` under *path*,
with three opt-outs:

| Pattern | Meaning |
|---|---|
| `_private.py` | Private — not run, not importable as a top-level module. |
| `lib_helper.py` | Library — not auto-run, but on `sys.path` so other scripts can `import lib_helper`. |
| `# jmri-autoload: false` (in the first 20 lines) | Runnable from the `/scripts` UI's Run button but skipped at server startup. Case-insensitive, whitespace-tolerant on the value (also `no`, `off`, `0`). |

`jmri-server` picks up two directories at startup, in order: the shared
`<settings>/python/` (operator-dropped), then the active profile's
`<profile>/python/`. Scripts saved via `scripts.put {save: true}` land
in the profile dir so they come back on reboot.

## Server + web

* **JSON API** at topic `scripts`: `list` / `get` / `put` / `post`
  for run/stop/save, plus `stdout` / `stderr` in list payloads. See
  `jmri_server.services.scripts`.
* **Web UI** at `/scripts` — inline editor, table of running scripts
  with expandable captured-output sections, per-row stop button. An
  interactive REPL at `/scripts/repl` streams stdout/stderr line-by-line
  inside the executing shim (see `repl.py`).

## Layout

```
jmri-scripting/
├── pyproject.toml
├── README.md
├── examples/                  smoke.py, loop_automaton.py
└── src/jmri_scripting/
    ├── __init__.py
    ├── shim.py                JmriShim + manager/bean shims + sync bridges
    ├── jython_shim.py         synthetic jmri.* module tree
    ├── api.py                 from jmri_scripting.api import … (proxies)
    ├── automaton.py           AbstractAutomaton base class
    ├── runner.py              ScriptRunner — worker threads + stdout capture + autoload
    ├── repl.py                interactive REPL session
    ├── watcher.py             ScriptWatcher — mtime polling hot-reload
    └── py.typed
```
