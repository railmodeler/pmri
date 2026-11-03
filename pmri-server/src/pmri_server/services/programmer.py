"""``ProgrammerService`` -- JSON wire interface for CV programming.

JMRI's stock JSON API doesn't expose decoder programming, so we ship a
small dedicated topic at ``type: "programmer"``. Wire shapes:

* ``method=get``, ``data={"cv": N, "mode": "service"|"ops", "address"?: N}``
  -> reads the CV, returns ``{"cv": N, "value": 0..255}``.
* ``method=post``, ``data={"cv": N, "value": V, ...}`` -> writes the CV.
* ``method=list``, ``data={"mode": "service"|"ops"}`` -> returns
  ``{"mode": ..., "available": true|false, "ops_address": ...}``.

The service uses the active connection's ``ProgrammerManager``. If the
connection has no programmer (default in-memory connections), every
request returns ``available: false``.
"""
from __future__ import annotations

from typing import Any, cast

from pmri_core.beans.programmer import Programmer, ProgrammingMode

from pmri_server.service import JsonService, JsonServiceError

# CV value range. JMRI ships CVs in the standard NMRA 8-bit range.
CV_VALUE_MIN = 0
CV_VALUE_MAX = 255


def _resolve_programmer(ctx_connection: Any, data: dict[str, Any]) -> Programmer:
    """Pick a service or ops-mode programmer from the request data."""
    mode_str = data.get("mode", "service")
    if mode_str == "service":
        programmer = ctx_connection.programmers.global_programmer()
        if programmer is None:
            raise JsonServiceError(
                503,
                f"connection {ctx_connection.name!r} has no service-mode programmer",
            )
        return cast("Programmer", programmer)
    if mode_str == "ops":
        address = data.get("address")
        if not isinstance(address, int):
            raise JsonServiceError(400, "ops-mode programming requires an 'address'")
        long_address = bool(data.get("isLongAddress", False))
        programmer = ctx_connection.programmers.ops_programmer(
            address, long_address=long_address
        )
        if programmer is None:
            raise JsonServiceError(
                503,
                f"connection {ctx_connection.name!r} has no ops-mode programmer",
            )
        return cast("Programmer", programmer)
    raise JsonServiceError(400, f"unknown programmer mode {mode_str!r}")


def _loco_cv_store(ctx: Any) -> Any:
    """Lazily attach a :class:`LocoCvStore` rooted at the active profile.

    Wires a ``default_lookup`` callable that maps ``loco_id`` ->
    ``{cv_number: default_str}`` by consulting the roster (for the loco's
    family/model) + the decoder catalog (for that model's variable
    defaults). When the catalog isn't loaded or the loco has no decoder
    metadata, returns ``{}`` so :meth:`LocoCvStore.set_cv` writes every
    CV (equivalent to pre-refactor behaviour).
    """
    store = getattr(ctx, "_loco_cv_store", None)
    if store is not None:
        return store
    from pmri_core.persistence.profile import Profile  # noqa: PLC0415

    from pmri_server.loco_cv_store import LocoCvStore  # noqa: PLC0415
    profile = getattr(ctx, "_roster_profile", None) or Profile.default()

    def _defaults_for(loco_id: str) -> dict[str, str]:
        """Look up catalog defaults for *loco_id*'s decoder model.

        Failure modes (missing catalog, unknown model, no roster entry)
        all resolve to an empty dict so the write path just skips the
        filter -- losing the "diff-only" optimisation but preserving
        correctness (every CV in memory gets written).
        """
        catalog = getattr(ctx, "_decoder_catalog", None)
        if catalog is None:
            return {}
        # Resolve family/model via the roster service. This runs
        # synchronously inside LocoCvStore.set_cv; the roster service's
        # own read path is sync-safe.
        try:
            from pmri_server.services.roster import (  # noqa: PLC0415
                _roster_service_get_sync,
            )
            entry = _roster_service_get_sync(ctx, loco_id)
        except Exception:  # noqa: BLE001
            return {}
        family = (entry or {}).get("decoderFamily") or ""
        model = (entry or {}).get("decoderModel") or ""
        if not family or not model:
            return {}
        defn = catalog.find(family, model)
        if defn is None:
            return {}
        # Compose per-CV *default bytes* by applying each variable's
        # human-default value through ``variable_apply_to_state``. Bit-
        # mask variables sharing a CV (Address Format + Speed Steps +
        # Power Source Conv on CV 29) each fold their bits into the
        # same byte via read-modify-write against the running snapshot.
        # Split-CV variables (long address = CV 17 + CV 18) produce
        # entries for every touched CV. Skip variable kinds that don't
        # participate in byte-mask composition (SPEED_TABLE, COMPOSITE,
        # SPLIT_DATETIME, CONSTANT).
        from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
            VariableKind,
            variable_apply_to_state,
            variable_is_indexed,
            variable_store_key,
        )
        # String-keyed so plain and indexed CV defaults coexist
        # (``275`` vs ``16.1.275``). Indexed variables get their
        # default bytes stored per compound key; plain variables get
        # str(cv) as before.
        result: dict[str, str] = {}
        int_view: dict[int, int] = {}
        for var in defn.variables:
            if var.default is None:
                continue
            if var.kind in (
                VariableKind.SPEED_TABLE,
                VariableKind.COMPOSITE,
                VariableKind.SPLIT_DATETIME,
                VariableKind.CONSTANT,
            ):
                continue
            if variable_is_indexed(var):
                # Indexed: emit one entry per byte under compound key.
                assert var.split is not None
                base = var.split.primary_cv
                for i in range(var.split.byte_count):
                    key = variable_store_key(var, base + i)
                    shift = 8 * (var.split.byte_count - 1 - i)
                    result[key] = str((int(var.default) >> shift) & 0xFF)
                continue
            try:
                updates = variable_apply_to_state(var, var.default, int_view)
            except Exception:  # noqa: BLE001
                continue
            int_view.update(updates)
            for cv_int, byte in updates.items():
                result[str(cv_int)] = str(byte)
        return result

    store = LocoCvStore(profile=profile, default_lookup=_defaults_for)
    ctx._loco_cv_store = store
    return store


class ProgrammerService(JsonService):
    topic = "programmer"

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        cv = data.get("cv")
        if not isinstance(cv, int):
            raise JsonServiceError(400, "programmer.get requires integer 'cv'")
        programmer = _resolve_programmer(self.ctx.connection, data)
        try:
            value = await programmer.read_cv(cv)
        except TimeoutError:
            raise JsonServiceError(504, f"timeout reading CV {cv}") from None
        except NotImplementedError as e:
            # e.g. NCE ops-mode CV read -- no RailCom on PowerPro.
            raise JsonServiceError(501, str(e)) from None
        except Exception as e:  # noqa: BLE001
            # Connection-specific errors (NceCommandError, LocoNet CS
            # NACKs, DCC-EX parse failures). Surface as 502 so the UI
            # can distinguish from timeouts/protocol errors upstream.
            raise JsonServiceError(502, f"CV {cv} read failed: {e}") from None
        # Record the freshly-read value in the in-memory cache so the
        # next pane render (and subsequent variable_extract_from_state
        # calls) sees it. JMRI Java treats reads as transient -- no
        # disk persistence, no ``.bak`` -- until the user explicitly
        # saves. We mirror that via ``record_read_result`` rather than
        # ``set_cv``. A Read pane over 28 CVs used to translate into
        # 28 atomic disk writes (rough on SD cards); now it's zero.
        loco_id = data.get("locoId") or data.get("loco_id")
        if isinstance(loco_id, str) and loco_id:
            file_name = data.get("fileName") or data.get("file_name")
            if not isinstance(file_name, str):
                file_name = None
            _loco_cv_store(self.ctx).record_read_result(
                loco_id, str(cv), value, file_name,
            )
        return {"cv": cv, "value": value, "mode": programmer.mode.value}

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        cv = data.get("cv")
        value = data.get("value")
        if not isinstance(cv, int):
            raise JsonServiceError(400, "programmer.post requires integer 'cv'")
        if not isinstance(value, int) or not CV_VALUE_MIN <= value <= CV_VALUE_MAX:
            raise JsonServiceError(
                400,
                f"programmer.post requires integer 'value' {CV_VALUE_MIN}..{CV_VALUE_MAX}",
            )
        programmer = _resolve_programmer(self.ctx.connection, data)
        try:
            await programmer.write_cv(cv, value)
        except TimeoutError:
            raise JsonServiceError(504, f"timeout writing CV {cv}") from None
        except NotImplementedError as e:
            raise JsonServiceError(501, str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(502, f"CV {cv} write failed: {e}") from None
        # Persist the committed write to the per-loco CV file if a loco
        # was identified -- keeps the on-disk state in sync with what
        # was actually programmed. JMRI's roster ``fileName`` attribute
        # decouples loco-id from filename (e.g. id=459 fileName=1.xml).
        loco_id = data.get("locoId") or data.get("loco_id")
        if isinstance(loco_id, str) and loco_id:
            file_name = data.get("fileName") or data.get("file_name")
            if not isinstance(file_name, str):
                file_name = None
            _loco_cv_store(self.ctx).set_cv(loco_id, str(cv), value, file_name)
        return {"cv": cv, "value": value, "mode": programmer.mode.value}

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Report what programmers are available on the active connection."""
        out: list[dict[str, Any]] = []
        global_prog = self.ctx.connection.programmers.global_programmer()
        out.append({
            "mode": ProgrammingMode.SERVICE_DIRECT.value,
            "available": global_prog is not None,
        })
        # Ops-mode availability per-address; we report just the manager-level capability.
        try:
            ops_capable = (
                self.ctx.connection.programmers.ops_programmer(3) is not None
            )
        except Exception:  # noqa: BLE001
            ops_capable = False
        out.append({
            "mode": ProgrammingMode.OPS_MODE.value,
            "available": ops_capable,
        })
        return out
