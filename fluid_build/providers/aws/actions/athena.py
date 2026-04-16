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

# fluid_build/providers/aws/actions/athena.py
"""AWS Athena query execution actions."""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_workgroup(action: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure Athena workgroup exists with configuration."""
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 library not available. Install with: pip install boto3",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    workgroup_name = action.get("name", "primary")
    output_location = action.get("output_location")
    description = action.get("description", "")
    encryption = action.get("encryption", True)
    bytes_scanned_cutoff = action.get("bytes_scanned_cutoff_per_query")
    region = action.get("region", "us-east-1")
    tags = action.get("tags", {})

    try:
        athena = boto3.client("athena", region_name=region)

        changed = False

        # Check if workgroup exists
        try:
            athena.get_work_group(WorkGroup=workgroup_name)
            workgroup_exists = True

            # Skip updates for now (UpdateWorkGroup API available but complex)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "InvalidRequestException":
                workgroup_exists = False
            else:
                raise

        if not workgroup_exists:
            # Create workgroup
            config = {}

            if output_location:
                config["ResultConfigurationUpdates"] = {"OutputLocation": output_location}

                # Add encryption if enabled
                if encryption:
                    config["ResultConfigurationUpdates"]["EncryptionConfiguration"] = {
                        "EncryptionOption": "SSE_S3"
                    }

            if bytes_scanned_cutoff:
                config["BytesScannedCutoffPerQuery"] = bytes_scanned_cutoff

            # Enable query result reuse
            config["ResultConfigurationUpdates"] = config.get("ResultConfigurationUpdates", {})

            create_params = {"Name": workgroup_name}

            if config:
                create_params["Configuration"] = config

            if description:
                create_params["Description"] = description

            if tags:
                tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
                create_params["Tags"] = tag_list

            athena.create_work_group(**create_params)
            changed = True

        return {
            "status": "changed" if changed else "ok",
            "workgroup": workgroup_name,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_table(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create Athena external table from contract schema.

    Generates and executes CREATE EXTERNAL TABLE DDL based on
    the table definition in the action.

    Args:
        action: Table creation action
            Required keys:
                database: Database name
                table: Table name
                columns: Column definitions from schema
                location: S3 location
            Optional keys:
                file_format: File format (default: parquet)
                partition_columns: Partition column definitions
                description: Table description
                region: AWS region

    Returns:
        Action result with DDL and execution details
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 library not available. Install with: pip install boto3",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    database = action.get("database")
    table = action.get("table")
    columns = action.get("columns", [])
    location = action.get("location")
    file_format = action.get("file_format", "parquet")
    partition_columns = action.get("partition_columns")
    description = action.get("description", "")
    region = action.get("region", "us-east-1")
    workgroup = action.get("workgroup", "primary")

    if not database or not table:
        return {
            "status": "error",
            "error": "'database' and 'table' required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not location:
        return {
            "status": "error",
            "error": "'location' (S3 path) is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        from ..util.ddl import generate_athena_ddl

        athena = boto3.client("athena", region_name=region)

        # Generate DDL
        table_properties = {}
        if description:
            table_properties["comment"] = description

        ddl = generate_athena_ddl(
            database=database,
            table=table,
            columns=columns,
            location=location,
            file_format=file_format,
            partition_columns=partition_columns,
            table_properties=table_properties,
        )

        # Execute DDL
        response = athena.start_query_execution(
            QueryString=ddl,
            QueryExecutionContext={"Database": database},
            WorkGroup=workgroup,
        )

        query_execution_id = response["QueryExecutionId"]

        # Wait for completion
        max_wait = 30  # seconds
        wait_interval = 1
        elapsed = 0

        while elapsed < max_wait:
            status_response = athena.get_query_execution(QueryExecutionId=query_execution_id)

            status = status_response["QueryExecution"]["Status"]["State"]

            if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break

            time.sleep(wait_interval)
            elapsed += wait_interval

        if status == "SUCCEEDED":
            return {
                "status": "changed",
                "database": database,
                "table": table,
                "location": location,
                "file_format": file_format,
                "columns_count": len(columns),
                "query_execution_id": query_execution_id,
                "ddl": ddl,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }
        elif status == "FAILED":
            error_reason = status_response["QueryExecution"]["Status"].get(
                "StateChangeReason", "Unknown error"
            )
            return {
                "status": "error",
                "error": f"DDL execution failed: {error_reason}",
                "ddl": ddl,
                "query_execution_id": query_execution_id,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }
        else:
            return {
                "status": "error",
                "error": f"Query timed out or was cancelled (status: {status})",
                "query_execution_id": query_execution_id,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def execute_query(action: Dict[str, Any]) -> Dict[str, Any]:
    """Execute Athena SQL query."""
    start_time = time.time()
    try:
        import boto3

        sql = action.get("sql")
        workgroup = action.get("workgroup", "primary")
        output_location = action.get("output_location")

        if not sql:
            return {"status": "error", "error": "'sql' required", "changed": False}

        athena = boto3.client("athena")
        response = athena.start_query_execution(
            QueryString=sql,
            WorkGroup=workgroup,
            ResultConfiguration={"OutputLocation": output_location} if output_location else {},
        )

        return {
            "status": "changed",
            "query_id": response["QueryExecutionId"],
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def create_view(action: Dict[str, Any]) -> Dict[str, Any]:
    """Create Athena view."""
    time.time()
    database = action.get("database")
    view = action.get("view")
    query = action.get("query")

    if not all([database, view, query]):
        return {
            "status": "error",
            "error": "'database', 'view', 'query' required",
            "changed": False,
        }

    sql = f"CREATE OR REPLACE VIEW {database}.{view} AS {query}"
    return execute_query({"sql": sql, **action})


def create_iceberg_table(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create an Iceberg table via Athena Engine v3.

    Uses ``CREATE TABLE ... TBLPROPERTIES ('table_type'='ICEBERG')`` DDL
    which is the Athena-native way to create Iceberg tables backed by S3
    and registered in the Glue Data Catalog.

    Args:
        action: Table creation action.
            Required keys:
                database: Glue database name
                table: Table name
                columns: Column definitions (Name, Type)
                location: S3 data location
            Optional keys:
                icebergConfig: Iceberg configuration dict
                workgroup: Athena workgroup (default "primary")
                region: AWS region

    Returns:
        Action result with DDL and execution details
    """
    start_time = time.time()

    try:
        import boto3
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    database = action.get("database")
    table = action.get("table")
    columns = action.get("columns", [])
    location = action.get("location")
    iceberg_config = action.get("icebergConfig", {})
    workgroup = action.get("workgroup", "primary")
    region = action.get("region", "us-east-1")

    if not database or not table:
        return {
            "status": "error",
            "error": "'database' and 'table' required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not location:
        return {
            "status": "error",
            "error": "'location' (S3 path) is required for Iceberg tables",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        from ..util.ddl import generate_iceberg_athena_ddl

        ddl = generate_iceberg_athena_ddl(
            database=database,
            table=table,
            columns=columns,
            location=location,
            iceberg_config=iceberg_config,
        )

        athena = boto3.client("athena", region_name=region)

        response = athena.start_query_execution(
            QueryString=ddl,
            QueryExecutionContext={"Database": database},
            WorkGroup=workgroup,
        )

        query_execution_id = response["QueryExecutionId"]

        # Wait for completion
        max_wait = 60
        wait_interval = 2
        elapsed = 0

        while elapsed < max_wait:
            status_response = athena.get_query_execution(QueryExecutionId=query_execution_id)
            status = status_response["QueryExecution"]["Status"]["State"]

            if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break

            time.sleep(wait_interval)
            elapsed += wait_interval

        if status == "SUCCEEDED":
            return {
                "status": "changed",
                "database": database,
                "table": table,
                "table_format": "iceberg",
                "location": location,
                "query_execution_id": query_execution_id,
                "ddl": ddl,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }
        elif status == "FAILED":
            error_reason = status_response["QueryExecution"]["Status"].get(
                "StateChangeReason", "Unknown error"
            )
            # Table may already exist — treat as idempotent
            if "already exists" in error_reason.lower():
                return {
                    "status": "ok",
                    "database": database,
                    "table": table,
                    "table_format": "iceberg",
                    "message": "Iceberg table already exists",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }
            return {
                "status": "error",
                "error": f"Iceberg DDL failed: {error_reason}",
                "ddl": ddl,
                "query_execution_id": query_execution_id,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }
        else:
            return {
                "status": "error",
                "error": f"Query timed out or cancelled (status: {status})",
                "query_execution_id": query_execution_id,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
