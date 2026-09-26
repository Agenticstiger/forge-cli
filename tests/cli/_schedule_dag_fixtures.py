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

"""Shared helpers for the scheduled-build DAG tests.

``load_dag`` imports a generated DAG file against stub ``airflow`` modules
(Airflow is not a test dependency) and returns what the file handed to
``DAG(...)`` and ``BashOperator(...)``. ``run_task`` then runs that
BashOperator's command the way Airflow does (``bash -c`` with ``env`` laid
over the worker's environment, ``append_env=True``) against a stub ``fluid``
that records its argv, working directory and environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

#: The demo product's contract, trimmed to what scheduling reads: one build
#: with a cron trigger, a Postgres source read through ``{{ env.X }}``
#: placeholders, and no ``orchestration`` block.
DEMO_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.customer_subscriptions
name: Customer Subscriptions
domain: Customer
metadata:
  layer: Bronze
  owner:
    team: data-platform
builds:
  - id: ingest_subscriptions
    pattern: acquisition
    engine: duckdb
    properties:
      source:
        kind: postgres
        connection:
          host: "{{ env.PGHOST }}"
          port: "{{ env.PGPORT }}"
          database: "{{ env.PGDATABASE }}"
          user: "{{ env.PGUSER }}"
          password: "{{ env.PGPASSWORD }}"
        mode: full_refresh
        streams:
          - public.product_subscription
    execution:
      trigger:
        type: schedule
        schedule: "0 */4 * * *"
    outputs:
      - subscriptions
exposes:
  - exposeId: subscriptions
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/customer_subscriptions.parquet
    contract:
      schema:
        - name: subscription_id
          type: VARCHAR
          required: true
"""

#: Where the demo keeps its contract, relative to the project root.
DEMO_CONTRACT_PATH = "contracts/customer_subscriptions/contract.fluid.yaml"

#: The demo's AWS overlay (patches only the expose binding).
DEMO_AWS_OVERLAY = """\
exposes:
  - binding:
      platform: aws
      format: parquet
      location:
        database: demo_bronze
        table: customer_subscriptions
        bucket: northwind-demo-lake
        path: bronze/customer_subscriptions/
        region: eu-north-1
"""


def write_project(root: Path, contract: str = DEMO_CONTRACT, *, overlay: bool = True) -> Path:
    """Lay out a product checkout under *root*; return the contract file."""
    path = root / DEMO_CONTRACT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contract, encoding="utf-8")
    if overlay:
        (path.parent / "overlays").mkdir(exist_ok=True)
        (path.parent / "overlays" / "aws.yaml").write_text(DEMO_AWS_OVERLAY, encoding="utf-8")
    return path


@dataclass
class LoadedDag:
    dag: Dict[str, Any] = field(default_factory=dict)
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    namespace: Dict[str, Any] = field(default_factory=dict)


def load_dag(source: str, monkeypatch: pytest.MonkeyPatch, *, airflow3: bool = True) -> LoadedDag:
    """Import a generated DAG against stub Airflow 3 (or 2) modules."""
    loaded = LoadedDag()

    class _DAG:
        def __init__(self, **kwargs: Any) -> None:
            loaded.dag = kwargs

        def __enter__(self) -> "_DAG":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _BashOperator:
        def __init__(self, **kwargs: Any) -> None:
            loaded.tasks.append(kwargs)

    def module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        return mod

    stubs: Dict[str, Optional[types.ModuleType]] = {
        "pendulum": module(
            "pendulum", datetime=lambda *a, **kw: ("pendulum.datetime", a, kw.get("tz"))
        ),
        # Airflow 2 names; an Airflow 3 install keeps these importable too.
        "airflow": module("airflow", DAG=_DAG),
        "airflow.operators": module("airflow.operators"),
        "airflow.operators.bash": module("airflow.operators.bash", BashOperator=_BashOperator),
    }
    if airflow3:
        stubs.update(
            {
                "airflow.sdk": module("airflow.sdk", DAG=_DAG),
                "airflow.providers": module("airflow.providers"),
                "airflow.providers.standard": module("airflow.providers.standard"),
                "airflow.providers.standard.operators": module(
                    "airflow.providers.standard.operators"
                ),
                "airflow.providers.standard.operators.bash": module(
                    "airflow.providers.standard.operators.bash", BashOperator=_BashOperator
                ),
            }
        )
    else:
        # ``None`` in sys.modules makes the import raise ImportError.
        stubs.update({"airflow.sdk": None, "airflow.providers": None})
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    namespace: Dict[str, Any] = {"__name__": "generated_dag"}
    exec(compile(source, "generated_dag.py", "exec"), namespace)  # noqa: S102 - test harness
    loaded.namespace = namespace
    return loaded


@dataclass
class TaskRun:
    returncode: int
    stderr: str
    argv: Optional[List[str]]
    cwd: Optional[str]
    env: Dict[str, str]


FLUID_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$FLUID_RECORD/argv"
pwd > "$FLUID_RECORD/cwd"
env > "$FLUID_RECORD/env"
"""


def python_stub(body: str) -> str:
    """A ``fluid`` stub that runs *body* with this interpreter (so with
    fluid_build importable) after recording argv, cwd and environment the
    way :data:`FLUID_STUB` does. *body* sees ``RECORD``, a ``Path``."""
    return (
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "RECORD = Path(os.environ['FLUID_RECORD'])\n"
        "(RECORD / 'argv').write_text('\\n'.join(sys.argv[1:]) + '\\n')\n"
        "(RECORD / 'cwd').write_text(os.getcwd() + '\\n')\n"
        "(RECORD / 'env').write_text(''.join(f'{k}={v}\\n' for k, v in os.environ.items()))\n"
        + body
    )


def run_task(
    task: Dict[str, Any],
    tmp_path: Path,
    worker_env: Dict[str, str],
    *,
    stub: str = FLUID_STUB,
) -> TaskRun:
    """Run a BashOperator task's command the way Airflow does."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not installed")
    record = tmp_path / "record"
    record.mkdir(exist_ok=True)
    bin_dir = tmp_path / "worker-bin"
    bin_dir.mkdir(exist_ok=True)
    stub_file = bin_dir / "fluid"
    stub_file.write_text(stub, encoding="utf-8")
    stub_file.chmod(0o755)

    env = {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "HOME": str(tmp_path),
        "FLUID_RECORD": str(record),
        **worker_env,
    }
    assert task["append_env"] is True
    env.update(task["env"])  # append_env=True: task env laid over the worker's
    proc = subprocess.run(  # noqa: S603 - the generated command under test
        [bash, "-c", task["bash_command"]],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    argv_file = record / "argv"
    env_seen: Dict[str, str] = {}
    if (record / "env").exists():
        for line in (record / "env").read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep:
                env_seen[name] = value
    return TaskRun(
        returncode=proc.returncode,
        stderr=proc.stderr,
        argv=argv_file.read_text().splitlines() if argv_file.exists() else None,
        cwd=(record / "cwd").read_text().strip() if (record / "cwd").exists() else None,
        env=env_seen,
    )
