# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The eval harness grades deterministically and its scenarios are sound.

This runs in the default lane: no agent, no network. Every scenario's reference
solution must validate and pass every check. A few broken solutions must fail exactly
the check that names their fault, so a check that passes everything cannot hide here.
The live run is ``test_live_eval.py`` (marker ``agent_eval``, off by default).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from tests.agent_evals import harness

SCENARIOS = harness.load_scenarios()


def _workspace(scenario: harness.Scenario, tmp_path: Path) -> Path:
    ws = harness.prepare_workspace(scenario, tmp_path / "ws")
    if scenario.reference_dir.is_dir():
        shutil.copytree(scenario.reference_dir, ws, dirs_exist_ok=True)
    return ws


def _failed(grade: harness.Grade) -> list:
    return [c.id for c in grade.checks if not c.passed]


def _edit(ws: Path, rel: str, change) -> None:
    path = ws / rel
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# The scenario set
# ---------------------------------------------------------------------------


def test_the_scenarios_cover_the_matrix() -> None:
    """At least ten: SDP, ADP and CDP; local, Snowflake and GCP; refine; compose."""
    assert len(SCENARIOS) >= 10, [s.id for s in SCENARIOS]
    types, platforms = set(), set()
    for scenario in SCENARIOS:
        for rel in scenario.contracts:
            path = scenario.reference_dir / rel
            if not path.is_file():
                path = scenario.context_dir / rel
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            types.add(doc["metadata"].get("productType"))
            platforms |= {e["binding"]["platform"] for e in doc["exposes"]}
    assert {"SDP", "ADP", "CDP"} <= types
    assert {"local", "snowflake", "gcp"} <= platforms
    assert any(s.id.startswith("refine-") for s in SCENARIOS)
    assert any(any("consumes" in c for c in s.checks) for s in SCENARIOS)
    forge = [s for s in SCENARIOS if "forge" in s.arms]
    assert len(forge) >= 8, "the embedded-forge arm needs most scenarios to compare on"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_is_well_formed(scenario: harness.Scenario) -> None:
    assert scenario.prompt and scenario.title
    assert set(scenario.arms) <= set(harness.ARMS)
    ids = [c["id"] for c in scenario.checks]
    assert len(ids) == len(set(ids)), "check ids must be unique"
    assert not {"no_invented_email", "no_template_residue"} & set(ids), "those always run"
    assert len(scenario.checks) >= 3
    for rel in scenario.contracts:
        assert (scenario.reference_dir / rel).is_file(), f"no reference for {rel}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_the_reference_solution_passes(scenario: harness.Scenario, tmp_path: Path) -> None:
    grade = harness.grade(scenario, _workspace(scenario, tmp_path))
    assert grade.valid, grade.validate_output
    assert grade.passed and grade.fidelity == 1.0, _failed(grade)


# ---------------------------------------------------------------------------
# Broken solutions fail the check that names the fault
# ---------------------------------------------------------------------------

ORDERS = next(s for s in SCENARIOS if s.id == "sdp-local-orders")
ENRICHED = next(s for s in SCENARIOS if s.id == "adp-local-orders-enriched")


@pytest.mark.parametrize(
    "fault, change, expected",
    [
        (
            "invented owner email",
            lambda d: d["metadata"]["owner"].update(email="sales-data@company.com"),
            ["no_invented_email"],
        ),
        (
            "wrong schedule",
            lambda d: d["builds"][0]["execution"]["trigger"].update(cron="0 3 * * *"),
            ["daily_0200"],
        ),
        (
            "uniqueness only in the deprecated ODCS rule key",
            lambda d: (
                d["exposes"][0]["contract"].pop("dq"),
                d["exposes"][0]["contract"]["schema"][0].update(
                    quality=[{"type": "library", "rule": "unique"}]
                ),
            ),
            ["order_id_unique"],
        ),
        (
            "starter residue",
            lambda d: d["metadata"]["owner"].update(team="data-team"),
            ["no_template_residue", "owner_team"],
        ),
        (
            "a column typed wrong",
            lambda d: d["exposes"][0]["contract"]["schema"][3].update(type="string"),
            ["columns"],
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_a_fault_fails_its_check(fault: str, change, expected: list, tmp_path: Path) -> None:
    ws = _workspace(ORDERS, tmp_path)
    _edit(ws, "contract.fluid.yaml", change)
    grade = harness.grade(ORDERS, ws)
    assert grade.valid, grade.validate_output
    assert sorted(_failed(grade)) == sorted(expected), fault


def test_grading_leaves_the_environment_as_it_found_it() -> None:
    """``fluid``'s ``main`` sets the dotenv switch; grading must not leak it into the
    tests that run after this one in the same process."""
    before = dict(os.environ)
    ok, _ = harness.validate(ORDERS.reference_dir / "contract.fluid.yaml")
    assert ok and dict(os.environ) == before


def test_an_invalid_contract_is_not_valid(tmp_path: Path) -> None:
    ws = _workspace(ORDERS, tmp_path)
    _edit(ws, "contract.fluid.yaml", lambda d: d["exposes"][0]["binding"].update(format="duckdb"))
    grade = harness.grade(ORDERS, ws)
    assert not grade.valid and not grade.passed
    assert "'duckdb' is not one of" in grade.validate_output


def test_a_wrong_expose_id_passes_validate_but_fails_consumes(tmp_path: Path) -> None:
    """``fluid validate`` does not check ``consumes[].exposeId``; the grader does."""
    ws = _workspace(ENRICHED, tmp_path)
    rel = ENRICHED.contracts[0]
    _edit(ws, rel, lambda d: d["consumes"][0].update(exposeId="orders"))
    grade = harness.grade(ENRICHED, ws)
    assert grade.valid
    assert _failed(grade) == ["consumes"]


def test_a_missing_contract_fails_everything_but_the_universal_checks(tmp_path: Path) -> None:
    ws = harness.prepare_workspace(ORDERS, tmp_path / "ws")
    grade = harness.grade(ORDERS, ws)
    assert not grade.valid
    assert set(_failed(grade)) == {c["id"] for c in ORDERS.checks}


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def test_resolve_selects_list_items() -> None:
    doc = {"exposes": [{"exposeId": "a", "contract": {"schema": [{"name": "x", "type": "int"}]}}]}
    assert harness.resolve(doc, "exposes[exposeId=a].contract.schema[name=x].type") == (True, "int")
    assert harness.resolve(doc, "exposes[0].exposeId") == (True, "a")
    assert harness.resolve(doc, "exposes[exposeId=b].contract") == (False, None)
    assert harness.resolve(doc, "metadata.owner") == (False, None)


@pytest.mark.parametrize(
    "type_name, family",
    [
        ("decimal(12,2)", "decimal"),
        ("VARCHAR(255)", "string"),
        ("TIMESTAMP_NTZ", "timestamp"),
        ("bigint", "integer"),
        ("double", "float"),
        ("geography", None),
    ],
)
def test_type_families(type_name: str, family: Any) -> None:
    assert harness._family_of(type_name) == family


def _stream(*messages: Dict[str, Any]) -> list:
    return [json.dumps(m) for m in messages] + ["not json"]


def test_parse_stream_reads_skills_tools_and_cost() -> None:
    lines = _stream(
        {"type": "system", "subtype": "init", "skills": ["authoring-fluid-contracts"]},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "input": {"skill": "authoring-fluid-contracts"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "fluid validate c.yaml"},
                    },
                    {"type": "text", "text": "done"},
                ]
            },
        },
        {
            "type": "result",
            "is_error": False,
            "num_turns": 7,
            "total_cost_usd": 0.12,
            "permission_denials": [{"tool_name": "WebFetch"}],
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "modelUsage": {
                "claude-sonnet": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cacheReadInputTokens": 1000,
                    "cacheCreationInputTokens": 10,
                }
            },
        },
    )
    info = harness.parse_stream(lines)
    assert info["skills_available"] == ["authoring-fluid-contracts"]
    assert info["skills_invoked"] == ["authoring-fluid-contracts"]
    assert info["bash_commands"] == ["fluid validate c.yaml"]
    assert info["tools_used"] == {"Skill": 1, "Bash": 1}
    assert info["permission_denials"] == 1
    totals = harness.usage_totals([info["result"]])
    assert totals == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 10,
        "cost_usd": 0.12,
        "turns": 7,
        "calls": 1,
    }


def test_usage_totals_falls_back_to_main_loop_usage() -> None:
    result = {"usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 5}}
    totals = harness.usage_totals([result, None])
    assert (totals["input_tokens"], totals["output_tokens"], totals["calls"]) == (3, 4, 1)


@pytest.mark.parametrize(
    "commands, kind, extra, expected",
    [
        (["fluid apply runtime/plan.json --mode amend"], "no_apply", {}, False),
        (["fluid apply runtime/plan.json --mode dry-run"], "no_apply", {}, True),
        (
            ["fluid plan c.yaml --allow-data-loss"],
            "no_flags",
            {"flags": ["--allow-data-loss"]},
            False,
        ),
        (["fluid plan c.yaml --env dev"], "ran", {"pattern": "fluid plan"}, True),
        ([], "ran", {"pattern": "fluid plan"}, False),
    ],
)
def test_transcript_checks(commands: list, kind: str, extra: dict, expected: bool) -> None:
    ok, _ = harness._check_transcript({"transcript": kind, **extra}, {"bash_commands": commands})
    assert ok is expected


def test_agent_runs_are_isolated_from_the_users_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = harness._agent_argv(harness.RunConfig(out=Path(".")), "do it")
    for flag in (
        "--setting-sources=project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
    ):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--bare" not in argv, "bare mode never reads the subscription login"
    assert argv[2].endswith(harness.NON_INTERACTIVE)
    for name, value in {
        "ANTHROPIC_API_KEY": "sk-test",  # pragma: allowlist secret
        "CLAUDECODE": "1",
        "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",  # pragma: allowlist secret
        "GOOGLE_APPLICATION_CREDENTIALS": "/keys/sa.json",
        "SNOWFLAKE_PASSWORD": "pw",  # pragma: allowlist secret
        "GITHUB_TOKEN": "ghp_test",  # pragma: allowlist secret
    }.items():
        monkeypatch.setenv(name, value)
    env = harness._child_env(tmp_path / "bin", Path("/src"))
    leaked = {"ANTHROPIC_API_KEY", "CLAUDECODE", "AWS_ACCESS_KEY_ID", "GITHUB_TOKEN"} & set(env)
    assert not leaked
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env and "SNOWFLAKE_PASSWORD" not in env
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert env["PATH"].startswith(str(tmp_path / "bin"))
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_agents_may_not_touch_anything_outside_their_workspace() -> None:
    """No unscoped file tool, no shell escape hatch, and no fluid stage that writes to
    a real account or outside the workspace."""
    tools = set(harness.ALLOWED_TOOLS)
    assert not tools & {"Read", "Write", "Edit", "Bash", "Bash(fluid *)", "Glob", "Grep"}
    assert {"Read(./**)", "Edit(./**)"} <= tools
    for banned in ("apply", "publish", "scaffold-ide", "schedule-sync", "policy-apply"):
        assert not any(f"fluid {banned}" in tool for tool in tools), banned
    for shell in ("find", "cp", "mv", "rm", "cat", "curl", "python", "sh", "env"):
        assert not any(tool.startswith(f"Bash({shell}") for tool in tools), shell


def test_workspaces_live_outside_every_repository() -> None:
    """Claude Code loads each CLAUDE.md on the way up from its working directory: a
    workspace inside this repository loaded forge-cli's own into every agent arm. The
    root is a fresh mkdtemp, so no other local user can prepare it."""
    cfg = harness.RunConfig(out=harness.HERE / "runs" / "x")
    run_dir = cfg.run_dir("sdp-local-orders", "no-skill")
    assert harness.REPO_ROOT not in run_dir.parents
    assert Path(tempfile.gettempdir()).resolve() in run_dir.resolve().parents
    assert cfg.run_dir("other", "skill").parents[1] == run_dir.parents[1], "one root per run"
    assert (cfg.work_root.stat().st_mode & 0o777) == 0o700
    other = harness.RunConfig(out=harness.HERE / "runs" / "x")
    assert other.run_dir("sdp-local-orders", "no-skill") != run_dir
    shutil.rmtree(cfg.work_root)
    shutil.rmtree(other.work_root)


def test_the_forge_arms_own_claude_call_is_isolated_too() -> None:
    bare = harness.isolated(["-p", "--output-format", "json"])
    assert "--setting-sources=" in bare and "--strict-mcp-config" in bare
    already = ["-p", "--setting-sources=", "--strict-mcp-config", "--no-session-persistence"]
    assert harness.isolated(already) == already


def test_the_fluid_shim_quotes_paths_and_never_runs_python_dash_m(tmp_path: Path) -> None:
    """``python -m`` would put the agent's workspace first on ``sys.path``; a quote or a
    ``$`` in a path must not reach the shell unquoted."""
    run_dir = tmp_path / "it's $HOME `x`"
    bin_dir = harness._shims(run_dir, harness.REPO_ROOT, "/opt/claude bin/claude")
    shim = (bin_dir / "fluid").read_text(encoding="utf-8")
    assert " -m " not in shim
    assert "'/opt/claude bin/claude'" in (bin_dir / "claude").read_text(encoding="utf-8")
    # It runs at all only if every path in it reached the shell intact.
    proc = subprocess.run(
        [str(bin_dir / "fluid"), "--version"],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_the_claude_shim_logs_and_passes_through(tmp_path: Path, monkeypatch, capsys) -> None:
    fake = tmp_path / "fake_claude.py"
    fake.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'total_cost_usd': 0.05, 'num_turns': 1}))\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("prompt"))
    real_run = subprocess.run
    fake_argv = [sys.executable, str(fake)]
    monkeypatch.setattr(
        harness.subprocess, "run", lambda argv, **kw: real_run([*fake_argv, *argv[1:]], **kw)
    )
    code = harness.claude_shim("claude", str(tmp_path / "calls"), ["-p", "--output-format", "json"])
    assert code == 3
    (log,) = (tmp_path / "calls").glob("*.json")
    assert json.loads(log.read_text())["result"]["total_cost_usd"] == 0.05
    assert '"total_cost_usd": 0.05' in capsys.readouterr().out


def test_the_report_is_deterministic_and_complete() -> None:
    meta = {
        "date": "2026-11-12T00:00:00+00:00",
        "model": "sonnet",
        "arms": ["no-skill", "skill", "forge"],
        "scenarios": ["a", "b"],
        "forge_src": "/src",
        "forge_src_commit": "abc",
        "skills_commit": "def",
        "claude_code_version": "2.1.314",
    }

    def rec(scenario: str, arm: str, passed: bool) -> Dict[str, Any]:
        checks = [{"id": "x", "passed": passed, "detail": ""}]
        return {
            "scenario": scenario,
            "arm": arm,
            "wall_s": 30.0,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.1, "turns": 3},
            "skills_invoked": ["authoring-fluid-contracts"] if arm == "skill" else [],
            "grade": {
                "valid": True,
                "passed": passed,
                "fidelity": 1.0 if passed else 0.0,
                "checks": checks,
            },
        }

    records = [rec("a", "no-skill", False), rec("a", "skill", True), rec("b", "skill", True)]
    records.append({"scenario": "b", "arm": "forge", "harness_error": "boom"})
    report = harness.render_report(meta, records)
    assert report == harness.render_report(meta, records)
    assert "| skill | 2 | 2/2 (100%) | 2/2 (100%) | 100% |" in report
    assert "| no-skill | 1 | 1/1 (100%) | 0/1 (0%) | 0% |" in report
    assert "- a / no-skill: failed x" in report
    assert "- b / forge: harness error boom" in report
