from __future__ import annotations

from jmri_scripting import AbstractAutomaton


class _CountUp(AbstractAutomaton):
    """Counts iterations until told to stop."""

    def init(self) -> None:
        self.iterations = 0

    def handle(self) -> bool:
        self.iterations += 1
        if self.iterations >= 3:
            return False
        return True


def test_automaton_runs_handle_until_returns_false() -> None:
    a = _CountUp("test")
    a.run()
    assert a.iterations == 3


def test_automaton_stop_breaks_loop() -> None:
    class _Forever(AbstractAutomaton):
        def init(self) -> None:
            self.iterations = 0

        def handle(self) -> bool:
            self.iterations += 1
            if self.iterations >= 2:
                self.stop()
            return True

    a = _Forever("test")
    a.run()
    assert a.iterations == 2


def test_automaton_cleanup_runs_on_exit() -> None:
    cleanup_ran = False

    class _WithCleanup(AbstractAutomaton):
        def handle(self) -> bool:
            return False

        def cleanup(self) -> None:
            nonlocal cleanup_ran
            cleanup_ran = True

    a = _WithCleanup("test")
    a.run()
    assert cleanup_ran


def test_automaton_cleanup_runs_even_on_exception() -> None:
    cleanup_ran = False

    class _Crash(AbstractAutomaton):
        def handle(self) -> bool:
            raise RuntimeError("kaboom")

        def cleanup(self) -> None:
            nonlocal cleanup_ran
            cleanup_ran = True

    a = _Crash("test")
    try:
        a.run()
    except RuntimeError:
        pass
    assert cleanup_ran


def test_automaton_name_defaults_to_class_name() -> None:
    a = _CountUp()
    assert a.name == "_CountUp"


def test_should_stop_property() -> None:
    a = _CountUp()
    assert not a.should_stop
    a.stop()
    assert a.should_stop
