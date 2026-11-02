"""Profile-driven script auto-launch (Tier C3, updated for Tier 3
python-dir migration).

When a profile is loaded -- either at server startup or via the
runtime profile-switch path -- every ``*.py`` file under two
directories is queued through the existing :class:`ScriptRunner`,
in this order:

1. ``<settings-root>/python/`` -- shared helpers, run for every profile.
2. ``<profile>/python/`` -- per-profile scripts.

Within each dir, files run in alphabetical order; files starting
with ``_`` are private modules and files starting with ``lib_`` are
library-only (importable, not run standalone). Failures are logged
but don't block the boot.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from fastapi.testclient import TestClient
from jmri_core.persistence import Profile

from jmri_server.app import (
    AppHandle,
    _autolaunch_profile_scripts,
    build_app,
)


def _write_script(dir_path: pathlib.Path, name: str, body: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / name).write_text(body, encoding="utf-8")


@pytest.fixture()
def profile_with_python(tmp_path: pathlib.Path) -> Profile:
    profile = Profile(root=tmp_path, name="DRGW.jmri")
    profile.ensure()
    return profile


async def test_autolaunch_runs_scripts(profile_with_python: Profile) -> None:
    marker = profile_with_python.profile_dir / "marker.txt"
    _write_script(
        profile_with_python.python_dir,
        "00_init.py",
        f"open(r'{marker.as_posix()}', 'w').write('hello')\n",
    )
    handle: AppHandle = build_app()
    handle.ctx._roster_profile = profile_with_python  # type: ignore[attr-defined]
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(50):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists()
    assert marker.read_text() == "hello"


async def test_autolaunch_orders_alphabetically(
    profile_with_python: Profile,
) -> None:
    log = profile_with_python.profile_dir / "log.txt"
    log.write_text("", encoding="utf-8")
    _write_script(
        profile_with_python.python_dir, "10_second.py",
        f"open(r'{log.as_posix()}', 'a').write('B')\n",
    )
    _write_script(
        profile_with_python.python_dir, "00_first.py",
        f"open(r'{log.as_posix()}', 'a').write('A')\n",
    )
    handle = build_app()
    handle.ctx._roster_profile = profile_with_python  # type: ignore[attr-defined]
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(50):
        if len(log.read_text()) == 2:
            break
        await asyncio.sleep(0.02)
    assert log.read_text() == "AB"


async def test_autolaunch_no_python_dir_is_noop(
    tmp_path: pathlib.Path,
) -> None:
    profile = Profile(root=tmp_path, name="empty.jmri")
    # Deliberately don't call ensure() -- python dir won't exist.
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    await _autolaunch_profile_scripts(handle.ctx)


async def test_autolaunch_skips_non_py_files(
    profile_with_python: Profile,
) -> None:
    """Only ``*.py`` is auto-loaded -- README.txt and friends are left alone."""
    marker = profile_with_python.profile_dir / "ran.txt"
    _write_script(
        profile_with_python.python_dir, "real.py",
        f"open(r'{marker.as_posix()}', 'w').write('ok')\n",
    )
    (profile_with_python.python_dir / "README.txt").write_text("not python")
    handle = build_app()
    handle.ctx._roster_profile = profile_with_python  # type: ignore[attr-defined]
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(50):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists()


async def test_failing_script_doesnt_block_others(
    profile_with_python: Profile,
) -> None:
    """One bad script must not stop the rest from running."""
    marker = profile_with_python.profile_dir / "later.txt"
    _write_script(
        profile_with_python.python_dir, "10_fails.py",
        "raise RuntimeError('boom')\n",
    )
    _write_script(
        profile_with_python.python_dir, "20_works.py",
        f"open(r'{marker.as_posix()}', 'w').write('ok')\n",
    )
    handle = build_app()
    handle.ctx._roster_profile = profile_with_python  # type: ignore[attr-defined]
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(50):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists()


def test_lifespan_autolaunches_scripts(
    profile_with_python: Profile,
) -> None:
    """Sanity: the lifespan calls autolaunch (not just
    ``_autolaunch_profile_scripts`` directly)."""
    marker = profile_with_python.profile_dir / "boot.txt"
    _write_script(
        profile_with_python.python_dir, "00_boot.py",
        f"open(r'{marker.as_posix()}', 'w').write('booted')\n",
    )
    handle = build_app()
    handle.ctx._roster_profile = profile_with_python  # type: ignore[attr-defined]
    with TestClient(handle.app):
        pass
    import time as _time
    for _ in range(50):
        if marker.exists():
            break
        _time.sleep(0.02)
    assert marker.exists()


# ---- shared vs per-profile dir ordering --------------------------------


async def test_shared_dir_runs_before_profile_dir(
    profile_with_python: Profile,
) -> None:
    """Scripts in ``<settings-root>/python/`` load before the
    per-profile ones so they can seed globals the per-profile
    scripts rely on."""
    log = profile_with_python.profile_dir / "order.txt"
    log.write_text("", encoding="utf-8")
    _write_script(
        profile_with_python.shared_python_dir, "00_shared.py",
        f"open(r'{log.as_posix()}', 'a').write('S')\n",
    )
    _write_script(
        profile_with_python.python_dir, "00_profile.py",
        f"open(r'{log.as_posix()}', 'a').write('P')\n",
    )
    handle = build_app()
    handle.ctx._roster_profile = profile_with_python  # type: ignore[attr-defined]
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(50):
        if len(log.read_text()) == 2:
            break
        await asyncio.sleep(0.02)
    assert log.read_text() == "SP"


async def test_shared_dir_alone_still_runs(
    tmp_path: pathlib.Path,
) -> None:
    """A profile with no per-profile python dir but a populated
    shared dir must still get shared scripts run -- otherwise the
    "one shared helper across all profiles" workflow wouldn't
    function."""
    profile = Profile(root=tmp_path, name="empty.jmri")
    profile.ensure()
    marker = profile.profile_dir / "ran.txt"
    _write_script(
        profile.shared_python_dir, "hello.py",
        f"open(r'{marker.as_posix()}', 'w').write('shared')\n",
    )
    handle = build_app()
    handle.ctx._roster_profile = profile  # type: ignore[attr-defined]
    await _autolaunch_profile_scripts(handle.ctx)
    for _ in range(50):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists()
    assert marker.read_text() == "shared"
