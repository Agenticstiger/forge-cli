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

"""Compile FLUID ``agentPolicy`` blocks to cloud-native IAM /
row-access SQL.

Closes the gap where the gateway enforces ``agentPolicy`` only for
calls THROUGH it — bypass-the-gateway scenarios (an analyst querying
Snowflake directly with their own role, an LLM with raw warehouse
credentials) were ungated by the contract.

Today this module emits **Snowflake Row Access Policies** that
honour ``agentPolicy.rowFilters`` against the
``CURRENT_ROLE() / CURRENT_USER()`` Snowflake context. Operators
apply the emitted SQL via ``fluid policy-apply --from-agent-policy
--target snowflake``; the result is defence-in-depth: the gateway
catches gateway-bound traffic, the cloud-native policy catches
direct-bypass traffic, both reference the same contract.

Other targets (BigQuery row-level access, Postgres ``CREATE POLICY``,
AWS Lake Formation) are stubs that emit clear ``-- TODO`` comments
so an operator copy-pasting the output knows which engine has full
support today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence

from fluid_build.providers._sql_safety import (
    quote_ansi_string_literal,
    quote_string_literal,
    validate_ident,
)

SUPPORTED_TARGETS = ("snowflake", "postgres", "bigquery", "aws")


@dataclass(frozen=True)
class CompiledPolicy:
    """Result of compiling one expose's ``agentPolicy`` against one
    cloud target. ``sql`` is ready for ``fluid policy-apply``;
    ``warnings`` lists the agentPolicy fields the target can't yet
    enforce so operators know to plug the gap with another control.
    """

    target: str
    expose_id: str
    sql: str
    warnings: Sequence[str]


def compile_agent_policy_to_iam(
    *,
    contract: Mapping[str, Any],
    target: str,
) -> List[CompiledPolicy]:
    """Compile every expose in ``contract`` for ``target`` engine.

    ``target`` ∈ ``SUPPORTED_TARGETS``. Returns one
    :class:`CompiledPolicy` per expose that has an ``agentPolicy``
    block with at least one runtime-enforceable field. Exposes
    without policy fields are skipped silently.
    """
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"unsupported IAM target {target!r}; supported: {SUPPORTED_TARGETS}")
    out: List[CompiledPolicy] = []
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        expose_id = expose.get("exposeId")
        if not isinstance(expose_id, str):
            continue
        agent_policy = (expose.get("policy") or {}).get("agentPolicy") or {}
        row_filters = (expose.get("policy") or {}).get("rowFilters") or []
        if not agent_policy and not row_filters:
            continue
        if target == "snowflake":
            compiled = _compile_snowflake(expose, expose_id, agent_policy, row_filters)
        elif target == "postgres":
            compiled = _compile_postgres(expose, expose_id, agent_policy, row_filters)
        elif target == "bigquery":
            compiled = _compile_bigquery(expose, expose_id, agent_policy, row_filters)
        elif target == "aws":
            compiled = _compile_aws(expose, expose_id, agent_policy, row_filters)
        else:  # pragma: no cover - exhaustive above
            continue
        if compiled is not None:
            out.append(compiled)
    return out


# ---------------------------------------------------------------------
# Snowflake — full row-access-policy + role grant compilation
# ---------------------------------------------------------------------


def _compile_snowflake(
    expose: Mapping[str, Any],
    expose_id: str,
    agent_policy: Mapping[str, Any],
    row_filters: Sequence[Mapping[str, Any]],
) -> Optional[CompiledPolicy]:
    binding = expose.get("binding") or {}
    location = binding.get("location") or {}
    database = location.get("database")
    schema = location.get("schema")
    table = location.get("table")
    if not (database and schema and table):
        return CompiledPolicy(
            target="snowflake",
            expose_id=expose_id,
            sql=(f"-- Skipping {expose_id}: snowflake binding missing " "database/schema/table.\n"),
            warnings=["binding.location is incomplete"],
        )
    db = validate_ident(str(database))
    sc = validate_ident(str(schema))
    tb = validate_ident(str(table))
    fully_qualified_table = f'"{db}"."{sc}"."{tb}"'
    policy_name = f"agent_policy_{expose_id}".lower()
    policy_qualified = f'"{db}"."{sc}"."{policy_name}"'

    warnings: List[str] = []
    predicates: List[str] = []

    # 1) Per-tenant row filters → Snowflake RAP body.
    for entry in row_filters:
        if not isinstance(entry, Mapping):
            continue
        column = entry.get("column")
        if not isinstance(column, str) or not column:
            continue
        col = validate_ident(column)
        if "equals" in entry:
            value = entry["equals"]
            if isinstance(value, str) and value.startswith("${caller."):
                # Caller placeholders → CURRENT_ROLE() / CURRENT_USER().
                attr = value[len("${caller.") : -1]
                if attr in {"role", "current_role"}:
                    predicates.append(f'"{col}" = CURRENT_ROLE()')
                elif attr in {"user", "current_user", "principal"}:
                    predicates.append(f'"{col}" = CURRENT_USER()')
                else:
                    # Fallback: ROLE_NAME-suffixed convention.
                    # Operator can still hand-edit the emitted SQL.
                    predicates.append(f"\"{col}\" = SPLIT_PART(CURRENT_ROLE(), '_', -1)")
                    warnings.append(
                        f"rowFilter caller.{attr} mapped to "
                        "SPLIT_PART(CURRENT_ROLE(), '_', -1) — review the "
                        "emitted policy and adjust to your role naming "
                        "convention."
                    )
            else:
                # Constant equality → keep verbatim, quote the literal via the
                # central _sql_safety chokepoint. Snowflake honours ``\``
                # escapes inside ``'...'``, so this site needs the
                # backslash-escaping variant to stay break-out-proof.
                predicates.append(f'"{col}" = {quote_string_literal(str(value))}')

    # 2) agentPolicy.allowedModels → restrict by tag/role.
    # Snowflake doesn't expose "calling LLM" natively, so the
    # convention is: the Snowflake role used for the data product
    # is named ``FLUID_MODEL_<NORMALISED>``. Operators map their
    # MLOps tooling to those roles. We emit a warning so the
    # operator knows this convention is in play.
    allowed_models = agent_policy.get("allowedModels") or []
    if allowed_models:
        role_set = ", ".join(f"'FLUID_MODEL_{_normalise_role(m)}'" for m in allowed_models)
        predicates.append(f"CURRENT_ROLE() IN ({role_set})")
        warnings.append(
            "allowedModels mapped to roles named FLUID_MODEL_<MODEL>; "
            "create those roles + grant them to the analyst accounts "
            "that proxy the LLM."
        )

    if not predicates:
        return CompiledPolicy(
            target="snowflake",
            expose_id=expose_id,
            sql=(
                f"-- Skipping {expose_id}: agentPolicy declares no enforceable "
                "row-level rules for Snowflake. Add `allowedModels`, "
                "`rowFilters`, or column-restriction grants.\n"
            ),
            warnings=["agentPolicy carries no row-level fields"],
        )
    body = " AND ".join(predicates)
    sql_lines = [
        f"-- Generated by fluid_build.output_ports.iam_compiler for " f"expose {expose_id!r}.",
        f"-- Defends against bypass-the-gateway reads of {fully_qualified_table}.",
        f"CREATE OR REPLACE ROW ACCESS POLICY {policy_qualified}",
        "  AS () RETURNS BOOLEAN ->",
        f"    {body};",
        f"ALTER TABLE {fully_qualified_table}",
        f"  ADD ROW ACCESS POLICY {policy_qualified} ON ();",
    ]
    return CompiledPolicy(
        target="snowflake",
        expose_id=expose_id,
        sql="\n".join(sql_lines) + "\n",
        warnings=warnings,
    )


def _normalise_role(name: str) -> str:
    """Snowflake role names: uppercase, only [A-Z0-9_]."""
    out = []
    for ch in name.upper():
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    normalised = "".join(out).strip("_")
    return normalised or "DEFAULT"


# ---------------------------------------------------------------------
# Postgres — CREATE POLICY using session.user/role
# ---------------------------------------------------------------------


def _compile_postgres(
    expose: Mapping[str, Any],
    expose_id: str,
    agent_policy: Mapping[str, Any],
    row_filters: Sequence[Mapping[str, Any]],
) -> Optional[CompiledPolicy]:
    binding = expose.get("binding") or {}
    location = binding.get("location") or {}
    schema = location.get("schema") or "public"
    table = location.get("table")
    if not table:
        return None
    sc = validate_ident(str(schema))
    tb = validate_ident(str(table))
    fully_qualified_table = f'"{sc}"."{tb}"'
    policy_name = f"agent_policy_{expose_id}".lower()
    warnings: List[str] = []
    predicates: List[str] = []
    for entry in row_filters:
        if not isinstance(entry, Mapping):
            continue
        column = entry.get("column")
        if not isinstance(column, str) or not column:
            continue
        col = validate_ident(column)
        if "equals" in entry:
            value = entry["equals"]
            if isinstance(value, str) and value.startswith("${caller."):
                attr = value[len("${caller.") : -1]
                if attr in {"user", "current_user", "principal"}:
                    predicates.append(f'"{col}" = current_user')
                elif attr in {"role", "current_role"}:
                    predicates.append(f'"{col}" = current_role')
                else:
                    warnings.append(
                        f"rowFilter caller.{attr} has no Postgres mapping; "
                        "edit the emitted policy to reference your "
                        "session.* GUC convention."
                    )
                    predicates.append(f"\"{col}\" = current_setting('fluid.caller_{attr}', true)")
            else:
                # PostgreSQL literals are standard-conforming (``\`` is an
                # ordinary character) — quote with the ANSI variant so values
                # containing a backslash are not corrupted.
                predicates.append(f'"{col}" = {quote_ansi_string_literal(str(value))}')
    if not predicates:
        return None
    body = " AND ".join(predicates)
    sql = (
        f"-- Generated by fluid_build.output_ports.iam_compiler for "
        f"expose {expose_id!r}.\n"
        f"ALTER TABLE {fully_qualified_table} ENABLE ROW LEVEL SECURITY;\n"
        f"DROP POLICY IF EXISTS {policy_name} ON {fully_qualified_table};\n"
        f"CREATE POLICY {policy_name} ON {fully_qualified_table}\n"
        f"  FOR SELECT USING ({body});\n"
    )
    if agent_policy.get("allowedModels"):
        warnings.append(
            "allowedModels has no native Postgres mapping; pair with a "
            "GRANT to a role used by the LLM proxy."
        )
    return CompiledPolicy(target="postgres", expose_id=expose_id, sql=sql, warnings=warnings)


# ---------------------------------------------------------------------
# BigQuery — full row-access policy emission via standard CREATE OR
# REPLACE ROW ACCESS POLICY syntax.
# ---------------------------------------------------------------------


def _compile_bigquery(
    expose: Mapping[str, Any],
    expose_id: str,
    agent_policy: Mapping[str, Any],
    row_filters: Sequence[Mapping[str, Any]],
) -> Optional[CompiledPolicy]:
    binding = expose.get("binding") or {}
    location = binding.get("location") or {}
    project = location.get("project")
    dataset = location.get("dataset")
    table = location.get("table")
    if not (project and dataset and table):
        return None
    fq = f"`{project}.{dataset}.{table}`"
    policy_name = f"agent_policy_{expose_id}".lower()
    warnings: List[str] = []
    predicates: List[str] = []
    grantees: List[str] = []

    # 1) rowFilters → BigQuery FILTER USING clauses. BigQuery exposes
    # SESSION_USER() but not "current LLM"; ${caller.user} maps to it.
    for entry in row_filters:
        if not isinstance(entry, Mapping):
            continue
        column = entry.get("column")
        if not isinstance(column, str) or not column:
            continue
        col = validate_ident(column)
        if "equals" in entry:
            value = entry["equals"]
            if isinstance(value, str) and value.startswith("${caller."):
                attr = value[len("${caller.") : -1]
                if attr in {"user", "current_user", "principal"}:
                    predicates.append(f"`{col}` = SESSION_USER()")
                else:
                    # No native BigQuery primitive for arbitrary
                    # caller attributes. Surface the gap loud and
                    # leave a hand-editable WHERE.
                    predicates.append(
                        f"`{col}` = SESSION_USER()  -- TODO: replace with caller.{attr} mapping"
                    )
                    warnings.append(
                        f"rowFilter caller.{attr} has no BigQuery primitive; "
                        "emitted SESSION_USER() placeholder — review."
                    )
            else:
                escaped = str(value).replace("'", r"\'")
                predicates.append(f"`{col}` = '{escaped}'")

    # 2) allowedModels → grantees set. BigQuery row-access policies
    # take a GRANT TO clause that lists IAM identities (users,
    # groups, service accounts). Convention: each LLM is proxied by
    # a GCP service account named ``fluid-mcp-<MODEL>@<project>.iam.gserviceaccount.com``.
    allowed_models = agent_policy.get("allowedModels") or []
    for model in allowed_models:
        normalised = _normalise_role(model).lower().replace("_", "-")
        grantees.append(
            f'"serviceAccount:fluid-mcp-{normalised}@{project}.iam.gserviceaccount.com"'
        )
    if allowed_models:
        warnings.append(
            "allowedModels mapped to service accounts named "
            f"fluid-mcp-<MODEL>@{project}.iam.gserviceaccount.com — provision "
            "those SAs and grant them to the analyst principals that proxy each LLM."
        )

    if not predicates:
        return CompiledPolicy(
            target="bigquery",
            expose_id=expose_id,
            sql=f"-- Skipping {expose_id}: no rowFilters → no BigQuery RAP body.\n",
            warnings=["agentPolicy carries no row-level fields"],
        )
    grant_clause = (
        f"GRANT TO ({', '.join(grantees)})\n"
        if grantees
        else 'GRANT TO ("allAuthenticatedUsers")\n'
    )
    if not grantees:
        warnings.append(
            'no allowedModels → emitted GRANT TO ("allAuthenticatedUsers"); '
            "tighten before applying in production."
        )
    body = " AND ".join(predicates)
    sql = (
        f"-- Generated by fluid_build.output_ports.iam_compiler for expose {expose_id!r}.\n"
        f"-- Defends against bypass-the-gateway reads of {fq}.\n"
        f"CREATE OR REPLACE ROW ACCESS POLICY {policy_name}\n"
        f"  ON {fq}\n"
        f"  {grant_clause}"
        f"  FILTER USING ({body});\n"
    )
    return CompiledPolicy(target="bigquery", expose_id=expose_id, sql=sql, warnings=warnings)


# ---------------------------------------------------------------------
# AWS — Lake Formation data-cell filter + permission grant. Output
# is the boto3 API call sequence (as Python) rather than SQL because
# Lake Formation has no SQL surface; operators apply via:
#   python <emitted-script>
# or by pasting into a CDK/Terraform module.
# ---------------------------------------------------------------------


def _compile_aws(
    expose: Mapping[str, Any],
    expose_id: str,
    agent_policy: Mapping[str, Any],
    row_filters: Sequence[Mapping[str, Any]],
) -> Optional[CompiledPolicy]:
    binding = expose.get("binding") or {}
    location = binding.get("location") or {}
    database = location.get("database")
    table = location.get("table")
    if not (database and table):
        return None

    warnings: List[str] = []
    filter_predicates: List[str] = []
    for entry in row_filters:
        if not isinstance(entry, Mapping):
            continue
        column = entry.get("column")
        if not isinstance(column, str) or not column:
            continue
        col = validate_ident(column)
        if "equals" in entry:
            value = entry["equals"]
            if isinstance(value, str) and value.startswith("${caller."):
                attr = value[len("${caller.") : -1]
                if attr in {"user", "principal"}:
                    # Lake Formation row-filter expressions don't have
                    # a SESSION_USER() primitive — operators bind row
                    # filters to specific IAM principals via
                    # CreateDataCellsFilter + GrantPermissions. We
                    # emit a per-principal scaffold: caller attribute
                    # is annotated for the operator to wire.
                    filter_predicates.append(
                        f'"{col}" = current_user  -- caller.{attr}; substitute with literal per principal'
                    )
                    warnings.append(
                        f"rowFilter caller.{attr} requires per-principal "
                        "CreateDataCellsFilter calls; the emitted script "
                        "uses a placeholder — duplicate per principal."
                    )
                else:
                    filter_predicates.append(f"\"{col}\" = '<caller.{attr}-per-principal>'")
                    warnings.append(
                        f"rowFilter caller.{attr} mapped to per-principal "
                        "literal — fill in via your IAM workflow."
                    )
            else:
                # Lake Formation filter expressions are evaluated by the
                # Athena/Trino engine — standard-conforming literals.
                filter_predicates.append(f'"{col}" = {quote_ansi_string_literal(str(value))}')

    allowed_models = agent_policy.get("allowedModels") or []
    grantee_arns: List[str] = []
    for model in allowed_models:
        normalised = _normalise_role(model).lower().replace("_", "-")
        grantee_arns.append(f'"arn:aws:iam::<ACCOUNT_ID>:role/fluid-mcp-{normalised}"')
    if allowed_models:
        warnings.append(
            "allowedModels mapped to IAM roles named "
            "arn:aws:iam::<ACCOUNT>:role/fluid-mcp-<MODEL>; provision "
            "those roles and trust the principals that proxy each LLM."
        )

    if not filter_predicates and not grantee_arns:
        return CompiledPolicy(
            target="aws",
            expose_id=expose_id,
            sql=f"# Skipping {expose_id}: no rowFilters or allowedModels for AWS Lake Formation.\n",
            warnings=["agentPolicy carries no Lake Formation-applicable fields"],
        )

    filter_name = f"agent_policy_{expose_id}".lower()
    row_filter_expression = " AND ".join(filter_predicates) if filter_predicates else "true"
    grantees_repr = (
        "[\n        " + ",\n        ".join(grantee_arns) + ",\n    ]" if grantee_arns else "[]"
    )

    py = (
        f'"""Generated by fluid_build.output_ports.iam_compiler for '
        f"expose {expose_id!r}.\n"
        f"AWS Lake Formation has no SQL surface; this script applies the\n"
        f"compiled agentPolicy via boto3. Run with `python <this-file>` or\n"
        f'paste into a CDK/Terraform module.\n"""\n'
        "import boto3\n\n"
        f'DATABASE = "{database}"\n'
        f'TABLE = "{table}"\n'
        f"GRANTEES = {grantees_repr}\n"
        f'ROW_FILTER_EXPRESSION = "{row_filter_expression}"\n\n'
        "lf = boto3.client('lakeformation')\n\n"
        "# 1. Create or update the data-cells filter (row-level access rule).\n"
        "lf.create_data_cells_filter(\n"
        "    TableData={\n"
        f'        "TableCatalogId": "<ACCOUNT_ID>",\n'
        f'        "DatabaseName": DATABASE,\n'
        f'        "TableName": TABLE,\n'
        f'        "Name": "{filter_name}",\n'
        '        "RowFilter": {"FilterExpression": ROW_FILTER_EXPRESSION},\n'
        '        "ColumnWildcard": {"ExcludedColumnNames": []},\n'
        "    }\n"
        ")\n\n"
        "# 2. Grant SELECT on the filter to each LLM-proxy IAM role.\n"
        "for grantee_arn in GRANTEES:\n"
        "    lf.grant_permissions(\n"
        '        Principal={"DataLakePrincipalIdentifier": grantee_arn},\n'
        "        Resource={\n"
        '            "DataCellsFilter": {\n'
        '                "TableCatalogId": "<ACCOUNT_ID>",\n'
        '                "DatabaseName": DATABASE,\n'
        '                "TableName": TABLE,\n'
        f'                "Name": "{filter_name}",\n'
        "            }\n"
        "        },\n"
        '        Permissions=["SELECT"],\n'
        "    )\n"
    )
    return CompiledPolicy(target="aws", expose_id=expose_id, sql=py, warnings=warnings)
