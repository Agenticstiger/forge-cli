# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""AWS IaC plugin — FLUID contract → Glue catalog + S3 ``.tf.json``.

Translates AWS-bound exposures into a Glue catalog database + table and
the backing S3 bucket. A pure function of the contract; no credentials,
no network.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..naming import safe_ident
from ..versions import required_providers

# FLUID column type → Hive/Glue column type.
_HIVE_TYPES = {
    "string": "string",
    "str": "string",
    "text": "string",
    "integer": "int",
    "int": "int",
    "int32": "int",
    "bigint": "bigint",
    "int64": "bigint",
    "long": "bigint",
    "float": "float",
    "float32": "float",
    "double": "double",
    "float64": "double",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "binary": "binary",
    "bytes": "binary",
}


def _hive_type(raw: Any) -> str:
    t = str(raw or "string").strip().lower()
    if t.startswith(("decimal", "numeric")):
        # decimal(10,2) passes through; a bare type widens to a safe default.
        return t.replace("numeric", "decimal") if "(" in t else "decimal(38,9)"
    return _HIVE_TYPES.get(t, "string")


def _columns(schema: List[Mapping[str, Any]]) -> List[Dict[str, str]]:
    columns: List[Dict[str, str]] = []
    for col in schema or []:
        entry: Dict[str, str] = {"name": col.get("name"), "type": _hive_type(col.get("type"))}
        if col.get("description"):
            entry["comment"] = col["description"]
        columns.append(entry)
    return columns


class AwsIacPlugin:
    """``IacProviderPlugin`` for Amazon Web Services (Glue catalog + S3)."""

    name = "aws"
    required_providers = required_providers("aws")
    # `tofu` reads whichever AWS_* var is set; the emitted `.tf.json`
    # stays credential-free regardless of the auth method.
    credential_env_vars = (
        # Static / temporary credentials.
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        # Named profile + shared config / credentials files.
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        # AssumeRoleWithWebIdentity — OIDC federation (CI runners, EKS IRSA).
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_SESSION_NAME",
        # Region.
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )

    def emit(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        tags = {"managed_by": "fluid", "fluid_contract": cid}

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            if binding.get("platform") != "aws":
                continue
            loc = binding.get("location") or {}
            fmt = binding.get("format") or "parquet"
            schema = (exposure.get("contract") or {}).get("schema") or []
            _emit_glue(resources, loc, fmt, schema, cid, tags)
            _emit_s3(resources, loc, cid, tags)
        return resources


def _emit_glue(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    fmt: str,
    schema: List[Mapping[str, Any]],
    cid: str,
    tags: Dict[str, str],
) -> None:
    database = loc.get("database")
    if not database:
        return
    db_name = safe_ident(f"{cid}_{database}")
    resources.setdefault("aws_glue_catalog_database", {}).setdefault(db_name, {"name": database})

    table = loc.get("table")
    if not table:
        return
    storage: Dict[str, Any] = {"columns": _columns(schema)}
    bucket = loc.get("bucket")
    if bucket:
        storage["location"] = f"s3://{bucket}/{(loc.get('path') or '').lstrip('/')}"
    resources.setdefault("aws_glue_catalog_table", {})[safe_ident(f"{cid}_{database}_{table}")] = {
        "name": table,
        "database_name": f"${{aws_glue_catalog_database.{db_name}.name}}",
        "table_type": "EXTERNAL_TABLE",
        "parameters": {"classification": fmt, "managed_by": "fluid"},
        "storage_descriptor": storage,
    }


def _emit_s3(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    bucket = loc.get("bucket")
    if not bucket:
        return
    resources.setdefault("aws_s3_bucket", {}).setdefault(
        safe_ident(f"{cid}_{bucket}"),
        {"bucket": bucket, "force_destroy": True, "tags": tags},
    )
