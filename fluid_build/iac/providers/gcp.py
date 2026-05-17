# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""GCP IaC plugin — FLUID contract → BigQuery / GCS / Pub-Sub ``.tf.json``.

Walks ``exposes[]`` and translates each ``binding.format`` into the
matching ``hashicorp/google`` resource. A pure function of the contract;
no credentials, no network.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping

from ..naming import safe_ident
from ..versions import required_providers

# FLUID column type → BigQuery type (best-effort; unknown types upper-cased).
_BQ_TYPES = {
    "string": "STRING",
    "str": "STRING",
    "text": "STRING",
    "integer": "INT64",
    "int": "INT64",
    "int64": "INT64",
    "bigint": "INT64",
    "float": "FLOAT64",
    "float64": "FLOAT64",
    "double": "FLOAT64",
    "numeric": "NUMERIC",
    "decimal": "NUMERIC",
    "boolean": "BOOL",
    "bool": "BOOL",
    "timestamp": "TIMESTAMP",
    "datetime": "DATETIME",
    "date": "DATE",
    "time": "TIME",
    "bytes": "BYTES",
    "json": "JSON",
}


def _bq_type(raw: Any) -> str:
    base = str(raw or "STRING").strip().lower().split("(", 1)[0]
    return _BQ_TYPES.get(base, str(raw).upper() if raw else "STRING")


def _bq_schema(schema: List[Mapping[str, Any]]) -> str:
    """FLUID contract schema → BigQuery schema JSON string."""
    fields = [
        {
            "name": col.get("name"),
            "type": _bq_type(col.get("type")),
            "mode": "REQUIRED" if col.get("required") else "NULLABLE",
            "description": col.get("description", ""),
        }
        for col in schema or []
    ]
    return json.dumps(fields, sort_keys=True)


class GcpIacPlugin:
    """``IacProviderPlugin`` for Google Cloud."""

    name = "gcp"
    required_providers = required_providers("google")
    # `tofu` reads whichever GOOGLE_* var is set; the emitted `.tf.json`
    # stays credential-free regardless of the auth method.
    credential_env_vars = (
        # Service account key / Application Default Credentials /
        # Workload Identity Federation config file (keyless CI auth).
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CREDENTIALS",
        # Short-lived OAuth 2.0 access token.
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        # Service account impersonation.
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
        # Project / region.
        "GOOGLE_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_REGION",
    )

    def emit(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        labels = {"managed_by": "fluid", "fluid_contract": cid}

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            fmt = binding.get("format")
            loc = binding.get("location") or {}
            schema = (exposure.get("contract") or {}).get("schema") or []
            if fmt in ("bigquery_table", "bigquery_view"):
                _emit_bigquery(
                    resources, exposure, loc, schema, cid, labels, is_view=(fmt == "bigquery_view")
                )
            elif fmt == "gcs_bucket":
                _emit_gcs(resources, loc, cid, labels)
            elif fmt == "pubsub_topic":
                _emit_pubsub(resources, loc, cid, labels)
        return resources


def _emit_bigquery(
    resources: Dict[str, Any],
    exposure: Mapping[str, Any],
    loc: Mapping[str, Any],
    schema: List[Mapping[str, Any]],
    cid: str,
    labels: Dict[str, str],
    *,
    is_view: bool,
) -> None:
    dataset = loc.get("dataset") or "default"
    table = loc.get("table") or loc.get("view") or exposure.get("exposeId") or "table"
    ds_name = safe_ident(f"{cid}_{dataset}")
    tbl_name = safe_ident(f"{cid}_{table}")

    resources.setdefault("google_bigquery_dataset", {}).setdefault(
        ds_name,
        {
            "dataset_id": dataset,
            "location": loc.get("region") or loc.get("location") or "US",
            "labels": labels,
        },
    )

    body: Dict[str, Any] = {
        "dataset_id": f"${{google_bigquery_dataset.{ds_name}.dataset_id}}",
        "table_id": table,
        "labels": labels,
        # Let `tofu destroy` clean the table — the spike applies and destroys.
        "deletion_protection": False,
    }
    if is_view:
        body["view"] = {"query": loc.get("query", ""), "use_legacy_sql": False}
    elif schema:
        body["schema"] = _bq_schema(schema)
    resources.setdefault("google_bigquery_table", {})[tbl_name] = body


def _emit_gcs(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, labels: Dict[str, str]
) -> None:
    bucket = loc.get("bucket") or f"{cid}-bucket"
    resources.setdefault("google_storage_bucket", {})[safe_ident(f"{cid}_{bucket}")] = {
        "name": bucket,
        "location": loc.get("region") or loc.get("location") or "US",
        "uniform_bucket_level_access": True,
        "force_destroy": True,
        "labels": labels,
    }


def _emit_pubsub(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, labels: Dict[str, str]
) -> None:
    topic = loc.get("topic") or f"{cid}-topic"
    resources.setdefault("google_pubsub_topic", {})[safe_ident(f"{cid}_{topic}")] = {
        "name": topic,
        "labels": labels,
    }
