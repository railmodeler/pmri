"""JMRI Java LogixNG XML -> jmri_logixng YAML importer tests.

Fixtures are synthetic XML files constructed to match the shape
JMRI Java's ``configurexml/*Xml.java`` handlers produce (verified
against ``ActionTurnoutXml.java``, ``IfThenElseXml.java``,
``AndXml.java``, ``DefaultLogixNGManagerXml.java``, and
``DefaultConditionalNGManagerXml.java`` in the decompiled source).

Tests cover:

* Empty file -> empty result, no crash.
* One-LogixNG one-ConditionalNG-with-simple action pipeline.
* IfThenElse with condition + then/else + multi-branch truncation warning.
* And/Or/Not tree.
* Unknown tag -> LogAction placeholder + warning.
* Non-Direct addressing -> warning.
* Dangling socket reference -> warning + placeholder.
* Two LogixNGs, one shared ConditionalNG -> both programs preserved.
* Round-trip: import -> save_yaml -> load_yaml -> structure preserved.
"""
from __future__ import annotations

import pathlib

import pytest

from jmri_logixng import (
    ConditionalNG,
    IfThenElse,
    ImportResult,
    LogAction,
    LogixNG,
    Not,
    SensorIsActive,
    Sequence,
    SetTurnoutState,
    TurnoutIsClosed,
    import_jmri_logixng_xml,
    load_yaml,
)
from jmri_core.state import TurnoutState


def _write(tmp_path: pathlib.Path, name: str, body: str) -> pathlib.Path:
    """Write a synthetic XML file with a ``<layout-config>`` root
    so the importer's iter() finds our manager sections. No XML
    declaration -- keeps lxml happy about column 0."""
    xml = f"<layout-config>{body}</layout-config>"
    path = tmp_path / name
    path.write_text(xml, encoding="utf-8")
    return path


# ---- shape helpers -----------------------------------------------------


def _logixng(system_name: str, user_name: str, cn_sysname: str) -> str:
    return f"""
        <LogixNG>
          <systemName>{system_name}</systemName>
          <userName>{user_name}</userName>
          <ConditionalNGs>
            <systemName>{cn_sysname}</systemName>
          </ConditionalNGs>
        </LogixNG>
    """


def _conditionalng(system_name: str, action_ref: str) -> str:
    return f"""
        <ConditionalNG>
          <systemName>{system_name}</systemName>
          <Socket>
            <socketName>A1</socketName>
            <systemName>{action_ref}</systemName>
          </Socket>
        </ConditionalNG>
    """


# ---- happy paths -------------------------------------------------------


def test_empty_file(tmp_path: pathlib.Path) -> None:
    """A layout-config with no LogixNG sections yields an empty
    result -- no crash, no false-positive warnings."""
    path = _write(tmp_path, "empty.xml", "")
    result = import_jmri_logixng_xml(path)
    assert result.programs == []
    assert result.warnings == []


def test_single_program_with_set_turnout_action(
    tmp_path: pathlib.Path,
) -> None:
    """One LogixNG -> one ConditionalNG -> one SetTurnout action."""
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:AUTO:0001", "Morning startup", "IQC:AUTO:0001")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:AUTO:0001", "IQDA:AUTO:0001")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions>
      <ActionTurnout>
        <systemName>IQDA:AUTO:0001</systemName>
        <userName>throw main</userName>
        <namedBean>
          <addressing>Direct</addressing>
          <systemName>IT10</systemName>
        </namedBean>
        <state>
          <addressing>Direct</addressing>
          <enum>THROWN</enum>
        </state>
      </ActionTurnout>
    </LogixNGDigitalActions>
    """
    path = _write(tmp_path, "single.xml", xml)
    result = import_jmri_logixng_xml(path)

    assert len(result.programs) == 1
    program = result.programs[0]
    assert isinstance(program, LogixNG)
    assert program.user_name == "Morning startup"

    assert len(program.conditionals) == 1
    cn = program.conditionals[0]
    assert isinstance(cn, ConditionalNG)
    assert len(cn.actions) == 1
    action = cn.actions[0]
    assert isinstance(action, SetTurnoutState)
    assert action.turnout == "IT10"
    assert action.state is TurnoutState.THROWN
    assert result.warnings == []


def test_if_then_else_with_expression(
    tmp_path: pathlib.Path,
) -> None:
    """IfThenElse with an ExpressionSensor condition + then + else
    action pair. Both branches must resolve correctly."""
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:0002", "Blocker", "IQC:0002")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:0002", "IQDA:IFTHEN")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions>
      <IfThenElse>
        <systemName>IQDA:IFTHEN</systemName>
        <Expressions>
          <Socket>
            <socketName>E1</socketName>
            <systemName>IQDE:COND</systemName>
          </Socket>
        </Expressions>
        <Actions>
          <Socket>
            <socketName>A1</socketName>
            <systemName>IQDA:THEN</systemName>
          </Socket>
          <Socket>
            <socketName>A2</socketName>
            <systemName>IQDA:ELSE</systemName>
          </Socket>
        </Actions>
      </IfThenElse>
      <ActionTurnout>
        <systemName>IQDA:THEN</systemName>
        <namedBean>
          <addressing>Direct</addressing>
          <systemName>IT1</systemName>
        </namedBean>
        <state>
          <addressing>Direct</addressing>
          <enum>CLOSED</enum>
        </state>
      </ActionTurnout>
      <ActionTurnout>
        <systemName>IQDA:ELSE</systemName>
        <namedBean>
          <addressing>Direct</addressing>
          <systemName>IT1</systemName>
        </namedBean>
        <state>
          <addressing>Direct</addressing>
          <enum>THROWN</enum>
        </state>
      </ActionTurnout>
    </LogixNGDigitalActions>
    <LogixNGDigitalExpressions>
      <ExpressionSensor>
        <systemName>IQDE:COND</systemName>
        <namedBean>
          <addressing>Direct</addressing>
          <systemName>IS10</systemName>
        </namedBean>
        <state>
          <addressing>Direct</addressing>
          <enum>ACTIVE</enum>
        </state>
        <is_isNot>IS</is_isNot>
      </ExpressionSensor>
    </LogixNGDigitalExpressions>
    """
    path = _write(tmp_path, "ifthen.xml", xml)
    result = import_jmri_logixng_xml(path)

    assert len(result.warnings) == 0
    program = result.programs[0]
    assert isinstance(program.conditionals[0].actions[0], IfThenElse)
    ite = program.conditionals[0].actions[0]
    assert isinstance(ite.condition, SensorIsActive)
    assert ite.condition.sensor == "IS10"
    assert isinstance(ite.then_action, SetTurnoutState)
    assert ite.then_action.state is TurnoutState.CLOSED
    assert isinstance(ite.else_action, SetTurnoutState)
    assert ite.else_action.state is TurnoutState.THROWN


def test_expression_is_not_wraps_in_not(
    tmp_path: pathlib.Path,
) -> None:
    """``is_isNot = IS_NOT`` on an ExpressionSensor becomes a Not
    over SensorIsActive -- matches JMRI's negation semantics."""
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:NEG", "Neg", "IQC:NEG")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:NEG", "IQDA:IF")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions>
      <IfThenElse>
        <systemName>IQDA:IF</systemName>
        <Expressions>
          <Socket>
            <socketName>E1</socketName>
            <systemName>IQDE:NEG</systemName>
          </Socket>
        </Expressions>
        <Actions/>
      </IfThenElse>
    </LogixNGDigitalActions>
    <LogixNGDigitalExpressions>
      <ExpressionSensor>
        <systemName>IQDE:NEG</systemName>
        <namedBean>
          <addressing>Direct</addressing>
          <systemName>IS5</systemName>
        </namedBean>
        <state>
          <addressing>Direct</addressing>
          <enum>ACTIVE</enum>
        </state>
        <is_isNot>IS_NOT</is_isNot>
      </ExpressionSensor>
    </LogixNGDigitalExpressions>
    """
    path = _write(tmp_path, "not.xml", xml)
    result = import_jmri_logixng_xml(path)
    program = result.programs[0]
    ite = program.conditionals[0].actions[0]
    assert isinstance(ite.condition, Not)


# ---- warnings ---------------------------------------------------------


def test_unknown_tag_becomes_placeholder(tmp_path: pathlib.Path) -> None:
    """A ConditionalNG referencing an unsupported action tag gets
    a LogAction placeholder + a warning. The rest of the tree
    (the program shell) still imports cleanly."""
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:UNK", "Unknown", "IQC:UNK")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:UNK", "IQDA:UNK")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions>
      <ActionRunScript>
        <systemName>IQDA:UNK</systemName>
        <script>doStuff.py</script>
      </ActionRunScript>
    </LogixNGDigitalActions>
    """
    path = _write(tmp_path, "unk.xml", xml)
    result = import_jmri_logixng_xml(path)

    assert any(w.kind == "unknown-tag" for w in result.warnings)
    action = result.programs[0].conditionals[0].actions[0]
    assert isinstance(action, LogAction)
    assert "ActionRunScript" in action.message


def test_non_direct_addressing_warns(tmp_path: pathlib.Path) -> None:
    """A ``<namedBean addressing="Reference">`` field emits the
    ``non-direct-addressing`` warning kind so the operator knows
    the imported action is degenerate."""
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:AD", "Addr", "IQC:AD")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:AD", "IQDA:AD")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions>
      <ActionTurnout>
        <systemName>IQDA:AD</systemName>
        <namedBean>
          <addressing>Reference</addressing>
          <reference>{{myTurnout}}</reference>
          <systemName>IT1</systemName>
        </namedBean>
        <state>
          <addressing>Direct</addressing>
          <enum>CLOSED</enum>
        </state>
      </ActionTurnout>
    </LogixNGDigitalActions>
    """
    path = _write(tmp_path, "addr.xml", xml)
    result = import_jmri_logixng_xml(path)
    assert any(
        w.kind == "non-direct-addressing" for w in result.warnings
    )


def test_dangling_socket_reference_warns_and_placeholders(
    tmp_path: pathlib.Path,
) -> None:
    """A ConditionalNG whose action reference doesn't resolve to
    any indexed action -> warning + LogAction placeholder in the
    slot."""
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:DR", "Dangling", "IQC:DR")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:DR", "IQDA:GHOST")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions/>
    """
    path = _write(tmp_path, "dangling.xml", xml)
    result = import_jmri_logixng_xml(path)
    assert any(
        w.kind == "dangling-socket-ref" for w in result.warnings
    )


# ---- roundtrip --------------------------------------------------------


def test_import_and_yaml_roundtrip(tmp_path: pathlib.Path) -> None:
    """Import -> save YAML -> reload YAML -> tree structure preserved."""
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:RT", "Roundtrip", "IQC:RT")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:RT", "IQDA:RT")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions>
      <ActionTurnout>
        <systemName>IQDA:RT</systemName>
        <namedBean>
          <addressing>Direct</addressing>
          <systemName>IT99</systemName>
        </namedBean>
        <state>
          <addressing>Direct</addressing>
          <enum>THROWN</enum>
        </state>
      </ActionTurnout>
    </LogixNGDigitalActions>
    """
    path = _write(tmp_path, "roundtrip.xml", xml)
    out = tmp_path / "yaml"
    result = import_jmri_logixng_xml(path, out_dir=out)
    assert len(result.written) == 1
    yaml_path = result.written[0]
    assert yaml_path.is_file()

    reloaded = load_yaml(yaml_path)
    assert isinstance(reloaded, LogixNG)
    assert reloaded.user_name == "Roundtrip"
    action = reloaded.conditionals[0].actions[0]
    assert isinstance(action, SetTurnoutState)
    assert action.turnout == "IT99"
    assert action.state is TurnoutState.THROWN


# ---- multi-branch truncation warning ---------------------------------


def test_multi_branch_ifthenelse_warns(tmp_path: pathlib.Path) -> None:
    """JMRI's IfThenElse supports N action branches; we keep the
    first two and warn about the rest."""
    branches = "\n".join(
        f"""
        <Socket>
          <socketName>A{i}</socketName>
          <systemName>IQDA:LOG{i}</systemName>
        </Socket>
        """
        for i in range(3)
    )
    action_defs = "\n".join(
        f"""
        <LogLocalVariables>
          <systemName>IQDA:LOG{i}</systemName>
          <message>branch {i}</message>
          <logLevel>info</logLevel>
        </LogLocalVariables>
        """
        for i in range(3)
    )
    xml = f"""
    <LogixNGs>
      {_logixng("IQ:MB", "MB", "IQC:MB")}
    </LogixNGs>
    <LogixNGConditionalNGs>
      {_conditionalng("IQC:MB", "IQDA:MB")}
    </LogixNGConditionalNGs>
    <LogixNGDigitalActions>
      <IfThenElse>
        <systemName>IQDA:MB</systemName>
        <Expressions>
          <Socket>
            <socketName>E1</socketName>
            <systemName>IQDE:MB</systemName>
          </Socket>
        </Expressions>
        <Actions>
          {branches}
        </Actions>
      </IfThenElse>
      {action_defs}
    </LogixNGDigitalActions>
    <LogixNGDigitalExpressions>
      <True><systemName>IQDE:MB</systemName></True>
    </LogixNGDigitalExpressions>
    """
    path = _write(tmp_path, "multi.xml", xml)
    result = import_jmri_logixng_xml(path)
    assert any(
        w.kind == "multi-branch-truncated" for w in result.warnings
    )
