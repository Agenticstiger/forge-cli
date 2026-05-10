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
seed_contract shape preserved."""

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
