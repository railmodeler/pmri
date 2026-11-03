"""``NamedBean`` -- the universal interface every JMRI domain object satisfies.

A bean has:
  * an immutable ``system_name`` -- globally unique within a manager, formed of
    a connection prefix (e.g. ``"L"`` for LocoNet) plus a type letter (e.g.
    ``"T"`` for turnout) plus the system-specific address (``"12"``);
  * an optional ``user_name`` -- a human-friendly label;
  * a ``comment`` -- free-form;
  * a ``state_changed`` :class:`Signal` that fires on any state mutation;
  * a generic ``properties`` dict for ad-hoc extensions.

The :class:`NamedBean` Protocol is structural; any object with the right shape
satisfies it. :class:`AbstractNamedBean` is the canonical implementation
domain beans inherit.
"""
from __future__ import annotations

from typing import Any, ClassVar, Protocol, runtime_checkable

from pmri_core.events import Signal


@runtime_checkable
class NamedBean(Protocol):
    """Structural protocol for any JMRI domain object."""

    system_name: str
    user_name: str | None
    comment: str | None
    state_changed: Signal[Any]


class AbstractNamedBean:
    """Concrete mixin providing the standard ``NamedBean`` machinery.

    Subclasses extend this and add their state attributes + transition logic.
    """

    __slots__ = ("comment", "properties", "state_changed", "system_name", "user_name")

    writable: ClassVar[bool] = True
    """Whether the client can command state on this bean.

    Default True: most drivers both observe and command the layout.
    Override to False in subclasses that observe only (e.g.
    ``pmri-ncemonitor``'s passive beans) so the web UI and JSON
    clients can present a read-only affordance instead of inviting a
    command that will raise. The flag is a UI capability signal only —
    the authoritative refusal still lives in the overridden
    ``set_commanded_state`` / ``set_power`` method.
    """

    def __init__(self, system_name: str, user_name: str | None = None) -> None:
        if not system_name:
            raise ValueError("system_name must be a non-empty string")
        self.system_name = system_name
        self.user_name = user_name
        self.comment: str | None = None
        self.state_changed: Signal[Any] = Signal()
        self.properties: dict[str, Any] = {}

    @property
    def display_name(self) -> str:
        """``user_name`` if set, else ``system_name``. Mirrors JMRI's getDisplayName()."""
        return self.user_name if self.user_name else self.system_name

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.system_name!r})"

    def __eq__(self, other: object) -> bool:
        # Equality by (type, system_name) -- JMRI manager invariant.
        if type(self) is not type(other):
            return False
        return self.system_name == other.system_name

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.system_name))
