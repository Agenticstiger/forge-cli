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

"""A Flink build and contract on a stable schema, shared by the Flink tests.

The job runs on its own platform: ``engine: flink`` (0.7.6), or
``properties.language: flink_sql`` on 0.7.5.
"""

from __future__ import annotations

from typing import Any, Dict

FLINK_SQL = (
    "INSERT INTO service_instances_full\n"
    "SELECT i.id, i.status, v.location\n"
    "FROM service_instances i\n"
    "JOIN inventory v ON i.id = v.instance_id\n"
)


def flink_build(platform: str = "kubernetes", *, engine: str = "flink") -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": "reconcile",
        "engine": engine,
        "pattern": "embedded-logic",
        "properties": {
            "language": "flink_sql",
            "sql": FLINK_SQL,
            "parameters": {"parallelism": 2},
        },
    }
    if platform:
        build["execution"] = {"runtime": {"platform": platform}}
    return build


def flink_contract(fluid_version: str = "0.7.6", engine: str = "flink") -> Dict[str, Any]:
    return {
        "fluidVersion": fluid_version,
        "kind": "DataProduct",
        "id": "network.service-instances-full",
        "name": "Service instances (reconciled)",
        "metadata": {"owner": {"team": "data-engineering"}},
        "consumes": [
            {
                "productId": "network.service-instances",
                "exposeId": "service_instances",
                "versionConstraint": "^1.0",
            }
        ],
        "builds": [flink_build(engine=engine)],
        "exposes": [
            {
                "exposeId": "service_instances_full",
                "kind": "topic",
                "binding": {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"topic": "network.service-instances-full"},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string"},
                        {"name": "status", "type": "string"},
                        {"name": "location", "type": "string"},
                    ]
                },
            }
        ],
    }
