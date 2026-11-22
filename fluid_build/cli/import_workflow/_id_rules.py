# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What ``airflowOrchestration.dagId`` and ``airflowTask.taskId`` take, in the schema a proposal
declares.

Airflow takes ASCII letters of either case, digits, ``_``, ``.`` and ``-`` in a DAG id and a task
id (250 characters at most). The preview schemas take the same; the released ones up to 0.7.6 take
lower case only. The importer asks the schema of the ``fluidVersion`` it writes, through the
validator every command uses, so an id is recorded exactly as Airflow spells it when that schema
takes it, and under the importer's lower case identifier of it when the schema does not. The day a
schema that takes Airflow's ids is the one proposals declare, the importer records exact ids
without a change here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict

#: What every schema up to 0.7.6 holds for both ids (lower case only), used when the schema of a
#: version cannot be read: the rule every released schema accepts.
_LOWER_CASE_ONLY: Dict[str, Any] = {
    "type": "string",
    "pattern": "^[a-z0-9_][a-z0-9_.-]*[a-z0-9_]$|^[a-z0-9_]$",
}

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


@dataclass(frozen=True)
class IdRules:
    """Whether an id is one the schema takes, for DAG ids and for task ids."""

    dag: Callable[[str], bool]
    task: Callable[[str], bool]
    #: Whether the rule keeps to lower case (said in the report for an id recorded under another).
    lower_case_only: bool

    @property
    def spelling(self) -> str:
        """The characters an id may hold, as the report says it."""
        if self.lower_case_only:
            return "lower case letters, digits, '_', '.' and '-'"
        return "letters, digits, '_', '.' and '-' (at most 250 of them)"


@lru_cache(maxsize=8)
def id_rules(version: str) -> IdRules:
    """The id rules of the bundled schema for ``version``; the lower case rule when that schema is
    not bundled or does not say."""
    from fluid_build.schema_manager import schema_validator

    dag_schema, task_schema = _declared(version)
    dag = schema_validator(dag_schema)
    task = schema_validator(task_schema)

    def takes(validator: Any) -> Callable[[str], bool]:
        return lambda value: bool(validator.is_valid(value))

    return IdRules(
        dag=takes(dag),
        task=takes(task),
        lower_case_only=not (dag.is_valid("A") and task.is_valid("A")),
    )


def _declared(version: str) -> "tuple[Dict[str, Any], Dict[str, Any]]":
    """The sub-schemas of the two ids in the bundled schema of ``version``."""
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", version):
        return _LOWER_CASE_ONLY, _LOWER_CASE_ONLY
    try:
        defs = json.loads((_SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))[
            "$defs"
        ]
        dag = defs["airflowOrchestration"]["properties"]["dagId"]
        task = defs["airflowTask"]["properties"]["taskId"]
    except (OSError, ValueError, KeyError, TypeError):
        return _LOWER_CASE_ONLY, _LOWER_CASE_ONLY
    if not isinstance(dag, dict) or not isinstance(task, dict):
        return _LOWER_CASE_ONLY, _LOWER_CASE_ONLY
    return dag, task
