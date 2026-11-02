"""Pytest fixtures shared by Stage 0+ conformance tests.

These tests run against the **contracts** in ``stage-0/contracts/``. Stage 1's
``jmri_core`` package will be added to the path here once it exists; until then
the tests in this directory only verify the contracts themselves are intact.
"""
from __future__ import annotations

import json
import pathlib

import pytest

STAGE_0 = pathlib.Path(__file__).resolve().parents[2]
CONTRACTS = STAGE_0 / "contracts"


@pytest.fixture(scope="session")
def json_schema_dir() -> pathlib.Path:
    return CONTRACTS / "json-schema"


@pytest.fixture(scope="session")
def xml_schema_dir() -> pathlib.Path:
    return CONTRACTS / "xml" / "schema"


@pytest.fixture(scope="session")
def golden_xml_dir() -> pathlib.Path:
    return CONTRACTS / "xml" / "golden"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize tests over every contract file by glob.

    Tests can opt in by declaring a parameter whose name matches a key below.
    """
    if "json_schema_file" in metafunc.fixturenames:
        files = sorted((CONTRACTS / "json-schema").rglob("*.json"))
        metafunc.parametrize(
            "json_schema_file",
            files,
            ids=[f.relative_to(CONTRACTS / "json-schema").as_posix() for f in files],
        )
    if "xml_schema_file" in metafunc.fixturenames:
        files = sorted((CONTRACTS / "xml" / "schema").glob("*.xsd"))
        metafunc.parametrize(
            "xml_schema_file",
            files,
            ids=[f.name for f in files],
        )
    if "golden_xml_file" in metafunc.fixturenames:
        files = sorted((CONTRACTS / "xml" / "golden").rglob("*.xml"))
        metafunc.parametrize(
            "golden_xml_file",
            files,
            ids=[f.relative_to(CONTRACTS / "xml" / "golden").as_posix() for f in files],
        )


@pytest.fixture()
def load_json_schema(json_schema_file: pathlib.Path) -> dict:
    return json.loads(json_schema_file.read_text(encoding="utf-8"))
