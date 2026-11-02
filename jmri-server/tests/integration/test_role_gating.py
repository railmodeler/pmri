"""Role-gate dispatch tests.

Auth stays off for these tests (default). We drive the registry
directly with an explicit ``role=`` so the middleware isn't in the
picture -- that keeps the tests focused on the JsonService/registry
contract rather than on the auth wire.
"""
from __future__ import annotations

import pytest

from jmri_server.auth.models import Role
from jmri_server.envelope import JmriRequest
from jmri_server.service import JsonServiceError


# ---- default role gates -----------------------------------------------


async def test_viewer_can_get_turnout_list(client, handle) -> None:
    """Default reads (``list`` / ``get``) require ``viewer`` -- the
    lowest role -- so every authenticated user can browse."""
    resp = await handle.registry.dispatch(
        JmriRequest(type="turnout", method="list", data={}),
        role=Role.VIEWER,
    )
    assert resp.type == "list"


async def test_viewer_blocked_from_turnout_post(handle) -> None:
    """Default mutations require ``operator``. Viewer can't drive."""
    with pytest.raises(JsonServiceError) as exc:
        await handle.registry.dispatch(
            JmriRequest(
                type="turnout", method="post",
                data={"name": "IT1", "state": 2},
            ),
            role=Role.VIEWER,
        )
    assert exc.value.code == 403
    assert "operator" in exc.value.message


async def test_operator_can_post_turnout(handle) -> None:
    """Sanity: escalating to operator gets past the gate."""
    # Provide the turnout first so post doesn't 404.
    await handle.registry.dispatch(
        JmriRequest(
            type="turnout", method="put",
            data={"name": "IT1"},
        ),
        role=Role.OPERATOR,
    )
    resp = await handle.registry.dispatch(
        JmriRequest(
            type="turnout", method="post",
            data={"name": "IT1", "state": 2},
        ),
        role=Role.OPERATOR,
    )
    assert resp.type == "turnout"


# ---- per-service overrides --------------------------------------------


async def test_operator_blocked_from_config_profile_put(handle) -> None:
    """``configProfile`` mutations are admin-only. Operator can't
    create a profile -- even though they can drive beans."""
    with pytest.raises(JsonServiceError) as exc:
        await handle.registry.dispatch(
            JmriRequest(
                type="configProfile", method="put",
                data={"name": "sneaky"},
            ),
            role=Role.OPERATOR,
        )
    assert exc.value.code == 403


async def test_operator_blocked_from_scripts_put(handle) -> None:
    """Scripts execute arbitrary code -- admin-only for mutations
    even though scripts.list stays open for operators."""
    with pytest.raises(JsonServiceError) as exc:
        await handle.registry.dispatch(
            JmriRequest(
                type="scripts", method="put",
                data={"name": "malicious", "source": "print('pwn')"},
            ),
            role=Role.OPERATOR,
        )
    assert exc.value.code == 403


async def test_viewer_blocked_from_scripts_list(handle) -> None:
    """Scripts.list is elevated to operator -- viewers don't need
    to see script state, and it makes the surface smaller."""
    with pytest.raises(JsonServiceError) as exc:
        await handle.registry.dispatch(
            JmriRequest(type="scripts", method="list", data={}),
            role=Role.VIEWER,
        )
    assert exc.value.code == 403


async def test_admin_can_do_everything(handle) -> None:
    """Sanity: admin passes every gate we set up above."""
    for req in [
        JmriRequest(type="configProfile", method="list", data={}),
        JmriRequest(type="scripts", method="list", data={}),
        JmriRequest(type="turnout", method="list", data={}),
    ]:
        await handle.registry.dispatch(req, role=Role.ADMIN)


async def test_default_admin_role_backward_compat(handle) -> None:
    """When callers don't pass ``role=``, the registry defaults to
    admin -- preserves the pre-auth behaviour for tests that don't
    care about auth."""
    resp = await handle.registry.dispatch(
        JmriRequest(
            type="configProfile", method="list", data={},
        ),
    )
    # Would fail (403) if the default weren't admin.
    assert resp.type == "list"
