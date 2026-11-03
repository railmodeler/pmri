"""DCC-EX CV programmer.

Wire protocol, verified against
``DCC-EX/CommandStation-EX/DCCEXParser.cpp`` (upstream ``master``,
callbacks ``callback_R`` / ``callback_W`` / ``callback_Vbyte`` /
``callback_r``):

* ``<W cv value>`` -- service-mode CV write. Response:
  ``<r cv value>`` on success, ``<r cv -1>`` on failure.
* ``<R cv>`` -- service-mode CV read. Response: ``<v cv value>`` on
  success, ``<v cv -1>`` on failure. *Note the different letter*:
  DCC-EX uses ``v`` for read replies and ``r`` for write acks. Older
  versions and some third-party firmwares reportedly emit ``r`` for
  both; the reply parser here accepts either so we work against both.
* ``<w cab cv value>`` (lowercase ``w``) -- ops-mode CV write to
  *cab*. Fire-and-forget: no reply.
* ``<r cab cv>`` (lowercase ``r``) -- ops-mode CV read on the main via
  RailCom. Response: ``<r cab cv value>`` (3 args) on success,
  ``<r cab cv -1>`` on RailCom no-response. The CS silently drops the
  request if RailCom isn't enabled in firmware, so callers see this
  as a plain timeout -- see :meth:`DccexProgrammer.read_cv_ops`.

Service-mode operations block normal layout traffic on the CS; expect
a ~1-3s wait per CV. Ops-mode writes are fire-and-forget. Ops-mode
reads require RailCom on both the CS and the decoder; timeouts on
non-RailCom setups are normal.
"""
from __future__ import annotations

import asyncio
import logging

from pmri_core.beans.programmer import Programmer, ProgrammerManager, ProgrammingMode

from pmri_dccex.codec import DccexMessage
from pmri_dccex.traffic import DccexTrafficController

_log = logging.getLogger(__name__)


class DccexProgrammer:
    """Direct programmer driver -- used by both service and ops adapters."""

    def __init__(self, tc: DccexTrafficController) -> None:
        self._tc = tc
        self._busy = asyncio.Lock()

    async def write_cv_service(
        self, cv: int, value: int, *, timeout: float = 6.0
    ) -> None:
        async with self._busy:
            await self._send_and_wait_r(
                DccexMessage(command="W", args=(str(cv), str(value))),
                expected_cv=cv,
                timeout=timeout,
            )

    async def read_cv_service(self, cv: int, *, timeout: float = 6.0) -> int:
        async with self._busy:
            return await self._send_and_wait_r(
                DccexMessage(command="R", args=(str(cv),)),
                expected_cv=cv,
                timeout=timeout,
            )

    async def write_cv_ops(
        self, address: int, cv: int, value: int, *, timeout: float = 2.0
    ) -> None:
        """Fire-and-forget -- ops-mode writes don't ack in DCC-EX's text protocol."""
        async with self._busy:
            await self._tc.send(DccexMessage(
                command="w", args=(str(address), str(cv), str(value)),
            ))

    async def read_cv_ops(
        self, address: int, cv: int, *, timeout: float = 6.0,
    ) -> int:
        """RailCom-backed ops-mode CV read on the main track.

        Wire: ``<r cab cv>`` request -> ``<r cab cv value>`` reply. The
        CS's ``r`` handler explicitly requires ``DCCWaveform::isRailcom()``
        to return true; if RailCom is disabled in the firmware config
        the request is silently dropped, so a caller sees this as a
        timeout rather than a distinctive error. The timeout message
        surfaces that possibility explicitly.

        Failure (``value == -1``) surfaces as :class:`RuntimeError`
        matching the same-shaped failure on the service-mode path so
        callers can use one exception filter for both.
        """
        async with self._busy:
            return await self._send_and_wait_ops_read(
                address=address, cv=cv, timeout=timeout,
            )

    async def _send_and_wait_r(
        self, request: DccexMessage, *, expected_cv: int, timeout: float
    ) -> int:
        """Await a 2-arg service-mode reply matching ``expected_cv``.

        Accepts either ``<r cv value>`` (write ack + some firmwares'
        read replies) or ``<v cv value>`` (canonical read reply per
        DCC-EX upstream). Callers pass the CV they're expecting; a
        reply for a different CV is ignored so an unrelated CS
        message doesn't complete our future prematurely.
        """
        future: asyncio.Future[int] = asyncio.get_event_loop().create_future()

        async def listener(msg: DccexMessage) -> None:
            if future.done():
                return
            # Accept both letters -- see the module docstring.
            if msg.command not in ("r", "v") or len(msg.args) != 2:
                return
            try:
                cv = int(msg.args[0])
                value = int(msg.args[1])
            except ValueError:
                return
            if cv != expected_cv:
                return
            if value < 0:
                future.set_exception(
                    RuntimeError(f"DCC-EX programming op failed on CV {cv}")
                )
            else:
                future.set_result(value)

        disconnect = self._tc.inbound.connect(listener)
        try:
            await self._tc.send(request)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            disconnect()

    async def _send_and_wait_ops_read(
        self, *, address: int, cv: int, timeout: float,
    ) -> int:
        """Send ``<r cab cv>`` and await ``<r cab cv value>`` (3 args).

        The 3-arg shape lets the listener distinguish ops-mode
        replies from service-mode replies unambiguously -- a 2-arg
        ``<r ...>`` is a write ack, a 3-arg ``<r ...>`` is a RailCom
        read result.
        """
        future: asyncio.Future[int] = asyncio.get_event_loop().create_future()

        async def listener(msg: DccexMessage) -> None:
            if future.done():
                return
            if msg.command != "r" or len(msg.args) != 3:
                return
            try:
                cab = int(msg.args[0])
                got_cv = int(msg.args[1])
                value = int(msg.args[2])
            except ValueError:
                return
            if cab != address or got_cv != cv:
                return
            if value < 0:
                future.set_exception(RuntimeError(
                    f"DCC-EX ops-mode read failed on cab={address} CV={cv} "
                    "(no RailCom response)"
                ))
            else:
                future.set_result(value)

        disconnect = self._tc.inbound.connect(listener)
        try:
            await self._tc.send(DccexMessage(
                command="r", args=(str(address), str(cv)),
            ))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"DCC-EX ops-mode read timed out on cab={address} CV={cv}. "
                "The CS silently drops <r ...> if RailCom is disabled -- "
                "verify the firmware was built with RAILCOM enabled and "
                "that the decoder supports RailCom cutout."
            ) from e
        finally:
            disconnect()


class DccexServiceProgrammer(Programmer):
    """Service-mode adapter satisfying the core ABC."""

    def __init__(self, inner: DccexProgrammer) -> None:
        self._inner = inner

    @property
    def mode(self) -> ProgrammingMode:
        return ProgrammingMode.SERVICE_DIRECT

    async def read_cv(self, cv: int) -> int:
        return await self._inner.read_cv_service(cv)

    async def write_cv(self, cv: int, value: int) -> None:
        await self._inner.write_cv_service(cv, value)


class DccexOpsProgrammer(Programmer):
    """Ops-mode adapter scoped to a specific DCC address."""

    def __init__(self, inner: DccexProgrammer, address: int, *, long_address: bool) -> None:
        self._inner = inner
        self._address = address
        self._long = long_address

    @property
    def mode(self) -> ProgrammingMode:
        return ProgrammingMode.OPS_MODE

    @property
    def address(self) -> int:
        return self._address

    async def read_cv(self, cv: int) -> int:
        """RailCom-backed ops-mode CV read for this address.

        Raises :class:`RuntimeError` when the CS times out (usually
        because RailCom isn't enabled in the firmware config) or
        returns ``-1`` (decoder didn't answer via RailCom). Callers
        that want a "try RailCom, fall back to write-and-read-back"
        strategy should catch that exception at their layer.
        """
        return await self._inner.read_cv_ops(self._address, cv)

    async def write_cv(self, cv: int, value: int) -> None:
        await self._inner.write_cv_ops(self._address, cv, value)


class DccexProgrammerManager(ProgrammerManager):
    def __init__(self, inner: DccexProgrammer) -> None:
        self._inner = inner
        self._service_singleton: DccexServiceProgrammer | None = None

    def global_programmer(self) -> DccexServiceProgrammer:
        if self._service_singleton is None:
            self._service_singleton = DccexServiceProgrammer(self._inner)
        return self._service_singleton

    def ops_programmer(self, address: int, *, long_address: bool = False) -> DccexOpsProgrammer:
        return DccexOpsProgrammer(self._inner, address, long_address=long_address)

    @property
    def inner(self) -> DccexProgrammer:
        return self._inner
