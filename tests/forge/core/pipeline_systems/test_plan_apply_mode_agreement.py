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

"""Generated stage 6 plans for the mode generated stage 7 applies.

``fluid plan`` stamps the mode into plan.json and ``fluid apply`` refuses a
plan made for another one (``apply_plan_mode_mismatch``), before any build.
The generators did not agree with themselves: the shared stage 6
(``_stage_specs``, what Tekton, GitHub Actions, GitLab, Azure DevOps,
Bitbucket and CircleCI render) planned with no ``--mode``, and both stage 7s
appended a second ``--mode amend-and-build`` whenever APPLY_BUILD_ID was set.
So every build run, and every non-amend APPLY_MODE, failed at stage 7.

Each case runs the RENDERED stage-6 and stage-7 shell bodies with ``sh``,
with the CI system's parameters in the environment and a ``fluid`` stub on
PATH that records its argv, then replays each recorded argv through the real
``fluid`` entry point (``fluid_build.cli.main``) in a workspace holding a
small local contract. Jenkins' parameters are routed through the stage's own
``environment {}`` block, as Jenkins does.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

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


def _tekton_stages() -> Dict[int, Tuple[Dict[str, str], str]]:
    """Stage 6/7 ``script:`` bodies from the rendered Tekton tasks (``_stage_specs``).

    The shared stage specs read ``APPLY_MODE`` / ``APPLY_BUILD_ID`` from the
    environment directly (GitHub Actions maps them at job level), so the
    parameter-to-environment mapping is the identity.
    """
    cfg = PipelineConfig(provider=PipelineProvider.TEKTON, complexity=PipelineComplexity.STANDARD)
    files = PipelineTemplateGenerator().generate_pipeline(cfg)
    identity = {
        name: name
        for name in ("APPLY_MODE", "APPLY_BUILD_ID", "ALLOW_DATA_LOSS", "NO_VERIFY_DIGEST")
    }
    stages: Dict[int, Tuple[Dict[str, str], str]] = {}
    for doc in yaml.safe_load_all(files["tekton/tasks.yaml"]):
        for step in ((doc or {}).get("spec") or {}).get("steps") or []:
            if step.get("name") in ("stage-6", "stage-7"):
                stages[int(step["name"][-1])] = (identity, step["script"])
    assert set(stages) == {6, 7}
    return stages


_JENKINS_PARAM = re.compile(r'^\s*(\w+)\s*=\s*"\$\{params\.(\w+)\}"\s*$', re.M)
_JENKINS_TERNARY = re.compile(
    r"^\s*(\w+)\s*=\s*\"\$\{params\.(\w+) \? '([^']*)' : '([^']*)'\}\"\s*$", re.M
)


def _jenkins_stages() -> Dict[int, Tuple[Dict[str, str], str]]:
    """Stage 6/7 ``sh '''...'''`` bodies from the rendered Jenkinsfile, with each
    stage's ``environment {}`` mapping (env var -> Jenkins parameter)."""
    cfg = PipelineConfig(provider=PipelineProvider.JENKINS, complexity=PipelineComplexity.BASIC)
    content = PipelineTemplateGenerator().generate_pipeline(cfg)["Jenkinsfile"]
    stages: Dict[int, Tuple[Dict[str, str], str]] = {}
    for num, label in ((6, "6 - plan"), (7, "7 - apply")):
        body = content[content.index(f"stage('{label}')") :]
        env_block = body[body.index("environment {\n") : body.index("steps {")]
        mapping = {var: param for var, param in _JENKINS_PARAM.findall(env_block)}
        for var, param, if_true, if_false in _JENKINS_TERNARY.findall(env_block):
            mapping[var] = f"{param}?{if_true}:{if_false}"
        sh_start = body.index("sh '''") + len("sh '''")
        stages[num] = (mapping, body[sh_start : body.index("'''", sh_start)])
    return stages


def _stage_env(mapping: Dict[str, str], params: Dict[str, object]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for var, param in mapping.items():
        if "?" in param:  # ``params.X ? 'a' : 'b'``
            name, choices = param.split("?", 1)
            if_true, if_false = choices.split(":", 1)
            env[var] = if_true if params.get(name) else if_false
        else:
            value = params.get(param, "")
            env[var] = str(value).lower() if isinstance(value, bool) else str(value)
    return env


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("FLUID_ENV", "APPLY_MODE", "APPLY_BUILD_ID", "APPLY_BUILD_ID_VAL", "CONTRACT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contracts" / "p").mkdir(parents=True)
    (tmp_path / _C).write_text(_CONTRACT, encoding="utf-8")
    (tmp_path / "runtime").mkdir()
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
    log = ws / "runtime" / "fluid-argv.jsonl"
    log.unlink(missing_ok=True)
    shell_env = {
        "PATH": f"{ws / 'stub-bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", str(ws)),
        "FLUID_ARGV_LOG": str(log),
        "CONTRACT": _C,
        **env,
    }
    subprocess.run(["sh", "-c", script], cwd=ws, env=shell_env, check=True)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1, calls
    return calls[0]


def _last_mode(argv: List[str]) -> str:
    modes = [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == "--mode"]
    assert modes, argv
    return modes[-1]  # argparse: the last occurrence wins


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

    plan_mapping, plan_script = stages[6]
    plan_argv = _run_stage(ws, plan_script, _stage_env(plan_mapping, params))
    assert plan_argv[0] == "plan"
    assert fluid_main(plan_argv) == 0
    planned_for = json.loads((ws / "runtime" / "plan.json").read_text(encoding="utf-8"))["mode"]

    apply_mapping, apply_script = stages[7]
    apply_argv = _run_stage(ws, apply_script, _stage_env(apply_mapping, params))
    assert apply_argv[0] == "apply"
    applied_as = _last_mode(apply_argv)

    # The plan was made for the mode apply runs (``None`` and ``amend`` are
    # the same additive default), and apply accepts it.
    assert (planned_for or "amend") == applied_as, (plan_argv, apply_argv)
    assert fluid_main(apply_argv) == 0

    landed = ws / "contracts" / "p" / "out" / "rows.parquet"
    if applied_as in _BUILD_MODES:
        assert duckdb.sql(f"SELECT count(*) FROM '{landed}'").fetchone() == (1,)
    if build_id:
        # A build id only means something with a build mode.
        assert applied_as in _BUILD_MODES
        assert apply_argv[apply_argv.index("--build-id") + 1] == build_id
