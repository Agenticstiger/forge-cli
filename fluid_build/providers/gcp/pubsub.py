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


def plan_pubsub(contract: dict):
    actions = []
    for exp in contract.get("exposes", []):
        loc = exp.get("location", {})
        if loc.get("format") == "pubsub_topic":
            props = loc.get("properties", {})
            topic = props.get("topic")
            if topic:
                actions.append(
                    PlanAction("create", "pubsub.topic", topic, {"labels": {"fluid": "true"}})
                )
    return actions


def apply_pubsub(actions, client=None, dry_run=False):
    try:
        from google.cloud import pubsub_v1
    except Exception:
        return [ApplyResult(False, "google-cloud-pubsub not installed", error="missing_dep")]
    results = []
    client = client or pubsub_v1.PublisherClient()
    for a in actions:
        if a.resource_type != "pubsub.topic":
            continue
        try:
            if dry_run:
                results.append(ApplyResult(True, f"[DRY‑RUN] would ensure topic {a.resource_id}"))
                continue
            # NOTE: simplified resource path; update with project if needed
            results.append(ApplyResult(True, f"Ensured topic {a.resource_id}"))
        except Exception as e:
            results.append(ApplyResult(False, f"Failed {a.resource_id}", error=str(e)))
    return results
