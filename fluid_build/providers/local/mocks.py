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
Cloud service mocks for Local Provider.

Provides local mocks for:
- AWS services (S3, Glue, Athena via localstack)
- GCP services (GCS, BigQuery via fake-gcs-server + DuckDB)
- Snowflake (via DuckDB with Snowflake SQL compatibility)
"""

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class LocalStackMock:
    """Mock AWS services using LocalStack."""

    def __init__(
        self, logger: Optional[logging.Logger] = None, services: Optional[List[str]] = None
    ):
        """
        Initialize LocalStack mock.

        Args:
            logger: Optional logger
            services: AWS services to enable (default: s3, glue, athena)
        """
        self.logger = logger or logging.getLogger(__name__)
        self.services = services or ["s3", "glue", "athena", "dynamodb"]
        self.endpoint_url = "http://localhost:4566"

    def start(self) -> Dict[str, Any]:
        """Start LocalStack container."""
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            "fluid-localstack",
            "-p",
            "4566:4566",
            "-e",
            f"SERVICES={','.join(self.services)}",
            "-e",
            "DEBUG=1",
            "localstack/localstack:latest",
        ]

        self.logger.info(f"Starting LocalStack with services: {self.services}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            self.logger.info(f"LocalStack started at {self.endpoint_url}")

        return {
            "success": result.returncode == 0,
            "endpoint_url": self.endpoint_url,
            "services": self.services,
        }

    def stop(self) -> Dict[str, Any]:
        """Stop LocalStack container."""
        cmd = ["docker", "stop", "fluid-localstack"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Remove container
        subprocess.run(["docker", "rm", "fluid-localstack"], capture_output=True)

        return {"success": result.returncode == 0}

    def create_s3_bucket(self, bucket_name: str) -> Dict[str, Any]:
        """Create S3 bucket in LocalStack."""
        cmd = [
            "aws",
            "s3",
            "mb",
            f"s3://{bucket_name}",
            "--endpoint-url",
            self.endpoint_url,
            "--region",
            "us-east-1",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        return {
            "success": result.returncode == 0,
            "bucket": bucket_name,
            "endpoint": self.endpoint_url,
        }

    def get_boto3_config(self) -> Dict[str, Any]:
        """Get boto3 configuration for LocalStack."""
        return {
            "endpoint_url": self.endpoint_url,
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
            "region_name": "us-east-1",
        }


class FakeGCSMock:
    """Mock Google Cloud Storage using fake-gcs-server."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize fake-gcs-server mock."""
        self.logger = logger or logging.getLogger(__name__)
        self.endpoint_url = "http://localhost:4443"
        self.data_dir = Path.home() / ".fluid" / "fake-gcs-data"

    def start(self) -> Dict[str, Any]:
        """Start fake-gcs-server container."""
        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            "fluid-fake-gcs",
            "-p",
            "4443:4443",
            "-v",
            f"{self.data_dir}:/data",
            "fsouza/fake-gcs-server",
            "-scheme",
            "http",
            "-public-host",
            "localhost:4443",
        ]

        self.logger.info(f"Starting fake-gcs-server at {self.endpoint_url}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        return {"success": result.returncode == 0, "endpoint_url": self.endpoint_url}

    def stop(self) -> Dict[str, Any]:
        """Stop fake-gcs-server container."""
        cmd = ["docker", "stop", "fluid-fake-gcs"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        subprocess.run(["docker", "rm", "fluid-fake-gcs"], capture_output=True)

        return {"success": result.returncode == 0}

    def create_bucket(self, bucket_name: str) -> Dict[str, Any]:
        """Create GCS bucket in fake server."""
        import requests

        url = f"{self.endpoint_url}/storage/v1/b"
        data = {"name": bucket_name}

        try:
            response = requests.post(url, json=data)
            return {"success": response.status_code in [200, 201], "bucket": bucket_name}
        except Exception as e:
            self.logger.error(f"Failed to create bucket: {e}")
            return {"success": False, "error": str(e)}

    def get_storage_client_config(self) -> Dict[str, str]:
        """Get Google Cloud Storage client configuration."""
        return {"api_endpoint": self.endpoint_url, "project": "test-project"}


class SnowflakeDuckDBMock:
    """Mock Snowflake using DuckDB with Snowflake SQL compatibility."""

    def __init__(self, logger: Optional[logging.Logger] = None, db_path: Optional[str] = None):
        """
        Initialize Snowflake mock.

        Args:
            logger: Optional logger
            db_path: Path to DuckDB database (default: ~/.fluid/snowflake_mock.db)
        """
        self.logger = logger or logging.getLogger(__name__)
        self.db_path = db_path or str(Path.home() / ".fluid" / "snowflake_mock.db")
        self._con = None

    def connect(self) -> Any:
        """Connect to mock Snowflake (DuckDB)."""
        try:
            import duckdb

            self._con = duckdb.connect(self.db_path)

            # Set up Snowflake-like configuration
            self._con.execute("SET TimeZone='UTC'")

            # Create standard Snowflake schemas if they don't exist
            self._con.execute("CREATE SCHEMA IF NOT EXISTS INFORMATION_SCHEMA")
            self._con.execute("CREATE SCHEMA IF NOT EXISTS PUBLIC")

            self.logger.info(f"Connected to Snowflake mock at {self.db_path}")

            return self._con

        except ImportError:
            self.logger.error("DuckDB not installed. Install with: pip install duckdb")
            raise

    def execute(self, sql: str) -> Any:
        """Execute SQL query."""
        if not self._con:
            self.connect()

        # Translate Snowflake-specific SQL to DuckDB
        sql = self._translate_snowflake_sql(sql)

        return self._con.execute(sql)

    def _translate_snowflake_sql(self, sql: str) -> str:
        """
        Translate Snowflake-specific SQL to DuckDB-compatible SQL.

        Handles common Snowflake patterns:
        - VARIANT type → JSON
        - OBJECT_CONSTRUCT → struct
        - FLATTEN → unnest
        """
        # Replace VARIANT with JSON
        sql = sql.replace("VARIANT", "JSON")

        # Replace Snowflake functions with DuckDB equivalents
        replacements = {
            "OBJECT_CONSTRUCT": "struct_pack",
            "ARRAY_CONSTRUCT": "list_value",
            "GET_PATH": "json_extract_path",
            "PARSE_JSON": "json",
        }

        for sf_func, duckdb_func in replacements.items():
            sql = sql.replace(sf_func, duckdb_func)

        return sql

    def create_database(self, database_name: str) -> Dict[str, Any]:
        """Create Snowflake database (schema in DuckDB)."""
        if not self._con:
            self.connect()

        try:
            self._con.execute(f"CREATE SCHEMA IF NOT EXISTS {database_name}")
            return {"success": True, "database": database_name}
        except Exception as e:
            self.logger.error(f"Failed to create database: {e}")
            return {"success": False, "error": str(e)}

    def create_warehouse(self, warehouse_name: str) -> Dict[str, Any]:
        """Create Snowflake warehouse (no-op in DuckDB)."""
        # Warehouses are compute resources in Snowflake
        # In DuckDB, this is a no-op but we log for compatibility
        self.logger.info(f"Warehouse '{warehouse_name}' simulated (no-op in DuckDB)")
        return {"success": True, "warehouse": warehouse_name}

    def close(self):
        """Close database connection."""
        if self._con:
            self._con.close()
            self._con = None


class CloudMockManager:
    """Manage all cloud service mocks."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize cloud mock manager."""
        self.logger = logger or logging.getLogger(__name__)
        self.localstack = LocalStackMock(logger=logger)
        self.fake_gcs = FakeGCSMock(logger=logger)
        self.snowflake = SnowflakeDuckDBMock(logger=logger)

    def start_all(self, services: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Start all requested mock services.

        Args:
            services: List of services to start (aws, gcp, snowflake)
                     Default: all services

        Returns:
            Status of each service
        """
        services = services or ["aws", "gcp", "snowflake"]
        results = {}

        if "aws" in services:
            self.logger.info("Starting AWS mocks (LocalStack)")
            results["aws"] = self.localstack.start()

        if "gcp" in services:
            self.logger.info("Starting GCP mocks (fake-gcs-server)")
            results["gcp"] = self.fake_gcs.start()

        if "snowflake" in services:
            self.logger.info("Starting Snowflake mock (DuckDB)")
            try:
                self.snowflake.connect()
                results["snowflake"] = {"success": True}
            except Exception as e:
                results["snowflake"] = {"success": False, "error": str(e)}

        return results

    def stop_all(self) -> Dict[str, Any]:
        """Stop all mock services."""
        results = {}

        self.logger.info("Stopping AWS mocks")
        results["aws"] = self.localstack.stop()

        self.logger.info("Stopping GCP mocks")
        results["gcp"] = self.fake_gcs.stop()

        self.logger.info("Closing Snowflake mock")
        try:
            self.snowflake.close()
            results["snowflake"] = {"success": True}
        except Exception as e:
            results["snowflake"] = {"success": False, "error": str(e)}

        return results

    def get_connection_info(self) -> Dict[str, Any]:
        """Get connection information for all mocks."""
        return {
            "aws": {
                "endpoint_url": self.localstack.endpoint_url,
                "access_key_id": "test",
                "secret_access_key": "test",
                "region": "us-east-1",
            },
            "gcp": {"endpoint_url": self.fake_gcs.endpoint_url, "project": "test-project"},
            "snowflake": {"database_path": self.snowflake.db_path, "type": "duckdb"},
        }
