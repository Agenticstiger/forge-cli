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
ETL Pipeline Template for FLUID Forge
Extract, transform, load data workflows with robust error handling
"""

from typing import Any, Dict

from ..core.interfaces import (
    ComplexityLevel,
    GenerationContext,
    ProjectTemplate,
    TemplateMetadata,
    ValidationResult,
)


class ETLPipelineTemplate(ProjectTemplate):
    """ETL Pipeline template for data integration workflows"""

    def get_metadata(self) -> TemplateMetadata:
        return TemplateMetadata(
            name="ETL Pipeline Data Product",
            description="Extract, transform, load data workflows with robust error handling",
            complexity=ComplexityLevel.INTERMEDIATE,
            provider_support=["local", "gcp", "snowflake", "aws", "azure"],
            use_cases=[
                "Data warehouse loading and updates",
                "Cross-system data synchronization",
                "Data lake ingestion and processing",
                "Legacy system migration",
                "API data integration",
            ],
            technologies=["Apache Airflow", "dbt", "Apache Beam", "Dataflow", "Fivetran"],
            estimated_time="15-25 minutes",
            tags=["etl", "pipeline", "data-integration", "batch"],
            category="integration",
            version="1.0.0",
        )

    def generate_structure(self, context: GenerationContext) -> Dict[str, Any]:
        return {
            "extracts/": {
                "sources/": {"databases/": {}, "apis/": {}, "files/": {}},
                "connectors/": {"sql/": {}, "rest/": {}, "streaming/": {}},
                "schemas/": {},
            },
            "transforms/": {
                "staging/": {"cleaning/": {}, "validation/": {}, "enrichment/": {}},
                "intermediate/": {"joins/": {}, "aggregations/": {}, "calculations/": {}},
                "marts/": {"dimensional/": {}, "fact_tables/": {}, "views/": {}},
            },
            "loads/": {
                "targets/": {"warehouse/": {}, "lake/": {}, "mart/": {}},
                "sinks/": {"batch/": {}, "streaming/": {}, "real_time/": {}},
            },
            "config/": {"environments/": {}, "connections/": {}, "schedules/": {}},
            "tests/": {"unit/": {}, "integration/": {}, "data_quality/": {}, "end_to_end/": {}},
            "docs/": {"lineage/": {}, "data_dictionary/": {}, "processes/": {}},
            "scripts/": {"deployment/": {}, "monitoring/": {}, "maintenance/": {}},
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
            template_name="etl_pipeline",
            product_type="ADP",
            pattern="embedded-logic",
            engine="python",
            properties={"model": "etl_pipeline.main"},
            expose_id="processed_records",
            binding_format="parquet",
            description_suffix="Multi-stage ETL pipeline with quality gates",
        )
        return build_contract(spec=spec, project_config=context.project_config)

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("name"):
            errors.append("Project name is required")
        return len(errors) == 0, errors
