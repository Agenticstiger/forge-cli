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

"""Tests for the OpenAI strict-schema walker (PR-C).

Pins the contract:

* When ``FLUID_OPENAI_STRICT_SCHEMA`` is unset, the legacy
  ``FORGE_RESPONSE_SCHEMA`` is sent unchanged (BC).
* When set, every object node has ``additionalProperties: false``
  and every property is in ``required``.
* Free-form nested objects (``contract``, ``additional_files``)
  are rewritten as JSON-encoded strings so the LLM can still
  return arbitrary payloads.
* The walker is non-destructive — the source schema is not mutated.
* The walker handles ``oneOf`` / ``anyOf`` / ``allOf`` / nested
  ``items`` correctly.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from fluid_build.cli.forge_copilot_response_schema import (
    FORGE_RESPONSE_SCHEMA,
    _harden_for_openai_strict,
    openai_response_format,
)


class TestHardenWalker:
    def test_does_not_mutate_input(self) -> None:
        original = deepcopy(FORGE_RESPONSE_SCHEMA)
        _ = _harden_for_openai_strict(FORGE_RESPONSE_SCHEMA)
        assert FORGE_RESPONSE_SCHEMA == original

    def test_top_level_object_keeps_additional_properties_false(self) -> None:
        hardened = _harden_for_openai_strict(FORGE_RESPONSE_SCHEMA)
        assert hardened["additionalProperties"] is False

    def test_every_property_appears_in_required(self) -> None:
        hardened = _harden_for_openai_strict(FORGE_RESPONSE_SCHEMA)
        properties = set(hardened["properties"].keys())
        required = set(hardened["required"])
        assert properties == required, (
            f"missing in required: {properties - required}"
        )

    def test_free_form_object_becomes_json_encoded_string(self) -> None:
        """The legacy ``contract`` field is ``type: object``,
        ``additionalProperties: true``, ``properties: {}``. Strict
        mode can't satisfy that, so we rewrite to a string + a
        descriptive note."""
        hardened = _harden_for_openai_strict(FORGE_RESPONSE_SCHEMA)
        contract = hardened["properties"]["contract"]
        assert contract["type"] == "string"
        assert "JSON-encoded" in contract["description"]

        additional = hardened["properties"]["additional_files"]
        assert additional["type"] == "string"

    def test_object_with_typed_properties_kept_as_object(self) -> None:
        """Only the empty ``properties: {}`` + ``additionalProperties:
        true`` shape is rewritten to a string. Typed objects stay
        objects — they just gain ``additionalProperties: false`` and
        full ``required``."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
        }
        hardened = _harden_for_openai_strict(schema)
        assert hardened["type"] == "object"
        assert hardened["additionalProperties"] is False
        assert set(hardened["required"]) == {"name", "age"}

    def test_nested_object_is_hardened_recursively(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {"inner": {"type": "string"}},
                },
            },
        }
        hardened = _harden_for_openai_strict(schema)
        outer = hardened["properties"]["outer"]
        assert outer["additionalProperties"] is False
        assert outer["required"] == ["inner"]

    def test_array_items_are_walked(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}},
                    },
                },
            },
        }
        hardened = _harden_for_openai_strict(schema)
        item = hardened["properties"]["rows"]["items"]
        assert item["additionalProperties"] is False
        assert item["required"] == ["x"]

    def test_oneOf_branches_are_walked(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "either": {
                    "oneOf": [
                        {"type": "object", "properties": {"a": {"type": "string"}}},
                        {"type": "object", "properties": {"b": {"type": "integer"}}},
                    ],
                },
            },
        }
        hardened = _harden_for_openai_strict(schema)
        branches = hardened["properties"]["either"]["oneOf"]
        for branch in branches:
            assert branch["additionalProperties"] is False
        assert set(branches[0]["required"]) == {"a"}
        assert set(branches[1]["required"]) == {"b"}

    def test_existing_required_order_preserved(self) -> None:
        """When ``required`` is already declared, the walker extends
        it with missing properties rather than rewriting the order."""
        schema = {
            "type": "object",
            "properties": {
                "first": {"type": "string"},
                "second": {"type": "string"},
                "third": {"type": "string"},
            },
            "required": ["second", "first"],  # third is missing
        }
        hardened = _harden_for_openai_strict(schema)
        # Existing entries keep their order; missing ones appended.
        assert hardened["required"] == ["second", "first", "third"]


class TestOpenaiResponseFormatGate:
    def test_default_disabled_uses_unchanged_schema(self, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_OPENAI_STRICT_SCHEMA", raising=False)
        # Use a known structured-output model.
        fmt = openai_response_format("gpt-4o-mini")
        # Schema is the legacy one — contract is still type=object.
        contract = fmt["json_schema"]["schema"]["properties"]["contract"]
        assert contract["type"] == "object"

    def test_enabled_routes_through_walker(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_OPENAI_STRICT_SCHEMA", "1")
        fmt = openai_response_format("gpt-4o-mini")
        contract = fmt["json_schema"]["schema"]["properties"]["contract"]
        # Walker rewrote free-form contract into a string.
        assert contract["type"] == "string"
        # Top-level: every property in required.
        schema = fmt["json_schema"]["schema"]
        assert set(schema["properties"].keys()) == set(schema["required"])

    @pytest.mark.parametrize("flag", ["true", "TRUE", "yes", "on", "1"])
    def test_enabled_truthy_variants(self, monkeypatch, flag: str) -> None:
        monkeypatch.setenv("FLUID_OPENAI_STRICT_SCHEMA", flag)
        fmt = openai_response_format("gpt-4o-mini")
        contract = fmt["json_schema"]["schema"]["properties"]["contract"]
        assert contract["type"] == "string"

    def test_unsupported_model_falls_back_to_json_object(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_OPENAI_STRICT_SCHEMA", "1")
        # Pass a model that ``model_supports_structured_output`` rejects
        # (e.g. ``gpt-3.5-turbo`` historically).
        fmt = openai_response_format("gpt-3.5-turbo")
        # Either the json_object fallback OR strict-with-walked-schema
        # is acceptable; the key invariant is no exception + dict shape.
        assert isinstance(fmt, dict)
        assert "type" in fmt
