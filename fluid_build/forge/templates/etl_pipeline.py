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
        project_config = context.project_config
        project_name = project_config.get("name", "etl-pipeline")
        description = project_config.get("description", "ETL pipeline data product")
        domain = project_config.get("domain", "integration")
        owner = project_config.get("owner", "data-team")
        provider = project_config.get("provider", "gcp")

        return {
            "fluidVersion": project_config.get("fluid_version", "0.5.7"),
            "kind": "DataProduct",
            "id": f"{project_name.replace('-', '_')}_etl_pipeline",
            "name": f"{project_name} ETL Pipeline",
            "description": description,
            "domain": domain,
            "metadata": {
                "layer": "Silver",
                "owner": {"team": owner, "email": f"{owner}@company.com"},
                "status": "Development",
                "tags": ["etl", "pipeline", "data-integration", "batch"],
                "created": context.creation_time,
                "template": "etl_pipeline",
                "forge_version": context.forge_version,
            },
            "consumes": [
                {
                    "id": "source_system",
                    "ref": "urn:fluid:source_system:v1",
                    "description": "Source system data for ETL processing",
                }
            ],
            "builds": [  # Changed from 'build' to 'builds' array
                {
                    "transformation": {
                        "pattern": "hybrid-reference",
                        "engine": "dbt",
                        "properties": {
                            "model_path": "transforms/",
                            "staging_models": "staging/",
                            "mart_models": "marts/",
                        },
                    },
                    "execution": {
                        "trigger": {"type": "schedule", "cron": "0 2 * * *"},
                        "runtime": {
                            "platform": provider,
                            "resources": {"cpu": "4", "memory": "8GB"},
                        },
                    },
                }
            ],  # Close builds array
            "exposes": [
                {
                    "exposeId": "data_warehouse",  # Changed from 'id'
                    "kind": "table",  # Changed from 'type'
                    "description": "Processed data warehouse tables",
                    "binding": {  # Changed from 'location'
                        "format": "table",
                        "dataset": "warehouse",  # Flattened from properties
                        "table": "fact_orders",
                    },
                    "schema": [
                        {
                            "name": "order_id",
                            "type": "string",
                            "description": "Unique order identifier",
                            "nullable": False,
                        },
                        {
                            "name": "customer_id",
                            "type": "string",
                            "description": "Customer identifier",
                            "nullable": False,
                        },
                        {
                            "name": "order_amount",
                            "type": "decimal",
                            "description": "Total order amount",
                            "nullable": False,
                        },
                        {
                            "name": "order_date",
                            "type": "date",
                            "description": "Date when order was placed",
                            "nullable": False,
                        },
                    ],
                    "quality": [
                        {
                            "name": "primary_key_check",
                            "rule": "order_id IS NOT NULL AND customer_id IS NOT NULL",
                            "onFailure": {"action": "reject_row"},
                        }
                    ],
                }
            ],
            "slo": {"freshnessMinutes": 180, "availabilityPct": 99.5},
        }

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("name"):
            errors.append("Project name is required")
        return len(errors) == 0, errors
