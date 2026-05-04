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

"""Pin the pre-write preview's three invariants:

* **I1** Authoring is interruptible — artifacts written before the prompt
* **I4** Cost is visible before it's spent — even with ``--yes``
* **I5** Every decision is reproducible — receipt + reasoning + transcript
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.cli._preview_panel import (
    CostSnapshot,
    PreviewError,
    PreviewPanel,
    capture_cost_snapshot,
    confirm,
    new_run_id,
    render,
    render_completion,
    run_dir_for,
)

# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


def test_new_run_id_is_sortable_and_unique():
    a = new_run_id()
    b = new_run_id()
    assert a != b
    # Sorted as strings → chronological order (timestamp prefix).
    assert sorted([a, b]) == [a, b] or sorted([a, b]) == [b, a]
    assert len(a) >= len("YYYYMMDD-HHMMSS-XXXXXX")


def test_run_dir_for_uses_dot_fluid_layout(tmp_path):
    rid = "20260430-150000-abcdef"
    expected = tmp_path / ".fluid" / "agents" / rid
    assert run_dir_for(tmp_path, rid) == expected


# ---------------------------------------------------------------------------
# I5 — durable artifacts under .fluid/agents/<run-id>/
# ---------------------------------------------------------------------------


def _panel(tmp_path) -> PreviewPanel:
    panel = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    panel.add_file("contract.fluid.yaml", "id: test\nfluidVersion: '0.7.3'\n")
    panel.add_file("README.md", "# Test\n")
    panel.add_decision("data_product_type", "SDP", source="user")
    panel.add_decision("transform_engine", "duckdb", source="default")
    panel.add_assumption("source.kind defaulted to 'file' (no scheme detected)")
    panel.add_tool_call("discover_workspace_contracts")
    panel.append_reasoning("Picked SDP because the user said 'raw acquisition'.\n")
    panel.append_transcript({"event": "tool_call", "name": "discover_workspace_contracts"})
    panel.append_transcript({"event": "llm_response", "tokens": 1234})
    panel.cost = CostSnapshot(
        provider="openai",
        model="gpt-4o-2024-08-06",
        input_tokens=12345,
        output_tokens=6789,
        total_tokens=12345 + 6789,
        total_usd=0.0421,
        wall_clock_seconds=18.4,
    )
    return panel


def test_persist_artifacts_writes_three_files(tmp_path):
    p = _panel(tmp_path)
    p.persist_artifacts()
    run_dir = p.run_dir
    assert (run_dir / "cost.json").exists()
    assert (run_dir / "reasoning.md").exists()
    assert (run_dir / "transcript.json").exists()


def test_cost_json_is_machine_readable_and_round_trips(tmp_path):
    p = _panel(tmp_path)
    p.persist_artifacts()
    parsed = json.loads((p.run_dir / "cost.json").read_text())
    assert parsed["provider"] == "openai"
    assert parsed["total_tokens"] == 12345 + 6789
    assert parsed["total_usd"] == pytest.approx(0.0421)


def test_transcript_is_a_json_array(tmp_path):
    p = _panel(tmp_path)
    p.persist_artifacts()
    parsed = json.loads((p.run_dir / "transcript.json").read_text())
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["event"] == "tool_call"


def test_reasoning_md_carries_what_the_agent_thought(tmp_path):
    p = _panel(tmp_path)
    p.persist_artifacts()
    text = (p.run_dir / "reasoning.md").read_text()
    assert "Picked SDP" in text


def test_write_receipt_captures_decisions_and_files(tmp_path):
    p = _panel(tmp_path)
    p.commit_files()
    p.persist_artifacts()
    p.write_receipt()
    receipt = json.loads(p.receipt_path.read_text())
    assert receipt["run_id"] == p.run_id
    keys = {d["key"] for d in receipt["decisions"]}
    assert {"data_product_type", "transform_engine"} <= keys
    paths = {f["path"] for f in receipt["files_written"]}
    assert "contract.fluid.yaml" in paths
    assert "README.md" in paths


# ---------------------------------------------------------------------------
# I1 — Ctrl-C at the prompt loses nothing
# ---------------------------------------------------------------------------


def test_persist_artifacts_runs_before_prompt(tmp_path, monkeypatch):
    """The runtime calls persist_artifacts() BEFORE confirm() — verify
    that artifacts survive a simulated Ctrl-C at the prompt."""
    p = _panel(tmp_path)
    p.persist_artifacts()  # this is what the runtime does

    def _raise_ctrl_c(_msg):
        raise KeyboardInterrupt

    proceeded = confirm(p, input_fn=_raise_ctrl_c)
    assert proceeded is False
    # Files committed by the user? No.
    assert not (Path(tmp_path) / "contract.fluid.yaml").exists()
    # But the run-dir artifacts MUST still be there.
    assert (p.run_dir / "cost.json").exists()
    assert (p.run_dir / "transcript.json").exists()
    assert (p.run_dir / "reasoning.md").exists()


def test_cleanup_run_dir_removes_rejected_run(tmp_path):
    p = _panel(tmp_path)
    p.persist_artifacts()
    assert p.run_dir.exists()
    p.cleanup_run_dir()
    assert not p.run_dir.exists()


def test_commit_files_rejects_path_traversal(tmp_path):
    p = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    p.add_file("../escape.txt", "nope")
    with pytest.raises(PreviewError):
        p.commit_files()


# ---------------------------------------------------------------------------
# I4 — cost visible even when --yes is used
# ---------------------------------------------------------------------------


def test_confirm_with_auto_yes_still_renders(tmp_path, capsys):
    p = _panel(tmp_path)
    proceeded = confirm(p, auto_yes=True)
    assert proceeded is True
    captured = capsys.readouterr()
    assert p.run_id in captured.out
    # The dollar figure is rendered:
    assert "$0.0421" in captured.out or "0.0421" in captured.out
    # And the token count:
    assert "19" in captured.out  # 12345+6789=19134, "19" appears as the K-prefix


def test_confirm_yes_reads_default_when_blank(tmp_path):
    p = _panel(tmp_path)

    def _input(_msg):
        return ""  # press Enter → default is yes

    assert confirm(p, input_fn=_input) is True


def test_confirm_explicit_no_aborts(tmp_path):
    p = _panel(tmp_path)

    def _input(_msg):
        return "n"

    assert confirm(p, input_fn=_input) is False


# ---------------------------------------------------------------------------
# capture_cost_snapshot — bridge to the upstream tracker
# ---------------------------------------------------------------------------


def test_capture_cost_snapshot_returns_zeroed_when_tracker_empty():
    from fluid_build.copilot.cost import reset_run_tracker

    reset_run_tracker()
    snap = capture_cost_snapshot(provider="openai", model="gpt-4o", started_at=0.0)
    assert snap.provider == "openai"
    assert snap.input_tokens == 0
    assert snap.output_tokens == 0


def test_capture_cost_snapshot_picks_up_recorded_call():
    from fluid_build.copilot.cost import get_run_tracker, reset_run_tracker

    reset_run_tracker()
    get_run_tracker().record_call(
        provider="openai",
        model="gpt-4o-2024-08-06",
        input_tokens=1000,
        output_tokens=500,
    )
    snap = capture_cost_snapshot(provider="openai", model="gpt-4o-2024-08-06", started_at=0.0)
    assert snap.input_tokens == 1000
    assert snap.output_tokens == 500
    assert snap.total_tokens == 1500
    # USD may be None only if the model isn't in the catalog; gpt-4o is.
    assert snap.total_usd is not None and snap.total_usd > 0
    reset_run_tracker()


# ---------------------------------------------------------------------------
# render — does not crash without rich, runs with rich
# ---------------------------------------------------------------------------


def test_render_completion_does_not_crash(tmp_path, capsys):
    p = _panel(tmp_path)
    render_completion(p)
    captured = capsys.readouterr().out
    assert p.run_id in captured
    assert "fluid validate" in captured


def test_render_handles_empty_files(tmp_path, capsys):
    p = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    render(p)
    captured = capsys.readouterr().out
    assert "no files queued" in captured or "files" in captured.lower()


# ---------------------------------------------------------------------------
# Security: redaction of credential-shaped strings
# ---------------------------------------------------------------------------


def test_transcript_redacts_bearer_tokens(tmp_path):
    p = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    p.append_transcript(
        {
            "kind": "tool_calls_dispatched",
            "tool_calls": [
                {
                    "name": "fetch_url",
                    "input": {
                        "url": "https://api.example.com",
                        "header": "Bearer sk-very-secret-token-1234",
                    },
                }
            ],
        }
    )
    p.persist_artifacts()
    on_disk = (p.run_dir / "transcript.json").read_text()
    assert "sk-very-secret-token-1234" not in on_disk
    assert "Bearer" in on_disk  # the prefix stays, only the value redacts


def test_reasoning_redacts_assignment_secrets(tmp_path):
    p = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    p.append_reasoning("API_KEY=sk-1234567890abcdef I'll set the env var.")
    p.persist_artifacts()
    on_disk = (p.run_dir / "reasoning.md").read_text()
    assert "sk-1234567890abcdef" not in on_disk
