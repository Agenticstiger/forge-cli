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

"""``fluid mission`` CLI — check / trust / list through the real parser."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PASSING_CONTRACT = REPO_ROOT / "examples" / "customer360" / "contract.fluid.yaml"
NO_DQ_CONTRACT = REPO_ROOT / "examples" / "05-data-quality-validation" / "contract.fluid.yaml"

LOGGER = logging.getLogger("test.mission.cli")

SPEC = """\
name: team-quality
description: Team-local quality gate.
goal: Ports carry dq rules.
success_criteria:
  - check: validate
  - check: predicate
    path: "exposes[*].contract.dq.rules"
    op: exists
"""


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("FLUID_USER_HOME", str(home))
    monkeypatch.chdir(workspace)
    return workspace


def _invoke(argv):
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(argv)
    return args.func(args, LOGGER)


def test_mission_check_green_scorecard_exits_zero(isolated, capsys):
    rc = _invoke(["mission", "check", "quality-coverage", str(PASSING_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Mission: quality-coverage" in out
    assert "Contract sha256:" in out
    assert "2/2 non-advisory checks passing — PASS" in out


def test_mission_check_red_scorecard_exits_one(isolated, capsys):
    rc = _invoke(["mission", "check", "quality-coverage", str(NO_DQ_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "exposes[0].contract.dq: path not found" in out


def test_mission_check_json_is_machine_readable(isolated, capsys):
    rc = _invoke(["mission", "check", "quality-coverage", str(PASSING_CONTRACT), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["passed"] is True
    assert payload["mission"] == "quality-coverage"
    assert {r["name"] for r in payload["results"]} == {"validate", "predicate"}
    assert payload["contract_sha256"]


def test_mission_check_untrusted_workspace_spec_refused_then_trusted(isolated, capsys):
    missions = isolated / ".fluid" / "missions"
    missions.mkdir(parents=True)
    spec_path = missions / "team_quality.yaml"
    spec_path.write_text(SPEC, encoding="utf-8")

    # Unseen spec: fail closed, exit 2, remediation printed.
    rc = _invoke(["mission", "check", "team-quality", str(PASSING_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "Refusing to run untrusted mission spec" in out
    assert "fluid mission trust" in out

    # Approve, then the same invocation runs.
    rc = _invoke(["mission", "trust", str(spec_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Trusted mission 'team-quality'" in out
    rc = _invoke(["mission", "check", "team-quality", str(PASSING_CONTRACT)])
    assert rc == 0

    # Tampering re-closes the gate.
    spec_path.write_text(SPEC + "plan_hint: [sneaky]\n", encoding="utf-8")
    rc = _invoke(["mission", "check", "team-quality", str(PASSING_CONTRACT)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "CHANGED" in out


def test_mission_trust_builtin_is_implicit_noop(isolated, capsys):
    rc = _invoke(["mission", "trust", "gdpr-clean"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "implicitly trusted" in out


def test_mission_check_missing_contract_exits_two(isolated, capsys):
    rc = _invoke(["mission", "check", "quality-coverage", "ghost.yaml"])
    assert rc == 2
    assert "Cannot run checks" in capsys.readouterr().out


def test_mission_check_unknown_spec_exits_two(isolated, capsys):
    rc = _invoke(["mission", "check", "no-such-mission", str(PASSING_CONTRACT)])
    assert rc == 2
    assert "Mission spec error" in capsys.readouterr().out


def test_mission_list_shows_builtins_with_trust_status(isolated, capsys):
    rc = _invoke(["mission", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gdpr-clean" in out
    assert "quality-coverage" in out
    assert "builtin" in out


def test_bare_mission_prints_usage_and_exits_two(isolated, capsys):
    rc = _invoke(["mission"])
    assert rc == 2
    assert "fluid mission" in capsys.readouterr().out
