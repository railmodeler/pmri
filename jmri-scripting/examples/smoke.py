"""Smoke test -- JMRI-style script using the injected shim globals.

Run via ``ScriptRunner.run_file(__file__)`` or just exec'd inline.
"""
sensor = sensors.provideSensor("IS5")  # noqa: F821 - injected by runner
sensor.setKnownState(ACTIVE)             # noqa: F821
print("sensor state:", sensor.known_state)

turnout = turnouts.provideTurnout("IT5")  # noqa: F821
turnout.setCommandedState(CLOSED)         # noqa: F821
print("turnout state:", turnout.commanded_state)

powermanager.setPower(powermanager.ON)    # noqa: F821
print("power:", powermanager.getPower())  # noqa: F821
