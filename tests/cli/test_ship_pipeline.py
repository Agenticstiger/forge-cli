# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship`` as the canonical 11-stage pipeline runner.

Five layers:

1. Parity: the shell bodies generated CI files ran (``_stage_specs``) are run
   against a fake ``fluid`` and the argv they record must equal the argv ship
   builds, stage by stage. This is what keeps the two from drifting.
2. Unit: stage selection, flag/env precedence, skips, preconditions, exit codes.
3. CI-native output for GitHub, GitLab, Azure and plain.
4. The JSON and JUnit run reports.
5. End to end with the real CLI and the local provider.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

import pytest

import fluid_build
from fluid_build.cli import _ci_output, ship
from fluid_build.cli import _ship_stages as st
from fluid_build.forge.core.pipeline_systems._base import (
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
)
from fluid_build.forge.core.pipeline_systems.github_actions import GitHubActionsTemplate

LOG = logging.getLogger("test_ship")

_ENV_VARS = (
    "FLUID_ENV",
    "APPLY_MODE",
    "APPLY_BUILD_ID",
    "ALLOW_DATA_LOSS",
    "NO_VERIFY_DIGEST",
    "PUBLISH_TARGETS",
    "SCHEDULER",
    "SCHEDULER_DESTINATION",
    "SCHEDULER_ENVIRONMENT_NAME",
    "SCHEDULER_LOCATION",
    "SCHEDULER_WORKSPACE",
    "SCHEDULE_SYNC_DRY_RUN",
    "GITHUB_ACTIONS",
    "GITHUB_STEP_SUMMARY",
    "GITLAB_CI",
    "TF_BUILD",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FLUID_RUN_ID", raising=False)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract.fluid.yaml").write_text("fluidVersion: '0.7.5'\nid: x.y\n")
    return tmp_path


def parse(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    ship.register(sub)
    return parser.parse_args(["ship", *argv])


class FakeRun:
    """Stands in for ``subprocess.run``: records stage argv, writes what a real
    stage would leave behind, returns the exit code configured for it."""

    def __init__(self, rcs: Optional[Dict[str, int]] = None) -> None:
        self.rcs = rcs or {}
        self.calls: List[List[str]] = []
        self.envs: List[Dict[str, str]] = []
        self.stderrs: List[object] = []

    def __call__(self, cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        assert cmd[1:3] == ["-m", "fluid_build.cli"]
        argv = cmd[3:]
        self.calls.append(argv)
        env = kwargs.get("env")
        assert isinstance(env, dict)
        self.envs.append(env)
        self.stderrs.append(kwargs.get("stderr"))
        stage = argv[0]
        if stage == "bundle":
            Path("runtime").mkdir(exist_ok=True)
            Path("runtime/bundle.tgz").write_bytes(b"x")
        elif stage == "plan":
            Path("runtime/plan.json").write_text("{}")
        elif stage == "generate":
            Path("dist/artifacts").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, self.rcs.get(stage, 0))

    @property
    def stages(self) -> List[str]:
        return [c[1] if c[0] == "generate" else c[0] for c in self.calls]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    runner = FakeRun()
    monkeypatch.setattr(ship.subprocess, "run", runner)
    return runner


def ship_run(argv: List[str]) -> int:
    return ship.run(parse(argv), LOG)


# ---------------------------------------------------------------------------
# 1. Parity with _stage_specs
# ---------------------------------------------------------------------------

_FAKE_FLUID = '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$FAKE_DIR/stage-$$.argv"\n'

_CASES = {
    "amend": dict(mode="amend", build_id="", targets=["datamesh-manager"]),
    "dry-run": dict(mode="dry-run", build_id="", targets=["datamesh-manager"]),
    "amend-and-build": dict(
        mode="amend-and-build",
        build_id="build_one",
        targets=["datamesh-manager", "openmetadata"],
        allow_data_loss=True,
        no_verify_digest=True,
        scheduler="airflow",
        scheduler_destination="/opt/airflow/dags",
        scheduler_environment_name="env-a",
        scheduler_location="eu-west1",
        scheduler_workspace="ws-1",
        schedule_sync_dry_run=True,
        env="staging",
    ),
    "amend-with-ignored-build-id": dict(
        mode="amend", build_id="build_one", scheduler="airflow", targets=["datamesh-manager"]
    ),
    # A pipeline that applies elsewhere: the shell's RUN_STAGE_7_APPLY=false,
    # ship's --skip-stage apply. A dry-run mode then skips nothing after stage 7.
    "dry-run-without-apply": dict(
        mode="dry-run",
        build_id="",
        scheduler="airflow",
        targets=["datamesh-manager"],
        apply_in_pipeline=False,
    ),
}


def _shell_argv(
    tmp: Path, body: str, env: Dict[str, str], bin_dir: Path, num: int
) -> Optional[List[str]]:
    record = tmp / f"rec-{num}"
    shutil.rmtree(record, ignore_errors=True)
    record.mkdir()
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "FAKE_DIR": str(record),
        "HOME": str(tmp),
        **env,
    }
    done = subprocess.run(["sh", "-c", body], cwd=tmp, env=full_env, capture_output=True, text=True)
    assert done.returncode == 0, f"stage {num} shell failed: {done.stderr}"
    files = list(record.glob("stage-*.argv"))
    if not files:
        return None
    assert len(files) == 1
    return files[0].read_text().splitlines()


@pytest.mark.parametrize("case", sorted(_CASES))
def test_ship_argv_equals_the_generated_pipeline_shell(case: str, tmp_path: Path) -> None:
    c = dict(_CASES[case])
    env_name = str(c.pop("env", "dev"))
    mode = str(c["mode"])
    config = PipelineConfig(
        provider=PipelineProvider.GITHUB_ACTIONS,
        complexity=PipelineComplexity.BASIC,
        contract_path="contract.fluid.yaml",
        fluid_env_default=env_name,
        apply_mode_default=mode,
        apply_build_id_default=str(c["build_id"]),
        scheduler_default=str(c.get("scheduler", "")),
    )
    specs = GitHubActionsTemplate()._stage_specs(config)
    assert [s.num for s in specs] == list(range(1, 12))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fluid = bin_dir / "fluid"
    fluid.write_text(_FAKE_FLUID)
    fluid.chmod(0o755)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/bundle.tgz").write_bytes(b"x")
    (tmp_path / "runtime/plan.json").write_text("{}")
    (tmp_path / "dist/artifacts/policy").mkdir(parents=True)
    (tmp_path / "dist/artifacts/policy/bindings.json").write_text("{}")
    (tmp_path / "dist/artifacts/schedule").mkdir()
    (tmp_path / "dist/artifacts/schedule/dag.py").write_text("")

    sh_env: Dict[str, str] = {}
    if c.get("allow_data_loss"):
        sh_env["ALLOW_DATA_LOSS"] = "true"
    if c.get("no_verify_digest"):
        sh_env["NO_VERIFY_DIGEST"] = "true"
    sh_env["PUBLISH_TARGETS"] = " ".join(c["targets"])  # type: ignore[arg-type]
    for key, var in (
        ("scheduler_destination", "SCHEDULER_DESTINATION"),
        ("scheduler_environment_name", "SCHEDULER_ENVIRONMENT_NAME"),
        ("scheduler_location", "SCHEDULER_LOCATION"),
        ("scheduler_workspace", "SCHEDULER_WORKSPACE"),
    ):
        if c.get(key):
            sh_env[var] = str(c[key])
    if c.get("schedule_sync_dry_run"):
        sh_env["SCHEDULE_SYNC_DRY_RUN"] = "true"
    apply_in_pipeline = c.get("apply_in_pipeline", True) is not False
    if not apply_in_pipeline:
        sh_env["RUN_STAGE_7_APPLY"] = "false"

    opts = st.ShipOptions(
        contract="contract.fluid.yaml",
        env=env_name,
        mode=mode,
        build_id=str(c["build_id"]),
        yes=True,
        allow_data_loss=bool(c.get("allow_data_loss")),
        no_verify_digest=bool(c.get("no_verify_digest")),
        targets=list(c["targets"]),  # type: ignore[arg-type]
        scheduler=str(c.get("scheduler", "")),
        scheduler_destination=str(c.get("scheduler_destination", "")),
        scheduler_environment_name=str(c.get("scheduler_environment_name", "")),
        scheduler_location=str(c.get("scheduler_location", "")),
        scheduler_workspace=str(c.get("scheduler_workspace", "")),
        schedule_sync_dry_run=bool(c.get("schedule_sync_dry_run")),
        apply_in_pipeline=apply_in_pipeline,
    )
    ws = st.Workspace(tmp_path)
    for spec in specs:
        expected = _shell_argv(tmp_path, spec.command, sh_env, bin_dir, spec.num)
        action = st.build_stage(spec.num, opts, ws)
        assert action.argv == expected, f"stage {spec.num} {spec.slug} ({case})"
        if expected is None:
            assert action.skip_reason, f"stage {spec.num} skipped without a reason"


# ---------------------------------------------------------------------------
# 2. Unit: selection, precedence, skips, preconditions, exit codes
# ---------------------------------------------------------------------------


def test_default_stage_set_is_one_to_nine() -> None:
    selected, why = st.select_stages(publish=False, schedule_sync=False)
    assert selected == list(range(1, 10))
    assert "--publish" in why[10] and "--schedule-sync" in why[11]


def test_publish_and_schedule_sync_add_stages_ten_and_eleven() -> None:
    assert st.select_stages(publish=True, schedule_sync=False)[0][-1] == 10
    assert st.select_stages(publish=True, schedule_sync=True)[0][-2:] == [10, 11]


def test_until_accepts_names_and_numbers() -> None:
    assert st.select_stages(publish=False, schedule_sync=False, until="plan")[0] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    assert st.select_stages(publish=False, schedule_sync=False, until="2")[0] == [1, 2]


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("7", [7]),
        ("6-9", [6, 7, 8, 9]),
        ("plan,apply", [6, 7]),
        ("plan-verify", [6, 7, 8, 9]),
        ("validate-artifacts", [4]),
        ("validate-artifacts-diff", [4, 5]),
        ("policy", [8]),
        ("artifacts", [3]),
        ("schedule-sync", [11]),
        ("2,2,bundle", [1, 2]),
    ],
)
def test_stage_spec_parsing(spec: str, expected: List[int]) -> None:
    assert st.parse_stage_spec(spec) == expected


@pytest.mark.parametrize("spec", ["12", "nope", "9-6", "plan,zzz", ""])
def test_bad_stage_spec_is_a_usage_error(spec: str) -> None:
    with pytest.raises(st.ShipUsageError):
        st.parse_stage_spec(spec)


def test_stage_overrides_the_default_set_and_skip_stage_removes() -> None:
    selected, why = st.select_stages(
        publish=False, schedule_sync=False, stage_spec="5-8", skip_stages=["diff", "7"]
    )
    assert selected == [6, 8]
    assert why[5] == "--skip-stage" and why[7] == "--skip-stage"
    assert why[1] == "not in --stage"


def test_flag_beats_env_beats_default() -> None:
    env = {"FLUID_ENV": "prod", "APPLY_MODE": "replace", "APPLY_BUILD_ID": "from_env"}
    from_env = st.resolve_options(parse([]), "c.yaml", env)
    assert (from_env.env, from_env.mode, from_env.build_id) == ("prod", "replace", "from_env")
    flags = parse(["--env", "qa", "--apply-mode", "amend", "--build-id", "flag"])
    from_flags = st.resolve_options(flags, "c.yaml", env)
    assert (from_flags.env, from_flags.mode, from_flags.build_id) == ("qa", "amend", "flag")
    bare = st.resolve_options(parse([]), "c.yaml", {})
    assert (bare.env, bare.mode, bare.build_id) == ("dev", "amend", None)


def test_set_but_empty_build_id_means_no_build_id() -> None:
    assert st.resolve_options(parse([]), "c.yaml", {"APPLY_BUILD_ID": ""}).build_id == ""


def test_booleans_are_true_only_for_the_word_true() -> None:
    for value, expected in (("true", True), ("1", False), ("TRUE", False), ("false", False)):
        opts = st.resolve_options(
            parse([]), "c.yaml", {"ALLOW_DATA_LOSS": value, "NO_VERIFY_DIGEST": value}
        )
        assert opts.allow_data_loss is expected and opts.no_verify_digest is expected
    assert st.resolve_options(parse(["--allow-data-loss"]), "c.yaml", {}).allow_data_loss


def test_publish_targets_come_from_flag_then_env_then_default() -> None:
    assert st.resolve_options(parse([]), "c", {}).targets == ["datamesh-manager"]
    assert st.resolve_options(parse([]), "c", {"PUBLISH_TARGETS": "a b"}).targets == ["a", "b"]
    flagged = parse(["--target", "x", "--target", "y"])
    assert st.resolve_options(flagged, "c", {"PUBLISH_TARGETS": "a"}).targets == ["x", "y"]


def test_scheduler_settings_read_flag_then_env() -> None:
    env = {"SCHEDULER": "airflow", "SCHEDULER_LOCATION": "eu", "SCHEDULE_SYNC_DRY_RUN": "true"}
    opts = st.resolve_options(parse(["--scheduler-location", "us"]), "c", env)
    assert (opts.scheduler, opts.scheduler_location, opts.schedule_sync_dry_run) == (
        "airflow",
        "us",
        True,
    )


def test_invalid_apply_mode_from_env_is_refused() -> None:
    with pytest.raises(st.ShipUsageError, match="APPLY_MODE"):
        st.resolve_options(parse([]), "c", {"APPLY_MODE": "yolo"})


def test_dry_run_flag_is_apply_mode_dry_run_and_conflicts_are_refused() -> None:
    assert st.resolve_options(parse(["--dry-run"]), "c", {}).mode == "dry-run"
    with pytest.raises(st.ShipUsageError):
        st.resolve_options(parse(["--dry-run", "--apply-mode", "amend"]), "c", {})


def _opts(**kw: object) -> st.ShipOptions:
    return st.ShipOptions(contract="contract.fluid.yaml", **kw)  # type: ignore[arg-type]


def test_dry_run_skips_verify_publish_and_schedule_sync(tmp_path: Path) -> None:
    ws = st.Workspace(tmp_path)
    opts = _opts(mode="dry-run", scheduler="airflow")
    for num in (9, 10, 11):
        action = st.build_stage(num, opts, ws)
        assert action.argv is None and "dry-run" in (action.skip_reason or "")


def test_policy_apply_checks_instead_of_enforcing_after_dry_run(tmp_path: Path) -> None:
    (tmp_path / "dist/artifacts/policy").mkdir(parents=True)
    (tmp_path / "dist/artifacts/policy/bindings.json").write_text("{}")
    ws = st.Workspace(tmp_path)
    assert st.build_stage(8, _opts(mode="dry-run"), ws).argv[-2:] == ["--mode", "check"]  # type: ignore[index]
    assert st.build_stage(8, _opts(), ws).argv[-2:] == ["--mode", "enforce"]  # type: ignore[index]


def test_policy_apply_skips_without_bindings(tmp_path: Path) -> None:
    action = st.build_stage(8, _opts(), st.Workspace(tmp_path))
    assert action.argv is None and "bindings.json" in (action.skip_reason or "")


def test_validate_artifacts_skips_without_the_directory(tmp_path: Path) -> None:
    action = st.build_stage(4, _opts(), st.Workspace(tmp_path))
    assert action.argv is None and "dist/artifacts/" in (action.skip_reason or "")


def test_schedule_sync_skip_reasons(tmp_path: Path) -> None:
    ws = st.Workspace(tmp_path)
    assert "SCHEDULER is blank" in (st.build_stage(11, _opts(), ws).skip_reason or "")
    no_dir = st.build_stage(11, _opts(scheduler="airflow"), ws)
    assert "dist/artifacts/schedule" in (no_dir.skip_reason or "")
    (tmp_path / "dist/artifacts/schedule").mkdir(parents=True)
    assert st.build_stage(11, _opts(scheduler="airflow"), ws).argv is None  # empty directory
    (tmp_path / "dist/artifacts/schedule/d.py").write_text("")
    assert st.build_stage(11, _opts(scheduler="airflow"), ws).argv is not None


def test_build_id_is_passed_only_to_build_modes() -> None:
    ws = st.Workspace(Path("/nonexistent"))
    for mode in ("amend-and-build", "replace-and-build"):
        argv = st.build_stage(7, _opts(mode=mode, build_id="b1"), ws).argv
        assert argv is not None and argv[argv.index("--build-id") + 1] == "b1"
    ignored = st.build_stage(7, _opts(mode="amend", build_id="b1"), ws)
    assert ignored.argv is not None and "--build-id" not in ignored.argv
    assert any("APPLY_BUILD_ID is not passed on" in n for n in ignored.notes)
    quiet = st.build_stage(7, _opts(mode="amend", build_id=""), ws)
    assert quiet.notes == []
    empty = st.build_stage(7, _opts(mode="amend-and-build", build_id=""), ws)
    assert empty.argv is not None and "--build-id" not in empty.argv


def test_build_id_defaults_to_the_contracts_only_build(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (work / "contract.fluid.yaml").write_text(
        "fluidVersion: '0.7.5'\nid: x.y\nbuilds:\n  - id: only_build\n    pattern: embedded-logic\n"
    )
    action = st.build_stage(7, _opts(mode="amend-and-build"), st.Workspace(work))
    assert (
        action.argv is not None and action.argv[action.argv.index("--build-id") + 1] == "only_build"
    )


def test_reference_only_contract_skips_generate_artifacts(work: Path) -> None:
    (work / "contract.fluid.yaml").write_text(
        "fluidVersion: '0.7.5'\nid: x.y\nbuilds:\n  - id: b\n    pattern: hybrid-reference\n"
    )
    action = st.build_stage(3, _opts(), st.Workspace(work))
    assert action.argv is None and "reference-only" in (action.skip_reason or "")
    off = st.build_stage(3, _opts(generate_artifacts=False), st.Workspace(work))
    assert "--no-generate-artifacts" in (off.skip_reason or "")


def test_target_with_a_colon_is_refused_with_exit_two(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--publish", "--target", "name:https://evil.example", "--yes"]) == 2
    assert fake.calls == []


def test_target_with_a_colon_from_env_is_refused(
    work: Path, fake: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PUBLISH_TARGETS", "ok bad:http://x")
    assert ship_run(["--publish", "--yes"]) == 2
    assert fake.calls == []


@pytest.mark.parametrize("bad", ["dev\n::error::INJECTED", "two words", "../etc", "1st", "x" * 65])
def test_an_env_name_a_later_stage_refuses_is_refused_before_stage_one(
    work: Path,
    fake: FakeRun,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bad: str,
) -> None:
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--env", bad, "--yes"]) == 2
        monkeypatch.setenv("FLUID_ENV", bad)
        assert ship_run(["--yes"]) == 2
    assert fake.calls == []
    assert "is not an environment name" in caplog.text
    assert "\n::error::INJECTED" not in caplog.text  # quoted with repr, never raw


def test_valid_env_names_reach_every_stage(work: Path, fake: FakeRun) -> None:
    for name in ("dev", "prod-eu.1", "Staging_2"):
        assert ship_run(["--env", name, "--until", "bundle"]) == 0
        assert fake.calls[-1][:4] == ["bundle", "contract.fluid.yaml", "--env", name]


def test_missing_input_exits_two_naming_the_producing_stage(
    work: Path, fake: FakeRun, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--stage", "apply", "--yes"]) == 2
    assert (
        "stage 7 apply needs runtime/plan.json: run stage 6 (fluid ship --stage plan) first"
        in caplog.text
    )
    assert fake.calls == []


@pytest.mark.parametrize("stage", ["validate", "generate-artifacts", "diff", "plan", "verify"])
def test_bundle_consumers_name_stage_one(
    stage: str, work: Path, fake: FakeRun, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--stage", stage]) == 2
    assert "runtime/bundle.tgz" in caplog.text and "stage 1" in caplog.text


def test_a_selected_producer_satisfies_the_precondition(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--stage", "bundle,plan", "--skip-stage", "5"]) == 0
    assert fake.stages == ["bundle", "plan"]


def test_default_run_runs_every_stage_in_order(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--yes"]) == 0
    # artifacts dir exists after stage 3, so 4 runs; no policy bindings, so 8 is skipped
    assert fake.stages == [
        "bundle",
        "validate",
        "artifacts",
        "validate-artifacts",
        "diff",
        "plan",
        "apply",
        "verify",
    ]
    apply_argv = fake.calls[fake.stages.index("apply")]
    assert apply_argv[:5] == [
        "apply",
        "runtime/plan.json",
        "--bundle",
        "runtime/bundle.tgz",
        "--mode",
    ]
    assert "--yes" in apply_argv
    assert ship_run([]) == 0
    assert "--yes" not in fake.calls[-2]


def test_until_plan_runs_one_to_six_only(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "plan"]) == 0
    assert fake.stages[-1] == "plan" and "apply" not in fake.stages


def test_first_failure_stops_the_run_and_the_stage_code_is_in_the_report(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRun({"validate": 7})
    monkeypatch.setattr(ship.subprocess, "run", runner)
    assert ship_run(["--yes"]) == 1
    assert runner.stages == ["bundle", "validate"]
    assert _report(work)["stages"][1]["exit_code"] == 7 and _report(work)["exit_code"] == 1


def test_a_stage_killed_by_a_signal_maps_to_exit_one(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"bundle": -9}))
    assert ship_run([]) == 1


def test_missing_python_is_a_failed_stage_with_code_127(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("nope")

    monkeypatch.setattr(ship.subprocess, "run", boom)
    assert ship_run([]) == 1
    assert _report(work)["stages"][0]["exit_code"] == 127


def test_children_get_the_run_id_and_the_project_environment(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "validate"]) == 0
    ids = {env["FLUID_RUN_ID"] for env in fake.envs}
    assert len(ids) == 1 and ids != {""}
    report = json.loads((work / "runtime/ship-report.json").read_text())
    assert report["run_id"] in ids


def test_skip_plan_is_refused(work: Path, fake: FakeRun, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--skip-plan", "--yes"]) == 2
    assert "--stage" in caplog.text and fake.calls == []


def test_skip_bundle_is_ignored_so_a_stale_bundle_is_never_applied(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    # A reused runtime/bundle.tgz could hold an older contract than the one given,
    # and stages 2-9 would plan, apply and verify it with exit 0.
    (work / "runtime").mkdir()
    (work / "runtime/bundle.tgz").write_bytes(b"stale")
    assert ship_run(["--skip-bundle", "--yes", "--until", "validate"]) == 0
    assert fake.stages == ["bundle", "validate"]
    assert (work / "runtime/bundle.tgz").read_bytes() == b"x"  # rebuilt, not reused
    assert "--skip-bundle is ignored" in capsys.readouterr().out


def test_explain_runs_nothing_writes_nothing_and_exits_zero(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(p.name for p in work.iterdir())
    assert ship_run(["--explain", "--publish", "--apply-mode", "amend-and-build"]) == 0
    out = capsys.readouterr().out
    assert fake.calls == [] and sorted(p.name for p in work.iterdir()) == before
    assert "fluid bundle contract.fluid.yaml --env dev --format tgz" in out
    assert (
        "--mode amend-and-build" in out
        and "publish" in out
        and "not run" not in out.split("10 publish")[1].splitlines()[0]
    )
    assert "11 schedule-sync" in out and "opt-in" in out


def test_explain_redacts_credential_flags(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ship_run(["--explain", "--stage", "bundle", "--env", "dev"]) == 0
    assert "hunter2" not in capsys.readouterr().out


def test_the_contract_is_auto_found_relative_to_the_project(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "bundle"]) == 0
    assert fake.calls[0][1] == "contract.fluid.yaml"


def test_strict_is_the_default_and_no_strict_drops_it(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--stage", "bundle,validate,artifacts,validate-artifacts"]) == 0
    assert "--strict" in fake.calls[fake.stages.index("validate")]
    assert "--strict" in fake.calls[fake.stages.index("validate-artifacts")]
    fake.calls.clear()
    assert ship_run(["--no-strict", "--stage", "bundle,validate,artifacts,validate-artifacts"]) == 0
    assert "--strict" not in fake.calls[fake.stages.index("validate")]
    assert "--strict" not in fake.calls[fake.stages.index("validate-artifacts")]
    assert parse(["--strict"]).strict is True and parse(["--no-strict"]).strict is False


def test_absolute_contract_under_the_cwd_becomes_relative(work: Path, fake: FakeRun) -> None:
    (work / "sub").mkdir()
    (work / "sub/c.fluid.yaml").write_text("id: x\n")
    assert ship_run([str(work / "contract.fluid.yaml"), "--until", "bundle"]) == 0
    assert fake.calls[-1][1] == "contract.fluid.yaml"
    assert ship_run(["./sub/../sub/c.fluid.yaml", "--until", "bundle"]) == 0
    assert fake.calls[-1][1] == "sub/c.fluid.yaml"


def test_a_contract_outside_the_cwd_runs_in_its_own_directory(
    work: Path,
    fake: FakeRun,
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Command Center's call: an absolute path into a per-call temp dir, from the
    # backend's own working directory.
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "contract.fluid.yaml").write_text("fluidVersion: '0.7.5'\nid: x.y\n")
    argv = [str(elsewhere / "contract.fluid.yaml"), "--yes", "--skip-bundle", "--env", "dev"]
    assert ship_run([*argv, "--no-strict", "--until", "bundle"]) == 0
    assert fake.calls[0][1] == "contract.fluid.yaml"
    assert (elsewhere / "runtime/bundle.tgz").is_file() and not (work / "runtime").exists()
    assert (elsewhere / "runtime/ship-report.json").is_file()
    assert Path.cwd() == work  # ship's own directory change is undone
    assert f"running in {elsewhere}" in capsys.readouterr().out


def test_stage_four_without_artifacts_is_refused_when_generation_is_expected(
    work: Path, fake: FakeRun, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--stage", "validate-artifacts"]) == 2
    assert (
        "stage 4 validate-artifacts needs dist/artifacts/: "
        "run stage 3 (fluid ship --stage artifacts) first" in caplog.text
    )
    assert fake.calls == []
    # a selected stage 3 that will run produces the directory
    assert ship_run(["--stage", "bundle,artifacts,validate-artifacts"]) == 0


def test_stage_four_skips_with_a_reason_when_generation_is_off_or_reference_only(
    work: Path, fake: FakeRun
) -> None:
    assert ship_run(["--stage", "validate-artifacts", "--no-generate-artifacts"]) == 0
    (work / "contract.fluid.yaml").write_text(
        "fluidVersion: '0.7.5'\nid: x.y\nbuilds:\n  - id: b\n    pattern: hybrid-reference\n"
    )
    assert ship_run(["--stage", "validate-artifacts"]) == 0
    assert fake.calls == []
    stage4 = json.loads((work / "runtime/ship-report.json").read_text())["stages"][3]
    assert stage4["status"] == "skipped" and "dist/artifacts/" in stage4["skip_reason"]


def test_the_ignored_build_id_note_appears_only_for_an_id_the_operator_gave(work: Path) -> None:
    (work / "contract.fluid.yaml").write_text(
        "fluidVersion: '0.7.5'\nid: x.y\nbuilds:\n  - id: only_build\n    pattern: embedded-logic\n"
    )
    ws = st.Workspace(work)
    assert st.build_stage(7, _opts(mode="amend"), ws).notes == []  # derived, not given
    assert st.build_stage(7, _opts(mode="amend", build_id="b"), ws).notes


def test_reports_accumulate_across_invocations_of_one_run(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "plan"]) == 0
    assert ship_run(["--stage", "7-9", "--yes"]) == 0
    report = json.loads((work / "runtime/ship-report.json").read_text())
    status = {s["number"]: s["status"] for s in report["stages"]}
    assert all(status[n] == "passed" for n in (1, 2, 3, 4, 5, 6, 7, 9)), status
    assert status[8] == "skipped" and report["exit_code"] == 0
    suite = ET.parse(work / "runtime/ship-junit.xml").getroot()
    assert suite.get("failures") == "0" and len(suite.findall("testcase")) == 11


def test_merged_report_keeps_the_first_failure_and_a_new_run_overwrites(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"plan": 9}))
    assert ship_run(["--until", "plan"]) == 1
    monkeypatch.setattr(ship.subprocess, "run", FakeRun())
    assert ship_run(["--stage", "diff", "--no-strict"]) == 0
    merged = json.loads((work / "runtime/ship-report.json").read_text())
    assert merged["exit_code"] == 1  # the run's status, not this invocation's
    plan = {s["number"]: s for s in merged["stages"]}[6]
    assert plan["status"] == "failed" and plan["exit_code"] == 9
    monkeypatch.setenv("FLUID_RUN_ID", "another-run")
    assert ship_run(["--stage", "diff"]) == 0
    fresh = json.loads((work / "runtime/ship-report.json").read_text())
    assert fresh["run_id"] == "another-run" and fresh["exit_code"] == 0
    assert {s["number"]: s["status"] for s in fresh["stages"]}[6] == "not-run"


def test_an_unreadable_previous_report_is_overwritten(work: Path, fake: FakeRun) -> None:
    (work / "runtime").mkdir()
    (work / "runtime/ship-report.json").write_text("not json")
    assert ship_run(["--until", "bundle"]) == 0
    assert (
        json.loads((work / "runtime/ship-report.json").read_text())["stages"][0]["status"]
        == "passed"
    )


def test_ctrl_c_writes_reports_closes_the_fence_and_exits_130(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*a: object, **k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ship.subprocess, "run", interrupted)
    assert ship_run(["--ci-output", "github"]) == 130
    lines = capsys.readouterr().out.splitlines()
    stop = next(line for line in lines if line.startswith("::stop-commands::"))
    token = stop.split("::stop-commands::")[1]
    assert f"::{token}::" in lines and "::endgroup::" in lines
    report = json.loads((work / "runtime/ship-report.json").read_text())
    by = {s["number"]: s for s in report["stages"]}
    assert by[1]["status"] == "failed" and by[1]["failure_reason"] == "interrupted"
    assert by[1]["exit_code"] == 130 and by[2]["status"] == "not-run"
    assert report["exit_code"] == 130 and (work / "runtime/ship-junit.xml").is_file()


# ---------------------------------------------------------------------------
# 3. CI-native output
# ---------------------------------------------------------------------------


def test_detect_reads_the_ci_environment() -> None:
    assert _ci_output.detect("auto", {"GITHUB_ACTIONS": "true"}) == "github"
    assert _ci_output.detect("auto", {"GITLAB_CI": "true"}) == "gitlab"
    assert _ci_output.detect("auto", {"TF_BUILD": "True"}) == "azure"
    assert _ci_output.detect("auto", {}) == "plain"
    assert _ci_output.detect("gitlab", {"GITHUB_ACTIONS": "true"}) == "gitlab"


def test_github_escaping_matches_the_toolkit() -> None:
    assert _ci_output.escape_data("100%\r\nnext") == "100%25%0D%0Anext"
    assert _ci_output.escape_property("a:b,c%\n") == "a%3Ab%2Cc%25%0A"
    assert _ci_output.escape_data("%0A") == "%250A"  # % first, never double-decoded


def test_github_groups_and_stop_commands_wrap_the_child(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def child(cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        sys.stdout.write("::error::injected by the child\n")
        sys.stdout.flush()
        Path("runtime").mkdir(exist_ok=True)
        Path("runtime/bundle.tgz").write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ship.subprocess, "run", child)
    assert ship_run(["--ci-output", "github", "--until", "validate"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "::group::1 · bundle"
    stop = next(line for line in lines if line.startswith("::stop-commands::"))
    token = stop.split("::stop-commands::")[1]
    assert len(token) == 32
    injected = lines.index("::error::injected by the child")
    assert lines.index(stop) < injected < lines.index(f"::{token}::")
    assert lines.index(f"::{token}::") < lines.index("::endgroup::")
    tokens = [line.split("::stop-commands::")[1] for line in lines if "::stop-commands::" in line]
    assert len(tokens) == len(set(tokens)) == 2  # one fresh token per stage


def test_github_failure_annotation_and_step_summary(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = work / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"validate": 3}))
    assert ship_run(["--yes"]) == 1
    out = capsys.readouterr().out
    assert "::error title=fluid ship::stage 2 validate failed (exit 3)" in out
    text = summary.read_text()
    assert "| Stage | Status | Duration |" in text
    assert "| 1 · bundle | passed |" in text and "| 2 · validate | failed |" in text


def test_gitlab_sections(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_CI", "true")
    assert ship_run(["--until", "validate"]) == 0
    out = capsys.readouterr().out
    assert re.search(
        r"\x1b\[0Ksection_start:\d+:fluid_ship_1_bundle\[collapsed=true\]\r\x1b\[0K1 · bundle", out
    )
    assert re.search(r"\x1b\[0Ksection_end:\d+:fluid_ship_1_bundle\r\x1b\[0K", out)
    for section in re.findall(r"section_start:\d+:([^\[\r]+)", out):
        assert re.fullmatch(r"[A-Za-z0-9_]+", section)


def test_gitlab_section_names_are_alphanumeric_for_hyphenated_stages() -> None:
    buf = io.StringIO()
    ci = _ci_output.CiOutput("gitlab", out=buf, clock=lambda: 100.0)
    ci.stage_start(11, "schedule-sync")
    assert "fluid_ship_11_schedule_sync[collapsed=true]" in buf.getvalue()


def test_azure_groups_and_logissue(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TF_BUILD", "True")
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"validate": 4}))
    assert ship_run([]) == 1
    out = capsys.readouterr().out
    assert "##[group]1 · bundle" in out and "##[endgroup]" in out
    assert "##vso[task.logissue type=error]stage 2 validate failed (exit 4)" in out


def test_plain_output_has_a_banner_and_no_markers(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ship_run(["--until", "bundle"]) == 0
    out = capsys.readouterr().out
    assert "stage 1/11 · bundle" in out
    assert "::group" not in out and "##[" not in out and "section_start" not in out


def test_user_influenced_text_cannot_forge_workflow_commands(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # FLUID_ENV is refused before stage 1 (see the env-name test), so the
    # hostile text rides on the contract path, which reaches the argv echo.
    hostile = "c\n::error::INJECTED-ENV\r::error::INJECTED-CR.fluid.yaml"
    (work / hostile).write_text("fluidVersion: '0.7.5'\nid: x.y\n")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ship_run([hostile, "--until", "bundle"]) == 0
    assert ship_run([hostile, "--explain", "--until", "bundle"]) == 0
    out = capsys.readouterr().out
    lines = out.replace("\r", "\n").splitlines()
    assert not any(line.startswith("::error::INJECTED") for line in lines)
    assert "\\n::error::INJECTED-ENV" in out  # visible, escaped
    own = re.compile(
        r"::(group::.*|endgroup::|stop-commands::[0-9a-f]{32}|[0-9a-f]{32}::"
        r"|error title=fluid ship::.*)"
    )
    assert [x for x in lines if x.startswith("::") and not own.fullmatch(x)] == []
    # the echo of the command sits inside the fence
    stop = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
    assert lines[stop + 1].startswith("$ fluid bundle")


def test_oneline_escapes_every_control_character() -> None:
    assert _ci_output.oneline("a\nb\rc\x1bd\x00e\tf") == "a\\nb\\rc\\x1bd\\x00e\\tf"
    assert "\n" not in _ci_output.oneline("x\u2028y")


def test_explicit_ci_output_overrides_detection(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ship_run(["--ci-output", "plain", "--until", "bundle"]) == 0
    assert "::group" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4. Reports
# ---------------------------------------------------------------------------


def test_reports_on_success(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--yes", "--env", "qa"]) == 0
    report = json.loads((work / "runtime/ship-report.json").read_text())
    assert report["env"] == "qa" and report["mode"] == "amend"
    assert report["contract"] == "contract.fluid.yaml" and report["exit_code"] == 0
    stages = report["stages"]
    assert [s["number"] for s in stages] == list(range(1, 12))
    by_name = {s["name"]: s for s in stages}
    assert by_name["bundle"]["status"] == "passed" and by_name["bundle"]["exit_code"] == 0
    assert by_name["bundle"]["argv"][:2] == ["bundle", "contract.fluid.yaml"]
    assert by_name["policy-apply"]["status"] == "skipped"
    assert by_name["policy-apply"]["skip_reason"]
    assert (
        by_name["publish"]["status"] == "not-run"
        and "--publish" in by_name["publish"]["skip_reason"]
    )
    assert all(isinstance(s["duration_seconds"], (int, float)) for s in stages)

    suite = ET.parse(work / "runtime/ship-junit.xml").getroot()
    assert suite.tag == "testsuite" and suite.get("tests") == "11" and suite.get("failures") == "0"
    cases = suite.findall("testcase")
    assert len(cases) == 11
    assert cases[0].get("name") == "1 bundle" and cases[0].find("skipped") is None
    assert cases[7].find("skipped") is not None  # policy-apply


def test_reports_on_a_failed_stage(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"plan": 9}))
    assert ship_run(["--yes"]) == 1
    report = json.loads((work / "runtime/ship-report.json").read_text())
    assert report["exit_code"] == 1
    by_name = {s["name"]: s for s in report["stages"]}
    assert by_name["plan"]["status"] == "failed" and by_name["plan"]["exit_code"] == 9
    assert by_name["validate"]["status"] == "passed"
    assert by_name["apply"]["status"] == "not-run"
    assert by_name["apply"]["skip_reason"] == "an earlier stage failed"

    suite = ET.parse(work / "runtime/ship-junit.xml").getroot()
    assert suite.get("failures") == "1"
    failed = [c for c in suite.findall("testcase") if c.find("failure") is not None]
    assert [c.get("name") for c in failed] == ["6 plan"]
    assert "exit code 9" in (failed[0].find("failure").text or "")  # type: ignore[union-attr]


def test_report_dir_is_honoured(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "bundle", "--report-dir", "out/reports"]) == 0
    assert (work / "out/reports/ship-report.json").is_file()
    assert (work / "out/reports/ship-junit.xml").is_file()


def test_reports_redact_credential_flags(work: Path) -> None:
    from fluid_build.cli._ship_report import StageRecord, build_report

    rec = StageRecord(1, "bundle", argv=ship._safe_argv(["x", "--client-secret", "hunter2"]))
    assert "hunter2" not in json.dumps(
        build_report([rec], run_id="r", env="e", mode="m", contract="c", exit_code=0)
    )


# ---------------------------------------------------------------------------
# 5. End to end: the real CLI, the local provider
# ---------------------------------------------------------------------------

REPO_ROOT = Path(fluid_build.__file__).resolve().parent.parent
HELLO = REPO_ROOT / "examples" / "01-hello-world" / "contract.fluid.yaml"
_E2E = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None or not HELLO.is_file(),
    reason="needs duckdb and examples/01-hello-world",
)


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _ENV_VARS and not k.endswith("_API_KEY") and k != "FLUID_RUN_ID"
    }
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture
def hello(tmp_path: Path) -> Path:
    # The example as shipped: it passes ``validate --strict`` (no placeholder owner).
    (tmp_path / "contract.fluid.yaml").write_text(HELLO.read_text())
    return tmp_path


@pytest.mark.integration
@_E2E
def test_ship_runs_the_real_pipeline_on_the_local_provider(hello: Path) -> None:
    done = _cli(hello, "ship", "contract.fluid.yaml", "--yes")
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    for rel in (
        "runtime/plan.json",
        "runtime/verify-report.json",
        "runtime/ship-report.json",
        "runtime/ship-junit.xml",
    ):
        assert (hello / rel).is_file(), rel
    report = json.loads((hello / "runtime/ship-report.json").read_text())
    for stage in report["stages"][:9]:
        assert stage["status"] in ("passed", "skipped"), stage
        if stage["status"] == "skipped":
            assert stage["skip_reason"]
    assert {s["name"]: s["status"] for s in report["stages"]}["apply"] == "passed"


@pytest.mark.integration
@_E2E
def test_until_plan_stops_before_apply(hello: Path) -> None:
    done = _cli(hello, "ship", "--until", "plan")
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert (hello / "runtime/plan.json").is_file()
    assert not (hello / "runtime/apply-report.html").exists()
    report = json.loads((hello / "runtime/ship-report.json").read_text())
    assert {s["name"]: s["status"] for s in report["stages"]}["apply"] == "not-run"


@pytest.mark.integration
@_E2E
def test_command_center_invocation_keeps_exiting_zero(hello: Path) -> None:
    # ``ship <contract> --yes --skip-bundle --env X``: no bundle yet, so it bundles anyway.
    done = _cli(hello, "ship", "contract.fluid.yaml", "--yes", "--skip-bundle", "--env", "dev")
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]


@pytest.mark.integration
def test_apply_stage_in_a_clean_dir_exits_two(tmp_path: Path) -> None:
    (tmp_path / "contract.fluid.yaml").write_text("id: x\n")
    done = _cli(tmp_path, "ship", "--stage", "apply", "--yes")
    assert done.returncode == 2
    assert "stage 7 apply needs runtime/plan.json" in done.stdout + done.stderr


# ---------------------------------------------------------------------------
# 6. Review round: the fence covers ship's own contract reads (security judge)
# ---------------------------------------------------------------------------

_HOSTILE = "zz\n::warning title=INJECTED::from-the-checkout\n"


def _lines_outside_fences(text: str) -> List[str]:
    """Every line GitHub would parse for workflow commands: those not between
    ``::stop-commands::<token>`` and ``::<token>::``."""
    outside: List[str] = []
    token: Optional[str] = None
    for line in text.replace("\r", "\n").splitlines():
        match = re.fullmatch(r"::stop-commands::([0-9a-f]{32})", line)
        if token is None and match:
            token = match.group(1)
        elif token is not None and line == f"::{token}::":
            token = None
        elif token is None:
            outside.append(line)
    return outside


_OWN_MARKERS = re.compile(r"::(group::.*|endgroup::|error title=fluid ship::.*)")


def test_parent_side_contract_reads_cannot_issue_workflow_commands(
    work: Path,
    fake: FakeRun,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The build id is read from the contract in the ship process; loading it
    # warns about overlay entries with their ids copied in (overlay_entry_added).
    import fluid_build.cli.generate_ci as generate_ci

    def hostile_single_build_id(contract: str) -> str:
        logging.getLogger("fluid_build.loader").warning("overlay adds exposeId %s", _HOSTILE)
        print(f"printed by the probe: {_HOSTILE}")
        return "b1"

    monkeypatch.setattr(generate_ci, "_single_build_id", hostile_single_build_id)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    Path("runtime").mkdir()
    Path("runtime/bundle.tgz").write_bytes(b"x")
    Path("runtime/plan.json").write_text("{}")
    with caplog.at_level(logging.WARNING):
        assert ship_run(["--stage", "apply", "--apply-mode", "amend-and-build", "--yes"]) == 0
    assert fake.calls[-1][-2:] == ["--build-id", "b1"]
    out = capsys.readouterr()
    outside = [ln for ln in _lines_outside_fences(out.out + out.err) if ln.startswith("::")]
    assert [ln for ln in outside if not _OWN_MARKERS.fullmatch(ln)] == []
    assert "INJECTED" not in caplog.text  # the probe's log is silenced, not printed


def test_build_id_is_derived_from_the_contract_only_in_build_modes(
    work: Path, fake: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.cli.generate_ci as generate_ci

    def must_not_run(contract: str) -> str:
        raise AssertionError("the contract was read for a build id a non-build mode ignores")

    monkeypatch.setattr(generate_ci, "_single_build_id", must_not_run)
    Path("runtime").mkdir()
    Path("runtime/bundle.tgz").write_bytes(b"x")
    Path("runtime/plan.json").write_text("{}")
    assert ship_run(["--stage", "apply", "--yes"]) == 0
    assert "--build-id" not in fake.calls[-1]


def test_on_github_each_stage_body_sits_inside_one_fence(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ship_run(["--until", "validate", "--yes"]) == 0
    lines = capsys.readouterr().out.splitlines()
    for num, name in ((1, "bundle"), (2, "validate")):
        start = lines.index(f"::group::{num} · {name}")
        end = lines.index("::endgroup::", start)
        opened = re.fullmatch(r"::stop-commands::([0-9a-f]{32})", lines[start + 1])
        assert opened, lines[start + 1]
        assert lines[end - 1] == f"::{opened.group(1)}::"
        # one fence per stage: the child re-enters it, no second token
        assert sum(ln.startswith("::stop-commands::") for ln in lines[start:end]) == 1


def test_child_stderr_joins_stdout_on_github_only(
    work: Path, fake: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ship_run(["--until", "bundle", "--ci-output", "plain"]) == 0
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ship_run(["--until", "bundle"]) == 0
    assert fake.stderrs == [None, subprocess.STDOUT]


def _previous_report(*stages: Dict[str, object]) -> str:
    """An earlier report of run-1 whose stage 1 record is well formed, beside ``stages``."""
    good = {"number": 1, "name": "bundle", "status": "passed", "exit_code": 0}
    head = {"run_id": "run-1", "env": "dev", "mode": "amend", "contract": "contract.fluid.yaml"}
    return json.dumps({**head, "exit_code": 0, "stages": [good, *stages]})


@pytest.mark.parametrize(
    "payload",
    [
        _previous_report({"number": 2, "name": "v", "status": "passed", "exit_code": "nan?"}),
        _previous_report({"number": 2, "name": "validate", "status": "pwned"}),
        _previous_report({"number": 99, "name": "x", "status": "passed"}),
        # json reads Infinity; int() of it raises OverflowError
        _previous_report({"number": 2, "name": "v", "status": "passed", "exit_code": float("inf")}),
        _previous_report(
            {"number": 2, "name": "v", "status": "passed", "duration_seconds": float("nan")}
        ),
        "[" * 100_000,  # json.loads raises RecursionError
        "not json",
    ],
    ids=[
        "bad-code",
        "bad-status",
        "bad-number",
        "overflow",
        "nan-duration",
        "deep-nesting",
        "not-json",
    ],
)
def test_a_malformed_earlier_report_is_overwritten_not_trusted(
    work: Path, fake: FakeRun, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "run-1")
    Path("runtime").mkdir()
    Path("runtime/bundle.tgz").write_bytes(b"x")
    Path("runtime/ship-report.json").write_text(payload)
    assert ship_run(["--stage", "validate"]) == 0  # continues run-1: the old report is read
    report = _report(work)
    statuses = {s["number"]: s["status"] for s in report["stages"]}
    # nothing of it was kept, not even its well-formed stage 1 record
    assert statuses[2] == "passed" and statuses[1] == "not-run"
    assert report["exit_code"] == 0


def test_a_well_formed_earlier_report_of_the_run_is_kept(
    work: Path, fake: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "run-1")
    Path("runtime").mkdir()
    Path("runtime/bundle.tgz").write_bytes(b"x")
    Path("runtime/ship-report.json").write_text(_previous_report())
    assert ship_run(["--stage", "validate"]) == 0
    statuses = {s["number"]: s["status"] for s in _report(work)["stages"]}
    assert statuses[1] == statuses[2] == "passed"


def test_until_says_how_to_go_on(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ship_run(["--until", "plan", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Stopped before stage 7 apply (--until). Review runtime/plan.json first." in out
    assert "run the same command with --stage 7-9 in place of --until" in out


def test_a_failed_stage_says_how_to_re_run_it(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"validate": 1}))
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--yes"]) == 1
    assert "After the fix, run the same command again" in caplog.text
    assert "to go on from that stage with this bundle, use --stage 2-9" in caplog.text


# ---------------------------------------------------------------------------
# 7. Review round 2: run scope, Ctrl-C patience, the legacy CATALOG variable
# ---------------------------------------------------------------------------


def _report(work: Path) -> Dict[str, object]:
    return json.loads((work / "runtime/ship-report.json").read_text())


def test_a_run_that_bundles_starts_a_new_run(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "bundle"]) == 0
    first = _report(work)["run_id"]
    assert ship_run(["--stage", "validate"]) == 0  # continues the run: one report
    assert _report(work)["run_id"] == first
    assert {s["number"]: s["status"] for s in _report(work)["stages"]}[1] == "passed"
    assert ship_run(["--until", "bundle"]) == 0  # bundles again: a new run
    assert _report(work)["run_id"] != first


def test_a_later_stage_for_another_env_does_not_merge(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "plan"]) == 0
    assert ship_run(["--stage", "diff", "--env", "prod"]) == 0
    statuses = {s["number"]: s["status"] for s in _report(work)["stages"]}
    assert statuses[6] == "not-run" and _report(work)["env"] == "prod"


def test_the_first_ctrl_c_lets_the_stage_stop_on_its_own(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import signal

    stages: List[str] = []

    def child(cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        stages.append(cmd[3])
        os.kill(os.getpid(), signal.SIGINT)  # the terminal's Ctrl-C reaches ship too
        return subprocess.CompletedProcess(cmd, 130)  # the child cleaned up and exited

    monkeypatch.setattr(ship.subprocess, "run", child)
    assert ship_run(["--yes"]) == 130
    assert stages == ["bundle"]  # no stage after the interrupted one
    assert "ship stops when this stage ends" in capsys.readouterr().out
    by = {s["number"]: s for s in _report(work)["stages"]}
    assert by[1]["failure_reason"] == "interrupted" and by[2]["status"] == "not-run"


def test_a_second_ctrl_c_stops_waiting(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    def child(cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGINT)  # raises KeyboardInterrupt: run() kills the child
        raise AssertionError("the second Ctrl-C did not interrupt")

    monkeypatch.setattr(ship.subprocess, "run", child)
    assert ship_run(["--yes"]) == 130
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_publish_targets_fall_back_to_the_legacy_catalog_variable() -> None:
    assert st._resolve_targets([], {"CATALOG": "datahub"}) == ["datahub"]
    assert st._resolve_targets([], {"PUBLISH_TARGETS": "a b", "CATALOG": "x"}) == ["a", "b"]
    assert st._resolve_targets([], {"PUBLISH_TARGETS": " ", "CATALOG": "x"}) == ["x"]
    assert st._resolve_targets(["t"], {"CATALOG": "x"}) == ["t"]


def test_catalog_fallback_matches_the_generated_shell(tmp_path: Path) -> None:
    config = PipelineConfig(
        provider=PipelineProvider.GITHUB_ACTIONS,
        complexity=PipelineComplexity.BASIC,
        contract_path="contract.fluid.yaml",
    )
    body = GitHubActionsTemplate()._stage_specs(config)[9].command
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fluid").write_text(_FAKE_FLUID)
    (bin_dir / "fluid").chmod(0o755)
    shell = _shell_argv(tmp_path, body, {"CATALOG": "datahub"}, bin_dir, 10)
    opts = st.resolve_options(
        parse(["contract.fluid.yaml"]), "contract.fluid.yaml", {"CATALOG": "datahub"}
    )
    assert shell == st.build_stage(10, opts, st.Workspace(tmp_path)).argv


# ---------------------------------------------------------------------------
# 8. Review round 3: ship's own exit status, opt-in stages under --stage, the
#    report's scope, and a workspace that is not what ship expects
# ---------------------------------------------------------------------------


def test_a_failure_after_a_passing_apply_exits_three_and_says_the_changes_are_live(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"verify": 1}))
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--yes"]) == 3
    assert "its changes are live" in caplog.text
    assert _report(work)["exit_code"] == 3


def test_a_failed_apply_or_a_dry_run_is_not_partial(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"apply": 1}))
    assert ship_run(["--yes"]) == 1
    (work / "dist/artifacts/policy").mkdir(parents=True, exist_ok=True)
    (work / "dist/artifacts/policy/bindings.json").write_text("{}")
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"policy-apply": 1}))
    assert ship_run(["--dry-run", "--yes"]) == 1  # a dry-run apply changed nothing
    assert ship_run(["--yes"]) == 3


def test_a_stages_own_exit_two_is_a_failed_stage_not_a_refused_argument(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fluid diff exits 2 when it cannot inspect a live target, fluid bundle when a
    # file is missing, and fluid's crash handler exits 2 as well.
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"diff": 2}))
    assert ship_run(["--yes"]) == 1
    by = {s["number"]: s for s in _report(work)["stages"]}
    assert by[5]["exit_code"] == 2 and by[7]["status"] == "not-run"


def test_the_exit_status_is_documented_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Exit status: 0 every selected stage passed or was skipped; 1 a stage failed;" in text
    assert "3 a stage failed after apply changed something, so those changes are live" in text


def test_publish_and_schedule_sync_add_their_stage_under_an_explicit_stage_set() -> None:
    chosen = st.select_stages(publish=True, schedule_sync=True, stage_spec="7-9")[0]
    assert chosen == [7, 8, 9, 10, 11]
    selected, why = st.select_stages(publish=False, schedule_sync=False, stage_spec="7-9")
    assert selected == [7, 8, 9] and why[10] == "not in --stage"
    dropped = st.select_stages(
        publish=True, schedule_sync=False, stage_spec="7", skip_stages=["publish"]
    )
    assert dropped[0] == [7]


def test_stage_with_publish_publishes(work: Path, fake: FakeRun) -> None:
    assert ship_run(["--until", "plan"]) == 0
    assert ship_run(["--stage", "7-9", "--publish", "--yes"]) == 0
    assert fake.stages[-2:] == ["verify", "publish"]


@pytest.mark.parametrize(
    "numbers,spec",
    [([7, 8, 9], "7-9"), ([10], "10"), ([7, 9, 10, 11], "7,9-11"), ([3, 1, 2, 2], "1-3")],
)
def test_compact_spec_round_trips_through_the_stage_parser(numbers: List[int], spec: str) -> None:
    assert st.compact_spec(numbers) == spec
    assert st.parse_stage_spec(spec) == sorted(set(numbers))


def test_until_hint_names_the_stages_the_command_would_have_run(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ship_run(["--until", "verify", "--publish", "--yes"]) == 0
    assert "with --stage 10 in place of --until" in capsys.readouterr().out
    assert ship_run(["--until", "plan", "--publish", "--yes"]) == 0
    assert "with --stage 7-10 in place of --until" in capsys.readouterr().out


def test_a_retried_stage_range_never_shows_the_earlier_attempts_pass(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "job-1")
    monkeypatch.setattr(ship.subprocess, "run", FakeRun())
    assert ship_run(["--until", "plan"]) == 0
    assert ship_run(["--stage", "7-9", "--yes"]) == 0
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"apply": 1}))
    assert ship_run(["--stage", "7-9", "--yes"]) == 1
    by = {s["number"]: s for s in _report(work)["stages"]}
    assert by[6]["status"] == "passed"  # not selected this time: kept from the run
    assert by[7]["status"] == "failed"
    assert by[8]["status"] == by[9]["status"] == "not-run"
    assert by[9]["skip_reason"] == "an earlier stage failed"
    assert _report(work)["exit_code"] == 1


def test_dry_run_without_apply_in_the_pipeline_still_verifies_and_publishes(
    tmp_path: Path,
) -> None:
    (tmp_path / "dist/artifacts/schedule").mkdir(parents=True)
    (tmp_path / "dist/artifacts/schedule/d.py").write_text("")
    ws = st.Workspace(tmp_path)
    opts = _opts(mode="dry-run", scheduler="airflow", apply_in_pipeline=False)
    for num in (9, 10, 11):
        assert st.build_stage(num, opts, ws).argv is not None, num
    assert not st.resolve_options(parse(["--skip-stage", "apply"]), "c", {}).apply_in_pipeline
    # --stage 9 alone: stage 7 runs in another step of the same run
    assert st.resolve_options(parse(["--stage", "9"]), "c", {}).apply_in_pipeline


def test_explain_runs_schedule_sync_only_for_a_contract_with_something_to_schedule(
    work: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--explain", "--stage", "1-4,11", "--schedule-sync", "--scheduler", "airflow"]

    def stage_11() -> List[str]:
        lines = capsys.readouterr().out.splitlines()
        return next(line for line in lines if "11 schedule-sync" in line).split()

    assert ship_run(argv) == 0
    assert stage_11()[2] == "skip"  # no orchestration.engine and no scheduled build
    (work / "contract.fluid.yaml").write_text(
        HELLO.read_text() + "\norchestration:\n  engine: airflow\n"
    )
    assert ship_run(argv) == 0
    assert stage_11()[2] == "run"


def test_junit_stays_well_formed_whatever_an_argument_holds() -> None:
    from fluid_build.cli._ship_report import FAILED, StageRecord, junit_xml

    hostile = "a" + chr(0x1B) + "b" + chr(0) + "c" + chr(0xDCFF) + chr(0xFFFE)
    records = [
        StageRecord(7, "apply", status=FAILED, argv=["apply", "--build-id", hostile], exit_code=1),
        StageRecord(8, "policy-apply", reason="not here: " + hostile),
    ]
    root = ET.fromstring(junit_xml(records))
    failure = root.find("testcase/failure")
    assert failure is not None and failure.text is not None
    assert "\\x1b" in failure.text and "U+DCFF" in failure.text and "U+FFFE" in failure.text
    skipped = root.findall("testcase")[1].find("skipped")
    assert skipped is not None and "U+DCFF" in (skipped.get("message") or "")


@pytest.mark.parametrize("token", [chr(0xB2), chr(0x663)])  # superscript 2, Arabic-Indic 3
def test_a_non_ascii_digit_is_an_unknown_stage_not_a_crash(
    token: str, work: Path, fake: FakeRun
) -> None:
    with pytest.raises(st.ShipUsageError, match="unknown stage"):
        st.resolve_stage(token)
    assert ship_run(["--until", token]) == 2 and fake.calls == []


def test_a_symlinked_or_system_contract_is_refused_before_anything_is_written(
    work: Path,
    fake: FakeRun,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # fluid bundle refuses a symlinked input (cli/security.py, F6): a link planted
    # in a checkout could point the run at a file outside it. Ship refuses it as
    # stage 1 would, before a run id or a runtime/ is written.
    shared = tmp_path_factory.mktemp("shared")
    (shared / "real.fluid.yaml").write_text("fluidVersion: '0.7.5'\nid: x.y\n")
    (work / "proj").mkdir()
    (work / "proj/contract.fluid.yaml").symlink_to(shared / "real.fluid.yaml")
    outside = tmp_path_factory.mktemp("outside")
    (outside / "contract.fluid.yaml").symlink_to(shared / "real.fluid.yaml")
    with caplog.at_level(logging.ERROR):
        assert ship_run(["proj/contract.fluid.yaml", "--until", "bundle"]) == 2
        assert ship_run([str(outside / "contract.fluid.yaml"), "--explain"]) == 2
        if Path("/etc/hosts").is_file():
            assert ship_run(["/etc/hosts", "--until", "bundle"]) == 2
    assert "Symlinked contract paths are not allowed" in caplog.text
    assert fake.calls == [] and not (work / ".fluid").exists()
    assert [p.name for p in outside.iterdir()] == ["contract.fluid.yaml"]
    assert not (shared / "runtime").exists()


def test_a_missing_contract_is_refused_before_anything_is_written(
    work: Path,
    fake: FakeRun,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    empty = tmp_path_factory.mktemp("empty")
    with caplog.at_level(logging.ERROR):
        assert ship_run([str(empty / "nofile.yaml"), "--yes"]) == 2
        assert ship_run([str(empty / "no/such/dir/c.yaml"), "--explain"]) == 2
    assert "no contract file at" in caplog.text
    assert list(empty.iterdir()) == [] and fake.calls == []


def test_a_relative_report_dir_is_taken_from_the_current_directory(
    work: Path, fake: FakeRun, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "contract.fluid.yaml").write_text("fluidVersion: '0.7.5'\nid: x.y\n")
    argv = [str(elsewhere / "contract.fluid.yaml"), "--until", "bundle", "--report-dir", "out"]
    assert ship_run(argv) == 0
    assert (work / "out/ship-report.json").is_file() and (work / "out/ship-junit.xml").is_file()
    assert not (elsewhere / "out").exists()
    assert (elsewhere / "runtime/bundle.tgz").is_file()  # the stages still run beside it


def test_a_file_named_runtime_fails_stage_one_cleanly(
    work: Path,
    fake: FakeRun,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (work / "runtime").write_text("a file, not a directory")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with caplog.at_level(logging.WARNING):
        assert ship_run(["--yes"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert any("cannot create runtime/" in line for line in lines)
    assert lines.count("::group::1 · bundle") == 1 and "::endgroup::" in lines
    assert fake.calls == [] and "could not write the run report" in caplog.text


@pytest.mark.parametrize("damage", ["dot-fluid-is-a-file", "run-id-is-not-text"])
def test_a_run_id_that_cannot_be_kept_does_not_stop_the_run(
    work: Path, fake: FakeRun, caplog: pytest.LogCaptureFixture, damage: str
) -> None:
    if damage == "dot-fluid-is-a-file":
        (work / ".fluid").write_text("a file, not a directory")
        argv = ["--until", "validate"]
    else:
        (work / ".fluid").mkdir()
        (work / ".fluid/run-id.txt").write_bytes(b"\xff\xfe")
        (work / "runtime").mkdir()
        (work / "runtime/bundle.tgz").write_bytes(b"x")
        argv = ["--stage", "validate"]  # continues a run, so it reads the persisted id
    with caplog.at_level(logging.WARNING):
        assert ship_run(argv) == 0
    assert "cannot keep the run id" in caplog.text
    ids = {env["FLUID_RUN_ID"] for env in fake.envs}
    assert len(ids) == 1 and _report(work)["run_id"] in ids


def test_the_abort_hint_goes_on_through_apply_before_it_publishes(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"diff": 1}))
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--yes", "--publish"]) == 1
    assert "use --stage 5-10" in caplog.text
    resumed = st.select_stages(publish=True, schedule_sync=False, stage_spec="5-10")[0]
    assert resumed.index(7) < resumed.index(10)  # apply runs again before publish
    caplog.clear()
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"verify": 1}))
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--stage", "9", "--yes"]) == 1
    assert "run the same command again." in caplog.text and "use --stage" not in caplog.text


def test_a_later_steps_failure_after_an_earlier_steps_apply_exits_three(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "job-1")
    monkeypatch.setattr(ship.subprocess, "run", FakeRun())
    assert ship_run(["--stage", "1-7", "--yes"]) == 0
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"verify": 1}))
    with caplog.at_level(logging.ERROR):
        assert ship_run(["--stage", "8-9", "--yes"]) == 3
    assert "its changes are live" in caplog.text and _report(work)["exit_code"] == 3
    monkeypatch.setattr(ship.subprocess, "run", FakeRun())
    assert ship_run(["--stage", "9", "--yes"]) == 0  # an earlier step's failure is not this one's
    assert _report(work)["exit_code"] == 0


def test_a_bad_target_is_refused_up_front_whenever_stage_ten_will_publish(
    work: Path, fake: FakeRun
) -> None:
    argv = ["--skip-stage", "apply", "--dry-run", "--publish", "--target", "a:b", "--yes"]
    assert ship_run(argv) == 2 and fake.calls == []
    # a dry run with apply in the pipeline skips publish, as the generated shell does
    assert ship_run(["--stage", "10", "--dry-run", "--target", "a:b"]) == 0
    assert fake.calls == []


def test_merged_records_get_this_versions_names_and_text_reasons(
    work: Path, fake: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "run-1")
    Path("runtime").mkdir()
    Path("runtime/bundle.tgz").write_bytes(b"x")
    odd_name = "a" + chr(1) + "b" + chr(0xD800)
    odd = {"number": 1, "name": odd_name, "status": "not-run", "skip_reason": 5}
    head = {"run_id": "run-1", "env": "dev", "mode": "amend", "contract": "contract.fluid.yaml"}
    Path("runtime/ship-report.json").write_text(
        json.dumps({**head, "exit_code": 0, "stages": [odd]})
    )
    assert ship_run(["--stage", "validate"]) == 0
    stage1 = _report(work)["stages"][0]
    assert stage1["name"] == "bundle" and stage1["skip_reason"] == "5"
    ET.parse(work / "runtime/ship-junit.xml")  # well formed


def test_a_dry_run_apply_in_an_earlier_step_does_not_make_a_failure_partial(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "job-1")
    (work / "dist/artifacts/policy").mkdir(parents=True)
    (work / "dist/artifacts/policy/bindings.json").write_text("{}")
    monkeypatch.setattr(ship.subprocess, "run", FakeRun())
    assert ship_run(["--stage", "1-7", "--dry-run", "--yes"]) == 0
    monkeypatch.setattr(ship.subprocess, "run", FakeRun({"policy-apply": 1}))
    assert ship_run(["--stage", "8", "--dry-run"]) == 1  # the dry run changed nothing
    assert _report(work)["exit_code"] == 1
