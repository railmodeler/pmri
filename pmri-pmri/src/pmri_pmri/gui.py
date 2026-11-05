"""customtkinter launcher window.

Owns the uvicorn lifecycle: closing the window stops the service
(matches the spec's "window owns the service" semantic). The main
tab carries Start / Stop / Open UI buttons plus a status line; two
settings tabs edit ``pmri.xml`` and ``profile.xml`` with
save-triggers-rolling-backup behaviour.

The window is intentionally thin -- it's a shell around
:class:`ServiceController` and the two config models. All the
real work (uvicorn, backup, XML round-trip) lives in pmri-core and
:mod:`pmri_pmri.service`; this module is the user-facing wiring.

Imported lazily by :func:`pmri_pmri.cli.main` so a headless install
without customtkinter never touches it.
"""
from __future__ import annotations

import logging
import pathlib
import threading
from typing import Any

import customtkinter as ctk
from pmri_core.persistence import (
    ConnectionDef,
    PmriConfig,
    Profile,
    ProfileRef,
    ProfileSettings,
    ScriptDef,
    load_profile_registry,
    pmri_config_path,
    save_pmri_config,
    save_profile_registry,
    save_profile_xml,
)

from pmri_pmri.browser import open_browser_when_ready
from pmri_pmri.service import ServiceController, ServiceState


def _list_serial_ports() -> list[str]:
    """Return the names of serial ports visible to pyserial, or an
    empty list if pyserial isn't installed. The connection editor
    uses this to populate the serial-port dropdown so a Case-3 new
    user doesn't have to guess the device name
    (``/dev/ttyUSB0`` on Linux, ``COM3`` on Windows).

    pyserial is a transitive dep of the hardware driver packages
    (pmri-nce, pmri-loconet, pmri-dccex). On a bare PMRI install
    without those, we degrade gracefully to a free-text entry.
    """
    try:
        from serial.tools import list_ports  # noqa: PLC0415
    except ImportError:
        return []
    try:
        return sorted({p.device for p in list_ports.comports() if p.device})
    except Exception:
        return []


# System-prefix defaults keyed by driver type. Mirrors the ``default``
# column in :data:`_SCHEMAS` -- centralised here so
# :meth:`PmriApp._on_change_connection_type` can refresh the stored
# prefix without re-parsing the schema. (Case-3 feedback: picking
# "nce" on a fresh connection row shouldn't leave the prefix at the
# previous type's "I".)
_DEFAULT_SYSTEM_PREFIX: dict[str, str] = {
    "nce": "N",
    "loconet": "L",
    "digitrax": "L",
    "dccex": "D",
    "lenz": "X",
    "mrc": "M",
    "openlcb": "M",
    "ncemonitor": "M",
    "internal": "I",
}

_log = logging.getLogger(__name__)

_STATUS_COLORS = {
    # Stopped is red (not grey) per the Case-1 feedback: "The dot
    # should be red or green, not grey." The transitional states
    # (STARTING / STOPPING) are amber to flag "in flight; please
    # wait" without picking a side.
    ServiceState.STOPPED: "#d04040",
    ServiceState.STARTING: "#d0b000",
    ServiceState.STARTED: "#2a9d2a",
    ServiceState.STOPPING: "#d0b000",
}

# ---- Driver schemas for the connection editor ---------------------------
#
# Each supported driver type gets a settings schema the GUI uses to
# render the right form fields when the operator picks that type from
# the Connection Type pulldown. ``kind`` is a dropdown (serial/tcp/
# loopback), everything else is a text entry with a default; ``choices``
# turns the entry into a dropdown.
#
# The list is kept intentionally minimal -- the fields JMRI stores in
# profile.xml and our drivers' ``open()`` methods consume. Operators
# wanting unlisted settings (reconnectMaxAttempts etc.) can still edit
# profile.xml directly; the editor loads/saves them opaquely as part
# of the settings dict.

CONNECTION_TYPES: tuple[str, ...] = (
    "nce", "loconet", "digitrax", "dccex", "lenz", "mrc",
    "openlcb", "ncemonitor", "internal",
)

# Schema: list of (key, label, default, choices_or_None).
# ``choices_or_None=None`` => free-text entry.
_SCHEMAS: dict[str, list[tuple[str, str, str, tuple[str, ...] | None]]] = {
    "nce": [
        ("kind", "Transport", "serial", ("serial", "tcp", "loopback")),
        ("port", "Serial port", "", None),
        ("baud", "Baud", "9600", None),
        ("host", "Host (tcp only)", "", None),
        ("tcp_port", "TCP port (tcp only)", "", None),
        ("systemPrefix", "System prefix", "N", None),
        ("command_set", "Command set", "PH-Pro 2006+",
         ("PH-Pro 2006+", "PowerCab", "SB5")),
        ("auto_detect", "Auto-detect firmware", "false", ("true", "false")),
    ],
    "loconet": [
        ("kind", "Transport", "serial", ("serial", "tcp")),
        ("port", "Serial port", "", None),
        ("baud", "Baud", "57600", None),
        ("host", "Host (tcp only)", "", None),
        ("tcp_port", "TCP port (tcp only)", "1234", None),
        ("systemPrefix", "System prefix", "L", None),
    ],
    # Digitrax is LocoNet protocol over a Digitrax command station
    # (DCS-xxx, PR3/PR4, LocoBuffer-USB, etc.). Driver lives in the
    # pmri-digitrax wheel; the stub there layers Digitrax-specific
    # command-station semantics on top of pmri-loconet's wire-protocol
    # transport.
    "digitrax": [
        ("kind", "Transport", "serial", ("serial", "tcp")),
        ("port", "Serial port", "", None),
        ("baud", "Baud", "57600", None),
        ("host", "Host (tcp only)", "", None),
        ("tcp_port", "TCP port (tcp only)", "1234", None),
        ("systemPrefix", "System prefix", "L", None),
    ],
    "dccex": [
        ("kind", "Transport", "serial", ("serial", "tcp")),
        ("port", "Serial port", "", None),
        ("baud", "Baud", "115200", None),
        ("host", "Host (tcp only)", "", None),
        ("tcp_port", "TCP port (tcp only)", "2560", None),
        ("systemPrefix", "System prefix", "D", None),
    ],
    # Lenz uses the XpressNet protocol over an LI-USB (or similar)
    # serial dongle. Server-side driver (pmri-lenz) isn't shipped
    # yet -- picking this type and clicking Start will surface
    # "unknown connection 'lenz'; installed: ..." in last_error.
    # The menu entry is here so a Case-3 Lenz operator sees their
    # system in the pulldown and knows PMRI is aware of it.
    "lenz": [
        ("kind", "Transport", "serial", ("serial", "tcp")),
        ("port", "Serial port", "", None),
        ("baud", "Baud", "9600", None),
        ("host", "Host (tcp only)", "", None),
        ("tcp_port", "TCP port (tcp only)", "5550", None),
        ("systemPrefix", "System prefix", "X", None),
    ],
    # MRC Prodigy Advance / Prodigy Wireless. Same no-driver-yet
    # story as Lenz; menu entry only.
    "mrc": [
        ("kind", "Transport", "serial", ("serial",)),
        ("port", "Serial port", "", None),
        ("baud", "Baud", "9600", None),
        ("systemPrefix", "System prefix", "M", None),
    ],
    "openlcb": [
        ("kind", "Transport", "tcp", ("serial", "tcp")),
        ("host", "Host (tcp)", "", None),
        ("tcp_port", "TCP port", "12021", None),
        ("port", "Serial port (serial only)", "", None),
        ("baud", "Baud (serial only)", "333333", None),
        ("systemPrefix", "System prefix", "M", None),
    ],
    "ncemonitor": [
        ("kind", "Transport", "serial", ("serial",)),
        ("port", "Serial port", "", None),
        ("baud", "Baud", "9600", None),
        ("systemPrefix", "System prefix", "M", None),
    ],
    "internal": [
        ("systemPrefix", "System prefix", "I", None),
    ],
}


def _schema_for(driver_type: str) -> list[tuple[str, str, str, tuple[str, ...] | None]]:
    """Return the GUI field schema for *driver_type*, or an empty
    schema (just a free-text settings area) when the type isn't one
    we know about. Keeps the editor working for forward-compat /
    operator-authored custom drivers."""
    return _SCHEMAS.get(driver_type, [])


class _StubEntry:
    """Non-widget stand-in for a collapsed-row field. ``get()``
    returns the stored string so the commit code path treats
    collapsed rows the same as expanded ones -- the operator can
    Add / Delete / reorder while most rows are hidden, and the
    hidden rows' values round-trip unchanged.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value


def _coerce_setting_value_live(raw: str) -> Any:
    """Best-effort type recovery for a GUI entry value.

    Mirrors ``profile_xml._coerce_setting_value`` -- int, then float,
    then bool, then raw string. Done here (not just on reload) so a
    saved port number persists as int 9600 rather than "9600" and
    the Save-then-Reload round-trip is a visual no-op."""
    if raw == "":
        return ""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    return raw


def run(
    cfg: PmriConfig,
    *,
    settings_root: pathlib.Path,
    profile: Profile,
    profile_settings: ProfileSettings,
    open_browser: bool | None,
) -> int:
    """Launch the GUI. Blocks until the window closes; returns a
    shell exit code (0 on normal shutdown)."""
    want_browser = (
        cfg.launcher.open_browser_on_start if open_browser is None else open_browser
    )
    app = PmriApp(
        cfg=cfg,
        settings_root=settings_root,
        profile=profile,
        profile_settings=profile_settings,
        open_browser_on_start=want_browser,
    )
    app.mainloop()
    return 0


class PmriApp(ctk.CTk):
    """Top-level launcher window.

    Composition over inheritance isn't idiomatic in customtkinter --
    the stdlib tkinter pattern of subclassing the root window is what
    customtkinter examples use throughout, so we match that.
    """

    def __init__(
        self,
        *,
        cfg: PmriConfig,
        settings_root: pathlib.Path,
        profile: Profile,
        profile_settings: ProfileSettings,
        open_browser_on_start: bool,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.settings_root = settings_root
        self.profile = profile
        self.profile_settings = profile_settings
        self.open_browser_on_start = open_browser_on_start
        self.controller = ServiceController(config=cfg)
        # Case-3 feedback: saving profile.xml before pmri.xml exists
        # lands the file under whatever profile name was discovered
        # (usually "default"), even though the operator may be about
        # to rename it. Gate the Startup Options Save button on this
        # flag so first-run users commit pmri.xml first.
        self._pmri_saved: bool = pmri_config_path(settings_root).exists()

        self.title("PMRI -- Python Model Railroad Interface")
        self.geometry("640x480")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_tabs()
        self._poll_status()
        # Case-1 feedback: on first launch (no pmri.xml yet), the
        # operator sees an empty-looking GUI and no breadcrumb for
        # what to do. Pop a one-shot dialog AFTER the window shows
        # so the modal sits on top of the already-painted tabs.
        if not pmri_config_path(settings_root).exists():
            self.after(200, self._show_first_launch_dialog)

    # ---- layout --------------------------------------------------------

    def _build_tabs(self) -> None:
        tabview = ctk.CTkTabview(self)
        tabview.pack(fill="both", expand=True, padx=10, pady=10)
        # Tab labels per Case-1 feedback: "PMRI Options" + "Startup
        # Options" are clearer than the on-disk filenames.
        tabview.add("Service")
        tabview.add("PMRI Options")
        tabview.add("Startup Options")
        self._build_service_tab(tabview.tab("Service"))
        self._build_pmri_tab(tabview.tab("PMRI Options"))
        self._build_profile_tab(tabview.tab("Startup Options"))

    def _build_service_tab(self, parent: ctk.CTkFrame) -> None:
        # Tab title -- Case-1 feedback asked for context clues; without
        # this, a user opening the GUI the first time sees a bare dot
        # and no idea what they're looking at.
        ctk.CTkLabel(
            parent, text="Web Service Control",
            font=("", 16, "bold"),
        ).pack(anchor="w", padx=10, pady=(10, 5))

        # Status row: "Web Server Status: ● Stopped". The leading label
        # names the thing; the dot shows the colour (red/amber/green)
        # and the trailing word gives the state text.
        status_row = ctk.CTkFrame(parent)
        status_row.pack(fill="x", pady=10, padx=10)
        ctk.CTkLabel(status_row, text="Web Server Status:", font=("", 13)).pack(
            side="left", padx=(5, 10),
        )
        self.status_dot = ctk.CTkLabel(
            status_row, text="●", font=("", 24),
            text_color=_STATUS_COLORS[ServiceState.STOPPED],
        )
        self.status_dot.pack(side="left")
        self.status_text = ctk.CTkLabel(status_row, text="Stopped", font=("", 14))
        self.status_text.pack(side="left", padx=(5, 0))

        # Buttons.
        btn_row = ctk.CTkFrame(parent)
        btn_row.pack(fill="x", pady=10)
        self.start_btn = ctk.CTkButton(btn_row, text="Start", command=self._on_start)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = ctk.CTkButton(
            btn_row, text="Stop", command=self._on_stop, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=5)
        self.open_btn = ctk.CTkButton(
            btn_row, text="Open UI", command=self._on_open_browser, state="disabled",
        )
        self.open_btn.pack(side="left", padx=5)

        # URL + error line, visible once started / failed.
        self.url_label = ctk.CTkLabel(
            parent, text="", font=("", 12), text_color="#4a9eff",
        )
        self.url_label.pack(pady=5)
        self.error_label = ctk.CTkLabel(
            parent, text="", font=("", 12), text_color="#d04040", wraplength=580,
        )
        self.error_label.pack(pady=5)
        # Hint shown after Start when auto-open-browser was requested;
        # points the operator at "Open UI" for setups where the
        # auto-open silently fails (chromium on some Linux desktops).
        self.hint_label = ctk.CTkLabel(
            parent, text="", font=("", 11), text_color="#888888",
            wraplength=580,
        )
        self.hint_label.pack(pady=5)

    def _build_pmri_tab(self, parent: ctk.CTkFrame) -> None:
        """PMRI Options form bound to the current PmriConfig. Edits
        write back via :func:`save_pmri_config` on Save -- that call
        goes through the rolling-backup writer, so saves never
        clobber the previous version.

        Layout: Save button + status pinned at the bottom; everything
        else goes in a scrollable frame above it. Without the scroll
        wrapper, auth checkboxes pushed Save off the fixed 640x480
        viewport (feedback item: "The button to save the pmri.xml
        has disappeared from the PMRI Options tab").
        """
        # Pin status label + Save button at the bottom FIRST so they
        # always claim space in the viewport regardless of how tall
        # the scrollable form above grows.
        self.pmri_status = ctk.CTkLabel(parent, text="")
        self.pmri_status.pack(side="bottom", pady=(0, 10))
        ctk.CTkButton(
            parent, text="Save pmri.xml", command=self._on_save_pmri,
            width=180,
        ).pack(side="bottom", pady=(5, 0))

        # Scrollable fields area fills the remaining space.
        scroll = ctk.CTkScrollableFrame(parent)
        scroll.pack(fill="both", expand=True, padx=5, pady=5)

        self._pmri_fields: dict[str, ctk.CTkEntry] = {}
        fields = [
            # Settings directory -- the JMRI/PMRI config root.
            # Platform defaults: ~/.jmri on Linux, ~/JMRI on Windows,
            # ~/Library/Preferences/JMRI on macOS. Prefilled from the
            # resolved default so a Case-3 new user doesn't need to
            # know the convention; editable so power users on a
            # non-standard tree can rebind without the --settings-dir
            # CLI flag.
            ("Settings directory", str(self.settings_root)),
            ("HTTP host", str(self.cfg.http.host)),
            ("HTTP port", str(self.cfg.http.port)),
            ("WiThrottle host", str(self.cfg.withrottle.host)),
            ("WiThrottle port", str(self.cfg.withrottle.port)),
            # Log file -- bare name resolves against the Settings
            # directory; absolute path passes through. Prefilled with
            # "pmri.log" so a Case-3 new user has a working default
            # alongside JMRI's own jmri.log.
            ("Log file",
             "pmri.log" if self.cfg.log.file is None else str(self.cfg.log.file)),
            # If pmri.xml carries no explicit active_profile override,
            # show the DISCOVERED profile name so the field is never
            # blank. Saving writes whatever the operator entered back
            # into pmri.xml -- so a Save turns an "implicit discovery
            # result" into an "explicit pmri.xml override."
            ("Active profile", self.cfg.active_profile or self.profile.name),
            # Auth fields (feedback item 12). The launcher publishes
            # these as JMRI_AUTH_* env vars before uvicorn starts.
            ("Auth secret", self.cfg.auth.secret),
            ("Auth admin token", self.cfg.auth.admin_token),
            # XML corpus paths -- fed to JMRI_SIGNAL_XML_ROOT /
            # JMRI_DECODER_XML_ROOT env vars at service start.
            # Without these the signal-system loader + the decoder
            # programmer start empty.
            ("Signal XML root",
             "" if self.cfg.xml_roots.signal is None else str(self.cfg.xml_roots.signal)),
            ("Decoder XML root",
             "" if self.cfg.xml_roots.decoder is None else str(self.cfg.xml_roots.decoder)),
        ]
        for label, value in fields:
            row = ctk.CTkFrame(scroll)
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=label, width=140, anchor="w").pack(side="left", padx=5)
            entry = ctk.CTkEntry(row)
            entry.insert(0, value)
            entry.pack(side="left", fill="x", expand=True, padx=5)
            self._pmri_fields[label] = entry
        self._pmri_browser_var = ctk.BooleanVar(
            value=self.cfg.launcher.open_browser_on_start,
        )
        ctk.CTkCheckBox(
            scroll,
            text="Open browser on service start",
            variable=self._pmri_browser_var,
        ).pack(anchor="w", padx=10, pady=5)
        self._pmri_auth_enabled_var = ctk.BooleanVar(value=self.cfg.auth.enabled)
        ctk.CTkCheckBox(
            scroll,
            text="Enable auth (JMRI_AUTH_ENABLED)",
            variable=self._pmri_auth_enabled_var,
        ).pack(anchor="w", padx=10, pady=5)
        self._pmri_auth_ephemeral_var = ctk.BooleanVar(
            value=self.cfg.auth.ephemeral_secret,
        )
        ctk.CTkCheckBox(
            scroll,
            text=(
                "Ephemeral auth secret (JMRI_AUTH_EPHEMERAL_SECRET) -- "
                "fresh random secret on every start; forces re-login"
            ),
            variable=self._pmri_auth_ephemeral_var,
        ).pack(anchor="w", padx=10, pady=5)
        # Log level selector -- dropdown of the standard Python levels
        # so a new user doesn't have to know the exact spelling
        # ("WARNING" vs "WARN") or case. The stored value is always the
        # canonical upper-case name.
        level_row = ctk.CTkFrame(scroll)
        level_row.pack(fill="x", pady=3)
        ctk.CTkLabel(
            level_row, text="Log level:", width=180, anchor="w",
        ).pack(side="left", padx=5)
        levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        current_level = (self.cfg.log.level or "INFO").upper()
        if current_level not in levels:
            current_level = "INFO"
        self._pmri_log_level_var = ctk.StringVar(value=current_level)
        ctk.CTkOptionMenu(
            level_row, variable=self._pmri_log_level_var,
            values=levels, width=240,
        ).pack(side="left", fill="x", expand=True, padx=5)

        # Console-echo tri-state (Case-1 feedback item 30). Auto =
        # echo iff no log file. Explicit opt-in for both-file-and-
        # console setups.
        console_row = ctk.CTkFrame(scroll)
        console_row.pack(fill="x", pady=3)
        ctk.CTkLabel(
            console_row, text="Echo log to console:",
            width=180, anchor="w",
        ).pack(side="left", padx=5)
        console_initial = {
            None: "Auto (echo iff no file)",
            True: "Always",
            False: "Never",
        }[self.cfg.log.console_echo]
        self._pmri_log_console_var = ctk.StringVar(value=console_initial)
        ctk.CTkOptionMenu(
            console_row,
            variable=self._pmri_log_console_var,
            values=["Auto (echo iff no file)", "Always", "Never"],
            width=240,
        ).pack(side="left", fill="x", expand=True, padx=5)

    def _build_profile_tab(self, parent: ctk.CTkFrame) -> None:
        """Startup Options tab -- full CRUD for profile.xml's
        connections + scripts, backed by one Save button that writes
        the file through :func:`save_profile_xml` (which uses the
        rolling-backup writer so edits never clobber the previous
        version).

        Layout: a tall scrollable frame holds the Connections section
        at the top and the Scripts section below it. Each editable
        row owns its own widgets whose references we stash in
        ``_conn_rows`` / ``_script_rows`` so Save can walk them in
        order and reconstruct the in-memory model.
        """
        header = ctk.CTkFrame(parent)
        header.pack(fill="x", pady=(5, 0), padx=5)
        # Stashed on self so _refresh_profile_tab_binding can update
        # the "Profile: <name>" text when the operator renames the
        # active profile on the PMRI Options tab and saves.
        self.profile_header_label = ctk.CTkLabel(
            header,
            text=f"Profile: {self.profile.name}",
            font=("", 14, "bold"),
        )
        self.profile_header_label.pack(side="left", padx=5)
        self.profile_save_button = ctk.CTkButton(
            header, text="Save Startup Options",
            command=self._on_save_profile, width=180,
        )
        self.profile_save_button.pack(side="right", padx=5)
        ctk.CTkButton(
            header, text="Reload", width=90,
            command=self._on_reload_profile,
        ).pack(side="right", padx=5)
        self.profile_status = ctk.CTkLabel(parent, text="")
        self.profile_status.pack(anchor="w", padx=10)
        # Enabled/disabled wiring for the Save button: first-run users
        # (no pmri.xml yet) must save PMRI Options first so the
        # profile directory is settled before profile.xml lands.
        self._refresh_profile_tab_binding()

        self._profile_scroll = ctk.CTkScrollableFrame(parent)
        self._profile_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        # ---- Connections section -------------------------------
        conn_header = ctk.CTkFrame(self._profile_scroll)
        conn_header.pack(fill="x", pady=(5, 2))
        ctk.CTkLabel(
            conn_header, text="Connections", font=("", 13, "bold"),
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            conn_header, text="+ Add connection", width=140,
            command=self._on_add_connection,
        ).pack(side="right", padx=5)
        # Container for the connection rows -- we destroy + rebuild
        # on every repaint so operations like Delete / Set Default /
        # change-type render consistently.
        self._conn_container = ctk.CTkFrame(self._profile_scroll)
        self._conn_container.pack(fill="x", pady=5)

        # ---- Scripts section -----------------------------------
        script_header = ctk.CTkFrame(self._profile_scroll)
        script_header.pack(fill="x", pady=(15, 2))
        ctk.CTkLabel(
            script_header, text="Startup scripts (launch order)",
            font=("", 13, "bold"),
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            script_header, text="+ Add script", width=120,
            command=self._on_add_script,
        ).pack(side="right", padx=5)
        self._script_container = ctk.CTkFrame(self._profile_scroll)
        self._script_container.pack(fill="x", pady=5)

        # Opaque-startup footer: if JMRI's non-script perform entries
        # are present, surface their count so the operator knows
        # they'll be round-tripped (not deleted) on Save.
        self._other_startup_label = ctk.CTkLabel(
            self._profile_scroll, text="", font=("", 11),
            text_color="#888888", anchor="w",
        )
        self._other_startup_label.pack(fill="x", pady=5, padx=5)

        # Row registries -- populated by _repaint_profile; walked by
        # Save to collect widget values back into the model.
        self._conn_rows: list[dict[str, Any]] = []
        self._script_rows: list[dict[str, Any]] = []
        # Collapse state per connection row, keyed by index. Survives
        # repaints so Add / Delete / Set-Default don't re-collapse
        # every open row -- except for the row that moved, which is
        # a reasonable tradeoff for the cleaner implementation.
        self._conn_row_state: dict[int, dict[str, Any]] = {}
        self._repaint_profile()

    # ---- profile tab: repaint from model -----------------------------

    def _repaint_profile(self) -> None:
        """Destroy + rebuild the editable rows from
        ``self.profile_settings``. Called after every model edit
        (Add / Delete / Set-Default / change-type) so the GUI stays
        in sync with the in-memory model without hand-tracking row
        indices."""
        for child in self._conn_container.winfo_children():
            child.destroy()
        for child in self._script_container.winfo_children():
            child.destroy()
        self._conn_rows.clear()
        self._script_rows.clear()
        if not self.profile_settings.connections:
            ctk.CTkLabel(
                self._conn_container,
                text="(none -- InternalConnection will be used until you add one)",
                text_color="#888888", anchor="w",
            ).pack(fill="x", padx=10, pady=5)
        for idx, c in enumerate(self.profile_settings.connections):
            self._build_connection_row(idx, c)
        if not self.profile_settings.scripts:
            ctk.CTkLabel(
                self._script_container,
                text="(none)", text_color="#888888", anchor="w",
            ).pack(fill="x", padx=10, pady=5)
        for idx, s in enumerate(self.profile_settings.scripts):
            self._build_script_row(idx, s)
        n_opaque = len(self.profile_settings.other_startup)
        if n_opaque:
            self._other_startup_label.configure(
                text=(
                    f"{n_opaque} other JMRI startup items (PanelPro.xml, "
                    "Beantable, SystemConsole, ...) are preserved on Save."
                ),
            )
        else:
            self._other_startup_label.configure(text="")

    def _build_connection_row(  # noqa: PLR0915 -- GUI layout, each statement is a widget
        self, idx: int, c: ConnectionDef,
    ) -> None:
        """One connection as a bordered frame with a collapsible body.

        Header (always visible): expand/collapse toggle, name + type
        summary, Set-Default checkbox, Delete button. Body (hidden
        when collapsed): type dropdown + per-driver settings fields +
        passthrough fields for unknown keys. Collapsed state is
        remembered per-row across repaints so a Save or Add doesn't
        re-expand every row.
        """
        row_state = self._conn_row_state.get(idx, {"expanded": False})
        self._conn_row_state[idx] = row_state

        frame = ctk.CTkFrame(self._conn_container, border_width=1)
        frame.pack(fill="x", padx=5, pady=4)

        # Header row (always visible).
        header = ctk.CTkFrame(frame)
        header.pack(fill="x", padx=5, pady=5)
        toggle_btn = ctk.CTkButton(
            header, text="▶" if not row_state["expanded"] else "▼",
            width=30, command=lambda i=idx: self._on_toggle_connection(i),
        )
        toggle_btn.pack(side="left", padx=(0, 10))
        default_tag = "  [default]" if c.default else ""
        ctk.CTkLabel(
            header,
            text=f"{c.name or '(unnamed)'}  —  type: {c.type}{default_tag}",
            font=("", 12, "bold"), anchor="w",
        ).pack(side="left", fill="x", expand=True)
        default_var = ctk.BooleanVar(value=c.default)
        ctk.CTkCheckBox(
            header, text="Default", variable=default_var,
            command=lambda i=idx: self._on_set_connection_default(i),
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            header, text="Delete", width=70, fg_color="#a04040",
            command=lambda i=idx: self._on_delete_connection(i),
        ).pack(side="right", padx=5)

        # Body (shown only when expanded). Name + type dropdown are
        # here, not in the header, because they're EDITABLE and need
        # the body's always-rebuilt-on-change widgets to pick up the
        # right schema.
        setting_vars: dict[str, Any] = {}
        name_entry: ctk.CTkEntry
        type_var = ctk.StringVar(value=c.type)
        if row_state["expanded"]:
            name_row = ctk.CTkFrame(frame)
            name_row.pack(fill="x", padx=15, pady=2)
            ctk.CTkLabel(name_row, text="Name:", width=60).pack(side="left")
            name_entry = ctk.CTkEntry(name_row, width=200)
            name_entry.insert(0, c.name)
            name_entry.pack(side="left", padx=(0, 10))
            ctk.CTkLabel(name_row, text="Type:", width=50).pack(side="left")
            ctk.CTkOptionMenu(
                name_row, variable=type_var, values=list(CONNECTION_TYPES), width=140,
                command=lambda _value, i=idx: self._on_change_connection_type(i),
            ).pack(side="left")

            schema = _schema_for(c.type)
            # Serial-port dropdown is only useful when the row is
            # actually in "serial" mode -- a tcp openlcb connection
            # has no USB device to pick. Decided once per row so the
            # "port" rendering loop below can branch on it.
            kind_setting = c.settings.get("kind")
            serial_mode = (
                str(kind_setting).lower() == "serial" if kind_setting is not None
                else any(s[0] == "kind" and s[2] == "serial" for s in schema)
            )
            for key, label, default, choices in schema:
                row = ctk.CTkFrame(frame)
                row.pack(fill="x", padx=15, pady=2)
                ctk.CTkLabel(row, text=f"{label}:", width=180, anchor="w").pack(side="left")
                current = c.settings.get(key)
                value_str = str(current) if current is not None else default
                # Case-3 feedback: pick serial device from an
                # auto-discovered list rather than typing e.g.
                # "/dev/ttyUSB0" from memory. Falls back to a free-text
                # entry when pyserial isn't installed or no ports
                # exist (headless CI, no USB adapters plugged in).
                if key == "port" and serial_mode:
                    ports = _list_serial_ports()
                    if ports:
                        var = ctk.StringVar(
                            value=value_str if value_str in ports else ports[0],
                        )
                        ctk.CTkOptionMenu(
                            row, variable=var, values=ports, width=200,
                        ).pack(side="left", fill="x", expand=True)
                        setting_vars[key] = var
                        continue
                if choices:
                    var = ctk.StringVar(
                        value=value_str if value_str in choices else choices[0],
                    )
                    ctk.CTkOptionMenu(
                        row, variable=var, values=list(choices), width=200,
                    ).pack(side="left", fill="x", expand=True)
                    setting_vars[key] = var
                else:
                    entry = ctk.CTkEntry(row)
                    entry.insert(0, value_str)
                    entry.pack(side="left", fill="x", expand=True)
                    setting_vars[key] = entry

            # Unknown-key passthrough -- survives edits even when the
            # schema doesn't know about the field.
            schema_keys = {k for k, _, _, _ in schema}
            for key, value in sorted(c.settings.items()):
                if key in schema_keys:
                    continue
                row = ctk.CTkFrame(frame)
                row.pack(fill="x", padx=15, pady=2)
                ctk.CTkLabel(
                    row, text=f"{key} (extra):", width=180, anchor="w",
                    text_color="#888888",
                ).pack(side="left")
                entry = ctk.CTkEntry(row)
                entry.insert(0, str(value))
                entry.pack(side="left", fill="x", expand=True)
                setting_vars[key] = entry
        else:
            # Collapsed -- stash dummy widgets so commit still works;
            # they return the stored model values verbatim.
            name_entry = _StubEntry(c.name)  # type: ignore[assignment]
            for key, value in c.settings.items():
                setting_vars[key] = _StubEntry(str(value) if value is not None else "")

        self._conn_rows.append({
            "name": name_entry,
            "type": type_var,
            "default": default_var,
            "settings": setting_vars,
            "pmri_class": c.pmri_class,  # preserve for round-trip
        })

    def _build_script_row(self, idx: int, s: ScriptDef) -> None:
        """One script row: name + dir entries + Up / Down / Delete."""
        frame = ctk.CTkFrame(self._script_container, border_width=1)
        frame.pack(fill="x", padx=5, pady=3)
        row = ctk.CTkFrame(frame)
        row.pack(fill="x", padx=5, pady=5)
        ctk.CTkLabel(row, text="File:", width=40).pack(side="left")
        name_entry = ctk.CTkEntry(row, width=200)
        name_entry.insert(0, s.name)
        name_entry.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(row, text="Dir:", width=30).pack(side="left")
        dir_entry = ctk.CTkEntry(row, width=120)
        dir_entry.insert(0, s.dir or "")
        dir_entry.pack(side="left", padx=(0, 10))
        up_disabled = "disabled" if idx == 0 else "normal"
        down_disabled = (
            "disabled" if idx == len(self.profile_settings.scripts) - 1 else "normal"
        )
        ctk.CTkButton(
            row, text="↑", width=30, state=up_disabled,
            command=lambda i=idx: self._on_move_script(i, -1),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            row, text="↓", width=30, state=down_disabled,
            command=lambda i=idx: self._on_move_script(i, 1),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            row, text="Delete", width=70, fg_color="#a04040",
            command=lambda i=idx: self._on_delete_script(i),
        ).pack(side="right", padx=5)
        self._script_rows.append({"name": name_entry, "dir": dir_entry})

    # ---- profile tab: model edits ------------------------------------

    def _commit_profile_rows_to_model(self) -> None:
        """Walk the current widgets and rebuild
        ``self.profile_settings.connections`` + ``.scripts``. Called
        before every structural edit (Add / Delete / reorder) so the
        operator's in-progress text edits aren't lost when we repaint."""
        new_conns: list[ConnectionDef] = []
        for row in self._conn_rows:
            name = row["name"].get()
            dtype = row["type"].get()
            default = bool(row["default"].get())
            settings: dict[str, Any] = {}
            for key, widget in row["settings"].items():
                # Both CTkEntry.get() and StringVar.get() return the
                # current value; one call covers both widget kinds.
                raw = str(widget.get()).strip()
                if raw == "":
                    continue
                settings[key] = _coerce_setting_value_live(raw)
            new_conns.append(ConnectionDef(
                name=name, type=dtype, settings=settings, default=default,
                pmri_class=row.get("pmri_class", "") or "",
            ))
        new_scripts: list[ScriptDef] = []
        for row in self._script_rows:
            name = row["name"].get().strip()
            if not name:
                continue
            dir_v = row["dir"].get().strip() or None
            new_scripts.append(ScriptDef(name=name, dir=dir_v))
        self.profile_settings.connections = new_conns
        self.profile_settings.scripts = new_scripts

    def _on_toggle_connection(self, idx: int) -> None:
        """Flip the collapsed/expanded flag on connection row *idx*
        and repaint. The commit step preserves whatever's in the
        other rows' widgets so toggling one row doesn't lose
        in-progress edits on another."""
        self._commit_profile_rows_to_model()
        state = self._conn_row_state.setdefault(idx, {"expanded": False})
        state["expanded"] = not state["expanded"]
        self._repaint_profile()

    def _on_add_connection(self) -> None:
        """Append a new ConnectionDef with default-type ``internal``
        so a fresh row doesn't look like a broken NCE install. The
        operator picks the real type via the dropdown. The new row
        opens expanded so the fields the operator came here to
        edit are visible without an extra click."""
        self._commit_profile_rows_to_model()
        self.profile_settings.connections.append(ConnectionDef(
            name="new-connection", type="internal",
            default=not self.profile_settings.connections,
        ))
        new_idx = len(self.profile_settings.connections) - 1
        self._conn_row_state[new_idx] = {"expanded": True}
        self._repaint_profile()

    def _on_delete_connection(self, idx: int) -> None:
        self._commit_profile_rows_to_model()
        if 0 <= idx < len(self.profile_settings.connections):
            del self.profile_settings.connections[idx]
        self._repaint_profile()

    def _on_change_connection_type(self, idx: int) -> None:
        """The type dropdown changed -- commit current row values,
        rewrite the system prefix to match the new type's convention,
        and repaint so the new type's schema fields render.

        Overwriting ``systemPrefix`` is deliberate: Case-3 feedback
        was that selecting "nce" after picking "internal" (the Add
        default) left the stored prefix at "I" because the committed
        row's settings dict already had that key populated with the
        internal default. The operator can still edit the prefix
        after the repaint; they just no longer need to know the
        letter-per-type convention.
        """
        self._commit_profile_rows_to_model()
        if 0 <= idx < len(self.profile_settings.connections):
            conn = self.profile_settings.connections[idx]
            default_prefix = _DEFAULT_SYSTEM_PREFIX.get(conn.type)
            if default_prefix is not None:
                conn.settings["systemPrefix"] = default_prefix
        self._repaint_profile()

    def _on_set_connection_default(self, idx: int) -> None:
        """Only one connection can be the default; clicking a
        checkbox turns off the others. Mirrors radio-button
        semantics without the radio-button widget's layout issues."""
        self._commit_profile_rows_to_model()
        for i, c in enumerate(self.profile_settings.connections):
            c.default = (i == idx)
        self._repaint_profile()

    def _on_add_script(self) -> None:
        self._commit_profile_rows_to_model()
        self.profile_settings.scripts.append(ScriptDef(name="", dir="jython"))
        self._repaint_profile()

    def _on_delete_script(self, idx: int) -> None:
        self._commit_profile_rows_to_model()
        if 0 <= idx < len(self.profile_settings.scripts):
            del self.profile_settings.scripts[idx]
        self._repaint_profile()

    def _on_move_script(self, idx: int, delta: int) -> None:
        self._commit_profile_rows_to_model()
        new_idx = idx + delta
        if 0 <= new_idx < len(self.profile_settings.scripts):
            scripts = self.profile_settings.scripts
            scripts[idx], scripts[new_idx] = scripts[new_idx], scripts[idx]
        self._repaint_profile()

    def _on_save_profile(self) -> None:
        """Commit the current widget state and write profile.xml.
        Uses the rolling-backup writer, so a bad edit can be
        recovered from ``profile.xml.bak``.

        Refuses to run until the PMRI Options tab has been saved at
        least once this install (Case-3 feedback: writing profile.xml
        at a stale profile name was leaving a ``~/.jmri/default/``
        orphan behind the rename).
        """
        if not self._pmri_saved:
            self.profile_status.configure(
                text=(
                    "Save PMRI Options first so the profile directory is "
                    "settled."
                ),
                text_color="#d04040",
            )
            return
        self._commit_profile_rows_to_model()
        try:
            save_profile_xml(self.profile, self.profile_settings)
            self._register_profile_in_profiles_xml()
        except OSError as e:
            self.profile_status.configure(
                text=f"save failed: {e}", text_color="#d04040",
            )
            return
        self.profile_status.configure(
            text=f"profile.xml saved under {self.profile.profile_dir}.",
            text_color="#2a9d2a",
        )

    def _register_profile_in_profiles_xml(self) -> None:
        """Ensure ``<settings>/profiles.xml`` lists the just-saved
        profile and mark it active via the ``.active_profile`` marker.

        For a Case-3 new install the registry file doesn't exist yet;
        we create it with just this one entry. For a Case-1 install
        the file already lists existing profiles -- we add the new
        profile if missing and leave the rest untouched. Then we
        write the ``.active_profile`` marker so the next launch (and
        the running server) resolves to the same profile the operator
        just saved.
        """
        registry = load_profile_registry(self.settings_root)
        profile_name = self.profile.name
        existing = registry.by_id(profile_name) or registry.by_name(profile_name)
        if existing is None:
            registry.profiles.append(ProfileRef(
                id=profile_name,
                path=f"settings:{profile_name}/",
                name=profile_name,
            ))
            save_profile_registry(registry)
            active_id = profile_name
        else:
            active_id = existing.id
        # set_active validates against the just-saved registry, so
        # reload before marking (save_profile_registry above may have
        # added the entry we need to point at).
        registry = load_profile_registry(self.settings_root)
        try:
            registry.set_active(active_id)
        except KeyError:
            # Shouldn't happen -- we just wrote the entry. Surface
            # anyway so a stale cache can't silently leave the marker
            # at a different profile.
            _log.warning(
                "profile %r missing from freshly-saved profiles.xml; "
                ".active_profile marker not updated", active_id,
            )

    def _refresh_profile_tab_binding(self) -> None:
        """Resync the Startup Options tab's header + Save-button state
        with the current :attr:`profile` and :attr:`_pmri_saved`.

        Called from ``__init__`` (via :meth:`_build_profile_tab`) and
        after :meth:`_on_save_pmri` rebinds the profile. Guarded on
        attribute existence because ``_build_profile_tab`` runs after
        ``_build_pmri_tab`` -- during the first pmri Save (from
        ``__init__``'s initial repaint path), the profile widgets may
        not exist yet.
        """
        label = getattr(self, "profile_header_label", None)
        if label is not None:
            label.configure(text=f"Profile: {self.profile.name}")
        button = getattr(self, "profile_save_button", None)
        status = getattr(self, "profile_status", None)
        if button is not None:
            button.configure(state="normal" if self._pmri_saved else "disabled")
        if status is not None and not self._pmri_saved:
            status.configure(
                text="Save PMRI Options first to enable profile.xml saves.",
                text_color="#888888",
            )

    # ---- actions -------------------------------------------------------

    def _on_start(self) -> None:
        self.error_label.configure(text="")
        self.controller.start()
        if self.open_browser_on_start:
            threading.Thread(
                target=open_browser_when_ready, args=(self.controller,),
                daemon=True, name="pmri-browser-opener",
            ).start()
            # Case-1 feedback: on some Linux desktop setups, chromium
            # ignores the webbrowser.open_new_tab call silently. Give
            # the operator a user-visible hint about the Open UI
            # button so they aren't stuck wondering why nothing
            # happened.
            self.hint_label.configure(
                text=(
                    "If the browser didn't open automatically, click "
                    "\"Open UI\" to launch it manually."
                ),
            )

    def _on_stop(self) -> None:
        self.controller.stop()
        self.hint_label.configure(text="")

    def _on_open_browser(self) -> None:
        """Explicit Open-UI click -- works whether we auto-opened on
        start or not. ``webbrowser.open_new_tab`` on an already-running browser
        opens a new tab; on a non-running one it launches."""
        import webbrowser  # noqa: PLC0415
        webbrowser.open(self.controller.url)

    def _on_save_pmri(self) -> None:
        """Mutate ``self.cfg`` from the entry widgets, then write.
        Rolling backup is applied by save_pmri_config -- no need to
        snapshot the file ourselves.

        Rebinds :attr:`settings_root` and :attr:`profile` in place
        when the operator changed either, so the Startup Options tab
        then saves profile.xml under the new path. Flips
        :attr:`_pmri_saved` so the Startup Options Save button
        becomes enabled.
        """
        try:
            raw_root = self._pmri_fields["Settings directory"].get().strip()
            if not raw_root:
                raise ValueError("Settings directory must not be empty")
            new_root = pathlib.Path(raw_root).expanduser()
            self.cfg.http.host = self._pmri_fields["HTTP host"].get()
            self.cfg.http.port = int(self._pmri_fields["HTTP port"].get())
            self.cfg.withrottle.host = self._pmri_fields["WiThrottle host"].get()
            self.cfg.withrottle.port = int(self._pmri_fields["WiThrottle port"].get())
            self.cfg.log.level = self._pmri_log_level_var.get()
            raw_log = self._pmri_fields["Log file"].get().strip()
            self.cfg.log.file = pathlib.Path(raw_log).expanduser() if raw_log else None
            new_profile_name = self._pmri_fields["Active profile"].get().strip()
            self.cfg.active_profile = new_profile_name
            self.cfg.launcher.open_browser_on_start = bool(
                self._pmri_browser_var.get(),
            )
            self.cfg.auth.enabled = bool(self._pmri_auth_enabled_var.get())
            self.cfg.auth.ephemeral_secret = bool(
                self._pmri_auth_ephemeral_var.get(),
            )
            self.cfg.auth.secret = self._pmri_fields["Auth secret"].get().strip()
            self.cfg.auth.admin_token = (
                self._pmri_fields["Auth admin token"].get().strip()
            )
            # XML roots: empty -> None (don't publish env var).
            raw_signal = self._pmri_fields["Signal XML root"].get().strip()
            self.cfg.xml_roots.signal = (
                pathlib.Path(raw_signal).expanduser() if raw_signal else None
            )
            raw_decoder = self._pmri_fields["Decoder XML root"].get().strip()
            self.cfg.xml_roots.decoder = (
                pathlib.Path(raw_decoder).expanduser() if raw_decoder else None
            )
            # Console-echo tri-state: map the dropdown choice back
            # to None / True / False.
            console_choice = self._pmri_log_console_var.get()
            self.cfg.log.console_echo = {
                "Auto (echo iff no file)": None,
                "Always": True,
                "Never": False,
            }.get(console_choice)
        except ValueError as e:
            self.pmri_status.configure(text=f"save failed: {e}", text_color="#d04040")
            return
        # Rebind settings_root / profile BEFORE writing so pmri.xml
        # lands where the operator asked. active_profile falling back
        # to a non-empty "default" keeps a Case-3 user who clears the
        # field from accidentally wiping the stored name.
        effective_profile_name = new_profile_name or self.profile.name or "default"
        root_changed = new_root != self.settings_root
        name_changed = effective_profile_name != self.profile.name
        self.settings_root = new_root
        if root_changed or name_changed:
            self.profile = Profile(root=new_root, name=effective_profile_name)
        save_pmri_config(self.settings_root, self.cfg)
        self._pmri_saved = True
        # Repaint the Startup Options header so its "Profile: ..." label
        # tracks the just-saved profile name, and so the Save button
        # becomes enabled.
        self._refresh_profile_tab_binding()
        self.pmri_status.configure(text="saved.", text_color="#2a9d2a")

    def _on_reload_profile(self) -> None:
        """Re-read profile.xml from disk -- discards unsaved edits and
        reloads what's on disk. Useful after JMRI (or an operator in
        a text editor) has changed the file externally. The service
        doesn't need to restart for the editor to refresh, but any
        connection change takes effect on next service start."""
        from pmri_core.persistence import load_profile_xml  # noqa: PLC0415
        self.profile_settings = load_profile_xml(self.profile)
        self._repaint_profile()
        self.profile_status.configure(
            text="profile.xml reloaded from disk.", text_color="#4a9eff",
        )

    def _show_first_launch_dialog(self) -> None:
        """Modal dialog shown once when pmri.xml doesn't exist.

        Walks the operator through the two-tab setup: PMRI Options
        for the install-level config (ports, log level, etc.) and
        Startup Options to verify the connection + script list the
        service brings up. Dismissed with OK; dialog never comes
        back for that install (pmri.xml exists after the operator's
        first Save)."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Welcome to PMRI")
        dialog.geometry("520x300")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text="Welcome to PMRI", font=("", 18, "bold"),
        ).pack(pady=(20, 10))
        ctk.CTkLabel(
            dialog, wraplength=480, justify="left",
            text=(
                "This looks like a first-time launch -- no pmri.xml was "
                "found at:\n\n"
                f"    {pmri_config_path(self.settings_root)}\n\n"
                "To finish setup:\n\n"
                "1. Open the \"PMRI Options\" tab and Save -- this writes "
                "the install-level config (ports, log settings, browser "
                "preference) with the defaults, and you can edit any of "
                "them before saving.\n\n"
                "2. Open the \"Startup Options\" tab and verify the "
                "connection list + script-launch order the service will "
                "bring up when Started. These settings live in JMRI's "
                "profile.xml -- the same file JMRI uses -- so JMRI and "
                "PMRI stay in sync. (If you were already a JMRI user, "
                "your existing connections and startup scripts have "
                "been picked up automatically.)"
            ),
        ).pack(padx=20, pady=10, fill="both", expand=True)
        ctk.CTkButton(
            dialog, text="OK", width=120, command=dialog.destroy,
        ).pack(pady=10)

    def _on_close(self) -> None:
        """Window-close handler: stop the service, then exit the
        mainloop. Matches the spec's "window owns the service"
        semantic."""
        if self.controller.state != ServiceState.STOPPED:
            self.controller.stop()
        self.destroy()

    # ---- status polling ------------------------------------------------

    def _poll_status(self) -> None:
        """Tk's after() callback -- repaints the service tab from
        ``controller.state`` and reschedules every 300 ms. Cheap
        enough to run forever."""
        state = self.controller.state
        self.status_dot.configure(text_color=_STATUS_COLORS[state])
        self.status_text.configure(text=state.value.title())

        can_start = state == ServiceState.STOPPED
        can_stop = state == ServiceState.STARTED
        self.start_btn.configure(state="normal" if can_start else "disabled")
        self.stop_btn.configure(state="normal" if can_stop else "disabled")
        self.open_btn.configure(state="normal" if can_stop else "disabled")

        if state == ServiceState.STARTED:
            self.url_label.configure(text=self.controller.url)
        else:
            self.url_label.configure(text="")
        if self.controller.last_error:
            self.error_label.configure(text=self.controller.last_error)

        self.after(300, self._poll_status)
