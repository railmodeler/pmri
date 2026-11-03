"""Lightweight async-friendly signal/slot event bus.

Replaces JMRI's ``PropertyChangeSupport`` / ``PropertyChangeListener``
machinery. A :class:`Signal` is a fan-out of zero or more listeners; emitting
delivers the payload to every listener synchronously, awaiting any that
return a coroutine.

Design choices:

* No global event bus -- every emitter owns its signals as instance attributes.
  This keeps coupling explicit and lets tests construct beans in isolation.
* Listeners are weakly *not* held -- callers are responsible for the lifetime
  of their callbacks (return value of :meth:`Signal.connect` is a disconnect
  function). Keeping a strong reference avoids the surprise behaviour where
  bound-method listeners disappear because nothing else holds the receiver.
* Errors in one listener don't suppress delivery to the rest; the first
  exception is collected and re-raised after every listener has run, mirroring
  asyncio.gather semantics.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable

type Listener[T] = Callable[[T], None] | Callable[[T], Awaitable[None]]


class Signal[T]:
    """An emitter with zero or more listeners. See module docstring."""

    __slots__ = ("_listeners",)

    def __init__(self) -> None:
        self._listeners: list[Listener[T]] = []

    def connect(self, listener: Listener[T]) -> Callable[[], None]:
        """Register *listener*. Returns a callable that disconnects it.

        ``listener`` may be a sync or async callable taking one argument.
        Returns a zero-arg disconnect function.
        """
        self._listeners.append(listener)

        def disconnect() -> None:
            # Idempotent: removing twice is a no-op.
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return disconnect

    def disconnect_all(self) -> None:
        """Drop every listener. Useful in teardown."""
        self._listeners.clear()

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    async def emit(self, payload: T) -> None:
        """Deliver *payload* to every connected listener.

        Sync listeners run inline. Async listeners are awaited sequentially in
        registration order -- emission preserves ordering, which beans rely on
        for state-change semantics (commanded-state notifications must arrive
        before known-state notifications). If you need concurrency, use
        :meth:`emit_concurrent`.
        """
        first_error: BaseException | None = None
        # Snapshot the list so listeners that disconnect during dispatch don't
        # mutate the iteration.
        for listener in list(self._listeners):
            try:
                result = listener(payload)
                if inspect.iscoroutine(result):
                    await result
            except BaseException as e:  # noqa: BLE001 -- re-raised below
                if first_error is None:
                    first_error = e
        if first_error is not None:
            raise first_error

    async def emit_concurrent(self, payload: T) -> None:
        """Fan out to every async listener concurrently via ``asyncio.gather``.

        Sync listeners still run inline before dispatching the async batch.
        Use only when listeners are explicitly independent.
        """
        async_results: list[Awaitable[None]] = []
        for listener in list(self._listeners):
            result = listener(payload)
            if inspect.iscoroutine(result):
                async_results.append(result)
        if async_results:
            await asyncio.gather(*async_results)
