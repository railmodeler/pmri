"""CV programming actions.

Two actions in this module:

* :class:`WriteCV` -- write a value into one CV.
* :class:`ReadCV` -- read one CV, optionally into a memory bean.

Both dispatch through the active connection's
:class:`~pmri_core.beans.programmer.ProgrammerManager`:

* When ``address`` is ``None``, they use the **service-mode** programmer
  (``programmers.global_programmer()``). Fastest and most reliable, but
  requires the decoder on the programming track.
* When ``address`` is an int, they use an **ops-mode** programmer for
  that DCC address. Writes always work on any DCC layout; reads only
  work on layouts with RailCom feedback -- non-RailCom drivers time
  out and the failure surfaces as a logged warning.

Timeouts, connection errors, and "no programmer available" all downgrade
to a logged warning instead of raising: matches the rest of the LogixNG
action taxonomy where a broken layout keeps automations running rather
than nuking the whole rule tree on the first fault.
"""
from __future__ import annotations

from typing import Any, ClassVar

from pmri_core.beans.programmer import Programmer

from pmri_logixng.context import ExecutionContext
from pmri_logixng.registry import register
from pmri_logixng.tree import Action


def _resolve_programmer(
    ctx: ExecutionContext,
    address: int | None,
    long_address: bool,
) -> Programmer | None:
    """Fetch a service-mode or ops-mode programmer, or None if
    unavailable. The caller logs the missing-programmer case."""
    pm = ctx.connection.programmers
    if address is None:
        return pm.global_programmer()
    return pm.ops_programmer(address, long_address=long_address)


def _validate_cv(cv: int) -> None:
    """CV numbers per NMRA are 1..1024. Guard against off-by-one bugs
    (CV 0 doesn't exist) and configuration typos."""
    if not isinstance(cv, int) or not (1 <= cv <= 1024):
        raise ValueError(f"CV must be an int in 1..1024, got {cv!r}")


def _validate_value(value: int) -> None:
    """A DCC CV byte is 0..255."""
    if not isinstance(value, int) or not (0 <= value <= 255):
        raise ValueError(f"CV value must be an int in 0..255, got {value!r}")


@register
class WriteCV(Action):
    """Write *value* into CV *cv*.

    * ``address=None`` -> service-mode (programming track).
    * ``address=N`` -> ops-mode, decoder at DCC address N.
      Set ``long_address=True`` for a 4-digit / 128-9999 address.
    """

    type_tag: ClassVar[str] = "action:write-cv"

    def __init__(
        self,
        *,
        cv: int,
        value: int,
        address: int | None = None,
        long_address: bool = False,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        _validate_cv(cv)
        _validate_value(value)
        super().__init__(user_name=user_name, node_id=node_id)
        self.cv = cv
        self.value = value
        self.address = address
        self.long_address = long_address

    async def execute(self, ctx: ExecutionContext) -> None:
        prog = _resolve_programmer(ctx, self.address, self.long_address)
        if prog is None:
            ctx.log.warning(
                "WriteCV: no programmer available (address=%r, long=%s)",
                self.address, self.long_address,
            )
            return
        ctx.emit_trace(
            f"write CV{self.cv}={self.value} "
            f"(address={self.address}, long={self.long_address})",
        )
        try:
            await prog.write_cv(self.cv, self.value)
        except TimeoutError:
            ctx.log.warning(
                "WriteCV: CV%d write timed out (address=%r)",
                self.cv, self.address,
            )
        except Exception:  # noqa: BLE001 -- driver-specific errors
            ctx.log.exception(
                "WriteCV: CV%d write raised (address=%r)",
                self.cv, self.address,
            )

    def extra_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cv": self.cv, "value": self.value}
        if self.address is not None:
            out["address"] = self.address
            out["long_address"] = self.long_address
        return out


@register
class ReadCV(Action):
    """Read CV *cv*; optionally store the result in memory *into_memory*.

    Addressing rules are the same as :class:`WriteCV`. On timeout / no
    programmer / decoder-refused, the memory bean (if named) is set to
    ``None`` so downstream expressions can detect the failure.
    """

    type_tag: ClassVar[str] = "action:read-cv"

    def __init__(
        self,
        *,
        cv: int,
        address: int | None = None,
        long_address: bool = False,
        into_memory: str | None = None,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        _validate_cv(cv)
        super().__init__(user_name=user_name, node_id=node_id)
        self.cv = cv
        self.address = address
        self.long_address = long_address
        self.into_memory = into_memory

    async def execute(self, ctx: ExecutionContext) -> None:
        prog = _resolve_programmer(ctx, self.address, self.long_address)
        if prog is None:
            ctx.log.warning(
                "ReadCV: no programmer available (address=%r, long=%s)",
                self.address, self.long_address,
            )
            await self._store(ctx, None)
            return
        ctx.emit_trace(
            f"read CV{self.cv} "
            f"(address={self.address}, long={self.long_address})",
        )
        try:
            value: int | None = await prog.read_cv(self.cv)
        except TimeoutError:
            ctx.log.warning(
                "ReadCV: CV%d read timed out (address=%r)",
                self.cv, self.address,
            )
            value = None
        except Exception:  # noqa: BLE001
            ctx.log.exception(
                "ReadCV: CV%d read raised (address=%r)",
                self.cv, self.address,
            )
            value = None
        await self._store(ctx, value)

    async def _store(
        self, ctx: ExecutionContext, value: int | None,
    ) -> None:
        if self.into_memory is None:
            return
        bean = ctx.connection.memories.get(self.into_memory)
        if bean is None:
            ctx.log.warning(
                "ReadCV: no memory %r for result", self.into_memory,
            )
            return
        await bean.set_value(value)

    def extra_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cv": self.cv}
        if self.address is not None:
            out["address"] = self.address
            out["long_address"] = self.long_address
        if self.into_memory is not None:
            out["into_memory"] = self.into_memory
        return out
