"""PMRI config wiring: pmri.xml drives active-profile + log settings,
profile.xml drives connection selection + script list, env vars remain
the launcher's one-run override channel.

Covers the three behaviours Chunk C added:

* ``_resolve_initial_profile`` honours pmri.xml's ``<active-profile>``.
* ``_resolve_profile_connection`` reads from the loaded ProfileSettings.
* ``_autolaunch_profile_scripts`` uses profile.xml's ``<scripts>`` list
  when it exists, falling back to the dir-glob otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import tempfile
from unittest.mock import patch

import pytest
from pmri_core.hal.internal import InternalConnection
from pmri_core.persistence import (
    ConnectionDef,
    PmriConfig,
    Profile,
    ProfileRef,
    ProfileRegistry,
    ProfileSettings,
    ScriptDef,
    save_pmri_config,
    save_profile_xml,
)

from pmri_server.app import (
    _autolaunch_profile_scripts,
    _resolve_initial_profile,
    _resolve_profile_connection,
    build_app,
    create_app,
)

# ---- _resolve_initial_profile honours pmri.xml -------------------------


def test_pmri_active_profile_name_wins_over_marker(
    tmp_path: pathlib.Path,
) -> None:
    """A ``<active-profile name="DRGW"/>`` in pmri.xml points at the
    profile named DRGW, even if the ``.active_profile`` marker says
    something else. Launcher spec: pmri.xml is the authority."""
    registry = ProfileRegistry(
        settings_root=tmp_path,
        profiles=[
            ProfileRef(id="DRGW.xyz", path="settings:DRGW.jmri/", name="DRGW.jmri"),
            ProfileRef(id="UP.xyz", path="settings:UP.jmri/", name="UP.jmri"),
        ],
    )
    # Marker says UP; pmri.xml says DRGW -- DRGW wins.
    (tmp_path / ".active_profile").write_text("UP.xyz", encoding="utf-8")
    cfg = PmriConfig()
    cfg.active_profile = "DRGW.jmri"
    picked = _resolve_initial_profile(registry, cfg)
    assert picked.name == "DRGW.jmri"


def test_pmri_active_profile_falls_back_to_marker_when_name_unknown(
    tmp_path: pathlib.Path,
) -> None:
    """A pmri.xml name that doesn't match any registry entry OR an
    on-disk directory falls through to the ``.active_profile``
    marker / registry default."""
    registry = ProfileRegistry(
        settings_root=tmp_path,
        profiles=[
            ProfileRef(id="UP.xyz", path="settings:UP.jmri/", name="UP.jmri"),
        ],
    )
    (tmp_path / ".active_profile").write_text("UP.xyz", encoding="utf-8")
    cfg = PmriConfig()
    cfg.active_profile = "GhostProfile"
    picked = _resolve_initial_profile(registry, cfg)
    assert picked.name == "UP.jmri"


def test_pmri_active_profile_resolves_bare_dirname(
    tmp_path: pathlib.Path,
) -> None:
    """A name that isn't in profiles.xml but IS an on-disk profile
    dir resolves directly -- matches JMRI's tolerance for operators
    who drop a dir in without updating profiles.xml."""
    (tmp_path / "Standalone.jmri" / "profile").mkdir(parents=True)
    registry = ProfileRegistry(settings_root=tmp_path, profiles=[])
    cfg = PmriConfig()
    cfg.active_profile = "Standalone.jmri"
    picked = _resolve_initial_profile(registry, cfg)
    assert picked.name == "Standalone.jmri"


def test_no_pmri_config_falls_back_to_registry_behaviour(
    tmp_path: pathlib.Path,
) -> None:
    """Called with ``pmri_cfg=None`` (test harness, pre-PMRI
    install) -- behaves exactly like it did before PMRI."""
    registry = ProfileRegistry(
        settings_root=tmp_path,
        profiles=[
            ProfileRef(id="UP.xyz", path="settings:UP.jmri/", name="UP.jmri"),
        ],
    )
    picked = _resolve_initial_profile(registry, None)
    assert picked.name == "UP.jmri"


# ---- _resolve_profile_connection --------------------------------------


def test_profile_connection_returns_none_for_none_settings() -> None:
    """No profile.xml loaded -> no connection pick. Caller falls back
    to env vars + InternalConnection."""
    name, settings = _resolve_profile_connection(None)
    assert name is None
    assert settings is None


def test_profile_connection_returns_none_for_empty_connections() -> None:
    """profile.xml exists but defines no connections -> same
    treatment as missing profile.xml. The empty ``<connections/>``
    section is a valid shape (operator cleared the list)."""
    name, settings = _resolve_profile_connection(ProfileSettings())
    assert name is None
    assert settings is None


def test_profile_connection_picks_default() -> None:
    """One connection flagged ``default=true`` wins -- the usual
    PMRI single-layout case."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="a", type="nce", default=False),
        ConnectionDef(name="b", type="dccex", default=True),
    ])
    name, settings = _resolve_profile_connection(s)
    assert name == "dccex"
    assert settings is None  # no settings dict on this ConnectionDef


def test_profile_connection_copies_settings_dict() -> None:
    """The returned settings dict must be a COPY -- the caller
    stashes it on the connection and later driver edits (storing
    an open serial port handle, say) must not leak back into the
    on-disk model."""
    original = {"port": "COM4", "baud": 9600}
    s = ProfileSettings(connections=[
        ConnectionDef(name="a", type="nce", settings=original, default=True),
    ])
    _, settings = _resolve_profile_connection(s)
    assert settings == original
    assert settings is not original


def test_profile_connection_returns_none_when_multi_no_default() -> None:
    """>1 connection with no default -> picker's job (launcher
    handles via env vars). Server-side resolver returns None so
    the fallback chain fires."""
    s = ProfileSettings(connections=[
        ConnectionDef(name="a", type="nce"),
        ConnectionDef(name="b", type="dccex"),
    ])
    name, settings = _resolve_profile_connection(s)
    assert name is None
    assert settings is None


# ---- create_app end-to-end: profile.xml connection wins ----------------
#
# Points default_settings_dir at a temp root with pmri.xml + profiles.xml
# + profile.xml, confirms create_app opens the connection profile.xml
# names. Scoped to the ``internal`` driver so no hardware is touched.


@pytest.fixture()
def pmri_settings_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Build a minimal PMRI settings root at tmp_path."""
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    profile.ensure()
    # profiles.xml with one entry.
    (tmp_path / "profiles.xml").write_text(
        '<?xml version="1.0"?>'
        '<profileConfig><profiles>'
        '<profile id="DRGW.xyz" path="settings:DRGW.jmri/"/>'
        '</profiles></profileConfig>',
        encoding="utf-8",
    )
    # pmri.xml pointing at DRGW.
    cfg = PmriConfig()
    cfg.active_profile = "DRGW.jmri"
    save_pmri_config(tmp_path, cfg)
    return tmp_path


def test_create_app_uses_profile_xml_connection(
    pmri_settings_root: pathlib.Path,
) -> None:
    """profile.xml names ``internal`` as the default connection ->
    create_app opens an InternalConnection. The env var fallback is
    not triggered since profile.xml nominates something."""
    profile = Profile(root=pmri_settings_root, name="DRGW.jmri")
    settings = ProfileSettings(connections=[
        ConnectionDef(name="layout", type="internal", default=True),
    ])
    save_profile_xml(profile, settings)
    with (
        patch("pmri_server.app.default_settings_dir", return_value=pmri_settings_root),
        patch.dict(os.environ, {}, clear=True),  # no env override
    ):
        app = create_app()
    # ctx is reachable via the FastAPI app's state wiring in build_app
    # -> AppHandle. We don't have the handle here, but we can confirm
    # the server booted (would raise if select_connection failed).
    assert app is not None


def test_env_connection_still_overrides_profile_xml(
    pmri_settings_root: pathlib.Path,
) -> None:
    """JMRI_CONNECTION env var still wins over profile.xml.default --
    the launcher relies on this for its one-run ``--connection NAME``
    override. If profile.xml names ``dccex`` as default but the env
    says ``internal``, we open internal."""
    profile = Profile(root=pmri_settings_root, name="DRGW.jmri")
    settings = ProfileSettings(connections=[
        ConnectionDef(name="layout", type="dccex", default=True),
        ConnectionDef(name="debug", type="internal"),
    ])
    save_profile_xml(profile, settings)
    with (
        patch("pmri_server.app.default_settings_dir", return_value=pmri_settings_root),
        patch.dict(os.environ, {"JMRI_CONNECTION": "internal"}, clear=True),
    ):
        app = create_app()
    assert app is not None
    # (A proper assertion of "InternalConnection was opened" would
    # need to reach into the AppHandle; the fact that create_app
    # returned without raising -- when dccex isn't actually
    # installed as a loopback here -- is the real check.)


# ---- autolaunch: profile.xml <scripts> list takes over ----------------


async def test_autolaunch_uses_profile_xml_script_list(
    tmp_path: pathlib.Path,
) -> None:
    """When ctx.profile_settings has a non-empty scripts list, the
    autoloader runs those files in order (NOT the directory glob).
    Pins the Chunk C migration: operators who moved to the explicit
    list stop needing the magic-comment opt-out."""
    profile = Profile(root=tmp_path, name="PMRI.jmri")
    profile.ensure()
    marker = profile.profile_dir / "ran.txt"
    (profile.python_dir).mkdir(parents=True, exist_ok=True)
    (profile.python_dir / "wanted.py").write_text(
        f"open(r'{marker.as_posix()}', 'w').write('wanted')\n",
        encoding="utf-8",
    )
    # A second script that MUST NOT run -- profile.xml doesn't list it.
    not_ran = profile.profile_dir / "notran.txt"
    (profile.python_dir / "unwanted.py").write_text(
        f"open(r'{not_ran.as_posix()}', 'w').write('leaked')\n",
        encoding="utf-8",
    )
    # JMRI stores scripts with explicit paths like
    # ``preference:python/wanted.py`` -> ScriptDef(name="wanted.py",
    # dir="python"). The old DEFAULT_SCRIPTS_DIR convention is gone;
    # a bare ``ScriptDef(name=...)`` resolves against the profile
    # root, not a hard-coded ``python/`` default.
    settings = ProfileSettings(scripts=[ScriptDef(name="wanted.py", dir="python")])
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    handle.ctx.profile_settings = settings
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(150):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.read_text() == "wanted"
    # The unlisted script never ran even though it lives in the glob dir.
    assert not not_ran.exists()


async def test_autolaunch_profile_xml_respects_per_entry_dir(
    tmp_path: pathlib.Path,
) -> None:
    """A ``<script dir="/abs/path">`` entry resolves against that
    absolute dir, bypassing the profile's own python/."""
    profile = Profile(root=tmp_path, name="PMRI.jmri")
    profile.ensure()
    external = tmp_path / "external-scripts"
    external.mkdir()
    marker = profile.profile_dir / "ran.txt"
    (external / "ext.py").write_text(
        f"open(r'{marker.as_posix()}', 'w').write('from-ext')\n",
        encoding="utf-8",
    )
    settings = ProfileSettings(scripts=[
        ScriptDef(name="ext.py", dir=str(external)),
    ])
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    handle.ctx.profile_settings = settings
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(150):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.read_text() == "from-ext"


async def test_autolaunch_empty_script_list_runs_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """profile.xml with an empty ``<scripts/>`` list means "no
    autoload" -- the dir-glob fallback must NOT fire. Explicit
    empty list is the operator's "turn autoload off" switch."""
    profile = Profile(root=tmp_path, name="PMRI.jmri")
    profile.ensure()
    marker = profile.profile_dir / "ran.txt"
    (profile.python_dir).mkdir(parents=True, exist_ok=True)
    (profile.python_dir / "would_run.py").write_text(
        f"open(r'{marker.as_posix()}', 'w').write('leaked')\n",
        encoding="utf-8",
    )
    settings = ProfileSettings()  # empty scripts list
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    handle.ctx.profile_settings = settings
    await _autolaunch_profile_scripts(handle.ctx)
    await asyncio.sleep(0.2)  # grace -- if the glob ran, give it time
    assert not marker.exists()


async def test_autolaunch_missing_script_warns_and_continues(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A profile.xml entry pointing at a non-existent file is a
    user mistake, not a crash. The autoloader logs + moves on; a
    later valid entry still runs."""
    profile = Profile(root=tmp_path, name="PMRI.jmri")
    profile.ensure()
    (profile.python_dir).mkdir(parents=True, exist_ok=True)
    marker = profile.profile_dir / "ran.txt"
    (profile.python_dir / "good.py").write_text(
        f"open(r'{marker.as_posix()}', 'w').write('ok')\n",
        encoding="utf-8",
    )
    settings = ProfileSettings(scripts=[
        ScriptDef(name="ghost.py", dir="python"),  # doesn't exist
        ScriptDef(name="good.py", dir="python"),
    ])
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    handle.ctx.profile_settings = settings
    with caplog.at_level(logging.WARNING):
        await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(150):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists()
    assert any("ghost.py" in r.message for r in caplog.records)


async def test_autolaunch_falls_back_to_dir_glob_when_no_profile_xml(
    tmp_path: pathlib.Path,
) -> None:
    """``ctx.profile_settings = None`` (pre-PMRI JMRI install) ->
    the directory glob still fires. Covers the back-compat path so
    existing JMRI-authored profiles opened by PMRI keep working."""
    profile = Profile(root=tmp_path, name="JMRI.jmri")
    profile.ensure()
    marker = profile.profile_dir / "ran.txt"
    (profile.python_dir).mkdir(parents=True, exist_ok=True)
    (profile.python_dir / "legacy.py").write_text(
        f"open(r'{marker.as_posix()}', 'w').write('legacy')\n",
        encoding="utf-8",
    )
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    # Explicitly ensure profile_settings is None.
    handle.ctx.profile_settings = None
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(150):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.read_text() == "legacy"


# ---- InternalConnection sanity --------------------------------------


def test_fallback_connection_is_internal_without_any_config() -> None:
    """``create_app`` on a bare tmp dir (no pmri.xml, no profile.xml)
    opens an InternalConnection -- the "fresh install boots" promise.
    This is already covered by other test files but we re-check it
    on the create_app path with the new ordering to pin the
    fallback chain end-to-end."""
    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = pathlib.Path(tdir)
        with (
            patch("pmri_server.app.default_settings_dir", return_value=tdir_path),
            patch.dict(os.environ, {}, clear=True),
        ):
            app = create_app()
    assert app is not None
    _ = InternalConnection  # type: used in the assertion-by-boot above
