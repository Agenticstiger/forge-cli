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
Local Provider for FLUID Forge
Development environment for local testing and prototyping
"""

import shutil
from typing import Any, Dict, List

from ..core.interfaces import GenerationContext, InfrastructureProvider, ValidationResult


class LocalProvider(InfrastructureProvider):
    """Local development provider for testing and prototyping"""

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "Local Development",
            "description": "Local development environment with Docker and Python",
            "supported_services": ["docker", "postgres", "sqlite", "python"],
            "complexity": "beginner",
            "cost": "free",
            "scalability": "low",
            "use_cases": [
                "Development and testing",
                "Learning and experimentation",
                "Small scale prototyping",
                "CI/CD testing",
            ],
        }

    def configure_interactive(self, context: GenerationContext) -> Dict[str, Any]:
        """Interactive configuration for local provider"""
        from rich.prompt import Confirm, Prompt

        config = {}

        # Docker setup
        use_docker = Confirm.ask("Use Docker for local development?", default=True)
        config["use_docker"] = use_docker

        if use_docker:
            docker_compose = Confirm.ask("Generate docker-compose.yml?", default=True)
            config["docker_compose"] = docker_compose

        # Python environment
        setup_venv = Confirm.ask("Set up Python virtual environment?", default=True)
        config["setup_venv"] = setup_venv

        if setup_venv:
            python_version = Prompt.ask("Python version", default="3.9")
            config["python_version"] = python_version

        # Database
        db_type = Prompt.ask(
            "Local database type", choices=["sqlite", "postgres", "none"], default="sqlite"
        )
        config["database"] = db_type

        if db_type == "postgres":
            config["postgres"] = {
                "host": "localhost",
                "port": 5432,
                "database": context.project_config.get("name", "dataproduct"),
                "user": "postgres",
                "password": "password",
            }

        return config

    def generate_config(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate local provider configuration files"""
        config = context.provider_config
        project_name = context.project_config.get("name", "dataproduct")

        files = {}

        # Docker configuration
        if config.get("use_docker", True):
            files.update(self._generate_docker_config(project_name, config))

        # Python configuration
        if config.get("setup_venv", True):
            files.update(self._generate_python_config(config))

        # Environment variables
        files.update(self._generate_env_config(project_name, config))

        # Development scripts
        files.update(self._generate_dev_scripts(project_name, config))

        return files

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        """Validate local provider configuration"""
        errors = []
        warnings = []

        # Check if Docker is available if requested
        if config.get("use_docker", False):
            if not shutil.which("docker"):
                warnings.append(
                    "Docker not found - install Docker Desktop for containerized development"
                )

        # Check Python version
        python_version = config.get("python_version", "3.9")
        try:
            if float(python_version) < 3.8:
                errors.append(f"Python {python_version} is not supported - minimum version is 3.8")
        except ValueError:
            warnings.append(f"Invalid Python version format: {python_version}")

        return len(errors) == 0, errors + [f"Warning: {w}" for w in warnings]

    def get_required_tools(self) -> List[str]:
        """Return list of required tools for local development"""
        return ["python", "pip", "git"]

    def get_environment_variables(self) -> List[str]:
        """Return required environment variables"""
        return ["FLUID_ENV=development", "PYTHONPATH=.", "LOG_LEVEL=INFO"]

    def check_prerequisites(self) -> ValidationResult:
        """Check if local development prerequisites are met"""
        errors = []
        warnings = []

        # Check Python
        if not shutil.which("python3") and not shutil.which("python"):
            errors.append("Python not found - install Python 3.8+")

        # Check Git
        if not shutil.which("git"):
            errors.append("Git not found - install Git for version control")

        # Check Docker (optional)
        if not shutil.which("docker"):
            warnings.append("Docker not found - recommended for containerized development")

        return len(errors) == 0, errors + [f"Warning: {w}" for w in warnings]

    def _generate_docker_config(self, project_name: str, config: Dict[str, Any]) -> Dict[str, str]:
        """Generate Docker configuration files"""
        files = {}

        # Dockerfile
        dockerfile = """FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \\
    build-essential \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV FLUID_ENV=development

# Expose port for development server
EXPOSE 8000

# Default command
CMD ["python", "-m", "src.main"]
"""

        files["Dockerfile"] = dockerfile

        # Docker Compose
        if config.get("docker_compose", True):
            docker_compose = self._generate_docker_compose(project_name, config)
            files["docker-compose.yml"] = docker_compose

        # .dockerignore
        dockerignore = """# Git
.git
.gitignore

# Python
__pycache__
*.pyc
*.pyo
*.pyd
.Python
env
pip-log.txt
pip-delete-this-directory.txt
.tox
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
*.log
.git
.mypy_cache
.pytest_cache
.hypothesis

# Virtual environments
venv/
ENV/

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Data files
data/
*.csv
*.parquet
*.json
"""

        files[".dockerignore"] = dockerignore

        return files

    def _generate_docker_compose(self, project_name: str, config: Dict[str, Any]) -> str:
        """Generate docker-compose.yml"""
        services = []

        # Main application service
        app_service = f"""  {project_name.replace('-', '_')}:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - .:/app
      - /app/__pycache__
    environment:
      - FLUID_ENV=development
      - PYTHONPATH=/app
    depends_on:"""

        # Add database service if configured
        db_type = config.get("database")
        if db_type == "postgres":
            postgres_config = config.get("postgres", {})

            services.append(
                f"""  postgres:
    image: postgres:13
    environment:
      POSTGRES_DB: {postgres_config.get('database', project_name)}
      POSTGRES_USER: {postgres_config.get('user', 'postgres')}
      POSTGRES_PASSWORD: {postgres_config.get('password', 'password')}
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data"""
            )

            app_service += "\n      - postgres"

        services.insert(0, app_service)

        # Add volumes if needed
        volumes = []
        if db_type == "postgres":
            volumes.append("  postgres_data:")

        compose_content = f"""version: '3.8'

services:
{chr(10).join(services)}

"""

        if volumes:
            compose_content += f"""volumes:
{chr(10).join(volumes)}
"""

        return compose_content

    def _generate_python_config(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Generate Python configuration files"""
        files = {}

        # requirements.txt
        requirements = """# Core dependencies
pyyaml>=6.0
click>=8.0
rich>=13.0
requests>=2.28

# Data processing
pandas>=1.5.0
numpy>=1.21.0

# Database
sqlalchemy>=1.4.0

# Testing
pytest>=7.0.0
pytest-cov>=4.0.0

# Development
black>=22.0.0
flake8>=5.0.0
mypy>=1.0.0

# FLUID Build
# fluid-forge>=1.0.0
"""

        files["requirements.txt"] = requirements

        # setup.py for development
        python_version = config.get("python_version", "3.9")
        setup_py = f'''"""Setup configuration for local development"""

from setuptools import setup, find_packages

setup(
    name="fluid-data-product",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">={python_version}",
    install_requires=[
        "pyyaml>=6.0",
        "click>=8.0", 
        "rich>=13.0",
        "pandas>=1.5.0",
        "sqlalchemy>=1.4.0"
    ],
    extras_require={{
        "dev": [
            "pytest>=7.0.0",
            "black>=22.0.0",
            "flake8>=5.0.0",
            "mypy>=1.0.0"
        ]
    }}
)
'''

        files["setup.py"] = setup_py

        # pyproject.toml
        pyproject = f"""[build-system]
requires = ["setuptools>=45", "wheel"]
build-backend = "setuptools.build_meta"

[tool.black]
line-length = 88
target-version = ['py{python_version.replace('.', '')}']

[tool.mypy]
python_version = "{python_version}"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[tool.pytest.ini_options]
minversion = "7.0"
addopts = "-ra -q --cov=src"
testpaths = ["tests"]
"""

        files["pyproject.toml"] = pyproject

        return files

    def _generate_env_config(self, project_name: str, config: Dict[str, Any]) -> Dict[str, str]:
        """Generate environment configuration"""
        files = {}

        # .env.example
        env_vars = [
            "# Environment Configuration",
            "FLUID_ENV=development",
            "LOG_LEVEL=INFO",
            f"PROJECT_NAME={project_name}",
            "",
            "# Database Configuration",
        ]

        db_type = config.get("database")
        if db_type == "postgres":
            postgres_config = config.get("postgres", {})
            env_vars.extend(
                [
                    f"DB_HOST={postgres_config.get('host', 'localhost')}",
                    f"DB_PORT={postgres_config.get('port', 5432)}",
                    f"DB_NAME={postgres_config.get('database', project_name)}",
                    f"DB_USER={postgres_config.get('user', 'postgres')}",
                    f"DB_PASSWORD={postgres_config.get('password', 'password')}",
                ]
            )
        elif db_type == "sqlite":
            env_vars.append(f"DB_PATH=data/{project_name}.db")

        files[".env.example"] = "\n".join(env_vars)

        return files

    def _generate_dev_scripts(self, project_name: str, config: Dict[str, Any]) -> Dict[str, str]:
        """Generate development scripts"""
        files = {}

        # Development setup script
        setup_script = """#!/bin/bash
# Development environment setup script

set -e

echo "Setting up development environment..."

# Check Python version
python3 --version

# Create virtual environment if requested
if [ "$1" = "--venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Install development dependencies
pip install -e ".[dev]"

# Copy environment file
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env file - please update with your configuration"
fi

# Run initial setup
echo "Running initial setup..."
python -c "
import sys
cprint(f'Python version: {sys.version}')
cprint('Setup complete!')
"

echo "Development environment ready!"
echo "Activate virtual environment with: source venv/bin/activate"
"""

        files["scripts/setup-dev.sh"] = setup_script

        # Test runner script
        test_script = """#!/bin/bash
# Test runner script

set -e

echo "Running tests..."

# Run linting
echo "Running linting..."
flake8 src/ tests/

# Run type checking
echo "Running type checking..."
mypy src/

# Run tests
echo "Running unit tests..."
pytest tests/ --cov=src --cov-report=html --cov-report=term

echo "All tests passed!"
"""

        files["scripts/run-tests.sh"] = test_script

        # Local run script
        run_script = f"""#!/bin/bash
# Local development server

set -e

# Load environment variables
if [ -f .env ]; then
    export $(cat .env | xargs)
fi

# Run the application
echo "Starting {project_name}..."
python -m src.main
"""

        files["scripts/run-local.sh"] = run_script

        return files
