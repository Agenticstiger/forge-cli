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
        """Generate a v0.7.3-canonical contract via the shared builder.

        Per-template specifics (product type, build pattern, engine,
        default columns) live in :class:`TemplateSpec` below;
        :func:`build_contract` populates the canonical fluidVersion,
        layer↔productType pair, owner, exposes[].binding, builds[],
        and consumes[] structure so every template stays in lockstep
        with schema changes.
        """
        from ._v073_builder import TemplateSpec, build_contract

        spec = TemplateSpec(
            template_name="streaming",
            product_type="SDP",
            pattern="acquisition",
            engine="kafka-connect",
            properties={"connector": "kafka-source"},
            expose_id="streaming_events",
            expose_kind="topic",
            binding_format="kafka_topic",
            description_suffix="Real-time streaming data product",
        )
        return build_contract(spec=spec, project_config=context.project_config)

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("name"):
            errors.append("Project name is required")
        provider = config.get("provider")
        if provider == "local":
            errors.append("Streaming templates require cloud providers (gcp, aws, azure)")
        return len(errors) == 0, errors
