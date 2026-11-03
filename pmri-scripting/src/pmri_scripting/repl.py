"""Interactive Python REPL bound to the JMRI shim.

The batch script runner in :mod:`pmri_scripting.runner` compiles and
runs a whole file per submission with a **fresh** globals namespace
each time. That's the right shape for automatons and one-shot
utilities, but it's a lousy fit for the "let me poke at the layout
interactively" workflow every layout builder actually wants -- you
lose every variable and import as soon as the script ends.

This module provides the interactive companion. One persistent
:class:`code.InteractiveInterpreter` per server, pre-seeded with the
shim's globals (``sensors``, ``turnouts``, ``ACTIVE``, everything).
Submit a fragment via :meth:`ReplSession.submit` and get back a
:class:`ReplResult` carrying the captured stdout, stderr, and the
last expression's ``repr`` -- the Jupyter-cell shape.

State persists across submissions until :meth:`ReplSession.reset`
is called, which rebuilds the interpreter (and drops every user
variable, import, and function).

**Multi-line handling** -- a submission that ends with an
unfinished statement (an open ``def``, a hanging ``if``, an
unclosed bracket) needs to accumulate until the next submission.
:func:`_needs_continuation` uses :func:`code.compile_command` to
detect that case; the caller sees ``result.incomplete == True``
and knows to append the next fragment before re-submitting.

**Concurrency** -- a single lock serialises submissions so two
browser tabs can't race and corrupt the interpreter state. The
lock is contested only during ``submit``, which does CPU-bound
work with the GIL held anyway; overhead is negligible.
"""
from __future__ import annotations

import code
import contextlib
import io
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pmri_scripting.shim import JmriShim

_log = logging.getLogger(__name__)


# The interpreter's globals are seeded once at :meth:`ReplSession.reset`
# time; subsequent submissions mutate that dict as normal Python code
# would. When ``reset`` runs again, we build a fresh dict -- the old
# one is left for the GC.


@dataclass(slots=True)
class ReplEntry:
    """One completed submission's record: what the user typed and
    what the interpreter produced.

    Kept lightweight so the running history buffer can hold ~100
    entries without meaningful memory pressure."""

    source: str
    stdout: str = ""
    stderr: str = ""
    result_repr: str = ""       # last-expression ``repr()`` (Jupyter style)
    error: str = ""             # formatted traceback on failure
    at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class ReplResult:
    """Return value of :meth:`ReplSession.submit`.

    Distinguishes the three outcomes:

    * ``incomplete=True`` -- the source parses as an unfinished
      statement (e.g. a ``def`` with no body yet). Client shows
      the continuation prompt.
    * ``error`` non-empty -- the source ran and raised.
    * Otherwise -- the source ran cleanly. ``stdout`` / ``stderr`` /
      ``result_repr`` carry whatever was produced.
    """

    entry: ReplEntry
    incomplete: bool = False


# History cap -- 100 entries x ~1 KB each = ~100 KB. If a user submits
# a huge print loop the individual entry stdout is bounded by
# ``_MAX_STDOUT_PER_ENTRY`` too.
_MAX_HISTORY_ENTRIES = 100
_MAX_STDOUT_PER_ENTRY = 64 * 1024


def _needs_continuation(source: str) -> bool:
    """Return True when *source* is syntactically incomplete --
    i.e. the user needs to keep typing before we can compile.

    :func:`code.compile_command` returns ``None`` for that case,
    a code object when compilable, and raises for a hard syntax
    error. We treat the hard error as "compilable enough to try
    executing", letting the ``exec`` path surface the traceback
    -- matches how Python's own REPL behaves."""
    try:
        result = code.compile_command(source, "<repl>", "single")
    except (SyntaxError, OverflowError, ValueError):
        return False
    return result is None


class ReplSession:
    """Persistent Python interpreter bound to the JMRI shim.

    One instance per server. Callers submit source fragments via
    :meth:`submit`; state persists across submissions until
    :meth:`reset` is called.
    """

    def __init__(self, shim: JmriShim) -> None:
        self._shim = shim
        self._lock = threading.Lock()
        self._history: list[ReplEntry] = []
        self._globals: dict[str, Any] = {}
        # Build the initial namespace so the very first submission
        # already has ``sensors`` / ``turnouts`` / etc. available.
        self._install_globals()

    def _install_globals(self) -> None:
        """Populate ``self._globals`` with the shim's injected names
        plus a couple of REPL-friendly built-ins."""
        self._globals = {
            "__name__": "__pmri_repl__",
            "__builtins__": __builtins__,
            **self._shim.globals(),
        }
        # A friendly reference to the shim itself, for scripts that
        # want to reach through to the underlying async connection.
        self._globals["shim"] = self._shim

    # ---- public API ----------------------------------------------------

    @property
    def history(self) -> list[ReplEntry]:
        """Snapshot of the history buffer, oldest first."""
        with self._lock:
            return list(self._history)

    def reset(self) -> None:
        """Rebuild the interpreter -- drops every user variable,
        import, and function definition. History is cleared too."""
        with self._lock:
            self._history.clear()
            self._install_globals()

    def submit(self, source: str) -> ReplResult:
        """Execute *source* against the persistent interpreter.

        Returns a :class:`ReplResult`. Bounded stdout capture means
        a script that prints megabytes only surfaces the first
        64 KB -- a runaway loop can't OOM the server.

        **Threading note**: this method blocks synchronously on the
        current thread. It must NOT be called from the event-loop
        thread if the submitted source might touch the shim, because
        the shim's ``_block(loop, coro)`` schedules coroutines onto
        that loop and waits for them -- the loop can't advance if the
        caller is holding its thread. Two supported patterns:

        * **From an async coroutine on the loop thread**: wrap in
          ``asyncio.to_thread(repl.submit, source)`` -- that off-loads
          the exec to a worker so the loop stays free.
        * **From any other thread** (batch worker, sync test): call
          directly.

        :meth:`submit_async` is a thin async wrapper that does the
        ``to_thread`` for you; web routes use it.
        """
        with self._lock:
            # Incomplete? Client keeps accumulating.
            if _needs_continuation(source):
                entry = ReplEntry(source=source)
                return ReplResult(entry=entry, incomplete=True)

            entry = ReplEntry(source=source)
            self._exec_one(source, entry)
            self._push_entry(entry)
            return ReplResult(entry=entry, incomplete=False)

    async def submit_async(self, source: str) -> ReplResult:
        """Async wrapper -- safe to call from a coroutine running on
        the same event loop the shim binds to. Off-loads the sync
        exec to a worker thread so the loop stays free to service
        shim coroutines."""
        import asyncio  # noqa: PLC0415
        return await asyncio.to_thread(self.submit, source)

    # ---- internals ----------------------------------------------------

    def _exec_one(self, source: str, entry: ReplEntry) -> None:
        """Run *source* under stdout/stderr capture, filling *entry*.

        Binds the shim to the current thread so
        ``jmri.InstanceManager.getDefault(...)`` resolves -- the shim
        is thread-local (matches the batch runner's convention where
        each script's worker thread gets its own bind).
        """
        from pmri_scripting import jython_shim  # noqa: PLC0415

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        jython_shim._bind_shim(self._shim)
        try:
            with (
                contextlib.redirect_stdout(stdout_buf),
                contextlib.redirect_stderr(stderr_buf),
            ):
                self._run_source_capturing(source)
        except SystemExit:
            entry.error = "SystemExit: refused to exit the server"
        except BaseException:  # noqa: BLE001
            entry.error = traceback.format_exc()
        finally:
            jython_shim._unbind_shim()

        entry.stdout = _clip(stdout_buf.getvalue(), _MAX_STDOUT_PER_ENTRY)
        entry.stderr = _clip(stderr_buf.getvalue(), _MAX_STDOUT_PER_ENTRY)

    def _run_source_capturing(self, source: str) -> None:
        """Two-pass compile: try as an expression to surface the
        result via ``repr()``, else exec as statements.

        Mirrors CPython's REPL: a bare expression at the prompt
        prints its ``repr``; a statement produces no automatic
        output. Storing the result on the *entry* is done by the
        caller via ``__last_result__``.
        """
        # ``self._entry_being_run`` set by ``_exec_one`` so the
        # ``compile...eval`` branch can stash the result. Threaded
        # through a per-call attribute rather than a return value so
        # the exception paths above can still see the partial
        # entry state.
        try:
            expr_code = compile(source, "<repl>", "eval")
        except SyntaxError:
            expr_code = None

        if expr_code is not None:
            result = eval(expr_code, self._globals)  # noqa: S307
            if result is not None:
                # Print ``repr`` to stdout so the console rendering
                # is one uniform place -- no separate "result" cell.
                print(repr(result))
            return

        # Statement form: compile + exec.
        stmt_code = compile(source, "<repl>", "exec")
        exec(stmt_code, self._globals)  # noqa: S102

    def _push_entry(self, entry: ReplEntry) -> None:
        self._history.append(entry)
        overflow = len(self._history) - _MAX_HISTORY_ENTRIES
        if overflow > 0:
            del self._history[:overflow]


def _clip(text: str, limit: int) -> str:
    """Truncate *text* to *limit* bytes with a leading marker.

    Called for both stdout and stderr per entry. Keeping the
    trailing bytes (the freshest output) matches what an operator
    watching a runaway loop expects -- the "last thing before it
    went wrong" is what they need to see."""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    marker = f"... ({dropped} bytes elided) ...\n"
    return marker + text[dropped + len(marker):]
