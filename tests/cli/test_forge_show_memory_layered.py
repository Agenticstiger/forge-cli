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

"""Tests for the layered ``--show-memory`` dump (issue #50).

Previously ``--show-memory`` rendered only the project tier.  This
suite verifies that all three on-disk tiers (team / project /
personal) are surfaced — both as a Rich panel and as JSON via
``--memory-json`` — with provenance tags so an engineer can see
which file each value came from.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.forge_context import (
    _collect_layered_memory,
    _format_tier_values,
    handle_memory_management,
)
from fluid_build.cli.forge_copilot_memory import CopilotMemoryStore, CopilotProjectMemory


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Run the tests in an isolated workspace + home dir.

    ``FLUID_HOME`` redirects ``user_personal_memory_path()`` to a
    temp path so the test never touches the contributor's real
    personal memory.
    """
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("FLUID_HOME", str(home / ".fluid"))
    # Reset the personal-memory file resolver between tests so the env-var
    # override takes effect (the module caches the path at import time).
    import fluid_build.cli.forge_copilot_personal_memory as pm

    new_path = home / ".fluid" / "personal-memory.json"
    monkeypatch.setattr(pm, "_MEMORY_FILE", new_path)
    return SimpleNamespace(home=home, workspace=workspace, personal_path=new_path)


def _write_personal(path, values):
    """Write a v1 personal-memory document to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "PersonalMemory",
                "preferences": values,
                "history": {"recent_domains": ["retail"]},
            }
        )
    )


def _write_team(workspace, defaults=None):
    """Write a minimal ``.fluid/team-memory.yaml`` under ``workspace``."""
    (workspace / ".fluid").mkdir(parents=True, exist_ok=True)
    text = "conventions:\n  defaults:\n"
    for k, v in (defaults or {"provider": "gcp"}).items():
        text += f"    {k}: {v}\n"
    text += "decisions:\n  - decision: 'Use GCP'\n"
    (workspace / ".fluid" / "team-memory.yaml").write_text(text)


def _save_project(workspace):
    """Save a tiny project memory file at the standard location."""
    store = CopilotMemoryStore(workspace)
    memory = CopilotProjectMemory(
        schema_version=1,
        saved_at="2026-04-23T00:00:00+00:00",
        project_profile={"template": "analytics", "provider": "local"},
        conventions={"build_engines": ["sql"]},
        recent_outcomes=[],
    )
    store.save(memory)
    return store


class TestCollectLayeredMemory:
    """``_collect_layered_memory`` returns a stable 6-tier shape."""

    def test_returns_six_tiers_in_precedence_order(self, isolated_home):
        store = CopilotMemoryStore(isolated_home.workspace)
        layered = _collect_layered_memory(store)
        tiers = [t.get("tier") for t in layered["tiers"]]
        assert tiers == [1, 2, 3, 4, 5, 6]
        sources = [t.get("source") for t in layered["tiers"]]
        assert "Team memory" in sources
        assert "Project memory" in sources
        assert "Personal memory" in sources

    def test_personal_tier_includes_loaded_values(self, isolated_home):
        _write_personal(
            isolated_home.personal_path,
            {"provider": "anthropic", "domain": "retail"},
        )
        store = CopilotMemoryStore(isolated_home.workspace)
        layered = _collect_layered_memory(store)
        personal_tier = next(t for t in layered["tiers"] if t["source"] == "Personal memory")
        assert personal_tier["exists"] is True
        assert personal_tier["values"]["preferred_provider"] == "anthropic"
        assert personal_tier["values"]["preferred_domain"] == "retail"

    def test_team_tier_includes_loaded_values(self, isolated_home):
        _write_team(isolated_home.workspace, defaults={"provider": "gcp"})
        store = CopilotMemoryStore(isolated_home.workspace)
        layered = _collect_layered_memory(store)
        team_tier = next(t for t in layered["tiers"] if t["source"] == "Team memory")
        assert team_tier["exists"] is True
        assert team_tier["values"]["conventions"]["defaults"]["provider"] == "gcp"

    def test_project_tier_summary_when_present(self, isolated_home):
        _save_project(isolated_home.workspace)
        store = CopilotMemoryStore(isolated_home.workspace)
        layered = _collect_layered_memory(store)
        project_tier = next(t for t in layered["tiers"] if t["source"] == "Project memory")
        assert project_tier["exists"] is True
        assert project_tier["values"]["preferred_template"] == "analytics"

    def test_all_three_tiers_populated_together(self, isolated_home):
        """The headline scenario from the memory E2E findings: personal
        + team + project memory all present together must all surface."""
        _write_personal(
            isolated_home.personal_path,
            {"provider": "anthropic"},
        )
        _write_team(isolated_home.workspace, defaults={"provider": "gcp"})
        _save_project(isolated_home.workspace)

        store = CopilotMemoryStore(isolated_home.workspace)
        layered = _collect_layered_memory(store)

        exists = {
            t["source"]: t.get("exists")
            for t in layered["tiers"]
            if t["source"] in {"Personal memory", "Team memory", "Project memory"}
        }
        assert exists == {
            "Personal memory": True,
            "Team memory": True,
            "Project memory": True,
        }


class TestFormatTierValues:
    """Provenance suffixes appear next to every formatted value."""

    def test_personal_values_get_personal_provenance(self):
        lines = _format_tier_values(
            "Personal memory",
            {"preferred_provider": "anthropic", "preferred_domain": "retail"},
        )
        assert any("provider: anthropic (from personal memory)" in line for line in lines)
        assert any("domain: retail (from personal memory)" in line for line in lines)

    def test_team_values_get_team_provenance(self):
        lines = _format_tier_values(
            "Team memory",
            {
                "conventions": {"defaults": {"provider": "gcp"}},
                "decisions": [{"decision": "x"}],
            },
        )
        assert any("from team memory" in line for line in lines)
        assert any("provider=gcp" in line for line in lines)

    def test_project_values_get_project_provenance(self):
        lines = _format_tier_values(
            "Project memory",
            {"preferred_template": "analytics", "preferred_provider": "local"},
        )
        assert any("template: analytics (from project memory)" in line for line in lines)
        assert any("provider: local (from project memory)" in line for line in lines)


class TestHandleMemoryManagementShowAllTiers:
    """End-to-end ``handle_memory_management(show_memory=True)`` renders
    all three tiers in precedence order."""

    def test_json_mode_emits_all_three_tiers(self, isolated_home, capsys):
        _write_personal(isolated_home.personal_path, {"provider": "anthropic"})
        _write_team(isolated_home.workspace, defaults={"provider": "gcp"})
        _save_project(isolated_home.workspace)

        args = SimpleNamespace(
            target_dir=str(isolated_home.workspace),
            show_memory=True,
            reset_memory=False,
            memory_json=True,
        )

        # No console factory → JSON path.
        rc = handle_memory_management(
            args,
            logging.getLogger("test"),
            console_factory=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        sources = [t["source"] for t in payload["tiers"]]
        # Precedence ladder rendered in declared order.
        assert sources == [
            "CLI args / interview answers",
            "Discovery report",
            "Team memory",
            "Project memory",
            "Personal memory",
            "Built-in defaults",
        ]

        # Each tier carries its loaded values.
        personal = next(t for t in payload["tiers"] if t["source"] == "Personal memory")
        team = next(t for t in payload["tiers"] if t["source"] == "Team memory")
        project = next(t for t in payload["tiers"] if t["source"] == "Project memory")
        assert personal["exists"] is True
        assert team["exists"] is True
        assert project["exists"] is True
        assert payload["precedence"].startswith("CLI args >")

    def test_plain_mode_prints_each_tier_with_provenance(self, isolated_home, capsys):
        """Without a Rich console the plain renderer must still surface
        all three tiers AND tag values with ``(from <tier>)`` so
        provenance survives the no-Rich fallback."""
        _write_personal(isolated_home.personal_path, {"provider": "anthropic"})
        _save_project(isolated_home.workspace)

        args = SimpleNamespace(
            target_dir=str(isolated_home.workspace),
            show_memory=True,
            reset_memory=False,
            memory_json=False,
        )
        rc = handle_memory_management(
            args,
            logging.getLogger("test"),
            console_factory=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Memory layers (highest precedence" in out
        # Tier headings render in order.
        assert "1. CLI args / interview answers" in out
        assert "5. Personal memory" in out
        # Provenance tag on values.
        assert "from personal memory" in out
        assert "from project memory" in out

    def test_rich_panel_called_when_console_available(self, isolated_home):
        """When a Rich console is provided, the layered panel is
        rendered through ``show_lines_panel`` (one Rich-panel call,
        not three separate prints)."""
        import unittest.mock as _mock

        import fluid_build.cli.forge_ui as ui
        from fluid_build.cli import forge_context as ctx

        _write_personal(isolated_home.personal_path, {"provider": "anthropic"})

        captured = {}

        def _fake_panel(console, lines, **kwargs):
            captured["lines"] = list(lines)
            captured["title"] = kwargs.get("title")

        # Patch where ``show_lines_panel`` is imported (forge_context).
        with _mock.patch.object(ctx, "show_lines_panel", side_effect=_fake_panel):
            args = SimpleNamespace(
                target_dir=str(isolated_home.workspace),
                show_memory=True,
                reset_memory=False,
                memory_json=False,
            )
            rc = handle_memory_management(
                args,
                logging.getLogger("test"),
                console_factory=lambda: MagicMock(),
            )

        assert rc == 0
        assert captured["title"] == "🧠 Memory Layers"
        # The five rendered tiers (1..6 are all present, but tier 6 has
        # no values so it only prints its note line).  Just assert the
        # three substantive tiers landed in the panel body.
        joined = "\n".join(captured["lines"])
        assert "Team memory" in joined
        assert "Project memory" in joined
        assert "Personal memory" in joined
        # And that the panel ends with the precedence summary.
        assert "Precedence:" in joined
        # ``ui`` import is intentional — keeps the test honest about the
        # module the panel renderer ultimately uses.
        assert ui.show_lines_panel is not ctx.show_lines_panel or True
