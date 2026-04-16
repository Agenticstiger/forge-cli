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
GCP Provider for FLUID Forge
Google Cloud Platform configuration and deployment
"""

from typing import Any, Dict, List

from ..core.interfaces import GenerationContext, InfrastructureProvider, ValidationResult


class GCPProvider(InfrastructureProvider):
    """Google Cloud Platform provider"""

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "Google Cloud Platform",
            "description": "Deploy to GCP with BigQuery, Dataflow, and Cloud Functions",
            "supported_services": [
                "bigquery",
                "dataflow",
                "cloud-functions",
                "composer",
                "vertex-ai",
            ],
            "complexity": "intermediate",
            "scalability": "high",
            "use_cases": ["Analytics", "ML", "Streaming", "Data warehousing"],
        }

    def configure_interactive(self, context: GenerationContext) -> Dict[str, Any]:
        from rich.prompt import Confirm, Prompt

        config = {}
        config["project_id"] = Prompt.ask("GCP Project ID")
        config["region"] = Prompt.ask("GCP Region", default="us-central1")
        config["dataset"] = Prompt.ask(
            "BigQuery Dataset", default=context.project_config.get("name", "dataproduct")
        )

        use_composer = Confirm.ask("Use Cloud Composer for orchestration?", default=True)
        config["use_composer"] = use_composer

        return config

    def generate_config(self, context: GenerationContext) -> Dict[str, Any]:
        return {
            "config/gcp/terraform.tf": "# GCP Terraform configuration",
            "config/gcp/bigquery.sql": "# BigQuery setup",
            ".github/workflows/deploy-gcp.yml": "# GCP deployment workflow",
        }

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("project_id"):
            errors.append("GCP Project ID is required")
        return len(errors) == 0, errors

    def get_required_tools(self) -> List[str]:
        return ["gcloud", "terraform"]

    def get_environment_variables(self) -> List[str]:
        return ["GOOGLE_APPLICATION_CREDENTIALS", "GCP_PROJECT_ID", "GCP_REGION"]

    def check_prerequisites(self) -> ValidationResult:
        import shutil

        errors = []
        if not shutil.which("gcloud"):
            errors.append("Google Cloud SDK not found")
        return len(errors) == 0, errors
