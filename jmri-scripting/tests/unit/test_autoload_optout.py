"""``# jmri-autoload: false`` opt-out for the startup autoloader."""
from __future__ import annotations

import pathlib

import pytest

from jmri_scripting import ScriptRunner
from jmri_scripting.runner import _autoload_opt_out


# ---- marker detection --------------------------------------------------


@pytest.mark.parametrize("source", [
    "# jmri-autoload: false",
    "# jmri-autoload:false",
    "# jmri-autoload : false",
    "#jmri-autoload:false",
    "# JMRI-AUTOLOAD: FALSE",
    "# jmri-autoload: no",
    "# jmri-autoload: off",
    "# jmri-autoload: 0",
])
def test_opt_out_marker_detected(source: str) -> None:
    """All the reasonable spellings of "opt out" should match.
    Case-insensitive on the value; whitespace-tolerant."""
    assert _autoload_opt_out(source)


def test_opt_out_marker_in_docstring_header() -> None:
    """A docstring line containing the marker on line 3 still counts
    -- the header scan looks at the first 20 lines."""
    source = '"""My script.\n\n# jmri-autoload: false\n"""\nprint("hi")\n'
    assert _autoload_opt_out(source)


def test_opt_out_marker_below_scan_window_ignored() -> None:
    """A script with the marker buried 100 lines down is NOT opted
    out -- the scan window prevents accidental match from unrelated
    docs / data blocks."""
    padding = "\n".join("# padding" for _ in range(50))
    source = padding + "\n# jmri-autoload: false\n"
    assert not _autoload_opt_out(source)


def test_no_marker_means_no_opt_out() -> None:
    assert not _autoload_opt_out("print('hi')")


def test_opt_out_true_value_ignored() -> None:
    """``jmri-autoload: true`` is a no-op -- the marker only opts out,
    it doesn't opt in. Every unmarked script autoloads by default."""
    assert not _autoload_opt_out("# jmri-autoload: true")


# ---- load_directory integration ----------------------------------------


def test_autoloader_skips_opted_out_files(
    runner: ScriptRunner, tmp_path: pathlib.Path,
) -> None:
    """load_directory runs the unmarked file, skips the marked one.
    Both files remain on disk -- the marker doesn't hide them, it
    just keeps them out of startup."""
    (tmp_path / "runs.py").write_text(
        "print('runs')\n", encoding="utf-8",
    )
    (tmp_path / "skipped.py").write_text(
        "# jmri-autoload: false\nprint('should not run')\n",
        encoding="utf-8",
    )

    started = runner.load_directory(tmp_path)
    started_names = {s.name for s in started}
    assert "runs" in started_names
    assert "skipped" not in started_names


def test_autoloader_still_skips_underscore_and_lib_prefixes(
    runner: ScriptRunner, tmp_path: pathlib.Path,
) -> None:
    """The pre-existing ``_``/``lib_`` opt-outs still work alongside
    the marker. Regression guard so a future refactor doesn't drop
    one of the three paths."""
    (tmp_path / "_private.py").write_text(
        "print('private')\n", encoding="utf-8",
    )
    (tmp_path / "lib_helper.py").write_text(
        "print('helper')\n", encoding="utf-8",
    )
    started = runner.load_directory(tmp_path)
    started_names = {s.name for s in started}
    assert "_private" not in started_names
    assert "lib_helper" not in started_names
