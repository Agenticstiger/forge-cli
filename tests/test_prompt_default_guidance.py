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

"""Locks the system prompt to a byte-identical baseline.

``fluid_build/cli/forge_copilot_prompts.py`` loads default prompt
guidance from YAML under
``fluid_build/cli/agent_specs/_defaults/``. These tests guard three
things:

1. The default-guidance YAML files load cleanly at module import time.
2. ``_DEFAULT_GUIDANCE`` exposes the required keys so the prompt
   composer never falls back to an empty string silently.
3. ``build_system_prompt`` output matches the snapshot at
   ``tests/data/forge_system_prompt_baseline.txt`` byte-for-byte.

When a legitimate prose edit lands in one of these YAML files,
regenerate the baseline with::

    .venv/bin/python -c 'from fluid_build.cli.forge_copilot_runtime \\
      import build_system_prompt, clear_system_prompt_cache; \\
      clear_system_prompt_cache(); \\
      open("tests/data/forge_system_prompt_baseline.txt","w").write( \\
        build_system_prompt({ \\
          "providers":["local","gcp","aws","snowflake"], \\
          "templates":{"starter":{},"analytics":{},"etl_pipeline":{}, \\
                       "ml_pipeline":{},"streaming":{}}, \\
          "build_engines":["sql","python","dbt","spark","custom"]}))'

and review the diff as part of the PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_BASELINE = _REPO_ROOT / "tests" / "data" / "forge_system_prompt_baseline.txt"
_DEFAULTS_DIR = _REPO_ROOT / "fluid_build" / "cli" / "agent_specs" / "_defaults"


def _canonical_matrix() -> dict:
    """Matrix used to regenerate ``forge_system_prompt_baseline.txt``.

    Must stay in sync with the command in this module's docstring.
    """
    return {
        "providers": ["local", "gcp", "aws", "snowflake"],
        "templates": {
            "starter": {},
            "analytics": {},
            "etl_pipeline": {},
            "ml_pipeline": {},
            "streaming": {},
        },
        "build_engines": ["sql", "python", "dbt", "spark", "custom"],
    }


class TestDefaultGuidanceFiles:
    """The default-guidance YAML files must ship and parse cleanly."""

    def test_sovereignty_yaml_exists_and_loads(self):
        import yaml

        path = _DEFAULTS_DIR / "sovereignty.yaml"
        assert path.exists(), f"expected {path} to ship with the package"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert isinstance(raw.get("system_prompt"), str)
        # Catch-all: the prose must at least mention the word
        # "sovereignty" — a trivial guard against swapping the file
        # contents for something unrelated.
        assert "sovereignty" in raw["system_prompt"].lower()

    def test_agent_policy_yaml_exists_and_loads(self):
        import yaml

        path = _DEFAULTS_DIR / "agent_policy.yaml"
        assert path.exists(), f"expected {path} to ship with the package"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert isinstance(raw.get("system_prompt"), str)
        assert "agentpolicy" in raw["system_prompt"].lower().replace(" ", "")

    def test_technique_mandate_yaml_exists_and_loads(self):
        import yaml

        path = _DEFAULTS_DIR / "technique_mandate.yaml"
        assert path.exists(), f"expected {path} to ship with the package"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert isinstance(raw.get("system_prompt"), str)
        text = raw["system_prompt"].lower()
        assert "modeling technique mandate" in text
        assert "data_modeling_guidance" in text

    def test_upstream_sql_yaml_exists_and_loads(self):
        import yaml

        path = _DEFAULTS_DIR / "upstream_sql.yaml"
        assert path.exists(), f"expected {path} to ship with the package"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert isinstance(raw.get("system_prompt"), str)
        text = raw["system_prompt"].lower()
        assert "upstream transformation sql" in text
        assert "upstream_products" in text
        assert "dbt_project/models/staging" in text

    def test_clarification_yaml_exists_and_loads(self):
        import yaml

        path = _DEFAULTS_DIR / "clarification.yaml"
        assert path.exists(), f"expected {path} to ship with the package"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert isinstance(raw.get("system_prompt"), str)
        text = raw["system_prompt"].lower()
        assert "interview planner" in text
        assert "${fluid_version}" in raw["system_prompt"]
        assert "${providers}" in raw["system_prompt"]

    def test_evaluation_yaml_exists_and_loads(self):
        import yaml

        path = _DEFAULTS_DIR / "evaluation.yaml"
        assert path.exists(), f"expected {path} to ship with the package"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert isinstance(raw.get("system_prompt"), str)
        payload = json.loads(raw["system_prompt"])
        assert payload["task"].startswith("Evaluate this FLUID contract")
        assert "evaluation_criteria" in payload
        assert "response_format" in payload


class TestDefaultGuidanceLoaded:
    """``_DEFAULT_GUIDANCE`` must expose required keys non-empty."""

    def test_guidance_map_has_required_keys(self):
        from fluid_build.cli.forge_copilot_prompts import _DEFAULT_GUIDANCE

        assert "sovereignty" in _DEFAULT_GUIDANCE
        assert "agent_policy" in _DEFAULT_GUIDANCE
        assert "technique_mandate" in _DEFAULT_GUIDANCE
        assert "upstream_sql" in _DEFAULT_GUIDANCE
        assert _DEFAULT_GUIDANCE[
            "sovereignty"
        ].strip(), "sovereignty guidance must not be empty — check _defaults/sovereignty.yaml"
        assert _DEFAULT_GUIDANCE[
            "agent_policy"
        ].strip(), "agent_policy guidance must not be empty — check _defaults/agent_policy.yaml"
        assert _DEFAULT_GUIDANCE[
            "technique_mandate"
        ].strip(), (
            "technique mandate guidance must not be empty — check _defaults/technique_mandate.yaml"
        )
        assert _DEFAULT_GUIDANCE[
            "upstream_sql"
        ].strip(), "upstream SQL guidance must not be empty — check _defaults/upstream_sql.yaml"

    def test_auxiliary_prompt_map_has_required_keys(self):
        from fluid_build.cli.forge_copilot_prompts import _AUXILIARY_PROMPTS

        assert "clarification" in _AUXILIARY_PROMPTS
        assert "evaluation" in _AUXILIARY_PROMPTS
        assert _AUXILIARY_PROMPTS[
            "clarification"
        ].strip(), "clarification prompt must not be empty — check _defaults/clarification.yaml"
        assert _AUXILIARY_PROMPTS[
            "evaluation"
        ].strip(), "evaluation prompt must not be empty — check _defaults/evaluation.yaml"

    def test_auxiliary_prompt_map_is_immutable(self):
        # Defensive immutability: the map is wrapped in MappingProxyType so a
        # caller (or accidental monkey-patch) can't mutate it post-import. The
        # ``Mapping[str, str]`` annotation now matches runtime behaviour.
        from fluid_build.cli.forge_copilot_prompts import _AUXILIARY_PROMPTS

        with pytest.raises(TypeError):
            _AUXILIARY_PROMPTS["clarification"] = "tampered"  # type: ignore[index]
        with pytest.raises(TypeError):
            del _AUXILIARY_PROMPTS["evaluation"]  # type: ignore[attr-defined]


class TestAuxiliaryPromptComposition:
    """Auxiliary YAML prompt fragments must drive their prompt builders."""

    def test_clarification_prompt_renders_yaml_placeholders(self):
        from fluid_build.cli.forge_copilot_prompts import build_clarification_system_prompt

        prompt = build_clarification_system_prompt(_canonical_matrix())

        assert "${" not in prompt
        assert "FLUID" in prompt
        assert "Allowed providers: local, gcp, aws, snowflake." in prompt
        assert (
            "Known templates: analytics, etl_pipeline, ml_pipeline, starter, streaming." in prompt
        )

    def test_evaluation_prompt_uses_yaml_template(self):
        from fluid_build.cli.forge_copilot_prompts import (
            _AUXILIARY_PROMPTS,
            build_evaluation_prompt,
        )

        template_payload = json.loads(_AUXILIARY_PROMPTS["evaluation"])
        prompt = build_evaluation_prompt(
            {"project_goal": "Customer analytics", "use_case": "analytics"},
            {"id": "customer_analytics"},
        )
        payload = json.loads(prompt)

        assert payload["task"] == template_payload["task"]
        assert payload["evaluation_criteria"] == template_payload["evaluation_criteria"]
        assert payload["response_format"] == template_payload["response_format"]
        assert payload["user_requirements"]["project_goal"] == "Customer analytics"
        assert payload["contract"]["id"] == "customer_analytics"


class TestSystemPromptSnapshot:
    """The composed system prompt must match the checked-in baseline byte-for-byte."""

    @pytest.mark.xfail(
        strict=False,
        reason="needs build_runners + acquisition pattern \u2014 lands in PR-3 (runners) or later",
    )
    def test_prompt_matches_baseline(self):
        from fluid_build.cli.forge_copilot_runtime import (
            build_system_prompt,
            clear_system_prompt_cache,
        )

        if not _BASELINE.exists():
            pytest.skip(f"baseline file missing: {_BASELINE}")

        expected = _BASELINE.read_text(encoding="utf-8")

        clear_system_prompt_cache()
        actual = build_system_prompt(_canonical_matrix())

        if actual != expected:
            # Emit the first divergence so the diff is obvious without
            # dumping 13 KB of context.
            for i, (a, b) in enumerate(zip(expected, actual, strict=False)):
                if a != b:
                    context_before = expected[max(0, i - 40) : i]
                    pytest.fail(
                        f"system prompt drift at byte {i}. "
                        f"context before diff: {context_before!r}; "
                        f"expected: {expected[i : i + 40]!r}; "
                        f"got: {actual[i : i + 40]!r}. "
                        f"If the change is intentional, regenerate the baseline "
                        f"per the instructions at the top of "
                        f"tests/test_prompt_default_guidance.py."
                    )
            # One prompt is a prefix of the other.
            pytest.fail(
                f"system prompt length mismatch: expected {len(expected)} bytes, "
                f"got {len(actual)} bytes. "
                f"Extra tail: {(actual[len(expected) :] or expected[len(actual) :])[:200]!r}"
            )
