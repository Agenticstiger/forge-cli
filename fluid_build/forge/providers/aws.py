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
AWS Provider for FLUID Forge
Amazon Web Services configuration and deployment
"""

from typing import Any, Dict, List

from ..core.interfaces import GenerationContext, InfrastructureProvider, ValidationResult


class AWSProvider(InfrastructureProvider):
    """Amazon Web Services provider"""

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "Amazon Web Services",
            "description": "Deploy to AWS with Redshift, Glue, and Lambda",
            "supported_services": ["redshift", "glue", "lambda", "emr", "sagemaker"],
            "complexity": "intermediate",
            "scalability": "high",
            "use_cases": ["Analytics", "ML", "ETL", "Data lakes"],
        }

    def configure_interactive(self, context: GenerationContext) -> Dict[str, Any]:
        from rich.prompt import Confirm, Prompt

        config = {}
        config["region"] = Prompt.ask("AWS Region", default="us-east-1")
        config["account_id"] = Prompt.ask("AWS Account ID")

        use_glue = Confirm.ask("Use AWS Glue for ETL?", default=True)
        config["use_glue"] = use_glue

        return config

    def generate_config(self, context: GenerationContext) -> Dict[str, Any]:
        return {
            "config/aws/cloudformation.yml": "# AWS CloudFormation template",
            "config/aws/glue-jobs.py": "# AWS Glue job definitions",
            ".github/workflows/deploy-aws.yml": "# AWS deployment workflow",
        }

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("region"):
            errors.append("AWS Region is required")
        return len(errors) == 0, errors

    def get_required_tools(self) -> List[str]:
        return ["aws", "terraform"]

    def get_environment_variables(self) -> List[str]:
        return ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"]

    def check_prerequisites(self) -> ValidationResult:
        import shutil

        errors = []
        if not shutil.which("aws"):
            errors.append("AWS CLI not found")
        return len(errors) == 0, errors
