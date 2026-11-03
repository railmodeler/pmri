"""``AbstractAutomaton`` -- the inheritable base every JMRI script extends.

In JMRI's Jython world, the common pattern is::

    class MyScript(AbstractAutomaton):
        def init(self): ...
        def handle(self): ...  # returns True to keep running, False to stop

    MyScript().start()

We provide the same shape with one tweak: scripts run on a worker thread
owned by :class:`ScriptRunner` rather than spawning their own. ``start``
just registers them; the runner pumps ``handle`` in a loop until it
returns False, the script is stopped externally, or it raises.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmri_scripting.shim import JmriShim


class AbstractAutomaton:
    """Base class for scripted automations.

    Subclass and override ``init`` (one-time setup) and ``handle``
    (called in a loop, returns ``True`` to continue). Scripts can call
    :meth:`stop` to terminate themselves.
    """

    # The shim is injected by the runner before init() runs.
    shim: JmriShim

    def __init__(self, name: str = "") -> None:
        self.name = name or type(self).__name__
        self._stop_event = threading.Event()
        self._log = logging.getLogger(f"pmri_scripting.{self.name}")

    # ----- lifecycle (overridden by subclasses) -----------------------------

    def init(self) -> None:
        """One-time setup before the handle loop starts."""

    def handle(self) -> bool:
        """Run one iteration. Return True to keep running, False to stop."""
        return False

    def cleanup(self) -> None:
        """Optional teardown after the handle loop exits."""

    # ----- control -------------------------------------------------------

    def stop(self) -> None:
        """Request the automaton to stop. Effective on the next handle iteration."""
        self._stop_event.set()

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    # ----- runner-driven loop ------------------------------------------------

    def run(self) -> None:
        """Run the full lifecycle: init -> handle-loop -> cleanup."""
        try:
            self.init()
            while not self._stop_event.is_set():
                if not self.handle():
                    break
        except Exception:
            self._log.exception("automaton %s crashed", self.name)
            raise
        finally:
            try:
                self.cleanup()
            except Exception:
                self._log.exception("automaton %s cleanup failed", self.name)
