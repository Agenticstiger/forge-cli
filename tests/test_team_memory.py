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

"""Tests for the three-layer memory system — team memory layer.

Covers:
- load_team_memory: loading, validation, missing file, malformed YAML
- TeamMemory: to_prompt_payload, summary_line
- scaffold_team_memory: file creation, idempotency
- Prompt integration: team memory appears in user prompt payload
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from fluid_build.cli.forge_team_memory import (
    TEAM_MEMORY_TEMPLATE,
    TeamMemory,
    load_team_memory,
    scaffold_team_memory,
)

_FULL_TEAM_MEMORY = dedent(
    """\
    conventions:
      naming:
        product_prefix: acme
        layer_convention: medallion
        column_style: snake_case
      defaults:
        provider: gcp
        build_engine: dbt
        domain: retail
        owner_team: data-platform

    decisions:
      - date: "2026-03-15"
        decision: "Use BigQuery for analytics"
        rationale: "Team has GCP expertise"
      - date: "2026-04-01"
        decision: "All PII products need sovereignty"
        rationale: "GDPR mandate"

    vocabulary:
      entities:
        - customer_id
        - order_id
      measures:
        - total_revenue
        - order_count
      dimensions:
        - order_date
        - region
"""
)

_MINIMAL_TEAM_MEMORY = dedent(
    """\
    conventions:
      defaults:
        provider: local
"""
)


class TestLoadTeamMemory:
    """load_team_memory reads and validates .fluid/team-memory.yaml."""

    def test_loads_full_file(self, tmp_path):
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "team-memory.yaml").write_text(_FULL_TEAM_MEMORY)

        tm = load_team_memory(tmp_path)
        assert tm is not None
        assert tm.naming["product_prefix"] == "acme"
        assert tm.defaults["provider"] == "gcp"
        assert len(tm.decisions) == 2
        assert tm.decisions[0]["decision"] == "Use BigQuery for analytics"
        assert tm.vocabulary_entities == ["customer_id", "order_id"]
        assert tm.vocabulary_measures == ["total_revenue", "order_count"]
        assert tm.vocabulary_dimensions == ["order_date", "region"]

    def test_loads_minimal_file(self, tmp_path):
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "team-memory.yaml").write_text(_MINIMAL_TEAM_MEMORY)

        tm = load_team_memory(tmp_path)
        assert tm is not None
        assert tm.defaults["provider"] == "local"
        assert tm.naming == {}
        assert tm.decisions == []
        assert tm.vocabulary_entities == []

    def test_returns_none_when_missing(self, tmp_path):
        assert load_team_memory(tmp_path) is None

    def test_returns_none_on_malformed_yaml(self, tmp_path):
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "team-memory.yaml").write_text("{{{{not yaml")

        tm = load_team_memory(tmp_path)
        assert tm is None

    def test_returns_none_on_non_mapping(self, tmp_path):
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "team-memory.yaml").write_text("- just a list")

        tm = load_team_memory(tmp_path)
        assert tm is None

    def test_empty_file_returns_empty_memory(self, tmp_path):
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "team-memory.yaml").write_text("")

        # yaml.safe_load("") returns None → load_team_memory returns None
        tm = load_team_memory(tmp_path)
        assert tm is None

    def test_decisions_bounded(self, tmp_path):
        decisions = [{"date": f"2026-01-{i:02d}", "decision": f"Decision {i}"} for i in range(20)]
        content = yaml.dump(
            {"decisions": decisions, "conventions": {"defaults": {"provider": "local"}}}
        )
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "team-memory.yaml").write_text(content)

        tm = load_team_memory(tmp_path)
        assert tm is not None
        assert len(tm.decisions) <= 10


class TestTeamMemoryDataclass:
    """TeamMemory serialization and summary."""

    def test_to_prompt_payload_full(self):
        tm = TeamMemory(
            naming={"product_prefix": "acme"},
            defaults={"provider": "gcp"},
            decisions=[
                {"date": "2026-01-01", "decision": "Use GCP", "rationale": "Team expertise"}
            ],
            vocabulary_entities=["customer_id"],
            vocabulary_measures=["revenue"],
            vocabulary_dimensions=["date"],
        )
        payload = tm.to_prompt_payload()
        assert "conventions" in payload
        assert payload["conventions"]["naming"]["product_prefix"] == "acme"
        assert len(payload["decisions"]) == 1
        assert payload["vocabulary"]["entities"] == ["customer_id"]

    def test_to_prompt_payload_empty(self):
        tm = TeamMemory()
        payload = tm.to_prompt_payload()
        assert payload == {}

    def test_summary_line(self):
        tm = TeamMemory(
            naming={"prefix": "x"},
            defaults={"provider": "gcp", "engine": "dbt"},
            decisions=[{"decision": "Use GCP", "date": "", "rationale": ""}],
            vocabulary_entities=["id1", "id2"],
        )
        line = tm.summary_line()
        assert "3 conventions" in line
        assert "1 decisions" in line
        assert "2 vocabulary" in line

    def test_summary_line_empty(self):
        tm = TeamMemory()
        assert tm.summary_line() == "empty"


class TestScaffoldTeamMemory:
    """scaffold_team_memory creates a template file."""

    def test_creates_file(self, tmp_path):
        path = scaffold_team_memory(tmp_path)
        assert path.exists()
        assert path.name == "team-memory.yaml"
        content = path.read_text()
        assert "conventions:" in content
        assert "decisions:" in content
        assert "vocabulary:" in content

    def test_idempotent(self, tmp_path):
        path1 = scaffold_team_memory(tmp_path)
        content1 = path1.read_text()
        # Write custom content
        path1.write_text("custom: true\n")
        # Scaffold again — should NOT overwrite
        path2 = scaffold_team_memory(tmp_path)
        assert path2.read_text() == "custom: true\n"

    def test_template_is_valid_yaml(self):
        parsed = yaml.safe_load(TEAM_MEMORY_TEMPLATE)
        assert isinstance(parsed, dict)
        assert "conventions" in parsed


class TestAutoScaffoldOnFirstForge:
    """Issue #49 fix: ``fluid forge`` in a fresh workspace auto-scaffolds
    ``.fluid/team-memory.yaml`` so engineers who never call ``fluid init``
    still discover team memory.

    Mirrors the ``git init`` pattern: re-running is safe / idempotent,
    only fills in missing template files."""

    def test_scaffold_helper_creates_file_when_absent(self, tmp_path, monkeypatch):
        """When ``.fluid/team-memory.yaml`` is absent, the helper writes
        the template and returns the new path."""
        import fluid_build.cli.forge_modes as fm

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_FORGE_NO_TEAM_MEMORY_SCAFFOLD", raising=False)

        path = fm._maybe_scaffold_team_memory_on_first_forge(console=None)
        assert path is not None
        assert path.exists()
        assert path.name == "team-memory.yaml"
        assert "conventions:" in path.read_text()

    def test_scaffold_helper_is_idempotent_when_file_exists(self, tmp_path, monkeypatch):
        """When the file already exists, the helper returns its path
        unchanged and does NOT overwrite — mirrors git init's
        ``running git init in an existing repository is safe`` rule."""
        import fluid_build.cli.forge_modes as fm

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_FORGE_NO_TEAM_MEMORY_SCAFFOLD", raising=False)
        (tmp_path / ".fluid").mkdir()
        existing = tmp_path / ".fluid" / "team-memory.yaml"
        existing.write_text("custom: preserved\n")

        path = fm._maybe_scaffold_team_memory_on_first_forge(console=None)
        assert path == existing
        # Content unchanged.
        assert existing.read_text() == "custom: preserved\n"

    def test_scaffold_helper_emits_console_hint_on_first_scaffold(self, tmp_path, monkeypatch):
        """The one-line hint must include the file path so a curious
        engineer can ``cat`` it and learn the format."""
        import fluid_build.cli.forge_modes as fm

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_FORGE_NO_TEAM_MEMORY_SCAFFOLD", raising=False)

        from unittest.mock import MagicMock

        console = MagicMock()
        path = fm._maybe_scaffold_team_memory_on_first_forge(console=console)
        assert path is not None
        console.print.assert_called_once()
        printed = console.print.call_args.args[0]
        assert "Scaffolded" in printed
        assert "team-memory.yaml" in printed

    def test_scaffold_helper_no_hint_when_file_already_exists(self, tmp_path, monkeypatch):
        """Idempotent: re-invocation must be silent (no spam on every run)."""
        import fluid_build.cli.forge_modes as fm

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FLUID_FORGE_NO_TEAM_MEMORY_SCAFFOLD", raising=False)
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "team-memory.yaml").write_text("conventions: {}\n")

        from unittest.mock import MagicMock

        console = MagicMock()
        fm._maybe_scaffold_team_memory_on_first_forge(console=console)
        console.print.assert_not_called()

    def test_scaffold_helper_skipped_when_env_var_set(self, tmp_path, monkeypatch):
        """``FLUID_FORGE_NO_TEAM_MEMORY_SCAFFOLD=1`` opts out entirely."""
        import fluid_build.cli.forge_modes as fm

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FLUID_FORGE_NO_TEAM_MEMORY_SCAFFOLD", "1")

        path = fm._maybe_scaffold_team_memory_on_first_forge(console=None)
        assert path is None
        assert not (tmp_path / ".fluid" / "team-memory.yaml").exists()


class TestTeamMemoryInPrompt:
    """Team memory appears in the user prompt payload."""

    def test_team_memory_in_user_prompt(self):
        import json

        from fluid_build.cli.forge_copilot_prompts import build_user_prompt

        team_payload = {"conventions": {"defaults": {"provider": "gcp"}}}
        prompt_str = build_user_prompt(
            context={"project_goal": "test"},
            discovery_report=_mock_discovery(),
            capability_matrix={"providers": ["local"], "templates": {}, "build_engines": ["sql"]},
            seed_contract={"fluidVersion": "0.7.2"},
            seed_template="starter",
            seed_provider="local",
            attempt_index=1,
            previous_errors=[],
            previous_payload=None,
            team_memory=team_payload,
        )
        parsed = json.loads(prompt_str)
        assert "team_memory" in parsed
        assert parsed["team_memory"]["conventions"]["defaults"]["provider"] == "gcp"

    def test_no_team_memory_when_none(self):
        import json

        from fluid_build.cli.forge_copilot_prompts import build_user_prompt

        prompt_str = build_user_prompt(
            context={"project_goal": "test"},
            discovery_report=_mock_discovery(),
            capability_matrix={"providers": ["local"], "templates": {}, "build_engines": ["sql"]},
            seed_contract={"fluidVersion": "0.7.2"},
            seed_template="starter",
            seed_provider="local",
            attempt_index=1,
            previous_errors=[],
            previous_payload=None,
            team_memory=None,
        )
        parsed = json.loads(prompt_str)
        assert "team_memory" not in parsed


class _MockDiscovery:
    """Minimal discovery report mock for prompt tests."""

    workspace_roots = ["/tmp"]
    sample_files = []
    sql_files = []
    detected_sources = []
    provider_hints = []
    existing_contracts = []
    build_constraints = []
    dbt_projects = []
    sample_data_missing = True
    files_scanned = 0
    readme_summary = ""

    def to_prompt_payload(self):
        return {"workspace_roots": self.workspace_roots}


def _mock_discovery():
    return _MockDiscovery()
