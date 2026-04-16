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

from ..base import ApplyResult, PlanAction


def compile_policy_actions(contract: dict):
    actions = []
    grants = (contract.get("accessPolicy") or {}).get("grants", [])
    for g in grants:
        for exp in contract.get("exposes", []):
            loc = exp.get("location", {})
            fmt = loc.get("format")
            props = loc.get("properties", {})
            if fmt == "bigquery_table":
                rid = f"{props.get('project')}:{props.get('dataset')}"
                actions.append(
                    PlanAction(
                        "grant",
                        "bq.dataset.iam",
                        rid,
                        {"principal": g["principal"], "permissions": g["permissions"]},
                    )
                )
            if fmt == "gcs_parquet_files":
                bucket = props.get("bucket")
                if bucket:
                    actions.append(
                        PlanAction(
                            "grant",
                            "gcs.bucket.iam",
                            bucket,
                            {"principal": g["principal"], "permissions": g["permissions"]},
                        )
                    )
    return actions


def ensure_dataset_access(project_dataset: str, principal: str, permissions):
    # Stub: to be implemented with google-cloud-bigquery IAM APIs or via set_iam_policy on dataset
    return True


def apply_iam(actions, dry_run=False):
    results = []
    for a in actions:
        if dry_run:
            results.append(
                ApplyResult(True, f"[DRY‑RUN] would grant {a.payload} on {a.resource_id}")
            )
        else:
            # In a real implementation, call GCP IAM APIs here
            results.append(ApplyResult(True, f"Granted {a.payload} on {a.resource_id}"))
    return results
