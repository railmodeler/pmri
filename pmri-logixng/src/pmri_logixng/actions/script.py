"""ScriptAction -- execute a user Python snippet as one action in the tree.

The snippet runs on the current event loop, not on the ``ScriptRunner``
worker thread used by :mod:`pmri_scripting`: LogixNG actions are already
async, and threading a fresh worker for a five-line snippet would
serialize badly under any real trigger load.

The snippet body is wrapped in ``async def`` so ``await`` calls on bean
methods work naturally:

.. code-block:: yaml

    type: action:script
    source: |
      turnout = ctx.connection.turnouts.get("IT1")
      if turnout is not None:
          await turnout.set_commanded_state(TurnoutState.THROWN)

Globals visible to the snippet:

* ``ctx`` -- the :class:`~pmri_logixng.ExecutionContext`
* ``connection`` -- ``ctx.connection`` (convenience alias)
* ``log`` -- ``ctx.log``
* ``TurnoutState``, ``SensorState``, ``LightState`` -- the state enums
* every key in the action's ``bindings`` mapping

Exceptions inside the snippet are caught + logged; they don't abort
sibling actions. This matches the ``SetTurnoutState`` etc. contract of
"missing bean -> warn, don't raise". A snippet that legitimately wants
to signal failure to the surrounding tree can re-raise; only the
inner-most :class:`~pmri_logixng.actions.digital.IfThenElse` etc. will
see it, and even they still swallow it here.
"""
from __future__ import annotations

import textwrap
from typing import Any, ClassVar

from pmri_core.state import LightState, SensorState, TurnoutState

from pmri_logixng.context import ExecutionContext
from pmri_logixng.registry import register
from pmri_logixng.tree import Action

_WRAPPER_NAME = "_logixng_script_action_body"


@register
class ScriptAction(Action):
    """Run *source* as an inline async Python snippet.

    *source* may contain ``await`` at top level; every non-empty
    action.execute call recompiles + runs it. Compilation errors are
    caught at execute-time (not construction-time) so a broken snippet
    in a serialized tree doesn't take out load_yaml -- the tree still
    loads, and the individual action logs the compile error.
    """

    type_tag: ClassVar[str] = "action:script"

    def __init__(
        self,
        *,
        source: str,
        bindings: dict[str, Any] | None = None,
        user_name: str = "",
        node_id: str | None = None,
    ) -> None:
        super().__init__(user_name=user_name, node_id=node_id)
        self.source = source
        self.bindings = dict(bindings) if bindings else {}

    async def execute(self, ctx: ExecutionContext) -> None:
        ctx.emit_trace(f"script action ({len(self.source)} chars)")
        wrapped = self._wrap_source(self.source)
        globals_dict: dict[str, Any] = {
            "ctx": ctx,
            "connection": ctx.connection,
            "log": ctx.log,
            "TurnoutState": TurnoutState,
            "SensorState": SensorState,
            "LightState": LightState,
            **self.bindings,
        }
        try:
            code = compile(wrapped, "<logixng-script>", "exec")
        except SyntaxError as e:
            ctx.log.warning("ScriptAction: syntax error at line %s: %s",
                            e.lineno, e.msg)
            return
        try:
            exec(code, globals_dict)  # noqa: S102 -- script execution is the feature
            body = globals_dict[_WRAPPER_NAME]
            await body()
        except Exception:  # noqa: BLE001 -- log + swallow, don't crash the tree
            ctx.log.exception("ScriptAction: snippet raised")

    def extra_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source}
        if self.bindings:
            out["bindings"] = dict(self.bindings)
        return out

    @staticmethod
    def _wrap_source(source: str) -> str:
        """Wrap *source* in ``async def _body()`` so top-level ``await``
        works. Empty snippet becomes an empty async body (pass)."""
        indented = textwrap.indent(source, "    ")
        if not indented.strip():
            indented = "    pass"
        return f"async def {_WRAPPER_NAME}():\n{indented}\n"
