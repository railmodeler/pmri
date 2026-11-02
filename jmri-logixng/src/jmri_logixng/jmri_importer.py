"""JMRI Java LogixNG XML -> jmri_logixng YAML importer.

JMRI Java's LogixNG persistence layout: the XML file (typically a
PanelPro.xml with a ``<LogixNGs>`` section) is a **flat forest** of
manager-scoped elements cross-referenced by system name:

* ``<LogixNGs>``            -- every LogixNG program, with its
                              conditional NGs referenced by system name.
* ``<LogixNGConditionalNGs>`` -- every ConditionalNG's root socket
                              references the head action by system name.
* ``<LogixNGDigitalActions>`` / ``<LogixNGDigitalExpressions>`` --
                              every action/expression node, with its
                              children (also by system name).

Reifying our nested tree means:

1. **Index** every node in the four manager sections by system name.
2. **Walk** from each ``<LogixNG>`` root -> its ConditionalNGs -> each
   conditional's root action -> recursively resolve child sockets to
   nodes in the index.
3. **Dispatch** each XML tag (``ActionTurnout``, ``ExpressionSensor``,
   ``And``, ...) to a handler that produces the matching jmri_logixng
   :class:`Node`.
4. **Serialize** each converted LogixNG to
   ``<out_dir>/<system_name_slug>.yaml`` using our normal
   :func:`~jmri_logixng.serialize.save_yaml`.

Unknown tags are surfaced as :class:`~jmri_logixng.actions.digital.LogAction`
placeholders whose ``message`` names the missing tag; the import
returns a list of :class:`ImportWarning` records so the caller can
report gaps to the operator. This keeps the "import a whole panel"
workflow going instead of aborting on the first unsupported node.

**What's supported today** (matches our current node set -- Stage 7
gaps are still open):

Actions
    ``ActionTurnout``, ``ActionSensor``, ``ActionLight``,
    ``ActionMemory``, ``ActionLog`` / ``LogLocalVariables``,
    ``IfThenElse``, ``DigitalMany`` (-> Sequence),
    ``DigitalActionTimer`` (-> Delay, one-branch simple form).

Expressions
    ``ExpressionTurnout``, ``ExpressionSensor``, ``ExpressionLight``,
    ``ExpressionMemory``, ``True`` / ``False``, ``And``, ``Or``,
    ``Not``.

Every action/expression carries a ``SelectNamedBean`` /
``SelectEnum`` wrapper in JMRI Java that supports Direct /
Reference / LocalVariable / Formula addressing. This importer only
recognises **Direct** addressing (the common case -- a static
system name and a static enum value). Non-Direct addressing degrades
to a warning + a best-effort static value.
"""
from __future__ import annotations

import logging
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

from jmri_core.state import LightState, SensorState, TurnoutState
from lxml import etree

from jmri_logixng.actions.digital import (
    IfThenElse,
    LogAction,
    SetLightState,
    SetMemory,
    SetSensorState,
    SetTurnoutState,
)
from jmri_logixng.actions.flow import Delay, Sequence
from jmri_logixng.expressions.digital import (
    And,
    ConstantBool,
    LightIsOn,
    MemoryEquals,
    Not,
    Or,
    SensorIsActive,
    TurnoutIsClosed,
)
from jmri_logixng.logix import ConditionalNG, LogixNG
from jmri_logixng.serialize import save_yaml
from jmri_logixng.tree import Action, Expression, Node

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportWarning:
    """One diagnostic the importer recorded during a run."""

    kind: str          # e.g. "unknown-tag", "non-direct-addressing"
    detail: str        # human-readable specifics
    system_name: str   # the JMRI system name that produced it


@dataclass(slots=True)
class ImportResult:
    """Outcome of :func:`import_jmri_logixng_xml`."""

    programs: list[LogixNG] = field(default_factory=list)
    written: list[pathlib.Path] = field(default_factory=list)
    warnings: list[ImportWarning] = field(default_factory=list)


# ---- parse phase -------------------------------------------------------


def import_jmri_logixng_xml(
    source: str | pathlib.Path | bytes,
    out_dir: str | pathlib.Path | None = None,
) -> ImportResult:
    """Read *source* and reconstruct our LogixNG trees.

    *source* may be a filesystem path (``str`` / ``pathlib.Path``)
    or raw XML bytes -- the latter is convenient for wiring an
    upload endpoint that hands us the file contents directly
    without a temp file.

    When *out_dir* is given, each converted program is also
    serialized to ``<out_dir>/<slug>.yaml``. Returns an
    :class:`ImportResult` carrying the in-memory programs, the
    written files, and any :class:`ImportWarning` records for
    unsupported nodes / addressing modes / dangling references.
    """
    if isinstance(source, bytes):
        root = etree.fromstring(source)
    else:
        root = etree.parse(str(pathlib.Path(source))).getroot()

    parser = _JmriLogixNGParser(root)
    parser.build_index()
    result = parser.walk_programs()

    if out_dir is not None:
        out_dir_p = pathlib.Path(out_dir)
        out_dir_p.mkdir(parents=True, exist_ok=True)
        for program in result.programs:
            slug = _slugify(program.user_name or program.node_id or "logixng")
            path = out_dir_p / f"{slug}.yaml"
            save_yaml(program, path)
            result.written.append(path)

    return result


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("_", name).strip("_") or "logixng"


class _JmriLogixNGParser:
    """The stateful heavy lifter for :func:`import_jmri_logixng_xml`.

    Keeps the four manager-section indexes + a fresh warnings list.
    Callers use the two-phase API: :meth:`build_index` first, then
    :meth:`walk_programs`.
    """

    def __init__(self, root: etree._Element) -> None:
        self._root = root
        # System-name -> element indexes for the two node managers +
        # the two container managers.
        self._logixngs: dict[str, etree._Element] = {}
        self._conditionalngs: dict[str, etree._Element] = {}
        self._actions: dict[str, etree._Element] = {}
        self._expressions: dict[str, etree._Element] = {}
        self.warnings: list[ImportWarning] = []

    # ---- indexing ------------------------------------------------------

    def build_index(self) -> None:
        """Walk the top-level manager sections + populate indexes.

        Supports both:

        * The one-file layout where ``<LogixNGs>`` etc. are direct
          children of a ``<layout-config>``.
        * The nested layout where the same sections are inside a
          ``<layoutParameters>`` wrapper. (Some JMRI panel files
          use this form for encapsulation.)
        """
        self._index_section("LogixNGs", "LogixNG", self._logixngs)
        self._index_section(
            "LogixNGConditionalNGs", "ConditionalNG", self._conditionalngs,
        )
        self._index_section(
            "LogixNGDigitalActions", None, self._actions,
        )
        self._index_section(
            "LogixNGDigitalExpressions", None, self._expressions,
        )

    def _index_section(
        self,
        section_tag: str,
        child_tag: str | None,
        target: dict[str, etree._Element],
    ) -> None:
        """Fill *target* from the named manager section.

        *child_tag* narrows to only that XML tag when supplied -- used
        for ``LogixNGs`` / ``LogixNGConditionalNGs`` where the section
        holds entries of exactly one type. For actions and
        expressions the section holds a heterogeneous mix of tags
        (``ActionTurnout``, ``ActionSensor``, ...), so passing None
        indexes every child.
        """
        for section in self._root.iter(section_tag):
            for child in section:
                if child_tag is not None and child.tag != child_tag:
                    continue
                sn = child.findtext("systemName")
                if sn:
                    target[sn] = child

    # ---- walk ---------------------------------------------------------

    def walk_programs(self) -> ImportResult:
        """Reify every indexed ``<LogixNG>`` into a converted
        :class:`LogixNG` tree."""
        result = ImportResult(warnings=self.warnings)
        for system_name, element in self._logixngs.items():
            program = self._convert_logixng(system_name, element)
            result.programs.append(program)
        return result

    def _convert_logixng(
        self, system_name: str, element: etree._Element,
    ) -> LogixNG:
        user_name = element.findtext("userName") or ""
        conditionals: list[ConditionalNG] = []
        conditional_names_elem = element.find("ConditionalNGs")
        if conditional_names_elem is not None:
            for cn_ref in conditional_names_elem.findall("systemName"):
                ref_sn = (cn_ref.text or "").strip()
                cn_element = self._conditionalngs.get(ref_sn)
                if cn_element is None:
                    self.warnings.append(ImportWarning(
                        kind="dangling-conditional-ref",
                        detail=(
                            f"LogixNG {system_name!r} references "
                            f"missing ConditionalNG {ref_sn!r}"
                        ),
                        system_name=system_name,
                    ))
                    continue
                conditionals.append(self._convert_conditionalng(cn_element))
        return LogixNG(
            user_name=user_name,
            node_id=system_name,
            conditionals=conditionals,
        )

    def _convert_conditionalng(
        self, element: etree._Element,
    ) -> ConditionalNG:
        system_name = element.findtext("systemName") or ""
        user_name = element.findtext("userName") or ""
        # ConditionalNG has one root socket -> an Action.
        root_action = self._follow_socket(element.find("Socket"), Action)
        actions: list[Action] = [root_action] if root_action is not None else []
        return ConditionalNG(
            user_name=user_name, node_id=system_name, actions=actions,
        )

    # ---- socket helpers ------------------------------------------------

    def _follow_socket(
        self, socket: etree._Element | None, expected: type,
    ) -> Any:
        """Resolve a ``<Socket>`` element to the child :class:`Node`.

        Returns ``None`` when the socket is unconnected -- that's
        legitimate in JMRI ("empty branch"). Type mismatch (an
        Action socket resolved to an Expression element) becomes a
        warning + None; better than a crash mid-import.
        """
        if socket is None:
            return None
        ref = socket.findtext("systemName")
        if not ref:
            return None
        ref = ref.strip()
        source_dict = (
            self._actions if expected is Action else self._expressions
        )
        element = source_dict.get(ref)
        if element is None:
            self.warnings.append(ImportWarning(
                kind="dangling-socket-ref",
                detail=f"socket references missing {expected.__name__} {ref!r}",
                system_name=ref,
            ))
            return None
        return self._convert_node(ref, element, expected)

    def _convert_node(
        self, system_name: str, element: etree._Element, expected: type,
    ) -> Node | None:
        tag = element.tag
        handler = _NODE_HANDLERS.get(tag)
        if handler is None:
            self.warnings.append(ImportWarning(
                kind="unknown-tag",
                detail=f"no importer for XML tag {tag!r}",
                system_name=system_name,
            ))
            return self._placeholder_for(expected, tag, system_name)
        try:
            return handler(self, element, system_name)
        except Exception as e:  # noqa: BLE001
            self.warnings.append(ImportWarning(
                kind="handler-failed",
                detail=f"{tag} handler raised: {e}",
                system_name=system_name,
            ))
            return self._placeholder_for(expected, tag, system_name)

    def _placeholder_for(
        self, expected: type, tag: str, system_name: str,
    ) -> Node:
        """Return a node of the right kind so the tree stays valid
        even when we don't have a mapping. LogAction fills the
        Action slot; ConstantBool(False) fills the Expression slot."""
        if expected is Action:
            return LogAction(
                message=f"imported placeholder: unsupported LogixNG XML tag {tag!r}",
                level="warning",
                user_name=f"placeholder for {system_name}",
                node_id=None,
            )
        return ConstantBool(
            value=False, user_name=f"placeholder for {system_name}",
            node_id=None,
        )


# ---- field helpers ------------------------------------------------------


def _named_bean_sysname(
    parser: _JmriLogixNGParser, wrapper: etree._Element | None,
    system_name: str, field_name: str,
) -> str:
    """Pull the target bean's system name out of a
    ``<namedBean>`` wrapper. Warns on non-Direct addressing."""
    if wrapper is None:
        return ""
    addressing = wrapper.findtext("addressing")
    if addressing and addressing != "Direct":
        parser.warnings.append(ImportWarning(
            kind="non-direct-addressing",
            detail=f"field {field_name!r} uses {addressing!r} addressing; "
                   "only Direct is imported",
            system_name=system_name,
        ))
    return (wrapper.findtext("systemName") or "").strip()


def _selected_enum(
    parser: _JmriLogixNGParser, wrapper: etree._Element | None,
    system_name: str, field_name: str,
) -> str:
    """Pull the ``<enum>`` value out of a state wrapper."""
    if wrapper is None:
        return ""
    addressing = wrapper.findtext("addressing")
    if addressing and addressing != "Direct":
        parser.warnings.append(ImportWarning(
            kind="non-direct-addressing",
            detail=f"enum {field_name!r} uses {addressing!r} addressing",
            system_name=system_name,
        ))
    return (wrapper.findtext("enum") or "").strip()


# ---- individual node handlers ------------------------------------------


def _handle_action_turnout(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> SetTurnoutState:
    turnout = _named_bean_sysname(
        parser, element.find("namedBean"), system_name, "turnout",
    )
    state = _selected_enum(
        parser, element.find("state"), system_name, "state",
    ) or "CLOSED"
    return SetTurnoutState(
        turnout=turnout, state=_turnout_state(state),
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_action_sensor(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> SetSensorState:
    sensor = _named_bean_sysname(
        parser, element.find("namedBean"), system_name, "sensor",
    )
    state = _selected_enum(
        parser, element.find("state"), system_name, "state",
    ) or "INACTIVE"
    return SetSensorState(
        sensor=sensor, state=_sensor_state(state),
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_action_light(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> SetLightState:
    light = _named_bean_sysname(
        parser, element.find("namedBean"), system_name, "light",
    )
    state = _selected_enum(
        parser, element.find("state"), system_name, "state",
    ) or "OFF"
    return SetLightState(
        light=light, state=_light_state(state),
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_action_memory(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> SetMemory:
    memory = _named_bean_sysname(
        parser, element.find("memoryNamedBean") or element.find("namedBean"),
        system_name, "memory",
    )
    value = element.findtext("otherConstantValue") or element.findtext("data") or None
    return SetMemory(
        memory=memory, value=value,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_action_log(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> LogAction:
    message = element.findtext("message") or ""
    level = (element.findtext("logLevel") or "info").lower()
    return LogAction(
        message=message, level=level,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_if_then_else(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> IfThenElse:
    # JMRI's IfThenElse allows N expressions x N actions; we take
    # the first expression + first two actions (then/else).
    expressions_elem = element.find("Expressions")
    actions_elem = element.find("Actions")
    condition = None
    if expressions_elem is not None:
        first = expressions_elem.find("Socket")
        condition = parser._follow_socket(first, Expression)
    then_action = None
    else_action = None
    if actions_elem is not None:
        sockets = actions_elem.findall("Socket")
        if sockets:
            then_action = parser._follow_socket(sockets[0], Action)
        if len(sockets) > 1:
            else_action = parser._follow_socket(sockets[1], Action)
        if len(sockets) > 2:
            parser.warnings.append(ImportWarning(
                kind="multi-branch-truncated",
                detail=f"IfThenElse has {len(sockets)} action branches; "
                       "only the first two (then/else) are kept",
                system_name=system_name,
            ))
    if condition is None:
        condition = ConstantBool(value=False, node_id=None)
    if then_action is None:
        then_action = LogAction(
            message="imported IfThenElse: empty 'then' branch",
            level="debug", node_id=None,
        )
    return IfThenElse(
        condition=condition, then_action=then_action, else_action=else_action,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_digital_many(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> Sequence:
    """DigitalMany (a.k.a. sequence) -- run all connected sockets in
    order. Maps directly to our :class:`Sequence`."""
    actions: list[Action] = []
    actions_elem = element.find("Actions") or element.find("SocketList")
    if actions_elem is not None:
        for socket in actions_elem.findall("Socket"):
            child = parser._follow_socket(socket, Action)
            if child is not None:
                actions.append(child)
    return Sequence(
        actions=actions,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_digital_timer(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> Delay:
    """DigitalActionTimer / DigitalActionTimerSocket -> our simple
    Delay. JMRI Java's timer is far richer (multiple stages, action
    per stage) -- we take the first duration and warn."""
    seconds = 0.0
    duration_text = (
        element.findtext("duration")
        or element.findtext("Duration")
        or element.findtext("delay")
        or ""
    )
    try:
        seconds = float(duration_text) / 1000.0 if duration_text else 0.0
    except ValueError:
        seconds = 0.0
    return Delay(
        seconds=seconds,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


# ---- expression handlers -----------------------------------------------


def _handle_expression_sensor(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> Node:
    sensor = _named_bean_sysname(
        parser, element.find("namedBean"), system_name, "sensor",
    )
    state = _selected_enum(
        parser, element.find("state"), system_name, "state",
    ) or "ACTIVE"
    # JMRI supports IS/IS_NOT + arbitrary target state; we model
    # "IS ACTIVE" and negate for IS_NOT.
    is_isnot = (element.findtext("is_isNot") or "IS").upper()
    node: Node = SensorIsActive(
        sensor=sensor,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )
    if state != "ACTIVE":
        parser.warnings.append(ImportWarning(
            kind="expression-state-approximated",
            detail=f"ExpressionSensor {system_name}: modelling non-ACTIVE "
                   "target as SensorIsActive; will not match for INACTIVE",
            system_name=system_name,
        ))
    if is_isnot == "IS_NOT":
        node = Not(child=node, node_id=None)  # type: ignore[arg-type]
    return node


def _handle_expression_turnout(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> Node:
    turnout = _named_bean_sysname(
        parser, element.find("namedBean"), system_name, "turnout",
    )
    state = _selected_enum(
        parser, element.find("state"), system_name, "state",
    ) or "CLOSED"
    is_isnot = (element.findtext("is_isNot") or "IS").upper()
    node: Node = TurnoutIsClosed(
        turnout=turnout,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )
    if state != "CLOSED":
        parser.warnings.append(ImportWarning(
            kind="expression-state-approximated",
            detail=f"ExpressionTurnout {system_name}: modelling non-CLOSED "
                   "target as TurnoutIsClosed",
            system_name=system_name,
        ))
    if is_isnot == "IS_NOT":
        node = Not(child=node, node_id=None)  # type: ignore[arg-type]
    return node


def _handle_expression_light(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> Node:
    light = _named_bean_sysname(
        parser, element.find("namedBean"), system_name, "light",
    )
    is_isnot = (element.findtext("is_isNot") or "IS").upper()
    node: Node = LightIsOn(
        light=light,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )
    if is_isnot == "IS_NOT":
        node = Not(child=node, node_id=None)  # type: ignore[arg-type]
    return node


def _handle_expression_memory(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> MemoryEquals:
    memory = _named_bean_sysname(
        parser, element.find("namedBean"), system_name, "memory",
    )
    value = element.findtext("constantValue") or element.findtext("data") or None
    return MemoryEquals(
        memory=memory, value=value,
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_true(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> ConstantBool:
    return ConstantBool(
        value=True, user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_false(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> ConstantBool:
    return ConstantBool(
        value=False, user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_and(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> And:
    return And(
        children=_collect_expression_children(parser, element),
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_or(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> Or:
    return Or(
        children=_collect_expression_children(parser, element),
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _handle_not(
    parser: _JmriLogixNGParser, element: etree._Element, system_name: str,
) -> Not:
    child_expr = None
    sockets_elem = element.find("Expressions") or element
    for socket in sockets_elem.findall("Socket"):
        child_expr = parser._follow_socket(socket, Expression)
        if child_expr is not None:
            break
    if child_expr is None:
        child_expr = ConstantBool(value=False, node_id=None)
    return Not(
        child=child_expr,  # type: ignore[arg-type]
        user_name=element.findtext("userName") or "",
        node_id=system_name,
    )


def _collect_expression_children(
    parser: _JmriLogixNGParser, element: etree._Element,
) -> list[Expression[Any]]:
    children: list[Expression[Any]] = []
    exprs_elem = element.find("Expressions")
    if exprs_elem is None:
        return children
    for socket in exprs_elem.findall("Socket"):
        child = parser._follow_socket(socket, Expression)
        if child is not None:
            children.append(child)  # type: ignore[arg-type]
    return children


# ---- state translation --------------------------------------------------

def _turnout_state(name: str) -> TurnoutState:
    """Map JMRI enum values (``CLOSED`` / ``THROWN`` / ``UNKNOWN``) to
    our TurnoutState. Unknown -> CLOSED (least-surprise default)."""
    try:
        return TurnoutState[name.upper()]
    except KeyError:
        return TurnoutState.CLOSED


def _sensor_state(name: str) -> SensorState:
    try:
        return SensorState[name.upper()]
    except KeyError:
        return SensorState.INACTIVE


def _light_state(name: str) -> LightState:
    try:
        return LightState[name.upper()]
    except KeyError:
        return LightState.OFF


# ---- tag dispatch registry ---------------------------------------------

# The XML tag names come straight from JMRI Java's
# ``configurexml/*Xml.java`` handlers (see the ``new Element(...)``
# calls quoted at the top of each). Every tag matches an
# ``ActionXxx`` or ``ExpressionXxx`` class in JMRI Java.
_NODE_HANDLERS: dict[str, Any] = {
    # Actions
    "ActionTurnout": _handle_action_turnout,
    "ActionSensor": _handle_action_sensor,
    "ActionLight": _handle_action_light,
    "ActionMemory": _handle_action_memory,
    "LogLocalVariables": _handle_action_log,
    "IfThenElse": _handle_if_then_else,
    "DigitalMany": _handle_digital_many,
    "DigitalActionTimer": _handle_digital_timer,
    # Expressions
    "ExpressionSensor": _handle_expression_sensor,
    "ExpressionTurnout": _handle_expression_turnout,
    "ExpressionLight": _handle_expression_light,
    "ExpressionMemory": _handle_expression_memory,
    "True": _handle_true,
    "False": _handle_false,
    "And": _handle_and,
    "Or": _handle_or,
    "Not": _handle_not,
}
