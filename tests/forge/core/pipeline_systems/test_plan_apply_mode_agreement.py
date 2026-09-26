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

"""Generated stages 1, 6 and 7 run the documented chain, and agree on the mode.

Stage 1 bundles the contract with ``--env``; stage 6 plans that bundle for
APPLY_MODE; stage 7 applies stage 6's plan with ``--bundle`` in the same
mode. ``fluid plan`` stamps the mode into plan.json and ``fluid apply``
refuses a plan made for another one (``apply_plan_mode_mismatch``), before
any build, and refuses ``--build-id`` with a mode that runs no build
(``apply_build_id_requires_build_mode``). So the mode is APPLY_MODE as
given, never rewritten to fit a build id, and the build id is passed only
with a build mode.

Each case runs the RENDERED stage bodies with ``sh``, with the CI system's
parameters in the environment the way it exports them (Jenkins exports each
build parameter as a variable of the same name) and a ``fluid`` stub on
PATH that records its argv, then replays each recorded argv through the real
``fluid`` entry point (``fluid_build.cli.main``) in a workspace holding a
small local contract. The parameterless case exports nothing: a Jenkins
job's first build, and its first after a restart re-seeded it, runs so, and
every stage must then fall back to the defaults the parameters declare.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

from fluid_build.cli import main as fluid_main
from fluid_build.forge.core.pipeline_templates import (
    PipelineComplexity,
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


def _config(provider: PipelineProvider, **kwargs: object) -> PipelineConfig:
    return PipelineConfig(
        provider=provider,
        complexity=PipelineComplexity.STANDARD,
        contract_path=_C,
        apply_build_id_default="make_rows",
        **kwargs,
    )


def _tekton_stages(**kwargs: object) -> Dict[int, str]:
    """Stage 1/6/7 ``script:`` bodies from the rendered Tekton tasks (``_stage_specs``)."""
    files = PipelineTemplateGenerator().generate_pipeline(
        _config(PipelineProvider.TEKTON, **kwargs)
    )
    stages: Dict[int, str] = {}
    for doc in yaml.safe_load_all(files["tekton/tasks.yaml"]):
        for step in ((doc or {}).get("spec") or {}).get("steps") or []:
            match = re.fullmatch(r"stage-(\d+)", step.get("name") or "")
            if match and int(match.group(1)) in (1, 6, 7):
                stages[int(match.group(1))] = step["script"]
    assert set(stages) == {1, 6, 7}
    return stages


def _jenkins_stages(**kwargs: object) -> Dict[int, str]:
    """Stage 1/6/7 ``sh '''...'''`` bodies from the rendered Jenkinsfile."""
    content = PipelineTemplateGenerator().generate_pipeline(
        _config(PipelineProvider.JENKINS, **kwargs)
    )["Jenkinsfile"]
    stages: Dict[int, str] = {}
    for num, label in ((1, "1 - bundle"), (6, "6 - plan"), (7, "7 - apply")):
        body = content[content.index(f"stage('{label}')") :]
        # No stage re-assigns parameters in an ``environment {}`` block: the
        # shell reads the variables Jenkins exports, with their defaults.
        assert "environment {" not in body[: body.index("steps {")]
        sh_start = body.index("sh '''") + len("sh '''")
        stages[num] = body[sh_start : body.index("'''", sh_start)]
    return stages


def _exported(params: Dict[str, object]) -> Dict[str, str]:
    """Parameters as a CI system exports them to the shell."""
    return {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()}


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("FLUID_ENV", "APPLY_MODE", "APPLY_BUILD_ID", "CONTRACT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contracts" / "p").mkdir(parents=True)
    (tmp_path / _C).write_text(_CONTRACT, encoding="utf-8")
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / "fluid"
    stub.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -c \'import json, os, sys; '
        'open(os.environ["FLUID_ARGV_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\' "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _run_stage(ws: Path, script: str, env: Dict[str, str]) -> List[str]:
    """Run one rendered stage body with ``sh``; return the argv it gave ``fluid``."""
    log = ws / "fluid-argv.jsonl"
    log.unlink(missing_ok=True)
    shell_env = {
        "PATH": f"{ws / 'stub-bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", str(ws)),
        "WORKSPACE": str(ws),
        "FLUID_ARGV_LOG": str(log),
        **env,
    }
    subprocess.run(["sh", "-c", script], cwd=ws, env=shell_env, check=True)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1, calls
    return calls[0]


def _flag(argv: List[str], name: str) -> Optional[str]:
    values = [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == name]
    assert len(values) <= 1, argv  # one --mode, one --build-id: nothing overridden
    return values[0] if values else None


def _run_chain(ws: Path, stages: Dict[int, str], env: Dict[str, str]) -> List[str]:
    """Stages 1, 6, 7 through the stub, each replayed for real; stage 7's argv."""
    bundle_argv = _run_stage(ws, stages[1], env)
    assert bundle_argv[:2] == ["bundle", _C]
    assert _flag(bundle_argv, "--env") == env.get("FLUID_ENV", "dev")
    assert fluid_main(bundle_argv) == 0

    plan_argv = _run_stage(ws, stages[6], env)
    assert plan_argv[:2] == ["plan", "runtime/bundle.tgz"]
    assert fluid_main(plan_argv) == 0

    apply_argv = _run_stage(ws, stages[7], env)
    assert apply_argv[:2] == ["apply", "runtime/plan.json"]
    assert _flag(apply_argv, "--bundle") == "runtime/bundle.tgz"
    planned_for = json.loads((ws / "runtime" / "plan.json").read_text(encoding="utf-8"))
    # The plan was made from the bundle (bound), for the mode apply runs
    # (``None`` and ``amend`` are the same additive default).
    assert planned_for.get("bundleDigest")
    assert (planned_for["mode"] or "amend") == _flag(apply_argv, "--mode")
    assert fluid_main(apply_argv) == 0
    return apply_argv


@pytest.mark.parametrize("build_id", ["", "make_rows"], ids=["no-build-id", "build-id"])
@pytest.mark.parametrize("apply_mode", _MODES)
@pytest.mark.parametrize("system", ["tekton", "jenkins"])
def test_generated_plan_and_apply_agree_on_the_mode(
    ws: Path, system: str, apply_mode: str, build_id: str
) -> None:
    stages = _tekton_stages() if system == "tekton" else _jenkins_stages()
    params: Dict[str, object] = {
        "APPLY_MODE": apply_mode,
        "APPLY_BUILD_ID": build_id,
        "ALLOW_DATA_LOSS": True,  # replace* in a fresh dev workspace
        "NO_VERIFY_DIGEST": False,
        "PLAN_HTML": False,
    }
    apply_argv = _run_chain(ws, stages, _exported(params))

    # APPLY_MODE as given: a build id never turns a mode into a build mode.
    assert _flag(apply_argv, "--mode") == apply_mode
    landed = ws / "contracts" / "p" / "out" / "rows.parquet"
    if apply_mode in _BUILD_MODES:
        assert duckdb.sql(f"SELECT count(*) FROM '{landed}'").fetchone() == (1,)
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
    ws: Path, system: str, apply_mode_default: Optional[str], expected_mode: str
) -> None:
    """No parameter exported at all: the contract, the env, the mode and the
    build id all come from the defaults the parameters declare."""
    render = _tekton_stages if system == "tekton" else _jenkins_stages
    stages = render(apply_mode_default=apply_mode_default)
    apply_argv = _run_chain(ws, stages, {})
    assert _flag(apply_argv, "--mode") == expected_mode
    assert _flag(apply_argv, "--env") == "dev"
    expected_build_id = "make_rows" if expected_mode in _BUILD_MODES else None
    assert _flag(apply_argv, "--build-id") == expected_build_id
    landed = ws / "contracts" / "p" / "out" / "rows.parquet"
    if expected_mode in _BUILD_MODES:
        assert duckdb.sql(f"SELECT count(*) FROM '{landed}'").fetchone() == (1,)
    if expected_mode == "dry-run":
        assert not landed.exists()  # a dry run writes nothing


def test_a_blank_build_id_parameter_runs_every_build(ws: Path) -> None:
    """APPLY_BUILD_ID deliberately set to blank is kept blank (``${X-default}``),
    not replaced by its default: ``--mode amend-and-build`` with no filter."""
    stages = _jenkins_stages(apply_mode_default="amend-and-build")
    apply_argv = _run_chain(ws, stages, {"APPLY_BUILD_ID": ""})
    assert _flag(apply_argv, "--mode") == "amend-and-build"
    assert _flag(apply_argv, "--build-id") is None
