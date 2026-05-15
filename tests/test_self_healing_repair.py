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

"""Pin the self-healing repair loop wiring (Phase 3 #2).

Before: ``build_schema_validation_message`` existed but nothing called it.
After: post-emit JSON-schema errors get prepended to the LLM's repair
context AND a prescriptive corrective message asks for re-emit with the
seed_contract shape preserved.

Also tests BUG A1-2: additionalProperties feedback is explicit + forceful,
and the last-resort key-stripping logic (strip_additional_props_from_contract)
removes offending keys when the LLM loops on the same violation.
"""

from __future__ import annotations

from typing import List
from unittest import mock


def test_schema_validation_message_renders_path_specific_errors():
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        build_schema_validation_message,
    )

    msg = build_schema_validation_message(
        [
            "builds[0].properties.source: 'mode' is a required property",
            "consumes[0]: Additional properties are not allowed ('id' was unexpected)",
        ]
    )
    assert msg["role"] == "user"
    body = msg["content"]
    assert "builds[0].properties.source" in body
    assert "'mode' is a required property" in body
    assert "seed_contract" in body  # tells the LLM where to look
    assert "field name" in body.lower() or "enum" in body.lower()


def test_schema_validation_message_handles_empty_errors():
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        build_schema_validation_message,
    )

    msg = build_schema_validation_message([])
    assert msg["content"] == ""


def test_schema_validation_message_caps_to_thirty_errors():
    """Cap so the prompt doesn't blow up when a corrupted contract emits 100 errors."""
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        build_schema_validation_message,
    )

    errors = [f"path[{i}]: error {i}" for i in range(100)]
    msg = build_schema_validation_message(errors)
    body = msg["content"]
    # First 30 included, the rest dropped (the function bullets the first 30)
    assert "path[0]" in body
    assert "path[29]" in body
    assert "path[40]" not in body


# ---------------------------------------------------------------------------
# BUG A1-2 — additionalProperties feedback is explicit and forceful
# ---------------------------------------------------------------------------


def test_additional_props_feedback_names_exact_path_and_instructs_removal():
    """Given an additionalProperties validation error for exposes[0].semantics.policy,
    build_schema_validation_message must:
    1. Include the exact JSON path (``exposes[0].semantics``).
    2. Name the offending key (``policy``).
    3. Use the word REMOVE (or similar forceful instruction) so the LLM
       understands the key must be deleted, not renamed.
    This is the DETERMINISTIC regression test for BUG A1-2.
    """
    from fluid_build.cli.forge_copilot_corrective_feedback import build_schema_validation_message

    error = (
        "Schema validation: exposes[0].semantics: "
        "Additional properties are not allowed ('policy' was unexpected)"
    )
    msg = build_schema_validation_message([error])

    assert msg["role"] == "user"
    body = msg["content"]

    # Must name the exact JSON path so the LLM knows WHERE to look.
    assert "exposes[0].semantics" in body, (
        "Repair message must include the exact JSON path 'exposes[0].semantics' "
        f"but got: {body[:800]}"
    )

    # Must name the offending key.
    assert "policy" in body, (
        "Repair message must name the offending key 'policy' " f"but got: {body[:800]}"
    )

    # Must instruct REMOVAL, not just "fix it".
    body_lower = body.lower()
    assert "remove" in body_lower or "delete" in body_lower, (
        "Repair message must instruct the LLM to REMOVE/DELETE the key, "
        f"not just 'fix' it. Got: {body[:800]}"
    )


def test_additional_props_feedback_multi_key_violation():
    """Multiple offending keys in a single error are all named in the repair message."""
    from fluid_build.cli.forge_copilot_corrective_feedback import build_schema_validation_message

    error = (
        "Schema validation: metadata: "
        "Additional properties are not allowed ('sla', 'retention_policy' were unexpected)"
    )
    msg = build_schema_validation_message([error])
    body = msg["content"]

    assert "metadata" in body
    assert "sla" in body
    assert "retention_policy" in body
    body_lower = body.lower()
    assert "remove" in body_lower or "delete" in body_lower


def test_additional_props_feedback_root_level_violation():
    """Root-level additionalProperties violations (no dot path prefix) are handled."""
    from fluid_build.cli.forge_copilot_corrective_feedback import build_schema_validation_message

    # Raw jsonschema error with no path prefix — path_str will be "root".
    error = (
        "Schema validation: root: Additional properties are not allowed ('bogus' was unexpected)"
    )
    msg = build_schema_validation_message([error])
    body = msg["content"]

    assert "bogus" in body
    body_lower = body.lower()
    assert "remove" in body_lower or "delete" in body_lower


def test_non_additional_props_error_is_unmodified():
    """Non-additionalProperties errors still appear as plain bullets (no regression)."""
    from fluid_build.cli.forge_copilot_corrective_feedback import build_schema_validation_message

    error = "Schema validation: builds[0].properties.source: 'mode' is a required property"
    msg = build_schema_validation_message([error])
    body = msg["content"]

    assert "'mode' is a required property" in body
    # Must NOT add an erroneous REMOVE instruction for a required-property error.
    assert "REMOVE REQUIRED" not in body


def test_strip_additional_props_removes_offending_keys():
    """strip_additional_props_from_contract deletes keys named in schema errors."""
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        strip_additional_props_from_contract,
    )

    contract = {
        "fluidVersion": "0.7.3",
        "exposes": [
            {
                "semantics": {
                    "classification": "confidential",
                    "policy": "retention_3y",  # NOT in schema
                }
            }
        ],
    }
    errors = [
        "Schema validation: exposes[0].semantics: "
        "Additional properties are not allowed ('policy' was unexpected)"
    ]
    patched, stripped_log = strip_additional_props_from_contract(contract, errors)

    # Original is not mutated.
    assert contract["exposes"][0]["semantics"]["policy"] == "retention_3y"

    # Patched has the key removed.
    assert "policy" not in patched["exposes"][0]["semantics"]
    assert patched["exposes"][0]["semantics"]["classification"] == "confidential"

    # Log lists the removed key.
    assert any("policy" in entry for entry in stripped_log)


def test_strip_additional_props_skips_non_parseable_errors():
    """strip_additional_props_from_contract silently skips errors it cannot parse."""
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        strip_additional_props_from_contract,
    )

    contract = {"exposes": [{"name": "x"}]}
    errors = ["Schema validation: builds[0]: 'mode' is a required property"]
    patched, stripped_log = strip_additional_props_from_contract(contract, errors)

    # Nothing removed — required-property errors are not our business.
    assert patched == contract
    assert stripped_log == []


def test_strip_additional_props_handles_missing_path():
    """strip_additional_props_from_contract is safe when the JSON path doesn't exist."""
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        strip_additional_props_from_contract,
    )

    contract = {"metadata": {"owner": {"team": "finance"}}}
    errors = [
        # Path exposes[5].semantics doesn't exist — should not crash.
        "Schema validation: exposes[5].semantics: "
        "Additional properties are not allowed ('policy' was unexpected)"
    ]
    patched, stripped_log = strip_additional_props_from_contract(contract, errors)
    assert patched == contract
    assert stripped_log == []


def test_self_healing_runs_schema_validator_in_repair_loop():
    """Mock the LLM to emit a contract missing required fields; assert
    the repair loop gets the schema errors prepended to repair context."""
    from fluid_build.cli import forge_copilot_runtime as rt
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    bad_contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "x.y.z",
        "name": "test",
        "domain": "x",
        "metadata": {"layer": "Bronze", "productType": "SDP", "owner": {"team": "d"}},
        # MISSING: builds, exposes — schema requires both
    }

    def _stub_llm(*_args, **_kwargs):
        # Emit the same bad contract every time
        return (
            '{"contract": '
            + __import__("json").dumps(bad_contract)
            + ', "suggestions": [], "additional_files": {}}'
        )

    captured_user_prompts: List[str] = []
    real_build_user_prompt = rt.build_user_prompt

    def _spy_user_prompt(**kwargs):
        prompt = real_build_user_prompt(**kwargs)
        captured_user_prompts.append(prompt)
        return prompt

    class _DiscoveryStub:
        sample_files = []
        sql_files = []
        user_data_models = []
        detected_sources = []
        provider_hints = []
        templates = []
        warnings = []
        notes = []

        def to_prompt_payload(self):
            return {}

    import os

    with (
        mock.patch.dict(os.environ, {"FLUID_FORGE_LEGACY_COPILOT": "1"}, clear=False),
        mock.patch.object(rt, "_call_llm_with_optional_streaming", _stub_llm),
        mock.patch.object(rt, "build_user_prompt", _spy_user_prompt),
    ):
        try:
            rt.generate_copilot_artifacts(
                context={"project_goal": "x"},
                llm_config=LlmConfig(
                    provider="openai",
                    model="gpt-4o",
                    endpoint="https://api.openai.com/v1/chat/completions",
                    api_key="test",
                ),
                discovery_report=_DiscoveryStub(),
                max_attempts=2,
            )
        except Exception as exc:
            # Expected — bad contract never validates; the function raises
            # CopilotGenerationError after exhausting attempts.
            import traceback as _tb

            print(f"[debug] caught {type(exc).__name__}: {exc}")
            _tb.print_exc()

    # The 2nd attempt's prompt MUST mention the schema errors that the
    # 1st attempt's output produced. That's the whole point of wiring.
    assert (
        len(captured_user_prompts) >= 2
    ), f"Expected >= 2 attempts, got {len(captured_user_prompts)}"
    second_attempt_prompt = captured_user_prompts[1]
    # The schema validator flags missing 'builds' and 'exposes' (required).
    assert (
        "schema validation" in second_attempt_prompt.lower()
        or "required property" in second_attempt_prompt.lower()
        or "schema error" in second_attempt_prompt.lower()
    ), (
        "Self-healing must inject schema validation errors into the "
        f"next attempt's repair context. Got prompt: {second_attempt_prompt[:600]}"
    )


def test_last_resort_strip_saves_contract_on_pure_additional_props_failure():
    """When the LLM loops on a pure additionalProperties violation for all 3 attempts,
    the last-resort strip must remove the offending key and return a valid contract
    instead of raising CopilotGenerationError.

    This is a DETERMINISTIC end-to-end test for BUG A1-2 (no live LLM).
    validate_generated_result and the schema validator are both mocked so only
    the additionalProperties schema error appears — mimicking the scenario where
    the LLM's contract is structurally sound except for one forbidden key.
    """
    import json as _json
    import os

    from fluid_build.cli import forge_copilot_runtime as rt
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError, LlmConfig

    # A contract that is schema-valid EXCEPT for 'policy' in exposes[0].semantics.
    good_contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "finance.rwa.adp",
        "name": "RWA ADP",
        "domain": "finance",
        "description": "Basel-III RWA analytics",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "regulatory-reporting"},
        },
        "exposes": [
            {
                "exposeId": "rwa_metrics",
                "kind": "table",
                "binding": {"platform": "local", "format": "other", "location": {}},
                "contract": {"schema": []},
                "semantics": {
                    "name": "RWA Metrics",
                    # 'policy' will be injected below — this is the forbidden key.
                    # 'policy' IS a valid top-level expose field but NOT inside
                    # semantics, which is where the LLM tends to put it.
                },
            }
        ],
        "builds": [
            {
                "id": "transform_rwa",
                "engine": "dbt",
                "pattern": "hybrid-reference",
                "properties": {"model": "transform_rwa"},
            }
        ],
    }
    # Insert the forbidden key
    contract_with_extra = _json.loads(_json.dumps(good_contract))
    contract_with_extra["exposes"][0]["semantics"]["policy"] = "retention_3y"

    def _stub_llm(*_args, **_kwargs):
        # Always emit the same contract with the forbidden key — simulates an
        # LLM that ignores the repair message.
        return (
            '{"contract": '
            + _json.dumps(contract_with_extra)
            + ', "suggestions": {}, "readme_markdown": "", "additional_files": {}}'
        )

    # Mock validate_generated_result to return NO non-schema errors —
    # only the additionalProperties violation comes from the schema validator.
    # This isolates the last-resort strip from unrelated validate_generated_result checks.
    _schema_error = (
        "exposes[0].semantics: Additional properties are not allowed ('policy' was unexpected)"
    )

    def _stub_validate(normalized, **kwargs):
        contract = (normalized or {}).get("contract") or {}
        semantics = (contract.get("exposes") or [{}])[0].get("semantics") or {}
        if "policy" in semantics:
            # Return the same additionalProperties error as the real schema validator
            return ([_schema_error], [])
        return ([], [])

    # Also mock the FluidSchemaManager call inside generate_copilot_artifacts
    # so the schema-prepend logic fires correctly.
    class _MockSchemaResult:
        def __init__(self, has_error):
            self.is_valid = not has_error
            self.errors = [_schema_error] if has_error else []

    def _stub_schema_manager_validate(self_sm, contract):
        semantics = (contract.get("exposes") or [{}])[0].get("semantics") or {}
        return _MockSchemaResult("policy" in semantics)

    class _DiscoveryStub:
        sample_files = []
        sql_files = []
        user_data_models = []
        detected_sources = []
        provider_hints = []
        templates = []
        warnings = []
        notes = []

        def to_prompt_payload(self):
            return {}

    from fluid_build import schema_manager as _sm_module

    with (
        mock.patch.dict(os.environ, {"FLUID_FORGE_LEGACY_COPILOT": "1"}, clear=False),
        mock.patch.object(rt, "_call_llm_with_optional_streaming", _stub_llm),
        mock.patch.object(rt, "_self_evaluate_contract", return_value=None),
        mock.patch.object(rt, "validate_generated_result", _stub_validate),
        mock.patch.object(
            _sm_module.FluidSchemaManager, "validate_contract", _stub_schema_manager_validate
        ),
    ):
        result = None
        exc_raised = None
        try:
            result = rt.generate_copilot_artifacts(
                context={"project_goal": "Basel-III RWA ADP"},
                llm_config=LlmConfig(
                    provider="openai",
                    model="gpt-4o",
                    endpoint="https://api.openai.com/v1/chat/completions",
                    api_key="test",
                ),
                discovery_report=_DiscoveryStub(),
                max_attempts=3,
            )
        except CopilotGenerationError as exc:
            exc_raised = exc

    # The last-resort strip should have kicked in on attempt 3 and returned a
    # valid contract (the forbidden 'policy' key removed) instead of raising.
    assert exc_raised is None, (
        f"CopilotGenerationError raised even though last-resort strip should have "
        f"saved the run. Error: {exc_raised}"
    )
    assert result is not None, "Expected a CopilotGenerationResult, got None"
    final_contract = result.contract
    assert "policy" not in final_contract.get("exposes", [{}])[0].get(
        "semantics", {}
    ), "The forbidden 'policy' key must have been stripped from exposes[0].semantics"
    # Verify provenance records the strip repair.
    assert result.provenance is not None
    assert result.provenance.get("strip_repair") is True
