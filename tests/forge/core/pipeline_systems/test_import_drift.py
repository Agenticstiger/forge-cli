# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``import-drift`` add-on: ``fluid import <tool> <source> --check`` as a gate.

The add-on runs on every pull or merge request and on every push to the default
branch, fails its job on drift (exit 1) or when it cannot decide (exit 2), and
neither waits for nor feeds the apply approval. Every value in the command is
one quoted shell word, never an expansion a run could override.
"""

from __future__ import annotations

import argparse
import logging
import shlex
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.cli._common import CLIError
from fluid_build.cli.generate_ci import ADD_ONS as CLI_ADD_ONS
from fluid_build.cli.generate_ci import run as generate_ci_run
from fluid_build.forge.core.pipeline_systems import _thin
from fluid_build.forge.core.pipeline_systems._thin import ImportDrift, import_drift_command
from fluid_build.forge.core.pipeline_templates import (
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

pytestmark = pytest.mark.unit

P = PipelineProvider
SYSTEMS = list(PipelineProvider)
_LOG = logging.getLogger("test_import_drift")
_TEXT = "fluid import airflow dags --check --format text"
_GITHUB = "fluid import airflow dags --check --format github"

#: What makes a step advisory in one CI system or another.
_ADVISORY = ("continue-on-error", "allow_failure", "continueOnError", "|| true", "|| echo")


def _files(provider: PipelineProvider, **kwargs: Any) -> Dict[str, str]:
    kwargs.setdefault("add_ons", ("import-drift",))
    kwargs.setdefault("import_drift", ImportDrift("airflow", "dags"))
    return PipelineTemplateGenerator().generate_pipeline(
        PipelineConfig(provider=provider, **kwargs)
    )


def _expected(provider: PipelineProvider) -> str:
    return _GITHUB if provider is P.GITHUB_ACTIONS else _TEXT


def _all_text(files: Dict[str, str]) -> str:
    return "\n".join(files.values())


def _gitlab(files: Dict[str, str]) -> Dict[str, Any]:
    _header, body = yaml.safe_load_all(files[".gitlab-ci.yml"])
    return body


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_two_add_on_tuples_stay_equal():
    assert tuple(CLI_ADD_ONS) == tuple(_thin.ADD_ONS)
    assert "import-drift" in _thin.ADD_ONS
    assert _thin.ADD_ON_TITLES["import-drift"] == "import drift check"


def test_the_command_for_each_format():
    spec = ImportDrift("airflow", "dags")
    assert import_drift_command(spec, "text") == _TEXT
    assert import_drift_command(spec, "github") == _GITHUB
    with pytest.raises(ValueError, match="format"):
        import_drift_command(spec, "json")


def test_the_command_carries_every_option_as_one_word():
    spec = ImportDrift(
        "pyspark",
        "jobs dir",
        out="contracts/out",
        split_by="job",
        conventions="conv.yaml",
        dags="the dags",
    )
    command = import_drift_command(spec, "text")
    assert command == (
        "fluid import pyspark 'jobs dir' --dags 'the dags' --conventions conv.yaml "
        "--split-by job --out contracts/out --check --format text"
    )
    assert shlex.split(command)[3] == "jobs dir"


@pytest.mark.parametrize("fmt", ["text", "github"])
def test_the_command_parses_with_the_real_parser(fmt):
    spec = ImportDrift(
        "pyspark", "jobs", out="out", split_by="dag", conventions="c.yaml", dags="dags"
    )
    argv = shlex.split(import_drift_command(spec, fmt))
    assert argv[0] == "fluid"
    args = build_parser().parse_args(argv[1:])
    assert args.check is True
    assert args.check_format == fmt
    assert args.dags == "dags" and args.conventions == "c.yaml"
    assert args.split_by == "dag" and args.out_path == "out"


def test_a_source_is_never_an_expansion():
    command = import_drift_command(ImportDrift("airflow", "dags"), "text")
    assert "${" not in command and "$(" not in command


# ---------------------------------------------------------------------------
# What the spec refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "dags; rm -rf /",
        "dags;ls",
        "$(whoami)",
        "`whoami`",
        "da'gs",
        'da"gs',
        "dags\nrm -rf /",
        "dags && ls",
        "dags | cat",
        "dags > out",
        "${HOME}/dags",
        "-dags",
        "--out=x",
        "dags\\x",
    ],
)
@pytest.mark.parametrize("field", ["source", "out", "conventions"])
def test_a_path_that_is_code_or_an_option_is_refused(field, bad):
    kwargs: Dict[str, str] = {"source": "dags"}
    kwargs[field] = bad
    with pytest.raises(ValueError):
        ImportDrift("airflow", **kwargs)


def test_dags_is_checked_too():
    with pytest.raises(ValueError):
        ImportDrift("pyspark", "jobs", dags="d; ls")
    with pytest.raises(ValueError):
        ImportDrift("pyspark", "jobs", dags="-d")


def test_a_path_with_a_space_is_quoted_not_refused():
    command = import_drift_command(ImportDrift("airflow", "my dags"), "text")
    assert command == "fluid import airflow 'my dags' --check --format text"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"tool": "dbt", "source": "x"}, "import tool"),
        ({"tool": "airflow", "source": ""}, "empty"),
        ({"tool": "airflow", "source": "x", "split_by": "group"}, "split-by"),
        ({"tool": "airflow", "source": "x", "dags": "d"}, "pyspark"),
    ],
)
def test_the_spec_refuses_a_value_the_import_does_not_take(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ImportDrift(**kwargs)


@pytest.mark.parametrize("split_by", list(_thin.IMPORT_SPLIT_BY))
def test_every_split_by_is_accepted_and_parses(split_by):
    command = import_drift_command(ImportDrift("airflow", "dags", split_by=split_by), "text")
    assert build_parser().parse_args(shlex.split(command)[1:]).split_by == split_by


@pytest.mark.parametrize("provider", SYSTEMS, ids=lambda p: p.name.lower())
def test_the_add_on_without_its_settings_is_refused(provider):
    with pytest.raises(ValueError, match="import_drift"):
        PipelineTemplateGenerator().generate_pipeline(
            PipelineConfig(provider=provider, add_ons=("import-drift",))
        )


# ---------------------------------------------------------------------------
# Every system: the command, once, as a gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SYSTEMS, ids=lambda p: p.name.lower())
def test_the_command_appears_once_and_nothing_is_advisory(provider):
    files = _files(provider)
    text = _all_text(files)
    # Bitbucket runs it in its pull-request and its branch pipeline.
    runs = 2 if provider is P.BITBUCKET else 1
    assert text.count(_expected(provider)) == runs, provider
    assert text.count("fluid import airflow") == runs
    other = _GITHUB if provider is not P.GITHUB_ACTIONS else _TEXT
    assert other not in text


@pytest.mark.parametrize("provider", SYSTEMS, ids=lambda p: p.name.lower())
def test_the_workdir_is_entered_before_the_command(provider):
    text = _all_text(_files(provider, workdir="sub/dir"))
    if provider is P.GITHUB_ACTIONS:
        workflow = yaml.safe_load(_files(provider, workdir="sub/dir")[_GITHUB_PATH])
        assert workflow["jobs"]["import-drift"]["defaults"]["run"]["working-directory"] == "sub/dir"
    elif provider is P.TEKTON:
        assert 'cd "sub/dir"' in text
    else:
        assert 'cd "sub/dir"' in text
    assert text.count(_expected(provider)) == (2 if provider is P.BITBUCKET else 1)


_GITHUB_PATH = ".github/workflows/fluid-import-drift.yml"


def test_github_has_its_own_read_only_workflow():
    files = _files(P.GITHUB_ACTIONS)
    assert _GITHUB_PATH in files
    workflow = yaml.safe_load(files[_GITHUB_PATH])
    assert workflow["permissions"] == {}
    assert list(workflow["jobs"]) == ["import-drift"]
    job = workflow["jobs"]["import-drift"]
    assert job["permissions"] == {"contents": "read"}
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert triggers["push"] == {"branches": ["main"]}
    assert "schedule" not in triggers
    runs = [step["run"] for step in job["steps"] if "run" in step]
    assert runs[-1] == _GITHUB
    assert not any(word in files[_GITHUB_PATH] for word in _ADVISORY)
    # The pipeline workflow itself is untouched by the add-on.
    assert (
        files[".github/workflows/fluid-pipeline.yml"]
        == _files(P.GITHUB_ACTIONS, add_ons=())[".github/workflows/fluid-pipeline.yml"]
    )


def test_github_follows_the_default_branch():
    files = _files(P.GITHUB_ACTIONS, default_branch="trunk")
    triggers = yaml.safe_load(files[_GITHUB_PATH])
    triggers = triggers.get("on", triggers.get(True))
    assert triggers["pull_request"] == {"branches": ["trunk"]}
    assert triggers["push"] == {"branches": ["trunk"]}


def test_gitlab_runs_it_on_merge_requests_and_the_default_branch_only():
    ci = _gitlab(_files(P.GITLAB_CI))
    job = ci["fluid-import-drift"]
    assert job["script"] == [_TEXT]
    assert job["rules"] == [
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
        {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
    ]
    assert "allow_failure" not in job and "artifacts" not in job
    assert job["needs"] == []
    # It adds no scheduled pipeline, and apply still needs only the plan.
    assert not any("schedule" in str(rule) for rule in ci["workflow"]["rules"])
    assert ci["fluid-apply"]["needs"] == [{"job": "fluid-plan", "artifacts": True}]


def test_jenkins_runs_it_before_the_apply_stage_on_every_build():
    text = _files(P.JENKINS)["Jenkinsfile"]
    marker = "stage('import drift check') {"
    start = text.index(marker)
    stage = text[start : text.index("        }\n", start)]
    assert "when" not in stage  # pull requests and the default branch alike
    assert _TEXT in stage
    assert not any(word in stage for word in _ADVISORY)
    assert "archiveArtifacts" not in stage
    assert start < text.index("stage('7 - apply')")


def test_azure_runs_it_as_its_own_stage_on_every_run():
    stages = yaml.safe_load(_files(P.AZURE_DEVOPS)["azure-pipelines.yml"])["stages"]
    stage = next(s for s in stages if s["stage"] == "import_drift")
    assert stage["dependsOn"] == []
    assert "condition" not in stage  # a pull request validation and the branch alike
    steps = stage["jobs"][0]["steps"]
    run = steps[-1]
    assert run["bash"].strip() == _TEXT
    assert not any(word in yaml.safe_dump(stage) for word in _ADVISORY)
    apply_stage = next(s for s in stages if s["stage"] == "apply")
    assert "import_drift" not in str(apply_stage.get("dependsOn", ""))


def test_bitbucket_runs_it_beside_the_plan_on_pull_requests_and_the_branch():
    document = yaml.safe_load(_files(P.BITBUCKET)["bitbucket-pipelines.yml"])
    pipelines = document["pipelines"]
    for parallel in (pipelines["pull-requests"]["**"], pipelines["branches"]["main"]):
        group = parallel[0]["parallel"]
        steps = [item["step"] for item in group]
        gate = next(s for s in steps if s["name"] == "import drift check")
        assert gate["script"][-1].strip() == _TEXT
        assert "artifacts" not in gate
        assert not any(word in yaml.safe_dump(gate) for word in _ADVISORY)
    assert "import drift check" not in str(pipelines["custom"])


def test_circleci_runs_it_on_every_push_and_apply_never_waits_for_it():
    config = yaml.safe_load(_files(P.CIRCLE_CI)[".circleci/config.yml"])
    job = config["jobs"]["import-drift"]
    commands = [step["run"]["command"] for step in job["steps"] if "run" in step]
    assert commands[-1].strip() == _TEXT
    assert not any(word in yaml.safe_dump(job) for word in _ADVISORY)
    jobs = config["workflows"]["fluid"]["jobs"]
    assert "import-drift" in jobs  # a bare name: no branch filter
    approve = next(j["approve-apply"] for j in jobs if isinstance(j, dict) and "approve-apply" in j)
    assert approve["requires"] == ["plan"]


def test_tekton_adds_a_task_and_a_pipeline_that_apply_never_references():
    files = _files(P.TEKTON)
    documents: List[Dict[str, Any]] = []
    for text in files.values():
        documents += [d for d in yaml.safe_load_all(text) if d]
    task = next(d for d in documents if d["metadata"]["name"] == "fluid-ship-import-drift")
    script = task["spec"]["steps"][0]["script"]
    assert script.strip().splitlines()[-1] == _TEXT
    assert "set -eu" in script
    assert not any(word in script for word in _ADVISORY)
    pipeline = next(
        d
        for d in documents
        if d["kind"] == "Pipeline" and d["metadata"]["name"] == "fluid-import-drift"
    )
    assert [t["taskRef"]["name"] for t in pipeline["spec"]["tasks"]] == ["fluid-ship-import-drift"]
    assert "every pull request" in pipeline["spec"]["description"]
    apply = next(d for d in documents if d["metadata"]["name"] == "fluid-apply")
    assert "import-drift" not in yaml.safe_dump(apply)


# ---------------------------------------------------------------------------
# Absent, the output does not change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", SYSTEMS, ids=lambda p: p.name.lower())
def test_without_the_add_on_the_output_is_the_same(provider):
    plain = PipelineTemplateGenerator().generate_pipeline(PipelineConfig(provider=provider))
    with_settings = _files(provider, add_ons=())
    assert plain == with_settings
    assert "fluid import" not in _all_text(plain)
    others = _files(provider, add_ons=("security", "sbom"))
    assert "fluid import" not in _all_text(others)


@pytest.mark.parametrize("provider", SYSTEMS, ids=lambda p: p.name.lower())
def test_the_add_on_leaves_the_pipeline_files_alone(provider):
    plain = _files(provider, add_ons=())
    with_it = _files(provider)
    if provider is P.GITHUB_ACTIONS:
        assert {k: v for k, v in with_it.items() if k != _GITHUB_PATH} == plain
    elif provider is P.GITLAB_CI:
        assert _gitlab(with_it)["fluid-plan"] == _gitlab(plain)["fluid-plan"]
        assert _gitlab(with_it)["fluid-apply"] == _gitlab(plain)["fluid-apply"]
        assert _gitlab(with_it)["workflow"] == _gitlab(plain)["workflow"]


# ---------------------------------------------------------------------------
# fluid generate ci
# ---------------------------------------------------------------------------


def _args(**extra: Any) -> argparse.Namespace:
    values: Dict[str, Any] = {
        "system": "gitlab",
        "out": None,
        "contract": "contract.fluid.yaml",
        "add": None,
    }
    values.update(extra)
    return argparse.Namespace(**values)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> Dict[str, str]:
    monkeypatch.chdir(tmp_path)
    assert generate_ci_run(_args(**extra), _LOG) == 0
    return {
        str(p.relative_to(tmp_path)): p.read_text(encoding="utf-8")
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file()
    }


def test_the_flags_build_the_command(tmp_path, monkeypatch):
    files = _run(
        tmp_path,
        monkeypatch,
        add=["import-drift"],
        import_tool="pyspark",
        import_source="jobs",
        import_out="contracts",
        import_split_by="job",
        import_conventions="conv.yaml",
        import_dags="dags",
    )
    expected = (
        "fluid import pyspark jobs --dags dags --conventions conv.yaml --split-by job "
        "--out contracts --check --format text"
    )
    assert expected in files[".gitlab-ci.yml"]


@pytest.mark.parametrize(
    "system", ["github", "gitlab", "jenkins", "azure", "bitbucket", "circleci", "tekton"]
)
def test_every_system_gets_the_command_from_the_flags(tmp_path, monkeypatch, system):
    files = _run(
        tmp_path,
        monkeypatch,
        system=system,
        add=["import-drift"],
        import_tool="airflow",
        import_source="dags",
    )
    fmt = "github" if system == "github" else "text"
    command = f"fluid import airflow dags --check --format {fmt}"
    runs = 2 if system == "bitbucket" else 1  # its pull-request and its branch pipeline
    assert sum(text.count(command) for text in files.values()) == runs


def test_the_add_on_needs_the_tool_and_the_source(tmp_path, monkeypatch):
    for extra in (
        {},
        {"import_tool": "airflow"},
        {"import_source": "dags"},
    ):
        with pytest.raises(CLIError) as err:
            _run(tmp_path, monkeypatch, add=["import-drift"], **extra)
        assert err.value.event == "generate_ci_import_drift_incomplete"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "extra",
    [
        {"import_tool": "airflow"},
        {"import_source": "dags"},
        {"import_out": "out"},
        {"import_split_by": "job"},
        {"import_conventions": "c.yaml"},
        {"import_dags": "dags"},
    ],
)
def test_an_import_flag_without_the_add_on_is_refused(tmp_path, monkeypatch, extra):
    with pytest.raises(CLIError) as err:
        _run(tmp_path, monkeypatch, add=["security"], **extra)
    assert err.value.event == "generate_ci_import_flags_without_add_on"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad", ["dags; ls", "$(id)", "`id`", "a'b", "a\nb", "-x"])
def test_an_unsafe_source_is_refused_before_anything_is_written(tmp_path, monkeypatch, bad):
    with pytest.raises(CLIError) as err:
        _run(
            tmp_path,
            monkeypatch,
            add=["import-drift"],
            import_tool="airflow",
            import_source=bad,
        )
    assert err.value.event == "generate_ci_invalid_option"
    assert not list(tmp_path.iterdir())


def test_dags_belongs_to_pyspark(tmp_path, monkeypatch):
    with pytest.raises(CLIError) as err:
        _run(
            tmp_path,
            monkeypatch,
            add=["import-drift"],
            import_tool="airflow",
            import_source="dags",
            import_dags="d",
        )
    assert err.value.event == "generate_ci_invalid_option"


def test_the_parser_takes_the_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "generate",
            "ci",
            "--add",
            "import-drift",
            "--import-tool",
            "airflow",
            "--import-source",
            "dags",
            "--import-split-by",
            "dag",
        ]
    )
    assert args.add == ["import-drift"]
    assert args.import_tool == "airflow" and args.import_source == "dags"
    with pytest.raises(SystemExit):
        parser.parse_args(["generate", "ci", "--import-tool", "dbt"])
    with pytest.raises(SystemExit):
        parser.parse_args(["generate", "ci", "--import-split-by", "group"])
