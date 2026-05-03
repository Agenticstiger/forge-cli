# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Pinned upstream Helm chart references.

Each engine has at most one well-known managed-mode chart. We pin the
reference here (repo + name + version) so the runtime cannot drift to an
unverified version. Bumps are reviewed via Renovate or equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ChartRef:
    repo: str
    name: str
    version: str
    namespace: str  # default install namespace
    description: str = ""


CHART_REGISTRY: Dict[str, ChartRef] = {
    "airbyte": ChartRef(
        repo="https://airbytehq.github.io/helm-charts",
        name="airbyte",
        version="0.520.0",
        namespace="forge-airbyte",
        description="Airbyte OSS — community edition.",
    ),
    "kafka-connect": ChartRef(
        repo="https://strimzi.io/charts/",
        name="strimzi-kafka-operator",
        version="0.41.0",
        namespace="forge-kafka",
        description="Strimzi Kafka operator (provides KafkaConnect CRD).",
    ),
    "debezium": ChartRef(
        repo="https://strimzi.io/charts/",
        name="strimzi-kafka-operator",
        version="0.41.0",
        namespace="forge-debezium",
        description="Strimzi-managed KafkaConnect with Debezium connectors.",
    ),
    "meltano": ChartRef(
        repo="https://meltano.github.io/charts",
        name="meltano",
        version="0.6.0",
        namespace="forge-meltano",
        description="Meltano UI + scheduler (optional managed mode).",
    ),
}


def register_chart(engine: str, chart: ChartRef) -> None:
    CHART_REGISTRY[engine] = chart


def get_chart(engine: str) -> Optional[ChartRef]:
    return CHART_REGISTRY.get(engine)
