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
Docker executor for Local Provider.

Enables container-based execution for:
- Isolated Python environments
- Specific tool versions (dbt, spark, etc.)
- Multi-container workflows
- Local Airflow development
"""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


class DockerExecutor:
    """Execute operations in Docker containers."""

    def __init__(
        self, logger: Optional[logging.Logger] = None, default_image: str = "python:3.9-slim"
    ):
        """
        Initialize Docker executor.

        Args:
            logger: Optional logger for execution events
            default_image: Default Docker image to use
        """
        self.logger = logger or logging.getLogger(__name__)
        self.default_image = default_image
        self._check_docker_available()

    def _check_docker_available(self) -> bool:
        """Check if Docker is available on the system."""
        try:
            result = subprocess.run(
                ["docker", "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                self.logger.info(f"Docker available: {result.stdout.strip()}")
                return True
            else:
                self.logger.warning("Docker not available")
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            self.logger.warning(f"Docker check failed: {e}")
            return False

    def run_python_script(
        self,
        script: str,
        image: Optional[str] = None,
        volumes: Optional[Dict[str, str]] = None,
        env_vars: Optional[Dict[str, str]] = None,
        requirements: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Run a Python script in a Docker container.

        Args:
            script: Python script content
            image: Docker image (default: python:3.9-slim)
            volumes: Volume mounts {host_path: container_path}
            env_vars: Environment variables
            requirements: Python packages to install

        Returns:
            Execution result with stdout, stderr, return code
        """
        image = image or self.default_image

        # Create temporary directory for script and requirements
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Write script to file
            script_file = tmpdir_path / "script.py"
            script_file.write_text(script, encoding="utf-8")

            # Write requirements if provided
            if requirements:
                req_file = tmpdir_path / "requirements.txt"
                req_file.write_text("\n".join(requirements), encoding="utf-8")

            # Build docker run command
            cmd = ["docker", "run", "--rm"]

            # Add volume mounts
            cmd.extend(["-v", f"{tmpdir_path}:/workspace"])
            if volumes:
                for host_path, container_path in volumes.items():
                    cmd.extend(["-v", f"{host_path}:{container_path}"])

            # Add environment variables
            if env_vars:
                for key, value in env_vars.items():
                    cmd.extend(["-e", f"{key}={value}"])

            # Set working directory
            cmd.extend(["-w", "/workspace"])

            # Specify image
            cmd.append(image)

            # Install requirements and run script
            if requirements:
                cmd.extend(
                    ["sh", "-c", "pip install --quiet -r requirements.txt && python script.py"]
                )
            else:
                cmd.extend(["python", "script.py"])

            # Execute
            self.logger.info(f"Running Python script in Docker: {image}")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300  # 5 minute timeout
            )

            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "success": result.returncode == 0,
            }

    def run_sql_with_duckdb(
        self, sql: str, data_dir: Optional[str] = None, output_format: str = "csv"
    ) -> Dict[str, Any]:
        """
        Run SQL query using DuckDB in a container.

        Args:
            sql: SQL query to execute
            data_dir: Directory containing data files
            output_format: Output format (csv, parquet, json)

        Returns:
            Execution result
        """
        script = f"""
import duckdb
import sys

con = duckdb.connect(':memory:')

# Execute SQL
try:
    result = con.execute('''{sql}''').fetchdf()
    
    # Output result
    if '{output_format}' == 'csv':
        cprint(result.to_csv(index=False))
    elif '{output_format}' == 'json':
        cprint(result.to_json(orient='records'))
    else:
        cprint(result)
        
except Exception as e:
    console_error(f"Error: {{e}}")
    sys.exit(1)
"""

        volumes = {data_dir: "/data"} if data_dir else None

        return self.run_python_script(
            script=script, requirements=["duckdb", "pandas"], volumes=volumes
        )

    def run_dbt_command(
        self, dbt_command: str, project_dir: str, profiles_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Run dbt command in a container.

        Args:
            dbt_command: dbt command (e.g., "dbt run", "dbt test")
            project_dir: Path to dbt project directory
            profiles_dir: Path to profiles directory

        Returns:
            Execution result
        """
        cmd = ["docker", "run", "--rm"]

        # Mount project directory
        cmd.extend(["-v", f"{project_dir}:/dbt"])

        # Mount profiles directory if provided
        if profiles_dir:
            cmd.extend(["-v", f"{profiles_dir}:/root/.dbt"])

        # Set working directory
        cmd.extend(["-w", "/dbt"])

        # Use dbt image
        cmd.append("ghcr.io/dbt-labs/dbt-core:latest")

        # Add dbt command
        cmd.extend(dbt_command.split())

        self.logger.info(f"Running dbt command: {dbt_command}")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600  # 10 minute timeout
        )

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }

    def start_airflow_local(
        self, compose_file: str = "docker-compose.airflow.yml", detached: bool = True
    ) -> Dict[str, Any]:
        """
        Start local Airflow using docker-compose.

        Args:
            compose_file: Path to docker-compose file
            detached: Run in detached mode

        Returns:
            Execution result
        """
        cmd = ["docker-compose", "-f", compose_file, "up"]
        if detached:
            cmd.append("-d")

        self.logger.info("Starting Airflow with docker-compose")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode == 0:
            self.logger.info("Airflow started successfully")
            if detached:
                self.logger.info("Access Airflow UI at http://localhost:8080")

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }

    def stop_airflow_local(
        self, compose_file: str = "docker-compose.airflow.yml"
    ) -> Dict[str, Any]:
        """Stop local Airflow."""
        cmd = ["docker-compose", "-f", compose_file, "down"]

        self.logger.info("Stopping Airflow")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }

    def build_custom_image(
        self, dockerfile_path: str, image_name: str, build_args: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Build a custom Docker image.

        Args:
            dockerfile_path: Path to Dockerfile
            image_name: Name for the built image
            build_args: Build arguments

        Returns:
            Build result
        """
        cmd = ["docker", "build", "-t", image_name, "-f", dockerfile_path]

        if build_args:
            for key, value in build_args.items():
                cmd.extend(["--build-arg", f"{key}={value}"])

        cmd.append(".")

        self.logger.info(f"Building Docker image: {image_name}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }


def create_airflow_compose_file(output_path: str = "docker-compose.airflow.yml"):
    """
    Create a docker-compose file for local Airflow development.

    Args:
        output_path: Where to write the compose file
    """
    compose_content = """# Docker Compose for Local Airflow Development
# Generated by FLUID Local Provider
version: '3.8'

x-airflow-common:
  &airflow-common
  image: apache/airflow:2.7.3-python3.9
  environment:
    &airflow-common-env
    AIRFLOW__CORE__EXECUTOR: LocalExecutor
    AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql+psycopg2://airflow:airflow@postgres/airflow
    AIRFLOW__CORE__FERNET_KEY: ''
    AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION: 'true'
    AIRFLOW__CORE__LOAD_EXAMPLES: 'false'
    AIRFLOW__API__AUTH_BACKENDS: 'airflow.api.auth.backend.basic_auth'
    _PIP_ADDITIONAL_REQUIREMENTS: ${_PIP_ADDITIONAL_REQUIREMENTS:-}
  volumes:
    - ./dags:/opt/airflow/dags
    - ./logs:/opt/airflow/logs
    - ./plugins:/opt/airflow/plugins
    - ./data:/opt/airflow/data
  user: "${AIRFLOW_UID:-50000}:0"
  depends_on:
    &airflow-common-depends-on
    postgres:
      condition: service_healthy

services:
  postgres:
    image: postgres:13
    environment:
      POSTGRES_USER: airflow
      POSTGRES_PASSWORD: airflow
      POSTGRES_DB: airflow
    volumes:
      - postgres-db-volume:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "airflow"]
      interval: 5s
      retries: 5
    ports:
      - "5432:5432"

  airflow-webserver:
    <<: *airflow-common
    command: webserver
    ports:
      - "8080:8080"
    healthcheck:
      test: ["CMD", "curl", "--fail", "http://localhost:8080/health"]
      interval: 10s
      timeout: 10s
      retries: 5
    restart: always
    depends_on:
      <<: *airflow-common-depends-on
      airflow-init:
        condition: service_completed_successfully

  airflow-scheduler:
    <<: *airflow-common
    command: scheduler
    healthcheck:
      test: ["CMD-SHELL", 'airflow jobs check --job-type SchedulerJob --hostname "$${HOSTNAME}"']
      interval: 10s
      timeout: 10s
      retries: 5
    restart: always
    depends_on:
      <<: *airflow-common-depends-on
      airflow-init:
        condition: service_completed_successfully

  airflow-init:
    <<: *airflow-common
    entrypoint: /bin/bash
    command:
      - -c
      - |
        mkdir -p /sources/logs /sources/dags /sources/plugins /sources/data
        chown -R "${AIRFLOW_UID:-50000}:0" /sources/{logs,dags,plugins,data}
        exec /entrypoint airflow version
    environment:
      <<: *airflow-common-env
      _AIRFLOW_DB_UPGRADE: 'true'
      _AIRFLOW_WWW_USER_CREATE: 'true'
      _AIRFLOW_WWW_USER_USERNAME: ${_AIRFLOW_WWW_USER_USERNAME:-airflow}
      _AIRFLOW_WWW_USER_PASSWORD: ${_AIRFLOW_WWW_USER_PASSWORD:-airflow}
    user: "0:0"
    volumes:
      - .:/sources

volumes:
  postgres-db-volume:

# Usage:
# docker-compose -f docker-compose.airflow.yml up -d
# Access UI at http://localhost:8080 (airflow/airflow)
# docker-compose -f docker-compose.airflow.yml down
"""

    Path(output_path).write_text(compose_content, encoding="utf-8")
    return output_path
