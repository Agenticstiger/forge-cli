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

"""
Streaming Template for FLUID Forge
Real-time data processing and streaming analytics with event-driven architecture
"""

from typing import Any, Dict

from ..core.interfaces import (
    ComplexityLevel,
    GenerationContext,
    ProjectTemplate,
    TemplateMetadata,
    ValidationResult,
)


class StreamingTemplate(ProjectTemplate):
    """Streaming template for real-time data processing"""

    def get_metadata(self) -> TemplateMetadata:
        return TemplateMetadata(
            name="Streaming Data Product",
            description="Real-time data processing and streaming analytics with event-driven architecture",
            complexity=ComplexityLevel.ADVANCED,
            provider_support=["gcp", "aws", "azure", "kafka", "confluent"],
            use_cases=[
                "Real-time analytics and dashboards",
                "Event-driven microservices",
                "IoT data processing and monitoring",
                "Fraud detection and alerting",
                "Live recommendation systems",
                "Real-time personalization",
            ],
            technologies=["Apache Kafka", "Apache Beam", "Dataflow", "Pub/Sub", "Kinesis", "Flink"],
            estimated_time="25-35 minutes",
            tags=["streaming", "real-time", "events", "analytics"],
            category="streaming",
            version="1.0.0",
        )

    def generate_structure(self, context: GenerationContext) -> Dict[str, Any]:
        return {
            "streams/": {
                "ingestion/": {"kafka/": {}, "pubsub/": {}, "kinesis/": {}},
                "processing/": {"windows/": {}, "aggregations/": {}, "enrichment/": {}},
                "windowing/": {"tumbling/": {}, "sliding/": {}, "session/": {}},
                "sinks/": {"storage/": {}, "alerts/": {}, "downstream/": {}},
            },
            "schemas/": {"avro/": {}, "protobuf/": {}, "json/": {}},
            "config/": {"topics/": {}, "schemas/": {}, "environments/": {}},
            "tests/": {"unit/": {}, "integration/": {}, "load/": {}, "chaos/": {}},
            "docs/": {"architecture/": {}, "event_catalog/": {}, "monitoring/": {}},
            "scripts/": {"deployment/": {}, "monitoring/": {}, "scaling/": {}},
        }

    def generate_contract(self, context: GenerationContext) -> Dict[str, Any]:
        project_config = context.project_config
        project_name = project_config.get("name", "streaming-pipeline")
        description = project_config.get("description", "Streaming data product")
        domain = project_config.get("domain", "streaming")
        owner = project_config.get("owner", "platform-team")
        provider = project_config.get("provider", "gcp")

        return {
            "fluidVersion": project_config.get("fluid_version", "0.5.7"),
            "kind": "DataProduct",
            "id": f"{project_name.replace('-', '_')}_streaming",
            "name": f"{project_name} Streaming",
            "description": description,
            "domain": domain,
            "metadata": {
                "layer": "Bronze",
                "owner": {"team": owner, "email": f"{owner}@company.com"},
                "status": "Development",
                "tags": ["streaming", "real-time", "events", "analytics"],
                "created": context.creation_time,
                "template": "streaming",
                "forge_version": context.forge_version,
            },
            "consumes": [
                {
                    "id": "event_stream",
                    "ref": "urn:fluid:events:v1",
                    "description": "Real-time event stream for processing",
                }
            ],
            "builds": [  # Changed from 'build' to 'builds' array
                {
                    "transformation": {
                        "pattern": "streaming",
                        "engine": "beam",
                        "properties": {
                            "pipeline": "src/streaming/event_processor.py",
                            "windowing": {"type": "tumbling", "size": "5m"},
                            "processing_guarantee": "exactly_once",
                        },
                    },
                    "execution": {
                        "trigger": {"type": "continuous", "mode": "streaming"},
                        "runtime": {
                            "platform": provider,
                            "resources": {"cpu": "2", "memory": "4GB", "parallelism": 4},
                        },
                    },
                }
            ],  # Close builds array
            "exposes": [
                {
                    "exposeId": "real_time_metrics",  # Changed from 'id'
                    "kind": "stream",  # Changed from 'type'
                    "description": "Real-time aggregated metrics stream",
                    "binding": {  # Changed from 'location'
                        "format": "stream",
                        "topic": "metrics-output",  # Flattened from properties
                        "sink": "monitoring_dashboard",
                    },
                    "schema": [
                        {
                            "name": "event_id",
                            "type": "string",
                            "description": "Unique event identifier",
                            "nullable": False,
                        },
                        {
                            "name": "metric_value",
                            "type": "float",
                            "description": "Aggregated metric value",
                            "nullable": False,
                        },
                        {
                            "name": "window_start",
                            "type": "timestamp",
                            "description": "Window start timestamp",
                            "nullable": False,
                        },
                        {
                            "name": "window_end",
                            "type": "timestamp",
                            "description": "Window end timestamp",
                            "nullable": False,
                        },
                    ],
                    "quality": [
                        {
                            "name": "event_ordering",
                            "rule": "window_start <= window_end",
                            "onFailure": {"action": "reject_event"},
                        }
                    ],
                }
            ],
            "slo": {"freshnessMinutes": 1, "availabilityPct": 99.9},
            "streaming_config": {
                "window_size": "5m",
                "processing_guarantee": "exactly_once",
                "checkpoint_interval": "30s",
                "parallelism": 4,
            },
        }

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("name"):
            errors.append("Project name is required")
        provider = config.get("provider")
        if provider == "local":
            errors.append("Streaming templates require cloud providers (gcp, aws, azure)")
        return len(errors) == 0, errors
