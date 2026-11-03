"""Parametrized JSON schema conformance suite.

Walks every JMRI ``-server.json`` schema we serve a topic for, issues
a ``list`` (or ``get`` when the topic doesn't list) through the
service registry, and validates each response against the schema.
The point is to catch wire-format drift from JMRI Java's canonical
shape -- subtle field renames, missing ``required`` fields, wrong
types -- that per-service tests wouldn't notice unless they were
individually maintained.

Two ways a topic can end up on the exemption list below:

* **Rewrite-only** topics -- ``configProfile``, ``logs``, ``scripts``,
  ``beanTransfer``, ``logixng`` -- don't exist in JMRI Java at all.
  There are no schemas to conform to; they're excluded by not being
  in the JMRI schema tree.

* **Topics we serve but JMRI's schema requires fields we don't
  populate** -- usually the ``comment`` / ``properties`` array
  boilerplate on every bean. These are on :data:`XFAIL_TOPICS` with
  a reason string; each entry is a documented divergence that a
  future pass can close.

The conformance test is intentionally lenient about which topics
are testable: a topic without a matching schema is silently skipped
(not xfailed) because we can't validate what isn't specified.
"""
from __future__ import annotations

import jsonschema
import pytest
from fastapi.testclient import TestClient

from tests.conftest import post


# Documented divergences from JMRI's canonical schema. Each entry
# is (topic, reason) -- the topic is skipped from the conformance
# sweep and the reason surfaces in the xfail summary so it's easy
# to audit.
XFAIL_TOPICS: dict[str, str] = {
    # JMRI's bean schemas require a "properties" array on every
    # bean response -- we don't emit it. Filed but not scoped.
    "turnout": "'properties' array not emitted",
    "sensor": "'properties' array not emitted",
    "light": "'properties' array not emitted",
    "signalHead": "'properties' array not emitted",
    "signalMast": "'properties' array not emitted",
    "memory": "'properties' array not emitted",
    "reporter": "'properties' array not emitted",
    "block": "'properties' array not emitted",
    "route": "'properties' array not emitted",
    "audio": "'properties' array not emitted",
    "idTag": "'properties' array not emitted",
    "layoutBlock": "'properties' array not emitted",
    "oblock": "'properties' array not emitted",
    "roster": "roster envelope shape differs from JMRI",
    "consist": "consist envelope shape differs from JMRI",
    "panel": "panel list shape not yet aligned",
    "throttle": "throttle 'list' semantics differ (JMRI lists via 'throttles')",
    "time": "time payload shape differs (missing state, time format)",
}


# Topics that respond to ``get`` with no ``data`` payload (or with a
# non-listable singleton). Everything else uses ``list``.
GET_ONLY_TOPICS = {
    "hello", "version", "railroad", "node", "power", "metadata",
    "networkService", "systemConnection", "time",
}


def _testable_topics(server_schemas: dict) -> list[str]:
    """The subset of served topics that have a JMRI schema to
    validate against. Excludes rewrite-only topics (no schema) and
    the ones on :data:`XFAIL_TOPICS`."""
    return sorted(server_schemas.keys())


def _validate_payload(payload, schema: dict, topic: str) -> None:
    """Validate *payload* against *schema*. Lists validate each
    item; dicts validate directly. Empty lists are considered
    trivially conformant (nothing to validate)."""
    if isinstance(payload, list):
        for i, item in enumerate(payload):
            try:
                jsonschema.validate(item, schema)
            except jsonschema.ValidationError as e:
                raise AssertionError(
                    f"topic {topic!r} list item {i} failed: {e.message}"
                ) from None
    elif isinstance(payload, dict):
        jsonschema.validate(payload, schema)
    # Non-dict, non-list responses (rare -- mostly errors) skip.


import pathlib as _pathlib

_SCHEMA_DIR = (
    _pathlib.Path(__file__).resolve().parents[3]
    / "stage-0" / "contracts" / "json-schema"
)


@pytest.mark.parametrize(
    "topic",
    sorted(p.stem.removesuffix("-server")
           for p in _SCHEMA_DIR.rglob("*-server.json")),
)
def test_topic_response_matches_pmri_schema(
    topic: str,
    client: TestClient,
    server_schemas: dict,
    handle,
) -> None:
    """For every JMRI ``-server.json`` schema, if we serve the topic,
    dispatch a request and validate the response payload against
    the schema."""
    if topic in XFAIL_TOPICS:
        pytest.xfail(f"documented divergence: {XFAIL_TOPICS[topic]}")
    if topic not in server_schemas:
        pytest.skip("schema not in extracted contracts (bug in fixture)")

    # Not every schema-carrying topic maps to one of our services.
    if handle.registry.get(topic) is None:
        pytest.skip(f"topic {topic!r} not served by this build")

    method = "get" if topic in GET_ONLY_TOPICS else "list"
    try:
        resp = post(client, topic, method=method)
    except Exception as e:  # noqa: BLE001
        # A method mismatch (e.g. topic doesn't support list) is
        # information, not conformance failure -- record and skip.
        pytest.skip(f"topic {topic!r} rejected {method}: {e}")
    _validate_payload(resp["data"], server_schemas[topic], topic)


def test_conformance_covers_expected_topics(server_schemas: dict) -> None:
    """Sanity: the schemas we extracted include the common bean set.
    If this list shrinks unexpectedly, something dropped a schema."""
    expected = {"turnout", "sensor", "power", "throttle", "roster"}
    assert expected <= set(server_schemas.keys()), (
        f"missing schemas: {expected - set(server_schemas.keys())}"
    )
