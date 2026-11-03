"""``PanelService`` -- JSON topic ``panel``.

Lists JMRI panel XML files under the active profile's ``panels/`` dir.
Wire shape per ``jmri/server/json/util/panel-server.json``:

* ``name`` -- reference name (``<Type>/<basename>``, matching JMRI's URL
  convention).
* ``userName`` -- display name (from the XML file's ``userName``
  attribute if present, else the basename).
* ``URL`` -- server-relative path to fetch the panel XML.
* ``type`` -- one of ``"Panel" | "Control Panel" | "Layout" |
  "Switchboard"``, guessed from the root XML element.

This is a **listing-only** service in this pass -- no serving of the
panel XML content, no editing. Real panel rendering lives with Panel
Editor (Tier 1 #3). What we ship here is enough for mobile clients
that probe the ``panel`` topic on connect to see valid entries and
stop 404-ing on the topic itself.
"""
from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET
from typing import Any

from pmri_server.service import JsonService, JsonServiceError


# Map JMRI panel-XML root elements to the schema's ``type`` enum.
_TYPE_BY_ROOT = {
    "LayoutEditor": "Layout",
    "ControlPanelEditor": "Control Panel",
    "PanelEditor": "Panel",
    "SwitchboardEditor": "Switchboard",
}


def _panels_dir(ctx: Any) -> pathlib.Path | None:
    """Locate the active profile's panels dir, if any.

    Falls back through:
    * ``ctx._panels_dir`` (test hook -- lets integration tests point at
      a temp directory);
    * ``ctx.config_store.profile.panels_dir`` (production path once
      the ConfigStore's profile is attached);
    * ``Profile.default().panels_dir`` (last-resort default).

    Returns ``None`` if none of those resolve to an existing directory
    -- callers treat that as "no panels" rather than an error.
    """
    override = getattr(ctx, "_panels_dir", None)
    if isinstance(override, (str, pathlib.Path)):
        p = pathlib.Path(override)
        if p.is_dir():
            return p

    store = getattr(ctx, "config_store", None)
    profile = getattr(store, "profile", None) if store is not None else None
    if profile is not None:
        panels = getattr(profile, "panels_dir", None)
        if isinstance(panels, pathlib.Path) and panels.is_dir():
            return panels

    try:
        from pmri_core.persistence.profile import Profile  # noqa: PLC0415
    except ImportError:
        return None
    default = getattr(Profile.default(), "panels_dir", None)
    if isinstance(default, pathlib.Path) and default.is_dir():
        return default
    return None


def _guess_panel_type(path: pathlib.Path) -> str | None:
    """Peek at the root element of a panel XML file. Returns one of the
    four schema types, or ``None`` if the root isn't a recognized
    panel-editor root."""
    try:
        # ``iterparse`` doesn't need the whole file -- we bail after the
        # first "start" event.
        for _event, elem in ET.iterparse(path, events=("start",)):
            return _TYPE_BY_ROOT.get(elem.tag)
    except (ET.ParseError, OSError):
        return None
    return None


def _project(path: pathlib.Path) -> dict[str, Any] | None:
    """Build the wire form for one panel XML file, or ``None`` if the
    file isn't a recognized panel."""
    ptype = _guess_panel_type(path)
    if ptype is None:
        return None
    # ``name`` matches the URL: ``<Type>/<basename>`` -- JMRI uses this
    # both as an id and as the fetch path.
    stem = path.stem
    short_type = ptype.replace(" ", "")
    reference = f"{short_type}/{stem}"
    return {
        "name": reference,
        "userName": stem,
        "URL": f"/panel/{reference}?format=xml",
        "type": ptype,
    }


class PanelService(JsonService):
    """List panels on disk under the active profile.

    Supports ``list`` and ``get`` (by ``name``). No ``put`` / ``post``
    / ``delete`` -- panel authoring goes through Panel Editor, not this
    service.
    """

    topic = "panel"

    async def on_list(
        self, data: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return list(self._all_panels())

    async def on_get(self, data: dict[str, Any] | None) -> dict[str, Any]:
        data = data or {}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise JsonServiceError(400, "panel.get requires 'name'")
        for panel in self._all_panels():
            if panel["name"] == name:
                return panel
        raise JsonServiceError(404, f"no panel named {name!r}")

    def _all_panels(self) -> list[dict[str, Any]]:
        panels_dir = _panels_dir(self.ctx)
        if panels_dir is None:
            return []
        out: list[dict[str, Any]] = []
        for xml_path in sorted(panels_dir.glob("*.xml")):
            projection = _project(xml_path)
            if projection is not None:
                out.append(projection)
        return out
