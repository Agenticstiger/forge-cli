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

"""Pin invariant **I1** — authoring is interruptible.

The agent loop calls ``preview_panel.persist_artifacts()`` after every
iteration so a Ctrl-C anywhere leaves cost.json / reasoning.md /
transcript.json on disk. Recovery is just reading the files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.cli._preview_panel import PreviewPanel, new_run_id


class _StubProvider:
    """Minimal LLM provider stub: emits a tool call on iteration 0,
    a final response on iteration 1.

    Used to drive ``run_copilot_agent_loop`` without an LLM key."""

    def __init__(self, *, raise_on_iteration: int | None = None):
        self.iteration = 0
        self.raise_on_iteration = raise_on_iteration

    def call(self, *_args, **_kwargs):
        if self.iteration == self.raise_on_iteration:
            raise KeyboardInterrupt("simulated Ctrl-C")
        self.iteration += 1
        if self.iteration == 1:
            return {
                "tool_calls": [
                    {"id": "1", "name": "discover_workspace", "input": {}},
                ]
            }
        # Last iteration — return a final JSON payload
        return {
            "text": json.dumps(
                {"contract": {"id": "x.y.z"}, "suggestions": [], "additional_files": {}}
            )
        }

    def extract_tool_calls(self, response):
        return response.get("tool_calls", [])

    def extract_text_from_tool_response(self, response):
        return response.get("text", "")

    def build_tool_result_messages(self, tool_calls, results):
        return [{"role": "tool", "content": str(results)}]

    def extract_prompt_cache(self, response):
        return {}


def _drive_agent_loop(
    panel: PreviewPanel, raise_on_iteration: int | None = None, max_iterations: int = 5
):
    """Run a real agent loop against a stub provider; return the final result."""
    import unittest.mock as mock

    from fluid_build.cli import forge_copilot_agent_loop

    provider = _StubProvider(raise_on_iteration=raise_on_iteration)

    def _fake_call_llm_with_tools(adapter, llm_config, system, msgs, tools):
        return provider.call()

    def _fake_dispatch(tool_calls, *, workspace_root):
        return [{"tool_call_id": tc["id"], "result": {"ok": True}} for tc in tool_calls]

    with (
        mock.patch.object(
            forge_copilot_agent_loop, "_call_llm_with_tools", _fake_call_llm_with_tools
        ),
        mock.patch.object(forge_copilot_agent_loop, "_dispatch_tools", _fake_dispatch),
        mock.patch.object(forge_copilot_agent_loop, "get_llm_provider", lambda *_a, **_k: provider),
    ):
        return forge_copilot_agent_loop.run_copilot_agent_loop(
            context={"project_goal": "test"},
            llm_config=mock.MagicMock(provider="openai", model="gpt-4o"),
            preview_panel=panel,
            max_iterations=max_iterations,
        )


def test_agent_loop_persists_transcript_per_iteration(tmp_path):
    panel = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    panel.persist_artifacts()  # initial empty stack

    result = _drive_agent_loop(panel)
    assert result["contract"]["id"] == "x.y.z"

    # The transcript file should have grown across iterations.
    transcript = json.loads((panel.run_dir / "transcript.json").read_text())
    assert len(transcript) >= 2
    kinds = {ev["kind"] for ev in transcript}
    assert "tool_calls_dispatched" in kinds
    assert "final_response" in kinds


def test_ctrl_c_mid_loop_leaves_recoverable_state(tmp_path):
    """Simulated Ctrl-C between iterations: artifacts written so far survive."""
    panel = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    panel.persist_artifacts()

    with pytest.raises(KeyboardInterrupt):
        _drive_agent_loop(panel, raise_on_iteration=1)

    # The first iteration's tool_calls_dispatched event MUST be on disk.
    transcript_path = panel.run_dir / "transcript.json"
    assert transcript_path.exists()
    transcript = json.loads(transcript_path.read_text())
    kinds = {ev["kind"] for ev in transcript}
    assert "tool_calls_dispatched" in kinds, (
        "Ctrl-C lost the in-flight transcript; "
        "panel.persist_artifacts() must be called BEFORE LLM call returns"
    )
    # cost.json + reasoning.md exist as well (always written together).
    assert (panel.run_dir / "cost.json").exists()
    assert (panel.run_dir / "reasoning.md").exists()


def test_panel_records_tool_call_names(tmp_path):
    panel = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    panel.persist_artifacts()

    _drive_agent_loop(panel)
    assert "discover_workspace" in panel.tools_called
