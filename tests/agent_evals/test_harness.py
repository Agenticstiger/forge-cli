# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The eval harness grades deterministically and its scenarios are sound.

This runs in the default lane: no agent, no network. Every scenario's reference
solution must validate and pass every check. A few broken solutions must fail exactly
the check that names their fault, so a check that passes everything cannot hide here.
The live run is ``test_live_eval.py`` (marker ``agent_eval``, off by default).
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
    """At least ten: SDP, ADP and CDP; local, Snowflake and GCP; refine; compose; and at
    least three negative controls, which run no forge arm and expect no contract."""
    assert len(SCENARIOS) >= 10, [s.id for s in SCENARIOS]
    controls = [s for s in SCENARIOS if not s.should_trigger]
    assert len(controls) >= 3, [s.id for s in controls]
    for control in controls:
        assert "forge" not in control.arms, control.id
        assert control.contracts == [], control.id
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
    assert set(scenario.arms) <= set(harness.CONDITIONS)
    ids = [c["id"] for c in scenario.checks]
    assert len(ids) == len(set(ids)), "check ids must be unique"
    reserved = {"no_invented_email", "no_template_residue", "no_skill_fired", "contracts_untouched"}
    assert not reserved & set(ids), "those are the grader's own"
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
    banned_stages = ("apply", "publish", "scaffold-ide", "schedule-sync", "policy-apply")
    assert set(harness.CODEX_FORBIDDEN_STAGES) == set(banned_stages), "the Codex rules agree"
    for banned in banned_stages:
        assert not any(f"fluid {banned}" in tool for tool in tools), banned
    for shell in ("find", "cp", "mv", "rm", "cat", "curl", "python", "sh", "env"):
        assert not any(tool.startswith(f"Bash({shell}") for tool in tools), shell


def test_concurrent_workers_share_one_work_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run``'s workers ask for their first run directories at once. Without a lock each
    made a root of its own (three on the 2026-11-13 live run); a slow ``mkdtemp`` makes
    that race certain."""
    real = tempfile.mkdtemp

    def slow_mkdtemp(*args: Any, **kwargs: Any) -> str:
        time.sleep(0.05)
        return real(*args, **kwargs)

    monkeypatch.setattr(harness.tempfile, "mkdtemp", slow_mkdtemp)
    cfg = harness.RunConfig(out=harness.HERE / "runs" / "x")
    start = threading.Barrier(8)

    def first_dir(i: int) -> Path:
        start.wait()
        return cfg.run_dir(f"s{i}", "skill").parents[2]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        roots = set(pool.map(first_dir, range(8)))
    shutil.rmtree(cfg.work_root)
    assert roots == {cfg.work_root}


def test_workspaces_live_outside_every_repository() -> None:
    """Claude Code loads each CLAUDE.md on the way up from its working directory: a
    workspace inside this repository loaded forge-cli's own into every agent arm. The
    root is a fresh mkdtemp, so no other local user can prepare it."""
    cfg = harness.RunConfig(out=harness.HERE / "runs" / "x")
    run_dir = cfg.run_dir("sdp-local-orders", "no-skill")
    assert run_dir.name == "run-1"
    assert harness.REPO_ROOT not in run_dir.parents
    assert Path(tempfile.gettempdir()).resolve() in run_dir.resolve().parents
    assert cfg.run_dir("other", "skill").parents[2] == run_dir.parents[2], "one root per run"
    second = cfg.run_dir("sdp-local-orders", "no-skill", 2)
    assert second != run_dir and second.name == "run-2", "a trial of its own"
    assert second.parents[2] == run_dir.parents[2]
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


# ---------------------------------------------------------------------------
# A timeout kills the whole process tree
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups")
def test_a_timeout_kills_the_grandchildren_too(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "child.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    out, _, code, timed_out = harness._run_bounded(
        [sys.executable, str(script)], cwd=tmp_path, env=dict(os.environ), timeout=3
    )
    assert timed_out and code != 0 and "started" in out
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), "the grandchild outlived the timeout"


def test_a_timeout_that_cuts_a_multibyte_character_is_not_an_error(tmp_path: Path) -> None:
    script = tmp_path / "cut.py"
    script.write_text(
        "import os, time\nos.write(1, b'ok \\xc3')\ntime.sleep(60)\n", encoding="utf-8"
    )
    out, _, _, timed_out = harness._run_bounded(
        [sys.executable, str(script)], cwd=tmp_path, env=dict(os.environ), timeout=2
    )
    assert timed_out and out.startswith("ok ")
    # What the old strict decode did with the bytes TimeoutExpired carries.
    assert harness._decode(b"ok \xc3") == "ok \ufffd"


def test_a_clean_exit_is_not_a_timeout(tmp_path: Path) -> None:
    out, err, code, timed_out = harness._run_bounded(
        [sys.executable, "-c", "print('hi')"], cwd=tmp_path, env=dict(os.environ), timeout=30
    )
    assert (out.strip(), code, timed_out) == ("hi", 0, False)


# ---------------------------------------------------------------------------
# Errored runs are failed runs
# ---------------------------------------------------------------------------


def _rec(scenario: str, arm: str, passed: bool = True, **extra: Any) -> Dict[str, Any]:
    return {
        "scenario": scenario,
        "arm": arm,
        "wall_s": 10.0,
        "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.5, "turns": 1},
        "grade": {
            "valid": True,
            "passed": passed,
            "fidelity": 1.0 if passed else 0.5,
            "checks": [{"id": "x", "passed": passed, "detail": ""}],
        },
        **extra,
    }


def _meta(**extra: Any) -> Dict[str, Any]:
    return {
        "date": "2026-11-12T00:00:00+00:00",
        "model": "sonnet",
        "arms": ["skill"],
        "scenarios": ["a", "b", "c", "d"],
        "forge_src": "/src",
        "forge_src_commit": "abc",
        "skills_commit": "def",
        "claude_code_version": "2.1.314",
        **extra,
    }


def test_errored_runs_count_as_failed_in_every_rate() -> None:
    records = [
        _rec("a", "skill"),
        _rec("b", "skill", timed_out=True, error=True, exit_code=-1),
        _rec("c", "skill", error=True, exit_code=1, terminal_reason="max_turns"),
        {"scenario": "d", "arm": "skill", "harness_error": "boom"},
    ]
    s = harness.summarise(records, "skill")
    assert (s["runs"], s["valid"], s["passed"]) == (4, 1, 1)
    assert s["fidelity"] == pytest.approx(0.25)
    assert (s["harness_errors"], s["agent_errors"], s["timeouts"]) == (1, 1, 1)
    assert s["cost_usd"] == 1.5, "a run that errored still spent its tokens"
    report = harness.render_report(_meta(), records)
    assert "| skill | 4 | 1/4 (25%) | 1/4 (25%) | 25% | 1 harness, 1 agent, 1 timeout |" in report
    assert "| b | timeout |" in report and "| c | error |" in report and "| d | error |" in report
    assert "- b / skill: timeout" in report
    assert "- c / skill: agent error (max_turns)" in report
    assert "- d / skill: harness error boom" in report


def test_a_run_with_no_errors_shows_a_zero_error_count() -> None:
    assert "| skill | 1 | 1/1 (100%) | 1/1 (100%) | 100% | 0 |" in harness.render_report(
        _meta(), [_rec("a", "skill")]
    )


def test_an_untouched_workspace_does_not_pass_when_the_run_timed_out(tmp_path: Path) -> None:
    """deploy-plan-only: an untouched workspace passes most checks."""
    scenario = next(s for s in SCENARIOS if s.id == "deploy-plan-only")
    ws = harness.prepare_workspace(scenario, tmp_path / "ws")
    graded = harness.grade(scenario, ws).to_dict()
    record = {"scenario": scenario.id, "arm": "skill", "timed_out": True, "error": True}
    record["grade"] = graded
    assert graded["fidelity"] > 0, "premise: the untouched workspace scores something"
    s = harness.summarise([record], "skill")
    assert (s["valid"], s["passed"], s["fidelity"]) == (0, 0, 0.0)


# ---------------------------------------------------------------------------
# Selectors with dots, per-run meta
# ---------------------------------------------------------------------------


def test_resolve_keeps_a_dot_inside_a_selector_value() -> None:
    doc = {"exposes": [{"exposeId": "web.sessions", "binding": {"format": "parquet"}}]}
    assert harness.resolve(doc, "exposes[exposeId=web.sessions].binding.format") == (
        True,
        "parquet",
    )
    assert harness.resolve(doc, "exposes[exposeId=web.other].binding") == (False, None)
    assert harness._split_path("a[k=v.w].b[0].c") == ["a[k=v.w]", "b[0]", "c"]


def _arms_rows(report: str) -> Dict[str, list]:
    """The report's Arms table as ``{arm: [agent, version, model, effort]}``."""
    lines = report.splitlines()
    start = lines.index("| Arm | Agent | Version | Model | Effort |") + 2
    rows: Dict[str, list] = {}
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0]] = cells[1:]
    return rows


def test_run_stamps_meta_on_every_record_and_the_report_says_when_it_is_mixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios = [s for s in SCENARIOS if s.id in ("sdp-local-orders", "deploy-plan-only")]
    versions = iter(["sonnet", "opus"])
    monkeypatch.setattr(harness, "_git_head", lambda path: "abc123")
    monkeypatch.setattr(harness, "_claude_version", lambda claude: "2.1.1")
    monkeypatch.setattr(
        harness,
        "run_one",
        lambda s, arm, cfg, stamp=None, trial=1: {
            **_rec(s.id, arm),
            "trial": trial,
            "run_meta": dict(stamp or {}),
        },
    )
    out = tmp_path / "out"
    for scenario in scenarios:
        cfg = harness.RunConfig(out=out, model=next(versions))
        harness.run([scenario], ["skill"], cfg, 1)
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert {r["scenario"]: r["run_meta"]["model"] for r in data["runs"]} == {
        scenarios[0].id: "sonnet",
        scenarios[1].id: "opus",
    }
    assert all(set(harness.RUN_META_KEYS) <= set(r["run_meta"]) for r in data["runs"])
    report = (out / "report.md").read_text(encoding="utf-8")
    agent, version, model, effort = _arms_rows(report)["skill"]
    assert model == "mixed: opus (1 runs), sonnet (1 runs)"
    assert (agent, version, effort) == ("claude-code", "2.1.1", "medium")


def test_the_header_shows_one_value_when_records_agree_and_falls_back_to_meta() -> None:
    meta = _meta(model="sonnet")
    assert _arms_rows(harness.render_report(meta, [_rec("a", "skill")]))["skill"][2] == "sonnet"
    one = _rec("a", "skill", run_meta={"model": "opus"})
    two = _rec("b", "skill")  # unstamped: the meta value
    rows = _arms_rows(harness.render_report(meta, [one, two]))
    assert rows["skill"][2] == "mixed: opus (1 runs), sonnet (1 runs)"
    agree = [_rec(s, "skill", run_meta={"model": "opus"}) for s in "ab"]
    assert _arms_rows(harness.render_report(meta, agree))["skill"][2] == "opus"


def test_what_the_stream_reported_is_not_a_run_meta_value() -> None:
    """An agent record's own ``model`` is the stream's model id, not what the run asked
    for; only ``run_meta.model_reported`` adds ``(ran ...)``."""
    meta = _meta(model="sonnet")
    agent = _rec("a", "skill", model="claude-sonnet-5-5", claude_code_version="2.1.322")
    rows = _arms_rows(harness.render_report(meta, [agent, _rec("b", "forge")]))
    assert rows["skill"][2] == "sonnet"
    ran = _rec("a", "skill", run_meta={"model": "sonnet", "model_reported": "claude-sonnet-5-5"})
    same = _rec("a", "forge", run_meta={"model": "sonnet", "model_reported": "sonnet"})
    rows = _arms_rows(harness.render_report(_meta(arms=["skill", "forge"]), [ran, same]))
    assert rows["skill"][2] == "sonnet (ran claude-sonnet-5-5)"
    assert rows["forge"][2] == "sonnet"


# ---------------------------------------------------------------------------
# Quality checks pass only for a form that asserts the property
# ---------------------------------------------------------------------------


def _expose(columns=None, dq=None, quality=None, column_quality=None) -> Dict[str, Any]:
    schema = [{"name": "order_id", "type": "string"}]
    if column_quality is not None:
        schema[0]["quality"] = column_quality
    contract: Dict[str, Any] = {"schema": schema}
    if dq is not None:
        contract["dq"] = {"rules": dq}
    if quality is not None:
        contract["quality"] = quality
    return {"exposeId": "t", "contract": contract}


def _rule(kind: str, **extra: Any) -> Dict[str, Any]:
    return {"id": "r", "type": kind, "selector": "order_id", **extra}


@pytest.mark.parametrize(
    "form, expected",
    [
        ({"column_quality": [{"metric": "duplicateValues", "mustBe": 0}]}, True),
        ({"column_quality": [{"metric": "duplicateValues", "mustBeLessThan": 1}]}, True),
        ({"column_quality": [{"metric": "duplicateValues", "mustBeLessOrEqualTo": 0}]}, True),
        ({"column_quality": [{"metric": "duplicateValues", "mustBeBetween": [0, 0]}]}, True),
        ({"dq": [_rule("uniqueness", threshold=1.0, operator=">=")]}, True),
        ({"dq": [_rule("uniqueness")]}, True),
        ({"column_quality": [{"metric": "duplicateValues", "mustBeLessThan": 99}]}, False),
        ({"column_quality": [{"metric": "duplicateValues", "mustBe": 5}]}, False),
        ({"column_quality": [{"metric": "duplicateValues", "mustBeLessOrEqualTo": 3}]}, False),
        ({"column_quality": [{"metric": "duplicateValues", "mustBeGreaterThan": 0}]}, False),
        (
            {"column_quality": [{"metric": "duplicateValues", "mustBe": 0, "mustBeLessThan": 9}]},
            False,
        ),
        (
            {
                "column_quality": [
                    {"metric": "duplicateValues", "mustBeLessThan": 1, "unit": "percent"}
                ]
            },
            False,
        ),
        ({"column_quality": [{"metric": "duplicateValues"}]}, False),
        ({"dq": [_rule("uniqueness", threshold=0.01, operator=">=")]}, False),
        ({"dq": [_rule("uniqueness", threshold=0.5)]}, False),
        ({"dq": [_rule("uniqueness", threshold=1.0, operator="<=")]}, False),
        ({"dq": [_rule("uniqueness", threshold=100)]}, False),
        (
            {"quality": [{"rule": "u", "expression": "SELECT COUNT(DISTINCT order_id) FROM t"}]},
            False,
        ),
        (
            {"quality": [{"rule": "u", "expression": "COUNT(DISTINCT order_id) = COUNT(*)"}]},
            False,
        ),
    ],
)
def test_unique_needs_a_real_assertion(form: Dict[str, Any], expected: bool) -> None:
    assert harness._expresses_unique(_expose(**form), "order_id") is expected


@pytest.mark.parametrize(
    "form, expected",
    [
        ({"column_quality": [{"metric": "nullValues", "mustBe": 0}]}, True),
        ({"column_quality": [{"metric": "missingValues", "mustBeLessThan": 1}]}, True),
        ({"dq": [_rule("completeness", threshold=1.0, operator=">=")]}, True),
        ({"dq": [_rule("completeness")]}, True),
        ({"column_quality": [{"metric": "nullValues", "mustBeLessThan": 99}]}, False),
        (
            {"column_quality": [{"metric": "nullValues", "mustBeLessThan": 50, "unit": "percent"}]},
            False,
        ),
        ({"column_quality": [{"metric": "nullValues"}]}, False),
        ({"dq": [_rule("completeness", threshold=0.01)]}, False),
        ({"dq": [_rule("completeness", threshold=0.9, operator=">=")]}, False),
    ],
)
def test_not_null_needs_a_real_assertion(form: Dict[str, Any], expected: bool) -> None:
    assert harness._expresses_not_null(_expose(**form), "order_id") is expected


def test_not_null_still_accepts_required_true() -> None:
    expose = _expose()
    expose["contract"]["schema"][0]["required"] = True
    assert harness._expresses_not_null(expose, "order_id")


# ---------------------------------------------------------------------------
# reads_upstream
# ---------------------------------------------------------------------------

UPSTREAM = {"productId": "sales.orders_raw", "exposeId": "orders_raw", "file": "orders_raw.parquet"}


def _reads(doc: Dict[str, Any], text: str = "") -> bool:
    target = harness.Target(doc, text or yaml.safe_dump(doc), Path("."))
    ok, _ = harness.run_check({"id": "r", "reads_upstream": UPSTREAM}, target)
    return ok


def _build(sql: str, inputs: Any = None) -> Dict[str, Any]:
    properties: Dict[str, Any] = {"sql": sql}
    if inputs:
        properties["parameters"] = {"inputs": inputs}
    return {"id": "b", "properties": properties}


CONSUMES = [{"productId": "sales.orders_raw", "exposeId": "orders_raw"}]


def test_reads_upstream_accepts_the_inputs_path() -> None:
    doc = {
        "builds": [
            _build(
                "SELECT * FROM src",
                [{"name": "src", "path": "../orders_raw/runtime/out/orders_raw.parquet"}],
            )
        ]
    }
    assert _reads(doc)


def test_reads_upstream_accepts_consumes_alone_when_the_sql_reads_the_expose() -> None:
    doc = {"consumes": CONSUMES, "builds": [_build("SELECT o.id FROM orders_raw o")]}
    assert _reads(doc)


def test_reads_upstream_rejects_sql_that_does_not_read_the_consumed_expose() -> None:
    doc = {"consumes": CONSUMES, "builds": [_build("SELECT 1 AS id FROM other_table")]}
    assert not _reads(doc)
    assert not _reads({"consumes": CONSUMES, "builds": [_build("SELECT * FROM orders_raw_v2")]})
    other = [{"productId": "sales.elsewhere", "exposeId": "orders_raw"}]
    assert not _reads({"consumes": other, "builds": [_build("SELECT * FROM orders_raw")]})


# ---------------------------------------------------------------------------
# regrade
# ---------------------------------------------------------------------------


def test_regrade_grades_kept_workspaces_again_and_lists_the_missing(tmp_path: Path) -> None:
    scenario = ORDERS
    runs = tmp_path / "runs"
    good = tmp_path / "work" / "good"
    harness.prepare_workspace(scenario, good / "workspace")
    shutil.copytree(scenario.reference_dir, good / "workspace", dirs_exist_ok=True)
    records = [
        {**_rec(scenario.id, "no-skill", passed=False), "run_dir": str(good), "bash_commands": []},
        {**_rec(scenario.id, "skill", passed=False), "run_dir": str(tmp_path / "gone")},
        {**_rec(scenario.id, "forge", passed=False), "run_dir": str(good)},
        {"scenario": scenario.id, "arm": "forge"},  # no run_dir: left alone
    ]
    runs.mkdir()
    (runs / "results.json").write_text(
        json.dumps({"meta": _meta(arms=["no-skill", "skill", "forge"]), "runs": records}),
        encoding="utf-8",
    )
    missing = harness.regrade(runs, {scenario.id: scenario})
    assert missing == [f"{scenario.id} / skill"]
    data = json.loads((runs / "results.json").read_text(encoding="utf-8"))
    by_arm = {r["arm"]: r for r in data["runs"] if "run_dir" in r}
    assert by_arm["no-skill"]["grade"]["passed"] is True
    assert by_arm["no-skill"]["wall_s"] == 10.0, "the rest of the record is kept"
    assert by_arm["skill"]["grade"]["passed"] is False, "a gone workspace keeps its old grade"
    assert by_arm["forge"]["grade"]["passed"] is True
    assert {"regraded_at", "grader_commit"} <= set(data["meta"])
    report = (runs / "report.md").read_text(encoding="utf-8")
    assert "| no-skill | 1 | 1/1 (100%)" in report
    assert f"- Regraded: {data['meta']['regraded_at']} with the grader at `" in report


def test_regrade_passes_the_record_to_agent_arms_as_the_transcript(tmp_path: Path) -> None:
    scenario = next(s for s in SCENARIOS if s.id == "deploy-plan-only")
    ws_dir = tmp_path / "work"
    ws = harness.prepare_workspace(scenario, ws_dir / "workspace")
    shutil.copytree(scenario.reference_dir, ws, dirs_exist_ok=True)
    runs = tmp_path / "runs"
    runs.mkdir()
    record = {
        **_rec(scenario.id, "skill"),
        "run_dir": str(ws_dir),
        "bash_commands": ["fluid apply x"],
    }
    (runs / "results.json").write_text(
        json.dumps({"meta": _meta(), "runs": [record]}), encoding="utf-8"
    )
    harness.regrade(runs, {scenario.id: scenario})
    data = json.loads((runs / "results.json").read_text(encoding="utf-8"))
    checks = {c["id"]: c for c in data["runs"][0]["grade"]["checks"]}
    assert any(
        not c["passed"] for c in checks.values() if "apply" in c["detail"]
    ), "the recorded fluid apply must reach the transcript checks"


# ---------------------------------------------------------------------------
# pass@k and pass^k
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "c, k, at_k, hat_k",
    [
        (0, 1, 0.0, 0.0),
        (0, 2, 0.0, 0.0),
        (0, 3, 0.0, 0.0),
        (1, 1, 1 / 3, 1 / 3),
        (1, 2, 2 / 3, 0.0),
        (1, 3, 1.0, 0.0),
        (2, 1, 2 / 3, 2 / 3),
        (2, 2, 1.0, 1 / 3),
        (2, 3, 1.0, 0.0),
        (3, 1, 1.0, 1.0),
        (3, 2, 1.0, 1.0),
        (3, 3, 1.0, 1.0),
    ],
)
def test_pass_at_k_and_pass_hat_k_known_values(c: int, k: int, at_k: float, hat_k: float) -> None:
    assert harness.pass_at_k(3, c, k) == pytest.approx(at_k)
    assert harness.pass_hat_k(3, c, k) == pytest.approx(hat_k)


@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_at_k_equal_to_n_is_any_and_all(n: int) -> None:
    for c in range(n + 1):
        assert harness.pass_at_k(n, c, n) == (1.0 if c > 0 else 0.0)
        assert harness.pass_hat_k(n, c, n) == (1.0 if c == n else 0.0)


@pytest.mark.parametrize("k", [0, -1, 4])
def test_pass_at_k_needs_k_between_one_and_n(k: int) -> None:
    with pytest.raises(ValueError):
        harness.pass_at_k(3, 1, k)
    with pytest.raises(ValueError):
        harness.pass_hat_k(3, 1, k)


# ---------------------------------------------------------------------------
# Several runs per scenario: summarise and the report
# ---------------------------------------------------------------------------


def _runs(scenario: str, arm: str, outcomes: str, **extra: Any) -> list:
    """One record per character of ``outcomes``: ``p`` passes, anything else fails."""
    return [
        _rec(scenario, arm, passed=ch == "p", trial=i, **extra)
        for i, ch in enumerate(outcomes, start=1)
    ]


def _table(report: str, first_cell: str) -> Dict[str, Dict[str, str]]:
    """The report table whose header starts ``| <first_cell> |``: ``{row: {column: cell}}``."""
    lines = report.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"| {first_cell} |"))
    header = [c.strip() for c in lines[start].strip("|").split("|")]
    rows: Dict[str, Dict[str, str]] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0]] = dict(zip(header[1:], cells[1:], strict=True))
    return rows


def test_summarise_with_three_runs_per_scenario_means_over_scenarios() -> None:
    records = _runs("a", "skill", "ppp") + _runs("b", "skill", "pff")
    s = harness.summarise(records, "skill", k=3)
    assert (s["runs"], s["scenarios"], s["min_runs"]) == (6, 2, 3)
    assert (s["valid"], s["passed"]) == (6, 4)
    assert s["valid_rate"] == pytest.approx(1.0)
    assert s["pass_rate"] == pytest.approx((1.0 + 1 / 3) / 2)
    assert s["fidelity"] == pytest.approx((1.0 + (1.0 + 0.5 + 0.5) / 3) / 2)
    assert s["pass_at_k"] == pytest.approx(1.0)  # b has one pass in 3 runs: any of 3 passes
    assert s["pass_hat_k"] == pytest.approx(0.5)  # a: all 3 pass; b: not all
    two = harness.summarise(records, "skill", k=2)
    assert two["pass_at_k"] == pytest.approx((1.0 + 2 / 3) / 2)
    assert two["pass_hat_k"] == pytest.approx(0.5)


def test_summarise_weighs_scenarios_not_runs() -> None:
    records = _runs("a", "skill", "pppp") + _runs("b", "skill", "f")
    s = harness.summarise(records, "skill")
    assert s["runs"] == 5 and s["passed"] == 4
    assert s["pass_rate"] == pytest.approx(0.5), "mean of 1.0 and 0.0, not 4/5"


def test_summarise_leaves_out_pass_at_k_when_a_scenario_has_fewer_runs() -> None:
    records = _runs("a", "skill", "ppp") + _runs("b", "skill", "pp")
    s = harness.summarise(records, "skill", k=3)
    assert s["min_runs"] == 2
    assert "pass_at_k" not in s and "pass_hat_k" not in s
    assert "pass_at_k" in harness.summarise(records, "skill", k=2)
    assert "pass_at_k" not in harness.summarise(records, "skill"), "no k, no estimate"


def test_summarise_counts_errored_runs_as_failures_in_every_trial() -> None:
    records = _runs("a", "skill", "ppp")
    records[2].update(timed_out=True, error=True)  # its workspace grades as a pass
    s = harness.summarise(records, "skill", k=3)
    assert (s["runs"], s["passed"], s["valid"], s["timeouts"]) == (3, 2, 2, 1)
    assert s["pass_rate"] == pytest.approx(2 / 3)
    assert s["fidelity"] == pytest.approx(2 / 3)
    assert s["pass_at_k"] == 1.0 and s["pass_hat_k"] == 0.0


def test_summarise_cost_and_turns_are_none_when_no_run_reported_them() -> None:
    codex_usage = {"input_tokens": 5, "output_tokens": 1, "cost_usd": None, "turns": None}
    records = _runs("a", "codex-skill", "pp", usage=codex_usage)
    s = harness.summarise(records, "codex-skill")
    assert s["cost_usd"] is None and s["turns"] is None
    assert s["input_tokens"] == 10
    records.append(_rec("a", "codex-skill", trial=3))  # one run that did report a cost
    s = harness.summarise(records, "codex-skill")
    assert s["cost_usd"] == 0.5 and s["turns"] == 1


def _three_run_meta(**extra: Any) -> Dict[str, Any]:
    return _meta(arms=["skill"], scenarios=["a", "b"], **extra)


def test_the_report_with_three_runs_adds_pass_at_k_and_pass_hat_k() -> None:
    records = _runs("a", "skill", "ppp") + _runs("b", "skill", "ppf")
    report = harness.render_report(_three_run_meta(), records)
    assert "3 runs per scenario and arm" in report
    header = next(ln for ln in report.splitlines() if ln.startswith("| Arm | Runs |"))
    assert "| pass@3 | pass^3 |" in header
    score = _score_row(report, "skill")
    assert score["Runs"] == "6"
    assert score["valid@1"] == "6/6 (100%)"
    assert score["pass@1"] == "5/6 (83%)"
    assert score["pass@3"] == "100%"
    assert score["pass^3"] == "50%"
    assert score["Fidelity"] == "92%"
    assert "| a | 3/3 pass, 100% |" in report
    assert "| b | 2/3 pass, 83% |" in report
    assert "- b / skill: x (1/3)" in report
    assert "- a / skill" not in report


def _score_row(report: str, arm: str) -> Dict[str, str]:
    lines = report.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("| Arm | Runs |"))
    header = [c.strip() for c in lines[start].strip("|").split("|")]
    for line in lines[start + 2 :]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == arm:
            return dict(zip(header, cells, strict=True))
    raise AssertionError(f"no {arm} row")


def test_the_report_with_one_run_has_no_pass_at_k_columns() -> None:
    report = harness.render_report(_meta(), [_rec("a", "skill"), _rec("b", "skill")])
    header = next(ln for ln in report.splitlines() if ln.startswith("| Arm | Runs |"))
    assert "pass@1" in header and "pass^" not in header
    assert not any(f"pass@{n}" in header for n in (2, 3))
    assert "one run per scenario and arm" in report
    assert "| a | pass 1/1 |" in report


def test_a_scenario_with_fewer_runs_drops_the_pass_at_k_columns_to_the_smallest_k() -> None:
    records = _runs("a", "skill", "ppp") + _runs("b", "skill", "pp")
    report = harness.render_report(_three_run_meta(), records)
    assert "2-3 runs per scenario and arm" in report
    assert "| pass@2 | pass^2 |" in next(
        ln for ln in report.splitlines() if ln.startswith("| Arm | Runs |")
    )


def test_a_codex_arm_row_shows_no_cost_or_turns() -> None:
    usage = {"input_tokens": 100, "output_tokens": 7, "cache_read_tokens": 40}
    usage.update(cost_usd=None, turns=None)
    stamp = {"agent": "codex", "provider": "ollama", "model": "m", "effort": "medium"}
    records = _runs("a", "codex-skill", "pp", usage=usage, run_meta=stamp)
    report = harness.render_report(_meta(arms=["codex-skill"], scenarios=["a"]), records)
    score = _score_row(report, "codex-skill")
    assert (score["Cost"], score["Turns"]) == ("-", "-")
    assert (score["Input tok"], score["Cache-read tok"]) == ("200", "80")
    assert _arms_rows(report)["codex-skill"][0] == "codex (ollama)"
    assert "mixed" not in report


# ---------------------------------------------------------------------------
# run(): trials, merging, controls, conditions
# ---------------------------------------------------------------------------


def _fake_run_one(calls: list, wall_s: float = 10.0):
    def fake(s, arm, cfg, stamp=None, trial=1):
        calls.append((s.id, arm, trial))
        record = {
            **_rec(s.id, arm),
            "trial": trial,
            "wall_s": wall_s,
            "run_meta": dict(stamp or {}),
        }
        if not s.should_trigger:
            record["control"] = True
        return record

    return fake


@pytest.fixture
def quiet_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(harness, "_git_head", lambda path: "abc123")
    monkeypatch.setattr(harness, "_claude_version", lambda claude: "2.1.1")
    monkeypatch.setattr(harness, "_codex_version", lambda codex: "0.1.0")


def _by_pair(data: Dict[str, Any]) -> Dict[tuple, list]:
    pairs: Dict[tuple, list] = {}
    for r in data["runs"]:
        pairs.setdefault((r["scenario"], r["arm"]), []).append(r)
    return pairs


def test_run_makes_one_record_per_trial_and_a_second_run_replaces_only_its_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_run: None
) -> None:
    orders = ORDERS
    control = next(s for s in SCENARIOS if s.id == "control-readme-typo")
    calls: list = []
    monkeypatch.setattr(harness, "run_one", _fake_run_one(calls))
    out = tmp_path / "out"
    harness.run([orders, control], ["no-skill", "skill"], harness.RunConfig(out=out, runs=3), 2)
    assert len(calls) == 12
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    pairs = _by_pair(data)
    assert len(pairs) == 4
    for key, runs in pairs.items():
        assert [r["trial"] for r in runs] == [1, 2, 3], key
    assert data["meta"]["controls"] == [control.id]
    assert data["meta"]["scenarios"] == [control.id, orders.id], "in the scenario order"
    assert "3 runs per scenario and arm" in (out / "report.md").read_text(encoding="utf-8")

    calls.clear()
    monkeypatch.setattr(harness, "run_one", _fake_run_one(calls, wall_s=99.0))
    harness.run([orders], ["skill"], harness.RunConfig(out=out, runs=1), 1)
    assert calls == [(orders.id, "skill", 1)]
    after = json.loads((out / "results.json").read_text(encoding="utf-8"))
    pairs = _by_pair(after)
    assert len(after["runs"]) == 12, "nothing lost, nothing added"
    redone = {r["trial"]: r["wall_s"] for r in pairs[(orders.id, "skill")]}
    assert redone == {1: 99.0, 2: 10.0, 3: 10.0}
    assert all(r["wall_s"] == 10.0 for r in pairs[(orders.id, "no-skill")])
    assert after["meta"]["controls"] == [control.id], "the earlier run's scenarios are kept"
    assert {r["scenario"] for r in after["runs"]} == {orders.id, control.id}


def test_every_arm_runs_the_conditions_a_scenario_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_run: None
) -> None:
    """``codex-skill`` runs wherever ``skill`` is listed; a control lists no forge."""
    control = next(s for s in SCENARIOS if s.id == "control-duckdb-top-customers")
    assert "forge" in ORDERS.arms and "forge" not in control.arms
    calls: list = []
    monkeypatch.setattr(harness, "run_one", _fake_run_one(calls))
    monkeypatch.setattr(harness, "preflight", lambda arms, cfg: None)
    arms = ["codex-no-skill", "codex-skill", "forge"]
    harness.run([ORDERS, control], arms, harness.RunConfig(out=tmp_path / "out"), 1)
    assert sorted(calls) == sorted(
        [
            (ORDERS.id, "codex-no-skill", 1),
            (ORDERS.id, "codex-skill", 1),
            (ORDERS.id, "forge", 1),
            (control.id, "codex-no-skill", 1),
            (control.id, "codex-skill", 1),
        ]
    )


def test_run_refuses_before_starting_anything_when_it_cannot_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    monkeypatch.setattr(harness, "run_one", _fake_run_one(calls))
    with pytest.raises(SystemExit, match="cannot run"):
        harness.run([ORDERS], ["skill"], harness.RunConfig(out=tmp_path / "out", runs=0), 1)
    assert calls == [] and not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# Negative controls: which skills fired
# ---------------------------------------------------------------------------

SKILL = harness.FLUID_SKILLS[0]
CONTROLS = [s for s in SCENARIOS if not s.should_trigger]


def test_the_skill_names_this_file_relies_on_exist() -> None:
    assert "authoring-fluid-contracts" in harness.FLUID_SKILLS
    assert len(harness.FLUID_SKILLS) == 4


@pytest.mark.parametrize(
    "transcript, expected",
    [
        ({"skills_invoked": ["authoring-fluid-contracts"]}, ["authoring-fluid-contracts"]),
        ({"skills_invoked": ["fluid:authoring-fluid-contracts"]}, ["authoring-fluid-contracts"]),
        (
            {"skill_files_read": ["/tmp/ws/.claude/skills/authoring-fluid-contracts/SKILL.md"]},
            ["authoring-fluid-contracts"],
        ),
        (
            {"bash_commands": ["head .claude/skills/composing-fluid-data-products/SKILL.md"]},
            ["composing-fluid-data-products"],
        ),
        (
            {
                "bash_commands": [
                    "/bin/zsh -lc \"sed -n '1,200p' "
                    '/tmp/x/ws/.agents/skills/deploying-fluid-contracts-safely/SKILL.md"'
                ]
            },
            ["deploying-fluid-contracts-safely"],
        ),
        (
            {
                "skills_invoked": ["troubleshooting-fluid-errors"],
                "bash_commands": ["head .claude/skills/authoring-fluid-contracts/SKILL.md"],
            },
            ["authoring-fluid-contracts", "troubleshooting-fluid-errors"],
        ),
        ({"bash_commands": ["ls .agents/skills", "ls .claude/skills/"]}, []),
        ({"skills_invoked": ["frontend-design", "other:pdf"]}, []),
        ({"skill_files_read": ["/tmp/ws/.claude/skills/frontend-design/SKILL.md"]}, []),
        ({"bash_commands": ["head .claude/skills/authoring-fluid-contracts-extra/SKILL.md"]}, []),
        ({"bash_commands": ["cat AGENTS.md", "fluid validate contract.fluid.yaml"]}, []),
        ({}, []),
        (None, []),
    ],
)
def test_fired_skills(transcript: Any, expected: list) -> None:
    assert harness.fired_skills(transcript) == expected


def _control(scenario_id: str) -> harness.Scenario:
    return next(s for s in CONTROLS if s.id == scenario_id)


@pytest.mark.parametrize("scenario", CONTROLS, ids=[s.id for s in CONTROLS])
def test_a_controls_reference_passes_with_and_without_a_transcript(
    scenario: harness.Scenario, tmp_path: Path
) -> None:
    ws = _workspace(scenario, tmp_path)
    plain = harness.grade(scenario, ws)
    assert plain.valid and plain.passed and plain.fidelity == 1.0
    ids = [c.id for c in plain.checks]
    assert ids[0] == "contracts_untouched" and "no_skill_fired" not in ids
    assert "no_invented_email" not in ids, "a control has no contract to hold an email"
    watched = harness.grade(scenario, ws, {"bash_commands": ["ls", "head x.py"]})
    assert watched.passed and "no_skill_fired" in [c.id for c in watched.checks]


def test_a_contract_written_by_a_control_run_fails_contracts_untouched(tmp_path: Path) -> None:
    scenario = _control("control-orders-summary-script")
    ws = _workspace(scenario, tmp_path)
    (ws / "new.fluid.yaml").write_text("name: x\n", encoding="utf-8")
    graded = harness.grade(scenario, ws)
    assert _failed(graded) == ["contracts_untouched"]
    assert "new.fluid.yaml" in next(c.detail for c in graded.checks if not c.passed)
    assert graded.valid, "a contract nobody asked for is not an invalid expected one"
    assert not graded.passed


def test_a_changed_or_deleted_context_contract_fails_contracts_untouched(tmp_path: Path) -> None:
    scenario = _control("control-readme-typo")
    changed = _workspace(scenario, tmp_path / "changed")
    path = changed / "contract.fluid.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert _failed(harness.grade(scenario, changed)) == ["contracts_untouched"]
    deleted = _workspace(scenario, tmp_path / "deleted")
    (deleted / "contract.fluid.yaml").unlink()
    assert _failed(harness.grade(scenario, deleted)) == ["contracts_untouched"]
    assert harness._contract_changes(scenario, _workspace(scenario, tmp_path / "same")) == []


@pytest.mark.parametrize("hidden", [".agents/skills/x/references", ".claude/skills/y/refs"])
def test_a_skills_example_contract_in_a_dot_directory_is_not_a_contract_change(
    hidden: str, tmp_path: Path
) -> None:
    scenario = _control("control-orders-summary-script")
    ws = _workspace(scenario, tmp_path)
    (ws / hidden).mkdir(parents=True)
    (ws / hidden / "example.contract.fluid.yaml").write_text("name: x\n", encoding="utf-8")
    assert harness.grade(scenario, ws).passed


def test_a_fluid_skill_read_fails_no_skill_fired_only_with_a_transcript(tmp_path: Path) -> None:
    scenario = _control("control-duckdb-top-customers")
    ws = _workspace(scenario, tmp_path)
    read = {"bash_commands": [f"head .claude/skills/{SKILL}/SKILL.md"]}
    graded = harness.grade(scenario, ws, read)
    assert _failed(graded) == ["no_skill_fired"]
    assert SKILL in next(c.detail for c in graded.checks if not c.passed)
    assert _failed(harness.grade(scenario, ws, {"skills_invoked": [f"fluid:{SKILL}"]})) == [
        "no_skill_fired"
    ]
    assert harness.grade(scenario, ws, None).passed, "nothing ran: nothing to score"
    assert harness.grade(scenario, ws, {}).passed


def test_a_positive_scenario_never_gets_the_control_checks(tmp_path: Path) -> None:
    ws = _workspace(ORDERS, tmp_path)
    graded = harness.grade(ORDERS, ws, {"bash_commands": [f"head skills/{SKILL}/SKILL.md"]})
    assert graded.passed
    assert not {"contracts_untouched", "no_skill_fired"} & {c.id for c in graded.checks}


def test_in_file_checks_say_why_they_fail(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Hello World\nsecond line\n", encoding="utf-8")
    target = harness.Target({}, "", tmp_path)

    def run(**check: Any) -> tuple:
        return harness.run_check({"id": "c", "in_file": "a.txt", **check}, target)

    assert run(matches="hello world")[0] is True, "case-insensitive"
    assert run(matches="^second")[0] is True, "line anchors"
    assert run(matches="hello", not_matches="goodbye")[0] is True
    ok, detail = run(matches="absent")
    assert not ok and "absent" in detail
    ok, detail = run(not_matches="world")
    assert not ok and "has" in detail
    assert run(matches="hello", not_matches="second")[0] is False
    missing = harness.run_check({"id": "c", "in_file": "nope.txt", "matches": "x"}, target)
    assert missing == (False, "nope.txt missing")


def test_a_missing_file_for_an_in_file_check_fails_it_on_a_real_control(tmp_path: Path) -> None:
    scenario = _control("control-readme-typo")
    ws = harness.prepare_workspace(scenario, tmp_path / "ws")  # the typo is still there
    assert set(_failed(harness.grade(scenario, ws))) == {"typo_fixed", "typo_gone"}
    (ws / "README.md").unlink()
    assert set(_failed(harness.grade(scenario, ws))) == {"typo_fixed", "typo_gone", "rest_kept"}


def _ctl(scenario: str, arm: str, *, fired=False, touched=False, done=True, **extra: Any) -> dict:
    checks = [
        {"id": "contracts_untouched", "passed": not touched, "detail": ""},
        {"id": "no_skill_fired", "passed": not fired, "detail": ""},
        {"id": "script", "passed": done, "detail": ""},
    ]
    record = _rec(scenario, arm, passed=not (fired or touched or not done), **extra)
    record["grade"]["checks"] = checks
    record["control"] = True
    if fired:
        record["skills_invoked"] = [SKILL]
    return record


def _control_records() -> list:
    return [
        _ctl("c1", "skill"),
        _ctl("c1", "skill", fired=True),
        _ctl("c1", "skill", touched=True),
        _ctl("c2", "skill", done=False),
        _ctl("c2", "skill", timed_out=True, error=True),
        _ctl("c1", "no-skill"),
        _ctl("c2", "no-skill"),
    ]


def test_summarise_controls_counts_fired_touched_and_done() -> None:
    records = _control_records()
    assert harness.summarise_controls(records, "skill") == {
        "runs": 5,
        "fired": 1,
        "touched": 1,
        "done": 3,
    }
    assert harness.summarise_controls(records, "no-skill") == {
        "runs": 2,
        "fired": 0,
        "touched": 0,
        "done": 2,
    }
    assert harness.summarise_controls(records, "forge")["runs"] == 0
    assert harness.summarise(records, "skill")["runs"] == 0, "controls are not scenarios"


def test_the_report_has_a_negative_controls_table_and_marks_controls() -> None:
    records = _control_records() + _runs("a", "skill", "p")
    meta = _meta(arms=["no-skill", "skill"], scenarios=["a", "c1", "c2"], controls=["c1", "c2"])
    report = harness.render_report(meta, records)
    assert "## Negative controls" in report
    assert "| skill | 5 | 1/5 (20%) | 1/5 (20%) | 3/5 (60%) |" in report
    assert "| no-skill | 2 | 0/2 (0%) | 0/2 (0%) | 2/2 (100%) |" in report
    assert "plus 2 negative controls" in report
    assert "| c1 (control) |" in report and "| a |" in report
    scenario_rows = _table(report, "Scenario")
    assert set(scenario_rows) == {"a", "c1 (control)", "c2 (control)"}
    # The scoring table counts only the positive scenarios.
    assert _score_row(report, "skill")["Runs"] == "1"
    assert "## Negative controls" not in harness.render_report(_meta(), [_rec("a", "skill")])


# ---------------------------------------------------------------------------
# The Claude Code argv, the claude shim, effort
# ---------------------------------------------------------------------------


def test_the_claude_argv_pins_model_and_effort() -> None:
    argv = harness._agent_argv(harness.RunConfig(out=Path(".")), "do it")
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5" == harness.DEFAULT_MODEL
    assert argv[argv.index("--effort") + 1] == "medium" == harness.DEFAULT_EFFORT
    custom = harness._agent_argv(harness.RunConfig(out=Path("."), model="m", effort="max"), "x")
    assert custom[custom.index("--model") + 1] == "m"
    assert custom[custom.index("--effort") + 1] == "max"


def test_the_defaults_are_the_documented_ones() -> None:
    assert harness.DEFAULT_ARMS == ("no-skill", "skill", "forge")
    assert set(harness.DEFAULT_ARMS) <= set(harness.ARMS)
    assert harness.CONDITIONS == ("no-skill", "skill", "forge")
    assert {a.condition for a in harness.ARM_SPECS.values()} == set(harness.CONDITIONS)
    assert harness.ARM_SPECS["codex-skill"].scaffold == "generic"
    assert harness.ARM_SPECS["skill"].scaffold == "claude-code"
    assert harness.ARM_SPECS["codex-no-skill"].scaffold is None


def test_isolated_adds_effort_only_when_the_call_has_none() -> None:
    assert harness.isolated(["-p"], "low")[-2:] == ["--effort", "low"]
    assert "--effort" not in harness.isolated(["-p"])
    for kept in (["--effort", "high"], ["--effort=high"]):
        out = harness.isolated(["-p", *kept], "low")
        assert "low" not in out
        assert harness._flag_value(out, "--effort") == "high"
        assert sum(a.startswith("--effort") for a in out) == 1


def test_flag_value_reads_both_spellings() -> None:
    assert harness._flag_value(["--model", "a"], "--model") == "a"
    assert harness._flag_value(["--model=b"], "--model") == "b"
    assert harness._flag_value(["--model"], "--model") is None
    assert harness._flag_value([], "--model") is None


def _run_shim(tmp_path: Path, monkeypatch, argv: list, effort: Any) -> dict:
    fake = tmp_path / "fake_claude.py"
    fake.write_text(
        "import json, sys\nsys.stdin.read()\nprint(json.dumps({'num_turns': 1}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("prompt"))
    real_run = subprocess.run
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda a, **kw: real_run([sys.executable, str(fake), *a[1:]], **kw),
    )
    calls = tmp_path / "calls"
    assert harness.claude_shim("claude", str(calls), argv, effort) == 0
    (log,) = calls.glob("*.json")
    return json.loads(log.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "argv, effort, model, logged_effort",
    [
        (["-p", "--model", "m1"], "low", "m1", "low"),
        (["-p", "--model=m2", "--effort", "high"], "low", "m2", "high"),
        (["-p", "--effort=max"], "low", None, "max"),
        (["-p"], None, None, None),
    ],
)
def test_the_claude_shim_logs_the_model_and_effort_of_the_final_call(
    tmp_path: Path, monkeypatch, argv: list, effort: Any, model: Any, logged_effort: Any
) -> None:
    entry = _run_shim(tmp_path, monkeypatch, argv, effort)
    assert entry["model"] == model
    assert entry["effort"] == logged_effort
    assert entry["result"] == {"num_turns": 1}


def test_the_claude_shim_line_carries_the_effort_when_given(tmp_path: Path) -> None:
    pinned = harness._shims(tmp_path / "a", harness.REPO_ROOT, "/opt/claude", "high")
    assert "claude-shim --effort high /opt/claude " in (pinned / "claude").read_text("utf-8")
    plain = harness._shims(tmp_path / "b", harness.REPO_ROOT, "/opt/claude")
    text = (plain / "claude").read_text("utf-8")
    assert "claude-shim /opt/claude " in text and "--effort" not in text


def test_main_passes_the_shims_effort_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []

    def fake(real: str, log: str, argv: Any, effort: Any = None) -> int:
        seen.append((real, log, list(argv), effort))
        return 7

    monkeypatch.setattr(harness, "claude_shim", fake)
    assert harness.main(["claude-shim", "--effort", "low", "REAL", "LOG", "-p", "--x"]) == 7
    assert harness.main(["claude-shim", "REAL", "LOG", "-p"]) == 7
    assert seen == [("REAL", "LOG", ["-p", "--x"], "low"), ("REAL", "LOG", ["-p"], None)]


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--runs", "0"],
        ["run", "--runs", "-2"],
        ["run", "--effort", "ultra"],
        ["run", "--codex-provider", "bedrock"],
    ],
)
def test_the_run_command_rejects_bad_flags_before_running_anything(
    args: list, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(harness, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as exc:
        harness.main(args)
    assert exc.value.code == 2
    assert capsys.readouterr().err


def test_the_run_command_builds_its_config_from_the_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def fake_run(scenarios, arms, cfg, concurrency):
        seen.update(arms=arms, cfg=cfg, concurrency=concurrency)
        cfg.out.mkdir(parents=True, exist_ok=True)
        (cfg.out / "report.md").write_text("report", encoding="utf-8")
        return [{"grade": {}}]

    monkeypatch.setattr(harness, "run", fake_run)
    out = tmp_path / "out"
    argv = ["run", "--arms", "no-skill,codex-skill", "--scenarios", "sdp-local-orders"]
    argv += ["--runs", "3", "--effort", "high", "--model", "m", "--out", str(out)]
    argv += ["--codex-model", "gx", "--codex-effort", "low", "--codex-provider", "ollama"]
    assert harness.main(argv) == 0
    cfg = seen["cfg"]
    assert seen["arms"] == ["no-skill", "codex-skill"]
    assert (cfg.runs, cfg.effort, cfg.model) == (3, "high", "m")
    assert (cfg.codex_model, cfg.codex_effort, cfg.codex_provider) == ("gx", "low", "ollama")


# ---------------------------------------------------------------------------
# Codex isolation
# ---------------------------------------------------------------------------

FAKE_KEY = "sk-test-not-real"  # pragma: allowlist secret


def _codex_cfg(tmp_path: Path, **extra: Any) -> harness.RunConfig:
    defaults: Dict[str, Any] = {"codex_model": "gpt-test", "work_root": tmp_path / "work"}
    return harness.RunConfig(out=tmp_path / "out", **{**defaults, **extra})


def test_the_codex_env_is_the_runs_own_and_carries_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test-not-real")  # pragma: allowlist secret
    monkeypatch.setenv("CODEX_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("CODEX_HOME", "/operators/codex")
    run_dir = tmp_path / "run"
    bin_dir = run_dir / "bin"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in harness._child_env(bin_dir, Path("/src")), "premise"
    env = harness._codex_env(bin_dir, run_dir)
    assert env["HOME"] == str(run_dir / "home")
    assert env["TMPDIR"] == str(run_dir / "tmp")
    assert env["CODEX_HOME"] == str(run_dir / "codex-home")
    for name in ("home", "tmp", "codex-home"):
        assert ((run_dir / name).stat().st_mode & 0o777) == 0o700, name
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"):
        assert name not in env, name
    assert FAKE_KEY not in " ".join(env.values())
    assert env["PATH"].startswith(str(bin_dir))


def test_the_codex_argv(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    cfg = _codex_cfg(tmp_path)
    argv = harness._codex_argv(cfg, "do it", ws)
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv and "--ephemeral" in argv
    assert argv[argv.index("--cd") + 1] == str(ws)
    assert argv[argv.index("--model") + 1] == "gpt-test"
    assert 'model_reasoning_effort="medium"' in argv
    assert argv[argv.index('model_reasoning_effort="medium"') - 1] == "-c"
    assert argv[-2] == "--" and argv[-1].startswith("do it")
    assert argv[-1].endswith(harness.NON_INTERACTIVE)
    assert not any("model_provider" in a for a in argv), "openai is Codex's own default"
    ollama = harness._codex_argv(_codex_cfg(tmp_path, codex_provider="ollama"), "x", ws)
    assert ollama[ollama.index('model_provider="ollama"') - 1] == "-c"
    assert ollama[-2] == "--"
    high = harness._codex_argv(_codex_cfg(tmp_path, codex_effort="high"), "x", ws)
    assert 'model_reasoning_effort="high"' in high


def test_the_codex_config_is_toml_with_the_isolation_the_claude_arms_have(
    tmp_path: Path,
) -> None:
    tomllib = pytest.importorskip("tomllib")
    run_dir = tmp_path / "run"
    bin_dir = run_dir / "bin"
    bin_dir.mkdir(parents=True)
    readable = harness._codex_readable(bin_dir)
    doc = tomllib.loads(harness._codex_config(run_dir.resolve(), readable))
    assert doc["approval_policy"] == "never"
    assert doc["web_search"] == "disabled"
    assert doc["history"]["persistence"] == "none"
    assert doc["skills"]["bundled"]["enabled"] is False
    assert set(doc["features"]) == set(harness.CODEX_DISABLED_FEATURES)
    assert all(v is False for v in doc["features"].values())
    assert doc["default_permissions"] == harness.CODEX_PROFILE
    profile = doc["permissions"][harness.CODEX_PROFILE]
    assert profile["network"]["enabled"] is False
    files = profile["filesystem"]
    assert files[":workspace_roots"] == "write"
    assert files[str(run_dir.resolve() / "home")] == "write"
    assert files[str(run_dir.resolve() / "tmp")] == "write"
    paths = [Path(k).resolve() for k, v in files.items() if not k.startswith(":") and v == "read"]
    home, root = Path.home().resolve(), harness.REPO_ROOT.resolve()
    assert bin_dir.resolve() in paths and root / "fluid_build" in paths
    prefixes = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
    for path in paths:
        assert path != home and path not in home.parents, f"{path} exposes the home directory"
        # The checkout's own files (git-ignored settings, the reference solutions) stay out.
        assert path != root and path not in root.parents, f"{path} exposes the checkout"
        assert path in (bin_dir.resolve(), root / "fluid_build") or any(
            p == path or p in path.parents for p in prefixes
        ), f"{path} is neither fluid_build nor the interpreter's"
    denied = {k for k, v in files.items() if v == "deny"}
    assert str(run_dir.resolve() / "codex-home") in denied
    assert any(k.endswith("codex-home/*") for k in denied), "every sibling run's API key too"
    assert str(root / "fluid_build" / "agent_skills") in denied, "no-skill must not find skills"


def _temp_denies(run_dir: Path) -> set:
    tomllib = pytest.importorskip("tomllib")
    doc = tomllib.loads(harness._codex_config(run_dir, []))
    files = doc["permissions"][harness.CODEX_PROFILE]["filesystem"]
    return {k for k, v in files.items() if v == "deny" and k.endswith("/**")}


def test_the_shared_temp_dirs_are_denied_unless_they_hold_the_run() -> None:
    """Other sessions' scratch files, checkout copies and a concurrent harness's runs live
    in /tmp and /var/tmp. A run inside one keeps that one (macOS puts mkdtemp under
    /var/folders, Linux under /tmp); the README says to keep TMPDIR out of /tmp."""
    tmp, var_tmp = (str(Path(p).resolve() / "**") for p in ("/tmp", "/var/tmp"))
    elsewhere = Path("/srv/evals/w/s/codex-skill/run-1")
    assert _temp_denies(elsewhere) == {tmp, var_tmp}
    under_tmp = Path("/tmp").resolve() / "w" / "s" / "codex-skill" / "run-1"
    assert _temp_denies(under_tmp) == {var_tmp}, "the run's own temp root stays"


def test_a_codex_arm_imports_fluid_from_a_directory_holding_only_the_package(
    tmp_path: Path,
) -> None:
    """Python lists a PYTHONPATH entry to import from it, so PYTHONPATH must not be the
    checkout: the checkout would then have to be readable to the agent's commands."""
    bin_dir = tmp_path / "run" / "bin"
    env = harness._codex_env(bin_dir, tmp_path / "run")
    src = Path(env["PYTHONPATH"])
    assert src == bin_dir / "src"
    assert [p.name for p in src.iterdir()] == ["fluid_build"]
    assert (src / "fluid_build").resolve() == harness.REPO_ROOT.resolve() / "fluid_build"
    shim_src = harness._shims(tmp_path / "run", src, None) / "fluid"
    assert f"PYTHONPATH={src}" in shim_src.read_text(encoding="utf-8")


def test_the_grader_never_follows_a_link_out_of_the_workspace(tmp_path: Path) -> None:
    """An agent with a shell can link a contract to a secret; the grader runs as the
    operator, outside any sandbox, and must not read or validate it."""
    secret = tmp_path / "secret.txt"
    secret.write_text("aws_secret_access_key = not-a-real-key\n", encoding="utf-8")
    ws = harness.prepare_workspace(ORDERS, tmp_path / "ws")
    (ws / "contract.fluid.yaml").symlink_to(secret)
    (ws / "notes.md").symlink_to(secret)
    graded = harness.grade(ORDERS, ws)
    assert not graded.valid
    assert "leaves the workspace" in graded.validate_output
    assert "not-a-real-key" not in json.dumps(graded.to_dict())
    target = harness.Target({}, "", ws)
    assert harness.run_check({"id": "x", "in_file": "notes.md", "matches": "key"}, target) == (
        False,
        "notes.md leaves the workspace",
    )
    assert harness.run_check({"id": "y", "exists_file": "notes.md"}, target)[0] is False
    (ws / "inside.md").write_text("a key\n", encoding="utf-8")
    (ws / "link.md").symlink_to(ws / "inside.md")
    assert harness.run_check({"id": "z", "in_file": "link.md", "matches": "key"}, target)[0]


def test_the_codex_rules_forbid_exactly_the_stages_the_claude_arms_lack() -> None:
    rules = harness._codex_rules().splitlines()
    pattern = re.compile(
        r'^prefix_rule\(pattern = \["fluid", "([a-z-]+)"\], decision = "forbidden", '
        r'justification = "fluid \1 is not allowed in this eval\."\)$'
    )
    stages = [m.group(1) for m in map(pattern.match, rules) if m]
    assert len(stages) == len(rules), rules
    assert sorted(stages) == sorted(harness.CODEX_FORBIDDEN_STAGES)
    for stage in harness.CODEX_FORBIDDEN_STAGES:
        assert not any(f"fluid {stage}" in tool for tool in harness.ALLOWED_TOOLS), stage
    assert all('decision = "forbidden"' in rule for rule in rules)


def test_the_codex_home_has_a_config_and_rules(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    bin_dir = harness._shims(run_dir, harness.REPO_ROOT, None)
    home = harness._write_codex_home(run_dir, bin_dir)
    assert home == run_dir / "codex-home"
    assert (home / "config.toml").read_text("utf-8").startswith("# One eval run's Codex config")
    rules = home / "rules" / f"{harness.CODEX_PROFILE}.rules"
    assert rules.read_text("utf-8") == harness._codex_rules()
    assert not (home / "auth.json").exists()


# ---------------------------------------------------------------------------
# Codex output
# ---------------------------------------------------------------------------

VALIDATE = "/bin/zsh -lc 'fluid validate contract.fluid.yaml --offline'"
APPLY = "/bin/zsh -lc 'fluid apply contract.fluid.yaml'"
CODEX_REJECTION = (
    "2026-11-13T18:16:12.746710Z ERROR codex_core::tools::router: error=exec_command failed: "
    'CreateProcess { message: "Rejected(\\"`' + APPLY + "` rejected: fluid apply is not allowed "
    'in this eval.\\")" }\n'
)


def _codex_lines(*, tail: tuple = ()) -> list:
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {
            "type": "item.completed",
            "item": {"id": "i0", "type": "error", "message": "Model metadata for x not found"},
        },
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "think"}},
        {
            "type": "item.started",
            "item": {
                "id": "i2",
                "type": "command_execution",
                "command": VALIDATE,
                "aggregated_output": "",
                "exit_code": None,
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "i2",
                "type": "command_execution",
                "command": VALIDATE,
                "aggregated_output": "ok",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "DONE"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 16025,
                "cached_input_tokens": 8000,
                "cache_write_input_tokens": 0,
                "output_tokens": 378,
                "reasoning_output_tokens": 0,
            },
        },
        *tail,
    ]
    return [json.dumps(e) for e in events] + ["not json", ""]


def test_parse_codex_stream_reads_commands_rejections_and_usage() -> None:
    info = harness.parse_codex_stream(_codex_lines(), CODEX_REJECTION)
    assert info["bash_commands"] == [VALIDATE, APPLY]
    assert info["rejected_commands"] == [APPLY]
    assert info["permission_denials"] == 1
    assert info["tools_used"] == {"command_execution": 1}
    assert info["result"] == {"is_error": False, "error": None}, "an error item is a warning"
    usage = info["usage"]
    assert (usage["input_tokens"], usage["cache_read_tokens"], usage["output_tokens"]) == (
        8025,
        8000,
        378,
    )
    assert usage["cost_usd"] is None and usage["turns"] is None
    assert info["skills_invoked"] == [] and info["skill_files_read"] == []


def test_parse_codex_stream_without_a_rejection_has_no_denials() -> None:
    info = harness.parse_codex_stream(_codex_lines())
    assert info["bash_commands"] == [VALIDATE]
    assert info["rejected_commands"] == [] and info["permission_denials"] == 0


@pytest.mark.parametrize(
    "tail",
    [
        ({"type": "turn.failed", "error": {"message": "boom"}},),
        ({"type": "error", "message": "boom"},),
    ],
)
def test_a_fatal_codex_event_makes_the_run_an_error(tail: tuple) -> None:
    info = harness.parse_codex_stream(_codex_lines(tail=tail))
    assert info["result"] == {"is_error": True, "error": "boom"}


def test_codex_usage_sums_turns_and_never_reports_a_zero_cost() -> None:
    usage = harness.codex_usage(
        [
            {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2},
            {"input_tokens": 5, "cache_write_input_tokens": 3, "reasoning_output_tokens": 1},
        ]
    )
    assert usage["input_tokens"] == 11 and usage["cache_read_tokens"] == 4
    assert usage["cache_creation_tokens"] == 3 and usage["reasoning_tokens"] == 1
    assert usage["cost_usd"] is None and usage["turns"] is None
    assert harness.codex_usage([])["input_tokens"] == 0


def test_a_rejected_fluid_apply_fails_no_apply_as_a_denied_claude_call_does(
    tmp_path: Path,
) -> None:
    info = harness.parse_codex_stream(_codex_lines(), CODEX_REJECTION)
    ok, detail = harness._check_transcript({"transcript": "no_apply"}, info)
    assert not ok and "fluid apply" in detail
    clean = harness.parse_codex_stream(_codex_lines())
    assert harness._check_transcript({"transcript": "no_apply"}, clean)[0]
    scenario = next(s for s in SCENARIOS if s.id == "deploy-plan-only")
    ws = _workspace(scenario, tmp_path)
    assert "not_applied" in _failed(harness.grade(scenario, ws, info))
    assert "not_applied" not in _failed(harness.grade(scenario, ws, clean))


# ---------------------------------------------------------------------------
# Codex login and a whole Codex run, against a fake codex
# ---------------------------------------------------------------------------

FAKE_CODEX = """\
import json, os, shutil, sys
log = {log!r}
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as f:
    entry = {{"argv": args, "env_keys": sorted(os.environ), "stdin": None}}
    if args[:1] == ["login"]:
        entry["stdin"] = sys.stdin.read()
    f.write(json.dumps(entry) + "\\n")
if args == ["--version"]:
    print("codex-cli 0.test")
elif args[:1] == ["login"]:
    if {fail!r}:
        sys.stderr.write("rejected key " + entry["stdin"] + "\\n")
        sys.exit(1)
    with open(os.path.join(os.environ["CODEX_HOME"], "auth.json"), "w") as f:
        f.write("{{}}")
elif args[:1] == ["exec"]:
    ws = args[args.index("--cd") + 1]
    with open(log + ".exec", "w", encoding="utf-8") as f:
        json.dump(
            {{
                "auth": os.path.exists(os.path.join(os.environ["CODEX_HOME"], "auth.json")),
                "home": os.environ["HOME"],
                "cwd": os.getcwd(),
                "argv": args,
            }},
            f,
        )
    if {reference!r}:
        shutil.copy({reference!r}, os.path.join(ws, "contract.fluid.yaml"))
    for line in {lines!r}:
        print(line)
"""


def _fake_codex(tmp_path: Path, *, fail: bool = False, reference: Any = None) -> tuple:
    log = tmp_path / "codex.log"
    script = tmp_path / "fake_codex.py"
    script.write_text(
        FAKE_CODEX.format(
            log=str(log),
            fail=fail,
            reference=str(reference) if reference else "",
            lines=_codex_lines(),
        ),
        encoding="utf-8",
    )
    shim = tmp_path / "shim" / "codex"
    harness._write_shim(
        shim, [f'exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"']
    )
    return shim, log


def _calls(log: Path) -> list:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_the_codex_login_sends_the_key_on_stdin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", FAKE_KEY)
    shim, log = _fake_codex(tmp_path)
    cfg = _codex_cfg(tmp_path, codex=str(shim))
    run_dir = tmp_path / "run"
    env = harness._codex_env(run_dir / "bin", run_dir)
    harness._codex_login(cfg, env, run_dir)
    (call,) = _calls(log)
    assert call["argv"] == ["login", "--with-api-key"]
    assert call["stdin"] == FAKE_KEY
    assert FAKE_KEY not in " ".join(call["argv"])
    assert not {"CODEX_API_KEY", "OPENAI_API_KEY"} & set(call["env_keys"])
    assert (run_dir / "codex-home" / "auth.json").is_file()


def test_the_codex_login_prefers_codex_api_key_and_falls_back_to_openai_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert harness._codex_api_key() is None
    monkeypatch.setenv("OPENAI_API_KEY", "second")
    assert harness._codex_api_key() == "second"
    monkeypatch.setenv("CODEX_API_KEY", "first")
    assert harness._codex_api_key() == "first"
    monkeypatch.setenv("CODEX_API_KEY", "")
    assert harness._codex_api_key() == "second"


def test_a_failed_codex_login_never_repeats_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", FAKE_KEY)
    shim, _ = _fake_codex(tmp_path, fail=True)
    cfg = _codex_cfg(tmp_path, codex=str(shim))
    run_dir = tmp_path / "run"
    env = harness._codex_env(run_dir / "bin", run_dir)
    with pytest.raises(RuntimeError, match=r"codex login failed \(1\)") as exc:
        harness._codex_login(cfg, env, run_dir)
    assert FAKE_KEY not in str(exc.value) and "***" in str(exc.value)
    monkeypatch.delenv("CODEX_API_KEY")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="no CODEX_API_KEY"):
        harness._codex_login(cfg, env, run_dir)


def _codex_record(tmp_path: Path, arm: str, monkeypatch: pytest.MonkeyPatch, **cfg_extra: Any):
    monkeypatch.setenv("CODEX_API_KEY", FAKE_KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(harness, "_git_head", lambda path: "abc123")
    shim, log = _fake_codex(tmp_path, reference=ORDERS.reference_dir / "contract.fluid.yaml")
    cfg = _codex_cfg(tmp_path, codex=str(shim), codex_effort="high", **cfg_extra)
    stamp = harness._arm_stamps(cfg, [arm], harness._run_meta(cfg))[arm]
    record = harness.run_one(ORDERS, arm, cfg, stamp, trial=2)
    return record, Path(record["run_dir"]), log


def test_a_codex_arm_runs_end_to_end_against_a_fake_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, run_dir, log = _codex_record(tmp_path, "codex-no-skill", monkeypatch)
    assert "harness_error" not in record, record
    assert (record["scenario"], record["arm"], record["trial"]) == (ORDERS.id, "codex-no-skill", 2)
    assert run_dir.name == "run-2"
    assert record["grade"]["passed"] and record["grade"]["valid"]
    assert record["error"] is False and "control" not in record
    assert record["bash_commands"] == [VALIDATE]
    assert record["usage"]["input_tokens"] == 8025 and record["usage"]["cost_usd"] is None
    meta = record["run_meta"]
    assert set(harness.RUN_META_KEYS) <= set(meta)
    assert (meta["agent"], meta["provider"], meta["model"]) == ("codex", "openai", "gpt-test")
    assert (meta["effort"], meta["agent_version"]) == ("high", "codex-cli 0.test")
    assert meta["model_reported"] is None
    codex_home = run_dir / "codex-home"
    assert not (codex_home / "auth.json").exists(), "the login does not outlive the run"
    assert (codex_home / "config.toml").is_file()
    assert (codex_home / "rules" / f"{harness.CODEX_PROFILE}.rules").is_file()
    assert (run_dir / "transcript.jsonl").is_file() and (run_dir / "result.json").is_file()
    seen = json.loads(log.with_name("codex.log.exec").read_text(encoding="utf-8"))
    assert seen["auth"] is True, "logged in before it ran"
    assert seen["home"] == str(run_dir / "home")
    assert seen["cwd"] == str(run_dir / "workspace")
    assert "--ephemeral" in seen["argv"] and "gpt-test" in seen["argv"]
    exec_call = next(c for c in _calls(log) if c["argv"][:1] == ["exec"])
    assert not {"CODEX_API_KEY", "OPENAI_API_KEY"} & set(exec_call["env_keys"])
    assert not (run_dir / "workspace" / ".agents").exists(), "no skills in the no-skill arm"


def test_the_codex_skill_arm_installs_the_skills_and_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, run_dir, _ = _codex_record(tmp_path, "codex-skill", monkeypatch)
    assert "harness_error" not in record, record
    ws = run_dir / "workspace"
    assert (ws / ".agents" / "skills").is_dir()
    assert (ws / "AGENTS.md").is_file()
    assert "grade" in record and record["run_meta"]["agent"] == "codex"
    assert record["arm"] == "codex-skill"


def test_the_ollama_provider_logs_in_nowhere_and_needs_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(harness, "_git_head", lambda path: "abc123")
    shim, log = _fake_codex(tmp_path, reference=ORDERS.reference_dir / "contract.fluid.yaml")
    cfg = _codex_cfg(tmp_path, codex=str(shim), codex_provider="ollama")
    stamp = harness._arm_stamps(cfg, ["codex-no-skill"], harness._run_meta(cfg))["codex-no-skill"]
    record = harness.run_one(ORDERS, "codex-no-skill", cfg, stamp)
    assert record["grade"]["passed"] and record["run_meta"]["provider"] == "ollama"
    assert not any(c["argv"][:1] == ["login"] for c in _calls(log))
    exec_call = next(c for c in _calls(log) if c["argv"][:1] == ["exec"])
    assert 'model_provider="ollama"' in exec_call["argv"]


def test_a_codex_run_that_cannot_log_in_is_a_harness_error_and_leaves_no_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", FAKE_KEY)
    shim, _ = _fake_codex(tmp_path, fail=True)
    record = harness.run_one(ORDERS, "codex-no-skill", _codex_cfg(tmp_path, codex=str(shim)))
    assert "grade" not in record and "codex login failed" in record["harness_error"]
    assert FAKE_KEY not in json.dumps(record)
    assert not list((tmp_path / "work").rglob("auth.json"))


# ---------------------------------------------------------------------------
# preflight, and the per-arm run meta
# ---------------------------------------------------------------------------


def test_preflight_names_each_thing_a_codex_arm_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = harness.RunConfig(out=tmp_path, codex="no-such-codex-binary-for-this-test")
    with pytest.raises(SystemExit) as exc:
        harness.preflight(["codex-skill"], cfg)
    message = str(exc.value)
    assert message.startswith("cannot run: ")
    assert "not on PATH" in message and "no-such-codex-binary" in message
    assert "--codex-model" in message
    assert "CODEX_API_KEY" in message and "ollama" in message


def test_preflight_passes_a_fully_set_up_codex_arm_and_needs_no_key_for_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    shim, _ = _fake_codex(tmp_path)
    ollama = harness.RunConfig(
        out=tmp_path, codex=str(shim), codex_model="m", codex_provider="ollama"
    )
    harness.preflight(["codex-no-skill", "codex-skill"], ollama)
    openai = harness.RunConfig(out=tmp_path, codex=str(shim), codex_model="m")
    with pytest.raises(SystemExit, match="CODEX_API_KEY"):
        harness.preflight(["codex-skill"], openai)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    harness.preflight(["codex-skill"], openai)


def test_preflight_lets_the_default_arms_run_with_no_codex_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = harness.RunConfig(out=tmp_path, codex="no-such-codex-binary-for-this-test")
    harness.preflight(harness.DEFAULT_ARMS, cfg)


def test_preflight_refuses_zero_runs_and_unknown_arms(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--runs must be 1 or more"):
        harness.preflight(["skill"], harness.RunConfig(out=tmp_path, runs=0))
    with pytest.raises(SystemExit, match="unknown arm"):
        harness.preflight(["skil"], harness.RunConfig(out=tmp_path))


def test_each_arm_is_stamped_with_its_own_agent_version_model_and_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "_git_head", lambda path: "abc123")
    monkeypatch.setattr(harness, "_claude_version", lambda claude: "2.1.9")
    monkeypatch.setattr(harness, "_codex_version", lambda codex: "0.9.9")
    cfg = harness.RunConfig(
        out=tmp_path,
        model="opus-x",
        effort="high",
        codex_model="gx",
        codex_effort="low",
        codex_provider="ollama",
    )
    common = harness._run_meta(cfg)
    assert set(common) == {"forge_src", "forge_src_commit", "skills_commit"}
    stamps = harness._arm_stamps(cfg, ["skill", "forge", "codex-skill"], common)
    for arm in ("skill", "forge"):
        assert stamps[arm] == {
            **common,
            "agent": "claude-code",
            "agent_version": "2.1.9",
            "model": "opus-x",
            "effort": "high",
        }
    assert stamps["codex-skill"] == {
        **common,
        "agent": "codex",
        "agent_version": "0.9.9",
        "provider": "ollama",
        "model": "gx",
        "effort": "low",
    }
    assert all(set(harness.RUN_META_KEYS) <= set(s) for s in stamps.values())


def test_a_version_is_only_looked_up_for_an_agent_that_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "_git_head", lambda path: "abc123")
    monkeypatch.setattr(harness, "_claude_version", lambda claude: "2.1.9")
    monkeypatch.setattr(
        harness, "_codex_version", lambda codex: pytest.fail("no codex arm is running")
    )
    cfg = harness.RunConfig(out=tmp_path)
    stamps = harness._arm_stamps(cfg, ["no-skill"], {})
    assert stamps["no-skill"]["agent_version"] == "2.1.9"
    assert "provider" not in stamps["no-skill"]
    monkeypatch.setattr(harness, "_codex_version", lambda codex: "0.9.9")
    monkeypatch.setattr(
        harness, "_claude_version", lambda claude: pytest.fail("no claude arm is running")
    )
    assert harness._arm_stamps(cfg, ["codex-no-skill"], {})["codex-no-skill"]["agent"] == "codex"


def test_a_missing_cli_has_an_unknown_version() -> None:
    assert harness._cli_version("no-such-binary-for-this-test") == "unknown"


# ---------------------------------------------------------------------------
# Defect-scan fixes: skill reads by glob or cd, recovered Codex errors, labels
# ---------------------------------------------------------------------------

AUTHORING = "authoring-fluid-contracts"


@pytest.mark.parametrize(
    "command, installed, expected",
    [
        ("cat .agents/skills/*/SKILL.md", None, list(harness.FLUID_SKILLS)),
        ("head .claude/skills/*/SKILL.md", [AUTHORING], [AUTHORING]),
        ("cat .agents/skills/*/SKILL.md", [], []),  # no skill installed: nothing read
        (f"cd .agents/skills && cat {AUTHORING}/SKILL.md", None, [AUTHORING]),
        (
            "sed -n 1,40p composing-fluid-data-products/references/x.md",
            None,
            ["composing-fluid-data-products"],
        ),
        ("cat AGENTS.md", None, []),
    ],
)
def test_a_skill_read_by_glob_or_after_cd_fires(
    command: str, installed: Any, expected: list
) -> None:
    assert harness.fired_skills({"bash_commands": [command]}, installed) == expected


def test_a_glob_read_fails_a_control_only_where_skills_are_installed(tmp_path: Path) -> None:
    control = next(s for s in SCENARIOS if s.id == "control-orders-summary-script")
    transcript = {"bash_commands": ["cat .agents/skills/*/SKILL.md"]}
    bare = _workspace(control, tmp_path / "bare")
    checks = {c.id: c for c in harness.grade(control, bare, transcript).checks}
    assert checks["no_skill_fired"].passed, "a no-skill workspace has no skill to read"
    skilled = _workspace(control, tmp_path / "skilled")
    (skilled / ".agents" / "skills" / AUTHORING).mkdir(parents=True)
    (skilled / ".agents" / "skills" / AUTHORING / "SKILL.md").write_text("x", encoding="utf-8")
    checks = {c.id: c for c in harness.grade(control, skilled, transcript).checks}
    assert not checks["no_skill_fired"].passed


@pytest.mark.parametrize(
    "events, is_error",
    [
        ([{"type": "error", "message": "Reconnecting"}, {"type": "turn.completed"}], False),
        ([{"type": "turn.completed"}, {"type": "error", "message": "stream lost"}], True),
        ([{"type": "turn.failed", "error": {"message": "x"}}, {"type": "turn.completed"}], True),
        (
            [
                {"type": "turn.failed", "error": {"message": "x"}},
                {"type": "error", "message": "y"},
                {"type": "turn.completed"},
            ],
            True,
        ),
    ],
)
def test_a_codex_error_a_completed_turn_follows_was_recovered_from(
    events: list, is_error: bool
) -> None:
    lines = [json.dumps(e) for e in events]
    assert harness.parse_codex_stream(lines)["result"]["is_error"] is is_error


def test_a_run_that_never_reported_its_model_is_not_a_model_mismatch() -> None:
    stamp = {"agent": "claude-code", "model": "claude-sonnet-5-5", "effort": "medium"}
    records = [
        _rec("a", "skill", run_meta={**stamp, "model_reported": "claude-sonnet-5-5"}),
        _rec("b", "skill", run_meta={**stamp, "model_reported": "claude-sonnet-5-5"}),
        _rec("c", "skill", timed_out=True, run_meta={**stamp, "model_reported": None}),
    ]
    (row,) = [
        r for r in harness._arms_table(_meta(), records, ["skill"]) if r.startswith("| skill")
    ]
    assert "| claude-sonnet-5-5 |" in row and "ran" not in row


def test_an_arm_whose_every_run_errored_is_flagged_under_the_results() -> None:
    records = [_rec("a", "skill")] + [
        {"scenario": s, "arm": "forge", "harness_error": "refused"} for s in ("a", "b")
    ]
    report = harness.render_report(_meta(arms=["skill", "forge"]), records)
    assert "- forge: all 2 runs errored (see Failed checks)" in report
    assert "- skill: all" not in report


def test_preflight_refuses_an_effort_or_provider_the_agent_would_reject(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--effort must be one of"):
        harness.preflight(["skill"], harness.RunConfig(out=tmp_path, effort="ultra"))
    cfg = harness.RunConfig(out=tmp_path, codex_model="m", codex_provider="Ollama")
    with pytest.raises(SystemExit, match="--codex-provider must be one of"):
        harness.preflight(["codex-skill"], cfg)


def test_a_scenario_naming_an_arm_instead_of_a_condition_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad-scenario"
    bad.mkdir()
    (bad / "prompt.md").write_text("do it", encoding="utf-8")
    (bad / "scenario.yaml").write_text("title: t\narms: [codex-skill]\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="arms names conditions"):
        harness.load_scenario(bad)


def test_completing_an_old_results_file_keeps_its_runs_own_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A results.json from before records carried run_meta: its runs keep the model and
    commits their own run recorded, not the completing run's."""
    out = tmp_path / "out"
    out.mkdir()
    old_meta = _meta(model="sonnet", skills_commit="old0", notes="first batch")
    old = {"meta": old_meta, "runs": [_rec("deploy-plan-only", "skill")]}
    (out / "results.json").write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(harness, "_git_head", lambda path: "new1")
    monkeypatch.setattr(harness, "_claude_version", lambda claude: "2.1.322")
    monkeypatch.setattr(
        harness,
        "run_one",
        lambda s, arm, cfg, stamp=None, trial=1: {**_rec(s.id, arm), "run_meta": dict(stamp)},
    )
    harness.run([ORDERS], ["skill"], harness.RunConfig(out=out), 1)
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    by_scenario = {r["scenario"]: r for r in data["runs"]}
    assert by_scenario["deploy-plan-only"]["run_meta"]["skills_commit"] == "old0"
    assert by_scenario["deploy-plan-only"]["run_meta"]["model"] == "sonnet"
    assert by_scenario[ORDERS.id]["run_meta"]["skills_commit"] == "new1"
    assert data["meta"]["notes"] == "first batch"


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups")
def test_nothing_an_agent_left_running_outlives_its_run(tmp_path: Path) -> None:
    """A background process could rewrite the workspace while it is graded."""
    stdout, _, code, timed_out = harness._run_bounded(
        ["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=30,
    )
    assert (code, timed_out) == (0, False)
    pid = int(stdout.strip())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "the leftover sleep must be killed with the run"


def test_the_grader_reads_the_resolved_path_it_checked(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real.md").write_text("x", encoding="utf-8")
    (ws / "link.md").symlink_to(ws / "real.md")
    assert harness._in_workspace(ws, "link.md") == (ws / "real.md").resolve()
