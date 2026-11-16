# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``exposes[].binding.properties.registry`` is typed in the 0.7.7 preview, and only there.

It is ``$defs/schemaRegistry``: the registry a produced topic's subject is registered
in. 0.7.5 and 0.7.6 keep ``binding.properties`` free-form, so their contracts and
plan digests are unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parents[1] / "fixtures" / "schema_registry" / "produced_subjects_077.yaml"
SCHEMAS = Path(__file__).parents[2] / "fluid_build" / "schemas"


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _contract() -> dict:
    return yaml.safe_load(FIXTURE.read_text())


def _validate(manager, contract):
    return manager.validate_contract(contract, offline_only=True)


def _with_registry(registry) -> dict:
    contract = _contract()
    contract["exposes"][0]["binding"]["properties"]["registry"] = registry
    return contract


def test_the_fixture_is_valid(manager):
    result = _validate(manager, _contract())
    assert result.is_valid, result.errors


@pytest.mark.parametrize(
    "registry",
    [
        {"kind": "glue"},
        {"kind": "glue", "registryName": "commerce", "subject": "orders.v1", "version": 3},
        {"kind": "confluent", "url": "http://schema-registry:8081"},
        {"kind": "confluent", "url": "https://sr.example.com", "compatibility": "FULL_TRANSITIVE"},
    ],
)
def test_accepted(manager, registry):
    result = _validate(manager, _with_registry(registry))
    assert result.is_valid, result.errors


@pytest.mark.parametrize(
    "registry",
    [
        {},
        {"kind": "kafka"},
        {"kind": "confluent"},
        {"kind": "glue", "url": "https://sr.example.com"},
        {"kind": "confluent", "url": "https://sr.example.com", "registryName": "x"},
        {"kind": "glue", "compatibility": "BACKWARD_TRANSITIVE"},
        {"kind": "confluent", "url": "https://sr.example.com", "compatibility": "FULL_ALL"},
        {"kind": "glue", "version": 0},
        {"kind": "glue", "unknown": True},
        "glue",
    ],
)
def test_refused(manager, registry):
    assert not _validate(manager, _with_registry(registry)).is_valid, registry


def test_other_binding_properties_stay_free_form(manager):
    contract = _contract()
    contract["exposes"][0]["binding"]["properties"]["cluster_by"] = ["order_id"]
    assert _validate(manager, contract).is_valid


@pytest.mark.parametrize("version", ["0.7.5", "0.7.6"])
def test_older_schemas_keep_binding_properties_untyped(version):
    schema = json.loads((SCHEMAS / f"fluid-schema-{version}.json").read_text())
    properties = schema["$defs"]["binding"]["properties"]["properties"]
    assert "properties" not in properties
    assert properties["additionalProperties"] is True


def test_the_field_is_the_shared_definition():
    schema = json.loads((SCHEMAS / "fluid-schema-0.7.7.json").read_text())
    field = schema["$defs"]["binding"]["properties"]["properties"]["properties"]["registry"]
    assert field["$ref"] == "#/$defs/schemaRegistry"


def test_validate_warns_for_a_registry_on_a_binding_that_is_no_topic():
    from fluid_build.iac.providers.confluent import validate_confluent_binding

    contract = copy.deepcopy(_contract())
    contract["exposes"][0]["binding"].update(platform="aws", format="iceberg")
    errors, warnings = validate_confluent_binding(contract)
    assert any("is ignored" in w for w in warnings)
    assert not any("exposes[0].binding.properties.registry" in e for e in errors)
