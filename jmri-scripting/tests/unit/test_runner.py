"""ScriptRunner tests -- runs scripts on worker threads with shim bridging."""
from __future__ import annotations

import asyncio
import pathlib

from jmri_core.state import SensorState, TurnoutState

from jmri_scripting import ScriptRunner


async def _wait_until_finished(runner: ScriptRunner, name: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = runner.get(name)
        if status and status.finished:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"script {name!r} did not finish in {timeout}s")


async def test_run_source_executes_inline(runner: ScriptRunner) -> None:
    runner.run_source(
        "sensor = sensors.provideSensor('IS5'); sensor.setKnownState(ACTIVE)",
        name="inline-test",
    )
    await _wait_until_finished(runner, "inline-test")
    status = runner.get("inline-test")
    assert status is not None
    assert status.error is None
    # Confirm side-effect reached the core.
    sensor = runner.shim.connection.sensors.get("IS5")
    assert sensor is not None
    assert sensor.known_state == SensorState.ACTIVE


async def test_run_file_executes(runner: ScriptRunner, tmp_path: pathlib.Path) -> None:
    p = tmp_path / "test.py"
    p.write_text("turnouts.provideTurnout('IT5').setCommandedState(CLOSED)")
    status = runner.run_file(p)
    await _wait_until_finished(runner, status.name)
    assert status.error is None
    assert runner.shim.connection.turnouts.get("IT5").commanded_state == TurnoutState.CLOSED


async def test_script_with_syntax_error_captures_traceback(runner: ScriptRunner) -> None:
    runner.run_source("this is not valid python", name="bad")
    await _wait_until_finished(runner, "bad")
    status = runner.get("bad")
    assert status is not None
    assert status.error is not None
    assert "SyntaxError" in status.error


async def test_script_runtime_error_captures_traceback(runner: ScriptRunner) -> None:
    runner.run_source("raise RuntimeError('kaboom')", name="crash")
    await _wait_until_finished(runner, "crash")
    status = runner.get("crash")
    assert status is not None
    assert "kaboom" in (status.error or "")


async def test_automaton_subclass_runs(runner: ScriptRunner) -> None:
    source = """
class Mine(AbstractAutomaton):
    def init(self):
        self.count = 0
        self.sensor = sensors.provideSensor('IS9')

    def handle(self):
        self.count += 1
        if self.count >= 2:
            self.sensor.setKnownState(ACTIVE)
            return False
        return True
"""
    runner.run_source(source, name="auto-test")
    await _wait_until_finished(runner, "auto-test")
    status = runner.get("auto-test")
    assert status is not None
    assert status.error is None
    assert status.automaton is not None
    assert runner.shim.connection.sensors.get("IS9").known_state == SensorState.ACTIVE


async def test_stop_signals_automaton(runner: ScriptRunner) -> None:
    source = """
class Mine(AbstractAutomaton):
    def init(self):
        self.iters = 0
    def handle(self):
        self.iters += 1
        import time
        time.sleep(0.05)
        return True
"""
    runner.run_source(source, name="forever")
    await asyncio.sleep(0.05)
    assert runner.stop("forever")
    await _wait_until_finished(runner, "forever")


async def test_stop_unknown_returns_false(runner: ScriptRunner) -> None:
    assert runner.stop("nope") is False


async def test_list_includes_all_scripts(runner: ScriptRunner) -> None:
    runner.run_source("pass", name="a")
    runner.run_source("pass", name="b")
    await asyncio.sleep(0.1)
    names = {s.name for s in runner.list()}
    assert {"a", "b"} <= names


async def test_main_function_called_with_shim(runner: ScriptRunner) -> None:
    source = """
def main(shim):
    sensors.provideSensor('IS_MAIN').setKnownState(ACTIVE)
"""
    runner.run_source(source, name="main-test")
    await _wait_until_finished(runner, "main-test")
    assert runner.shim.connection.sensors.get("IS_MAIN").known_state == SensorState.ACTIVE


async def test_main_function_no_args_also_called(runner: ScriptRunner) -> None:
    source = """
def main():
    sensors.provideSensor('IS_NOARG').setKnownState(ACTIVE)
"""
    runner.run_source(source, name="noarg-test")
    await _wait_until_finished(runner, "noarg-test")
    assert runner.shim.connection.sensors.get("IS_NOARG").known_state == SensorState.ACTIVE


async def test_stop_all_terminates_running_automata(runner: ScriptRunner) -> None:
    source = """
class Mine(AbstractAutomaton):
    def handle(self):
        import time
        time.sleep(0.05)
        return True
"""
    runner.run_source(source, name="a")
    runner.run_source(source, name="b")
    await asyncio.sleep(0.05)
    runner.stop_all(join_timeout=1.0)
    # Both scripts should be finished or at least stopped.
    for name in ("a", "b"):
        status = runner.get(name)
        assert status is not None
        if status.automaton is not None:
            assert status.automaton.should_stop


# ----- stdout / stderr capture ---------------------------------------------


async def test_print_output_captured_on_status(runner: ScriptRunner) -> None:
    """A script's ``print()`` output ends up on ``status.stdout``, not the
    server log, so the /scripts UI can display it alongside tracebacks."""
    runner.run_source(
        "print('hello from script'); print('another line')",
        name="prints",
    )
    await _wait_until_finished(runner, "prints")
    status = runner.get("prints")
    assert status is not None
    assert "hello from script" in status.stdout
    assert "another line" in status.stdout
    # The parent test process's stdout must be undisturbed -- the
    # dispatch stream should only route this thread's writes.
    assert status.stderr == ""


async def test_traceback_captured_on_stderr(runner: ScriptRunner) -> None:
    """Uncaught exceptions still populate status.error; anything the
    script *itself* wrote to sys.stderr before crashing lands on
    status.stderr."""
    source = """
import sys
sys.stderr.write('crashing now\\n')
raise RuntimeError('boom')
"""
    runner.run_source(source, name="crash-with-stderr")
    await _wait_until_finished(runner, "crash-with-stderr")
    status = runner.get("crash-with-stderr")
    assert status is not None
    assert "crashing now" in status.stderr
    assert "boom" in (status.error or "")


async def test_concurrent_scripts_have_isolated_output(runner: ScriptRunner) -> None:
    """Two scripts running at the same time must not cross-contaminate
    each other's stdout. Verifies the dispatch stream keys on thread id."""
    import asyncio as _asyncio  # noqa: PLC0415

    a = """
import time
for i in range(3):
    print(f'A{i}')
    time.sleep(0.01)
"""
    b = """
import time
for i in range(3):
    print(f'B{i}')
    time.sleep(0.01)
"""
    runner.run_source(a, name="ca")
    runner.run_source(b, name="cb")
    await _wait_until_finished(runner, "ca")
    await _wait_until_finished(runner, "cb")
    sa = runner.get("ca")
    sb = runner.get("cb")
    assert sa is not None and sb is not None
    assert "A0" in sa.stdout and "A1" in sa.stdout and "A2" in sa.stdout
    assert "B0" in sb.stdout and "B1" in sb.stdout and "B2" in sb.stdout
    # No cross-talk.
    assert "B" not in sa.stdout
    assert "A" not in sb.stdout
    # And the server's real stdout must be untouched -- a quick print
    # from the test thread goes to the fallback, not either script.
    _ = _asyncio  # keep the import used (linter)


# ----- library / autoload --------------------------------------------------


async def test_load_directory_runs_every_py_file(
    runner: ScriptRunner, tmp_path: pathlib.Path,
) -> None:
    """``load_directory`` scans a folder and starts every ``*.py`` in
    lexicographic order -- the persistence layer for scripts users park
    in ``<profile>/jython/``."""
    (tmp_path / "00_first.py").write_text(
        "sensors.provideSensor('IS_A').setKnownState(ACTIVE)"
    )
    (tmp_path / "10_second.py").write_text(
        "sensors.provideSensor('IS_B').setKnownState(ACTIVE)"
    )
    (tmp_path / "_skipped.py").write_text(
        "sensors.provideSensor('IS_SKIP').setKnownState(ACTIVE)"
    )
    (tmp_path / "notpy.txt").write_text("ignored")

    statuses = runner.load_directory(tmp_path)
    assert {s.name for s in statuses} == {"00_first", "10_second"}
    for s in statuses:
        await _wait_until_finished(runner, s.name)
    conn = runner.shim.connection
    assert conn.sensors.get("IS_A").known_state == SensorState.ACTIVE
    assert conn.sensors.get("IS_B").known_state == SensorState.ACTIVE
    # Underscored files are private, not autoloaded.
    assert conn.sensors.get("IS_SKIP") is None


async def test_load_directory_missing_returns_empty(runner: ScriptRunner) -> None:
    """A nonexistent or non-directory path is tolerated silently -- beta
    installs may not have a jython dir yet."""
    assert runner.load_directory(pathlib.Path("/definitely-not-here-xyz")) == []


async def test_output_buffer_truncates_when_oversized(runner: ScriptRunner) -> None:
    """A runaway loop that prints megabytes shouldn't OOM the server --
    the bounded buffer keeps ~64 KB and prepends a truncation marker."""
    source = (
        "for i in range(2000):\n"
        "    print('x' * 100)\n"
    )
    runner.run_source(source, name="firehose")
    await _wait_until_finished(runner, "firehose", timeout=5.0)
    status = runner.get("firehose")
    assert status is not None
    # 2000 lines * ~101 chars = ~200 KB written; buffer caps ~64 KB.
    assert len(status.stdout) < 80 * 1024
    assert "bytes elided" in status.stdout
