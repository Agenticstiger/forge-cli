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
import re

from ..providers.common.codegen_utils import py_str_literal, sanitize_identifier
from ._common import CLIError, load_contract_with_overlay
from ._logging import info

COMMAND = "scaffold-composer"

# A cron expression is the 5 (or 6, with seconds/year) standard fields.
# We allow only the cron grammar's character set — digits, the wildcards
# ``* / , -`` and field-separating whitespace. This rejects anything that
# could be smuggled through the ``cron`` contract field into the generated
# Python source (the value lands inside a string literal which Airflow
# imports at DAG-parse time). Belt-and-suspenders with ``py_str_literal``
# below — a malformed cron is rejected outright rather than silently
# emitted as an inert-but-wrong schedule.
_CRON_FIELD_CHARS = re.compile(r"^[0-9*/,\-]+$")


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
    schedule={cron},
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


def _validate_cron(raw: object) -> str:
    """Validate an untrusted ``cron`` contract value against the cron grammar.

    Allowlist: 5 or 6 whitespace-separated fields, each drawn only from
    ``[0-9*/,\\-]``. Anything else (extra fields, shell metacharacters,
    newlines, quotes) is rejected. This is the *primary* guard — the
    value also passes through :func:`py_str_literal` before it reaches the
    generated source, so even a future grammar gap can't break out of the
    string literal and inject a top-level statement Airflow would execute.
    """
    cron = str(raw if raw is not None else "0 2 * * *").strip()
    fields = cron.split()
    if len(fields) not in (5, 6) or not all(_CRON_FIELD_CHARS.fullmatch(f) for f in fields):
        raise ValueError(
            f"invalid cron expression {cron!r}: expected 5 or 6 fields drawn from [0-9*/,-]"
        )
    return cron


def _extract_cron(c: dict) -> object:
    """Pull the trigger cron from a contract, tolerating both the
    normalized ``builds: [...]`` list shape (what
    ``load_contract_with_overlay`` emits) and the legacy singular
    ``build: {...}`` mapping. Returns the default daily cron when absent."""
    builds = c.get("builds")
    if isinstance(builds, list):
        for b in builds:
            if isinstance(b, dict):
                cron = ((b.get("execution") or {}).get("trigger") or {}).get("cron")
                if cron is not None:
                    return cron
    legacy = c.get("build")
    if isinstance(legacy, dict):
        cron = ((legacy.get("execution") or {}).get("trigger") or {}).get("cron")
        if cron is not None:
            return cron
    return "0 2 * * *"


def run(args, logger: logging.Logger) -> int:
    try:
        c = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        cron = _validate_cron(_extract_cron(c))
        # SECURITY (path traversal + filename injection): the contract's
        # id/name is attacker-influenced and was previously only ``.``->``_``
        # cleaned, which leaves ``/`` (path traversal) and an absolute name
        # (``os.path.join`` discards ``out_dir`` on an absolute right-hand
        # side) in play — either writes the DAG OUTSIDE ``out_dir``.
        # ``sanitize_identifier`` collapses every non-``[A-Za-z0-9_]`` char
        # to ``_`` and strips leading digits, yielding a single safe path
        # component that doubles as a valid Python ``dag_id``.
        dag_id = sanitize_identifier(c.get("id") or c.get("name") or "fluid_product")
        provider = "gcp"
        out_dir = os.path.abspath(args.out_dir)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{dag_id}.py")
        # Defence in depth: confirm the resolved write target stays inside
        # ``out_dir`` even though ``dag_id`` is already a safe component.
        if os.path.commonpath([out_dir, os.path.abspath(path)]) != out_dir:
            raise ValueError(f"refusing to write DAG outside out_dir: {path!r}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                DAG_TMPL.format(
                    dag_id=dag_id,
                    cron=py_str_literal(cron),
                    contract=args.contract,
                    provider=provider,
                )
            )
        info(logger, "composer_dag_written", path=path)
        return 0
    except Exception as e:
        raise CLIError(1, "scaffold_composer_failed", {"error": str(e)})
