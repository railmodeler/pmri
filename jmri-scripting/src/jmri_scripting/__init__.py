"""jmri-scripting -- user script host for the JMRI Python rewrite (Stage 9).

Loads JMRI-style Python scripts and runs them with a familiar shim of
synchronous wrappers over the async core. Scripts written for JMRI's
Jython runtime port with minimal changes (the most-used 200 attribute
paths from the Stage 0 script-api ranking are covered).
"""
from jmri_scripting.automaton import AbstractAutomaton
from jmri_scripting.repl import ReplEntry, ReplResult, ReplSession
from jmri_scripting.runner import ScriptRunner, ScriptStatus
from jmri_scripting.shim import JmriShim
from jmri_scripting.watcher import ScriptWatcher

__version__ = "0.1.0"

__all__ = [
    "AbstractAutomaton",
    "JmriShim",
    "ReplEntry",
    "ReplResult",
    "ReplSession",
    "ScriptRunner",
    "ScriptStatus",
    "ScriptWatcher",
    "__version__",
]
