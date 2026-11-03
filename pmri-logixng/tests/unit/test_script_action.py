"""ScriptAction -- inline Python snippet as one node in the tree."""
from __future__ import annotations

import logging

from pmri_core.state import LightState, TurnoutState

from pmri_logixng import (
    ExecutionContext,
    ScriptAction,
    from_yaml,
    to_yaml,
)


async def test_script_runs_sync_body(ctx: ExecutionContext) -> None:
    """A snippet with no await runs to completion + can mutate bindings."""
    scratch: list[int] = []
    action = ScriptAction(
        source="scratch.append(1); scratch.append(2)",
        bindings={"scratch": scratch},
    )
    await action.execute(ctx)
    assert scratch == [1, 2]


async def test_script_runs_await(ctx: ExecutionContext) -> None:
    """Snippet uses top-level ``await`` to drive a bean method."""
    t = await ctx.connection.turnouts.provide("IT_S1")
    action = ScriptAction(source=(
        "bean = connection.turnouts.get('IT_S1')\n"
        "await bean.set_commanded_state(TurnoutState.THROWN)\n"
    ))
    await action.execute(ctx)
    assert t.commanded_state == TurnoutState.THROWN


async def test_script_sees_ctx_and_bindings(ctx: ExecutionContext) -> None:
    """ctx + user bindings are both visible."""
    l = await ctx.connection.lights.provide("IL_S1")
    action = ScriptAction(
        source=(
            "log.info('marker')\n"
            "await connection.lights.get(target).set_state(LightState.ON)\n"
        ),
        bindings={"target": "IL_S1"},
    )
    await action.execute(ctx)
    assert l.state == LightState.ON


async def test_script_swallows_exceptions(
    ctx: ExecutionContext, caplog: object,
) -> None:
    """Exceptions inside the snippet log but don't propagate."""
    from _pytest.logging import LogCaptureFixture  # noqa: PLC0415
    assert isinstance(caplog, LogCaptureFixture)
    with caplog.at_level(logging.ERROR, logger="pmri_logixng"):
        action = ScriptAction(source="raise RuntimeError('boom')")
        await action.execute(ctx)   # must not raise
    assert any("snippet raised" in r.getMessage() for r in caplog.records)


async def test_script_syntax_error_logs_and_returns(
    ctx: ExecutionContext, caplog: object,
) -> None:
    from _pytest.logging import LogCaptureFixture  # noqa: PLC0415
    assert isinstance(caplog, LogCaptureFixture)
    with caplog.at_level(logging.WARNING, logger="pmri_logixng"):
        action = ScriptAction(source="this is not valid python !!!")
        await action.execute(ctx)
    assert any("syntax error" in r.getMessage() for r in caplog.records)


async def test_script_empty_source_is_noop(ctx: ExecutionContext) -> None:
    await ScriptAction(source="").execute(ctx)
    await ScriptAction(source="\n\n").execute(ctx)


def test_script_action_round_trip_yaml() -> None:
    original = ScriptAction(
        source="await connection.turnouts.get('IT1').set_commanded_state(TurnoutState.THROWN)",
        bindings={"note": "test"},
        user_name="Throw IT1",
    )
    text = to_yaml(original)
    restored = from_yaml(text)
    assert isinstance(restored, ScriptAction)
    assert restored.source == original.source
    assert restored.bindings == {"note": "test"}
    assert restored.user_name == "Throw IT1"


def test_script_action_wraps_empty_source_as_pass() -> None:
    """Sanity: internal wrapper produces valid Python for empty input."""
    wrapped = ScriptAction._wrap_source("")
    compile(wrapped, "<t>", "exec")   # would raise on invalid syntax
    wrapped = ScriptAction._wrap_source("   \n\n")
    compile(wrapped, "<t>", "exec")
