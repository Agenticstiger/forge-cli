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

"""The two forge callers that resolve the workspace root actually reach it.

Both ``_template_mode._create_project_agent_loop`` (the ``--agent-loop``
path) and ``CopilotAgentBase.generate_project_artifacts`` (team memory)
used to import ``find_workspace_root`` from ``fluid_build.util.workspace``,
a module that does not exist. The first raised ``ModuleNotFoundError``
before the agent loop ran; the second swallowed the error, so
``.fluid/team-memory.yaml`` was never loaded and never reached the prompt.

Every test here runs from a nested directory *below* the workspace root,
so a result that matches the root can only come from the real
``fluid_build.cli.workspace_config.find_workspace_root`` walking up, not
from the ``Path.cwd()`` fallback.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

# ``forge_modes`` before ``_template_mode``: the two import each other and
# loading ``_template_mode`` cold trips the half-built module. The CLI loads
# them in this order too (see tests/cli/test_forge_apply_enrichment_flag.py).
from fluid_build.cli import forge_contract_factory, forge_copilot_agent_loop
from fluid_build.cli import forge_copilot_agent as agent_mod
from fluid_build.cli import forge_copilot_runtime as runtime_mod
from fluid_build.cli import forge_modes as _forge_modes  # noqa: F401
from fluid_build.cli._template_mode import _create_project_agent_loop
from fluid_build.cli.forge_copilot_runtime import LlmConfig
from fluid_build.cli.workspace_config import WORKSPACE_FILENAME

_TEAM_MEMORY_YAML = dedent(
    """\
    conventions:
      naming:
        product_prefix: acme
        column_style: snake_case
      defaults:
        provider: gcp
        build_engine: dbt
    decisions:
      - date: "2026-03-15"
        decision: "Use BigQuery for analytics"
        rationale: "Team has GCP expertise"
    vocabulary:
      entities:
        - customer_id
      measures:
        - total_revenue
    """
)

# What ``load_team_memory(...).to_prompt_payload()`` yields for the file
# above. Spelled out rather than recomputed so the assertion cannot agree
# with a broken loader by construction.
_EXPECTED_PAYLOAD: Dict[str, Any] = {
    "conventions": {
        "naming": {"product_prefix": "acme", "column_style": "snake_case"},
        "defaults": {"provider": "gcp", "build_engine": "dbt"},
    },
    "decisions": [
        {
            "date": "2026-03-15",
            "decision": "Use BigQuery for analytics",
            "rationale": "Team has GCP expertise",
        }
    ],
    "vocabulary": {"entities": ["customer_id"], "measures": ["total_revenue"]},
}


def _ancestor_workspace(path: Path) -> Optional[Path]:
    """Return an ancestor of *path* holding a workspace file, if any."""
    for parent in path.resolve().parents:
        if (parent / WORKSPACE_FILENAME).is_file():
            return parent
    return None


def _make_workspace(root: Path, *, team_memory: bool) -> Path:
    """Create ``root`` as a workspace and return a nested product dir in it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / WORKSPACE_FILENAME).write_text(
        "schema_version: 1\nkind: WorkspaceConfig\nworkspace:\n  name: ws\n",
        encoding="utf-8",
    )
    if team_memory:
        (root / ".fluid").mkdir()
        (root / ".fluid" / "team-memory.yaml").write_text(_TEAM_MEMORY_YAML, encoding="utf-8")
    nested = root / "products" / "orders"
    nested.mkdir(parents=True)
    return nested


# ---------------------------------------------------------------------------
# _template_mode._create_project_agent_loop  (``fluid forge --agent-loop``)
# ---------------------------------------------------------------------------


def _run_agent_loop_entry(
    monkeypatch: pytest.MonkeyPatch, target_dir: Path
) -> tuple[bool, mock.MagicMock, mock.MagicMock, object]:
    """Call the agent-loop entry with the LLM loop and the writer mocked."""
    monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    llm_config = object()
    loop = mock.MagicMock(return_value={"contract": {"id": "orders"}})
    writer = mock.MagicMock()
    monkeypatch.setattr(forge_copilot_agent_loop, "run_copilot_agent_loop", loop)
    monkeypatch.setattr(forge_contract_factory, "write_contract", writer)

    ok = _create_project_agent_loop(
        target_dir=target_dir,
        context={"project_goal": "orders"},
        copilot_options={"llm_config": llm_config},
        copilot=None,
        dry_run=False,
        logger=logging.getLogger("test_workspace_root_callers"),
        console=None,
    )
    return ok, loop, writer, llm_config


def test_agent_loop_entry_resolves_workspace_root(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    nested = _make_workspace(workspace, team_memory=False)
    monkeypatch.chdir(nested)
    target_dir = tmp_path / "out"

    ok, loop, writer, llm_config = _run_agent_loop_entry(monkeypatch, target_dir)

    assert ok is True
    loop.assert_called_once()
    kwargs = loop.call_args.kwargs
    assert kwargs["workspace_root"] == workspace.resolve()
    assert kwargs["llm_config"] is llm_config
    writer.assert_called_once()
    assert writer.call_args.args[1] == target_dir / "contract.fluid.yaml"


def test_agent_loop_entry_falls_back_to_cwd_without_workspace_file(tmp_path, monkeypatch):
    if _ancestor_workspace(tmp_path) is not None:
        pytest.skip("a workspace file above tmp_path makes the fallback untestable")
    cwd = tmp_path / "loose"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    ok, loop, _writer, _cfg = _run_agent_loop_entry(monkeypatch, tmp_path / "out")

    assert ok is True
    assert loop.call_args.kwargs["workspace_root"] == cwd.resolve()


# ---------------------------------------------------------------------------
# CopilotAgentBase.generate_project_artifacts  (team memory)
# ---------------------------------------------------------------------------


def _agent_with_captured_generation() -> tuple[agent_mod.CopilotAgentBase, mock.MagicMock]:
    agent = agent_mod.CopilotAgentBase()
    agent.prepare_runtime_inputs = mock.MagicMock(  # type: ignore[method-assign]
        return_value={
            "llm_config": "llm",
            "discovery_report": "discovery",
            "project_memory": None,
            "capability_matrix": {"providers": ["local"]},
            "capability_warnings": [],
        }
    )
    generate = mock.MagicMock(return_value="generated")
    agent._generate_copilot_artifacts_dependency = generate  # type: ignore[method-assign]
    return agent, generate


def test_team_memory_loads_from_workspace_root(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    nested = _make_workspace(workspace, team_memory=True)
    monkeypatch.chdir(nested)
    agent, generate = _agent_with_captured_generation()

    result = agent.generate_project_artifacts({"project_goal": "orders"}, {})

    assert result == "generated"
    generate.assert_called_once()
    assert generate.call_args.kwargs["team_memory"] == _EXPECTED_PAYLOAD
    assert generate.call_args.kwargs["llm_config"] == "llm"


def test_team_memory_falls_back_to_cwd_without_workspace_file(tmp_path, monkeypatch):
    if _ancestor_workspace(tmp_path) is not None:
        pytest.skip("a workspace file above tmp_path makes the fallback untestable")
    cwd = tmp_path / "loose"
    (cwd / ".fluid").mkdir(parents=True)
    (cwd / ".fluid" / "team-memory.yaml").write_text(_TEAM_MEMORY_YAML, encoding="utf-8")
    monkeypatch.chdir(cwd)
    agent, generate = _agent_with_captured_generation()

    agent.generate_project_artifacts({"project_goal": "orders"}, {})

    assert generate.call_args.kwargs["team_memory"] == _EXPECTED_PAYLOAD


def test_team_memory_is_none_without_a_team_memory_file(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    nested = _make_workspace(workspace, team_memory=False)
    monkeypatch.chdir(nested)
    agent, generate = _agent_with_captured_generation()

    agent.generate_project_artifacts({"project_goal": "orders"}, {})

    assert generate.call_args.kwargs["team_memory"] is None


def test_team_memory_failure_is_logged_and_does_not_block_generation(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    nested = _make_workspace(workspace, team_memory=True)
    monkeypatch.chdir(nested)
    agent, generate = _agent_with_captured_generation()
    log = mock.MagicMock()
    monkeypatch.setattr(agent_mod, "LOG", log)

    def _boom(_root: Path) -> None:
        raise RuntimeError("unreadable team memory")

    monkeypatch.setattr("fluid_build.cli.forge_team_memory.load_team_memory", _boom)

    result = agent.generate_project_artifacts({"project_goal": "orders"}, {})

    assert result == "generated"
    assert generate.call_args.kwargs["team_memory"] is None
    log.debug.assert_called_once_with("team_memory_load_failed", exc_info=True)


class _StopAfterPrompt(Exception):
    """Raised by the fake LLM call once the prompt has been captured."""


def test_team_memory_reaches_the_user_prompt(tmp_path, monkeypatch):
    """End to end through the real runtime: the loaded file lands in the prompt."""
    from fluid_build.cli.forge import CopilotAgent

    monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
    monkeypatch.delenv("FLUID_FORGE_LEGACY_COPILOT", raising=False)
    workspace = tmp_path / "ws"
    nested = _make_workspace(workspace, team_memory=True)
    monkeypatch.chdir(nested)
    prompts: List[str] = []

    def _fake_llm(_adapter: Any, _config: Any, _system: str, user_prompt: str) -> str:
        prompts.append(user_prompt)
        raise _StopAfterPrompt

    monkeypatch.setattr(runtime_mod, "_call_llm_with_optional_streaming", _fake_llm)
    agent = CopilotAgent()
    agent.console = None
    llm_config = LlmConfig(
        provider="openai",
        model="gpt-4o-mini",
        endpoint="https://api.openai.com/v1/chat/completions",
        api_key="sk-not-a-real-key",  # pragma: allowlist secret
    )

    with pytest.raises(_StopAfterPrompt):
        agent.generate_project_artifacts(
            {"project_goal": "Orders product", "use_case": "other"},
            {
                "llm_config": llm_config,
                "memory": False,
                "discover": False,
                "capability_matrix": {"providers": ["local", "gcp"], "templates": {"starter": {}}},
            },
        )

    assert len(prompts) == 1
    assert json.loads(prompts[0])["team_memory"] == _EXPECTED_PAYLOAD
