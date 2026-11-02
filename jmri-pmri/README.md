# jmri-pmri — PMRI launcher

The single launch point for the Python Model Railroad Interface web
stack. Replaces the raw `uvicorn jmri_server:create_app --factory ...`
command line with a customtkinter GUI that owns the uvicorn
lifecycle, and a `--headless` mode for Pi / systemd installs.

Per the launcher spec, PMRI is one entry point (`pmri`), not JMRI's
PanelPro / DecoderPro split — the unified web UI makes separate
launchers unnecessary.

## Quality gates

| Gate | Result |
|---|---|
| `ruff check src` | all checks pass |
| `pytest tests/` | **41 / 41** passing |
| Coverage | **75%** (target 70%; `gui.py` + `__main__.py` omitted — need a display) |

## Usage

```
pmri                          # open the GUI
pmri --headless               # service only (Pi / systemd)
pmri --port 9000              # one-shot bind-port override
pmri --profile DRGW           # use a specific profile
pmri --connection yard        # pick a specific connection by name
pmri --no-browser             # don't auto-open the browser
pmri --settings-dir /tmp/x    # non-default settings root (tests)
```

`pmri --help` for the full flag list.

Config resolution order on launch (first match wins):

1. CLI flag.
2. `<settings>/pmri.xml` (see `jmri_core.persistence.PmriConfig`).
3. `JMRI_*` env var (debug-only fallback — Chunk C migration).
4. Hard-coded default.

The launcher writes nothing on startup; `pmri.xml` and `profile.xml`
are only touched when the GUI's Save buttons run.

## What the GUI shows

Three tabs on a 640×480 window:

* **Service** — colored status dot (grey/amber/green), Start / Stop
  / Open UI buttons, URL + error label. Status polls every 300 ms.
* **pmri.xml** — form bound to the loaded `PmriConfig`: HTTP host /
  port, WiThrottle host / port, log level, log file, active profile,
  "open browser on service start" checkbox. Save writes through
  `write_with_backup` so the previous version rotates to `pmri.xml.bak`.
* **profile.xml** — read-only preview of the active profile's
  connections + script list, with a Reload button so an operator
  can edit the file in a text editor and refresh. Full CRUD lands in
  a later Chunk B pass.

Window close stops the service — spec's "window owns the service"
semantic. On `--headless` a SIGINT / SIGTERM handler plays the same
role.

## Browser integration

On service start (or GUI Start button), when
`launcher.open-browser-on-start` is true (default), the launcher:

1. Polls `127.0.0.1:<port>` with 10 ms TCP probes up to a 5 s cap.
2. Calls `webbrowser.open(url)` once the port accepts.

Stdlib `webbrowser.open` handles the three-platform story: Windows
`start`, macOS `open`, Linux `xdg-open` — all three open a new tab in
an already-running browser or launch one if not. No extra dependency.

A `0.0.0.0` bind coerces to `127.0.0.1` for the probe + URL (listen-
everywhere isn't a dialable destination).

## Why uvicorn in a thread

The GUI mainloop blocks the Python interpreter. uvicorn needs an
asyncio event loop. We give it its own daemon thread running
`asyncio.run(server.serve())`, with `server.should_exit =
server.force_exit = True` as the shutdown signal. A failed start
(bad app import, bind collision) populates `controller.last_error`
+ transitions `state` to STOPPED so the Service tab can display the
reason inline.

## Layout

```
jmri-pmri/
├── pyproject.toml                 # installs `pmri` on PATH via console_scripts
├── README.md
└── src/jmri_pmri/
    ├── __init__.py                re-exports ServiceController + state enum
    ├── __main__.py                `python -m jmri_pmri` wrapper
    ├── cli.py                     argparse + mode dispatch (GUI / headless)
    ├── service.py                 ServiceController -- uvicorn in a thread
    ├── browser.py                 open_browser_when_ready (TCP probe + webbrowser.open)
    ├── picker.py                  Startup connection picker (CLI + Tk dialog)
    └── gui.py                     customtkinter window
tests/
├── unit/
│   ├── test_cli.py                argparse surface + env stopgap
│   ├── test_picker.py             resolver + CLI picker edge cases
│   ├── test_service.py            state machine + TCP probe + wait_until_ready
│   └── test_browser.py            probe-then-open sequencing
└── integration/
    └── test_headless_boot.py      start/wait/stop + bad-app-import error path
```

## Chunk C hand-off

The connection the launcher picks is passed to the server by writing
`JMRI_CONNECTION` + `JMRI_CONNECTION_SETTINGS` env vars just before
spawning uvicorn — a stopgap. Chunk C replaces this with a direct
call path: the server reads `<profile>/profile/profile.xml` itself.
At that point `_publish_connection_env` + its callers go away.
