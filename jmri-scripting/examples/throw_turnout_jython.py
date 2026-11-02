"""Throw a turnout using the Jython-compat shim -- JMRI Java style.

Runs unchanged against JMRI Java's Jython environment AND our
Python rewrite. Same script; two environments. That's the whole
point of the Jython namespace shim: existing scripts port over
with no edits.

Two authoring conventions covered below for reference -- pick one:

1. **Injected globals** -- bare ``turnouts``, ``THROWN``, no import.
   This is what most Jython examples on the JMRI forums look like.

2. **Explicit imports via jmri.***  -- ``from jmri import
   InstanceManager, Turnout``. More common in library code and
   larger scripts, and what our jython_shim mirrors.

Save as ``<profile>/python/throw_turnout_jython.py`` OR paste
into the /scripts inline runner box.
"""

# --- Convention 1: injected globals (the common Jython form) --------

t = turnouts.provideTurnout("IT2")            # noqa: F821 - injected
print("before:", t.commanded_state.name)
t.setCommandedState(THROWN)                    # noqa: F821 - injected
print("after commanded THROWN:", t.commanded_state.name)


# --- Convention 2: explicit imports via the jmri.* shim -------------

from jmri import InstanceManager, Turnout

mgr = InstanceManager.getDefault(Turnout)
t2 = mgr.provideTurnout("IT3")
print("second turnout before:", t2.commanded_state.name)

# The state constant works via bare ``THROWN`` (injected) or via
# ``mgr.THROWN`` on the state enum re-exported through the shim.
# The bare form is what the JMRI Java Jython scripts use, so we go
# with that for maximum copy-paste from existing user scripts.
t2.setCommandedState(THROWN)                   # noqa: F821 - injected
print("second turnout after:", t2.commanded_state.name)

# Reset both so re-running is idempotent-ish.
t.setCommandedState(CLOSED)                    # noqa: F821 - injected
t2.setCommandedState(CLOSED)                   # noqa: F821 - injected
print("both returned to CLOSED")
