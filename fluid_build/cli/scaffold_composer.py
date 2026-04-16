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

from __future__ import annotations

import argparse
import logging
import os

from ._common import CLIError, load_contract_with_overlay
from ._logging import info

COMMAND = "scaffold-composer"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Generate Cloud Composer DAG from contract")
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    p.add_argument("--out-dir", default="runtime/composer/dags", help="DAGs directory")
    p.set_defaults(cmd=COMMAND, func=run)


DAG_TMPL = """from __future__ import annotations
from airflow import DAG
from airflow.operators.bash import BashOperator
from datetime import datetime
with DAG(
    dag_id="{dag_id}",
    start_date=datetime(2024,1,1),
    schedule="{cron}",
    catchup=False,
    default_args={{"retries": 1}},
    tags=["FLUID"]
) as dag:
    validate = BashOperator(
        task_id="validate",
        bash_command="python -m fluid_build.cli validate {contract}"
    )
    plan = BashOperator(
        task_id="plan",
        bash_command="python -m fluid_build.cli --provider {provider} plan {contract} --out /tmp/plan.json"
    )
    apply = BashOperator(
        task_id="apply",
        bash_command="python -m fluid_build.cli --provider {provider} apply /tmp/plan.json --yes"
    )
    validate >> plan >> apply
"""


def run(args, logger: logging.Logger) -> int:
    try:
        c = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        cron = (c.get("build", {}).get("execution", {}).get("trigger", {}) or {}).get(
            "cron", "0 2 * * *"
        )
        dag_id = (c.get("id") or c.get("name") or "fluid_product").replace(".", "_")
        provider = "gcp"
        os.makedirs(args.out_dir, exist_ok=True)
        path = os.path.join(args.out_dir, f"{dag_id}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                DAG_TMPL.format(dag_id=dag_id, cron=cron, contract=args.contract, provider=provider)
            )
        info(logger, "composer_dag_written", path=path)
        return 0
    except Exception as e:
        raise CLIError(1, "scaffold_composer_failed", {"error": str(e)})
