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


def compile_policy(contract: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Compile accessPolicy from contract into provider IAM bindings.

    Reads binding.platform from the contract schema to determine the
    provider and generate appropriate bindings.  The provider and project
    are embedded in the output so downstream tools (policy-apply) don't
    need separate flags.

    Returns:
        (bindings, warnings) where bindings is a list of IAM binding dicts
    """
    bindings = []
    warnings = []
    grants = (contract.get("accessPolicy") or {}).get("grants", [])

    if not grants:
        warnings.append("No grants found in accessPolicy")
        return bindings, warnings

    for g in grants:
        principal = g.get("principal")
        if not principal:
            warnings.append("Grant missing principal, skipping")
            continue

        permissions = g.get("permissions", ["read"])

        for exp in contract.get("exposes", []):
            binding = exp.get("binding", {})
            platform = binding.get("platform", "")
            fmt = binding.get("format", "")
            loc = binding.get("location", {})

            if platform == "gcp" or fmt in ("bigquery_table", "gcs_parquet_files", "gcs_file"):
                _compile_gcp_bindings(bindings, fmt, loc, principal, permissions)
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


def _compile_aws_bindings(bindings, fmt, loc, principal, permissions):
    """Generate AWS IAM policy statements."""
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
    if database:
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
