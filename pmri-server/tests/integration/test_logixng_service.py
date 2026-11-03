"""Integration tests for the ``logixng`` JSON topic.

There's no JMRI-Java precedent for this topic, so the tests pin our
own wire shape. Anything the /logixng web page relies on must be
covered here.
"""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from tests.conftest import post


def _sample_tree() -> dict:
    """A LogixNG dict identical to what ``LogixNG.to_dict()`` emits --
    the JSON path uses this shape to upload trees."""
    return {
        "type": "logixng:root",
        "id": "prog-1",
        "user_name": "Yard automation",
        "enabled": True,
        "conditionals": [
            {
                "type": "logixng:conditional",
                "id": "cond-1",
                "user_name": "Set turnout",
                "enabled": True,
                "actions": [
                    {
                        "type": "action:set-turnout",
                        "id": "act-1",
                        "turnout": "IT1",
                        "state": "CLOSED",
                    },
                ],
            },
        ],
    }


# ---- CRUD ----------------------------------------------------------------


def test_list_empty_by_default(client: TestClient) -> None:
    resp = post(client, "logixng", method="list")
    assert resp["data"] == []


def test_put_creates_empty_program(client: TestClient) -> None:
    resp = post(client, "logixng", method="put", data={
        "name": "IQ1", "userName": "Yard",
    })
    d = resp["data"]
    assert d["name"] == "IQ1"
    assert d["userName"] == "Yard"
    assert d["enabled"] is True
    assert d["conditionals"] == []


def test_put_full_tree_roundtrips(client: TestClient) -> None:
    """Uploading a tree via ``put`` returns the same structure on
    subsequent ``get``. Actions must be preserved down to type + fields."""
    tree = _sample_tree()
    post(client, "logixng", method="put", data={
        "name": "IQ2", "tree": tree,
    })
    resp = post(client, "logixng", method="get", data={"name": "IQ2"})
    d = resp["data"]
    assert d["name"] == "IQ2"
    assert d["userName"] == "Yard automation"
    assert len(d["conditionals"]) == 1
    c = d["conditionals"][0]
    assert c["user_name"] == "Set turnout"
    assert len(c["actions"]) == 1
    a = c["actions"][0]
    assert a["type"] == "action:set-turnout"
    assert a["turnout"] == "IT1"
    assert a["state"] == "CLOSED"


def test_get_unknown_returns_404(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "logixng", "method": "get", "data": {"name": "IQnope"},
    })
    assert r.status_code == 404


def test_delete_drops_the_program(client: TestClient) -> None:
    post(client, "logixng", method="put", data={"name": "IQdel"})
    r = post(client, "logixng", method="delete", data={"name": "IQdel"})
    assert r["data"]["deleted"] is True
    r2 = client.post("/json/", json={
        "type": "logixng", "method": "get", "data": {"name": "IQdel"},
    })
    assert r2.status_code == 404


def test_put_bad_tree_400s(client: TestClient) -> None:
    """A tree that decodes to something other than a LogixNG root
    (e.g. a bare Action) must be rejected with a clear error."""
    r = client.post("/json/", json={
        "type": "logixng", "method": "put",
        "data": {
            "name": "IQbad",
            "tree": {"type": "action:log", "message": "hi"},
        },
    })
    assert r.status_code == 400


def test_put_missing_type_400s(client: TestClient) -> None:
    r = client.post("/json/", json={
        "type": "logixng", "method": "put",
        "data": {"name": "IQmissing", "tree": {"userName": "no type"}},
    })
    assert r.status_code == 400


# ---- enable / disable ---------------------------------------------------


def test_disable_and_enable(client: TestClient) -> None:
    post(client, "logixng", method="put", data={"name": "IQe"})
    r_disable = post(client, "logixng", method="post", data={
        "name": "IQe", "command": "disable",
    })
    assert r_disable["data"]["enabled"] is False
    r_enable = post(client, "logixng", method="post", data={
        "name": "IQe", "command": "enable",
    })
    assert r_enable["data"]["enabled"] is True


# ---- run ----------------------------------------------------------------


def test_run_all_executes_actions(client: TestClient) -> None:
    """Running a program with a SetTurnoutState action should actually
    mutate the target turnout on the connection."""
    # Ensure the target turnout exists on the internal connection.
    post(client, "turnout", method="put", data={"name": "IT1"})
    post(client, "logixng", method="put", data={
        "name": "IQrun", "tree": _sample_tree(),
    })
    post(client, "logixng", method="post", data={
        "name": "IQrun", "command": "run",
    })
    r = post(client, "turnout", method="get", data={"name": "IT1"})
    # Internal turnout state is wire-mapped; CLOSED == 2 on JMRI wire.
    assert r["data"]["state"] == 2


def test_run_conditional_by_id(client: TestClient) -> None:
    post(client, "turnout", method="put", data={"name": "IT2"})
    tree = _sample_tree()
    tree["conditionals"][0]["actions"][0]["turnout"] = "IT2"
    post(client, "logixng", method="put", data={
        "name": "IQrunone", "tree": tree,
    })
    post(client, "logixng", method="post", data={
        "name": "IQrunone", "command": "run_conditional",
        "conditional_id": "cond-1",
    })
    r = post(client, "turnout", method="get", data={"name": "IT2"})
    assert r["data"]["state"] == 2


def test_run_conditional_unknown_id_404s(client: TestClient) -> None:
    post(client, "logixng", method="put", data={
        "name": "IQruntag", "tree": _sample_tree(),
    })
    r = client.post("/json/", json={
        "type": "logixng", "method": "post",
        "data": {
            "name": "IQruntag", "command": "run_conditional",
            "conditional_id": "nope",
        },
    })
    assert r.status_code == 404


def test_unknown_command_400s(client: TestClient) -> None:
    post(client, "logixng", method="put", data={"name": "IQcmd"})
    r = client.post("/json/", json={
        "type": "logixng", "method": "post",
        "data": {"name": "IQcmd", "command": "levitate"},
    })
    assert r.status_code == 400


def test_conditional_enable_and_disable(client: TestClient) -> None:
    """Toggle one conditional's enabled flag without touching siblings
    or the program-level flag."""
    tree = _sample_tree()
    tree["conditionals"].append({
        "type": "logixng:conditional", "id": "cond-2",
        "user_name": "sibling", "enabled": True, "actions": [],
    })
    post(client, "logixng", method="put", data={
        "name": "IQcond", "tree": tree,
    })
    # Disable cond-1
    post(client, "logixng", method="post", data={
        "name": "IQcond", "command": "conditional_disable",
        "conditional_id": "cond-1",
    })
    got = post(client, "logixng", method="get", data={"name": "IQcond"})
    conds = got["data"]["conditionals"]
    assert conds[0]["id"] == "cond-1" and conds[0]["enabled"] is False
    assert conds[1]["id"] == "cond-2" and conds[1]["enabled"] is True
    assert got["data"]["enabled"] is True   # program stays on
    # Re-enable
    post(client, "logixng", method="post", data={
        "name": "IQcond", "command": "conditional_enable",
        "conditional_id": "cond-1",
    })
    got = post(client, "logixng", method="get", data={"name": "IQcond"})
    assert got["data"]["conditionals"][0]["enabled"] is True


def test_conditional_toggle_unknown_id_404s(client: TestClient) -> None:
    post(client, "logixng", method="put", data={
        "name": "IQcondmiss", "tree": _sample_tree(),
    })
    r = client.post("/json/", json={
        "type": "logixng", "method": "post",
        "data": {
            "name": "IQcondmiss", "command": "conditional_disable",
            "conditional_id": "no-such-id",
        },
    })
    assert r.status_code == 404


def test_replace_yaml_swaps_whole_tree(client: TestClient) -> None:
    """Send new YAML source; program is replaced in place."""
    post(client, "logixng", method="put", data={
        "name": "IQyaml", "userName": "original", "tree": _sample_tree(),
    })
    new_yaml = (
        "type: logixng:root\n"
        "user_name: rewritten\n"
        "enabled: true\n"
        "conditionals:\n"
        "  - type: logixng:conditional\n"
        "    id: cond-x\n"
        "    enabled: true\n"
        "    actions:\n"
        "      - type: action:log\n"
        "        message: hello\n"
    )
    post(client, "logixng", method="post", data={
        "name": "IQyaml", "command": "replace_yaml", "yaml": new_yaml,
    })
    got = post(client, "logixng", method="get", data={"name": "IQyaml"})
    assert got["data"]["userName"] == "rewritten"
    assert got["data"]["conditionals"][0]["id"] == "cond-x"
    assert got["data"]["conditionals"][0]["actions"][0]["type"] == "action:log"


def test_update_node_leaf_fields_preserve_tree(client: TestClient) -> None:
    """Updating a leaf's fields (user_name, bean refs, scalar values)
    must leave the surrounding tree intact. Pin: without preserve-child
    semantics, container-node field edits would nuke their children."""
    post(client, "logixng", method="put", data={
        "name": "IQnode1", "tree": _sample_tree(),
    })
    # Rename the action's user_name field. The surrounding
    # conditional + action list must stay the same.
    post(client, "logixng", method="post", data={
        "name": "IQnode1", "command": "update_node",
        "node_id": "act-1",
        "fields": {
            "type": "action:set-turnout",
            "user_name": "Renamed",
            "turnout": "IT1",
            "state": "THROWN",
        },
    })
    got = post(client, "logixng", method="get", data={"name": "IQnode1"})
    conds = got["data"]["conditionals"]
    assert conds[0]["actions"][0]["user_name"] == "Renamed"
    assert conds[0]["actions"][0]["state"] == "THROWN"
    assert conds[0]["id"] == "cond-1"   # conditional untouched


def test_update_node_rejects_type_change(client: TestClient) -> None:
    """update_node is for edits WITHIN a type; changing the type is
    a delete + add. Rejecting here prevents a confused UI from
    silently dropping child nodes when the user picks the wrong
    picker."""
    post(client, "logixng", method="put", data={
        "name": "IQnode2", "tree": _sample_tree(),
    })
    r = client.post("/json/", json={
        "type": "logixng", "method": "post",
        "data": {
            "name": "IQnode2", "command": "update_node",
            "node_id": "act-1",
            "fields": {"type": "action:log", "message": "x"},
        },
    })
    assert r.status_code == 400


def test_add_child_appends_to_list_slot(client: TestClient) -> None:
    """A list slot (ConditionalNG.actions, Sequence.actions, etc.)
    accepts an append via add_child."""
    post(client, "logixng", method="put", data={
        "name": "IQadd1", "tree": _sample_tree(),
    })
    post(client, "logixng", method="post", data={
        "name": "IQadd1", "command": "add_child",
        "parent_id": "cond-1", "slot": "actions",
        "child": {
            "type": "action:log", "id": "appended",
            "message": "hello",
        },
    })
    got = post(client, "logixng", method="get", data={"name": "IQadd1"})
    acts = got["data"]["conditionals"][0]["actions"]
    assert [a["id"] for a in acts] == ["act-1", "appended"]


def test_add_child_rejects_unknown_slot(client: TestClient) -> None:
    post(client, "logixng", method="put", data={
        "name": "IQadd2", "tree": _sample_tree(),
    })
    r = client.post("/json/", json={
        "type": "logixng", "method": "post",
        "data": {
            "name": "IQadd2", "command": "add_child",
            "parent_id": "cond-1", "slot": "nope",
            "child": {"type": "action:log", "message": "x"},
        },
    })
    assert r.status_code == 400


def test_delete_node_removes_from_list(client: TestClient) -> None:
    """Delete walks the tree, finds the parent, drops the entry."""
    tree = _sample_tree()
    # Add a second action so delete leaves something to verify.
    tree["conditionals"][0]["actions"].append({
        "type": "action:log", "id": "to-delete", "message": "x",
    })
    post(client, "logixng", method="put", data={
        "name": "IQdel1", "tree": tree,
    })
    post(client, "logixng", method="post", data={
        "name": "IQdel1", "command": "delete_node",
        "node_id": "to-delete",
    })
    got = post(client, "logixng", method="get", data={"name": "IQdel1"})
    acts = got["data"]["conditionals"][0]["actions"]
    assert [a["id"] for a in acts] == ["act-1"]


def test_delete_node_404_on_missing(client: TestClient) -> None:
    post(client, "logixng", method="put", data={
        "name": "IQdel2", "tree": _sample_tree(),
    })
    r = client.post("/json/", json={
        "type": "logixng", "method": "post",
        "data": {
            "name": "IQdel2", "command": "delete_node",
            "node_id": "ghost",
        },
    })
    assert r.status_code == 404


def test_replace_yaml_bad_content_400s(client: TestClient) -> None:
    post(client, "logixng", method="put", data={
        "name": "IQbadyaml", "tree": _sample_tree(),
    })
    r = client.post("/json/", json={
        "type": "logixng", "method": "post",
        "data": {
            "name": "IQbadyaml", "command": "replace_yaml",
            "yaml": "type: nonsense\nfoo: bar",
        },
    })
    assert r.status_code == 400
    # Original still present
    got = post(client, "logixng", method="get", data={"name": "IQbadyaml"})
    assert got["data"]["userName"] == "Yard automation"


# ---- persistence --------------------------------------------------------


def test_put_persists_to_disk_when_dir_attached(
    tmp_path: pathlib.Path, handle,
) -> None:
    """When the ctx has ``_logixng_dir`` attached (production path),
    ``put`` writes the program to disk so it survives a restart."""
    logixng_dir = tmp_path / "logixng"
    handle.ctx._logixng_dir = logixng_dir
    with TestClient(handle.app) as tc:
        tc.post("/json/", json={
            "type": "logixng", "method": "put",
            "data": {"name": "IQdisk", "tree": _sample_tree()},
        })
        assert (logixng_dir / "IQdisk.yaml").exists()
        # Delete via API removes from disk too.
        tc.post("/json/", json={
            "type": "logixng", "method": "delete",
            "data": {"name": "IQdisk"},
        })
        assert not (logixng_dir / "IQdisk.yaml").exists()


def test_service_reports_503_when_manager_absent(handle) -> None:
    """If pmri-logixng isn't installed, ``ctx.logix_ng`` is None and
    every request should 503 rather than crash."""
    handle.ctx.logix_ng = None
    with TestClient(handle.app) as tc:
        r = tc.post("/json/", json={"type": "logixng", "method": "list"})
        assert r.status_code == 503
