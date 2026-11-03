"""Throw a turnout using the native pmri_scripting.api.

For scripts that want explicit imports instead of the JMRI Jython
injected-globals convention (``turnouts``, ``THROWN``, ...). Same
runtime behaviour; different authoring style.

Copy-paste-friendly:
   1. Save as ``<profile>/python/throw_turnout_native.py`` OR paste
      into the /scripts inline runner box.
   2. Adjust the system name if ``IT1`` isn't the turnout you meant.
   3. Run.

The script:
   - Provides turnout IT1 (creates it if new, else fetches).
   - Sets the commanded state to THROWN.
   - Prints the resulting state.
"""
from pmri_scripting.api import turnouts, THROWN, CLOSED

turnout = turnouts.provideTurnout("IT1")
print(f"before: {turnout.commanded_state.name}")

turnout.setCommandedState(THROWN)
print(f"after commanding THROWN: {turnout.commanded_state.name}")

# On the Internal driver, commanded state maps straight through to
# known state. On real hardware you'd wait for the layout to echo
# back the position -- a WaitAndReport / AbstractAutomaton pattern.
print(f"known state: {turnout.known_state.name}")

# Toggle back to CLOSED so re-running the script is idempotent-ish.
turnout.setCommandedState(CLOSED)
print(f"returned to: {turnout.commanded_state.name}")
