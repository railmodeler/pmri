"""``SignalSystemLoader`` -- parse JMRI's ``xml/signals/`` corpus.

JMRI ships ~50 signal systems under ``<install>/xml/signals/<name>/``
-- each is a directory containing an ``aspects.xml`` (the aspect
table for the system) plus one or more ``appearance-*.xml`` files
(per-mast-type appearance tables). This loader handles the first
half: it reads every ``aspects.xml`` and returns a
:class:`SignalSystem` per directory.

Appearance files, images, and the ``aspectMappings`` transition
graphs are deferred with Panel Editor (Tier 1 #3). What we ship
here is enough to:

* enumerate the systems the operator has available so mobile
  clients / a picker UI can present a valid choice;
* return the valid aspect names for a given system so a
  :class:`~jmri_core.beans.signal_mast.SignalMast` can validate
  ``set_aspect`` calls;
* render the aspect table in a read-only web view.

XML shape (checked against JMRI's ``xml/schema/aspecttable.xsd`` and
sampled files from ``basic``, ``AAR-1946``, ``B&O-1957``)::

    <aspecttable>
      <name>...</name>
      <reference>...</reference>
      <aspects>
        <aspect>
          <name>Clear</name>
          <indication>Proceed</indication>
          <speed>Normal</speed>
          <speed2>Normal</speed2>          <!-- optional -->
          <route>Diverging</route>         <!-- optional -->
        </aspect>
        ...
      </aspects>
    </aspecttable>
"""
from __future__ import annotations

import logging
import pathlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SignalAspect:
    """One aspect within a signal system.

    ``route`` is JMRI's flag distinguishing diverging aspects from
    straight-route ones. Real XML uses the value ``Diverging`` when
    set; other values are preserved as-is.
    """

    name: str
    indication: str = ""
    speed: str = ""
    speed2: str = ""
    route: str = ""


@dataclass(frozen=True, slots=True)
class SignalSystem:
    """One signal system loaded from ``aspects.xml``.

    ``id`` is the directory name (e.g. ``"basic"`` or ``"AAR-1946"``);
    ``name`` and ``reference`` come from the XML. ``aspects`` is
    presented in file order -- JMRI callers depend on the order
    matching what a signaller expects (Clear first, Stop somewhere
    down the list, etc.).
    """

    id: str
    name: str = ""
    reference: str = ""
    aspects: tuple[SignalAspect, ...] = field(default_factory=tuple)


class SignalSystemLoader:
    """Scans a signal-XML root once and caches the result.

    Typical use::

        loader = SignalSystemLoader(pathlib.Path("/opt/JMRI/xml/signals"))
        systems = loader.systems()
        basic = loader.get("basic")

    ``root`` may be ``None`` (unset env var, missing install) -- in
    that case :meth:`systems` returns an empty tuple and :meth:`get`
    returns ``None`` without raising. That's the state the
    ``signalSystem`` service reflects as 503 to clients.
    """

    def __init__(self, root: pathlib.Path | None) -> None:
        self._root = root
        self._cache: dict[str, SignalSystem] | None = None

    @property
    def root(self) -> pathlib.Path | None:
        return self._root

    def systems(self) -> tuple[SignalSystem, ...]:
        return tuple(self._loaded().values())

    def get(self, system_id: str) -> SignalSystem | None:
        return self._loaded().get(system_id)

    def reload(self) -> None:
        """Drop the cache so the next access re-reads the XML.

        Useful for operators editing signal XML in place; not called
        automatically because the cache should be stable for
        performance reasons.
        """
        self._cache = None

    # ---- internals ------------------------------------------------------

    def _loaded(self) -> dict[str, SignalSystem]:
        cache = self._cache
        if cache is not None:
            return cache
        cache = {}
        root = self._root
        if root is not None and root.is_dir():
            for system_dir in sorted(
                p for p in root.iterdir() if p.is_dir()
            ):
                system = _parse_system(system_dir)
                if system is not None:
                    cache[system.id] = system
        self._cache = cache
        return cache


# ---- XML parsing --------------------------------------------------------


def _parse_system(system_dir: pathlib.Path) -> SignalSystem | None:
    """Read ``<system_dir>/aspects.xml``. Missing / malformed -> None
    with a warning logged. Namespaced elements are ignored (docbook
    ``copyright`` etc.); we only care about the aspect table itself.
    """
    aspects_xml = system_dir / "aspects.xml"
    if not aspects_xml.is_file():
        return None
    try:
        tree = ET.parse(aspects_xml)
    except (ET.ParseError, OSError) as e:
        _log.warning("signal system %s: %s", aspects_xml, e)
        return None
    root = tree.getroot()
    if _localname(root.tag) != "aspecttable":
        _log.warning(
            "signal system %s: root is <%s>, expected <aspecttable>",
            aspects_xml, root.tag,
        )
        return None

    name = _first_text(root, "name")
    reference = _first_text(root, "reference")
    aspects = tuple(_parse_aspects(root))
    return SignalSystem(
        id=system_dir.name,
        name=name,
        reference=reference,
        aspects=aspects,
    )


def _parse_aspects(root: ET.Element) -> list[SignalAspect]:
    """Extract each ``<aspect>`` element from the ``<aspects>`` container.

    Missing sub-elements default to empty strings -- JMRI itself is
    lax about them (some legacy files omit ``speed2``, some pre-2011
    files omit ``route``).
    """
    out: list[SignalAspect] = []
    aspects_el = _find_child(root, "aspects")
    if aspects_el is None:
        return out
    for aspect_el in aspects_el:
        if _localname(aspect_el.tag) != "aspect":
            continue
        aspect_name = _first_text(aspect_el, "name")
        if not aspect_name:
            # An aspect without a name is meaningless; skip rather
            # than emit a nameless entry that the UI can't render.
            continue
        out.append(SignalAspect(
            name=aspect_name,
            indication=_first_text(aspect_el, "indication"),
            speed=_first_text(aspect_el, "speed"),
            speed2=_first_text(aspect_el, "speed2"),
            route=_first_text(aspect_el, "route"),
        ))
    return out


def _localname(tag: str) -> str:
    """Strip an XML namespace prefix. ``{http://ns}name`` -> ``name``."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _find_child(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent:
        if _localname(child.tag) == name:
            return child
    return None


def _first_text(parent: ET.Element, name: str) -> str:
    child = _find_child(parent, name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()
