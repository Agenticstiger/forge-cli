# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The GitLab pipeline's hand-offs: the Run pipeline form on ``spec:inputs``, the
release packet the apply job downloads, evidence from a failed plan, and the
merge request's impact report.

The file is two YAML documents: the ``spec`` header and the pipeline. Each input
reaches ship as a job variable, never as script text.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

from fluid_build.cli._ship_table import STAGE_TOGGLES
from fluid_build.forge.core.pipeline_systems import _thin
from fluid_build.forge.core.pipeline_systems.gitlab_ci import (
    FORM_KNOBS,
    MAX_INPUTS,
    RELEASE_DIR,
    RELEASE_VARIABLE,
)
from fluid_build.forge.core.pipeline_templates import (
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

pytestmark = pytest.mark.unit

_SCHEMA = Path(__file__).parents[3] / "fixtures" / "ci_schemas" / "gitlab-ci.json"
_CONFIGS: Dict[str, Dict[str, Any]] = {
    "plain": {},
    "workdir+cloud": {"workdir": "sub/dir", "cloud_auth": ("aws", "gcp", "snowflake")},
    "all-add-ons": {"add_ons": _thin.ADD_ONS},
}


def _text(**kwargs: Any) -> str:
    config = PipelineConfig(provider=PipelineProvider.GITLAB_CI, **kwargs)
    return PipelineTemplateGenerator().generate_pipeline(config)[".gitlab-ci.yml"]


def _documents(**kwargs: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    header, body = yaml.safe_load_all(_text(**kwargs))
    return header, body


def _scripts(job: Dict[str, Any]) -> Iterator[str]:
    for key in ("before_script", "script", "after_script"):
        yield from (str(line) for line in job.get(key) or [])


def _jobs(body: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in body.items() if isinstance(v, dict) and "script" in v}


# ── the Run pipeline form ──────────────────────────────────────────────────────


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_the_form_is_spec_inputs_within_gitlabs_limit(config: str) -> None:
    header, body = _documents(**_CONFIGS[config])

    inputs = header["spec"]["inputs"]
    assert len(inputs) <= MAX_INPUTS == 20
    expected = {n.lower() for n in STAGE_TOGGLES.values()} | {k.lower() for k in FORM_KNOBS}
    assert set(inputs) == expected
    for key, spec in inputs.items():
        assert "default" in spec, key  # a push or merge request pipeline sets none
        assert body["variables"][key.upper()] == f"$[[ inputs.{key} ]]"


def test_no_input_is_pasted_into_a_script() -> None:
    _, body = _documents(**_CONFIGS["all-add-ons"])

    for name, job in _jobs(body).items():
        for line in _scripts(job):
            assert "$[[" not in line, (name, line)


def test_the_form_never_takes_the_install_settings() -> None:
    header, body = _documents()

    inputs = header["spec"]["inputs"]
    for name in _thin.INSTALL_VARIABLES:
        assert name.lower() not in inputs
        assert isinstance(body["variables"][name], str)
    # Pipeline variables are no longer needed for the form.
    assert "No one allowed" in _text()


def test_a_value_off_the_form_is_a_plain_default_a_project_variable_sets() -> None:
    _, body = _documents()

    for name, value in body["variables"].items():
        assert isinstance(value, str), name  # no prefilled-variable entry on the form


def test_a_blank_choice_is_a_regex_and_a_closed_choice_is_options() -> None:
    inputs = _documents()[0]["spec"]["inputs"]

    assert inputs["apply_mode"]["options"][0] == "dry-run"
    assert re.fullmatch(inputs["scheduler"]["regex"], "")
    assert re.fullmatch(inputs["scheduler"]["regex"], "airflow")
    assert not re.fullmatch(inputs["scheduler"]["regex"], "airflow; rm -rf /")
    assert inputs["run_stage_10_publish"]["options"] == ["true", "false"]


@pytest.mark.parametrize("config", sorted(_CONFIGS))
def test_both_documents_validate_against_gitlabs_schema(config: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft7Validator(json.loads(_SCHEMA.read_text(encoding="utf-8")))

    for document in _documents(**_CONFIGS[config]):
        errors = [
            f"{list(e.absolute_path)}: {e.message[:160]}" for e in validator.iter_errors(document)
        ]
        assert errors == [], errors


# ── the release packet ─────────────────────────────────────────────────────────


def test_the_plan_publishes_the_packet_on_the_default_branch_only() -> None:
    plan = _documents()[1]["fluid-plan"]

    ship = next(line for line in plan["script"] if line.startswith("fluid ship"))
    assert ship.endswith(f'${{{RELEASE_VARIABLE}:+--release-dir "${RELEASE_VARIABLE}"}}')
    branch = [r for r in plan["rules"] if "variables" in r]
    assert branch == [
        {
            "if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH",
            "variables": {RELEASE_VARIABLE: RELEASE_DIR},
        }
    ]
    assert f"{RELEASE_DIR}/" in plan["artifacts"]["paths"]


@pytest.mark.parametrize("value, argv", [("release", ["--release-dir", "release"]), ("", [])])
def test_the_release_argument_is_one_word_or_nothing(value: str, argv: List[str]) -> None:
    line = f'${{{RELEASE_VARIABLE}:+--release-dir "${RELEASE_VARIABLE}"}}'
    out = subprocess.run(
        ["sh", "-c", f'set -- {line}; for a; do printf "%s\\n" "$a"; done'],
        env={RELEASE_VARIABLE: value, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert out == argv


def test_apply_downloads_the_plan_jobs_packet_or_does_not_start() -> None:
    apply = _documents()[1]["fluid-apply"]

    assert apply["needs"] == [{"job": "fluid-plan", "artifacts": True}]
    assert apply["dependencies"] == ["fluid-plan"]
    guard, ship = apply["script"][-2:]
    assert guard.startswith(f"test -f {RELEASE_DIR}/release.json ||")
    assert ship == "fluid ship --stage 7-11 --yes"


def test_no_apply_on_a_merge_request_a_schedule_or_any_other_ref() -> None:
    rules = _documents()[1]["fluid-apply"]["rules"]

    assert rules[:2] == [
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"', "when": "never"},
        {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
    ]
    # The one rule that adds the job: the project's default branch.
    assert rules[2:] == [{"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH", "when": "manual"}]


# ── evidence from a failed plan ────────────────────────────────────────────────


def _after_script_effect(tmp_path: Path, status: str, workdir: str = "") -> List[str]:
    from fluid_build.cli._ship_stages import ARTIFACTS_DIR, BUNDLE_PATH, PLAN_PATH

    plan = _documents(**({"workdir": workdir} if workdir else {}))[1]["fluid-plan"]
    root = tmp_path / workdir if workdir else tmp_path
    for rel in (BUNDLE_PATH, PLAN_PATH, "runtime/plan.tfplan.json", "runtime/ship-report.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    (root / ARTIFACTS_DIR).mkdir(parents=True)
    (root / ARTIFACTS_DIR / "odcs.yaml").write_text("x", encoding="utf-8")
    (root / RELEASE_DIR).mkdir()
    (root / RELEASE_DIR / "release.json").write_text("{}", encoding="utf-8")
    script = "\n".join(plan["after_script"])
    subprocess.run(
        ["sh", "-ec", script],
        cwd=tmp_path,
        env={"CI_JOB_STATUS": status, "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
    )
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_a_failed_plan_keeps_its_reports_and_drops_what_could_be_deployed(tmp_path: Path) -> None:
    assert _after_script_effect(tmp_path, "failed") == ["runtime/ship-report.json"]


def test_a_passed_plan_keeps_everything(tmp_path: Path) -> None:
    assert len(_after_script_effect(tmp_path, "success")) == 6


def test_the_evidence_rule_runs_in_the_workdir(tmp_path: Path) -> None:
    assert _after_script_effect(tmp_path, "canceled", "sub/dir") == ["runtime/ship-report.json"]


# ── the merge request's impact report ──────────────────────────────────────────


def test_a_merge_request_gets_an_impact_report_beside_the_plan() -> None:
    impact = _documents()[1]["fluid-impact"]

    assert impact["rules"] == [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]
    assert impact["needs"] == [{"job": "fluid-plan", "artifacts": True}]
    assert impact["allow_failure"] is True
    assert impact["artifacts"]["expose_as"] == "fluid impact"
    diff = shlex.split(impact["script"][-1])
    assert diff[:3] == ["fluid", "diff", "contract.fluid.yaml"]
    assert {"--impact", "--baseline", "--format", "markdown", "--out"} <= set(diff)


def test_the_impact_job_holds_no_cloud_credential() -> None:
    impact = _documents(cloud_auth=("aws", "gcp"))[1]["fluid-impact"]

    assert "id_tokens" not in impact and "variables" not in impact
    assert not any("OIDC" in line for line in _scripts(impact))


def test_the_impact_job_reads_the_base_with_git(tmp_path: Path) -> None:
    """The merge request's base, unpacked from git, is what --baseline reads."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode:
        pytest.skip("git is not on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "contract.fluid.yaml").write_text("id: old\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    base = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "contract.fluid.yaml").write_text("id: new\n", encoding="utf-8")

    lines = _documents()[1]["fluid-impact"]["script"]
    unpack = [line for line in lines if "git archive" in line or "mktemp" in line]
    script = "\n".join([*unpack, 'cat "$base"/contract.fluid.yaml'])
    out = subprocess.run(
        ["sh", "-ec", script],
        cwd=repo,
        env={"CI_MERGE_REQUEST_DIFF_BASE_SHA": base, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == "id: old\n"


# ── what an input can hold ─────────────────────────────────────────────────────


def test_every_input_is_closed_by_options_or_a_regex() -> None:
    for key, spec in _documents()[0]["spec"]["inputs"].items():
        assert "options" in spec or "regex" in spec, key


@pytest.mark.parametrize(
    "key, good, bad",
    [
        ("fluid_env", ["dev", "prod", "eu.prod-2"], ["$CI_JOB_TOKEN", "../prod", "", "2prod"]),
        (
            "publish_targets",
            ["datamesh-manager", "datahub openmetadata", ""],
            ["$SECRET", "a;b", "a  b", "x\ny"],
        ),
    ],
)
def test_a_free_text_input_cannot_name_another_variable(
    key: str, good: List[str], bad: List[str]
) -> None:
    pattern = _documents()[0]["spec"]["inputs"][key]["regex"]
    for value in good:
        assert re.search(pattern, value), (key, value)
    for value in bad:
        assert not re.search(pattern, value), (key, value)


# ── what the impact job writes ─────────────────────────────────────────────────


def test_the_impact_command_runs_and_names_why_the_resources_are_unknown(tmp_path: Path) -> None:
    """The job's own lines, run against ship's plan.json (fluid actions, no OpenTofu
    plan): the report is written, and its resource section says why it is unknown."""
    import sys

    import fluid_build

    contract = Path(__file__).parents[3] / "fixtures" / "impact" / "base.fluid.yaml"
    work, base, bin_dir = tmp_path / "work", tmp_path / "base", tmp_path / "bin"
    for d in (work / "runtime", base, bin_dir):
        d.mkdir(parents=True)
    (work / "contract.fluid.yaml").write_text(contract.read_text("utf-8"), "utf-8")
    (base / "contract.fluid.yaml").write_text(contract.read_text("utf-8"), "utf-8")
    (work / "runtime" / "plan.json").write_text(json.dumps({"actions": []}), "utf-8")
    shim = bin_dir / "fluid"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m fluid_build.cli "$@"\n', "utf-8")
    shim.chmod(0o700)

    lines = _documents()[1]["fluid-impact"]["script"]
    run = [lines[0], *lines[-2:]]  # mkdir, the plan switch and fluid diff
    subprocess.run(
        ["sh", "-ec", f'base="{base}"\n' + "\n".join(run)],
        cwd=work,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "PYTHONPATH": str(Path(fluid_build.__file__).parents[1]),
            "HOME": str(tmp_path),
        },
        check=True,
        capture_output=True,
    )

    document = json.loads((work / "runtime" / "impact" / "impact.json").read_text("utf-8"))
    resources = document["impact"]["resource_impact"]
    assert resources["status"] == "unknown" and "fluid plan --tofu" in resources["reason"]
    assert "## Impact" in (work / "runtime" / "impact" / "impact.md").read_text("utf-8")
