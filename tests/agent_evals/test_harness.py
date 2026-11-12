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
        lambda s, arm, cfg, stamp=None: {**_rec(s.id, arm), "run_meta": dict(stamp or {})},
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
    assert "Model: mixed: opus (1 runs), sonnet (1 runs);" in report
    assert "Claude Code 2.1.1" in report


def test_the_header_shows_one_value_when_records_agree_and_falls_back_to_meta() -> None:
    meta = _meta(model="sonnet")
    assert "Model: sonnet;" in harness.render_report(meta, [_rec("a", "skill")])
    one = _rec("a", "skill", run_meta={"model": "opus"})
    two = _rec("b", "skill")  # unstamped: the meta value
    assert "Model: mixed: opus (1 runs), sonnet (1 runs);" in harness.render_report(
        meta, [one, two]
    )


def test_what_the_stream_reported_is_not_a_run_meta_value() -> None:
    """An agent record's own ``model`` is the stream's model id, not the alias run with."""
    meta = _meta(model="sonnet", claude_code_version="2.1.322 (Claude Code)")
    agent = _rec("a", "skill", model="claude-sonnet-5-5", claude_code_version="2.1.322")
    report = harness.render_report(meta, [agent, _rec("b", "forge")])
    assert "Model: sonnet; Claude Code 2.1.322 (Claude Code)" in report


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
