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
Configuration Generator for FLUID Forge

Generates configuration files for projects
"""

from typing import Dict

import yaml

from ..core.interfaces import GenerationContext, Generator, ValidationResult


class ConfigGenerator(Generator):
    """Generator for configuration files"""

    def generate(self, context: GenerationContext) -> Dict[str, str]:
        """Generate configuration files"""
        project_config = context.project_config
        provider = project_config.get("provider", "local")

        files = {}

        # Generate requirements.txt
        files["requirements.txt"] = self._generate_requirements(context)

        # Generate .env.example
        files[".env.example"] = self._generate_env_example(context)

        # Generate .gitignore
        files[".gitignore"] = self._generate_gitignore(context)

        # Generate provider-specific config
        if provider == "gcp":
            files["config/gcp.yaml"] = self._generate_gcp_config(context)
        elif provider == "aws":
            files["config/aws.yaml"] = self._generate_aws_config(context)
        elif provider == "snowflake":
            files["config/snowflake.yaml"] = self._generate_snowflake_config(context)

        # Generate development scripts
        files["scripts/setup-dev.sh"] = self._generate_setup_script(context)
        files["scripts/run-local.sh"] = self._generate_run_script(context)
        files["scripts/run-tests.sh"] = self._generate_test_script(context)

        for relative_path, content in (project_config.get("copilot_generated_files") or {}).items():
            files[relative_path] = content

        return files

    def _generate_requirements(self, context: GenerationContext) -> str:
        """Generate requirements.txt"""
        template_name = context.project_config.get("template", "starter")

        base_requirements = ["pyyaml>=6.0", "click>=8.0", "rich>=13.0", "python-dotenv>=1.0"]

        if template_name == "analytics":
            base_requirements.extend(
                ["pandas>=2.0", "numpy>=1.24", "matplotlib>=3.7", "seaborn>=0.12", "jupyter>=1.0"]
            )
        elif template_name == "ml_pipeline":
            base_requirements.extend(
                [
                    "scikit-learn>=1.3",
                    "pandas>=2.0",
                    "numpy>=1.24",
                    "joblib>=1.3",
                    "matplotlib>=3.7",
                ]
            )
        elif template_name == "etl_pipeline":
            base_requirements.extend(
                [
                    "pandas>=2.0",
                    "sqlalchemy>=2.0",
                    "psycopg2-binary>=2.9",
                    "great-expectations>=0.17",
                ]
            )
        elif template_name == "streaming":
            base_requirements.extend(
                ["kafka-python>=2.0", "confluent-kafka>=2.0", "avro-python3>=1.10"]
            )

        provider = context.project_config.get("provider", "local")
        if provider == "gcp":
            base_requirements.extend(["google-cloud-bigquery>=3.11", "google-cloud-storage>=2.10"])
        elif provider == "aws":
            base_requirements.extend(["boto3>=1.28", "awscli>=1.29"])
        elif provider == "snowflake":
            base_requirements.extend(
                ["snowflake-connector-python>=4.4", "snowflake-sqlalchemy>=1.9"]
            )

        return "\n".join(sorted(set(base_requirements))) + "\n"

    def _generate_env_example(self, context: GenerationContext) -> str:
        """Generate .env.example"""
        provider = context.project_config.get("provider", "local")

        env_vars = [
            "# Environment Configuration",
            "ENVIRONMENT=development",
            "LOG_LEVEL=INFO",
            "",
            "# Project Configuration",
            f"PROJECT_NAME={context.project_config.get('name', 'fluid-project')}",
            f"PROJECT_DOMAIN={context.project_config.get('domain', 'data')}",
            f"PROJECT_OWNER={context.project_config.get('owner', 'team')}",
            "",
        ]

        if provider == "gcp":
            env_vars.extend(
                [
                    "# GCP Configuration",
                    "GOOGLE_CLOUD_PROJECT=your-project-id",
                    "GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json",
                    "GCP_DATASET_LOCATION=us-central1",
                    "",
                ]
            )
        elif provider == "aws":
            env_vars.extend(
                [
                    "# AWS Configuration",
                    "AWS_REGION=us-east-1",
                    "AWS_PROFILE=default",
                    "AWS_S3_BUCKET=your-bucket-name",
                    "",
                ]
            )
        elif provider == "snowflake":
            env_vars.extend(
                [
                    "# Snowflake Configuration",
                    "SNOWFLAKE_ACCOUNT=your-account",
                    "SNOWFLAKE_USER=your-username",
                    "SNOWFLAKE_PASSWORD=your-password",
                    "SNOWFLAKE_WAREHOUSE=your-warehouse",
                    "SNOWFLAKE_DATABASE=your-database",
                    "SNOWFLAKE_SCHEMA=your-schema",
                    "",
                ]
            )

        return "\n".join(env_vars)

    def _generate_gitignore(self, context: GenerationContext) -> str:
        """Generate .gitignore"""
        return """# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class

# C extensions
*.so

# Distribution / packaging
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
share/python-wheels/
*.egg-info/
.installed.cfg
*.egg
MANIFEST

# PyInstaller
*.manifest
*.spec

# Installer logs
pip-log.txt
pip-delete-this-directory.txt

# Unit test / coverage reports
htmlcov/
.tox/
.nox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
*.py,cover
.hypothesis/
.pytest_cache/
cover/

# Virtual environments
.env
.venv
env/
venv/
ENV/
env.bak/
venv.bak/

# IDEs
.vscode/
.idea/
*.swp
*.swo
*~

# OS generated files
.DS_Store
.DS_Store?
._*
.Spotlight-V100
.Trashes
ehthumbs.db
Thumbs.db

# Project specific
logs/
runtime/
*.log
.secrets/
credentials/
"""

    def _generate_gcp_config(self, context: GenerationContext) -> str:
        """Generate GCP configuration"""
        config = {
            "project_id": "${GOOGLE_CLOUD_PROJECT}",
            "location": "${GCP_DATASET_LOCATION:-us-central1}",
            "bigquery": {
                "dataset": context.project_config.get("name", "fluid_project").replace("-", "_"),
                "location": "${GCP_DATASET_LOCATION:-us-central1}",
            },
            "storage": {"bucket": "${GCS_BUCKET:-your-bucket}", "prefix": "data/"},
            "dataflow": {
                "region": "${GCP_DATAFLOW_REGION:-us-central1}",
                "temp_location": "gs://${GCS_BUCKET}/temp/",
                "staging_location": "gs://${GCS_BUCKET}/staging/",
            },
        }
        return yaml.dump(config, default_flow_style=False)

    def _generate_aws_config(self, context: GenerationContext) -> str:
        """Generate AWS configuration"""
        config = {
            "region": "${AWS_REGION:-us-east-1}",
            "s3": {"bucket": "${AWS_S3_BUCKET}", "prefix": "data/"},
            "redshift": {
                "cluster_identifier": "${REDSHIFT_CLUSTER}",
                "database": "${REDSHIFT_DATABASE}",
                "user": "${REDSHIFT_USER}",
            },
            "glue": {
                "database": context.project_config.get("name", "fluid_project").replace("-", "_"),
                "role_arn": "${GLUE_ROLE_ARN}",
            },
        }
        return yaml.dump(config, default_flow_style=False)

    def _generate_snowflake_config(self, context: GenerationContext) -> str:
        """Generate Snowflake configuration"""
        config = {
            "account": "${SNOWFLAKE_ACCOUNT}",
            "warehouse": "${SNOWFLAKE_WAREHOUSE}",
            "database": "${SNOWFLAKE_DATABASE}",
            "schema": "${SNOWFLAKE_SCHEMA}",
            "user": "${SNOWFLAKE_USER}",
            "password": "${SNOWFLAKE_PASSWORD}",
            "role": "${SNOWFLAKE_ROLE:-PUBLIC}",
        }
        return yaml.dump(config, default_flow_style=False)

    def _generate_setup_script(self, context: GenerationContext) -> str:
        """Generate development setup script"""
        return """#!/bin/bash
set -e

echo "Setting up development environment..."

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env from example..."
    cp .env.example .env
    echo "Please update .env with your configuration"
fi

echo "Development environment setup complete!"
echo "Run 'source venv/bin/activate' to activate the environment"
"""

    def _generate_run_script(self, context: GenerationContext) -> str:
        """Generate local run script"""
        template_name = context.project_config.get("template", "starter")

        if template_name == "analytics":
            return """#!/bin/bash
set -e

echo "Starting analytics pipeline..."
source venv/bin/activate
python -m src.analytics.main
"""
        elif template_name == "ml_pipeline":
            return """#!/bin/bash
set -e

echo "Starting ML pipeline..."
source venv/bin/activate
python -m src.ml.train
"""
        elif template_name == "etl_pipeline":
            return """#!/bin/bash
set -e

echo "Starting ETL pipeline..."
source venv/bin/activate
python -m src.etl.main
"""
        elif template_name == "streaming":
            return """#!/bin/bash
set -e

echo "Starting streaming pipeline..."
source venv/bin/activate
python -m src.streaming.consumer
"""
        else:
            return """#!/bin/bash
set -e

echo "Starting application..."
source venv/bin/activate
python -m src.main
"""

    def _generate_test_script(self, context: GenerationContext) -> str:
        """Generate test script"""
        return """#!/bin/bash
set -e

echo "Running tests..."
source venv/bin/activate

# Run pytest with coverage
python -m pytest tests/ -v --cov=src --cov-report=html --cov-report=term

echo "Tests complete! Coverage report available in htmlcov/"
"""

    def validate_context(self, context: GenerationContext) -> ValidationResult:
        """Validate that context has required data"""
        if not context.project_config.get("name"):
            return False, ["Project name is required for config generation"]
        return True, []
