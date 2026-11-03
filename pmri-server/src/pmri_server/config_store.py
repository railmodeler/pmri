"""``ConfigStore`` -- server-owned ``Configuration.xml`` bean persistence.

Holds the parsed bean tables (turnouts, sensors, lights) from the
``layout-config`` Configuration.xml file in the active profile. Other
sections (signal heads, memories, routes, blocks, panels, LogixNG)
are preserved verbatim so users can keep their existing JMRI configs
without losing untouched data when our web UI saves changes.

Storage policy:

* One Configuration.xml at ``Profile.config_file``.
* On startup: read the file once, populate in-memory state.
* On every bean mutation: rewrite the whole file.
* Atomic write (write to ``.tmp``, rename) so a crash mid-write can
  never lose data.
* In-memory state is the source of truth during a server session;
  the XML file is the source of truth at startup.
"""
from __future__ import annotations

import logging
import os
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from pmri_core.persistence import (
    ConfigFile,
    LightRow,
    LightSection,
    SensorRow,
    SensorSection,
    TurnoutRow,
    TurnoutSection,
    empty_config,
    light_mgr_class_for_prefix,
    load_config,
    save_config_bytes,
    sensor_mgr_class_for_prefix,
    turnout_mgr_class_for_prefix,
)
from pmri_core.persistence.profile import Profile

_log = logging.getLogger(__name__)

# Rows are grouped by system-name prefix at flush time so each JMRI-style
# ``<turnouts class="...">`` block carries a *concrete* driver-specific
# manager class (e.g. ``NceTurnoutManagerXml``). The abstract class names
# (``AbstractTurnoutManagerConfigXML`` etc.) that used to live here are
# schema-invalid -- they end in uppercase ``L`` and JMRI Java refuses to
# parse them at startup.


def _system_prefix(system_name: str) -> str:
    """The JMRI system-prefix character -- first letter of the system name."""
    return system_name[:1].upper() if system_name else "I"


def _group_by_prefix[R](rows: Iterable[R]) -> list[tuple[str, list[R]]]:
    """Group *rows* by their system-name prefix.

    Returns ``[(prefix, sorted_rows), ...]`` with stable ordering
    (prefixes alphabetical, rows sorted by system_name within each
    group). One returned tuple maps to one ``<turnouts>`` / ``<sensors>``
    / ``<lights>`` block in the emitted XML -- JMRI writes one block per
    connection prefix, and matching that layout keeps files loadable in
    JMRI Java.
    """
    buckets: dict[str, list[R]] = {}
    for row in rows:
        buckets.setdefault(_system_prefix(row.system_name), []).append(row)  # type: ignore[attr-defined]
    return [
        (prefix, sorted(buckets[prefix], key=lambda r: r.system_name))  # type: ignore[attr-defined]
        for prefix in sorted(buckets)
    ]


@dataclass
class ConfigStore:
    """In-memory + on-disk Configuration.xml."""

    profile: Profile
    _turnouts: dict[str, TurnoutRow] = field(default_factory=dict)
    _sensors: dict[str, SensorRow] = field(default_factory=dict)
    _lights: dict[str, LightRow] = field(default_factory=dict)
    _preserved_doctype: str | None = None
    _preserved_root_attribs: dict[str, str] = field(default_factory=dict)
    _preserved_elements: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.profile.ensure()
        self._load_existing()

    def _load_existing(self) -> None:
        """Load the on-disk Configuration.xml into memory, if it exists."""
        path = self.profile.config_file
        if not path.exists():
            cfg = empty_config()
            self._preserved_doctype = cfg.doctype
            return
        try:
            cfg = load_config(path)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "ConfigStore: failed to parse %s (%s); starting empty", path, e
            )
            self._preserved_doctype = empty_config().doctype
            return
        for row in cfg.all_turnouts():
            self._turnouts[row.system_name] = row
        for row in cfg.all_sensors():
            self._sensors[row.system_name] = row
        for row in cfg.all_lights():
            self._lights[row.system_name] = row
        self._preserved_doctype = cfg.doctype
        self._preserved_root_attribs = dict(cfg.root_attribs)
        self._preserved_elements = list(cfg.preserved)

    # ----- read --------------------------------------------------------

    def turnouts(self) -> Iterable[TurnoutRow]:
        return self._turnouts.values()

    def sensors(self) -> Iterable[SensorRow]:
        return self._sensors.values()

    def lights(self) -> Iterable[LightRow]:
        return self._lights.values()

    # ----- write -------------------------------------------------------

    def upsert_turnout(self, row: TurnoutRow) -> None:
        # Preserve unmodeled extras (locked / lockMode / decoder etc.)
        # from any existing row -- the bean services don't carry these
        # fields and shouldn't accidentally erase them on edit.
        prev = self._turnouts.get(row.system_name)
        if prev and not row.extra_attribs and prev.extra_attribs:
            row.extra_attribs = dict(prev.extra_attribs)
        if prev and row.comment is None and prev.comment:
            row.comment = prev.comment
        self._turnouts[row.system_name] = row
        self._flush()

    def upsert_sensor(self, row: SensorRow) -> None:
        prev = self._sensors.get(row.system_name)
        if prev and not row.extra_attribs and prev.extra_attribs:
            row.extra_attribs = dict(prev.extra_attribs)
        if prev and row.comment is None and prev.comment:
            row.comment = prev.comment
        self._sensors[row.system_name] = row
        self._flush()

    def upsert_light(self, row: LightRow) -> None:
        prev = self._lights.get(row.system_name)
        if prev and not row.extra_attribs and prev.extra_attribs:
            row.extra_attribs = dict(prev.extra_attribs)
        if prev and row.comment is None and prev.comment:
            row.comment = prev.comment
        self._lights[row.system_name] = row
        self._flush()

    def remove_turnout(self, system_name: str) -> bool:
        existed = self._turnouts.pop(system_name, None) is not None
        if existed:
            self._flush()
        return existed

    def remove_sensor(self, system_name: str) -> bool:
        existed = self._sensors.pop(system_name, None) is not None
        if existed:
            self._flush()
        return existed

    def remove_light(self, system_name: str) -> bool:
        existed = self._lights.pop(system_name, None) is not None
        if existed:
            self._flush()
        return existed

    # ----- I/O ---------------------------------------------------------

    def _build_config(self) -> ConfigFile:
        return ConfigFile(
            root_attribs=dict(self._preserved_root_attribs),
            doctype=self._preserved_doctype,
            turnout_sections=self._build_turnout_sections(),
            sensor_sections=self._build_sensor_sections(),
            light_sections=self._build_light_sections(),
            preserved=list(self._preserved_elements),
        )

    def _build_turnout_sections(self) -> list[TurnoutSection]:
        return [
            TurnoutSection(
                manager_class=turnout_mgr_class_for_prefix(prefix),
                rows=rows,
            )
            for prefix, rows in _group_by_prefix(self._turnouts.values())
        ]

    def _build_sensor_sections(self) -> list[SensorSection]:
        return [
            SensorSection(
                manager_class=sensor_mgr_class_for_prefix(prefix),
                rows=rows,
            )
            for prefix, rows in _group_by_prefix(self._sensors.values())
        ]

    def _build_light_sections(self) -> list[LightSection]:
        return [
            LightSection(
                manager_class=light_mgr_class_for_prefix(prefix),
                rows=rows,
            )
            for prefix, rows in _group_by_prefix(self._lights.values())
        ]

    def _flush(self) -> None:
        """Atomically rewrite the file. Failures are logged, not raised."""
        path = self.profile.config_file
        try:
            data = save_config_bytes(self._build_config())
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as e:
            _log.warning("ConfigStore: failed to write %s: %s", path, e)

    # ----- introspection ----------------------------------------------

    def __len__(self) -> int:
        return len(self._turnouts) + len(self._sensors) + len(self._lights)

    def path(self) -> pathlib.Path:
        return self.profile.config_file
