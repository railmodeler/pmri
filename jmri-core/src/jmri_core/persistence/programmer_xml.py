"""Programmer definition XML -- parses JMRI's ``<programmer-config>`` files.

Each file under ``xml/programmers/*.xml`` declares which panes make up
one flavour of the DecoderPro programmer UI (Comprehensive.xml =
everything, Basic.xml = just Basic, ESU.xml = ESU-specific, ...). The
panes themselves live as reusable fragments in
``xml/programmers/parts/*.xml`` and are pulled in via ``xi:include``:

    <programmer-config>
      <programmer decoderFilePanes="yes" showEmptyPanes="default" ...>
        <xi:include href="http://jmri.org/xml/programmers/parts/BasicPane.xml"/>
        <xi:include href="http://jmri.org/xml/programmers/parts/MotorPane.xml"/>
        ...
      </programmer>
    </programmer-config>

This loader resolves those ``xi:include`` references by stripping the
``http://jmri.org/xml/`` URL prefix and looking them up under a local
``xml_root`` directory (typically ``<JMRI install>/xml``). Each
fragment is parsed via :func:`load_pane_bytes` so the resulting
:class:`DecoderPane` objects share the same shape as decoder-declared
panes from step 1.
"""
from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterator

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from jmri_core.persistence.decoder_xml import (
    XINCLUDE_NS,
    DecoderPane,
    load_pane_bytes,
)

_log = logging.getLogger(__name__)

# URL prefix JMRI uses in xi:include hrefs. Everything after this maps
# to a path under the local ``xml/`` root.
_JMRI_URL_PREFIX = "http://jmri.org/xml/"


class ProgrammerConfig(BaseModel):
    """One loaded programmer definition (e.g. Comprehensive.xml).

    * ``panes`` -- the standard programmer panes in include order (Basic,
      Motor, Basic Speed Control, ...). One entry per successful
      ``xi:include`` resolution.
    * ``unresolved_includes`` -- hrefs that couldn't be resolved to a
      local file, preserved for diagnostics. An unresolved include just
      means that pane doesn't appear in the tab list; the rest of the
      programmer still works.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = ""
    panes: list[DecoderPane] = Field(default_factory=list)
    unresolved_includes: list[str] = Field(default_factory=list)
    source_file: pathlib.Path | None = None
    # ``<programmer decoderFilePanes="yes" showEmptyPanes="default">``
    # attributes. ``decoderFilePanes="yes"`` means: also render any
    # decoder-declared panes in addition to the standard ones.
    # ``showEmptyPanes="default"`` = respect per-model showEmptyPanes;
    # ``"yes"`` = always show; ``"no"`` = always hide.
    decoder_file_panes: bool = True
    show_empty_panes: str = "default"


def load_programmer_config_bytes(
    data: bytes,
    *,
    xml_root: pathlib.Path,
    source_file: pathlib.Path | None = None,
) -> ProgrammerConfig:
    """Parse a ``<programmer-config>`` byte string, resolving xi:includes.

    *xml_root* is the directory ``http://jmri.org/xml/`` maps to
    (typically ``<JMRI install>/xml``). Includes whose href lies outside
    that prefix, or whose target file is missing, are recorded in
    :attr:`ProgrammerConfig.unresolved_includes` and skipped.
    """
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(data, parser=parser)
    if root.tag != "programmer-config":
        raise ValueError(f"expected <programmer-config>, got <{root.tag}>")

    programmer = root.find("programmer")
    panes: list[DecoderPane] = []
    unresolved: list[str] = []
    decoder_file_panes = True
    show_empty_panes = "default"

    if programmer is not None:
        decoder_file_panes = (
            programmer.get("decoderFilePanes", "yes").lower() == "yes"
        )
        show_empty_panes = programmer.get("showEmptyPanes", "default")
        for inc in programmer.iter(f"{XINCLUDE_NS}include"):
            href = inc.get("href", "")
            if not href.startswith(_JMRI_URL_PREFIX):
                unresolved.append(href)
                continue
            rel = href[len(_JMRI_URL_PREFIX):]
            fragment = xml_root / rel
            if not fragment.is_file():
                unresolved.append(href)
                _log.debug("programmer include not found: %s", fragment)
                continue
            try:
                pane = load_pane_bytes(fragment.read_bytes())
            except (etree.XMLSyntaxError, ValueError, OSError) as e:
                unresolved.append(href)
                _log.warning(
                    "programmer include %s failed to parse: %s", fragment, e,
                )
                continue
            panes.append(pane)

    name = source_file.stem if source_file is not None else ""
    return ProgrammerConfig(
        name=name,
        panes=panes,
        unresolved_includes=unresolved,
        source_file=source_file,
        decoder_file_panes=decoder_file_panes,
        show_empty_panes=show_empty_panes,
    )


def load_programmer_config(
    path: str | pathlib.Path,
    *,
    xml_root: pathlib.Path | None = None,
) -> ProgrammerConfig:
    """Load a ``<programmer-config>`` file from disk.

    *xml_root* defaults to the file's grandparent (JMRI's standard
    layout: ``<xml_root>/programmers/foo.xml``).
    """
    p = pathlib.Path(path)
    if xml_root is None:
        xml_root = p.parent.parent
    return load_programmer_config_bytes(
        p.read_bytes(), xml_root=xml_root, source_file=p,
    )


def iter_programmer_configs(
    dir_path: str | pathlib.Path,
    *,
    xml_root: pathlib.Path | None = None,
) -> Iterator[ProgrammerConfig]:
    """Walk a ``programmers/`` directory yielding one
    :class:`ProgrammerConfig` per top-level ``*.xml``. Sub-directory
    ``parts/`` is skipped -- those are the include targets, not
    programmer definitions.
    """
    root = pathlib.Path(dir_path)
    if xml_root is None:
        xml_root = root.parent   # <jmri>/xml
    for xml in sorted(root.glob("*.xml")):
        try:
            yield load_programmer_config(xml, xml_root=xml_root)
        except etree.XMLSyntaxError as e:
            _log.debug("iter_programmer_configs: XML parse failure %s: %s", xml, e)
            continue
        except ValueError as e:
            _log.debug("iter_programmer_configs: skipping %s (%s)", xml, e)
            continue
        except OSError as e:
            _log.warning("iter_programmer_configs: cannot read %s: %s", xml, e)
            continue
