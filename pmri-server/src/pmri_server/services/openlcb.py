"""OpenLCB discovery + CS management topics.

Two JSON services expose the OpenLCB layer we built in
:mod:`pmri_openlcb`:

* ``openlcb`` -- bus roster & inspection. ``list`` returns every
  alias we've heard from with cached SNIP / PIP / produced-events /
  consumed-events data. ``get {"alias": N}`` returns one node's
  detail. ``post {"action": ...}`` verbs: ``discover`` (broadcast
  Verify NID Global + Identify Events Global to repopulate maps),
  ``probe_snip {"alias": N}`` (fetch SNIP), ``probe_pip {"alias": N}``
  (fetch PIP flags), ``search {"address": N, "long_address": bool}``
  (traction search -- find loco nodes by DCC address).
* ``openlcbCS`` -- hosted train nodes. ``list`` snapshots every
  currently-hosted loco. Add / remove is deferred until the
  multi-connection story lands; today the CS is wired from Python
  code once the OpenLCB + DCC connections are both open.

Both services error out with a clear 400 when the active
``ctx.connection`` isn't an :class:`~pmri_openlcb.OlcbConnection` --
they're driver-specific and shouldn't silently succeed on an NCE or
LocoNet bus.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from pmri_server.service import JsonService, JsonServiceError

# Import from pmri_openlcb at module load. Missing package ->
# ImportError propagates up through ``services/__init__``; the
# service list ends up without OpenLCB entries, which is the right
# behaviour for a rewrite install with the driver deselected.
try:
    from pmri_openlcb import (
        FIELD_TYPES,
        Cdi,
        CdiBit,
        CdiEventId,
        CdiFloat,
        CdiGroup,
        CdiInt,
        CdiSegment,
        CdiString,
        EventId,
        Fdi,
        FdiFunction,
        FdiGroup,
        FdiSegment,
        MemoryClient,
        NodeId,
        OlcbConnection,
        ProtocolFlag,
        dcc_to_train_node_id,
        fetch_cdi,
        fetch_fdi,
        read_field_raw,
        write_field_raw,
    )
    from pmri_openlcb.datagrams import DatagramEngine
    _OPENLCB_AVAILABLE = True
except ImportError:   # pragma: no cover -- install-time optional
    _OPENLCB_AVAILABLE = False


if TYPE_CHECKING:
    from pmri_openlcb.command_station import CommandStationNode, HostedLoco


# ---- helpers -------------------------------------------------------------


def _require_olcb(ctx) -> OlcbConnection:  # noqa: ANN001
    """Return ``ctx.connection`` as an :class:`OlcbConnection`, or
    raise 400 when the active connection isn't OpenLCB."""
    if not _OPENLCB_AVAILABLE:
        raise JsonServiceError(
            400, "OpenLCB driver (pmri_openlcb) is not installed",
        )
    conn = ctx.connection
    if not isinstance(conn, OlcbConnection):
        raise JsonServiceError(
            400,
            f"active connection is not OpenLCB "
            f"(got {type(conn).__name__})",
        )
    return conn


def _alias_from(data: dict[str, Any] | None) -> int:
    """Extract a 12-bit alias from a request. Accepts a Python int
    or a hex string (``"0x222"`` / ``"222"``)."""
    if not data or "alias" not in data:
        raise JsonServiceError(400, "request requires 'alias'")
    raw = data["alias"]
    try:
        if isinstance(raw, str):
            alias = int(raw, 16 if raw.lower().startswith("0x") else 0)
        else:
            alias = int(raw)
    except (TypeError, ValueError):
        raise JsonServiceError(
            400, f"'alias' must be an int, got {raw!r}",
        ) from None
    if not 0 < alias <= 0xFFF:
        raise JsonServiceError(
            400, f"alias must be 12-bit and non-zero, got {alias:#x}",
        )
    return alias


def _wire_release_forgets(ctx, cache: dict[int, Any]) -> None:  # noqa: ANN001
    """Register a cache with the connection's AMR watcher so entries
    for released aliases drop automatically. No-op when the active
    connection isn't OpenLCB (the caches still work -- they just don't
    self-clean)."""
    conn = getattr(ctx, "connection", None)
    if _OPENLCB_AVAILABLE and isinstance(conn, OlcbConnection):
        conn.alias_release.on_release(lambda alias: cache.pop(alias, None))


def _project_top_event(event: str, count: int) -> dict[str, Any]:
    """One row of the bus_stats.top_events list. Looks up the
    dotted event ID in the S-9.7.3 well-known catalog so a UI can
    render "Emergency Off" alongside the hex."""
    try:
        from pmri_openlcb import EventId, well_known_name  # noqa: PLC0415
        name = well_known_name(EventId.parse(event))
    except Exception:  # noqa: BLE001
        name = None
    return {"event": event, "count": count, "name": name}


def _format_timestamp(ts: float) -> str:
    """Format a wall-clock ``time.time()`` value as ``HH:MM:SS.mmm``
    in UTC. Raw epoch seconds stay in the ``timestamp`` field so a
    CSV export or a JSON client that wants machine-parseable time
    still has the original value."""
    from datetime import datetime, timezone  # noqa: PLC0415
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%H:%M:%S") + f".{dt.microsecond // 1000:03d}"


def _hex_or_none(raw: Any) -> int | None:
    """Parse *raw* as a hex int (with or without ``0x`` prefix) or
    return None if empty / unparseable. Used by monitor filters where
    a partially-typed value shouldn't fail the request -- the operator
    is still typing.

    Bare numeric strings default to base 16 (typing ``222`` in the
    alias filter matches alias 0x222, which is what an OpenLCB
    operator expects); ``0x222`` also works.
    """
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return None
            if s.lower().startswith("0x"):
                s = s[2:]
            return int(s, 16)
        return int(raw)
    except (TypeError, ValueError):
        return None


def _snip_cache(ctx) -> dict[int, Any]:  # noqa: ANN001
    """Lazy per-connection SNIP cache stashed on the server context.
    Populated by :meth:`OpenLcbService._probe_all` from each alias's
    :meth:`SnipClient.probe`. Auto-drops any alias whose peer sends
    AMR."""
    cache = getattr(ctx, "_openlcb_snip_cache", None)
    if cache is None:
        cache = {}
        ctx._openlcb_snip_cache = cache
        _wire_release_forgets(ctx, cache)
    return cache


SEND_HISTORY_MAX = 20
"""Rolling depth of the send-frame history. Twenty entries covers
a typical debug session without ballooning."""


def _send_history(ctx) -> list[dict[str, Any]]:  # noqa: ANN001
    """Lazy per-connection rolling list of frames the operator sent
    via ``send_frame``. Newest first. Populated by
    :meth:`OpenLcbService._send_frame` after the frame is on the
    wire so a resend button can echo the exact bytes back."""
    hist = getattr(ctx, "_openlcb_send_history", None)
    if hist is None:
        hist = []
        ctx._openlcb_send_history = hist
    return hist


def _cdi_cache(ctx, conn) -> dict[int, Any]:  # noqa: ANN001
    """Lazy per-connection cache of parsed CDIs, keyed by peer alias.
    Downloading a large CDI is expensive (KBs of XML over 64-byte
    datagrams); caching the parsed structure makes a fresh browser
    load of the CDI editor page near-instant on the second visit.

    Invalidated on: AMR from that peer (via ``alias_release``),
    stale-peer sweep, explicit ``cdi_invalidate`` action, and after
    each successful ``memory_reboot`` / ``memory_factory_reset``
    (the peer may come back with new CDI)."""
    cache = getattr(ctx, "_openlcb_cdi_cache", None)
    if cache is None:
        cache = {}
        ctx._openlcb_cdi_cache = cache
        # Wire AMR forgets so a peer that goes away drops from
        # cache automatically. Same fan-out point every other
        # cache hooks into.
        if _OPENLCB_AVAILABLE and isinstance(conn, OlcbConnection):
            conn.alias_release.on_release(
                lambda alias: cache.pop(alias, None),
            )
    return cache


def _pip_cache(ctx) -> dict[int, Any]:  # noqa: ANN001
    cache = getattr(ctx, "_openlcb_pip_cache", None)
    if cache is None:
        cache = {}
        ctx._openlcb_pip_cache = cache
        _wire_release_forgets(ctx, cache)
    return cache


def _project_snip_info(info) -> dict[str, Any]:  # noqa: ANN001
    return {
        "manufacturer": info.manufacturer,
        "model": info.model,
        "hardware_version": info.hardware_version,
        "software_version": info.software_version,
        "user_name": info.user_name,
        "user_description": info.user_description,
    }


def _project_node(
    conn: OlcbConnection, alias: int, node_id: NodeId | None = None,
    *, ctx=None,  # noqa: ANN001
) -> dict[str, Any]:
    """Bundle everything we know about *alias* into a wire dict.

    Keys are lower_snake_case (matches the rewrite's convention for
    OpenLCB fields; the stock JMRI JSON topic uses camelCase, but no
    OpenLCB topics ship on the stock JSON API so there's no
    established precedent to match). Missing data (SNIP not probed
    yet, PIP flags unknown) surfaces as ``None`` rather than being
    omitted, so a UI can render "unknown" without checking key
    presence.

    When *ctx* is provided, cached SNIP + PIP results
    (populated by :meth:`OpenLcbService._probe_all`) are included
    inline so the projection is a single source of truth for the
    discovery UI.
    """
    nid = node_id or conn.node_id_lookup.get(alias)
    produced = conn.event_roster.produced_by(alias)
    consumed = conn.event_roster.consumed_by(alias)
    produced_ranges = conn.event_roster.produced_ranges_by(alias)
    consumed_ranges = conn.event_roster.consumed_ranges_by(alias)
    # ``last_seen`` is wall-clock epoch seconds; ``seconds_ago`` is
    # the derived age, precomputed so a UI can render "12s ago"
    # without shipping the current server clock too. Both are None
    # for aliases we know about only through the NID map or event
    # roster snapshot (i.e. we've never seen a frame from them since
    # the connection opened).
    last_seen_ts = conn.last_seen.last_seen(alias)
    seconds_ago = (
        time.time() - last_seen_ts if last_seen_ts is not None else None
    )
    result: dict[str, Any] = {
        "alias": f"{alias:#05x}",
        "node_id": nid.dotted() if nid is not None else None,
        "produced_events": [str(ev) for ev in produced],
        "consumed_events": [str(ev) for ev in consumed],
        # Range Identified -- each entry is [start, end] as dotted-hex
        # event ID strings, inclusive on both ends. UIs typically show
        # one row per range: "produces 05...00 through 05...FF".
        "produced_ranges": [
            [str(s), str(e)] for s, e in produced_ranges
        ],
        "consumed_ranges": [
            [str(s), str(e)] for s, e in consumed_ranges
        ],
        "last_seen": last_seen_ts,
        "seconds_ago": seconds_ago,
        "snip": None,
        "pip": None,
    }
    if ctx is not None:
        snip = _snip_cache(ctx).get(alias)
        if snip is not None:
            result["snip"] = _project_snip_info(snip)
        pip = _pip_cache(ctx).get(alias)
        if pip is not None:
            result["pip"] = _project_flags(pip)
    return result


def _project_flags(flags: ProtocolFlag) -> dict[str, Any]:
    """PIP flag mask -> wire dict. ``names`` is the list of set flags
    as canonical strings (``"DATAGRAM"``, ``"SNIP"``, ...); ``raw``
    is the numeric mask for callers who want to test/bitwise it
    themselves."""
    return {
        "names": sorted(f.name for f in ProtocolFlag if f in flags),
        "raw": f"{int(flags):#08x}",
    }


def _project_cdi(cdi: Cdi) -> dict[str, Any]:
    """Serialize a parsed :class:`Cdi` to a JSON-friendly dict.

    The tree keeps its full structure -- segments containing groups
    containing leaves -- so a UI can render the same hierarchy the
    node authors laid out. Every leaf carries a ``type``
    discriminator (``"int"``, ``"string"``, ``"eventid"``,
    ``"float"``, ``"bit"``) plus its wire coordinates
    (``space``/``offset``/``size``) so a subsequent read/write
    doesn't need the parsed tree -- it can round-trip on the raw
    coords alone.
    """
    return {
        "identification": {
            "manufacturer": cdi.identification.manufacturer,
            "model": cdi.identification.model,
            "hardware_version": cdi.identification.hardware_version,
            "software_version": cdi.identification.software_version,
        },
        "supports_acdi": cdi.supports_acdi,
        "segments": [_project_segment(s) for s in cdi.segments],
    }


def _project_segment(seg: CdiSegment) -> dict[str, Any]:
    return {
        "name": seg.name,
        "description": seg.description,
        "origin": seg.origin,
        "space": seg.space,
        "size": seg.size,
        "children": [_project_cdi_entry(c, seg.space) for c in seg.children],
    }


def _project_cdi_entry(entry, space: int) -> dict[str, Any]:  # noqa: ANN001
    """Recurse into a group or serialize one leaf. *space* is
    passed down so every leaf carries the memory space it lives in
    -- the UI's write button then has every coord it needs without
    walking back up the tree."""
    if isinstance(entry, CdiGroup):
        return {
            "kind": "group",
            "name": entry.name,
            "description": entry.description,
            "replication": entry.replication,
            "replication_name": entry.replication_name,
            "offset": entry.offset,
            "size": entry.size,
            "children": [
                _project_cdi_entry(c, space) for c in entry.children
            ],
        }
    # Leaf.
    base = {
        "kind": "field",
        "name": entry.name,
        "description": entry.description,
        "space": space,
        "offset": entry.offset,
        "size": entry.size,
    }
    if isinstance(entry, CdiInt):
        base["type"] = "int"
        base["default"] = entry.default
        base["min"] = entry.min
        base["max"] = entry.max
        if entry.value_map is not None:
            base["value_map"] = [
                {"value": v, "label": l}
                for v, l in sorted(entry.value_map.values.items())
            ]
        return base
    if isinstance(entry, CdiString):
        base["type"] = "string"
        base["default"] = entry.default
        return base
    if isinstance(entry, CdiEventId):
        base["type"] = "eventid"
        return base
    if isinstance(entry, CdiFloat):
        base["type"] = "float"
        return base
    if isinstance(entry, CdiBit):
        base["type"] = "bit"
        base["bit"] = entry.bit
        return base
    # Unknown leaf class -- should never happen given the parser
    # only produces the five above, but a safe fallback keeps the
    # projection non-lossy.
    base["type"] = "unknown"
    return base


def _cdi_value_from_wire(value: Any, type_name: str):
    """Turn a JSON payload's ``value`` field into the Python value
    :func:`write_field_raw` expects.

    * ``int`` / ``bit`` -- Python int (bit accepts bool too).
    * ``string`` -- Python str.
    * ``eventid`` -- dotted-hex string or int; decoded to
      :class:`EventId`.
    * ``float`` -- Python float.
    """
    if type_name == "int":
        return int(value)
    if type_name == "string":
        return str(value)
    if type_name == "eventid":
        if isinstance(value, str):
            return EventId.parse(value)
        return EventId(int(value))
    if type_name == "float":
        return float(value)
    if type_name == "bit":
        return bool(value)
    raise JsonServiceError(
        400, f"unknown CDI field type {type_name!r}",
    )


def _cdi_value_to_wire(value: Any) -> Any:
    """Prepare a Python value for JSON serialization.

    :class:`EventId` needs the dotted-hex string form; everything
    else round-trips as-is."""
    if isinstance(value, EventId):
        return str(value)
    return value


def _project_fdi(fdi: Fdi) -> dict[str, Any]:
    """Serialize a parsed FDI to a JSON-friendly dict.

    Preserves the tree (segments -> groups -> functions) for UIs that
    want to render group headings, and also emits a top-level
    ``flat`` list for throttles that just need "F# -> name/kind"
    without walking the tree.
    """
    return {
        "segments": [_project_fdi_segment(s) for s in fdi.segments],
        "flat": [_project_fdi_function(fn) for fn in fdi.flatten()],
    }


def _project_fdi_segment(seg: FdiSegment) -> dict[str, Any]:
    return {
        "name": seg.name,
        "description": seg.description,
        "groups": [_project_fdi_group(g) for g in seg.groups],
    }


def _project_fdi_group(grp: FdiGroup) -> dict[str, Any]:
    return {
        "name": grp.name,
        "description": grp.description,
        "functions": [_project_fdi_function(fn) for fn in grp.functions],
        "groups": [_project_fdi_group(g) for g in grp.groups],
    }


def _project_fdi_function(fn: FdiFunction) -> dict[str, Any]:
    return {
        "number": fn.number,
        "name": fn.name,
        "kind": fn.kind,
        "size": fn.size,
        "momentary": fn.momentary,
        "min": fn.min,
        "max": fn.max,
    }


def _project_hosted_loco(loco: HostedLoco) -> dict[str, Any]:
    """Project a :class:`~pmri_openlcb.HostedLoco` for CS listings."""
    return {
        "address": loco.address,
        "long_address": loco.long_address,
        "node_id": loco.node_id.dotted(),
        "alias": f"{loco.alias:#05x}",
        "name": loco.canonical_name,
        "controller": loco.controller.dotted(),
        "has_throttle": loco.throttle is not None,
        "consist_members": [
            {
                "node_id": m.node_id.dotted(),
                "hidden": m.hidden,
                "reverse": m.reverse,
            }
            for m in loco.consist_members
        ],
        "last_activity_at": loco.last_activity_at,
    }


# ---- openlcb service -----------------------------------------------------


class OpenLcbService(JsonService):
    topic = "openlcb"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Every alias with any cached data (NID, produced/consumed
        events, and cached SNIP/PIP if a prior ``probe_all`` filled
        them in). Aliases we've *only* heard control-frame traffic
        from -- no Verified NID, no P/C Identified -- don't appear
        yet; call ``discover`` first to populate them."""
        conn = _require_olcb(self.ctx)
        # Union the sources: NID lookup + roster.
        aliases: set[int] = set()
        aliases.update(conn.node_id_lookup.known())
        aliases.update(conn.event_roster.known_aliases())
        return [
            _project_node(conn, a, ctx=self.ctx) for a in sorted(aliases)
        ]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        conn = _require_olcb(self.ctx)
        alias = _alias_from(data)
        return _project_node(conn, alias, ctx=self.ctx)

    async def on_post(self, data: dict[str, Any] | None) -> Any:
        conn = _require_olcb(self.ctx)
        data = data or {}
        action = data.get("action")
        if action == "discover":
            return await self._discover(conn)
        if action == "probe_snip":
            return await self._probe_snip(conn, data)
        if action == "probe_pip":
            return await self._probe_pip(conn, data)
        if action == "node_health":
            return await self._node_health(conn, data)
        if action == "pcer_history":
            return self._pcer_history(conn, data)
        if action == "identify_events_addressed":
            return await self._identify_events_addressed(conn, data)
        if action == "snip_get":
            return self._snip_get(conn)
        if action == "snip_update":
            return self._snip_update(conn, data)
        if action == "roster_export":
            return await self._roster_export()
        if action == "roster_import":
            return self._roster_import(conn, data)
        if action == "bus_wide_health":
            return await self._bus_wide_health(conn, data)
        if action == "search":
            return await self._search(conn, data)
        if action == "probe_all":
            return await self._probe_all(conn, data)
        if action == "recent_frames":
            return self._recent_frames(conn, data)
        if action == "clear_frames":
            return self._clear_frames(conn)
        if action == "prune_stale":
            return self._prune_stale(conn)
        if action == "prune_all":
            return self._prune_all(conn)
        if action == "bus_health":
            return self._bus_health(conn)
        if action == "pause_monitor":
            return self._pause_monitor(conn)
        if action == "resume_monitor":
            return self._resume_monitor(conn)
        if action == "bus_stats":
            return self._bus_stats(conn, data)
        if action == "send_frame":
            return await self._send_frame(conn, data)
        if action == "send_datagram":
            return await self._send_datagram(conn, data)
        if action == "send_frame_presets":
            return self._send_frame_presets(conn)
        if action == "send_frame_history":
            return self._send_frame_history()
        if action == "well_known_events":
            return self._well_known_events()
        if action == "firmware_upgrade":
            return await self._firmware_upgrade(conn, data)
        if action == "memory_reboot":
            return await self._memory_reboot(conn, data)
        if action == "memory_factory_reset":
            return await self._memory_factory_reset(conn, data)
        if action == "memory_freeze":
            return await self._memory_freeze(conn, data)
        if action == "memory_unfreeze":
            return await self._memory_unfreeze(conn, data)
        if action == "cdi_fetch":
            return await self._cdi_fetch(conn, data)
        if action == "cdi_invalidate":
            return self._cdi_invalidate(conn, data)
        if action == "cdi_read":
            return await self._cdi_read(conn, data)
        if action == "cdi_write":
            return await self._cdi_write(conn, data)
        if action == "fdi_fetch":
            return await self._fdi_fetch(conn, data)
        raise JsonServiceError(400, f"unknown openlcb action: {action!r}")

    async def _discover(self, conn: OlcbConnection) -> dict[str, Any]:
        """Kick off a bus-wide discovery sweep. Fires Verify NID
        Global (draws Verified NID from every node -> populates the
        alias<->NID map) and Identify Events Global (draws Producer/
        Consumer Identified from every node -> populates the event
        roster). Returns immediately -- replies flow in on the
        traffic-controller thread and populate the maps."""
        await conn.node_id_lookup.verify_all()
        await conn.event_roster.discover()
        return {"status": "broadcast", "verify_nid": True, "identify_events": True}

    async def _probe_snip(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        alias = _alias_from(data)
        timeout = float(data.get("timeout", 3.0))
        try:
            info = await conn.snip_client.probe(alias, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"SNIP probe to {alias:#05x} failed: {e}",
            ) from None
        return {
            "alias": f"{alias:#05x}",
            "manufacturer": info.manufacturer,
            "model": info.model,
            "hardware_version": info.hardware_version,
            "software_version": info.software_version,
            "user_name": info.user_name,
            "user_description": info.user_description,
        }

    async def _probe_pip(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        alias = _alias_from(data)
        timeout = float(data.get("timeout", 2.0))
        try:
            flags = await conn.pip_client.probe(alias, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"PIP probe to {alias:#05x} failed: {e}",
            ) from None
        return {"alias": f"{alias:#05x}", "flags": _project_flags(flags)}

    async def _node_health(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Fire Verify NID + PIP + SNIP against *alias* in parallel
        and return each round-trip time in ms. A None time means the
        probe timed out or errored. Aggregate status:

        * ``healthy`` -- all three responded under 500ms.
        * ``slow`` -- everything responded but at least one > 500ms.
        * ``partial`` -- at least one probe responded, at least one
          did not.
        * ``unresponsive`` -- every probe timed out.
        """
        import asyncio, time  # noqa: PLC0415
        alias = _alias_from(data)
        timeout = float(data.get("timeout", 2.0))

        async def _time(coro) -> float | None:  # noqa: ANN001
            start = time.perf_counter()
            try:
                await coro
            except Exception:  # noqa: BLE001
                return None
            return (time.perf_counter() - start) * 1000.0

        verify_ms, pip_ms, snip_ms = await asyncio.gather(
            _time(conn.node_id_lookup.probe(alias, timeout=timeout)),
            _time(conn.pip_client.probe(alias, timeout=timeout)),
            _time(conn.snip_client.probe(alias, timeout=timeout)),
        )
        responded = [t for t in (verify_ms, pip_ms, snip_ms) if t is not None]
        if not responded:
            status = "unresponsive"
        elif len(responded) < 3:
            status = "partial"
        elif max(responded) > 500.0:
            status = "slow"
        else:
            status = "healthy"
        return {
            "alias": f"{alias:#05x}",
            "verify_ms": verify_ms,
            "pip_ms": pip_ms,
            "snip_ms": snip_ms,
            "status": status,
        }

    def _snip_get(self, conn: OlcbConnection) -> dict[str, Any]:
        """Return our currently-advertised SNIP identity strings.
        Callers building the "Our identity" UI use this to seed the
        edit form on page load."""
        return _project_snip_info(conn.node_info)

    def _snip_update(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update the operator-mutable SNIP fields
        (``user_name`` / ``user_description``). Any string type is
        accepted; empty string is valid ("clear the label")."""
        user_name = data.get("user_name")
        user_description = data.get("user_description")
        if user_name is not None and not isinstance(user_name, str):
            raise JsonServiceError(400, "user_name must be a string")
        if user_description is not None and not isinstance(user_description, str):
            raise JsonServiceError(
                400, "user_description must be a string",
            )
        info = conn.update_snip(
            user_name=user_name, user_description=user_description,
        )
        return _project_snip_info(info)

    async def _roster_export(self) -> dict[str, Any]:
        """Full roster dump -- same node projections as ``list`` plus a
        version tag + export timestamp so the import side can spot a
        mismatched schema. Kept compatible with ``list`` output so a
        caller can round-trip through ``list`` if it prefers."""
        from datetime import datetime, timezone  # noqa: PLC0415
        return {
            "version": 1,
            "exported_at": (
                datetime.now(timezone.utc).isoformat(timespec="seconds")
            ),
            "nodes": await self.on_list(None),
        }

    def _roster_import(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Load a previously-exported roster back into the SNIP + PIP
        caches + NodeIdLookup map. Skips our own alias so an import
        can't overwrite the local identity. Returns a summary count."""
        from pmri_openlcb import NodeId  # noqa: PLC0415
        from pmri_openlcb.pip import ProtocolFlag  # noqa: PLC0415
        from pmri_openlcb.snip import NodeInfo  # noqa: PLC0415
        payload = data.get("roster") or {}
        if not isinstance(payload, dict):
            raise JsonServiceError(400, "roster_import requires 'roster' dict")
        nodes = payload.get("nodes") or []
        if not isinstance(nodes, list):
            raise JsonServiceError(
                400, "roster.nodes must be a list of node projections",
            )
        us_alias = conn.traffic.source_alias
        snip_cache = _snip_cache(self.ctx)
        pip_cache = _pip_cache(self.ctx)
        loaded_snip = 0
        loaded_pip = 0
        loaded_nid = 0
        for node in nodes:
            if not isinstance(node, dict):
                continue
            alias_raw = node.get("alias") or ""
            try:
                alias = int(str(alias_raw), 16)
            except (TypeError, ValueError):
                continue
            if alias == us_alias:
                continue
            nid_str = node.get("node_id")
            if isinstance(nid_str, str) and nid_str:
                try:
                    conn.node_id_lookup._by_alias[alias] = (  # noqa: SLF001
                        NodeId.parse(nid_str)
                    )
                    loaded_nid += 1
                except ValueError:
                    pass
            snip = node.get("snip")
            if isinstance(snip, dict):
                try:
                    snip_cache[alias] = NodeInfo(
                        manufacturer=snip.get("manufacturer", ""),
                        model=snip.get("model", ""),
                        hardware_version=snip.get("hardware_version", ""),
                        software_version=snip.get("software_version", ""),
                        user_name=snip.get("user_name", ""),
                        user_description=snip.get("user_description", ""),
                    )
                    loaded_snip += 1
                except TypeError:
                    pass
            pip = node.get("pip")
            if isinstance(pip, dict) and "raw" in pip:
                try:
                    raw = int(str(pip["raw"]), 16)
                    pip_cache[alias] = ProtocolFlag(raw)
                    loaded_pip += 1
                except (TypeError, ValueError):
                    pass
        return {
            "nodes_seen": len(nodes),
            "nid_loaded": loaded_nid,
            "snip_loaded": loaded_snip,
            "pip_loaded": loaded_pip,
        }

    async def _identify_events_addressed(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Send MTI 0x0968 (Identify Events Addressed) targeting
        *alias*. Peer answers with a burst of Producer/Consumer
        Identified frames which the roster picks up naturally.

        Returns immediately -- the replies flow in asynchronously.
        Callers who want to observe them poll the roster or
        node-detail projections a moment later."""
        from pmri_openlcb.addressed import encode_addressed_frames  # noqa: PLC0415
        from pmri_openlcb.event_ident import (  # noqa: PLC0415
            MTI_IDENTIFY_EVENTS_ADDRESSED,
        )
        alias = _alias_from(data)
        for frame in encode_addressed_frames(
            MTI_IDENTIFY_EVENTS_ADDRESSED,
            conn.traffic.source_alias, alias, b"",
        ):
            await conn.traffic.send_frame(frame)
        return {"alias": f"{alias:#05x}", "sent": True}

    def _pcer_history(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return the PCER history for *alias*: one row per event
        with count, last_seen (epoch), seconds_ago, and the S-9.7.3
        well-known name where applicable. Newest-first."""
        from pmri_openlcb import well_known_name  # noqa: PLC0415
        alias = _alias_from(data)
        entries = conn.pcer_history.for_alias(alias)
        now = time.time()
        return [
            {
                "event": str(e.event),
                "name": well_known_name(e.event),
                "count": e.count,
                "last_seen": e.last_seen,
                "seconds_ago": now - e.last_seen,
            }
            for e in entries
        ]

    async def _bus_wide_health(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Run :meth:`_node_health` against every known alias in
        parallel and return the results sorted by alias. Aliases
        come from the NID lookup + event roster (same union as
        ``list``) minus our own -- probing ourselves would be
        nonsensical.

        Per-probe timeout defaults to 1.0s so one silent peer
        doesn't stall the whole sweep; the caller can pass
        ``timeout`` to override. Failures inside a single
        node_health call surface as ``status=unresponsive`` in
        that row instead of blocking the sweep."""
        import asyncio  # noqa: PLC0415

        timeout = float(data.get("timeout", 1.0))
        us = conn.traffic.source_alias
        aliases: set[int] = set()
        aliases.update(conn.node_id_lookup.known())
        aliases.update(conn.event_roster.known_aliases())
        aliases.discard(us)
        if not aliases:
            return []

        async def _probe_one(alias: int) -> dict[str, Any]:
            try:
                return await self._node_health(
                    conn, {"alias": alias, "timeout": timeout},
                )
            except JsonServiceError:
                return {
                    "alias": f"{alias:#05x}",
                    "verify_ms": None,
                    "pip_ms": None,
                    "snip_ms": None,
                    "status": "unresponsive",
                }

        rows = await asyncio.gather(
            *(_probe_one(a) for a in sorted(aliases)),
        )
        return list(rows)

    async def _search(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        address = data.get("address")
        if not isinstance(address, int):
            raise JsonServiceError(
                400, "openlcb.search requires int 'address'",
            )
        long_address_raw = data.get("long_address")
        long_address: bool | None
        if long_address_raw is None:
            long_address = None
        else:
            long_address = bool(long_address_raw)
        timeout = float(data.get("timeout", 1.0))
        results = await conn.traction_search.search(
            address, long_address=long_address, timeout=timeout,
        )
        return [
            {
                "alias": f"{r.alias:#05x}",
                "node_id": (
                    conn.node_id_lookup.get(r.alias).dotted()
                    if conn.node_id_lookup.get(r.alias) is not None
                    else None
                ),
            }
            for r in results
        ]

    async def _probe_all(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Probe SNIP + PIP for every currently-known alias in
        parallel, cache the results on the context, and return the
        enriched roster.

        ``timeout`` bounds how long we wait per probe (default 1.5s).
        Since probes run concurrently, the total wall-clock cost
        is roughly max(timeouts) rather than N x timeout -- a bus
        with 20 offline peers still returns quickly. A peer that
        doesn't answer either probe just gets ``snip: null`` and
        ``pip: null`` in its projection; no error surfaces.
        """
        import asyncio  # noqa: PLC0415
        timeout = float(data.get("timeout", 1.5))
        aliases: set[int] = set()
        aliases.update(conn.node_id_lookup.known())
        aliases.update(conn.event_roster.known_aliases())
        snip_cache = _snip_cache(self.ctx)
        pip_cache = _pip_cache(self.ctx)

        async def _probe_snip(alias: int) -> None:
            try:
                snip_cache[alias] = await conn.snip_client.probe(
                    alias, timeout=timeout,
                )
            except Exception:  # noqa: BLE001
                # Peer offline / didn't answer SNIP / malformed reply.
                # Leave any existing cache entry alone (a peer that
                # answered previously but is now silent still shows
                # its last-known identity).
                pass

        async def _probe_pip(alias: int) -> None:
            try:
                pip_cache[alias] = await conn.pip_client.probe(
                    alias, timeout=timeout,
                )
            except Exception:  # noqa: BLE001
                pass

        tasks: list[asyncio.Task] = []
        for alias in aliases:
            tasks.append(asyncio.create_task(_probe_snip(alias)))
            tasks.append(asyncio.create_task(_probe_pip(alias)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return [
            _project_node(conn, a, ctx=self.ctx) for a in sorted(aliases)
        ]

    def _recent_frames(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return the frame monitor's recent-frame ring buffer.

        Optional ``limit`` bounds the count (from the newest); default
        returns everything the buffer holds. Newest-first ordering so
        a UI can render a live-looking log without extra reversal.

        Optional filters, all applied server-side so the auto-poll
        swap only ships the matching rows:

        * ``source_alias`` -- hex string; exact match.
        * ``frame_type`` -- hex or int; exact match on the 4-bit
          frame-type nibble (0x9 = MTI, 0x0 = control, etc.).
        * ``text`` -- case-insensitive substring on the summary.

        Each filter has a matching ``_invert`` companion flag
        (``source_alias_invert`` / ``frame_type_invert`` /
        ``text_invert``). When truthy, the filter runs as an
        exclusion instead of an inclusion -- so ``source_alias=222,
        source_alias_invert=1`` shows every frame *except* those
        from 0x222. Filters cascade: set multiple inverted to hide
        several noisy things at once.

        Invalid hex silently degrades to "no filter for that field" --
        a partially-typed alias in the UI shouldn't 400 the whole
        request while the operator is still typing.
        """
        try:
            monitor = conn.frame_monitor
        except RuntimeError as e:
            raise JsonServiceError(400, str(e)) from None
        entries = monitor.recent()
        entries.reverse()   # newest first

        alias_filter = _hex_or_none(data.get("source_alias"))
        alias_invert = bool(data.get("source_alias_invert"))
        ftype_filter = _hex_or_none(data.get("frame_type"))
        ftype_invert = bool(data.get("frame_type_invert"))
        text_filter = data.get("text") or ""
        text_filter = text_filter.strip().lower() if isinstance(text_filter, str) else ""
        text_invert = bool(data.get("text_invert"))

        if alias_filter is not None:
            if alias_invert:
                entries = [e for e in entries if e.source_alias != alias_filter]
            else:
                entries = [e for e in entries if e.source_alias == alias_filter]
        if ftype_filter is not None:
            if ftype_invert:
                entries = [e for e in entries if e.frame_type != ftype_filter]
            else:
                entries = [e for e in entries if e.frame_type == ftype_filter]
        if text_filter:
            if text_invert:
                entries = [
                    e for e in entries
                    if text_filter not in e.summary.lower()
                ]
            else:
                entries = [
                    e for e in entries
                    if text_filter in e.summary.lower()
                ]

        limit = data.get("limit")
        if isinstance(limit, int) and limit > 0:
            entries = entries[:limit]
        # Local alias so the UI can flag "us"-authored frames without
        # a second lookup per row.
        us_alias = conn.traffic.source_alias
        from pmri_openlcb import (  # noqa: PLC0415
            OlcbFrame,
            decode_frame_details,
        )
        rows = []
        for e in entries:
            row = {
                "timestamp": e.timestamp,
                "time_str": _format_timestamp(e.timestamp),
                "header": f"{e.header:#010x}",
                "source_alias": f"{e.source_alias:#05x}",
                "frame_type": e.frame_type,
                "summary": e.summary,
                "data_hex": e.data_hex,
                "is_us": e.source_alias == us_alias,
            }
            # Structured decode for the click-to-expand drill-down.
            # Reconstruct the OlcbFrame from the ring entry's raw
            # bytes -- the decoder only needs (header, data).
            try:
                frame = OlcbFrame(
                    header=e.header,
                    data=bytes.fromhex(e.data_hex) if e.data_hex else b"",
                )
                row["details"] = decode_frame_details(frame)
            except Exception:  # noqa: BLE001
                row["details"] = None
            rows.append(row)
        return rows

    def _clear_frames(self, conn: OlcbConnection) -> dict[str, Any]:
        """Clear the frame monitor's ring buffer. Used by a "clear
        log" UI button."""
        try:
            monitor = conn.frame_monitor
        except RuntimeError as e:
            raise JsonServiceError(400, str(e)) from None
        monitor.clear()
        return {"cleared": True}

    def _bus_health(self, conn: OlcbConnection) -> dict[str, Any]:
        """Aggregate roster + cache counts into a one-glance health
        view. All fields derive from data we already collect --
        adding no new state, just a compact projection an operator
        can read at a glance."""
        aliases: set[int] = set()
        aliases.update(conn.node_id_lookup.known())
        aliases.update(conn.event_roster.known_aliases())
        aliases.update(conn.last_seen.known())
        snip_c = _snip_cache(self.ctx)
        pip_c = _pip_cache(self.ctx)
        identified = sum(
            1 for a in aliases if conn.node_id_lookup.get(a) is not None
        )
        with_snip = sum(1 for a in aliases if a in snip_c)
        with_pip = sum(1 for a in aliases if a in pip_c)
        # Quiet = last-seen more than 60s ago. Aliases we've heard
        # of via NID lookup or roster but never had a frame from
        # (last_seen returns None) count as unknown-silence, not
        # quiet -- we don't lump them in.
        now = time.time()
        quiet_threshold = 60.0
        ages = [
            now - ts for ts in conn.last_seen.known().values()
        ]
        quiet_count = sum(1 for age in ages if age > quiet_threshold)
        oldest_silence = max(ages) if ages else None
        return {
            "total_nodes": len(aliases),
            "identified": identified,
            "with_snip": with_snip,
            "with_pip": with_pip,
            "quiet_count": quiet_count,
            "quiet_threshold_seconds": quiet_threshold,
            "oldest_silence_seconds": oldest_silence,
        }

    def _prune_all(self, conn: OlcbConnection) -> dict[str, Any]:
        """Drop every known peer regardless of age. Skips our own
        alias -- that would be nonsensical. Reuses the same release
        fan-out as AMR / stale-sweep so all caches (NID, roster,
        last-seen, SNIP, PIP) clear atomically."""
        aliases: set[int] = set()
        aliases.update(conn.node_id_lookup.known())
        aliases.update(conn.event_roster.known_aliases())
        aliases.update(conn.last_seen.known())
        us = conn.traffic.source_alias
        aliases.discard(us)
        for alias in aliases:
            conn.alias_release.notify_release(alias)
        return {
            "pruned": [f"{a:#05x}" for a in sorted(aliases)],
            "count": len(aliases),
        }

    def _prune_stale(self, conn: OlcbConnection) -> dict[str, Any]:
        """Run one sweep synchronously and return the aliases that
        were pruned. Same behaviour as the periodic background sweep,
        but on-demand -- an operator can force it when they know a
        peer just powered off.

        Errors 400 when the sweeper was disabled at connection open
        (``skip_stale_sweep=True``); the tracker still records
        last-seen times but nothing is wired to prune on demand
        without instantiating a sweeper first."""
        try:
            sweeper = conn.stale_sweeper
        except RuntimeError as e:
            raise JsonServiceError(400, str(e)) from None
        pruned = sweeper.sweep_once()
        return {
            "pruned": [f"{a:#05x}" for a in pruned],
            "count": len(pruned),
        }

    def _pause_monitor(self, conn: OlcbConnection) -> dict[str, Any]:
        """Stop the monitor from appending new frames. Buffered
        entries stay visible so the operator can still analyze what
        was captured up to the pause point."""
        try:
            monitor = conn.frame_monitor
        except RuntimeError as e:
            raise JsonServiceError(400, str(e)) from None
        monitor.pause()
        return {"paused": True}

    def _resume_monitor(self, conn: OlcbConnection) -> dict[str, Any]:
        try:
            monitor = conn.frame_monitor
        except RuntimeError as e:
            raise JsonServiceError(400, str(e)) from None
        monitor.resume()
        return {"paused": False}

    async def _send_frame(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Hand-crafted frame send -- a troubleshooting tool. Accepts
        29-bit ``header`` as a hex string + ``data`` as an even-length
        hex string (empty allowed). Bypasses the higher-level codecs
        so the operator can send anything the wire supports."""
        header_raw = data.get("header")
        if not isinstance(header_raw, str) or not header_raw.strip():
            raise JsonServiceError(400, "send_frame requires 'header' (hex string)")
        try:
            header = int(header_raw.strip(), 16)
        except ValueError:
            raise JsonServiceError(
                400, f"send_frame 'header' must be hex, got {header_raw!r}",
            ) from None
        if not 0 <= header <= 0x1FFFFFFF:
            raise JsonServiceError(
                400, "send_frame 'header' must fit in 29 bits (extended CAN ID)",
            )
        data_hex = str(data.get("data", "")).replace(" ", "").replace(".", "")
        try:
            payload = bytes.fromhex(data_hex) if data_hex else b""
        except ValueError:
            raise JsonServiceError(
                400, f"send_frame 'data' must be hex, got {data_hex!r}",
            ) from None
        if len(payload) > 8:
            raise JsonServiceError(
                400, f"send_frame 'data' must be <= 8 bytes, got {len(payload)}",
            )
        # Build + send. Uses the low-level frame API so we don't
        # accidentally rewrite the source alias field.
        from pmri_openlcb import OlcbFrame  # noqa: PLC0415
        await conn.traffic.send_frame(OlcbFrame(header=header, data=payload))
        # Append to the rolling history for the resend UI. Dedupe
        # consecutive identical sends (a spammed button shouldn't
        # crowd out earlier distinct frames).
        header_hex = f"{header:#010x}"
        data_hex = payload.hex().upper()
        hist = _send_history(self.ctx)
        if not (hist and hist[0]["header"] == header_hex
                and hist[0]["data"] == data_hex):
            hist.insert(0, {
                "header": header_hex,
                "data": data_hex,
                "sent_at": time.time(),
            })
            del hist[SEND_HISTORY_MAX:]
        return {
            "sent": True,
            "header": header_hex,
            "data": data_hex,
        }

    async def _send_datagram(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Hand-crafted datagram send. Accepts a 12-bit dest alias +
        payload as hex (up to 72 bytes per S-9.7.4.1). Uses the
        connection's datagram engine, so a peer's Received-OK is
        awaited and a Rejected reply raises with the friendly
        error-taxonomy description."""
        from pmri_openlcb import (  # noqa: PLC0415
            DatagramEngine,
            DatagramRejected,
        )

        alias = _alias_from(data)
        data_hex = str(data.get("data", "")).replace(" ", "").replace(".", "")
        try:
            payload = bytes.fromhex(data_hex) if data_hex else b""
        except ValueError:
            raise JsonServiceError(
                400, f"send_datagram 'data' must be hex, got {data_hex!r}",
            ) from None
        if len(payload) > 72:
            raise JsonServiceError(
                400,
                f"send_datagram 'data' must be <= 72 bytes "
                f"(S-9.7.4.1 datagram cap), got {len(payload)}",
            )
        timeout = float(data.get("timeout", 3.0))
        # Cache an engine on the connection for reuse -- same pattern
        # as the memory client. A dedicated engine keeps the send
        # bookkeeping isolated from the memory-config client's
        # in-flight state.
        engine = getattr(conn, "_send_datagram_engine", None)
        if engine is None:
            engine = DatagramEngine(conn.traffic)
            await engine.attach()
            conn._send_datagram_engine = engine  # type: ignore[attr-defined]
        try:
            await engine.send(alias, payload, timeout=timeout)
        except DatagramRejected as exc:
            return {
                "sent": False,
                "alias": f"{alias:#05x}",
                "rejected": True,
                "error_code": f"{exc.error_code:#06x}",
                "description": exc.description,
            }
        except asyncio.TimeoutError:
            raise JsonServiceError(
                504,
                f"send_datagram to {alias:#05x} timed out after "
                f"{timeout:.1f}s (no Received OK from peer)",
            ) from None
        return {
            "sent": True,
            "alias": f"{alias:#05x}",
            "bytes": len(payload),
        }

    def _well_known_events(self) -> list[dict[str, Any]]:
        """Return the S-9.7.3 well-known event catalog as
        ``[{event, name}, ...]`` for the send-form dropdown. Sorted
        by event ID so the display order is deterministic."""
        from pmri_openlcb import WELL_KNOWN_EVENTS  # noqa: PLC0415
        return [
            {"event": str(ev), "name": name}
            for ev, name in sorted(
                WELL_KNOWN_EVENTS.items(),
                key=lambda kv: kv[0].value,
            )
        ]

    def _send_frame_history(self) -> list[dict[str, Any]]:
        """Return the rolling send-frame history, newest first.
        Each entry is ``{header, data, sent_at}``. Empty on a fresh
        connection. Bounded to :data:`SEND_HISTORY_MAX` entries."""
        return list(_send_history(self.ctx))

    def _send_frame_presets(
        self, conn: OlcbConnection,
    ) -> list[dict[str, Any]]:
        """Common troubleshooting frames pre-composed with our
        current source alias. Web UI (and any REST client) can
        render one-click buttons that stuff these into the send
        form. Kept server-side so alias / MTI constants stay in
        one place -- the template just consumes strings."""
        from pmri_openlcb.codec import (  # noqa: PLC0415
            CTRL_AME,
            FT_CONTROL,
            _pack_header,
            make_pcer_header,
        )
        alias = conn.traffic.source_alias
        return [
            {
                "name": "PCER (example event)",
                "description": (
                    "Producer/Consumer Event Report. Replace the "
                    "example event ID with the real one you want "
                    "to fire on the bus."
                ),
                "header": f"{make_pcer_header(alias):#010x}",
                "data": "0501010100000042",
            },
            {
                "name": "Verify NID Global",
                "description": (
                    "Ask every node to publish its NodeID. Every "
                    "reachable peer replies with a Verified NID."
                ),
                "header": f"{_pack_header(0x9, 0x0490, alias):#010x}",
                "data": "",
            },
            {
                "name": "Identify Events Global",
                "description": (
                    "Ask every node to publish its produced + "
                    "consumed events. Populates the P/C event roster."
                ),
                "header": f"{_pack_header(0x9, 0x0970, alias):#010x}",
                "data": "",
            },
            {
                "name": "AME (global)",
                "description": (
                    "Alias Map Enquiry -- every node replies with "
                    "AMD (alias<->NID). Useful for building the "
                    "alias<->NID map from scratch after a bus reset."
                ),
                "header": f"{_pack_header(FT_CONTROL, CTRL_AME, alias):#010x}",
                "data": "",
            },
            # Well-known emergency events (S-9.7.3). PCER of these
            # event IDs is what every OpenLCB node listens for to
            # implement bus-wide emergency behaviour.
            {
                "name": "Emergency Off",
                "description": (
                    "Broadcast PCER for the reserved Emergency Off "
                    "event (01.00.00.00.00.00.FF.FE). Every node on "
                    "the bus should kill track power."
                ),
                "header": f"{make_pcer_header(alias):#010x}",
                "data": "010000000000FFFE",
            },
            {
                "name": "Clear Emergency Off",
                "description": (
                    "Broadcast PCER for the Clear Emergency Off "
                    "event (01.00.00.00.00.00.FF.FF). Every node "
                    "should re-enable track power."
                ),
                "header": f"{make_pcer_header(alias):#010x}",
                "data": "010000000000FFFF",
            },
            {
                "name": "Emergency Stop of All Trains",
                "description": (
                    "PCER for 01.00.00.00.00.00.FF.FD -- every loco "
                    "on the bus stops but track power stays up."
                ),
                "header": f"{make_pcer_header(alias):#010x}",
                "data": "010000000000FFFD",
            },
            {
                "name": "Clear Emergency Stop of All Trains",
                "description": (
                    "PCER for 01.00.00.00.00.00.FF.FC -- clears the "
                    "all-trains emergency stop."
                ),
                "header": f"{make_pcer_header(alias):#010x}",
                "data": "010000000000FFFC",
            },
        ]

    def _bus_stats(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Aggregate the monitor ring into headline counts + top-N
        breakdowns. Purely from what's already in the ring -- no
        long-running counters."""
        try:
            monitor = conn.frame_monitor
        except RuntimeError as e:
            raise JsonServiceError(400, str(e)) from None
        from pmri_openlcb import compute_bus_stats  # noqa: PLC0415
        top_n = int(data.get("top_n", 10))
        stats = compute_bus_stats(monitor.recent(), top_n=top_n)
        return {
            "total_frames": stats.total_frames,
            "span_seconds": stats.span_seconds,
            "oldest_timestamp": stats.oldest_timestamp,
            "newest_timestamp": stats.newest_timestamp,
            "paused": monitor.paused,
            "our_alias": f"{conn.traffic.source_alias:#05x}",
            "top_talkers": [
                {"alias": f"{alias:#05x}", "count": count}
                for alias, count in stats.top_talkers
            ],
            "top_categories": [
                {"category": category, "count": count}
                for category, count in stats.top_categories
            ],
            "top_events": [
                _project_top_event(event, count)
                for event, count in stats.top_events
            ],
        }

    # ---- CDI ----------------------------------------------------------

    async def _cdi_fetch(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Download + parse the target node's CDI, return the tree
        as JSON. Requires the datagram engine to be attached -- we
        set it up on demand so a plain OlcbConnection doesn't have
        to pay the datagram overhead until a CDI operation asks
        for it.

        Caches the parsed CDI per (connection, alias) so a page
        reload doesn't re-download the XML over the wire. The
        cache is invalidated on peer reboot, factory reset, AMR,
        and stale-peer sweep. Callers can force a fresh fetch
        with ``refresh=True`` when they know the CDI has changed
        out-of-band (e.g. after a firmware upgrade)."""
        alias = _alias_from(data)
        refresh = bool(data.get("refresh", False))
        cache = _cdi_cache(self.ctx, conn)
        if not refresh and alias in cache:
            return _project_cdi(cache[alias])
        max_bytes = int(data.get("max_bytes", 256 * 1024))
        client = await self._memory_client(conn)
        try:
            cdi = await fetch_cdi(client, alias, max_bytes=max_bytes)
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"CDI fetch from {alias:#05x} failed: {e}",
            ) from None
        cache[alias] = cdi
        return _project_cdi(cdi)

    def _cdi_invalidate(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Drop the cached CDI for *alias* (or every alias if not
        given). Useful after a firmware upgrade or when the operator
        suspects the CDI drifted from what's cached."""
        cache = _cdi_cache(self.ctx, conn)
        if data.get("alias") is None:
            n = len(cache)
            cache.clear()
            return {"cleared": n, "alias": None}
        alias = _alias_from(data)
        was_cached = alias in cache
        cache.pop(alias, None)
        return {"cleared": 1 if was_cached else 0, "alias": f"{alias:#05x}"}

    async def _cdi_read(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        alias = _alias_from(data)
        space = int(data.get("space", 0xFE))
        offset = data.get("offset")
        size = data.get("size")
        type_name = data.get("type")
        if not isinstance(offset, int) or not isinstance(size, int):
            raise JsonServiceError(
                400, "cdi_read requires int 'offset' + 'size'",
            )
        if type_name not in FIELD_TYPES:
            raise JsonServiceError(
                400, f"cdi_read 'type' must be one of {FIELD_TYPES}",
            )
        bit = int(data.get("bit", 0))
        client = await self._memory_client(conn)
        try:
            value = await read_field_raw(
                client, alias,
                space=space, offset=offset, size=size,
                type_name=type_name, bit=bit,
            )
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"CDI read from {alias:#05x} failed: {e}",
            ) from None
        return {
            "alias": f"{alias:#05x}",
            "space": space,
            "offset": offset,
            "size": size,
            "type": type_name,
            "value": _cdi_value_to_wire(value),
        }

    async def _cdi_write(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        alias = _alias_from(data)
        space = int(data.get("space", 0xFE))
        offset = data.get("offset")
        size = data.get("size")
        type_name = data.get("type")
        raw_value = data.get("value")
        if not isinstance(offset, int) or not isinstance(size, int):
            raise JsonServiceError(
                400, "cdi_write requires int 'offset' + 'size'",
            )
        if type_name not in FIELD_TYPES:
            raise JsonServiceError(
                400, f"cdi_write 'type' must be one of {FIELD_TYPES}",
            )
        if raw_value is None:
            raise JsonServiceError(400, "cdi_write requires 'value'")
        bit = int(data.get("bit", 0))
        try:
            value = _cdi_value_from_wire(raw_value, type_name)
        except (TypeError, ValueError) as e:
            raise JsonServiceError(
                400, f"cdi_write value decode failed: {e}",
            ) from None
        client = await self._memory_client(conn)
        try:
            await write_field_raw(
                client, alias,
                space=space, offset=offset, size=size,
                type_name=type_name, value=value, bit=bit,
            )
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"CDI write to {alias:#05x} failed: {e}",
            ) from None
        return {
            "alias": f"{alias:#05x}",
            "space": space,
            "offset": offset,
            "size": size,
            "type": type_name,
            "written": True,
        }

    async def _fdi_fetch(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Download + parse the target node's FDI from memory space
        0xFA. Returns the tree + a flat list. Peers that don't
        publish FDI (short read -> empty XML) surface as 504."""
        alias = _alias_from(data)
        max_bytes = int(data.get("max_bytes", 64 * 1024))
        client = await self._memory_client(conn)
        try:
            fdi = await fetch_fdi(client, alias, max_bytes=max_bytes)
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"FDI fetch from {alias:#05x} failed: {e}",
            ) from None
        return _project_fdi(fdi)

    async def _firmware_upgrade(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Push a firmware upgrade binary to the target using the
        S-9.7.4.3 flow. Requires ``firmware_hex`` -- the raw firmware
        bytes as a hex string (the web layer converts multipart upload
        bytes to hex before dispatch since JSON payloads don't carry
        binary natively).

        Runs synchronously -- blocks the request until the transfer
        finishes or fails. For beta5 this is fine; a follow-up can
        move to a job-queue + SSE for progress on big transfers."""
        from pmri_openlcb import (  # noqa: PLC0415
            FirmwareUpgradeClient,
            FirmwareUpgradeError,
            StreamClient,
        )

        alias = _alias_from(data)
        hex_str = data.get("firmware_hex")
        if not isinstance(hex_str, str) or not hex_str:
            raise JsonServiceError(
                400, "firmware_upgrade requires 'firmware_hex' -- "
                "the firmware binary as hex string",
            )
        try:
            firmware = bytes.fromhex(hex_str)
        except ValueError as e:
            raise JsonServiceError(
                400, f"firmware_hex is not valid hex: {e}",
            ) from None
        if not firmware:
            raise JsonServiceError(400, "firmware_hex is empty")

        memory = await self._memory_client(conn)
        stream = StreamClient(conn.traffic)
        try:
            fw = FirmwareUpgradeClient(
                memory, conn.pip_client, conn.traffic, stream=stream,
            )
            try:
                await fw.upgrade(alias, firmware)
            except FirmwareUpgradeError as e:
                raise JsonServiceError(504, str(e)) from None
        finally:
            stream.close()
        return {
            "alias": f"{alias:#05x}",
            "bytes_written": len(firmware),
        }

    async def _memory_reboot(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Send Memory Config Reboot (0xA9) to the target. Peer
        typically restarts immediately -- bank on it dropping off
        the bus and re-claiming an alias.

        Invalidates the cached CDI for that alias: a firmware
        update or config-driven CDI regeneration would otherwise
        leave stale content in the cache."""
        alias = _alias_from(data)
        client = await self._memory_client(conn)
        try:
            await client.reboot(alias, timeout=float(data.get("timeout", 3.0)))
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"reboot to {alias:#05x} failed: {e}",
            ) from None
        _cdi_cache(self.ctx, conn).pop(alias, None)
        return {"alias": f"{alias:#05x}", "action": "reboot"}

    async def _memory_factory_reset(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        """Send Memory Config Factory Reset (0xAA). Payload carries
        the target's 6-byte NodeID per S-9.10.1 sec.3.3 -- the caller
        must pass ``target_node_id`` (dotted or int form). The node
        rejects the request if the payload doesn't match its own
        NodeID, so passing the wrong NID is a safe no-op on the
        peer side (still a wasted round-trip though)."""
        from pmri_openlcb import NodeId  # noqa: PLC0415
        alias = _alias_from(data)
        raw_nid = data.get("target_node_id")
        if raw_nid is None:
            raise JsonServiceError(
                400,
                "memory_factory_reset requires 'target_node_id' -- "
                "the target's own NodeID as a safety payload",
            )
        try:
            if isinstance(raw_nid, str):
                nid = NodeId.parse(raw_nid)
            else:
                nid = NodeId(int(raw_nid))
        except (TypeError, ValueError) as e:
            raise JsonServiceError(
                400, f"invalid target_node_id {raw_nid!r}: {e}",
            ) from None
        client = await self._memory_client(conn)
        try:
            await client.factory_reset(
                alias, nid, timeout=float(data.get("timeout", 3.0)),
            )
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"factory reset to {alias:#05x} failed: {e}",
            ) from None
        # A factory reset clears the peer's config storage; the CDI
        # description usually doesn't change, but a defensive drop
        # of the cache means the next fetch reflects whatever the
        # peer comes back with.
        _cdi_cache(self.ctx, conn).pop(alias, None)
        return {"alias": f"{alias:#05x}", "action": "factory_reset"}

    async def _memory_freeze(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        alias = _alias_from(data)
        space = int(data.get("space", 0))
        client = await self._memory_client(conn)
        try:
            await client.freeze(
                alias, space, timeout=float(data.get("timeout", 3.0)),
            )
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"freeze on {alias:#05x} space {space:#x} failed: {e}",
            ) from None
        return {"alias": f"{alias:#05x}", "space": space, "action": "freeze"}

    async def _memory_unfreeze(
        self, conn: OlcbConnection, data: dict[str, Any],
    ) -> dict[str, Any]:
        alias = _alias_from(data)
        space = int(data.get("space", 0))
        client = await self._memory_client(conn)
        try:
            await client.unfreeze(
                alias, space, timeout=float(data.get("timeout", 3.0)),
            )
        except Exception as e:  # noqa: BLE001
            raise JsonServiceError(
                504, f"unfreeze on {alias:#05x} space {space:#x} failed: {e}",
            ) from None
        return {"alias": f"{alias:#05x}", "space": space, "action": "unfreeze"}

    async def _memory_client(self, conn: OlcbConnection) -> MemoryClient:
        """Lazily attach a memory client to the connection.

        The datagram engine + memory client aren't spun up by
        :meth:`OlcbConnection.open` today (there's no fixed use case
        that needs them up front), so a first CDI action wires them
        onto the connection and reuses the same instance for later
        calls. Cached on the connection via ``_cdi_memory_client``
        so parallel operations share the pending-datagram state."""
        cached = getattr(conn, "_cdi_memory_client", None)
        if cached is not None:
            return cached
        engine = DatagramEngine(conn.traffic)
        await engine.attach()
        client = MemoryClient(engine)
        conn._cdi_memory_client = client  # type: ignore[attr-defined]
        conn._cdi_datagram_engine = engine  # type: ignore[attr-defined]
        return client


# ---- openlcbCS service ---------------------------------------------------


class OpenLcbCSService(JsonService):
    """Command Station node view + hosted-loco management.

    Read (``list``) works whenever a CS is attached; write actions
    (``post {"action": "add"|"remove"}``) require both an attached
    CS and -- from the operator side -- that they already set up the
    DCC-side connection separately (multi-connection binding
    happens at the ``ctx._openlcb_cs`` construction step, so once
    the CS exists here we can safely add/remove hosted DCC
    addresses to/from it).
    """

    topic = "openlcbCS"

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        cs = self._cs()
        if cs is None:
            return []
        return [_project_hosted_loco(loco) for loco in cs.hosted()]

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        cs = self._cs()
        if cs is None:
            raise JsonServiceError(
                400,
                "no OpenLCB command station attached "
                "(set ctx._openlcb_cs via a startup script first)",
            )
        data = data or {}
        action = data.get("action")
        if action == "add":
            return await self._add(cs, data)
        if action == "remove":
            return await self._remove(cs, data)
        raise JsonServiceError(400, f"unknown openlcbCS action: {action!r}")

    async def _add(
        self, cs: CommandStationNode, data: dict[str, Any],
    ) -> dict[str, Any]:
        address, long_address = self._parse_address(data)
        loco = await cs.add_loco(address, long_address=long_address)
        return _project_hosted_loco(loco)

    async def _remove(
        self, cs: CommandStationNode, data: dict[str, Any],
    ) -> dict[str, Any]:
        address, long_address = self._parse_address(data)
        await cs.remove_loco(address, long_address=long_address)
        return {
            "address": address,
            "long_address": long_address,
            "removed": True,
        }

    def _parse_address(
        self, data: dict[str, Any],
    ) -> tuple[int, bool]:
        address = data.get("address")
        if not isinstance(address, int):
            raise JsonServiceError(
                400, "openlcbCS action requires int 'address'",
            )
        long_address = bool(data.get("long_address", False))
        if long_address:
            if not 0 <= address <= 0x3FFF:
                raise JsonServiceError(
                    400, f"long DCC address out of range (0..16383): {address}",
                )
        elif not 0 <= address <= 127:
            raise JsonServiceError(
                400, f"short DCC address out of range (0..127): {address}",
            )
        return address, long_address

    def _cs(self) -> CommandStationNode | None:
        """Return the wired CS if one exists on the context. The
        attribute name ``_openlcb_cs`` is a convention -- a script or
        startup hook does ``ctx._openlcb_cs = CommandStationNode(...)``
        to make the CS visible to the JSON layer."""
        _require_olcb(self.ctx)
        return getattr(self.ctx, "_openlcb_cs", None)
