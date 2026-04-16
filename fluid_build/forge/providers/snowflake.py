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
Snowflake Provider for FLUID Forge
Snowflake Data Cloud configuration and deployment
"""

from typing import Any, Dict, List

from ..core.interfaces import GenerationContext, InfrastructureProvider, ValidationResult


class SnowflakeProvider(InfrastructureProvider):
    """Snowflake Data Cloud provider"""

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "Snowflake Data Cloud",
            "description": "Deploy to Snowflake with native data warehouse capabilities",
            "supported_services": ["snowflake", "snowpipe", "streams", "tasks"],
            "complexity": "intermediate",
            "scalability": "high",
            "use_cases": ["Data warehousing", "Analytics", "Data sharing", "ML"],
        }

    def configure_interactive(self, context: GenerationContext) -> Dict[str, Any]:
        from rich.prompt import Prompt

        config = {}
        config["account"] = Prompt.ask("Snowflake Account")
        config["user"] = Prompt.ask("Snowflake User")
        config["database"] = Prompt.ask(
            "Database", default=context.project_config.get("name", "DATAPRODUCT")
        )
        config["warehouse"] = Prompt.ask("Warehouse", default="COMPUTE_WH")
        config["schema"] = Prompt.ask("Schema", default="PUBLIC")

        return config

    def generate_config(self, context: GenerationContext) -> Dict[str, Any]:
        return {
            "config/snowflake/setup.sql": "# Snowflake setup scripts",
            "config/snowflake/dbt_profile.yml": "# dbt profile for Snowflake",
            ".github/workflows/deploy-snowflake.yml": "# Snowflake deployment workflow",
        }

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        required_fields = ["account", "user", "database", "warehouse"]
        for field in required_fields:
            if not config.get(field):
                errors.append(f"Snowflake {field} is required")
        return len(errors) == 0, errors

    def get_required_tools(self) -> List[str]:
        return ["snowsql", "dbt"]

    def get_environment_variables(self) -> List[str]:
        return ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_ROLE"]

    def check_prerequisites(self) -> ValidationResult:
        import shutil

        warnings = []
        if not shutil.which("snowsql"):
            warnings.append("SnowSQL CLI not found - recommended for deployment")
        return True, [f"Warning: {w}" for w in warnings]
