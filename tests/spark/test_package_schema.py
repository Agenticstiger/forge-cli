# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Schema pins for ``builds[].properties.package`` (0.7.7 preview): a Spark build that runs a
prebuilt PySpark package by its sha256 digest, while forge's runner owns the write."""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build.schema_manager import FluidSchemaManager

from .package_fixtures import PACKAGE_CONTRACT, SHA

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _errors(manager: FluidSchemaManager, contract: Dict[str, Any]) -> List[str]:
    result = manager.validate_contract(contract, schema_version="0.7.7", offline_only=True)
    return [getattr(e, "message", str(e)) for e in result.errors]


def _contract(**package: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    contract["builds"][0]["properties"]["package"].update(package)
    return contract


def _properties(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract["builds"][0]["properties"]


def test_a_package_build_validates(manager):
    assert _errors(manager, copy.deepcopy(PACKAGE_CONTRACT)) == []


def test_the_smallest_package_build_validates(manager):
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    props = _properties(contract)
    props.pop("language")
    props["package"] = {
        "uri": props["package"]["uri"],
        "sha256": SHA,
        "entryPoint": "orders_pkg.jobs:merge_orders",
    }
    props["parameters"]["spark"].pop("mode")
    assert _errors(manager, contract) == []


def test_a_local_package_is_a_file_uri(manager):
    contract = _contract(uri="file:///home/dev/orders/dist/orders_pkg-1.4.0.zip")
    assert _errors(manager, contract) == []


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/orders_pkg.whl",  # only s3:// or file:///
        "s3://commerce-artifacts/code/orders/orders_pkg.tar.gz",  # a wheel or a zip
        "s3://commerce-artifacts/",  # no object
        "file://host/orders_pkg.whl",  # a file URI names no host
        "s3://commerce-artifacts/code/../orders_pkg.whl",  # no dot segments
        "s3://commerce-artifacts/code/orders pkg.whl",  # no whitespace
        "s3://commerce-artifacts/code/orders_pkg.whl\n",
    ],
)
def test_a_package_uri_names_one_wheel_or_zip(manager, uri):
    assert _errors(manager, _contract(uri=uri))


@pytest.mark.parametrize(
    "sha",
    [SHA.upper(), SHA[:-1], "sha256:" + SHA, SHA + "\n", ""],
)
def test_the_digest_is_64_lowercase_hex(manager, sha):
    assert _errors(manager, _contract(sha256=sha))


@pytest.mark.parametrize(
    "entry",
    [
        "orders_pkg.jobs",  # a module alone runs nothing
        "orders_pkg.jobs:",
        ":merge_orders",
        "orders-pkg.jobs:merge_orders",  # each part is a Python identifier
        "orders_pkg.jobs:merge orders",
        "orders_pkg..jobs:merge_orders",
        "orders_pkg.jobs:merge_orders\n",
    ],
)
def test_the_entry_point_is_an_object_reference(manager, entry):
    assert _errors(manager, _contract(entryPoint=entry))


def test_a_dotted_attribute_is_an_object_reference(manager):
    assert _errors(manager, _contract(entryPoint="orders_pkg.jobs:Jobs.merge_orders")) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"window": 1},  # strings only
        {"bad key": "x"},
        {f"k{i}": "v" for i in range(65)},
        {"window": "x" * 4097},
    ],
)
def test_arguments_are_a_small_map_of_strings(manager, arguments):
    assert _errors(manager, _contract(arguments=arguments))


def test_an_unknown_package_key_is_refused(manager):
    assert _errors(manager, _contract(entrypoint="orders_pkg.jobs:merge_orders"))


@pytest.mark.parametrize("required", ["uri", "sha256", "entryPoint"])
def test_uri_digest_and_entry_point_are_required(manager, required):
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    _properties(contract)["package"].pop(required)
    assert _errors(manager, contract)


@pytest.mark.parametrize(
    "dead",
    [
        {"table": "orders dead"},
        {"maxRecordsBeforeAbort": -1},
        {"enabled": "yes"},
        {"sink": "topic", "topic": "x"},  # a batch build's dead letters are a table
    ],
)
def test_the_dead_letters_are_a_table(manager, dead):
    assert _errors(manager, _contract(deadLetter=dead))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sql", "SELECT 1"),
        ("mapping", {"columns": []}),
        ("stream", {"kind": "kafka", "topic": "t", "format": "json", "schema": []}),
    ],
)
def test_a_package_replaces_sql_mapping_and_stream(manager, key, value):
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    _properties(contract)[key] = value
    assert _errors(manager, contract)


def test_a_package_build_is_a_batch_spark_build(manager):
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    _properties(contract)["parameters"]["spark"]["mode"] = "streaming"
    assert _errors(manager, contract)
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    _properties(contract)["parameters"].pop("spark")
    assert _errors(manager, contract)


def test_a_package_runs_pyspark(manager):
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    _properties(contract)["language"] = "scala"
    assert _errors(manager, contract)


def test_an_embedded_logic_build_without_package_still_needs_sql(manager):
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    _properties(contract).pop("package")
    assert any("sql" in e for e in _errors(manager, contract))


def test_the_package_is_a_preview_field(manager):
    """0.7.6 refuses the key: the field exists from the 0.7.7 preview on."""
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    contract["fluidVersion"] = "0.7.6"
    result = manager.validate_contract(contract, schema_version="0.7.6", offline_only=True)
    assert not result.is_valid
