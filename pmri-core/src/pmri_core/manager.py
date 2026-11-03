"""``Manager[T]`` -- the per-type bean registry.

Every JMRI bean type (turnouts, sensors, signal heads, ...) has a manager that
owns the set of beans of that type for one or more system connections. Two
lookup keys are supported:

  * ``system_name`` -- globally unique, e.g. ``"LT12"``;
  * ``user_name``   -- optional human label, e.g. ``"Main Street"``.

The canonical operation is :meth:`provide` -- get-or-create by either key.
Drivers subclass :class:`Manager` and override :meth:`_create` to build
their concrete bean type.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pmri_core.bean import NamedBean
from pmri_core.events import Signal
from pmri_core.names import SystemName


class Manager[T: NamedBean]:
    """Per-type bean registry. Subclass and override :meth:`_create`.

    Attributes set on the class describe the bean type:

    * ``type_letter`` -- single uppercase letter the bean class owns
      (e.g. ``"T"`` for Turnout). Must be set by subclass.
    * ``system_prefix`` -- connection prefix (e.g. ``"L"``). Set per-instance
      because one bean class can have many connection-specific managers.
    """

    type_letter: str = ""  # subclass must override

    def __init__(self, system_prefix: str) -> None:
        if not self.type_letter:
            raise TypeError(
                f"{type(self).__name__}.type_letter must be set on the class"
            )
        if not system_prefix:
            raise ValueError("system_prefix must be a non-empty string")
        self.system_prefix = system_prefix
        self._by_system: dict[str, T] = {}
        self._by_user: dict[str, T] = {}
        self.added: Signal[T] = Signal()
        self.removed: Signal[T] = Signal()

    # ------------------------------------------------------------------ public

    @property
    def full_prefix(self) -> str:
        """The connection prefix + type letter, e.g. ``"LT"``."""
        return f"{self.system_prefix}{self.type_letter}"

    def __iter__(self) -> Iterator[T]:
        return iter(self._by_system.values())

    def __len__(self) -> int:
        return len(self._by_system)

    def __contains__(self, name: str) -> bool:
        return name in self._by_system or name in self._by_user

    def get_by_system_name(self, name: str) -> T | None:
        return self._by_system.get(name)

    def get_by_user_name(self, name: str) -> T | None:
        return self._by_user.get(name)

    def get(self, name: str) -> T | None:
        """Look up by either system or user name; system first."""
        return self._by_system.get(name) or self._by_user.get(name)

    def named_bean_set(self) -> list[T]:
        """Snapshot list, JMRI naming."""
        return list(self._by_system.values())

    async def provide(self, name: str) -> T:
        """Get-or-create. Accepts either system name or address-only form.

        Address-only example: passing ``"12"`` to a LocoNet turnout manager
        with ``system_prefix='L'`` resolves to ``"LT12"``. Passing the full
        system name ``"LT12"`` is also accepted.
        """
        if name in self._by_user:
            return self._by_user[name]
        sysname = self._normalize_name(name)
        if sysname in self._by_system:
            return self._by_system[sysname]
        bean = self._create(sysname)
        await self.register(bean)
        return bean

    async def register(self, bean: T) -> None:
        """Add an externally constructed *bean* to the registry."""
        if bean.system_name in self._by_system:
            raise ValueError(f"bean with system_name {bean.system_name!r} already registered")
        self._by_system[bean.system_name] = bean
        if bean.user_name:
            if bean.user_name in self._by_user:
                raise ValueError(f"user_name {bean.user_name!r} already in use")
            self._by_user[bean.user_name] = bean
        await self.added.emit(bean)

    async def deregister(self, bean: T) -> None:
        """Remove *bean* from the registry. No-op if not present."""
        if bean.system_name not in self._by_system:
            return
        del self._by_system[bean.system_name]
        if bean.user_name and self._by_user.get(bean.user_name) is bean:
            del self._by_user[bean.user_name]
        await self.removed.emit(bean)

    # ---------------------------------------------------------------- internal

    def _normalize_name(self, name: str) -> str:
        """Turn *name* into a full system name. Accepts either form."""
        if name.startswith(self.full_prefix):
            return name
        # Treat as bare address. Validate it parses cleanly.
        return SystemName.make(self.system_prefix, self.type_letter, name)

    def _create(self, system_name: str) -> T:
        """Subclasses must override to construct their concrete bean type."""
        raise NotImplementedError(
            f"{type(self).__name__}._create must be implemented by subclasses"
        )

    # Convenience for tests / persistence ------------------------------------

    def _set_user_name(self, bean: T, new_name: str | None) -> None:
        """Rename *bean* maintaining the user-name index. Internal."""
        # Remove old binding if any
        old = bean.user_name
        if old and self._by_user.get(old) is bean:
            del self._by_user[old]
        bean.user_name = new_name
        if new_name:
            if new_name in self._by_user and self._by_user[new_name] is not bean:
                raise ValueError(f"user_name {new_name!r} already in use")
            self._by_user[new_name] = bean

    def to_dict(self) -> dict[str, Any]:
        """Serializable snapshot used by persistence layer."""
        return {
            "type_letter": self.type_letter,
            "system_prefix": self.system_prefix,
            "beans": [
                {
                    "system_name": b.system_name,
                    "user_name": b.user_name,
                    "comment": b.comment,
                }
                for b in self._by_system.values()
            ],
        }
