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
import json
from pathlib import Path

import jsonschema

from fluid_build.api.changelog import compare_contracts
from fluid_build.providers.odcs.mappers.schema import _field_to_property, _property_to_field


def test_recursive_odcs_native_shape_and_lossless_extension(contract):
    for field in contract["exposes"][0]["contract"]["schema"]:
        prop = _field_to_property(field, None, include_quality=False)
        schema_path = (
            Path(__file__).parents[2] / "fluid_build/providers/odcs/odcs-schema-v3.1.0.json"
        )
        schema = json.loads(schema_path.read_text())
        jsonschema.Draft201909Validator(
            {
                "$ref": "#/$defs/SchemaProperty",
                "$defs": schema["$defs"],
            }
        ).validate(prop)
        recovered = _property_to_field(prop)
        for key in ("type", "fields", "element", "key", "value"):
            if key in field:
                assert recovered[key] == field[key]
        if field["type"] == "struct":
            assert prop["logicalType"] == "object"
            assert prop["properties"][1]["properties"][0]["name"] == "country"
        elif field["type"] == "array":
            assert prop["items"]["logicalType"] == "object"
        elif field["type"] == "map":
            assert prop["customProperties"][0]["property"] == "fluid.recursiveType"


def _schemas(contract):
    """The output and the stream it is read from: a changed field stays readable."""
    return (
        contract["exposes"][0]["contract"]["schema"],
        contract["builds"][0]["properties"]["stream"]["schema"],
    )


def test_recursive_field_diff_in_canonical_contract(contract):
    new = copy.deepcopy(contract)
    for schema in _schemas(new):
        schema[2]["element"]["fields"][1]["type"] = "string"
    report = compare_contracts(contract, new)
    assert report.has_breaking
    assert any("items.element.fields.price.type" in change.path for change in report.breaking)


def test_nested_required_field_addition_is_breaking(contract):
    new = copy.deepcopy(contract)
    for schema in _schemas(new):
        schema[1]["fields"].append(
            {
                "name": "new_required",
                "type": "string",
                "required": True,
            }
        )
    report = compare_contracts(contract, new)
    assert report.has_breaking


def test_array_element_nullability_tightening_is_breaking(contract):
    new = copy.deepcopy(contract)
    for schema in _schemas(new):
        schema[2]["element"]["required"] = True
    report = compare_contracts(contract, new)
    assert report.has_breaking


def test_a_container_child_carrying_its_own_name_is_still_diffed_at_its_slot():
    # A `name` inside element/key/value is schema-invalid, so this goes through
    # the unvalidated diff (validate=False, for a contract still being
    # repaired); it must not drop the child or move it to another path.
    def contract(value_type):
        child = {"name": 7, "type": value_type}
        column = {"name": "tags", "type": "map", "key": {"type": "string"}, "value": child}
        return {"exposes": [{"exposeId": "e", "contract": {"schema": [column]}}]}

    report = compare_contracts(contract("int"), contract("string"), validate=False)
    assert [c.path for c in report.breaking] == ["exposes[0].contract.schema.tags.value.type"]
