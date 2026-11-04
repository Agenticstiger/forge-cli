# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy

import pytest

from fluid_build.iac.providers.aws import _columns
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.streaming.types import parse_fields, parse_type


def test_nested_type_compiles_losslessly(contract):
    fields = contract["exposes"][0]["contract"]["schema"]
    parsed = dict(parse_fields(fields))
    assert parsed["customer"].sql() == (
        "ROW<`id` STRING, `address` ROW<`country` STRING, "
        "`geo` ROW<`location` ROW<`coordinates` ROW<`latitude` DOUBLE, `longitude` DOUBLE>>>>>"
    )
    assert parsed["items"].sql() == "ARRAY<ROW<`sku` STRING, `price` DECIMAL(18,2)>>"
    assert parsed["attributes"].sql() == "MAP<STRING NOT NULL, STRING>"
    physical = {c["name"]: c["type"] for c in _columns(fields)}
    assert physical["customer"] == (
        "struct<id:string,address:struct<country:string,"
        "geo:struct<location:struct<coordinates:struct<latitude:double,longitude:double>>>>>"
    )
    # As Glue reports a type back: lowercase, no space after a comma.
    assert physical["items"] == "array<struct<sku:string,price:decimal(18,2)>>"
    assert physical["attributes"] == "map<string,string>"


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "struct"},
        {"type": "array"},
        {"type": "map"},
        {"type": "decimal(39,2)"},
        {"type": "decimal(4,5)"},
        {"type": "decimal"},
        {"type": "ROW<id STRING>"},
        {"type": "string; DROP TABLE x"},
        {"type": "string", "fields": []},
        {"type": "int", "required": "true"},
        {"type": "map", "key": {"type": "string"}, "value": {"type": "string"}},
    ],
)
def test_unsupported_or_ambiguous_types_fail(spec):
    with pytest.raises(ValueError):
        parse_type(spec)


def test_duplicate_nested_fields_and_injection_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        parse_fields([{"name": "id", "type": "string"}, {"name": "ID", "type": "int"}])
    with pytest.raises(ValueError, match="identifier"):
        parse_fields([{"name": "id`; DROP TABLE x", "type": "string"}])


def test_depth_bounded():
    node = {"type": "string"}
    for _ in range(34):
        node = {"type": "array", "element": node}
    with pytest.raises(ValueError, match="depth"):
        parse_type(node)


def test_preview_only_and_no_default_bump(contract, tmp_path):
    manager = FluidSchemaManager(cache_dir=tmp_path)
    assert manager.latest_bundled_version() == "0.7.6"
    result = manager.validate_contract(contract, offline_only=True)
    assert result.is_valid, result.errors
    stable = copy.deepcopy(contract)
    stable["fluidVersion"] = "0.7.6"
    assert not manager.validate_contract(stable, offline_only=True).is_valid


def test_semantic_type_error_reaches_schema_gate(contract, tmp_path):
    contract["exposes"][0]["contract"]["schema"][1]["fields"] = []
    result = FluidSchemaManager(cache_dir=tmp_path).validate_contract(contract, offline_only=True)
    assert not result.is_valid


def test_legacy_scalar_emit_unchanged():
    assert _columns([{"name": "id", "type": "int"}]) == [{"name": "id", "type": "int"}]
