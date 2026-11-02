"""LogixNG detail page + YAML editor + per-conditional toggle + types catalog."""
from __future__ import annotations

from fastapi.testclient import TestClient


# ---- type catalog -------------------------------------------------------


def test_types_catalog_lists_registered_tags(client: TestClient) -> None:
    r = client.get("/logixng/types")
    r.raise_for_status()
    body = r.text
    # Actions we shipped in this beta
    assert "action:set-turnout" in body
    assert "action:write-cv" in body
    assert "action:script" in body
    assert "action:set-memory-from-expression" in body
    # New expressions
    assert "expr:num-op" in body
    assert "expr:string-concat" in body
    # Containers
    assert "logixng:root" in body
    assert "logixng:conditional" in body


# ---- detail page --------------------------------------------------------


def test_detail_page_renders_yaml(client: TestClient) -> None:
    """After creating an empty program, the detail page shows a YAML
    textarea containing the serialized tree."""
    client.post("/logixng/create", data={
        "name": "IQdetail1", "userName": "Detail test",
    }).raise_for_status()
    r = client.get("/logixng/IQdetail1")
    r.raise_for_status()
    body = r.text
    # Full-page title + textarea
    assert "IQdetail1" in body
    assert "<textarea" in body
    # Serialized YAML mentions the root type tag
    assert "logixng:root" in body


def test_detail_page_404_on_missing(client: TestClient) -> None:
    r = client.get("/logixng/IQmissing_zzz")
    assert r.status_code == 404


def test_save_yaml_round_trips(client: TestClient) -> None:
    """POST /save-yaml with a modified tree replaces the program."""
    client.post("/logixng/create", data={"name": "IQyaml1"}).raise_for_status()
    new_yaml = """type: logixng:root
user_name: Renamed via YAML
enabled: true
conditionals:
  - type: logixng:conditional
    user_name: added
    enabled: true
    actions:
      - type: action:log
        message: hello
        level: info
"""
    r = client.post(
        "/logixng/IQyaml1/save-yaml", data={"yaml": new_yaml},
    )
    r.raise_for_status()
    assert "Saved" in r.text
    # Detail page now reflects the new tree
    detail = client.get("/logixng/IQyaml1")
    detail.raise_for_status()
    body = detail.text
    assert "Renamed via YAML" in body
    assert "action:log" in body
    assert "1 conditional" in body


def test_save_yaml_invalid_returns_error_but_keeps_original(
    client: TestClient,
) -> None:
    client.post("/logixng/create", data={
        "name": "IQyaml2", "userName": "Untouched",
    }).raise_for_status()
    r = client.post(
        "/logixng/IQyaml2/save-yaml",
        data={"yaml": "type: logixng:root\nconditionals: not-a-list"},
    )
    # 200 body with an error span (htmx-friendly)
    assert "Save failed" in r.text
    # Original program still present
    detail = client.get("/logixng/IQyaml2")
    detail.raise_for_status()
    assert "Untouched" in detail.text


def test_save_yaml_rejects_non_root_tree(client: TestClient) -> None:
    """YAML that parses but isn't a logixng:root at the top must fail."""
    client.post("/logixng/create", data={"name": "IQyaml3"}).raise_for_status()
    r = client.post(
        "/logixng/IQyaml3/save-yaml",
        data={"yaml": "type: action:log\nmessage: hi\n"},
    )
    assert "Save failed" in r.text


# ---- per-conditional toggle --------------------------------------------


_TREE_WITH_ONE_CONDITIONAL = """type: logixng:root
enabled: true
conditionals:
  - type: logixng:conditional
    id: cond-toggle-test
    enabled: true
    actions:
      - type: action:log
        message: probe
"""


def test_disable_one_conditional(client: TestClient) -> None:
    client.post("/logixng/create", data={"name": "IQtoggle1"}).raise_for_status()
    client.post(
        "/logixng/IQtoggle1/save-yaml",
        data={"yaml": _TREE_WITH_ONE_CONDITIONAL},
    ).raise_for_status()

    # Initially enabled: the tree view's conditional badge reads "enabled".
    detail = client.get("/logixng/IQtoggle1").text
    marker = 'data-enabled-for="cond-toggle-test"'
    idx = detail.find(marker)
    assert idx >= 0
    end = detail.find("</span>", idx)
    assert "enabled" in detail[idx:end] and "disabled" not in detail[idx:end]

    r = client.post(
        "/logixng/IQtoggle1/conditional/cond-toggle-test/disable",
    )
    assert r.status_code == 200

    detail = client.get("/logixng/IQtoggle1").text
    idx = detail.find(marker)
    end = detail.find("</span>", idx)
    assert "disabled" in detail[idx:end]


def test_disable_missing_conditional_returns_404(
    client: TestClient,
) -> None:
    client.post("/logixng/create", data={"name": "IQtoggle2"}).raise_for_status()
    r = client.post(
        "/logixng/IQtoggle2/conditional/does-not-exist/disable",
    )
    assert r.status_code == 404


# ---- tree editor ---------------------------------------------------------
#
# The /tree route + node/{id}/edit, add, delete routes drive the visual
# editor. Each test posts an interaction and asserts the tree fragment
# reflects the mutation.


_TREE_WITH_CONDITIONAL_AND_ACTION = """type: logixng:root
enabled: true
id: tree-root-1
conditionals:
  - type: logixng:conditional
    id: cond-tree-1
    enabled: true
    user_name: morning yard
    actions:
      - type: action:log
        id: act-log-1
        message: hello
"""


def test_tree_route_renders_recursive_structure(client: TestClient) -> None:
    client.post("/logixng/create", data={"name": "IQtree1"}).raise_for_status()
    client.post(
        "/logixng/IQtree1/save-yaml",
        data={"yaml": _TREE_WITH_CONDITIONAL_AND_ACTION},
    ).raise_for_status()
    r = client.get("/logixng/IQtree1/tree")
    r.raise_for_status()
    body = r.text
    # Every node renders with its data-node-id + data-node-type attrs.
    assert 'data-node-id="tree-root-1"' in body
    assert 'data-node-id="cond-tree-1"' in body
    assert 'data-node-id="act-log-1"' in body
    assert 'data-node-type="logixng:root"' in body
    assert 'data-node-type="logixng:conditional"' in body
    assert 'data-node-type="action:log"' in body
    # Root has an "enabled" badge; its data-enabled-for links to its id.
    assert 'data-enabled-for="tree-root-1"' in body
    # The leaf action shows the compact "[level] message" summary.
    assert "[info] hello" in body


def test_node_edit_form_returns_type_specific_inputs(client: TestClient) -> None:
    """Edit on a set-turnout node must show the turnout + state fields,
    not just the generic user_name box."""
    client.post("/logixng/create", data={"name": "IQedit1"}).raise_for_status()
    client.post(
        "/logixng/IQedit1/save-yaml",
        data={"yaml": (
            "type: logixng:root\nid: ed-root\nenabled: true\nconditionals:\n"
            "  - type: logixng:conditional\n    id: ed-cond\n    enabled: true\n"
            "    actions:\n"
            "      - type: action:set-turnout\n        id: ed-act\n"
            "        turnout: IT9\n        state: CLOSED\n"
        )},
    ).raise_for_status()
    r = client.get("/logixng/IQedit1/node/ed-act/edit")
    r.raise_for_status()
    body = r.text
    assert 'name="turnout"' in body and 'value="IT9"' in body
    assert 'name="state"' in body
    # The form posts back to update, and swaps the whole tree fragment.
    assert 'hx-post="/logixng/IQedit1/node/ed-act/update"' in body
    assert 'hx-target="#logixng-tree"' in body


def test_node_update_persists_leaf_change(client: TestClient) -> None:
    """Submit an edit form; the stored YAML reflects the new field value."""
    client.post("/logixng/create", data={"name": "IQupd1"}).raise_for_status()
    client.post(
        "/logixng/IQupd1/save-yaml",
        data={"yaml": (
            "type: logixng:root\nid: u-root\nenabled: true\nconditionals:\n"
            "  - type: logixng:conditional\n    id: u-cond\n    enabled: true\n"
            "    actions:\n"
            "      - type: action:log\n        id: u-log\n"
            "        message: before\n        level: info\n"
        )},
    ).raise_for_status()
    r = client.post(
        "/logixng/IQupd1/node/u-log/update",
        data={"type": "action:log", "message": "after", "level": "warning"},
    )
    r.raise_for_status()
    # The response is the tree fragment -- the new text is in it.
    assert "[warning] after" in r.text
    # And the detail page shows it after a reload.
    body = client.get("/logixng/IQupd1").text
    assert "[warning] after" in body


def test_add_child_appends_a_valid_node(client: TestClient) -> None:
    """Picker POST creates a decoder-valid default node of the chosen
    type -- no "KeyError: 'turnout'" from the strict decoders."""
    client.post("/logixng/create", data={"name": "IQadd1"}).raise_for_status()
    client.post(
        "/logixng/IQadd1/save-yaml",
        data={"yaml": _TREE_WITH_CONDITIONAL_AND_ACTION},
    ).raise_for_status()
    r = client.post(
        "/logixng/IQadd1/node/cond-tree-1/add-child",
        data={"slot": "actions", "child_type": "action:set-turnout"},
    )
    r.raise_for_status()
    # The new node appears in the returned tree fragment with the
    # default state label and an auto-generated id.
    assert "action:set-turnout" in r.text
    assert "state=CLOSED" in r.text


def test_delete_node_removes_from_tree(client: TestClient) -> None:
    client.post("/logixng/create", data={"name": "IQdel1"}).raise_for_status()
    client.post(
        "/logixng/IQdel1/save-yaml",
        data={"yaml": _TREE_WITH_CONDITIONAL_AND_ACTION},
    ).raise_for_status()
    r = client.post("/logixng/IQdel1/node/act-log-1/delete")
    r.raise_for_status()
    # Returned tree fragment no longer carries the deleted id.
    assert 'data-node-id="act-log-1"' not in r.text


def test_delete_required_single_slot_returns_error_card(
    client: TestClient,
) -> None:
    """Deleting if-then-else.condition would leave the parent invalid;
    the service rejects with 400 and the UI returns an error card
    (status 200 so htmx renders it) rather than crashing."""
    client.post("/logixng/create", data={"name": "IQdel2"}).raise_for_status()
    client.post(
        "/logixng/IQdel2/save-yaml",
        data={"yaml": (
            "type: logixng:root\nid: dr\nenabled: true\nconditionals:\n"
            "  - type: logixng:conditional\n    id: dc\n    enabled: true\n"
            "    actions:\n"
            "      - type: action:if-then-else\n        id: dite\n"
            "        condition: {type: expr:const-bool, id: dcond, value: true}\n"
            "        then: {type: action:log, id: dthen, message: y}\n"
        )},
    ).raise_for_status()
    r = client.post("/logixng/IQdel2/node/dcond/delete")
    # htmx-friendly error (card with red border + "Delete failed:").
    assert r.status_code == 200
    assert "Delete failed" in r.text
