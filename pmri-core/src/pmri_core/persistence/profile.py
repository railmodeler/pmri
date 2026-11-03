"""``Profile`` -- JMRI's per-railroad data directory.

JMRI's settings root (``~/JMRI`` on Windows, ``~/Library/Preferences/JMRI``
on macOS, ``~/.jmri`` on Linux) holds one or more *profile* directories
named after the railroad -- e.g. ``DRGW.jmri/`` for the D&RGW layout. The
list of known profiles is in ``<settings>/profiles.xml``.

Each profile is a self-contained directory holding:

* ``PanelPro.xml`` -- bean tables (turnouts, sensors, lights, ...) plus
  the rest of the JMRI layout-config (signal heads, blocks, panels,
  LogixNG, etc.). Older installs called this ``Configuration.xml``.
* ``roster.xml`` -- index of locomotives in the roster.
* ``roster/<id>.xml`` -- one file per locomotive (locomotive-config).
* ``rosters/<id>.xml`` -- per-loco CV state (programmer output).
* ``throttle/`` -- saved throttle layouts.
* ``signal/`` -- user signal-mast definitions.
* ``jython/`` -- auto-load Python scripts (Stage 9.5+).
* ``programmers/`` -- programmer XML files.

This module models the layout so persistence code never hard-codes
paths and tests can point at a temp dir. The :class:`ProfileRegistry`
sister class reads ``profiles.xml`` and tracks which profile is
currently active.
"""
from __future__ import annotations

import os
import pathlib
import platform
from dataclasses import dataclass


def default_settings_dir() -> pathlib.Path:
    """OS-appropriate default settings directory matching JMRI's Java behavior.

    Honours the ``JMRI_SETTINGS_DIR`` environment variable when set --
    useful for pointing a Linux host at a Windows-formatted JMRI tree
    (e.g. an SMB-mounted ``//Pi/JMRI`` or an rsync of the dev machine's
    ``%USERPROFILE%\\JMRI``). The read goes through
    :func:`pmri_core.env.settings_dir` so operator-facing env-var
    names live in one registry.
    """
    from pmri_core.env import settings_dir  # noqa: PLC0415 -- avoid an import cycle at module load
    override = settings_dir()
    if override is not None:
        return override
    system = platform.system()
    if system == "Windows":
        # JMRI installer creates %userprofile%\JMRI on Windows.
        return pathlib.Path(os.environ.get("USERPROFILE", "~")).expanduser() / "JMRI"
    if system == "Darwin":
        return pathlib.Path("~/Library/Preferences/JMRI").expanduser()
    # Linux + everything else.
    return pathlib.Path("~/.jmri").expanduser()


@dataclass(slots=True)
class Profile:
    """One JMRI railroad profile.

    Lives at ``<root>/<name>`` (e.g. ``~/JMRI/DRGW.jmri``). The bare name
    is whatever appears in ``profiles.xml`` -- JMRI conventionally
    suffixes it with ``.jmri`` but we don't enforce that.
    """

    root: pathlib.Path
    name: str = "default"

    @classmethod
    def default(cls) -> Profile:
        """Construct using the platform-default settings directory."""
        return cls(root=default_settings_dir())

    # ----- known paths ----------------------------------------------------

    @property
    def profile_dir(self) -> pathlib.Path:
        """``<root>/<name>`` -- the profile's root directory."""
        return self.root / self.name

    @property
    def config_file(self) -> pathlib.Path:
        """The bean-tables file. Prefers ``PanelPro.xml`` (JMRI 5.x) if
        either it exists already; otherwise falls back to the older
        ``Configuration.xml``. Brand-new profiles get ``PanelPro.xml``.
        """
        panel = self.profile_dir / "PanelPro.xml"
        legacy = self.profile_dir / "Configuration.xml"
        if panel.exists():
            return panel
        if legacy.exists():
            return legacy
        return panel

    @property
    def roster_dir(self) -> pathlib.Path:
        """Where per-loco ``locomotive-config`` XML files live."""
        return self.profile_dir / "roster"

    @property
    def roster_index(self) -> pathlib.Path:
        """Roster index -- ``<profile>/roster.xml``, alongside the roster dir."""
        return self.profile_dir / "roster.xml"

    @property
    def rosters_cv_dir(self) -> pathlib.Path:
        """Per-loco CV state files (programmer output). Loaded on demand."""
        return self.profile_dir / "rosters"

    @property
    def throttle_dir(self) -> pathlib.Path:
        return self.profile_dir / "throttle"

    @property
    def signal_dir(self) -> pathlib.Path:
        """Per-profile signal-mast definitions."""
        return self.profile_dir / "signal"

    @property
    def programmers_dir(self) -> pathlib.Path:
        return self.profile_dir / "programmers"

    @property
    def jython_dir(self) -> pathlib.Path:
        """Legacy autoload dir (JMRI Java's Jython 2.7 tree). Kept as a
        model attribute so imports from an older rewrite install still
        parse -- the autoloader no longer scans it. Use :attr:`python_dir`
        for CPython 3 scripts."""
        return self.profile_dir / "jython"

    @property
    def python_dir(self) -> pathlib.Path:
        """Per-profile CPython 3 script dir.

        Autoload sequence (see
        :func:`pmri_server.app._autolaunch_profile_scripts`):

        1. ``<settings-root>/python/`` -- cross-profile shared scripts.
        2. ``<profile>/python/`` -- per-profile scripts.

        Files ending in ``.py`` at the top level run in alphabetical
        order; files starting with ``_`` are treated as private
        modules that other scripts may import but aren't run standalone.
        Missing directories are silently ignored -- running without
        either dir is a valid setup for a bare install.
        """
        return self.profile_dir / "python"

    @property
    def shared_python_dir(self) -> pathlib.Path:
        """Cross-profile CPython 3 script dir at
        ``<settings-root>/python/``. See :attr:`python_dir` for the
        autoload sequence."""
        return self.root / "python"

    @property
    def operations_dir(self) -> pathlib.Path:
        return self.profile_dir / "operations"

    @property
    def panel_dir(self) -> pathlib.Path:
        return self.profile_dir / "panels"

    @property
    def logixng_dir(self) -> pathlib.Path:
        """Where LogixNG programs are stored as YAML files.

        Each program lives at ``<profile>/logixng/<system_name>.yaml``
        so a profile switch swaps the whole automation set atomically.
        Directory is created on first save; loads treat a missing dir
        as "no programs" rather than an error.
        """
        return self.profile_dir / "logixng"

    @property
    def log_dir(self) -> pathlib.Path:
        return self.root / "log"

    @property
    def preferences_dir(self) -> pathlib.Path:
        return self.profile_dir / "profile"

    @property
    def signal_systems_dir(self) -> pathlib.Path:
        """User-defined signal systems shared across profiles."""
        return self.root / "resources" / "signals"

    # ----- helpers ---------------------------------------------------------

    def ensure(self) -> None:
        """Create every standard per-profile subdirectory if missing.

        Idempotent. Does not touch the settings-root level directories
        (those are shared across profiles).
        """
        for d in (
            self.root,
            self.profile_dir,
            self.roster_dir,
            self.rosters_cv_dir,
            self.operations_dir,
            self.panel_dir,
            self.throttle_dir,
            self.signal_dir,
            self.log_dir,
            self.signal_systems_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"Profile(name={self.name!r}, root={str(self.root)!r})"
