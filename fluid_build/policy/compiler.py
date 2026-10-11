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

from typing import Any, Dict, List, Tuple

from ._common import iter_exposes

SAFE_BQ_PERMS = {
    "readData": ["roles/bigquery.dataViewer"],
    "readMetadata": ["roles/bigquery.metadataViewer"],
    "manage": ["roles/bigquery.dataOwner"],  # tighten in prod
}
SAFE_GCS_PERMS = {
    "readData": ["roles/storage.objectViewer"],
    "readMetadata": ["roles/storage.legacyBucketReader"],
    "manage": ["roles/storage.objectAdmin"],  # tighten in prod
}

# AWS permission mappings
SAFE_S3_PERMS = {
    "readData": ["s3:GetObject", "s3:ListBucket"],
    "readMetadata": ["s3:ListBucket", "s3:GetBucketLocation"],
    "manage": ["s3:PutObject", "s3:DeleteObject", "s3:GetObject", "s3:ListBucket"],
}
SAFE_GLUE_PERMS = {
    "readData": [
        "glue:GetTable",
        "glue:GetDatabase",
        "athena:StartQueryExecution",
        "athena:GetQueryResults",
    ],
    "readMetadata": ["glue:GetTable", "glue:GetDatabase"],
    "manage": ["glue:CreateTable", "glue:UpdateTable", "glue:DeleteTable"],
}

# Snowflake permission mappings
SAFE_SNOWFLAKE_PERMS = {
    "readData": ["SELECT"],
    "readMetadata": ["USAGE"],
    "manage": ["INSERT", "UPDATE", "DELETE", "SELECT"],
}

#: The message for a contract with no grants. It stays in ``warnings`` (and
#: so in ``bindings.json``), but it reports a legitimate no-op, not a grant
#: left unenforced, so ``fluid policy compile`` / ``apply`` do not show it as
#: a WARNING.
NO_GRANTS = "No grants found in accessPolicy"


def compile_policy(contract: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Compile accessPolicy from contract into provider IAM bindings.

    Reads binding.platform from the contract schema to determine the
    provider and generate appropriate bindings.  The provider and project
    are embedded in the output so downstream tools (policy-apply) don't
    need separate flags. An Iceberg expose is granted where its catalog
    lives: Glue grants only for a Glue-cataloged table (see
    :func:`_compile_catalog_managed_iceberg` for every other catalog).

    Returns:
        (bindings, warnings) where bindings is a list of IAM binding dicts
    """
    bindings = []
    warnings = []
    grants = (contract.get("accessPolicy") or {}).get("grants", [])

    if not grants:
        warnings.append(NO_GRANTS)
        return bindings, warnings

    for g in grants:
        principal = g.get("principal")
        if not principal:
            warnings.append("Grant missing principal, skipping")
            continue

        permissions = g.get("permissions", ["read"])

        for exp in iter_exposes(contract):
            binding = exp.get("binding", {})
            platform = binding.get("platform", "")
            fmt = binding.get("format", "")
            loc = binding.get("location", {})

            if platform == "gcp" or fmt in ("bigquery_table", "gcs_parquet_files", "gcs_file"):
                _compile_gcp_bindings(bindings, fmt, loc, principal, permissions)
            elif not _glue_cataloged(binding):
                _compile_catalog_managed_iceberg(
                    bindings, warnings, exp, binding, principal, permissions
                )
            elif _is_tableflow(binding):
                _compile_tableflow_bindings(
                    bindings, warnings, exp, fmt, loc, principal, permissions
                )
            elif platform == "aws" or fmt in ("s3_file", "iceberg", "parquet"):
                _compile_aws_bindings(bindings, fmt, loc, principal, permissions)
            elif platform == "snowflake" or fmt == "snowflake_table":
                _compile_snowflake_bindings(bindings, fmt, loc, principal, permissions)
            else:
                warnings.append(f"Unsupported platform/format: {platform}/{fmt}")

    if not bindings:
        warnings.append("No IAM bindings generated from contract")

    return bindings, warnings


def _compile_gcp_bindings(bindings, fmt, loc, principal, permissions):
    """Generate GCP IAM bindings."""
    if fmt == "bigquery_table":
        dataset = loc.get("dataset")
        project = loc.get("project")
        if dataset:
            perm_key = (
                "manage"
                if any(p in permissions for p in ("write", "insert", "update", "delete"))
                else "readData"
            )
            bindings.append(
                {
                    "provider": "gcp",
                    "resource_type": "bigquery.dataset",
                    "resource_id": f"{project}.{dataset}" if project else dataset,
                    "project": project,
                    "dataset": dataset,
                    "principal": principal,
                    "roles": SAFE_BQ_PERMS.get(perm_key, SAFE_BQ_PERMS["readData"]),
                }
            )
    elif fmt in ("gcs_parquet_files", "gcs_file"):
        bucket = loc.get("bucket")
        if bucket:
            perm_key = (
                "manage"
                if any(p in permissions for p in ("write", "insert", "update", "delete"))
                else "readData"
            )
            bindings.append(
                {
                    "provider": "gcp",
                    "resource_type": "gcs.bucket",
                    "resource_id": bucket,
                    "bucket": bucket,
                    "principal": principal,
                    "roles": SAFE_GCS_PERMS.get(perm_key, SAFE_GCS_PERMS["readData"]),
                }
            )


def _glue_cataloged(binding) -> bool:
    """Is this expose's table registered in AWS Glue?

    True for every non-Iceberg format and for an Iceberg table whose catalog
    is Glue (named, or the AWS default). The format check used to be enough
    on its own: every ``iceberg`` expose was routed to the AWS compiler, so a
    Lakekeeper table, a Snowflake-managed table and a ``platform: local``
    table each got a ``glue.table`` grant on a Glue table that does not
    exist. Reads the classification every emitter shares, imported lazily so
    this module stays cheap to import.
    """
    from ..providers._iceberg_catalog import is_glue_cataloged

    if not isinstance(binding.get("location") or {}, dict):
        return True  # malformed: keep the historic routing, schema reports it
    return is_glue_cataloged(binding)


def _compile_catalog_managed_iceberg(bindings, warnings, exp, binding, principal, permissions):
    """Compile an Iceberg expose whose catalog is not AWS Glue.

    The catalog owns the table, so the table-level grant belongs to the
    catalog's own access control (Snowflake RBAC for a Snowflake-managed
    table; Lakekeeper / Polaris / Unity authorization for a REST catalog).
    Snowflake RBAC is compiled here. A grant this compiler cannot express is
    reported as a warning, the same channel as an unsupported platform, never
    dropped: a contract that reads as "principal X may read this table" must
    not compile to silence.

    On AWS the bucket the binding names is still the table's storage, so its
    S3 statement is emitted; the Glue statement is not.
    """
    from ..iac.provider_match import canonical_cloud
    from ..providers._iceberg_catalog import (
        FAMILY_SNOWFLAKE_MANAGED,
        binding_catalog_kind,
        catalog_kind_info,
    )

    kind = binding_catalog_kind(binding)
    fmt = binding.get("format", "")
    loc = binding.get("location") or {}
    cloud = canonical_cloud(binding.get("platform"))
    snowflake_managed = catalog_kind_info(kind).family == FAMILY_SNOWFLAKE_MANAGED

    if snowflake_managed or cloud == "snowflake":
        _compile_snowflake_bindings(bindings, fmt, loc, principal, permissions)
    elif cloud == "aws":
        _compile_aws_bindings(bindings, fmt, loc, principal, permissions, glue=False)

    if snowflake_managed:
        return
    expose_id = exp.get("exposeId") or exp.get("id") or "?"
    perms = list(permissions)
    if cloud == "snowflake":
        warnings.append(
            f"Iceberg expose '{expose_id}' is cataloged in '{kind}': the Snowflake grant "
            f"compiled for {principal} covers Snowflake readers only. Enforce {perms} for "
            f"every other engine in the '{kind}' catalog's own access control."
        )
    else:
        warnings.append(
            f"Iceberg expose '{expose_id}' is cataloged in '{kind}', not AWS Glue, so no "
            f"table grant was compiled for {principal} {perms}. Enforce it in the "
            f"'{kind}' catalog's own access control."
        )


def _is_tableflow(binding) -> bool:
    """Is this a Glue-cataloged Iceberg expose the Confluent Tableflow IaC publishes?

    Matched the way that emitter matches its exposes (``is_cloud``), so a
    platform alias it publishes is compiled here too.
    """
    from ..iac.provider_match import is_cloud
    from ..providers._iceberg_catalog import is_iceberg_format

    return is_cloud(binding, "confluent") and is_iceberg_format(binding.get("format"))


def _compile_tableflow_bindings(bindings, warnings, exp, fmt, loc, principal, permissions):
    """Compile a Confluent Tableflow expose: its bucket and the Glue table it publishes.

    Tableflow publishes the table for the Kafka topic, not ``location.table``:
    the Tableflow IaC names it with ``_topic_name`` (topic > table > exposeId)
    and publishes it into ``location.database`` (``custom_database``). With no
    database, Tableflow's Glue database is the Kafka cluster id ("By default,
    the Glue database name is the cluster ID, and each topic becomes a table
    under it": docs.confluent.io/cloud/current/topics/tableflow/how-to-guides/
    catalog-integration/integrate-with-aws-glue-catalog). When neither names
    the database, the Glue grant is reported as a warning, not widened to a
    database.
    """
    from ..iac.providers.confluent import _topic_name

    topic = _topic_name(loc, exp)
    database = loc.get("database") or loc.get("kafka_cluster_id")
    if database:
        published = {**loc, "database": database, "dataset": None, "table": topic}
        _compile_aws_bindings(bindings, fmt, published, principal, permissions)
        return
    _compile_aws_bindings(bindings, fmt, loc, principal, permissions, glue=False)
    expose_id = exp.get("exposeId") or exp.get("id") or "?"
    warnings.append(
        f"Iceberg expose '{expose_id}' (platform confluent) names neither "
        f"binding.location.database nor kafka_cluster_id, so the Glue database Tableflow "
        f"publishes topic '{topic}' into is unknown and no Glue table grant was compiled "
        f"for {principal} {list(permissions)}."
    )


def _compile_aws_bindings(bindings, fmt, loc, principal, permissions, *, glue=True):
    """Generate AWS IAM policy statements.

    ``glue=False`` emits only the S3 statement, for an Iceberg table whose
    catalog is not Glue (:func:`_compile_catalog_managed_iceberg`).
    """
    bucket = loc.get("bucket")
    if bucket:
        perm_key = (
            "manage"
            if any(p in permissions for p in ("write", "insert", "update", "delete"))
            else "readData"
        )
        bindings.append(
            {
                "provider": "aws",
                "resource_type": "s3.bucket",
                "resource_id": bucket,
                "bucket": bucket,
                "region": loc.get("region"),
                "principal": principal,
                "actions": SAFE_S3_PERMS.get(perm_key, SAFE_S3_PERMS["readData"]),
            }
        )

    # Glue/Athena bindings
    database = loc.get("database") or loc.get("dataset")
    table = loc.get("table")
    if glue and database:
        perm_key = (
            "manage"
            if any(p in permissions for p in ("write", "insert", "update", "delete"))
            else "readData"
        )
        bindings.append(
            {
                "provider": "aws",
                "resource_type": "glue.table",
                "resource_id": f"{database}.{table}" if table else database,
                "database": database,
                "table": table,
                "region": loc.get("region"),
                "principal": principal,
                "actions": SAFE_GLUE_PERMS.get(perm_key, SAFE_GLUE_PERMS["readData"]),
            }
        )


def _compile_snowflake_bindings(bindings, fmt, loc, principal, permissions):
    """Generate Snowflake RBAC grants."""
    database = loc.get("database")
    schema = loc.get("schema")
    table = loc.get("table")
    if database:
        perm_key = (
            "manage"
            if any(p in permissions for p in ("write", "insert", "update", "delete"))
            else "readData"
        )
        resource_id = ".".join(filter(None, [database, schema, table]))
        bindings.append(
            {
                "provider": "snowflake",
                "resource_type": "snowflake.table" if table else "snowflake.schema",
                "resource_id": resource_id,
                "database": database,
                "schema": schema,
                "table": table,
                "principal": principal,
                "grants": SAFE_SNOWFLAKE_PERMS.get(perm_key, SAFE_SNOWFLAKE_PERMS["readData"]),
            }
        )
