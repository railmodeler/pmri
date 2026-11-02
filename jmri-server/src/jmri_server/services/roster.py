"""Roster service -- full CRUD over the server-owned :class:`RosterStore`.

Wire shape per ``contracts/json-schema/roster/rosterEntry-server.json``:
each entry is a dict with the per-locomotive fields (id, dccAddress,
roadName, decoderFamily, decoderModel, ...).

Methods:

* ``method=list`` -> list of roster entries.
* ``method=get``, ``data={"id": ...}`` -> one entry.
* ``method=put``, ``data={...fields...}`` -> create-or-replace.
* ``method=post``, ``data={...fields...}`` -> update.
* ``method=delete``, ``data={"id": ...}`` -> remove.
"""
from __future__ import annotations

from typing import Any

from jmri_core.beans.roster_entry import RosterEntry

from jmri_server.roster_media import media_url_for, resolve_media_path
from jmri_server.service import JsonService, JsonServiceError


def _entry_to_json(
    e: RosterEntry, *, profile: Any | None = None,
) -> dict[str, Any]:
    """Project a RosterEntry to the JMRI-style camelCase wire shape.

    ``profile`` (optional) enables the ``imageUrl`` / ``iconUrl``
    extension: when the entry's ``image_file_path`` /
    ``icon_file_path`` resolves to a real file under the profile,
    the corresponding URL is included. Legacy callers that pass no
    profile get the same shape they always did -- the extension is
    opt-in per call to avoid touching the filesystem for every list
    render on hosts without a profile."""
    out = {
        "id": e.id,
        "fileName": e.file_name,
        "dccAddress": e.dcc_address,
        "longAddress": e.long_address,
        "protocol": e.protocol,
        "decoderFamily": e.decoder_family,
        "decoderModel": e.decoder_model,
        "roadName": e.road_name,
        "roadNumber": e.road_number,
        "mfg": e.mfg,
        "owner": e.owner,
        "model": e.model,
        "comment": e.comment,
        "maxSpeedPct": e.max_speed_pct,
        "imageFilePath": e.image_file_path,
        "iconFilePath": e.icon_file_path,
        "url": e.url,
        "attributes": e.attributes,
    }
    if profile is not None:
        if resolve_media_path(profile, e.image_file_path) is not None:
            out["imageUrl"] = media_url_for(e, "image")
        if resolve_media_path(profile, e.icon_file_path) is not None:
            out["iconUrl"] = media_url_for(e, "icon")
    return out


def _json_to_entry(data: dict[str, Any], *, base: RosterEntry | None = None) -> RosterEntry:
    """Build (or update) a RosterEntry from the JSON shape."""
    fields = {
        "id": data.get("id") or (base.id if base else ""),
        "file_name": data.get("fileName") or (base.file_name if base else ""),
        "dcc_address": data.get("dccAddress") or (base.dcc_address if base else "3"),
        "long_address": data.get("longAddress", base.long_address if base else False),
        "protocol": data.get("protocol") or (base.protocol if base else "dcc_short"),
        "decoder_family": data.get("decoderFamily") or (base.decoder_family if base else ""),
        "decoder_model": data.get("decoderModel") or (base.decoder_model if base else ""),
        "road_name": data.get("roadName") or (base.road_name if base else ""),
        "road_number": data.get("roadNumber") or (base.road_number if base else ""),
        "mfg": data.get("mfg") or (base.mfg if base else ""),
        "owner": data.get("owner") or (base.owner if base else ""),
        "model": data.get("model") or (base.model if base else ""),
        "comment": data.get("comment") or (base.comment if base else ""),
        "max_speed_pct": int(data.get("maxSpeedPct", base.max_speed_pct if base else 100)),
        "image_file_path": data.get("imageFilePath") or (base.image_file_path if base else None),
        "icon_file_path": data.get("iconFilePath") or (base.icon_file_path if base else None),
        "url": data.get("url") or (base.url if base else None),
        "attributes": data.get("attributes") or (base.attributes if base else {}),
    }
    return RosterEntry(**fields)


def _lazy_roster_store(ctx: Any) -> Any | None:
    """Return the shared :class:`RosterStore` for *ctx*, constructing
    it on first access. Returns ``None`` when no profile can be
    resolved (bare-context tests) -- callers should treat that as
    "no roster available"."""
    store = getattr(ctx, "_roster_store", None)
    if store is not None:
        return store
    try:
        from jmri_core.persistence.profile import Profile  # noqa: PLC0415

        from jmri_server.roster_store import RosterStore  # noqa: PLC0415
    except ImportError:
        return None
    profile = getattr(ctx, "_roster_profile", None) or Profile.default()
    try:
        store = RosterStore(profile=profile)
    except Exception:  # noqa: BLE001
        return None
    ctx._roster_store = store
    # If the WiThrottle server registered a signal-attach hook, let it
    # wire its rebroadcast listeners onto this fresh store.
    attach = getattr(ctx, "_attach_roster_signals", None)
    if callable(attach):
        try:
            attach()
        except Exception:  # noqa: BLE001
            pass
    return store


def _roster_service_get_sync(ctx: Any, loco_id: str) -> dict[str, Any] | None:
    """Sync helper for callers outside the async service dispatch path.

    Used by :func:`jmri_server.services.programmer._loco_cv_store` to
    resolve a loco's decoder family/model without spinning an event
    loop -- the RosterStore's read path is sync-safe.

    Returns the JSON-shaped entry dict or ``None`` if the store isn't
    initialised / the loco isn't in it.
    """
    store = _lazy_roster_store(ctx)
    if store is None:
        return None
    entry = store.get(loco_id)
    if entry is None:
        return None
    return _entry_to_json(entry)


class RosterService(JsonService):
    """Full-CRUD roster service over the :class:`RosterStore` on the context."""

    topic = "roster"

    @property
    def _store(self) -> Any:
        """Return the active RosterStore. Lazily constructed on first
        use via :func:`_lazy_roster_store`. Raises if no profile can
        be resolved -- the service can't function without one, unlike
        the roster-media routes which just 404."""
        store = _lazy_roster_store(self.ctx)
        if store is None:
            raise RuntimeError("no roster store on the context")
        return store

    async def on_list(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        profile = self._store.profile
        return [_entry_to_json(e, profile=profile) for e in self._store]

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        entry_id = data.get("id")
        if not isinstance(entry_id, str):
            raise JsonServiceError(400, "roster.get requires 'id'")
        entry = self._store.get(entry_id)
        if entry is None:
            raise JsonServiceError(404, f"no roster entry with id {entry_id!r}")
        return _entry_to_json(entry, profile=self._store.profile)

    async def on_put(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        entry_id = data.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise JsonServiceError(400, "roster.put requires a non-empty 'id'")
        existing = self._store.get(entry_id)
        entry = _json_to_entry(data, base=existing)
        return _entry_to_json(self._store.put(entry), profile=self._store.profile)

    async def on_post(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        entry_id = data.get("id")
        if not isinstance(entry_id, str):
            raise JsonServiceError(400, "roster.post requires 'id'")
        existing = self._store.get(entry_id)
        if existing is None:
            raise JsonServiceError(404, f"no roster entry with id {entry_id!r}")
        entry = _json_to_entry(data, base=existing)
        return _entry_to_json(self._store.put(entry), profile=self._store.profile)

    async def on_delete(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        entry_id = data.get("id")
        if not isinstance(entry_id, str):
            raise JsonServiceError(400, "roster.delete requires 'id'")
        if not self._store.delete(entry_id):
            raise JsonServiceError(404, f"no roster entry with id {entry_id!r}")
        return {"id": entry_id}
