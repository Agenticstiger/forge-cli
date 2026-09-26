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
from fluid_build.cli.generate_ci import run as generate_ci_run
from fluid_build.forge.core.pipeline_templates import (
    PipelineComplexity,
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
    config = PipelineConfig(
        provider=PipelineProvider.JENKINS, complexity=PipelineComplexity.STANDARD, **kwargs
    )
    return PipelineTemplateGenerator().generate_pipeline(config)["Jenkinsfile"]


def _stage(content: str, label: str) -> str:
    start = content.index(f"stage('{label}')")
    nxt = content.find("\n        stage('", start + 10)
    return content[start : nxt if nxt != -1 else content.index("    post {")]


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
    args = argparse.Namespace(
        system="jenkins", complexity="standard", out="Jenkinsfile", contract=contract, **options
    )
    assert generate_ci_run(args, _LOG) == 0
    return (cwd / "Jenkinsfile").read_text(encoding="utf-8")


# ── (1) the contract given, from wherever it is generated ─────────────────


def test_generated_from_the_repository_root_names_the_contract_from_there(repo, monkeypatch):
    content = _generate(repo, "contracts/customer_subscriptions/contract.fluid.yaml", monkeypatch)
    assert (
        "name: 'CONTRACT', defaultValue: 'contracts/customer_subscriptions/contract.fluid.yaml'"
        in content
    )
    assert 'cd "' not in content  # runs at the checkout root
    assert 'fluid bundle "${CONTRACT:-contracts/customer_subscriptions/contract.fluid.yaml}"' in (
        content
    )


def test_generated_from_the_contract_directory_cds_there(repo, monkeypatch):
    product = repo / "contracts" / "customer_subscriptions"
    content = _generate(product, "contract.fluid.yaml", monkeypatch)
    assert "name: 'CONTRACT', defaultValue: 'contract.fluid.yaml'" in content
    assert 'cd "contracts/customer_subscriptions" && set -eu' in content
    # The scheduled DAG applies the contract relative to the checkout.
    assert (
        '--contract-path "contracts/customer_subscriptions/${CONTRACT:-contract.fluid.yaml}"'
        in content
    )


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
    assert "//   workflow-aggregator, git\n" in content


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
        },
    ],
    ids=["defaults", "demo-options"],
)
def test_every_parameter_read_falls_back_to_the_default_it_declares(options):
    content = _jenkinsfile(**options)
    declared = _declared(content)
    assert len(declared) >= 30
    shells = "\n".join(_sh_bodies(content))
    for name, default in declared.items():
        for op, fallback in re.findall(r"\$\{" + name + r"(:-|-)([^}]*)\}", shells):
            assert fallback == default, (name, op, fallback, default)
        # Never read without a fallback: a parameterless build exports none.
        assert not re.search(r"\$\{" + name + r"\}", shells), name
        assert not re.search(r"\$" + name + r"\b", shells), name
    # Every stage toggle's when{} falls back to the declared default too.
    for name, default in re.findall(r"params\.(RUN_STAGE_\w+) == null \? (true|false)", content):
        assert declared[name] == default, name
    # GENERATE_EMIT has one.
    assert '--emit "${GENERATE_EMIT:-odcs,odps-bitol,schedule,policies}"' in content


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


def test_stages_2_3_5_6_9_read_the_bundle_stage_1_made_for_the_env():
    content = _jenkinsfile()
    stage1 = _stage(content, "1 - bundle")
    assert '--env "${FLUID_ENV:-dev}" --format tgz --out runtime/bundle.tgz' in stage1
    for label, command in (
        ("2 - validate", "set -- runtime/bundle.tgz --env"),
        ("3 - generate artifacts", "fluid generate artifacts runtime/bundle.tgz --env"),
        ("5 - diff (drift gate)", "set -- runtime/bundle.tgz --env"),
        ("6 - plan", "set -- runtime/bundle.tgz --env"),
        ("9 - verify", "set -- runtime/bundle.tgz --env"),
    ):
        assert command in _stage(content, label), label
    stage7 = _stage(content, "7 - apply")
    assert "set -- runtime/plan.json --bundle runtime/bundle.tgz --mode" in stage7


# ── (6) the archive steps read what the stages write ─────────────────────


def test_every_archived_runtime_report_is_one_its_stage_writes():
    content = _jenkinsfile()
    labels = re.findall(r"stage\('(\d+ - [^']+)'\)", content)
    assert len(labels) == 11
    for label in labels:
        stage = _stage(content, label)
        shells = "\n".join(_sh_bodies(stage))
        for pattern in re.findall(r"archiveArtifacts artifacts: '([^']+)'", stage):
            for path in pattern.split(","):
                if not path.startswith("runtime/") or path == "runtime/bundle.tgz":
                    continue
                written = re.search(
                    r"(--out|--report|--html|>)\s+" + re.escape(path) + r"(?![\w./-])", shells
                )
                assert written, f"stage {label} archives {path} but writes no such file"


def test_stage_10_publishes_json_to_the_registered_target_with_the_env():
    content = _jenkinsfile(default_publish_target="fluid-command-center")
    stage10 = _stage(content, "10 - publish")
    assert (
        'set -- "${CONTRACT:-contract.fluid.yaml}" --env "${FLUID_ENV:-dev}" --format json'
        in stage10
    )
    assert 'for t in ${PUBLISH_TARGETS:-fluid-command-center}; do set -- "$@" "--target=$t"' in (
        stage10
    )
    assert "command-center" not in stage10.replace("fluid-command-center", "")
    for var in ("FLUID_CC_ENDPOINT", "FLUID_API_KEY", "FLUID_CC_ORG_ID"):
        assert var in content[: content.index("pipeline {")], var


# ── (5) the last applied plan reaches stage 5 across builds ──────────────


def test_last_applied_is_copied_from_the_last_successful_build_when_asked_for():
    content = _jenkinsfile(diff_last_applied=True)
    head = content[: content.index("pipeline {")]
    assert "workflow-aggregator, git, copyartifact" in head
    assert 'copyArtifactPermission("/${env.JOB_NAME}")' in content
    stage0 = _stage(content, "0 — Bootstrap FLUID [pypi]")
    # Emptied first: nothing committed at that path can pose as a baseline.
    assert stage0.index('rm -rf "$WORKSPACE/.fluid-ci"') < stage0.index("copyArtifacts(")
    assert "selector: lastSuccessful()" in stage0 and "optional: true" in stage0
    stage5 = _stage(content, "5 - diff (drift gate)")
    assert 'BASELINE="$WORKSPACE/.fluid-ci/last-applied/${FLUID_ENV:-dev}.json"' in stage5
    assert 'set -- "$@" --last-applied "$BASELINE"' in stage5
    stage7 = _stage(content, "7 - apply")
    assert 'if [ "$MODE" != "dry-run" ]; then' in stage7
    assert 'cp runtime/plan.json "$WORKSPACE/.fluid-ci/applied/${FLUID_ENV:-dev}.json"' in stage7
    post = content[content.index("    post {") :]
    success = post[post.index("success {") : post.index("failure {")]
    assert "archiveArtifacts artifacts: '.fluid-ci/applied/*.json'" in success


def test_without_last_applied_the_file_needs_no_copyartifact_plugin():
    content = _jenkinsfile()
    assert "copyArtifact" not in content and "copyartifact" not in content
    assert "--last-applied" not in content


def test_generate_ci_output_lists_the_plugins(repo, monkeypatch, capsys):
    product = repo / "contracts" / "customer_subscriptions"
    _generate(product, "contract.fluid.yaml", monkeypatch, diff_last_applied=True)
    out = " ".join(capsys.readouterr().out.split())
    assert "Jenkins plugins required: workflow-aggregator, git, copyartifact" in out


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


# ── stages 8 and 9 after a dry run ────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "fluid_called"), [(None, False), ("dry-run", False), ("amend", True)]
)
def test_stage_9_verifies_only_an_apply_that_changed_something(tmp_path, mode, fluid_called):
    stage9 = _stage(_jenkinsfile(), "9 - verify")
    (body,) = _sh_bodies(stage9)
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "called"
    fluid = stub / "fluid"
    fluid.write_text(f"#!/bin/sh\necho \"$@\" > '{log}'\n", encoding="utf-8")
    fluid.chmod(fluid.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "bundle.tgz").write_bytes(b"")
    env = {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}
    if mode:
        env["APPLY_MODE"] = mode
    subprocess.run(["sh", "-c", body], cwd=tmp_path, env=env, check=True)
    assert log.exists() is fluid_called
    if fluid_called:
        assert log.read_text().split()[:2] == ["verify", "runtime/bundle.tgz"]


def test_stage_8_checks_rather_than_enforces_after_a_dry_run(tmp_path):
    (body,) = _sh_bodies(_stage(_jenkinsfile(), "8 - policy apply"))
    stub = tmp_path / "bin"
    stub.mkdir()
    fluid = stub / "fluid"
    fluid.write_text('#!/bin/sh\necho "ARGV $*"\n', encoding="utf-8")
    fluid.chmod(fluid.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "dist" / "artifacts" / "policy").mkdir(parents=True)
    (tmp_path / "dist" / "artifacts" / "policy" / "bindings.json").write_text("{}")
    for mode, expected in ((None, "check"), ("amend", "enforce")):
        env = {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}
        if mode:
            env["APPLY_MODE"] = mode
        out = subprocess.run(
            ["sh", "-c", body], cwd=tmp_path, env=env, check=True, capture_output=True, text=True
        ).stdout
        assert f"ARGV policy-apply dist/artifacts/policy/bindings.json --mode {expected}" in out


def test_the_generated_file_has_no_groovy_escape_in_a_shell_body():
    """A backslash inside ``sh '''...'''`` is a Groovy escape, not the shell's."""
    for body in _sh_bodies(_jenkinsfile(diff_last_applied=True, scheduler_default="airflow")):
        if "forge-cli-src" in body:
            continue
        assert "\\" not in body


def test_every_shell_body_is_posix_sh():
    """Jenkins runs ``sh`` with /bin/sh (dash on Debian): no bash arrays or [[ ]]."""
    for body in _sh_bodies(_jenkinsfile(diff_last_applied=True)):
        assert "[[" not in body and "declare -a" not in body
