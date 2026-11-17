# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A generated wrapper hands ``fluid ship`` an environment under which plan and
apply agree on the mode.

Stage 1 bundles the contract with ``--env``; stage 6 plans that bundle for
APPLY_MODE; stage 7 applies stage 6's plan with ``--bundle`` in the same
mode. ``fluid plan`` stamps the mode into plan.json and ``fluid apply``
refuses a plan made for another one (``apply_plan_mode_mismatch``), before
any build, and refuses ``--build-id`` with a mode that runs no build
(``apply_build_id_requires_build_mode``). So the mode is APPLY_MODE as
given, never rewritten to fit a build id, and the build id is passed only
with a build mode.

The stage logic is ship's (``cli/_ship_stages.py``), so each case reads the
environment the RENDERED wrapper gives ship (the Tekton Pipeline's parameter
defaults, the Jenkinsfile's ``environment {}`` fallbacks), applies the
parameters an operator set on top, and runs the real ``fluid ship`` over
stages 1, 6 and 7 in a workspace holding a small local contract. The
parameterless case sets nothing: a Jenkins job's first build, and its first
after a restart re-seeded it, runs so, and every stage must then fall back to
the defaults the parameters declare.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

from fluid_build.cli import main as fluid_main
from fluid_build.forge.core.pipeline_templates import (
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

duckdb = pytest.importorskip("duckdb")

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.modes
name: Modes Demo
description: Inline SQL build.
domain: Demo
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
builds:
  - id: make_rows
    description: Inline rows.
    pattern: embedded-logic
    engine: sql
    properties:
      sql: "SELECT 1 AS id, 'a' AS name"
    outputs: [rows]
exposes:
  - exposeId: rows
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/rows.parquet
    contract:
      schema:
        - {name: id, type: INTEGER, required: true}
        - {name: name, type: VARCHAR, required: false}
"""

_C = "contracts/p/contract.fluid.yaml"
_MODES = ["dry-run", "create-only", "amend", "amend-and-build", "replace", "replace-and-build"]
_BUILD_MODES = {"amend-and-build", "replace-and-build"}
#: Stages 2-5 are switched off, as the chain this pins is bundle, plan, apply.
_ONLY_1_6_7 = {
    "RUN_STAGE_2_VALIDATE": "false",
    "RUN_STAGE_3_GENERATE_ARTIFACTS": "false",
    "RUN_STAGE_4_VALIDATE_ARTIFACTS": "false",
    "RUN_STAGE_5_DIFF": "false",
}
_ENV_VARS = ("FLUID_ENV", "APPLY_MODE", "APPLY_BUILD_ID", "CONTRACT", "FLUID_RUN_ID")


def _config(provider: PipelineProvider, **kwargs: object) -> PipelineConfig:
    return PipelineConfig(
        provider=provider,
        contract_path=_C,
        apply_build_id_default="make_rows",
        **kwargs,
    )


def _tekton_env(**kwargs: object) -> Dict[str, str]:
    """The environment of a fluid-apply run of the rendered Tekton Pipeline: each
    Pipeline parameter's default, and the contract the Task script names."""
    files = PipelineTemplateGenerator().generate_pipeline(
        _config(PipelineProvider.TEKTON, **kwargs)
    )
    documents = list(yaml.safe_load_all(files["tekton/pipeline.yaml"]))
    apply_pipeline = next(d for d in documents if d["metadata"]["name"] == "fluid-apply")
    env = {
        p["name"]: p["default"]
        for p in apply_pipeline["spec"]["params"]
        if "default" in p and p["name"] != "FLUID_RUN_ID"
    }
    tasks = list(yaml.safe_load_all(files["tekton/tasks.yaml"]))
    script = next(t for t in tasks if t["metadata"]["name"] == "fluid-ship")["spec"]["steps"][0][
        "script"
    ]
    assert f"fluid ship {_C} --stage" in script
    return env


def _jenkins_env(**kwargs: object) -> Dict[str, str]:
    """What the rendered Jenkinsfile's ``environment {}`` gives ship when no
    parameter is set: every ``params.X == null ? 'default'`` fallback."""
    content = PipelineTemplateGenerator().generate_pipeline(
        _config(PipelineProvider.JENKINS, **kwargs)
    )["Jenkinsfile"]
    found = re.findall(r'^\s+(\w+) = "\$\{params\.\1 == null \? \'([^\']*)\' : ', content, re.M)
    env = dict(found)
    assert env["CONTRACT"] == _C
    return env


def _exported(params: Dict[str, object]) -> Dict[str, str]:
    """Parameters as a CI system exports them to the environment."""
    return {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()}


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contracts" / "p").mkdir(parents=True)
    (tmp_path / _C).write_text(_CONTRACT, encoding="utf-8")
    return tmp_path


def _flag(argv: List[str], name: str) -> Optional[str]:
    values = [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == name]
    assert len(values) <= 1, argv  # one --mode, one --build-id: nothing overridden
    return values[0] if values else None


def _run_chain(
    ws: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapper_env: Dict[str, str],
    params: Dict[str, str],
    default_env: str = "dev",
) -> List[str]:
    """``fluid ship`` stages 1, 6, 7 under the wrapper's environment with
    ``params`` set on top; the argv ship gave stage 7."""
    env = {**wrapper_env, **_ONLY_1_6_7, **params}
    contract = env.pop("CONTRACT", _C)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert fluid_main(["ship", contract, "--stage", "1-7", "--yes", "--ci-output", "plain"]) == 0

    report = json.loads((ws / "runtime" / "ship-report.json").read_text(encoding="utf-8"))
    argvs = {stage["number"]: stage["argv"] for stage in report["stages"]}
    assert argvs[1][:2] == ["bundle", _C]
    assert _flag(argvs[1], "--env") == env.get("FLUID_ENV", default_env)
    assert argvs[6][:2] == ["plan", "runtime/bundle.tgz"]
    assert argvs[7][:2] == ["apply", "runtime/plan.json"]
    assert _flag(argvs[7], "--bundle") == "runtime/bundle.tgz"
    planned_for = json.loads((ws / "runtime" / "plan.json").read_text(encoding="utf-8"))
    # The plan was made from the bundle (bound), for the mode apply runs
    # (``None`` and ``amend`` are the same additive default).
    assert planned_for.get("bundleDigest")
    assert (planned_for["mode"] or "amend") == _flag(argvs[7], "--mode")
    return argvs[7]


@pytest.mark.parametrize(
    ("system", "build_id"),
    # Each real ship run costs seconds: Tekton carries the build id, Jenkins both.
    [("tekton", "make_rows"), ("jenkins", ""), ("jenkins", "make_rows")],
    ids=["tekton-build-id", "jenkins-no-build-id", "jenkins-build-id"],
)
@pytest.mark.parametrize("apply_mode", _MODES)
def test_generated_plan_and_apply_agree_on_the_mode(
    ws: Path, monkeypatch: pytest.MonkeyPatch, system: str, apply_mode: str, build_id: str
) -> None:
    wrapper_env = _tekton_env() if system == "tekton" else _jenkins_env()
    params: Dict[str, object] = {
        "APPLY_MODE": apply_mode,
        "APPLY_BUILD_ID": build_id,
        "ALLOW_DATA_LOSS": True,  # replace* in a fresh dev workspace
        "NO_VERIFY_DIGEST": False,
        "PLAN_HTML": False,
    }
    apply_argv = _run_chain(ws, monkeypatch, wrapper_env, _exported(params))

    # APPLY_MODE as given: a build id never turns a mode into a build mode.
    assert _flag(apply_argv, "--mode") == apply_mode
    landed = ws / "contracts" / "p" / "out" / "rows.parquet"
    if apply_mode in _BUILD_MODES:
        assert duckdb.sql(f"SELECT count(*) FROM '{landed}'").fetchall() == [(1,)]
    # A build id reaches ``fluid apply`` only with a build mode.
    expected_build_id = build_id if (build_id and apply_mode in _BUILD_MODES) else None
    assert _flag(apply_argv, "--build-id") == expected_build_id


@pytest.mark.parametrize(
    ("system", "apply_mode_default", "expected_mode"),
    [
        ("jenkins", None, "dry-run"),
        ("jenkins", "amend-and-build", "amend-and-build"),
        ("tekton", None, "amend"),
        ("tekton", "amend-and-build", "amend-and-build"),
    ],
)
def test_a_parameterless_run_uses_the_declared_defaults(
    ws: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    apply_mode_default: Optional[str],
    expected_mode: str,
) -> None:
    """No parameter set at all: the contract, the env, the mode and the
    build id all come from the defaults the parameters declare."""
    render = _tekton_env if system == "tekton" else _jenkins_env
    wrapper_env = render(apply_mode_default=apply_mode_default)
    apply_argv = _run_chain(ws, monkeypatch, wrapper_env, {})
    assert _flag(apply_argv, "--mode") == expected_mode
    assert _flag(apply_argv, "--env") == "dev"
    expected_build_id = "make_rows" if expected_mode in _BUILD_MODES else None
    assert _flag(apply_argv, "--build-id") == expected_build_id
    landed = ws / "contracts" / "p" / "out" / "rows.parquet"
    if expected_mode in _BUILD_MODES:
        assert duckdb.sql(f"SELECT count(*) FROM '{landed}'").fetchall() == [(1,)]
    if expected_mode == "dry-run":
        assert not landed.exists()  # a dry run writes nothing


def test_a_blank_build_id_parameter_runs_every_build(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APPLY_BUILD_ID deliberately set to blank stays blank, it is not replaced by
    its default: ``--mode amend-and-build`` with no filter."""
    wrapper_env = _jenkins_env(apply_mode_default="amend-and-build")
    apply_argv = _run_chain(ws, monkeypatch, wrapper_env, {"APPLY_BUILD_ID": ""})
    assert _flag(apply_argv, "--mode") == "amend-and-build"
    assert _flag(apply_argv, "--build-id") is None


_STAGING_OVERLAY = """\
exposes:
  - binding:
      location:
        path: ./out/staging/rows.parquet
"""


@pytest.mark.parametrize("system", ["tekton", "jenkins"])
@pytest.mark.parametrize(
    "exported",
    [{}, {"FLUID_ENV": "staging"}],
    ids=["parameterless", "declared-defaults"],
)
def test_a_run_on_defaults_applies_the_fluid_env_default(
    ws: Path, monkeypatch: pytest.MonkeyPatch, system: str, exported: Dict[str, str]
) -> None:
    """``fluid generate ci --fluid-env-default staging``: a build that gets no
    parameters (a Jenkins job's first build, its first after a restart) and one
    that gets the declared defaults (what an upstream trigger starts) both
    bundle, plan and apply the staging overlay, not the base contract."""
    (ws / "contracts" / "p" / "overlays").mkdir()
    (ws / "contracts" / "p" / "overlays" / "staging.yaml").write_text(
        _STAGING_OVERLAY, encoding="utf-8"
    )
    render = _tekton_env if system == "tekton" else _jenkins_env
    wrapper_env = render(apply_mode_default="amend-and-build", fluid_env_default="staging")
    apply_argv = _run_chain(ws, monkeypatch, wrapper_env, exported, default_env="staging")
    assert _flag(apply_argv, "--env") == "staging"
    out = ws / "contracts" / "p" / "out"
    assert duckdb.sql(f"SELECT count(*) FROM '{out / 'staging' / 'rows.parquet'}'").fetchall() == [
        (1,)
    ]
    assert not (out / "rows.parquet").exists()  # the base binding was not applied
