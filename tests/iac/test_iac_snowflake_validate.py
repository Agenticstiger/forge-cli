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

"""Integration: emitted Snowflake ``.tf.json`` is accepted by real ``tofu``.

``tofu validate`` checks config syntax and provider-schema correctness
against the real ``snowflakedb/snowflake`` v2 provider. It needs ``tofu``
on PATH and registry network access (``tofu init`` downloads the provider)
— but **no Snowflake credentials**. Skipped when ``tofu`` is absent.

This is the contract-shape gate: every representative contract — the
in-repo ``examples/snowflake/*`` ones and a synthetic contract that
exercises all 11 emitted resource types — must compile to a module the
provider's own schema accepts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli._common import resolve_env_templates_in_contract
from fluid_build.iac import build_module, runner

from .conftest import snowflake_plugin

pytestmark = [pytest.mark.integration, pytest.mark.provider, pytest.mark.snowflake]

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "snowflake"
_EXAMPLE_CONTRACTS = sorted(_EXAMPLES_DIR.glob("*/contract.fluid.yaml"))

# Dummy values for the ``{{ env.* }}`` placeholders the example contracts
# carry — `tofu validate` never connects, so any valid identifier works.
_DUMMY_ENV = {
    "SNOWFLAKE_ACCOUNT": "VALIDATE-ACCT",
    "SNOWFLAKE_DATABASE": "VALIDATE_DB",
    "SNOWFLAKE_SCHEMA": "VALIDATE_SC",
    "SNOWFLAKE_WAREHOUSE": "VALIDATE_WH",
    "SNOWFLAKE_ROLE": "VALIDATE_ROLE",
}

# A synthetic contract that drives every data-plane + governance resource
# the plugin emits: database, schema, table, view, and two grant scopes.
_FULL_CONTRACT: Dict[str, Any] = {
    "id": "silver.full.surface",
    "name": "Full Surface",
    "exposes": [
        {
            "exposeId": "events",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {"database": "FULL_DB", "schema": "FULL_SC", "table": "EVENTS"},
            },
            "contract": {
                "schema": [
                    {"name": "ID", "type": "integer", "required": True},
                    {"name": "LABEL", "type": "string"},
                    {"name": "AMOUNT", "type": "decimal(12,2)"},
                    {"name": "TS", "type": "timestamp"},
                ]
            },
        },
        {
            "exposeId": "events_v",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_view",
                "location": {
                    "database": "FULL_DB",
                    "schema": "FULL_SC",
                    "view": "EVENTS_V",
                    "query": "SELECT * FROM EVENTS",
                },
            },
        },
    ],
    "security": {
        "access_control": {
            "grants": [
                {
                    "role": "ANALYST",
                    "privilege": "SELECT",
                    "object_type": "TABLE",
                    "object_name": "FULL_DB.FULL_SC.EVENTS",
                },
                {
                    "role": "LOADER",
                    "privilege": "USAGE",
                    "object_type": "DATABASE",
                    "object_name": "FULL_DB",
                },
            ]
        },
        "policies": {
            "masking": [
                {
                    "name": "MASK_LABEL",
                    "body": "CASE WHEN CURRENT_ROLE() = 'ANALYST' THEN val ELSE '***' END",
                    "signature": "(val VARCHAR) RETURNS VARCHAR",
                }
            ],
            "row_access": [
                {
                    "name": "TENANT_ISOLATION",
                    "condition": "TRUE",
                    "signature": "(tenant VARCHAR) RETURNS BOOLEAN",
                }
            ],
        },
    },
}

# Orchestration actions — streams / tasks / views / procedures / functions.
_FULL_ACTIONS = [
    {
        "op": "sf.stream.ensure",
        "database": "FULL_DB",
        "schema": "FULL_SC",
        "name": "EVENTS_STREAM",
        "source_table": "EVENTS",
        "append_only": True,
    },
    {
        "op": "sf.task.ensure",
        "database": "FULL_DB",
        "schema": "FULL_SC",
        "name": "ROLLUP",
        "sql": "INSERT INTO FULL_DB.FULL_SC.AGG SELECT COUNT(*) FROM FULL_DB.FULL_SC.EVENTS",
        "schedule": "USING CRON 0 2 * * * UTC",
        "warehouse": "COMPUTE_WH",
        "after": [],
    },
    {"op": "sf.task.resume", "name": "ROLLUP"},
    {
        "op": "sf.view.ensure",
        "database": "FULL_DB",
        "schema": "FULL_SC",
        "name": "EVENTS_RECENT",
        "query": "SELECT * FROM FULL_DB.FULL_SC.EVENTS",
    },
    {
        "op": "sf.procedure.ensure",
        "database": "FULL_DB",
        "schema": "FULL_SC",
        "name": "REFRESH_AGG",
        "language": "SQL",
        "parameters": [],
        "body": "BEGIN RETURN 'ok'; END;",
    },
    {
        "op": "sf.udf.ensure",
        "database": "FULL_DB",
        "schema": "FULL_SC",
        "name": "DOUBLE_FN",
        "language": "SQL",
        "return_type": "NUMBER",
        "parameters": [{"name": "n", "type": "NUMBER"}],
        "body": "n * 2",
    },
]


def _init_and_validate(workdir: Path, env: Dict[str, str]) -> None:
    """``tofu init -backend=false`` then ``tofu validate`` — assert both pass."""
    init = runner.tofu_init(str(workdir), backend=False, env=env)
    assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
    result = runner.tofu_validate(str(workdir), env=env)
    assert result.ok, f"tofu validate failed:\n{result.stderr or result.stdout}"


@pytest.mark.skipif(not _EXAMPLE_CONTRACTS, reason="no examples/snowflake/* contracts found")
@pytest.mark.parametrize("contract_path", _EXAMPLE_CONTRACTS, ids=lambda p: p.parent.name)
def test_example_contract_emits_validatable_module(
    contract_path, tofu_binary, tofu_env, tmp_path, monkeypatch
):
    """Each in-repo ``examples/snowflake/*`` contract compiles to a module
    the real provider schema accepts."""
    for var, value in _DUMMY_ENV.items():
        monkeypatch.setenv(var, value)
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract = resolve_env_templates_in_contract(contract)

    (tmp_path / "main.tf.json").write_text(
        build_module(snowflake_plugin(), contract), encoding="utf-8"
    )
    _init_and_validate(tmp_path, tofu_env)


def test_full_surface_contract_validates(tofu_binary, tofu_env, tmp_path):
    """A contract exercising all 11 emitted resource types validates clean."""
    text = build_module(snowflake_plugin(), _FULL_CONTRACT, actions=_FULL_ACTIONS)
    (tmp_path / "main.tf.json").write_text(text, encoding="utf-8")
    _init_and_validate(tmp_path, tofu_env)

    # The module really does carry every resource type — guards against a
    # silent emit regression hiding behind a green `tofu validate`.
    import json

    resources = json.loads(text)["resource"]
    for resource_type in (
        "snowflake_database",
        "snowflake_schema",
        "snowflake_table",
        "snowflake_view",
        "snowflake_grant_privileges_to_account_role",
        "snowflake_stream_on_table",
        "snowflake_task",
        "snowflake_procedure_sql",
        "snowflake_function_sql",
        "snowflake_masking_policy",
        "snowflake_row_access_policy",
    ):
        assert resource_type in resources, f"{resource_type} missing from the module"


def test_contract_without_snowflake_exposures_validates_as_empty(tofu_binary, tofu_env, tmp_path):
    """A contract with no Snowflake exposures emits an empty — still valid —
    module: ``tofu validate`` accepts a module with no resources."""
    contract = {"id": "no.snowflake", "exposes": []}
    (tmp_path / "main.tf.json").write_text(
        build_module(snowflake_plugin(), contract), encoding="utf-8"
    )
    _init_and_validate(tmp_path, tofu_env)
