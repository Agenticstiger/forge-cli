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

import pathlib

DAG_TEMPLATE = """from airflow import DAG
from airflow.operators.bash import BashOperator
from datetime import datetime
with DAG(dag_id='{dag_id}', schedule='{cron}', start_date=datetime(2024,1,1), catchup=False, tags=['fluid']) as dag:
    run = BashOperator(
        task_id='run_product',
        bash_command='{bash_cmd}'
    )
"""


def scaffold_dag(contract: dict, out_dir: str, project: str, region: str):
    # Support both 0.5.7 (builds array) and 0.4.0 (build object)
    from fluid_build.util.contract import get_primary_build

    build = get_primary_build(contract) or {}
    cron = build.get("execution", {}).get("trigger", {}).get("cron", "0 2 * * *")
    dag_id = contract.get("id", "fluid_product").replace(".", "_")
    bash_cmd = f"python -m fluid_build.cli apply {contract.get('id','contract.fluid.yaml')} --provider gcp --project {project} --region {region}"
    code = DAG_TEMPLATE.format(dag_id=dag_id, cron=cron, bash_cmd=bash_cmd)
    p = pathlib.Path(out_dir) / f"{dag_id}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code, encoding="utf-8")
    return str(p)
