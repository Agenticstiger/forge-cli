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

"""Tests for declarative Forge domain-agent specs."""

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.forge_agent_specs import (
    AGENT_SPECS_DIR,
    AgentSpecError,
    load_agent_spec_from_path,
    load_builtin_agent_spec,
)
from fluid_build.cli.forge_agents import FinanceAgent, TelcoAgent
from fluid_build.cli.forge_domain_agent_base import DeclarativeDomainAgent


def _write_spec(tmp_path: Path, name: str, body: str) -> Path:
    spec_path = tmp_path / f"{name}.yaml"
    spec_path.write_text(dedent(body).strip() + "\n", encoding="utf-8")
    return spec_path


class TestAgentSpecLoader:
    def test_load_builtin_finance_spec(self):
        spec = load_builtin_agent_spec("finance")
        assert spec.name == "finance"
        assert spec.domain == "finance"

    def test_load_builtin_choices_are_normalized(self):
        spec = load_builtin_agent_spec("finance")
        first_choice = spec.questions[0]["choices"][0]
        assert first_choice["label"] == "Risk Analytics"
        assert first_choice["value"] == "risk_analytics"
        assert "risk" in first_choice["aliases"]

    def test_load_telco_spec(self):
        spec = load_builtin_agent_spec("telco")
        assert spec.name == "telco"
        assert spec.domain == "telco"
        assert "telecom" in spec.description.lower()

    def test_load_spec_from_yaml_only(self, tmp_path: Path):
        spec_path = _write_spec(
            tmp_path,
            "custom_agent",
            """
            name: custom
            domain: custom
            description: Custom agent
            questions:
              - key: product_type
                question: Pick one
                type: choice
                choices:
                  - label: Alpha
                    value: alpha
            suggestion_defaults:
              recommended_template: starter
              recommended_provider: local
              recommended_patterns: []
              architecture_suggestions: []
              best_practices: []
            """,
        )
        spec = load_agent_spec_from_path(spec_path)
        assert spec.name == "custom"
        assert spec.description == "Custom agent"

    def test_invalid_spec_missing_questions(self, tmp_path: Path):
        spec_path = _write_spec(
            tmp_path,
            "broken_agent",
            """
            name: broken
            domain: broken
            description: Broken agent
            suggestion_defaults:
              recommended_template: starter
              recommended_provider: local
            """,
        )
        with pytest.raises(AgentSpecError, match="questions must be a non-empty list"):
            load_agent_spec_from_path(spec_path)

    def test_invalid_rule_shape_fails_fast(self, tmp_path: Path):
        spec_path = _write_spec(
            tmp_path,
            "broken_rule",
            """
            name: broken
            domain: broken
            description: Broken rule
            questions:
              - key: mode
                question: Pick one
                type: choice
                choices:
                  - label: Alpha
                    value: alpha
            suggestion_defaults:
              recommended_template: starter
              recommended_provider: local
              recommended_patterns: []
              architecture_suggestions: []
              best_practices: []
            rules:
              - when:
                  all:
                    - field: mode
                      equals: alpha
                actions:
                  - op: explode
                    path: recommended_template
                    value: nope
            """,
        )
        with pytest.raises(AgentSpecError, match="each action must include op/path/value"):
            load_agent_spec_from_path(spec_path)


class TestDeclarativeDomainAgent:
    def test_defaults_and_ordered_rules_are_deterministic(self, tmp_path: Path):
        spec_path = _write_spec(
            tmp_path,
            "runtime_agent",
            """
            name: runtime
            domain: runtime
            description: Runtime test agent
            questions:
              - key: product_type
                question: Pick one
                type: choice
                choices:
                  - label: Alpha
                    value: alpha
                    aliases: ["alpha phrase"]
            resolver_defaults:
              product_type: alpha
            suggestion_defaults:
              recommended_template: starter
              recommended_provider: local
              recommended_patterns: []
              architecture_suggestions: []
              best_practices: []
            rules:
              - when:
                  all:
                    - field: product_type
                      equals: alpha
                actions:
                  - op: set
                    path: recommended_template
                    value: first-template
                  - op: append_unique
                    path: recommended_patterns
                    value: alpha-pattern
              - when:
                  all:
                    - field: product_type
                      equals: alpha
                actions:
                  - op: set
                    path: recommended_template
                    value: second-template
                  - op: append_unique
                    path: recommended_patterns
                    value: alpha-pattern
            """,
        )
        agent = DeclarativeDomainAgent(spec=load_agent_spec_from_path(spec_path))
        suggestions = agent.analyze_requirements({})
        assert suggestions["recommended_template"] == "second-template"
        assert suggestions["recommended_patterns"] == ["alpha-pattern"]

    def test_finance_next_step_tips_come_from_spec(self):
        agent = FinanceAgent()
        agent.console = MagicMock()
        agent._show_next_steps(
            Path("/tmp"),
            {"compliance_requirements": "sox"},
            {"recommended_provider": "gcp"},
        )
        panel = agent.console.print.call_args.args[0]
        text = str(panel.renderable)
        assert "/tmp" in text
        assert "contract.fluid.yaml" in text
        assert "fluid validate" in text

    def test_global_security_baseline_applies_to_all_agents(self):
        finance = FinanceAgent().analyze_requirements({"compliance_requirements": "none"})
        telco = TelcoAgent().analyze_requirements({})
        assert any("GDPR-aligned retention" in item for item in finance["security_requirements"])
        assert any("least-privilege RBAC" in item for item in telco["security_requirements"])

    def test_builtin_spec_directory_exists(self):
        assert AGENT_SPECS_DIR.exists()
