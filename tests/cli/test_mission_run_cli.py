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

"""``fluid mission run`` — the CLI surface, through the real parser.

The loop itself is pinned in
``tests/copilot/missions/test_mission_runner.py``; this file covers the
CLI's own contract: argument wiring, the trust gate firing *before*
anything the spec configures takes effect, exit codes, ``--json``,
``--resume``, and the fact that an LLM is genuinely required (missions
plan and edit; ``fluid mission check`` is the zero-LLM half).

``run_mission`` is stubbed at the module boundary — no LLM call, no
network, no clock dependency.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
NO_DQ_CONTRACT = REPO_ROOT / "examples" / "05-data-quality-validation" / "contract.fluid.yaml"

LOGGER = logging.getLogger("test.mission.run.cli")

WORKSPACE_SPEC = """\
name: workspace-mission
description: A mission that arrived with the repo.
goal: Ports carry dq rules.
success_criteria:
  - check: predicate
    path: "exposes[*].contract.dq.rules"
    op: exists
budgets:
  max_iterations: 1
"""


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("FLUID_USER_HOME", str(home))
    # A resolvable provider so the LLM gate isn't what fails these tests.
    monkeypatch.setenv("FLUID_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.chdir(workspace)
    return workspace


def _invoke(argv):
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(argv)
    return args.func(args, LOGGER)


class _FakeOutcome:
    """Minimal stand-in for :class:`MissionOutcome`."""

    def __init__(self, status="complete", pause_reason=None, run_dir=Path("/tmp/run")):
        self.status = status
        self.pause_reason = pause_reason
        self.run_id = "run-test"
        self.run_dir = run_dir
        self.mission = "quality-coverage"
        self.cycles = 2
        self.scorecard = None
        self.spend_usd = 0.1234
        self.detail = "detail text"
        self.events: list = []

    @property
    def passed(self):
        return self.status == "complete"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "pause_reason": self.pause_reason,
            "cycles": self.cycles,
            "spend_usd": self.spend_usd,
        }


@pytest.fixture()
def stub_run(monkeypatch):
    """Capture the kwargs the CLI hands the runner."""
    from fluid_build.copilot.missions import runner as runner_module

    calls: list = []

    def _fake(spec, contract_path, **kwargs):
        calls.append({"spec": spec, "contract_path": contract_path, **kwargs})
        return _FakeOutcome()

    monkeypatch.setattr(runner_module, "run_mission", _fake)
    return calls


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_run_subcommand_is_registered_with_the_documented_flags():
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(["mission", "run", "quality-coverage"])
    assert args.subcommand == "run"
    assert args.spec == "quality-coverage"
    assert args.contract == "contract.fluid.yaml"
    assert args.resume is False


def test_usage_line_advertises_run(isolated, capsys):
    rc = _invoke(["mission"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "check,run,trust,list" in out


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_run_completes_and_exits_zero(isolated, stub_run, capsys):
    rc = _invoke(["mission", "run", "quality-coverage", str(NO_DQ_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MISSION COMPLETE" in out
    assert "Budgets: max_usd=3.0" in out
    assert "Spend: $0.1234" in out


def test_run_paused_exits_one_and_tells_you_how_to_resume(isolated, monkeypatch, capsys):
    from fluid_build.copilot.missions import runner as runner_module

    monkeypatch.setattr(
        runner_module,
        "run_mission",
        lambda *a, **k: _FakeOutcome(status="paused", pause_reason="budget"),
    )
    rc = _invoke(["mission", "run", "quality-coverage", str(NO_DQ_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISSION PAUSED (budget)" in out
    assert "fluid mission run quality-coverage --resume" in out


def test_run_json_is_machine_readable(isolated, stub_run, capsys):
    rc = _invoke(["mission", "run", "quality-coverage", str(NO_DQ_CONTRACT), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out[out.index("{") :])
    assert payload["status"] == "complete"
    assert payload["run_id"] == "run-test"


def test_resume_flag_reaches_the_runner(isolated, stub_run):
    _invoke(["mission", "run", "quality-coverage", str(NO_DQ_CONTRACT), "--resume"])
    assert stub_run[0]["resume"] is True


def test_spec_budgets_and_workspace_reach_the_runner(isolated, stub_run, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    _invoke(
        [
            "mission",
            "run",
            "quality-coverage",
            str(NO_DQ_CONTRACT),
            "--workspace",
            str(target),
        ]
    )
    call = stub_run[0]
    assert call["workspace_root"] == target.resolve()
    assert call["spec"].name == "quality-coverage"
    assert call["spec"].budgets.max_iterations == 4


def test_llm_flags_reach_the_resolved_config(isolated, stub_run):
    _invoke(
        [
            "mission",
            "run",
            "quality-coverage",
            str(NO_DQ_CONTRACT),
            "--llm-provider",
            "gemini",
            "--llm-model",
            "gemini-2.5-flash",
        ]
    )
    config = stub_run[0]["llm_config"]
    assert config.provider == "gemini"
    assert config.model == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Refusals — the security surface
# ---------------------------------------------------------------------------


def test_untrusted_workspace_spec_is_refused_before_the_runner_starts(isolated, monkeypatch):
    """A cloned repo's .fluid/missions/ must not configure autonomous work."""
    from fluid_build.copilot.missions import runner as runner_module

    def _must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError("runner started on an untrusted spec")

    monkeypatch.setattr(runner_module, "run_mission", _must_not_run)

    missions = isolated / ".fluid" / "missions"
    missions.mkdir(parents=True)
    spec_path = missions / "workspace_mission.yaml"
    spec_path.write_text(WORKSPACE_SPEC, encoding="utf-8")

    rc = _invoke(["mission", "run", str(spec_path), str(NO_DQ_CONTRACT)])
    assert rc == 2


def test_trusting_a_workspace_spec_lets_the_run_proceed(isolated, stub_run):
    missions = isolated / ".fluid" / "missions"
    missions.mkdir(parents=True)
    spec_path = missions / "workspace_mission.yaml"
    spec_path.write_text(WORKSPACE_SPEC, encoding="utf-8")

    assert _invoke(["mission", "trust", str(spec_path)]) == 0
    assert _invoke(["mission", "run", str(spec_path), str(NO_DQ_CONTRACT)]) == 0
    assert stub_run[0]["spec"].name == "workspace-mission"


def test_unknown_mission_errors_without_touching_the_runner(isolated):
    rc = _invoke(["mission", "run", "no-such-mission", str(NO_DQ_CONTRACT)])
    assert rc == 2


def test_missing_llm_config_is_a_clean_error_not_a_traceback(isolated, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("FLUID_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = _invoke(["mission", "run", "quality-coverage", str(NO_DQ_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "Cannot run mission" in out
    assert "fluid mission check" in out  # points at the zero-LLM half


def test_runner_crash_surfaces_a_typed_name_not_a_traceback(isolated, monkeypatch, capsys):
    from fluid_build.copilot.missions import runner as runner_module

    def _boom(*a, **k):
        raise ValueError("s3 secret AKIAIOSFODNN7EXAMPLE in the message")

    monkeypatch.setattr(runner_module, "run_mission", _boom)
    rc = _invoke(["mission", "run", "quality-coverage", str(NO_DQ_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "ValueError" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
