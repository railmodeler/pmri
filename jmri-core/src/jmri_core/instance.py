"""``InstanceManager`` -- service locator container.

JMRI's Java ``InstanceManager`` is a reflection-driven service registry where
defaults are auto-discovered at runtime. In Python we make this explicit: you
register an implementation against an interface type, and look it up by type.

Two registration modes:

* :meth:`register` -- install an already-built object (singleton).
* :meth:`register_factory` -- install a zero-arg callable that builds the
  object on first :meth:`get`. Lets you defer construction without exposing
  the order in which subsystems initialize.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class InstanceManager:
    """Per-application service container. Not a global singleton.

    Construct one and pass it explicitly (or via dependency injection) into
    components that need to resolve services. This avoids the global-state
    pitfalls of JMRI's Java ``InstanceManager`` while keeping the locator
    pattern that scripts and tools expect.
    """

    def __init__(self) -> None:
        self._services: dict[type, object] = {}
        self._factories: dict[type, Callable[[], object]] = {}

    def register(self, iface: type[T], impl: T) -> None:
        """Bind a concrete instance to *iface*. Replaces any prior binding."""
        self._services[iface] = impl
        # Clearing a factory ensures explicit register() wins over factory.
        self._factories.pop(iface, None)

    def register_factory(self, iface: type[T], factory: Callable[[], T]) -> None:
        """Bind a zero-arg factory invoked on first :meth:`get`. Idempotent."""
        # If something is already bound, prefer the factory only if no live
        # instance exists.
        self._factories[iface] = factory

    def get(self, iface: type[T]) -> T:
        """Return the bound implementation of *iface*. Build via factory if needed."""
        if iface in self._services:
            return self._services[iface]  # type: ignore[return-value]
        if iface in self._factories:
            built = self._factories[iface]()
            self._services[iface] = built
            return built  # type: ignore[return-value]
        raise LookupError(f"No service registered for {iface.__name__}")

    def try_get(self, iface: type[T]) -> T | None:
        """Like :meth:`get` but returns ``None`` if no binding exists."""
        try:
            return self.get(iface)
        except LookupError:
            return None

    def contains(self, iface: type) -> bool:
        return iface in self._services or iface in self._factories

    def remove(self, iface: type) -> None:
        """Drop the binding for *iface*. No-op if absent."""
        self._services.pop(iface, None)
        self._factories.pop(iface, None)
