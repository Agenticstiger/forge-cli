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

from ..base import ApplyResult, PlanAction

try:
    from google.cloud import storage
except Exception:  # pragma: no cover
    storage = None


def plan_gcs(contract: dict):
    actions = []
    for exp in contract.get("exposes", []):
        loc = exp.get("location", {})
        if loc.get("format") == "gcs_parquet_files":
            props = loc.get("properties", {})
            bucket = props.get("bucket")
            if bucket:
                actions.append(
                    PlanAction(
                        "create", "gcs.bucket", bucket, {"location": props.get("location", "EU")}
                    )
                )
    return actions


def apply_gcs(actions, client=None, dry_run=False):
    results = []
    if storage is None:
        return [ApplyResult(False, "google-cloud-storage not installed", error="missing_dep")]
    client = client or storage.Client()
    for a in actions:
        if a.resource_type != "gcs.bucket":
            continue
        try:
            if dry_run:
                results.append(ApplyResult(True, f"[DRY‑RUN] would ensure bucket {a.resource_id}"))
                continue
            bucket = client.bucket(a.resource_id)
            if not bucket.exists():
                client.create_bucket(a.resource_id)
            results.append(ApplyResult(True, f"Ensured bucket {a.resource_id}"))
        except Exception as e:
            logging.getLogger("fluid.providers.gcp.gcs").exception("gcs_apply_failed")
            results.append(ApplyResult(False, f"Failed {a.resource_id}", error=str(e)))
    return results
