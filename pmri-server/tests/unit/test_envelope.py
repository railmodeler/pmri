from __future__ import annotations

import pytest

from pmri_server.envelope import JmriRequest, normalize_request


def test_default_method_is_get() -> None:
    req = JmriRequest(type="turnout", data={"name": "IT1"})
    topic, method, data = normalize_request(req)
    assert topic == "turnout"
    assert method == "get"
    assert data == {"name": "IT1"}


def test_explicit_method_preserved() -> None:
    req = JmriRequest(type="turnout", method="put", data={"name": "IT1"})
    _, method, _ = normalize_request(req)
    assert method == "put"


def test_list_shorthand_expands() -> None:
    req = JmriRequest(type="list", list="turnout")
    topic, method, _ = normalize_request(req)
    assert topic == "turnout"
    assert method == "list"


def test_id_round_trips() -> None:
    req = JmriRequest(type="ping", id=42)
    assert req.id == 42


def test_invalid_method_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 -- pydantic ValidationError
        JmriRequest(type="ping", method="patch")  # type: ignore[arg-type]


def test_extra_field_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011
        JmriRequest(type="ping", junk="x")  # type: ignore[call-arg]
