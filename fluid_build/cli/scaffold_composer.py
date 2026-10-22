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
import shlex
from typing import Any, Dict, List

from .._console import cprint
from ..providers.common.codegen_utils import (
    airflow_dag_import,
    airflow_standard_import,
    py_str_literal,
    sanitize_identifier,
)
from ..util.contract import has_only_unscheduled_builds, is_unscheduled_build
from ._acquisition_stage_ext import _validate_cron as _validate_cron_grammar
from ._common import CLIError, load_contract_with_overlay
from ._logging import info

COMMAND = "scaffold-composer"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Generate Cloud Composer DAG from contract")
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    p.add_argument("--out-dir", default="runtime/composer/dags", help="DAGs directory")
    p.set_defaults(cmd=COMMAND, func=run)


# Airflow 2.7+ and 3.x: see codegen_utils.airflow_dag_import. The import blocks hold
# no braces, so the template stays safe for str.format.
DAG_TMPL = (
    "from __future__ import annotations\n"
    + airflow_dag_import()
    + airflow_standard_import("BashOperator")
    + """from datetime import datetime
with DAG(
    dag_id={dag_id},
    start_date=datetime(2024,1,1),
    schedule={cron},
    catchup=False,
    default_args={{"retries": 1}},
    tags=["FLUID"]
) as dag:
    validate = BashOperator(
        task_id="validate",
        bash_command={validate_cmd}
    )
    plan = BashOperator(
        task_id="plan",
        bash_command={plan_cmd}
    )
    apply = BashOperator(
        task_id="apply",
        bash_command={apply_cmd}
    )
    validate >> plan >> apply
"""
)


def _validate_cron(raw: object) -> str:
    """Validate an untrusted ``cron`` contract value against the cron grammar.

    Delegates the grammar check to the canonical
    :func:`fluid_build.cli._acquisition_stage_ext._validate_cron` so the
    scaffold and the schedule-sync stage share ONE cron allowlist. That
    shared allowlist correctly accepts the Quartz tokens ``? L W #`` (the
    old scaffold-local ``[0-9*/,-]`` regex wrongly rejected them) while
    still rejecting shell metacharacters, newlines and quotes. The value
    also passes through :func:`py_str_literal` before it reaches the
    generated source, so even a future grammar gap can't break out of the
    string literal and inject a top-level statement Airflow would execute.

    Two thin adaptations preserve this caller's historical contract:

    * a ``None``/missing value defaults to the daily ``0 2 * * *`` cron
      (the shared validator requires a ``str``), and a non-string value is
      coerced with ``str()`` before validation;
    * the shared validator raises :class:`IdentifierViolation`, a
      ``ValueError`` subclass, so existing ``except Exception`` handling in
      :func:`run` and ``pytest.raises(ValueError)`` callers both still fire.
    """
    cron = str(raw if raw is not None else "0 2 * * *")
    return _validate_cron_grammar(cron)


# A generated DAG's ``bash_command`` is executed by a shell at run time.
# The contract path comes in as ``args.contract`` (operator-supplied on the
# CLI) and is interpolated into that shell string, so a metacharacter
# (``;`` ``|`` ``&`` ``$`` ``` ` ``` ``(`` ``)`` newline …) or a leading
# ``-`` (option-injection into ``python -m fluid_build.cli``) must never
# reach the shell as an executable token. We ``shlex.quote`` the value
# (run-time shell safety) AND ``py_str_literal`` the whole command
# (DAG-parse-time Python-literal safety); this allowlist is a third wall
# that rejects the obviously-hostile shapes outright so the generated DAG
# fails loud rather than emitting an inert-but-surprising command.
_SAFE_CONTRACT_PATH = re.compile(r"^[A-Za-z0-9_./@:\-]+$")


def _validate_contract_path(raw: str) -> str:
    """Validate the operator-supplied contract path before it is woven into
    a generated shell command.

    ``shlex.quote`` already neutralises shell metacharacters, but a path
    that begins with ``-`` would be parsed by ``python -m fluid_build.cli``
    as an *option* even when shell-quoted (the argument-injection pattern),
    and an embedded newline/NUL has no business in a contract path. Reject
    those shapes here so the generated DAG never carries a surprising token.
    """
    path = str(raw)
    if not path or path != path.strip():
        raise ValueError(f"invalid contract path {path!r}: empty or has surrounding whitespace")
    if "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError(f"invalid contract path {path!r}: contains a NUL/newline")
    if path.startswith("-"):
        raise ValueError(
            f"invalid contract path {path!r}: must not start with '-' (option-injection guard)"
        )
    if not _SAFE_CONTRACT_PATH.fullmatch(path):
        raise ValueError(
            f"invalid contract path {path!r}: only [A-Za-z0-9_./@:-] are allowed "
            "(it is interpolated into a generated shell command)"
        )
    return path


def _build_pipeline_bash_commands(contract_path: str, provider: str) -> tuple[str, str, str]:
    """Return ``(validate_cmd, plan_cmd, apply_cmd)`` as ready-to-embed Python
    string literals for the three generated ``BashOperator`` tasks.

    Two independent layers make these injection-proof regardless of the
    inputs (the front-door ``_validate_contract_path`` is a third wall):

    1. ``shlex.quote`` on every interpolated value → at *run time* the shell
       sees the path/provider as a single inert token, never as a
       metacharacter (``;`` ``$(...)`` `` `…` `` newline …) it would execute.
    2. ``py_str_literal`` (``repr``) on the whole command → at *DAG-parse
       time* the value is a fully-escaped Python literal, so an embedded
       quote/newline cannot break out of ``bash_command=<literal>`` and
       inject a top-level statement Airflow would run.
    """
    q_contract = shlex.quote(contract_path)
    q_provider = shlex.quote(provider)
    validate_cmd = py_str_literal(f"python -m fluid_build.cli validate {q_contract}")
    plan_cmd = py_str_literal(
        f"python -m fluid_build.cli --provider {q_provider} plan {q_contract} --out /tmp/plan.json"
    )
    apply_cmd = py_str_literal(
        f"python -m fluid_build.cli --provider {q_provider} apply /tmp/plan.json --yes"
    )
    return validate_cmd, plan_cmd, apply_cmd


def _builds(c: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The contract's builds: the normalized ``builds: [...]`` list (what
    ``load_contract_with_overlay`` emits), then the legacy singular
    ``build: {...}`` mapping."""
    builds = c.get("builds")
    found = [b for b in builds if isinstance(b, dict)] if isinstance(builds, list) else []
    legacy = c.get("build")
    if isinstance(legacy, dict):
        found.append(legacy)
    return found


def _extract_cron(c: dict) -> object:
    """Pull the trigger cron of the first build a scheduler starts, from the
    normalized ``builds: [...]`` list or the legacy singular ``build: {...}``
    mapping. A build no scheduler starts (a Flink job, or a continuous trigger:
    :func:`~fluid_build.util.contract.is_unscheduled_build`) is skipped, so a
    cron it carries for its own platform does not schedule this DAG. Returns
    the default daily cron when absent."""
    for b in _builds(c):
        if is_unscheduled_build(b):
            continue
        cron = ((b.get("execution") or {}).get("trigger") or {}).get("cron")
        if cron is not None:
            return cron
    return "0 2 * * *"


def _wrote_dag(dag_path: str, dag_id: str) -> bool:
    """True when ``dag_path`` is a DAG this command wrote for ``dag_id``: it
    carries the template's ``tags=["FLUID"]`` and its ``dag_id``, quoted as
    ``py_str_literal`` writes it or as the template did before it."""
    if not os.path.isfile(dag_path):
        return False
    try:
        with open(dag_path, encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
    except OSError:
        return False
    return 'tags=["FLUID"]' in head and (
        f"dag_id={py_str_literal(dag_id)}," in head or f'dag_id="{dag_id}",' in head
    )


def _report_nothing_to_schedule(dag_path: str, dag_id: str) -> None:
    """Say why no DAG is written, as ``fluid generate schedule`` does, and name
    a DAG this command wrote for the product before: left in place, it keeps
    scheduling the product wherever it was deployed. A file of the same name
    that it did not write is someone else's, and is not mentioned."""
    cprint(
        "Every build runs outside forge-cli (a Flink job, for example) or continuously "
        "(trigger.type: continuous), so there is nothing to schedule. No DAG written."
    )
    if _wrote_dag(dag_path, dag_id):
        # Printed as written: soft_wrap leaves wrapping to the terminal, so the
        # path is never broken by an inserted newline and copies whole, and no
        # [markup] or :emoji: code in it is rendered.
        cprint(
            f"A DAG this command wrote for the product before is still at {dag_path}: delete "
            "it, and remove it from Cloud Composer if you deployed it there.",
            markup=False,
            emoji=False,
            soft_wrap=True,
        )


def run(args, logger: logging.Logger) -> int:
    try:
        c = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        # SECURITY (path traversal + filename injection): the contract's
        # id/name is attacker-influenced and was previously only ``.``->``_``
        # cleaned, which leaves ``/`` (path traversal) and an absolute name
        # (``os.path.join`` discards ``out_dir`` on an absolute right-hand
        # side) in play — either writes the DAG OUTSIDE ``out_dir``.
        # ``sanitize_identifier`` collapses every non-``[A-Za-z0-9_]`` char
        # to ``_`` and strips leading digits, yielding a single safe path
        # component that doubles as a valid Python ``dag_id``.
        dag_id = sanitize_identifier(c.get("id") or c.get("name") or "fluid_product")
        # No scheduler starts a Flink job or a continuous build, so a contract
        # with no other build has nothing to schedule and gets no DAG. The
        # test is `fluid generate schedule`'s, hand-declared orchestration
        # tasks included, so the two commands agree on such a contract.
        from fluid_build.schedulers.airflow.fluid_apply import has_explicit_tasks

        if has_only_unscheduled_builds(c) and not has_explicit_tasks(c):
            dag_path = os.path.join(os.path.abspath(args.out_dir), f"{dag_id}.py")
            _report_nothing_to_schedule(dag_path, dag_id)
            return 0
        cron = _validate_cron(_extract_cron(c))
        # SECURITY (shell + Python-literal injection): the contract path is
        # interpolated into a generated ``bash_command`` that a shell runs at
        # task time AND into Python source Airflow imports at DAG-parse time.
        # Validate its shape, ``shlex.quote`` it for the shell layer, and
        # ``py_str_literal`` the whole command for the Python-literal layer —
        # three independent walls. ``provider`` is fixed (``gcp``) today but is
        # ``sanitize_identifier``-cleaned + shell-quoted so a future dynamic
        # value can't inject either.
        contract_path = _validate_contract_path(args.contract)
        provider = sanitize_identifier("gcp")
        validate_cmd, plan_cmd, apply_cmd = _build_pipeline_bash_commands(contract_path, provider)
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
                    dag_id=py_str_literal(dag_id),
                    cron=py_str_literal(cron),
                    validate_cmd=validate_cmd,
                    plan_cmd=plan_cmd,
                    apply_cmd=apply_cmd,
                )
            )
        info(logger, "composer_dag_written", path=path)
        return 0
    except Exception as e:
        raise CLIError(1, "scaffold_composer_failed", {"error": str(e)})
