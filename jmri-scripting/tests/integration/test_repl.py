"""ReplSession tests -- persistence, capture, incomplete detection, reset.

The REPL is the interactive companion to the batch ScriptRunner.
Its distinguishing feature is state persistence: a variable defined
in submission N is visible in submission N+1. These tests lock that
contract in.
"""
from __future__ import annotations

import asyncio

import pytest
from jmri_core.hal.internal import InternalConnection
from jmri_core.state import TurnoutState

from jmri_scripting import JmriShim, ReplSession


@pytest.fixture()
def repl(shim: JmriShim) -> ReplSession:
    return ReplSession(shim)


# ---- expression vs statement ------------------------------------------


def test_expression_prints_its_repr(repl: ReplSession) -> None:
    """Bare expressions echo their ``repr`` -- Jupyter-style. Matches
    CPython's REPL: at the prompt, ``2 + 2`` shows ``4``, not
    silence."""
    r = repl.submit("2 + 2")
    assert r.entry.stdout.strip() == "4"
    assert r.entry.error == ""


def test_statement_produces_no_auto_output(repl: ReplSession) -> None:
    """A pure statement (assignment, def, import) produces no
    stdout unless the user explicitly ``print``s. Matches CPython's
    REPL."""
    r = repl.submit("x = 42")
    assert r.entry.stdout == ""
    assert r.entry.stderr == ""


def test_print_output_captured(repl: ReplSession) -> None:
    r = repl.submit("print('hello world')")
    assert r.entry.stdout == "hello world\n"


# ---- state persistence -------------------------------------------------


def test_variable_persists_across_submissions(repl: ReplSession) -> None:
    """The Jupyter-cell shape: assign in one submission, use in
    the next. This is the whole point of the REPL -- the batch
    ScriptRunner throws its namespace away on each script end."""
    repl.submit("x = 42")
    r = repl.submit("x * 2")
    assert r.entry.stdout.strip() == "84"


def test_imports_persist_across_submissions(repl: ReplSession) -> None:
    repl.submit("import math")
    r = repl.submit("math.pi")
    assert "3.141" in r.entry.stdout


def test_function_definition_persists(repl: ReplSession) -> None:
    repl.submit("def double(n):\n    return n * 2\n")
    r = repl.submit("double(21)")
    assert r.entry.stdout.strip() == "42"


# ---- shim access -------------------------------------------------------


async def test_injected_globals_available_without_import(
    conn: InternalConnection, repl: ReplSession,
) -> None:
    """The shim's globals are pre-seeded. A user's very first
    submission can touch ``turnouts`` / ``CLOSED`` / etc. without
    any import -- matches the batch runner's convention.

    Uses ``submit_async`` since the shim calls block on the
    active event loop; the sync ``submit`` on the loop thread
    would deadlock."""
    await repl.submit_async("t = turnouts.provideTurnout('IT_REPL')")
    r = await repl.submit_async("t.setCommandedState(THROWN)")
    assert r.entry.error == ""
    turnout = conn.turnouts.get_by_system_name("IT_REPL")
    assert turnout is not None
    assert turnout.commanded_state is TurnoutState.THROWN


async def test_from_jmri_import_works(repl: ReplSession) -> None:
    """The Jython namespace shim is on ``sys.modules`` at import
    time, so ``from jmri import InstanceManager`` resolves without
    a fresh install call."""
    r = await repl.submit_async(
        "from jmri import InstanceManager, Sensor",
    )
    assert r.entry.error == ""
    r2 = await repl.submit_async(
        "mgr = InstanceManager.getDefault(Sensor)",
    )
    assert r2.entry.error == ""


# ---- error handling ---------------------------------------------------


def test_exception_captured_as_traceback(repl: ReplSession) -> None:
    """Errors surface in ``entry.error`` as a full formatted
    traceback, not just the exception message. Otherwise
    debugging a script from the REPL means squinting at the last
    line only."""
    r = repl.submit("1 / 0")
    assert r.entry.error != ""
    assert "ZeroDivisionError" in r.entry.error
    assert "Traceback" in r.entry.error


def test_syntax_error_surfaces_traceback(repl: ReplSession) -> None:
    """A hard syntax error (unmistakable, not just incomplete)
    doesn't leave the user waiting -- it surfaces as a normal
    error entry."""
    r = repl.submit("def bad(:\n    pass\n")
    assert r.incomplete is False
    assert r.entry.error != ""
    assert "SyntaxError" in r.entry.error


def test_state_survives_after_error(repl: ReplSession) -> None:
    """A crashing submission doesn't clobber the interpreter's
    globals. Variables from before still work."""
    repl.submit("x = 100")
    repl.submit("1 / 0")   # crashes
    r = repl.submit("x")
    assert r.entry.stdout.strip() == "100"


# ---- incomplete-statement detection ------------------------------------


def test_open_def_reports_incomplete(repl: ReplSession) -> None:
    """A ``def`` header without a body is syntactically incomplete
    -- client is expected to accumulate more source before
    re-submitting."""
    r = repl.submit("def foo():\n")
    assert r.incomplete is True


def test_open_bracket_reports_incomplete(repl: ReplSession) -> None:
    r = repl.submit("x = [1, 2,\n3,")
    assert r.incomplete is True


# ---- reset -------------------------------------------------------------


def test_reset_clears_user_state(repl: ReplSession) -> None:
    """After reset, previously-assigned variables are gone. The
    shim's globals come back automatically -- the reset rebuilds
    the interpreter with a fresh copy."""
    repl.submit("x = 42")
    repl.reset()
    r = repl.submit("x")
    assert "NameError" in r.entry.error
    # But shim globals are back.
    r2 = repl.submit("type(sensors).__name__")
    assert r2.entry.error == ""


def test_reset_clears_history(repl: ReplSession) -> None:
    repl.submit("print('one')")
    repl.submit("print('two')")
    assert len(repl.history) == 2
    repl.reset()
    assert repl.history == []


# ---- history bookkeeping ----------------------------------------------


def test_history_records_stdout(repl: ReplSession) -> None:
    repl.submit("print('recorded')")
    hist = repl.history
    assert len(hist) == 1
    assert hist[0].stdout == "recorded\n"


def test_history_bounded(repl: ReplSession) -> None:
    """History caps at ~100 entries -- a runaway script loop
    submitting via REPL shouldn't blow memory."""
    for i in range(120):
        repl.submit(f"y = {i}")
    assert len(repl.history) <= 100
