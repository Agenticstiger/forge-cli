# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Jenkinsfile ``fluid generate ci --system jenkins`` writes runs as generated.

Measured against a lab Jenkins (Debian, Python 3.13 with python3-venv, PEP 668,
no ws-cleanup): the generated file named ``contract.fluid.yaml`` whatever
contract it was generated for, ran a bare ``pip install`` stage 0 cannot run,
ended in ``cleanWs()`` (a plugin), read GENERATE_EMIT with no fallback on a
parameterless build, archived a plan.html written elsewhere, and expanded the
pip index URLs unquoted. These tests pin the file's shape and run its shell
bodies with ``sh``.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest

from fluid_build import __version__
from fluid_build.cli import _ship_stages as st
from fluid_build.cli import ship
from fluid_build.cli.generate_ci import run as generate_ci_run
from fluid_build.forge.core.pipeline_systems import _thin
from fluid_build.forge.core.pipeline_templates import (
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

_LOG = logging.getLogger("test_jenkins_generated_pipeline")

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.customer_subscriptions
name: Customer Subscriptions
description: A product with a local base binding and cloud overlays.
domain: Customer
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
builds:
  - id: ingest_subscriptions
    description: Full refresh.
    pattern: embedded-logic
    engine: duckdb
    properties:
      sql: "SELECT 1 AS id"
    outputs: [subscriptions]
exposes:
  - exposeId: subscriptions
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/subscriptions.parquet
    contract:
      schema:
        - {name: id, type: INTEGER, required: true}
"""
_AWS = "exposes:\n  - binding:\n      platform: aws\n      format: parquet\n"
_GCP = "exposes:\n  - binding:\n      platform: gcp\n      format: bigquery_table\n"


def _jenkinsfile(**kwargs: object) -> str:
    config = PipelineConfig(provider=PipelineProvider.JENKINS, **kwargs)
    return PipelineTemplateGenerator().generate_pipeline(config)["Jenkinsfile"]


def _stage(content: str, label: str) -> str:
    start = content.index(f"stage('{label}')")
    # The next stage, at any depth: stages 7-11 sit inside the apply stage.
    nxt = re.search(r"\n\s+stage\('", content[start + 10 :])
    return content[start : start + 10 + nxt.start() if nxt else content.index("    post {")]


def _sh_bodies(text: str) -> List[str]:
    return re.findall(r"sh '''(.*?)'''", text, re.S)


def _declared(content: str) -> Dict[str, str]:
    """``{parameter: declared default}`` from the ``parameters {}`` block."""
    block = content[content.index("    parameters {") : content.index("    environment {")]
    declared: Dict[str, str] = {}
    for name, value in re.findall(r"name: '(\w+)', defaultValue: '([^']*)'", block):
        declared[name] = value
    for name, value in re.findall(r"name: '(\w+)', defaultValue: (true|false)", block):
        declared[name] = value
    for name, first in re.findall(r"choice\(name: '(\w+)',\s*choices: \['([^']*)'", block):
        declared[name] = first  # Jenkins' default for a choice is its first
    return declared


def _environment_fallbacks(content: str) -> Dict[str, str]:
    """``{NAME: fallback}`` of the ``environment {}`` entries ship reads."""
    return dict(
        re.findall(r'^\s+(\w+) = "\$\{params\.\1 == null \? \'([^\']*)\' : ', content, re.M)
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository holding the product under contracts/customer_subscriptions/."""
    root = tmp_path / "repo"
    product = root / "contracts" / "customer_subscriptions"
    (product / "overlays").mkdir(parents=True)
    (product / "contract.fluid.yaml").write_text(_CONTRACT, encoding="utf-8")
    (product / "overlays" / "aws.yaml").write_text(_AWS, encoding="utf-8")
    (product / "overlays" / "gcp.yaml").write_text(_GCP, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _generate(cwd: Path, contract: str, monkeypatch: pytest.MonkeyPatch, **options: object) -> str:
    monkeypatch.chdir(cwd)
    args = argparse.Namespace(system="jenkins", out="Jenkinsfile", contract=contract, **options)
    assert generate_ci_run(args, _LOG) == 0
    return (cwd / "Jenkinsfile").read_text(encoding="utf-8")


# ── (1) the contract given, from wherever it is generated ─────────────────


def test_generated_from_the_repository_root_names_the_contract_from_there(repo, monkeypatch):
    content = _generate(repo, "contracts/customer_subscriptions/contract.fluid.yaml", monkeypatch)
    path = "contracts/customer_subscriptions/contract.fluid.yaml"
    assert f"name: 'CONTRACT', defaultValue: '{path}'" in content
    assert 'cd "' not in content  # runs at the checkout root
    # Ship is given the contract through the environment, with the same fallback.
    assert f"CONTRACT = \"${{params.CONTRACT == null ? '{path}' : params.CONTRACT}}\"" in content
    assert 'fluid ship "$CONTRACT" --stage 1' in _stage(content, "1 - bundle")


def test_generated_from_the_contract_directory_cds_there(repo, monkeypatch):
    product = repo / "contracts" / "customer_subscriptions"
    content = _generate(product, "contract.fluid.yaml", monkeypatch)
    assert "name: 'CONTRACT', defaultValue: 'contract.fluid.yaml'" in content
    assert 'cd "contracts/customer_subscriptions" && set -eu' in content
    # Every ship step runs from the product's directory, archives from the root.
    for label in ("1 - bundle", "6 - plan", "7 - apply"):
        assert 'cd "contracts/customer_subscriptions" && set -eu' in _stage(content, label)
    assert "archiveArtifacts artifacts: 'contracts/customer_subscriptions/runtime/" in content


def test_a_contract_outside_the_current_directory_is_named_from_its_repository_root(
    repo, monkeypatch
):
    elsewhere = repo / "pipelines"
    elsewhere.mkdir()
    content = _generate(
        elsewhere, "../contracts/customer_subscriptions/contract.fluid.yaml", monkeypatch
    )
    assert (
        "name: 'CONTRACT', defaultValue: 'contracts/customer_subscriptions/contract.fluid.yaml'"
        in content
    )
    assert 'cd "' not in content  # not the shell's cwd: the repository root


# ── (2) stage 0: a venv in the workspace, the generating version pinned ───


def test_the_package_spec_pins_this_version_with_the_extras_of_every_overlay(repo, monkeypatch):
    content = _generate(
        repo / "contracts" / "customer_subscriptions", "contract.fluid.yaml", monkeypatch
    )
    spec = f"data-product-forge[aws,gcp,local]=={__version__}"
    assert f"name: 'FLUID_PACKAGE_SPEC', defaultValue: '{spec}'" in content
    assert f'-- "${{FLUID_PACKAGE_SPEC:-{spec}}}"' in content


def test_the_package_spec_option_overrides_and_an_option_shaped_value_is_refused(repo, monkeypatch):
    product = repo / "contracts" / "customer_subscriptions"
    content = _generate(
        product, "contract.fluid.yaml", monkeypatch, fluid_package_spec="/wheels/x.whl[local]"
    )
    assert "name: 'FLUID_PACKAGE_SPEC', defaultValue: '/wheels/x.whl[local]'" in content
    from fluid_build.cli._common import CLIError

    for bad in ("--index-url=http://x", "a$(id)", "", "x'y"):
        with pytest.raises(CLIError):
            _generate(product, "contract.fluid.yaml", monkeypatch, fluid_package_spec=bad)


def test_stage_0_installs_into_a_workspace_venv_every_stage_runs_from():
    content = _jenkinsfile()
    stage0 = _stage(content, "0 — Bootstrap FLUID [pypi]")
    assert 'python3 -m venv "$FLUID_VENV"' in stage0
    assert '"$FLUID_VENV/bin/python" -m pip install' in stage0
    assert not re.search(r"(^|\s)pip install", stage0.replace("-m pip install", ""))
    start = content.index("    environment {")
    env = content[start : content.index("\n    }\n", start)]
    assert 'FLUID_VENV = "${env.WORKSPACE}/.fluid-venv"' in env
    assert 'PATH = "${env.WORKSPACE}/.fluid-venv/bin:${env.PATH}"' in env
    # The stage fails when PATH does not put that venv first.
    assert '"$(command -v fluid || true)" != "$FLUID_VENV/bin/fluid"' in stage0
    # A checkout's modules never shadow the installed CLI.
    assert "PYTHONPATH" not in env


@pytest.fixture
def fake_python(tmp_path: Path) -> Path:
    """``python3`` whose ``-m venv DIR`` makes DIR/bin/python record its argv."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "pip-argv.txt"
    recorder = "#!/bin/sh\n" f"for a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{log}'\n"
    python3 = bin_dir / "python3"
    python3.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "-m" ] && [ "$2" = "venv" ] || exit 9\n'
        'mkdir -p "$3/bin"\n'
        f"cat > \"$3/bin/python\" <<'EOF'\n{recorder}EOF\n"
        'chmod +x "$3/bin/python"\n',
        encoding="utf-8",
    )
    python3.chmod(python3.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _run_install(fake_python: Path, env: Dict[str, str]) -> List[str]:
    stage0 = _stage(_jenkinsfile(), "0 — Bootstrap FLUID [pypi]")
    install = next(b for b in _sh_bodies(stage0) if "python3 -m venv" in b)
    workspace = fake_python / "ws"
    workspace.mkdir(exist_ok=True)
    subprocess.run(
        ["sh", "-c", install],
        cwd=workspace,
        env={
            "PATH": f"{fake_python / 'fake-bin'}{os.pathsep}{os.environ['PATH']}",
            "FLUID_VENV": str(workspace / ".fluid-venv"),
            **env,
        },
        check=True,
    )
    return (fake_python / "pip-argv.txt").read_text(encoding="utf-8").splitlines()


def test_pip_index_urls_and_the_spec_cannot_smuggle_pip_options(fake_python):
    """Each value is ONE pip argument: an index URL carrying ``--trusted-host``,
    or a package spec that is an option, cannot add a pip option."""
    argv = _run_install(
        fake_python,
        {
            "FLUID_PIP_INDEX_URL": "https://mirror.example/simple --trusted-host evil.example",
            "FLUID_PIP_EXTRA_INDEX_URL": "https://pypi.org/simple/ -r /etc/passwd",
            "FLUID_PACKAGE_SPEC": "--target=/tmp/elsewhere",
        },
    )
    assert argv[:2] == ["-m", "pip"]
    assert "--index-url=https://mirror.example/simple --trusted-host evil.example" in argv
    assert "--extra-index-url=https://pypi.org/simple/ -r /etc/passwd" in argv
    assert "--trusted-host" not in argv and "-r" not in argv
    # ``--`` ends pip's options: the spec is a requirement, whatever it says.
    assert argv[-2:] == ["--", "--target=/tmp/elsewhere"]


def test_a_parameterless_install_uses_the_declared_spec(fake_python):
    argv = _run_install(fake_python, {})
    assert argv[-2:] == ["--", f"data-product-forge=={__version__}"]
    assert not any(a.startswith(("--index-url", "--extra-index-url")) for a in argv)


# ── (3) no plugin for the workspace cleanup ───────────────────────────────


def test_the_workspace_is_removed_with_the_core_deleteDir_step():
    content = _jenkinsfile()
    assert "cleanWs" not in content
    post = content[content.index("    post {") :]
    cleanup = post[post.index("cleanup {") :]
    assert "deleteDir()" in cleanup[: cleanup.index("}")]
    assert "//   workflow-aggregator, git, lockable-resources, junit\n" in content


# ── (4) every read falls back to the declared default ─────────────────────


@pytest.mark.parametrize(
    "options",
    [
        {},
        {
            "apply_mode_default": "amend-and-build",
            "apply_build_id_default": "ingest_subscriptions",
            "default_publish_target": "fluid-command-center",
            "publish_stage_default": True,
            "schedule_sync_default": True,
            "scheduler_default": "airflow",
            "scheduler_destination_default": "file:///opt/airflow/dags",
            "contract_path": "contracts/p/contract.fluid.yaml",
            "diff_last_applied": True,
            "fluid_env_default": "aws",
        },
    ],
    ids=["defaults", "demo-options"],
)
def test_every_parameter_read_falls_back_to_the_default_it_declares(options):
    content = _jenkinsfile(**options)
    declared = _declared(content)
    assert len(declared) >= 30
    fallbacks = _environment_fallbacks(content)
    # Ship reads each of these from the environment: a parameterless build gets
    # exactly what the parameter declares.
    # (CATALOG is the legacy name of PUBLISH_TARGETS; DIFF_LAST_APPLIED is set by
    # stage 5's own shell, from the last build's plan.)
    assert set(_thin.ship_variables()) - {"CATALOG", "DIFF_LAST_APPLIED"} <= set(fallbacks)
    for name, fallback in fallbacks.items():
        assert fallback == declared[name], (name, fallback, declared[name])
    # The install's shell reads (stage 0) fall back the same way, and never bare.
    install = "\n".join(_sh_bodies(_stage(content, "0 — Bootstrap FLUID [pypi]")))
    for name, default in declared.items():
        for op, fallback in re.findall(r"\$\{" + name + r"(:-|-)([^}]*)\}", install):
            assert fallback == default, (name, op, fallback, default)
        if name not in fallbacks:
            assert not re.search(r"\$\{" + name + r"\}", install), name
            assert not re.search(r"\$" + name + r"\b", install), name
    # Every stage toggle's when{} falls back to the declared default too.
    for name, default in re.findall(r"params\.(RUN_STAGE_\w+) == null \? (true|false)", content):
        assert declared[name] == default, name


def test_the_demo_defaults_become_the_parameters_defaults():
    content = _jenkinsfile(
        apply_mode_default="amend-and-build",
        apply_build_id_default="ingest_subscriptions",
        default_publish_target="fluid-command-center",
        publish_stage_default=True,
        schedule_sync_default=True,
        scheduler_default="airflow",
        scheduler_destination_default="file:///opt/airflow/dags",
    )
    declared = _declared(content)
    assert declared["APPLY_MODE"] == "amend-and-build"
    assert declared["APPLY_BUILD_ID"] == "ingest_subscriptions"
    assert declared["PUBLISH_TARGETS"] == "fluid-command-center"
    assert declared["RUN_STAGE_10_PUBLISH"] == "true"
    assert declared["RUN_STAGE_11_SCHEDULE_SYNC"] == "true"
    assert declared["SCHEDULER"] == "airflow"
    assert declared["SCHEDULER_DESTINATION"] == "file:///opt/airflow/dags"


def test_safe_current_defaults_without_options():
    declared = _declared(_jenkinsfile())
    assert declared["APPLY_MODE"] == "dry-run"
    assert declared["APPLY_BUILD_ID"] == ""
    assert declared["PUBLISH_TARGETS"] == "datamesh-manager"
    assert declared["RUN_STAGE_10_PUBLISH"] == "false"
    assert declared["RUN_STAGE_11_SCHEDULE_SYNC"] == "false"
    assert declared["SCHEDULER"] == ""


def test_a_single_build_is_the_build_id_default_and_two_are_none(repo, monkeypatch):
    product = repo / "contracts" / "customer_subscriptions"
    content = _generate(product, "contract.fluid.yaml", monkeypatch)
    assert _declared(content)["APPLY_BUILD_ID"] == "ingest_subscriptions"
    two = _CONTRACT.replace(
        "exposes:",
        "  - id: second_build\n    pattern: embedded-logic\n    engine: duckdb\n"
        "    properties: {sql: 'SELECT 2 AS id'}\n    outputs: [subscriptions]\nexposes:",
        1,
    )
    (product / "contract.fluid.yaml").write_text(two, encoding="utf-8")
    assert _declared(_generate(product, "contract.fluid.yaml", monkeypatch))["APPLY_BUILD_ID"] == ""


# ── (5b) FLUID_ENV defaults to --fluid-env-default ───────────────────────


def test_the_fluid_env_default_is_the_parameter_default_and_every_fallback(repo, monkeypatch):
    """An upstream trigger starts the build with the declared defaults, and a
    first or post-restart build exports no parameters: both run in the env
    ``--fluid-env-default`` names, never in dev."""
    product = repo / "contracts" / "customer_subscriptions"
    content = _generate(product, "contract.fluid.yaml", monkeypatch, fluid_env_default="aws")
    assert _declared(content)["FLUID_ENV"] == "aws"
    # Ship reads FLUID_ENV from the environment {} block, with the default as fallback.
    assert _environment_fallbacks(content)["FLUID_ENV"] == "aws"
    # Stage 0's name check is the one shell read left.
    reads = re.findall(r"\$\{FLUID_ENV(:-|-)([^}]*)\}", "\n".join(_sh_bodies(content)))
    assert reads == [(":-", "aws")]
    assert "FLUID_ENV:-dev" not in content and "'dev'" not in _environment_block(content)


def _environment_block(content: str) -> str:
    start = content.index("    environment {")
    return content[start : content.index("\n    }\n", start)]


def test_without_the_option_only_dev_is_the_default(repo, monkeypatch):
    """No option: the file ``--fluid-env-default dev`` gives. The option
    changes the FLUID_ENV default and its fallbacks, and nothing else."""
    product = repo / "contracts" / "customer_subscriptions"
    default = _generate(product, "contract.fluid.yaml", monkeypatch)
    assert _declared(default)["FLUID_ENV"] == "dev"
    assert _generate(product, "contract.fluid.yaml", monkeypatch, fluid_env_default=None) == default
    assert (
        _generate(product, "contract.fluid.yaml", monkeypatch, fluid_env_default="dev") == default
    )
    staging = _generate(product, "contract.fluid.yaml", monkeypatch, fluid_env_default="staging")
    assert staging == (
        default.replace("${FLUID_ENV:-dev}", "${FLUID_ENV:-staging}")
        .replace(
            "name: 'FLUID_ENV', defaultValue: 'dev'", "name: 'FLUID_ENV', defaultValue: 'staging'"
        )
        .replace("params.FLUID_ENV == null ? 'dev'", "params.FLUID_ENV == null ? 'staging'")
    )
    assert staging != default


#: Values a stage's ``--env`` would refuse, and whether a programmatic
#: PipelineConfig refuses them too. It refuses everything the CLI does except
#: blank, which it reads as unset (dev).
_BAD_ENV_DEFAULTS = [
    ("", False),  # blank: the CLI refuses it, PipelineConfig reads it as unset
    ("   ", False),
    ("../prod", True),
    ("overlays/prod", True),
    ("-prod", True),
    (".prod", True),
    ("prod aws", True),
    ("prod;id", True),
    ("prod*", True),
    ("a$(id)", True),
    ("pr\nod", True),
    ("1prod", True),  # stage 3's fluid generate artifacts --env refuses a leading digit
    ("_prod", True),  # stage 10's fluid publish --env refuses a leading underscore
    ("p" * 65, True),  # and more than 64 characters
]


@pytest.mark.parametrize(("bad", "config_refuses"), _BAD_ENV_DEFAULTS)
def test_a_fluid_env_default_a_stage_would_refuse_is_refused(
    repo, monkeypatch, bad, config_refuses
):
    from fluid_build.cli._common import CLIError

    product = repo / "contracts" / "customer_subscriptions"
    with pytest.raises(CLIError) as exc:
        _generate(product, "contract.fluid.yaml", monkeypatch, fluid_env_default=bad)
    assert exc.value.event == "generate_ci_fluid_env_default_invalid"
    assert not (product / "Jenkinsfile").exists()
    if config_refuses:
        with pytest.raises(ValueError, match="not an environment name"):
            _jenkinsfile(fluid_env_default=bad)


def _publish_accepts(env: str) -> bool:
    """Whether stage 10's ``fluid publish`` parser takes ``--env=<env>``."""
    from fluid_build.cli import publish

    parser = argparse.ArgumentParser(prog="fluid")
    publish.register(parser.add_subparsers())
    try:
        parser.parse_args([publish.COMMAND, "contract.fluid.yaml", f"--env={env}"])
    except SystemExit:
        return False
    return True


def _generate_artifacts_accepts(env: str) -> bool:
    """Whether stage 3's ``fluid generate artifacts --env`` takes ``env``: the
    rule its scheduled DAGs hold ``fluid apply --env`` to."""
    from fluid_build.schedulers.airflow.fluid_apply import ScheduleRenderError, validate_env_name

    try:
        validate_env_name(env)
    except ScheduleRenderError:
        return False
    return True


@pytest.mark.parametrize(
    "env",
    ["dev", "aws", "Prod-2.eu_west", "p" * 64, "p" * 65, "_prod", "1prod", "9", "-prod"]
    + [".prod", "prod aws", "pr\nod", "../prod", "prod/x", "\u00e9t\u00e9"],
)
def test_a_config_takes_a_fluid_env_default_exactly_when_every_stage_does(env):
    """Measured: ``PipelineConfig(fluid_env_default='_prod')`` built directly,
    not through ``fluid generate ci``, wrote a Jenkinsfile whose build on the
    defaults ran stages 1 to 9 (``fluid apply`` included) in ``_prod`` and then
    exited 2 at stage 10, whose ``fluid publish --env`` refuses the name. The
    config, not only the CLI, holds the default to both stages' validators."""
    every_stage = _publish_accepts(env) and _generate_artifacts_accepts(env)
    try:
        content = _jenkinsfile(fluid_env_default=env)
    except ValueError as exc:
        assert not every_stage, exc
        assert "not an environment name" in str(exc)
    else:
        assert every_stage, f"{env!r} generated, but a stage's --env refuses it"
        assert _declared(content)["FLUID_ENV"] == env


def test_the_option_is_on_the_command_line(repo, monkeypatch):
    from fluid_build.cli import main as fluid_main

    product = repo / "contracts" / "customer_subscriptions"
    monkeypatch.chdir(product)
    argv = ["generate", "ci", "contract.fluid.yaml", "--system", "jenkins", "--out", "Jenkinsfile"]
    assert fluid_main([*argv, "--fluid-env-default", "aws"]) == 0
    assert _declared((product / "Jenkinsfile").read_text(encoding="utf-8"))["FLUID_ENV"] == "aws"
    (product / "Jenkinsfile").unlink()
    assert fluid_main([*argv, "--fluid-env-default", "../aws"]) != 0
    assert not (product / "Jenkinsfile").exists()


# ── (6) the archive steps read what the stages write ─────────────────────


_SHIP_OWN_FILES = {"runtime/ship-report.json", "runtime/ship-junit.xml"}


@pytest.mark.parametrize("number", range(1, 12))
def test_every_archived_runtime_report_is_one_its_stage_writes(number, tmp_path):
    """Each stage archives only runtime/ files ship's own argv for that stage writes."""
    content = _jenkinsfile()
    label = next(
        l for l in re.findall(r"stage\('(\d+ - [^']+)'\)", content) if l.startswith(f"{number} ")
    )
    opts = st.resolve_options(
        _parse_ship(["--yes", "--publish", "--schedule-sync"]),
        "contract.fluid.yaml",
        {"SCHEDULER": "airflow", "PLAN_HTML": "true"},
    )
    (tmp_path / "dist" / "artifacts" / "policy").mkdir(parents=True)
    (tmp_path / "dist" / "artifacts" / "policy" / "bindings.json").write_text("{}")
    (tmp_path / "dist" / "artifacts" / "schedule").mkdir()
    (tmp_path / "dist" / "artifacts" / "schedule" / "dag.py").write_text("")
    argv = st.build_stage(number, opts, st.Workspace(tmp_path)).argv
    assert argv, f"stage {number} did not build an argv"
    written = {tok for tok in argv if tok.startswith("runtime/")}
    for pattern in re.findall(r"archiveArtifacts artifacts: '([^']+)'", _stage(content, label)):
        for path in pattern.split(","):
            if path.startswith("runtime/") and path != "runtime/bundle.tgz":
                assert path in written, f"stage {label} archives {path} but ship writes {written}"
    # The run's own report is archived after every build, and is ship's.
    post = content[content.index("    post {") :]
    for path in _SHIP_OWN_FILES:
        assert path in post


def _parse_ship(argv):
    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers(dest="command"))
    return parser.parse_args(["ship", *argv])


def test_stage_10_publishes_to_the_registered_target_with_the_catalog_credentials_named():
    content = _jenkinsfile(default_publish_target="fluid-command-center")
    assert _environment_fallbacks(content)["PUBLISH_TARGETS"] == "fluid-command-center"
    assert _declared(content)["PUBLISH_TARGETS"] == "fluid-command-center"
    assert 'fluid ship "$CONTRACT" --stage 10' in _stage(content, "10 - publish")
    # The catalog credential is named for the operator, never bound.
    head = content[: content.index("pipeline {")]
    banner_and_env = content[content.index("    environment {") :]
    for var in ("FLUID_CC_ENDPOINT", "FLUID_API_KEY", "FLUID_CC_ORG_ID"):
        assert var in head or var in banner_and_env, var


# ── (5) the last applied plan reaches stage 5 across builds ──────────────


def test_last_applied_is_copied_from_the_last_successful_build_when_asked_for():
    content = _jenkinsfile(diff_last_applied=True)
    head = content[: content.index("pipeline {")]
    assert "workflow-aggregator, git, lockable-resources, junit, copyartifact" in head
    assert 'copyArtifactPermission("/${env.JOB_NAME}")' in content
    stage0 = _stage(content, "0 — Bootstrap FLUID [pypi]")
    # Emptied first: nothing committed at that path can pose as a baseline.
    assert stage0.index('rm -rf "$WORKSPACE/.fluid-ci"') < stage0.index("copyArtifacts(")
    assert "selector: lastSuccessful()" in stage0 and "optional: true" in stage0
    stage5 = _stage(content, "5 - diff (drift gate)")
    assert 'BASELINE="$WORKSPACE/.fluid-ci/last-applied/$FLUID_ENV.json"' in stage5
    # Ship reads the baseline from DIFF_LAST_APPLIED, and says so when there is none.
    assert 'export DIFF_LAST_APPLIED="$BASELINE"' in stage5
    stage7 = _stage(content, "7 - apply")
    assert 'if [ "$APPLY_MODE" != "dry-run" ]; then' in stage7
    assert 'cp runtime/plan.json "$WORKSPACE/.fluid-ci/applied/$FLUID_ENV.json"' in stage7
    post = content[content.index("    post {") :]
    success = post[post.index("success {") : post.index("failure {")]
    assert "archiveArtifacts artifacts: '.fluid-ci/applied/*.json'" in success


def test_without_last_applied_the_file_needs_no_copyartifact_plugin():
    content = _jenkinsfile()
    assert "copyArtifact" not in content and "copyartifact" not in content
    assert "--last-applied" not in content and "DIFF_LAST_APPLIED=" not in content


def test_generate_ci_output_lists_the_plugins(repo, monkeypatch, capsys):
    product = repo / "contracts" / "customer_subscriptions"
    _generate(product, "contract.fluid.yaml", monkeypatch, diff_last_applied=True)
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "Jenkins plugins required: workflow-aggregator, git, lockable-resources, junit, "
        "copyartifact" in out
    )


def test_a_relative_or_unsafe_scheduler_destination_default_is_refused(repo, monkeypatch):
    from fluid_build.cli._common import CLIError

    product = repo / "contracts" / "customer_subscriptions"
    for bad in ("dags", "file:///dags;id", "scp://-oProxyCommand=x/dags"):
        with pytest.raises(CLIError):
            _generate(
                product,
                "contract.fluid.yaml",
                monkeypatch,
                scheduler_default="airflow",
                scheduler_destination_default=bad,
            )


def test_the_generated_file_has_no_groovy_escape_in_a_shell_body():
    """A backslash inside ``sh '''...'''`` is a Groovy escape, not the shell's."""
    for body in _sh_bodies(_jenkinsfile(diff_last_applied=True, scheduler_default="airflow")):
        if "forge-cli-src" in body:
            continue
        assert "\\" not in body


@pytest.mark.parametrize("workdir", ['a"b', "a$(id)", "a*", "a,b", "a\\b"])
def test_a_workdir_the_file_cannot_carry_literally_is_refused(workdir):
    with pytest.raises(ValueError, match="workdir"):
        _jenkinsfile(workdir=workdir)


def test_every_shell_body_is_posix_sh():
    """Jenkins runs ``sh`` with /bin/sh (dash on Debian): no bash arrays or [[ ]]."""
    for body in _sh_bodies(_jenkinsfile(diff_last_applied=True)):
        assert "[[" not in body and "declare -a" not in body
