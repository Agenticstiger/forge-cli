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

"""Tests for Bring Your Own Domain Agent (BYODA).

Verifies that users can define custom domain agents by dropping YAML
files in .fluid/agents/ and have them discovered, loaded, and used
transparently by the forge copilot.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from fluid_build.cli.forge_agent_specs import (
    AgentSpec,
    AgentSpecError,
    discover_all_agent_specs,
    load_agent_spec_from_path,
    load_user_or_builtin_spec,
    scaffold_user_agent,
)
from fluid_build.cli.forge_agents import get_agent, get_all_domain_names, list_agents

_MINIMAL_AGENT_YAML = dedent(
    """\
    name: insurance
    domain: Insurance
    description: Expert in insurance data products and actuarial analytics
    keywords:
      - insurance
      - claims
      - underwriting
      - actuarial

    questions:
      - key: product_type
        question: What type of insurance data product?
        type: choice
        required: true
        choices:
          - label: Claims Analytics
            value: claims_analytics
            aliases: ["claims"]
          - label: Underwriting
            value: underwriting

    resolver_defaults:
      product_type: claims_analytics

    suggestion_defaults:
      recommended_template: analytics
      recommended_provider: local
      recommended_patterns: []
      architecture_suggestions:
        - Use claims fact tables with policy dimensions
      best_practices:
        - Implement claims fraud detection patterns
      technology_stack: []
      security_requirements: []

    rules: []
    next_step_tips: []
    conditional_next_step_tips: []
"""
)


class TestAgentSpecKeywords:
    """The keywords field is loaded from agent YAML specs."""

    def test_keywords_parsed_from_yaml(self, tmp_path):
        (tmp_path / "insurance.yaml").write_text(_MINIMAL_AGENT_YAML)
        spec = load_agent_spec_from_path(tmp_path / "insurance.yaml")
        assert spec.keywords == ["insurance", "claims", "underwriting", "actuarial"]

    def test_keywords_default_empty(self, tmp_path):
        yaml_without_keywords = _MINIMAL_AGENT_YAML.replace(
            "keywords:\n  - insurance\n  - claims\n  - underwriting\n  - actuarial\n\n",
            "",
        )
        (tmp_path / "no_kw.yaml").write_text(yaml_without_keywords)
        spec = load_agent_spec_from_path(tmp_path / "no_kw.yaml")
        assert spec.keywords == []


class TestUserSpecDiscovery:
    """User-defined specs in .fluid/agents/ are discovered."""

    def test_discover_from_workspace(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agents_dir = tmp_path / ".fluid" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "insurance.yaml").write_text(_MINIMAL_AGENT_YAML)

        specs = discover_all_agent_specs()
        assert "insurance" in specs
        assert specs["insurance"].domain == "Insurance"

    def test_user_spec_shadows_builtin(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agents_dir = tmp_path / ".fluid" / "agents"
        agents_dir.mkdir(parents=True)

        custom_finance = _MINIMAL_AGENT_YAML.replace("insurance", "finance").replace(
            "Insurance", "Custom Finance"
        )
        (agents_dir / "finance.yaml").write_text(custom_finance)

        spec = load_user_or_builtin_spec("finance")
        assert spec.domain == "Custom Finance"

    def test_builtin_fallback(self):
        spec = load_user_or_builtin_spec("finance")
        assert spec.name == "finance"

    def test_unknown_spec_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(AgentSpecError):
            load_user_or_builtin_spec("nonexistent_xyz")

    def test_invalid_user_spec_skipped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agents_dir = tmp_path / ".fluid" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "bad.yaml").write_text("not: valid\nagent: spec")

        specs = discover_all_agent_specs()
        assert "bad" not in specs


class TestScaffoldUserAgent:
    """scaffold_user_agent creates a spec from the template."""

    def test_creates_file(self, tmp_path):
        path = scaffold_user_agent("insurance", target_dir=tmp_path)
        assert path.exists()
        assert path.name == "insurance.yaml"
        assert ".fluid/agents/" in str(path)

    def test_substitutes_name(self, tmp_path):
        path = scaffold_user_agent("energy", target_dir=tmp_path)
        content = path.read_text()
        assert "name: energy" in content
        assert "domain: Energy" in content

    def test_valid_spec(self, tmp_path):
        path = scaffold_user_agent("logistics", target_dir=tmp_path)
        spec = load_agent_spec_from_path(path)
        assert spec.name == "logistics"


class TestDynamicAgentRegistry:
    """get_agent and list_agents include user-defined agents."""

    def test_get_agent_loads_user_spec(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agents_dir = tmp_path / ".fluid" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "insurance.yaml").write_text(_MINIMAL_AGENT_YAML)

        agent = get_agent("insurance")
        assert agent.name == "insurance"
        assert agent.domain == "Insurance"

    def test_get_all_domain_names_includes_user(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agents_dir = tmp_path / ".fluid" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "insurance.yaml").write_text(_MINIMAL_AGENT_YAML)

        names = get_all_domain_names()
        assert "insurance" in names
        assert "finance" in names  # built-in still present

    def test_list_agents_shows_source(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agents_dir = tmp_path / ".fluid" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "insurance.yaml").write_text(_MINIMAL_AGENT_YAML)

        agents = list_agents()
        insurance = [a for a in agents if a["name"] == "insurance"]
        assert len(insurance) == 1
        assert insurance[0]["source"] == "user"

        finance = [a for a in agents if a["name"] == "finance"]
        assert len(finance) == 1
        assert finance[0]["source"] == "built-in"


class TestKeywordAutoDetection:
    """User agent keywords are merged into auto-detection."""

    def test_user_keywords_merged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agents_dir = tmp_path / ".fluid" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "insurance.yaml").write_text(_MINIMAL_AGENT_YAML)

        # Clear the LRU cache so fresh discovery runs.
        from fluid_build.cli.forge_domain_enrichment import _load_domain_keywords

        _load_domain_keywords.cache_clear()

        keywords_map, min_hits = _load_domain_keywords()
        assert "insurance" in keywords_map
        assert "claims" in keywords_map["insurance"]

        # Clean up cache for other tests.
        _load_domain_keywords.cache_clear()
