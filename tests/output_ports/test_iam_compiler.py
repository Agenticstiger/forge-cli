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

"""Tests for the agentPolicy → cloud-IAM compiler.

The compiler closes the bypass-the-gateway gap by emitting native
Snowflake / Postgres row-access policies that honour the same
contract the runtime gateway enforces. We pin:

* Snowflake RAP includes the row-filter predicate AND the
  allowedModels-derived role check.
* Postgres CREATE POLICY honours rowFilters with current_user.
* BigQuery / AWS stubs emit clear TODO markers so operators see the
  gap rather than silently shipping nothing.
* SQL identifier validation prevents injection via column names.
"""

from __future__ import annotations

import pytest

from fluid_build.output_ports.iam_compiler import (
    SUPPORTED_TARGETS,
    compile_agent_policy_to_iam,
)


def _contract(*, expose_id="demo", agent_policy=None, row_filters=None, binding=None):
    expose = {
        "exposeId": expose_id,
        "binding": binding
        or {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "PROD", "schema": "TELCO", "table": "CUSTOMERS"},
        },
        "policy": {
            "agentPolicy": agent_policy or {},
            "rowFilters": row_filters or [],
        },
    }
    return {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "test.iam.compiler",
        "exposes": [expose],
    }


def test_supported_targets_documented():
    assert "snowflake" in SUPPORTED_TARGETS
    assert "postgres" in SUPPORTED_TARGETS
    assert "bigquery" in SUPPORTED_TARGETS
    assert "aws" in SUPPORTED_TARGETS


def test_unknown_target_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        compile_agent_policy_to_iam(contract={}, target="iceberg")


def test_snowflake_emits_rap_for_row_filters_and_allowed_models():
    contract = _contract(
        agent_policy={"allowedModels": ["claude-haiku", "gpt-4o-mini"]},
        row_filters=[
            {"column": "tenant_id", "equals": "${caller.role}"},
            {"column": "region", "equals": "us"},
        ],
    )
    compiled = compile_agent_policy_to_iam(contract=contract, target="snowflake")
    assert len(compiled) == 1
    sql = compiled[0].sql
    # Row-access policy + ALTER TABLE attaching it.
    assert "CREATE OR REPLACE ROW ACCESS POLICY" in sql
    assert 'ALTER TABLE "PROD"."TELCO"."CUSTOMERS"' in sql
    assert "ADD ROW ACCESS POLICY" in sql
    # Caller.role → CURRENT_ROLE() mapping.
    assert "CURRENT_ROLE()" in sql
    # Constant predicate kept literal.
    assert "= 'us'" in sql
    # allowedModels → role-name set.
    assert "FLUID_MODEL_CLAUDE_HAIKU" in sql
    assert "FLUID_MODEL_GPT_4O_MINI" in sql
    # Operator must know about the role-mapping convention.
    assert any("FLUID_MODEL_<MODEL>" in w for w in compiled[0].warnings)


def test_snowflake_skips_expose_without_runtime_fields():
    contract = _contract(agent_policy={"canStore": False, "auditRequired": True})
    compiled = compile_agent_policy_to_iam(contract=contract, target="snowflake")
    assert len(compiled) == 1
    assert "Skipping demo" in compiled[0].sql
    assert "no row-level fields" in compiled[0].warnings[0]


def test_postgres_emits_create_policy_with_current_user():
    contract = _contract(
        binding={
            "platform": "postgres",
            "format": "postgres_table",
            "location": {"database": "appdb", "schema": "telco", "table": "customers"},
        },
        row_filters=[{"column": "tenant", "equals": "${caller.user}"}],
    )
    compiled = compile_agent_policy_to_iam(contract=contract, target="postgres")
    assert len(compiled) == 1
    sql = compiled[0].sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY agent_policy_demo" in sql
    assert "current_user" in sql
    assert '"telco"."customers"' in sql


def test_postgres_warning_when_allowed_models_present():
    contract = _contract(
        binding={
            "platform": "postgres",
            "format": "postgres_table",
            "location": {"database": "appdb", "schema": "telco", "table": "customers"},
        },
        agent_policy={"allowedModels": ["claude-haiku"]},
        row_filters=[{"column": "tenant", "equals": "${caller.user}"}],
    )
    compiled = compile_agent_policy_to_iam(contract=contract, target="postgres")
    assert any("allowedModels has no native Postgres mapping" in w for w in compiled[0].warnings)


def test_bigquery_emits_real_row_access_policy_with_grant_to_clause():
    bq_contract = _contract(
        binding={
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {"project": "my-proj", "dataset": "ds", "table": "t"},
        },
        agent_policy={"allowedModels": ["claude-haiku", "gpt-4o-mini"]},
        row_filters=[
            {"column": "tenant_id", "equals": "${caller.user}"},
            {"column": "region", "equals": "us"},
        ],
    )
    bq = compile_agent_policy_to_iam(contract=bq_contract, target="bigquery")
    assert len(bq) == 1
    sql = bq[0].sql
    assert "CREATE OR REPLACE ROW ACCESS POLICY" in sql
    assert "ON `my-proj.ds.t`" in sql
    assert "FILTER USING" in sql
    # Caller.user maps to BigQuery SESSION_USER().
    assert "SESSION_USER()" in sql
    # Constant predicate kept literal.
    assert "= 'us'" in sql
    # allowedModels mapped to per-LLM service accounts.
    assert "fluid-mcp-claude-haiku@my-proj.iam.gserviceaccount.com" in sql
    assert "fluid-mcp-gpt-4o-mini@my-proj.iam.gserviceaccount.com" in sql
    assert "GRANT TO" in sql
    # Operator must know about the SA-naming convention.
    assert any("fluid-mcp-<MODEL>" in w for w in bq[0].warnings)


def test_bigquery_skips_when_no_row_filters_present():
    bq_contract = _contract(
        binding={
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {"project": "p", "dataset": "d", "table": "t"},
        },
        agent_policy={"allowedModels": ["claude-haiku"]},
    )
    bq = compile_agent_policy_to_iam(contract=bq_contract, target="bigquery")
    assert "Skipping demo" in bq[0].sql


def test_bigquery_warns_when_no_allowed_models_so_grants_authenticated_users():
    bq_contract = _contract(
        binding={
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {"project": "p", "dataset": "d", "table": "t"},
        },
        row_filters=[{"column": "tenant", "equals": "x"}],
    )
    bq = compile_agent_policy_to_iam(contract=bq_contract, target="bigquery")
    assert 'GRANT TO ("allAuthenticatedUsers")' in bq[0].sql
    assert any("allAuthenticatedUsers" in w for w in bq[0].warnings)


def test_aws_lake_formation_emits_boto3_script_with_data_cells_filter():
    aws_contract = _contract(
        binding={
            "platform": "aws",
            "format": "athena_table",
            "location": {"database": "analytics", "table": "events"},
        },
        agent_policy={"allowedModels": ["claude-haiku", "gpt-4o-mini"]},
        row_filters=[
            {"column": "tenant_id", "equals": "${caller.user}"},
            {"column": "region", "equals": "eu"},
        ],
    )
    aws = compile_agent_policy_to_iam(contract=aws_contract, target="aws")
    assert len(aws) == 1
    py = aws[0].sql
    # Real boto3 calls, not a TODO placeholder.
    assert "import boto3" in py
    assert "lf.create_data_cells_filter" in py
    assert "lf.grant_permissions" in py
    # Filter expression carries the row-filter predicates.
    assert "tenant_id" in py
    assert "region" in py
    assert "= 'eu'" in py
    # IAM role naming convention surfaces.
    assert "arn:aws:iam::<ACCOUNT_ID>:role/fluid-mcp-claude-haiku" in py
    assert "arn:aws:iam::<ACCOUNT_ID>:role/fluid-mcp-gpt-4o-mini" in py
    # Database/table from the binding land verbatim.
    assert 'DATABASE = "analytics"' in py
    assert 'TABLE = "events"' in py


def test_aws_lake_formation_skips_when_no_row_filters_or_allowed_models():
    aws_contract = _contract(
        binding={
            "platform": "aws",
            "format": "athena_table",
            "location": {"database": "analytics", "table": "events"},
        },
        agent_policy={"canStore": False},
    )
    aws = compile_agent_policy_to_iam(contract=aws_contract, target="aws")
    assert len(aws) == 1
    assert "Skipping demo" in aws[0].sql


def test_compiler_rejects_injection_in_column_name():
    contract = _contract(row_filters=[{"column": "tenant; DROP TABLE", "equals": "x"}])
    with pytest.raises(Exception):
        compile_agent_policy_to_iam(contract=contract, target="snowflake")
