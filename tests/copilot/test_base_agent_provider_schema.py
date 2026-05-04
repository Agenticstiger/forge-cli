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

from fluid_build.copilot.agents.base import BaseStageAgent
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft


def _walk_schema_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object" or isinstance(schema.get("properties"), dict):
            yield schema
        for value in schema.values():
            yield from _walk_schema_objects(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _walk_schema_objects(item)


def test_gemini_injection_uses_json_mime_without_schema_by_default(monkeypatch):
    """Gemini's ``responseSchema`` engine has a "too many constraint
    states" cap and rejects deep enums + bounded numbers from the
    LogicalDraft schema. The default for Gemini is to ask for a
    JSON-mime response and skip the schema; the validator + repair
    loop catches mis-shaped output afterwards. Operators can opt-in to
    sending the schema via ``FLUID_GEMINI_RESPONSE_SCHEMA=1`` for
    smaller schemas where Gemini accepts it.
    """
    monkeypatch.delenv("FLUID_GEMINI_RESPONSE_SCHEMA", raising=False)
    payload = {"generationConfig": {"temperature": 0}}

    BaseStageAgent(stage="modeler", tier="deep")._inject_provider_schema(
        "gemini", payload, LogicalDraft
    )

    # Default: ``json_object`` mime, no schema. litellm forwards this
    # as ``responseMimeType: application/json`` to Gemini.
    assert payload["response_format"] == {"type": "json_object"}


def test_gemini_response_schema_is_debug_opt_in(monkeypatch):
    """When the operator sets the debug env var, send the full
    json_schema. Gemini may still reject it for very complex schemas;
    that's the documented risk of opting in."""
    monkeypatch.setenv("FLUID_GEMINI_RESPONSE_SCHEMA", "1")
    payload = {"generationConfig": {"temperature": 0}}

    BaseStageAgent(stage="modeler", tier="deep")._inject_provider_schema(
        "gemini", payload, LogicalDraft
    )

    assert "response_format" in payload
    assert payload["response_format"]["type"] == "json_schema"


def test_openai_schema_injection_is_strict_schema_compatible():
    payload = {}

    BaseStageAgent(stage="modeler", tier="deep")._inject_provider_schema(
        "openai", payload, LogicalDraft
    )

    schema = payload["response_format"]["json_schema"]["schema"]
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "default" not in str(schema)
    for obj in _walk_schema_objects(schema):
        assert obj["additionalProperties"] is False
        if isinstance(obj.get("properties"), dict):
            assert obj["required"] == list(obj["properties"].keys())
