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

import logging
from typing import List

from fluid_build.util.contract import get_expose_binding, get_expose_id, get_expose_location

from ..base import ApplyResult, PlanAction

try:
    from google.cloud import bigquery
except Exception:  # pragma: no cover
    bigquery = None


def plan_bigquery(contract: dict) -> List[PlanAction]:
    actions: List[PlanAction] = []
    for exp in contract.get("exposes", []):
        binding = get_expose_binding(exp)
        location = get_expose_location(exp)

        if not binding or not location:
            continue

        format_type = binding.get("format")
        if format_type == "bigquery_table" or format_type == "snowflake_table":
            # Get location properties - handle both old and new formats
            if isinstance(location, dict):
                project = location.get("project") or "UNKNOWN_PROJ"
                dataset = location.get("dataset", "UNKNOWN_DS")
                table = location.get("table", get_expose_id(exp))
            else:
                # Fallback for old string format
                project = "UNKNOWN_PROJ"
                dataset = "UNKNOWN_DS"
                table = get_expose_id(exp)

            # Get schema from contract
            schema = exp.get("contract", {}).get("schema", []) or exp.get("schema", [])
            actions.append(
                PlanAction(
                    op="create",
                    resource_type="bq.table",
                    resource_id=f"{project}:{dataset}.{table}",
                    payload={"schema": schema, "partitioning": None},
                )
            )
    return actions


def apply_bigquery(actions: List[PlanAction], client=None, dry_run=False) -> List[ApplyResult]:
    results: List[ApplyResult] = []
    if bigquery is None:
        return [ApplyResult(False, "google-cloud-bigquery not installed", error="missing_dep")]
    client = client or bigquery.Client()
    for a in actions:
        if a.resource_type != "bq.table":
            continue
        try:
            proj, rest = a.resource_id.split(":")
            dataset, table = rest.split(".")
            ds_ref = bigquery.DatasetReference(proj, dataset)
            tb_ref = ds_ref.table(table)
            schema = [
                bigquery.SchemaField(col["name"], (col["type"] or "STRING"))
                for col in a.payload.get("schema", [])
            ]
            tbl = bigquery.Table(tb_ref, schema=schema)
            if dry_run:
                results.append(ApplyResult(True, f"[DRY‑RUN] would create table {a.resource_id}"))
                continue
            client.create_table(tbl, exists_ok=True)
            results.append(ApplyResult(True, f"Created/ensured table {a.resource_id}"))
        except Exception as e:
            logging.getLogger("fluid.providers.gcp.bq").exception("bq_apply_failed")
            results.append(ApplyResult(False, f"Failed {a.resource_id}", error=str(e)))
    return results
