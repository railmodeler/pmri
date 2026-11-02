"""Programmer XML tests -- parse <programmer-config> + resolve xi:includes."""
from __future__ import annotations

import pathlib

import pytest

from jmri_core.persistence import (
    ProgrammerConfig,
    iter_programmer_configs,
    load_programmer_config,
    load_programmer_config_bytes,
)


def _make_programmer_tree(root: pathlib.Path) -> pathlib.Path:
    """Build a miniature JMRI ``xml/`` tree with programmers + pane parts.

    Returns the ``xml/`` root so tests can point ``xml_root`` at it.
    """
    xml_root = root / "xml"
    parts = xml_root / "programmers" / "parts"
    parts.mkdir(parents=True)
    (parts / "BasicPane.xml").write_bytes(
        b'<?xml version="1.0"?>'
        b'<pane><name>Basic</name>'
        b'<column><display item="Short Address"/></column>'
        b'</pane>'
    )
    (parts / "MotorPane.xml").write_bytes(
        b'<?xml version="1.0"?>'
        b'<pane><name>Motor</name>'
        b'<column><display item="Vstart"/><display item="Vmid"/></column>'
        b'</pane>'
    )
    # A programmer file that includes both.
    (xml_root / "programmers" / "Basic.xml").write_bytes(
        b'<?xml version="1.0"?>'
        b'<programmer-config xmlns:xi="http://www.w3.org/2001/XInclude">'
        b'<programmer decoderFilePanes="yes" showEmptyPanes="default">'
        b'<xi:include href="http://jmri.org/xml/programmers/parts/BasicPane.xml"/>'
        b'<xi:include href="http://jmri.org/xml/programmers/parts/MotorPane.xml"/>'
        b'</programmer>'
        b'</programmer-config>'
    )
    return xml_root


# ----- parsing ----------------------------------------------------------


def test_load_programmer_config_resolves_includes(tmp_path: pathlib.Path) -> None:
    """Includes with the ``http://jmri.org/xml/`` prefix are mapped to
    files under *xml_root* and loaded via the step-1 pane loader."""
    xml_root = _make_programmer_tree(tmp_path)
    cfg = load_programmer_config(
        xml_root / "programmers" / "Basic.xml",
        xml_root=xml_root,
    )
    assert cfg.name == "Basic"
    assert [p.name for p in cfg.panes] == ["Basic", "Motor"]
    assert cfg.unresolved_includes == []
    assert cfg.panes[0].items == ["Short Address"]
    assert cfg.panes[1].items == ["Vstart", "Vmid"]


def test_load_programmer_config_default_xml_root_uses_grandparent(
    tmp_path: pathlib.Path,
) -> None:
    """No explicit ``xml_root`` -> derive from the programmer file's
    grandparent (JMRI standard layout: ``<xml>/programmers/foo.xml``)."""
    xml_root = _make_programmer_tree(tmp_path)
    cfg = load_programmer_config(xml_root / "programmers" / "Basic.xml")
    assert [p.name for p in cfg.panes] == ["Basic", "Motor"]


def test_load_programmer_config_records_unresolved_includes(
    tmp_path: pathlib.Path,
) -> None:
    """A missing include shouldn't abort the whole load -- the successful
    panes come through, and the failure is recorded for diagnostics."""
    xml_root = _make_programmer_tree(tmp_path)
    # Add a programmer that references a non-existent fragment.
    (xml_root / "programmers" / "Broken.xml").write_bytes(
        b'<?xml version="1.0"?>'
        b'<programmer-config xmlns:xi="http://www.w3.org/2001/XInclude">'
        b'<programmer>'
        b'<xi:include href="http://jmri.org/xml/programmers/parts/BasicPane.xml"/>'
        b'<xi:include href="http://jmri.org/xml/programmers/parts/NoSuchPane.xml"/>'
        b'</programmer>'
        b'</programmer-config>'
    )
    cfg = load_programmer_config(
        xml_root / "programmers" / "Broken.xml", xml_root=xml_root,
    )
    assert [p.name for p in cfg.panes] == ["Basic"]
    assert cfg.unresolved_includes == [
        "http://jmri.org/xml/programmers/parts/NoSuchPane.xml",
    ]


def test_load_programmer_config_captures_showEmptyPanes_and_decoderFilePanes(
    tmp_path: pathlib.Path,
) -> None:
    """The programmer element carries two flags that drive tab visibility;
    both round-trip into the config."""
    xml_root = _make_programmer_tree(tmp_path)
    (xml_root / "programmers" / "Custom.xml").write_bytes(
        b'<?xml version="1.0"?>'
        b'<programmer-config><programmer decoderFilePanes="no" showEmptyPanes="yes"/>'
        b'</programmer-config>'
    )
    cfg = load_programmer_config(
        xml_root / "programmers" / "Custom.xml", xml_root=xml_root,
    )
    assert cfg.decoder_file_panes is False
    assert cfg.show_empty_panes == "yes"


def test_load_programmer_config_rejects_wrong_root() -> None:
    with pytest.raises(ValueError, match="programmer-config"):
        load_programmer_config_bytes(
            b'<?xml version="1.0"?><wrong/>', xml_root=pathlib.Path("/"),
        )


def test_load_programmer_config_skips_non_jmri_include_prefix(
    tmp_path: pathlib.Path,
) -> None:
    """We only resolve the ``http://jmri.org/xml/`` prefix; other hrefs
    (relative paths, foreign URLs) are recorded as unresolved so a
    future fixer can look at them."""
    xml_root = _make_programmer_tree(tmp_path)
    (xml_root / "programmers" / "Foreign.xml").write_bytes(
        b'<?xml version="1.0"?>'
        b'<programmer-config xmlns:xi="http://www.w3.org/2001/XInclude">'
        b'<programmer>'
        b'<xi:include href="parts/BasicPane.xml"/>'
        b'<xi:include href="http://example.com/parts/OtherPane.xml"/>'
        b'</programmer>'
        b'</programmer-config>'
    )
    cfg = load_programmer_config(
        xml_root / "programmers" / "Foreign.xml", xml_root=xml_root,
    )
    assert cfg.panes == []
    assert set(cfg.unresolved_includes) == {
        "parts/BasicPane.xml",
        "http://example.com/parts/OtherPane.xml",
    }


# ----- directory iteration ----------------------------------------------


def test_iter_programmer_configs_skips_parts_subdir(tmp_path: pathlib.Path) -> None:
    """The ``parts/`` subdir holds pane fragments, not programmer
    definitions. Iterator should skip it and only yield real
    ``<programmer-config>`` files at the top level."""
    xml_root = _make_programmer_tree(tmp_path)
    configs = list(iter_programmer_configs(xml_root / "programmers"))
    assert len(configs) == 1
    assert configs[0].name == "Basic"


def test_iter_programmer_configs_over_empty_dir(tmp_path: pathlib.Path) -> None:
    (tmp_path / "programmers").mkdir()
    assert list(iter_programmer_configs(tmp_path / "programmers")) == []
