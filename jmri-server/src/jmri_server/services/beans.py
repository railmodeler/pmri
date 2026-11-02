"""Per-bean JSON services -- turnout, sensor, light, signal heads/masts, memory,
reporter, block, route.

Each service follows the same pattern:

* ``on_get(data)``    -- look up by ``data["name"]``, project to JSON shape
* ``on_list(data)``   -- project every bean of the type
* ``on_put(data)``    -- get-or-create + apply mutations
* ``on_post(data)``   -- find + apply mutations
* ``on_delete(data)`` -- deregister

The JSON shapes match the schemas in ``stage-0/contracts/json-schema/<topic>/``.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from jmri_core.bean import AbstractNamedBean
from jmri_core.beans.block import Block
from jmri_core.beans.light import Light
from jmri_core.beans.memory import Memory
from jmri_core.beans.reporter import Reporter
from jmri_core.beans.route import Route
from jmri_core.beans.sensor import Sensor
from jmri_core.beans.signal_head import SignalHead
from jmri_core.beans.signal_mast import SignalMast
from jmri_core.beans.turnout import Turnout
from jmri_core.manager import Manager
from jmri_core.state import SignalAppearance, TurnoutFeedback

from jmri_server.service import JsonService, JsonServiceError
from jmri_server.wire_states import (
    light_from_wire,
    sensor_from_wire,
    to_wire,
    turnout_from_wire,
)

# --- shared helpers --------------------------------------------------------


def _common_named_bean(bean: AbstractNamedBean) -> dict[str, Any]:
    """The required fields on every NamedBean JSON payload.

    Omits the ``writable`` capability flag when the bean is in its
    default (writable=True) state so the payload stays within JMRI's
    canonical JSON schema (``additionalProperties: false``). Observe-only
    drivers like ``jmri-ncemonitor`` mark their beans ``writable=False``;
    only then does the field appear on the wire. Clients that don't
    know about the field see no change; clients that do know default
    to writable=True when the field is absent.
    """
    payload: dict[str, Any] = {
        "name": bean.system_name,
        "userName": bean.user_name,
        "comment": bean.comment,
        "properties": [
            {"key": k, "value": v if v is None or isinstance(v, str) else str(v)}
            for k, v in bean.properties.items()
        ],
    }
    if not getattr(bean, "writable", True):
        payload["writable"] = False
    return payload


def _require_name(data: dict[str, Any] | None) -> str:
    if not data or "name" not in data:
        raise JsonServiceError(400, "request data missing required 'name'")
    return str(data["name"])


class _BeanService(JsonService):
    """Base class encapsulating get/list/put/post/delete plumbing.

    Concrete bean services set :attr:`topic` and override :meth:`manager`,
    :meth:`project`, and (when mutations are supported) :meth:`apply`.
    """

    def manager(self) -> Manager[Any]:
        raise NotImplementedError(
            f"{type(self).__name__}.manager must return the bean manager"
        )

    def project(self, bean: Any) -> dict[str, Any]:
        raise NotImplementedError(
            f"{type(self).__name__}.project must convert a bean to its wire shape"
        )

    async def apply(self, bean: Any, data: dict[str, Any]) -> None:
        """Apply mutations from a client request. Default: no-op."""

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        name = _require_name(data)
        bean = self.manager().get(name)
        if bean is None:
            raise JsonServiceError(404, f"no {self.topic} with name {name!r}")
        return self.project(bean)

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        return [self.project(b) for b in self.manager()]

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        name = _require_name(data or {})
        bean = await self.manager().provide(name)
        await self._apply_common(bean, data or {})
        await self.apply(bean, data or {})
        self._persist(bean)
        return self.project(bean)

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        name = _require_name(data or {})
        bean = self.manager().get(name)
        if bean is None:
            raise JsonServiceError(404, f"no {self.topic} with name {name!r}")
        await self._apply_common(bean, data or {})
        await self.apply(bean, data or {})
        self._persist(bean)
        return self.project(bean)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        name = _require_name(data)
        bean = self.manager().get(name)
        if bean is None:
            raise JsonServiceError(404, f"no {self.topic} with name {name!r}")
        await self.manager().deregister(bean)
        self._persist_delete(name)
        return {"name": name}

    def _persist(self, bean: AbstractNamedBean) -> None:
        """Hook: subclasses override to write the bean back to ConfigStore."""

    def _persist_delete(self, system_name: str) -> None:
        """Hook: subclasses override to remove the bean from ConfigStore."""

    async def _apply_common(self, bean: AbstractNamedBean, data: dict[str, Any]) -> None:
        if "userName" in data:
            self.manager()._set_user_name(bean, data["userName"])
        if "comment" in data:
            bean.comment = data["comment"]


# --- Turnout ----------------------------------------------------------------


class TurnoutService(_BeanService):
    topic = "turnout"

    def manager(self) -> Manager[Turnout]:
        return self.ctx.connection.turnouts

    def project(self, bean: Turnout) -> dict[str, Any]:
        return _common_named_bean(bean) | {
            "inverted": bean.inverted,
            "state": to_wire(int(bean.known_state)),
            "feedbackMode": int(bean.feedback_mode),
            "feedbackModes": [int(f) for f in TurnoutFeedback],
        }

    async def apply(self, bean: Turnout, data: dict[str, Any]) -> None:
        if "inverted" in data:
            bean.inverted = bool(data["inverted"])
        if "feedbackMode" in data:
            bean.feedback_mode = TurnoutFeedback(int(data["feedbackMode"]))
        if "state" in data:
            await bean.set_commanded_state(turnout_from_wire(int(data["state"])))

    def _persist(self, bean: AbstractNamedBean) -> None:
        store = getattr(self.ctx, "config_store", None)
        if store is None:
            return
        assert isinstance(bean, Turnout)
        from jmri_core.persistence import TurnoutRow  # noqa: PLC0415
        store.upsert_turnout(TurnoutRow(
            system_name=bean.system_name,
            user_name=bean.user_name or None,
            feedback=bean.feedback_mode.name if bean.feedback_mode else None,
            inverted=bool(bean.inverted),
        ))

    def _persist_delete(self, system_name: str) -> None:
        store = getattr(self.ctx, "config_store", None)
        if store is not None:
            store.remove_turnout(system_name)


# --- Sensor -----------------------------------------------------------------


class SensorService(_BeanService):
    topic = "sensor"

    def manager(self) -> Manager[Sensor]:
        return self.ctx.connection.sensors

    def project(self, bean: Sensor) -> dict[str, Any]:
        return _common_named_bean(bean) | {
            "inverted": bean.inverted,
            "state": to_wire(int(bean.known_state)),
        }

    async def apply(self, bean: Sensor, data: dict[str, Any]) -> None:
        if "inverted" in data:
            bean.inverted = bool(data["inverted"])
        if "state" in data:
            await bean._set_known_state(sensor_from_wire(int(data["state"])))

    def _persist(self, bean: AbstractNamedBean) -> None:
        store = getattr(self.ctx, "config_store", None)
        if store is None:
            return
        assert isinstance(bean, Sensor)
        from jmri_core.persistence import SensorRow  # noqa: PLC0415
        store.upsert_sensor(SensorRow(
            system_name=bean.system_name,
            user_name=bean.user_name or None,
            inverted=bool(bean.inverted),
        ))

    def _persist_delete(self, system_name: str) -> None:
        store = getattr(self.ctx, "config_store", None)
        if store is not None:
            store.remove_sensor(system_name)


# --- Light ------------------------------------------------------------------


class LightService(_BeanService):
    topic = "light"

    def manager(self) -> Manager[Light]:
        return self.ctx.connection.lights

    def project(self, bean: Light) -> dict[str, Any]:
        return _common_named_bean(bean) | {"state": to_wire(int(bean.state))}

    async def apply(self, bean: Light, data: dict[str, Any]) -> None:
        if "state" in data:
            await bean.set_state(light_from_wire(int(data["state"])))

    def _persist(self, bean: AbstractNamedBean) -> None:
        store = getattr(self.ctx, "config_store", None)
        if store is None:
            return
        assert isinstance(bean, Light)
        from jmri_core.persistence import LightRow  # noqa: PLC0415
        store.upsert_light(LightRow(
            system_name=bean.system_name,
            user_name=bean.user_name or None,
        ))

    def _persist_delete(self, system_name: str) -> None:
        store = getattr(self.ctx, "config_store", None)
        if store is not None:
            store.remove_light(system_name)


# --- Signal Head ------------------------------------------------------------


_HEAD_APPEARANCE_NAMES = {
    SignalAppearance.DARK: "Dark",
    SignalAppearance.RED: "Red",
    SignalAppearance.YELLOW: "Yellow",
    SignalAppearance.GREEN: "Green",
    SignalAppearance.LUNAR: "Lunar",
    SignalAppearance.FLASHRED: "Flashing Red",
    SignalAppearance.FLASHYELLOW: "Flashing Yellow",
    SignalAppearance.FLASHGREEN: "Flashing Green",
    SignalAppearance.FLASHLUNAR: "Flashing Lunar",
}
HELD_WIRE_STATE = 100  # per JMRI's signalHead schema


class SignalHeadService(_BeanService):
    topic = "signalHead"

    def manager(self) -> Manager[SignalHead]:
        return self.ctx.connection.signal_heads

    def project(self, bean: SignalHead) -> dict[str, Any]:
        appearance = bean.commanded_appearance
        state = HELD_WIRE_STATE if bean.held else int(appearance)
        return _common_named_bean(bean) | {
            "userName": bean.user_name or "",  # schema says string, not nullable
            "lit": appearance != SignalAppearance.DARK,
            "held": bean.held,
            "state": state,
            "appearance": int(appearance),
            "appearanceName": _HEAD_APPEARANCE_NAMES.get(appearance, "Unknown"),
        }

    async def apply(self, bean: SignalHead, data: dict[str, Any]) -> None:
        if "held" in data:
            await bean.set_held(bool(data["held"]))
        if "appearance" in data:
            await bean.set_appearance(SignalAppearance(int(data["appearance"])))


# --- Signal Mast ------------------------------------------------------------


class SignalMastService(_BeanService):
    topic = "signalMast"

    def manager(self) -> Manager[SignalMast]:
        return self.ctx.connection.signal_masts

    def project(self, bean: SignalMast) -> dict[str, Any]:
        # state per schema: "dark" if not lit, "held" if held, else the aspect name.
        if bean.held:
            state = "Held"
        elif not bean.aspect:
            state = "Dark"
        else:
            state = bean.aspect
        projection = _common_named_bean(bean) | {
            "aspect": bean.aspect,
            "lit": bool(bean.aspect) and not bean.held,
            "held": bean.held,
            "state": state,
            "signalSystem": bean.signal_system or None,
        }
        # Best-effort valid-aspect list from the signal-system loader.
        # Adds ``validAspects: [str, ...]`` when the mast carries a
        # ``signal_system`` that the loader knows about; skipped when
        # the loader hasn't been touched yet (avoids constructing it
        # for every bean-list request when no XML root is configured).
        loader = getattr(self.ctx, "_signal_system_loader", None)
        if loader is not None and bean.signal_system:
            system = loader.get(bean.signal_system)
            if system is not None:
                projection["validAspects"] = [
                    a.name for a in system.aspects
                ]
        return projection

    async def apply(self, bean: SignalMast, data: dict[str, Any]) -> None:
        if "signalSystem" in data:
            v = data["signalSystem"]
            bean.signal_system = str(v) if v is not None else ""
        if "aspect" in data:
            await bean.set_aspect(str(data["aspect"]))
        if "held" in data:
            await bean.set_held(bool(data["held"]))


# --- Memory -----------------------------------------------------------------


class MemoryService(_BeanService):
    topic = "memory"

    def manager(self) -> Manager[Memory]:
        return self.ctx.connection.memories

    def project(self, bean: Memory) -> dict[str, Any]:
        v = bean.value
        return _common_named_bean(bean) | {
            "value": v if v is None or isinstance(v, str) else str(v),
        }

    async def apply(self, bean: Memory, data: dict[str, Any]) -> None:
        if "value" in data:
            await bean.set_value(data["value"])


# --- Reporter ---------------------------------------------------------------


class ReporterService(_BeanService):
    topic = "reporter"

    def manager(self) -> Manager[Reporter]:
        return self.ctx.connection.reporters

    def project(self, bean: Reporter) -> dict[str, Any]:
        report = bean.current_report
        last = bean.last_report
        # ``value`` defaults to a sentinel string per JMRI convention.
        value = (
            str(report) if report not in (None, "") else "<no report>"
        )
        # state mirrors active/inactive based on whether we have a report.
        state = 2 if report not in (None, "") else 4  # ACTIVE / INACTIVE wire
        return _common_named_bean(bean) | {
            "state": state,
            "report": None if report is None else str(report),
            "lastReport": None if last is None else str(last),
            "value": value,
        }

    async def apply(self, bean: Reporter, data: dict[str, Any]) -> None:
        if "report" in data:
            await bean.set_report(data["report"])


# --- Block ------------------------------------------------------------------


class BlockService(_BeanService):
    topic = "block"

    def manager(self) -> Manager[Block]:
        return self.ctx.connection.blocks

    def project(self, bean: Block) -> dict[str, Any]:
        v = bean.value
        return _common_named_bean(bean) | {
            "state": to_wire(int(bean.state)),
            "value": v if v is None or isinstance(v, str) else str(v),
            "sensor": bean.sensor.system_name if bean.sensor is not None else None,
            "reporter": None,
            "speed": "",
            "curvature": 0,
            "direction": 0,
            "length": 0.0,
            "permissive": False,
            "speedLimit": -1.0,
            "denied": [],
        }

    async def apply(self, bean: Block, data: dict[str, Any]) -> None:
        if "value" in data:
            await bean.set_value(data["value"])
        if "sensor" in data:
            sensor_name = data["sensor"]
            sensor = (
                self.ctx.connection.sensors.get(sensor_name) if sensor_name else None
            )
            bean.attach_sensor(sensor)


# --- Route ------------------------------------------------------------------


class RouteService(_BeanService):
    topic = "route"

    def manager(self) -> Manager[Route]:
        return self.ctx.connection.routes

    def project(self, bean: Route) -> dict[str, Any]:
        return _common_named_bean(bean) | {"state": to_wire(int(bean.state))}

    async def apply(self, bean: Route, data: dict[str, Any]) -> None:
        # JMRI's wire convention: posting state=8 (INCONSISTENT) triggers a fire.
        # We accept either state == any non-zero or an explicit "fire" boolean.
        if data.get("fire") or int(data.get("state", 0)) != 0:
            try:
                await bean.fire()
            except RuntimeError as e:
                raise JsonServiceError(409, str(e)) from None


# Suppress unused-import warning -- Callable kept for future hook fields.
_RESERVED: tuple[type, ...] = (Awaitable, Callable)  # type: ignore[assignment]
