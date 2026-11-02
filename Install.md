# JMRI Python rewrite — alpha install (0.1.0a3)

For alpha testing on an NCE PowerCab / PowerPro / SB5. Hardware-verified
on PowerPro 6.2.3 RS-232 with a USB-serial adapter; the same packages also
drive LocoNet, DCC-EX, and OpenLCB if you swap the connection setting.

## What's new in alpha-3 (patch over alpha-2)

- **PanelPro.xml interop fix**: the `class="..."` attribute on each
  bean-manager block now uses driver-specific concrete class names
  (e.g. `jmri.jmrix.nce.configurexml.NceTurnoutManagerXml`) instead of
  the abstract base — files we wrote in alpha-2 were rejected by JMRI
  Java at startup with a `cvc-pattern-valid` schema error. Beans are
  now grouped into one `<turnouts>` / `<sensors>` / `<lights>` block
  per system prefix, matching JMRI's own layout.
- **WiThrottle turnout state fix**: the `TurnoutManager.state_changed`
  subscription is now wired inside `WiThrottleServer.start()` (which
  runs *after* the connection's `open()`), so live turnout changes
  from the web / cab / script now reach every connected Engine Driver
  / WiThrottle iOS client — that subscription was silently landing on
  the pre-open base manager in alpha-2.
- **Diagnostic**: `NceConnection.open()` now emits a one-shot
  `WARNING: NCE connection opening: kind=... port=... command_set=...`
  line at startup so misconfigured `JMRI_CONNECTION_SETTINGS` (which
  silently falls back to `InternalConnection`) is visible in the log
  immediately.

## What's new in alpha-2

- **Fast clock**: reads and writes the NCE command station's fast clock
  (opcode 0x8F/0x8E at `0xDC00`). Web widget + JSON `time` service.
- **WiThrottle server** on TCP port 12090 — Engine Driver / WiThrottle iOS
  compatible: roster, function labels, throttle, turnout panel, and fast
  clock sync all wired up.
- **Multi-profile support**: JMRI 5.x `profiles.xml` is read and the
  active profile is switchable from the web UI. Profile `jython/*.py`
  scripts auto-launch on profile load.
- **NCE opcode safety audit**: every opcode we send is now verified
  against JMRI's decoder table. Fixed several silently-wrong opcodes
  (see the audit table in `jmri-nce/src/jmri_nce/opcodes.py`).

## Prerequisites

- Python 3.12 or newer
- pip (comes with Python)
- USB cable from the host to the NCE command station

### Linux (Raspberry Pi / Debian / Ubuntu)

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip
# Add yourself to the dialout group so you can open /dev/ttyUSB*:
sudo usermod -aG dialout $USER
# Log out and back in for the group change to take effect.
```

### macOS

```bash
brew install python@3.12
# No special group setup needed.
```

## Getting the source

The packages aren't on PyPI yet — copy the directories you need:

```
jmri-core/         (required)
jmri-server/       (required)
jmri-nce/          (required for NCE hardware)
jmri-web/          (recommended — browser UI)
jmri-withrottle/   (recommended — Engine Driver / WiThrottle iOS support)
jmri-loconet/      (optional — second hardware shape)
jmri-ncemonitor/   (optional — passive NCE cab-bus sniffer)
jmri-dccex/        (optional — DCC-EX command stations)
jmri-openlcb/      (optional — LCC/OpenLCB nodes)
jmri-logixng/      (optional — automation engine)
jmri-scripting/    (optional — Python automation scripts)
jmri-decoderpro/   (optional — CV programming)
```

Easiest:
- `rsync -av pmri/ user@target:/home/user/jmri/`
- Zip + scp, or
- `git clone` if a repo has been initialized.

## Install the packages

In the directory containing all the `jmri-*` subdirs:

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# On Windows PowerShell:  .venv\Scripts\Activate.ps1

pip install -e ./jmri-core
pip install -e ./jmri-scripting
pip install -e ./jmri-server
pip install -e ./jmri-decoderpro
pip install -e ./jmri-web
pip install -e ./jmri-withrottle
pip install -e ./jmri-nce
```

Verify the NCE driver is discoverable:

```bash
python -c "from jmri_core.plugins import load_connections; print(sorted(load_connections()))"
# Should print: ['internal', 'nce']   (+ any other drivers you installed)
```

## Identify the serial port

The NCE PowerCab/PowerPro presents as a USB-serial device.

### Linux

```bash
ls /dev/ttyUSB* /dev/ttyACM*  2>/dev/null
# Typical: /dev/ttyUSB0 or /dev/ttyACM0
dmesg | tail -20                          # recent device attaches
udevadm info -a /dev/ttyUSB0 | head -30   # vendor/product IDs
```

Optional stable name via udev:

```
# /etc/udev/rules.d/99-nce.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="<from udevadm>", ATTRS{idProduct}=="<from udevadm>", SYMLINK+="nce"
```

Then `/dev/nce` always points at the CS.

### macOS

```bash
ls /dev/tty.usbserial-*    # FTDI-based
ls /dev/tty.usbmodem*       # CDC-based
```

## Start the server

Two environment variables select the connection:

| Variable | Value |
|---|---|
| `JMRI_CONNECTION` | `nce` |
| `JMRI_CONNECTION_SETTINGS` | JSON object passed to `connection.open()` |

```bash
export JMRI_CONNECTION=nce
export JMRI_CONNECTION_SETTINGS='{"kind":"serial","port":"/dev/ttyUSB0","baud":9600,"command_set":"PH-Pro 2006+"}'
uvicorn jmri_web:create_app --factory --host 0.0.0.0 --port 8000
```

If you didn't install `jmri-web`, run the API-only server:

```bash
uvicorn jmri_server:create_app --factory --host 0.0.0.0 --port 8000
```

## NCE settings reference

```json
{
  "kind": "serial",
  "port": "/dev/ttyUSB0",
  "baud": 9600,
  "command_set": "PH-Pro 2006+",
  "aiu_addresses": [1, 2],
  "disable_turnout_monitor": false
}
```

Field notes:

- `command_set` is one of `"PH-Pro 2006+"`, `"PowerCab"`, `"Smart Booster SB5"`. Set this explicitly for a fast startup; auto-detect (opcode 0xAA, `SW_REV_CMD`) is safe as of alpha-2 but adds a small probe delay.
- `aiu_addresses` is the list of AIU module addresses to poll for occupancy sensors. Leave empty if you don't have AIUs.
- `disable_turnout_monitor` opts out of the accessory-state poll (every 1.5s). The monitor reads CS memory at `0xEC00` via opcode `0x8F`; if your firmware variant doesn't accept that, set this to `true` and turnout known-state will only update from your own commands.

## Bringing in your existing JMRI configuration

The server reads `profiles.xml` from the settings directory and uses it
to discover JMRI railroad profiles. Each profile's `PanelPro.xml`
provides the bean tables (turnouts/sensors/lights), and its `roster.xml`
+ `roster/` directory provides locomotives.

### Default settings paths

| OS | Settings dir | Example profile path |
|---|---|---|
| Linux | `~/.jmri/` | `~/.jmri/DRGW.jmri/PanelPro.xml` |
| macOS | `~/Library/Preferences/JMRI/` | `~/Library/Preferences/JMRI/DRGW.jmri/PanelPro.xml` |
| Windows | `%USERPROFILE%\JMRI\` | `%USERPROFILE%\JMRI\DRGW.jmri\PanelPro.xml` |

### Pointing at a non-default tree

If your existing JMRI install is on a different host (e.g. you do
config on Windows but run the server on a Pi), set
`JMRI_SETTINGS_DIR` to where the tree lives. The server reads
`profiles.xml` from that location and resolves each profile's `path`
attribute relative to it.

```bash
# Linux Pi running against an SMB-mounted Windows JMRI directory:
export JMRI_SETTINGS_DIR=/mnt/jmri
# or against an rsync'd copy:
export JMRI_SETTINGS_DIR=/home/pi/jmri-data
```

### What's loaded

- The **active profile** is resolved from `<settings>/.active_profile`
  (our own marker file) or, failing that, the first `<profile>` entry
  in `profiles.xml`. Open the home page and click **Load** on a
  different profile to switch — the server hot-swaps the bean tables,
  roster, and persistence target without restarting the hardware
  connection.
- The active profile's `PanelPro.xml` (or legacy `Configuration.xml`)
  populates turnouts, sensors, and lights with the active connection's
  prefix (`N*` for NCE, `I*` for internal). Sections we don't model
  yet (signal heads, blocks, panels, LogixNG) are **preserved
  verbatim** on every save, so the file remains openable in JMRI.
- The active profile's `roster.xml` + `roster/<id>.xml` files populate
  the roster page.

Every web-UI mutation persists atomically into the active profile.

What you get:

- Turnouts, sensors, and lights with the active connection's prefix
  (`N…` for NCE plus `I…` internal) are materialized as live beans.
- Sections we don't yet model (signal heads, memories, blocks, panels,
  LogixNG conditionals) are **preserved verbatim** on every save, so
  the file remains openable in JMRI Java.
- Roster files live separately at `<profile-root>/roster/` (matches
  JMRI's layout). Per-loco XML files + `roster.xml` index are read at
  first roster API hit.

Every web-UI mutation (create / edit / delete a turnout/sensor/light or
roster entry) persists immediately via an atomic write.

## Verify the layout

From any machine via curl (replace `<target-ip>` with the host):

```bash
# Server identity:
curl -X POST http://<target-ip>:8000/json/ \
     -H 'Content-Type: application/json' \
     -d '{"type":"hello"}'

# Power on:
curl -X POST http://<target-ip>:8000/json/ \
     -H 'Content-Type: application/json' \
     -d '{"type":"power","method":"post","data":{"state":2}}'

# Acquire throttle for loco 3 and roll at half speed forward:
curl -X POST http://<target-ip>:8000/json/ \
     -H 'Content-Type: application/json' \
     -d '{"type":"throttle","method":"put","data":{"address":3}}'

curl -X POST http://<target-ip>:8000/json/ \
     -H 'Content-Type: application/json' \
     -d '{"type":"throttle","method":"post","data":{"name":"3","speed":0.5,"forward":true}}'

# Throw turnout NT12:
curl -X POST http://<target-ip>:8000/json/ \
     -H 'Content-Type: application/json' \
     -d '{"type":"turnout","method":"put","data":{"name":"NT12"}}'

curl -X POST http://<target-ip>:8000/json/ \
     -H 'Content-Type: application/json' \
     -d '{"type":"turnout","method":"post","data":{"name":"NT12","state":4}}'
```

From a browser (if `jmri-web` is installed):

Open `http://<target-ip>:8000/`. Nav bar gives you home / throttle /
turnouts / sensors / lights / roster / programmer / scripts / panel.
Each page has create + edit + delete (where appropriate); throttle
supports long addresses; mutations persist to `Configuration.xml`
automatically.

## Troubleshooting

| Symptom | Likely cause + fix |
|---|---|
| `Permission denied` opening serial port (Linux) | Add yourself to `dialout`; verify `ls -l /dev/ttyUSB0` |
| `unknown connection 'nce'` at startup | `jmri-nce` not installed — `pip install -e ./jmri-nce` |
| Auto-detect returns quickly and falls back | Alpha-2 uses the safe `SW_REV_CMD` (0xAA); no wedging risk. Falling back means the CS didn't answer within 500 ms — the fallback command set is usually fine. Set `command_set` explicitly if you want to skip the probe. |
| `NCE command READ16(0xec00) timed out` | Your firmware or link is dropping the 16-byte accessory-state read. Add `"disable_turnout_monitor": true` to your settings. Throw/close commands still work; only the *known* state poll is off. |
| `NceCommandError` with weird ACK byte | The traffic controller drains stale buffer bytes between commands; if you still see this with the alpha, capture the log and file an issue with the `ack=` byte value. |
| `TimeoutError` on every command | Wrong baud rate. PowerCab/PowerPro USB defaults to 9600; some legacy firmware uses 19200. |
| Turnouts move but `known_state` never updates | Either `disable_turnout_monitor` is true, or the CS doesn't support the accessory-state read. Commanded → known mirroring still gives immediate UI feedback. |

### Watching what's happening on the wire

```bash
PYTHONUNBUFFERED=1 \
  LOGLEVEL=DEBUG \
  uvicorn jmri_web:create_app --factory --log-level debug
```

`jmri_nce.traffic` logs every command at DEBUG including drained-stale-byte
diagnostics — useful when isolating a misbehaving opcode.

## Running as a service (systemd, Linux)

```
# /etc/systemd/system/jmri.service
[Unit]
Description=JMRI Python server
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pmri
Environment=JMRI_CONNECTION=nce
Environment=JMRI_CONNECTION_SETTINGS={"kind":"serial","port":"/dev/nce","baud":9600,"command_set":"PH-Pro 2006+"}
ExecStart=/home/pi/pmri/.venv/bin/uvicorn jmri_web:create_app --factory --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jmri
sudo journalctl -u jmri -f      # follow logs
```

## Connecting Engine Driver / WiThrottle iOS

If `jmri-withrottle` is installed, the server also listens for WiThrottle
clients on **TCP port 12090**. Open your phone's WiFi throttle app and
add a manual server entry pointing at `<target-ip>:12090` (some clients
auto-discover via Bonjour — that's a Stage 11 addition; for alpha add it
by hand).

What works:
- Roster listing (roster loaded from the active profile)
- Loco acquire by roster ID **or** by manual DCC address
- Full 128-step speed, direction, F0-F28 buttons
- Emergency stop
- Turnouts tab: full list, throw/close/toggle, live state updates from
  every source (WiThrottle, web UI, physical cab, script)
- Fast clock sync: pushes CS clock time + rate every 5s so the client
  clock stays aligned when the CS clock starts, stops, or changes rate

Multiple simultaneous WiThrottle clients are supported. All clients see
turnout state changes and clock updates in real time.

## What's in this alpha

End-to-end verified on real PowerPro 6.2.3 hardware:

1. `{"type":"hello"}` → server identity (always works first; if not, the server isn't running)
2. Power on/off → relay click on the CS
3. Turnouts `N*` → physical movement on the layout; persisted in profile `PanelPro.xml`
4. Throttle short + long DCC addresses, speed, direction, F0-F28
5. Sensors via AIU polling (if `aiu_addresses` is set)
6. Roster CRUD via web UI + JSON API
7. CV programming via DecoderPro `/programmer` web page (direct mode only —
   ops-mode is temporarily withdrawn pending the packet-shipping rewrite)
8. Fast clock: read/write CS clock + rate + start/stop from web widget
9. WiThrottle: Engine Driver / iOS clients drive throttles, turnouts, clock
10. Multi-profile: switch profiles from home page; `jython/*.py` auto-launches on load

Known gaps for this alpha:
- NCE ops-mode CV write is disabled (was using wrong opcode; will return on
  the `QUEUEn_TMP_CMD` packet-shipping path). Use direct mode for now.
- The accessory-state monitor (`0x8F`) doesn't work on every firmware;
  workaround (`disable_turnout_monitor: true`) is documented above.
- Web UI doesn't yet edit roster function labels; WiThrottle uses default
  labels (Light/Bell/Horn for F0-F2, generic Fn for F3-F28).
- Panel viewer shows file metadata only; rendering the layout SVG is Stage 11.
- WiThrottle Bonjour auto-discovery not implemented (manual server entry works).

## Reporting issues

When something doesn't work, capture three things:

1. Connection settings (paste JMRI_CONNECTION_SETTINGS)
2. The full server log from startup through the failing operation (`LOGLEVEL=DEBUG`)
3. What you expected vs. what happened on the layout

Particularly useful: NCE firmware revision (visible on the CS's LCD or via the JMRI Java "EPROM revision" tool) — this alpha was tuned against 6.2.3 and other firmwares may behave differently in subtle ways.
