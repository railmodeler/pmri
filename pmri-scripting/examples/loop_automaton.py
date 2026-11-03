"""Example using the AbstractAutomaton pattern -- JMRI's classic shape.

This script defines a subclass and instantiates it; the runner detects
the class and calls .run() on the worker thread automatically.
"""
import time


class BlinkLight(AbstractAutomaton):  # noqa: F821 - injected by runner
    """Toggle a light every iteration until stopped."""

    def init(self) -> None:
        self.light = lights.provideLight("IL5")        # noqa: F821
        self.state_on = True
        self.iterations = 0

    def handle(self) -> bool:
        self.light.setState(ON if self.state_on else OFF)  # noqa: F821
        self.state_on = not self.state_on
        self.iterations += 1
        # Stop after 5 iterations so the test doesn't hang forever.
        if self.iterations >= 5:
            return False
        time.sleep(0.01)
        return True


# The runner auto-detects the AbstractAutomaton subclass; no need to .start().
