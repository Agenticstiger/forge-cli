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

# fluid_build/providers/aws/actions/glue.py
"""
AWS Glue Data Catalog actions.

Implements idempotent Glue operations including:
- Database creation and management
- Table creation with schema evolution
- Crawler configuration
"""

import time
from typing import Any, Dict, List

from ..util.logging import duration_ms
from ..util.names import normalize_database_name, normalize_table_name


def ensure_database(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Glue database exists with specified configuration.

    Creates database if it doesn't exist, updates configuration if changed.
    Idempotent operation - safe to run multiple times.

    Args:
        action: Database action configuration

    Returns:
        Action result with status and details
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
    description = action.get("description", "")
    location = action.get("location")
    tags = action.get("tags", {})

    if not database:
        return {
            "status": "error",
            "error": "'database' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        glue = boto3.client("glue")

        normalized_db = normalize_database_name(database)
        changed = False

        # Check if database exists
        try:
            response = glue.get_database(Name=normalized_db)
            existing_db = response["Database"]

            # Check if update needed
            update_needed = False
            database_input = {"Name": normalized_db}

            if existing_db.get("Description") != description:
                database_input["Description"] = description
                update_needed = True

            if location and existing_db.get("LocationUri") != location:
                database_input["LocationUri"] = location
                update_needed = True

            if update_needed:
                glue.update_database(Name=normalized_db, DatabaseInput=database_input)
                changed = True

            return {
                "status": "changed" if changed else "ok",
                "database": normalized_db,
                "location": existing_db.get("LocationUri"),
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code != "EntityNotFoundException":
                raise

        # Database doesn't exist, create it
        database_input = {"Name": normalized_db}

        if description:
            database_input["Description"] = description

        if location:
            database_input["LocationUri"] = location

        glue.create_database(DatabaseInput=database_input)

        return {
            "status": "changed",
            "database": normalized_db,
            "location": location,
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "database": database if "database" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_table(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Glue table exists with specified schema.

    Creates table if it doesn't exist, updates schema if changed.
    Supports schema evolution.

    Args:
        action: Table action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
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
    input_format = action.get("input_format", "parquet")
    description = action.get("description", "")

    if not database or not table:
        return {
            "status": "error",
            "error": "'database' and 'table' required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        glue = boto3.client("glue")

        normalized_table = normalize_table_name(table)

        # Build table input
        storage_descriptor = {
            "Columns": columns,
            "Location": location or "",
            "InputFormat": _get_input_format(input_format),
            "OutputFormat": _get_output_format(input_format),
            "SerdeInfo": {"SerializationLibrary": _get_serde_lib(input_format)},
        }

        table_input = {
            "Name": normalized_table,
            "StorageDescriptor": storage_descriptor,
            "TableType": "EXTERNAL_TABLE",
        }

        if description:
            table_input["Description"] = description

        # Check if table exists
        try:
            response = glue.get_table(DatabaseName=database, Name=normalized_table)
            existing_table = response["Table"]

            # ── Schema-diff: only update if columns or location changed ──
            existing_cols = existing_table.get("StorageDescriptor", {}).get("Columns", [])
            existing_location = existing_table.get("StorageDescriptor", {}).get("Location", "")

            cols_match = _columns_equal(existing_cols, columns)
            location_match = (not location) or (
                existing_location.rstrip("/") == (location or "").rstrip("/")
            )

            if cols_match and location_match:
                return {
                    "status": "ok",
                    "database": database,
                    "table": normalized_table,
                    "columns_count": len(columns),
                    "message": "Table already up-to-date",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

            # Something changed — apply update
            glue.update_table(DatabaseName=database, TableInput=table_input)

            return {
                "status": "changed",
                "database": database,
                "table": normalized_table,
                "columns_count": len(columns),
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code != "EntityNotFoundException":
                raise

        # Table doesn't exist, create it
        glue.create_table(DatabaseName=database, TableInput=table_input)

        return {
            "status": "changed",
            "database": database,
            "table": normalized_table,
            "columns_count": len(columns),
            "location": location,
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


def ensure_crawler(action: Dict[str, Any]) -> Dict[str, Any]:
    """Configure Glue crawler."""
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

    crawler_name = action.get("name")
    database = action.get("database")
    s3_path = action.get("s3_path")
    role = action.get("role")

    if not all([crawler_name, database, s3_path, role]):
        return {
            "status": "error",
            "error": "'name', 'database', 's3_path', and 'role' required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        glue = boto3.client("glue")

        # Create or update crawler
        crawler_config = {
            "Name": crawler_name,
            "Role": role,
            "DatabaseName": database,
            "Targets": {"S3Targets": [{"Path": s3_path}]},
        }

        try:
            glue.get_crawler(Name=crawler_name)
            glue.update_crawler(**crawler_config)
            changed = True
        except glue.exceptions.EntityNotFoundException:
            glue.create_crawler(**crawler_config)
            changed = True

        return {
            "status": "changed" if changed else "ok",
            "crawler": crawler_name,
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


def run_crawler(action: Dict[str, Any]) -> Dict[str, Any]:
    """Start Glue crawler execution."""
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

    crawler_name = action.get("name")

    if not crawler_name:
        return {"status": "error", "error": "'name' required", "changed": False}

    try:
        glue = boto3.client("glue")
        glue.start_crawler(Name=crawler_name)

        return {
            "status": "changed",
            "crawler": crawler_name,
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


def _get_input_format(format_type: str) -> str:
    """Get Glue input format class for format type."""
    format_map = {
        "parquet": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
        "orc": "org.apache.hadoop.hive.ql.io.orc.OrcInputFormat",
        "avro": "org.apache.hadoop.hive.ql.io.avro.AvroContainerInputFormat",
        "csv": "org.apache.hadoop.mapred.TextInputFormat",
        "json": "org.apache.hadoop.mapred.TextInputFormat",
    }
    return format_map.get(format_type.lower(), format_map["parquet"])


def _get_output_format(format_type: str) -> str:
    """Get Glue output format class for format type."""
    format_map = {
        "parquet": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
        "orc": "org.apache.hadoop.hive.ql.io.orc.OrcOutputFormat",
        "avro": "org.apache.hadoop.hive.ql.io.avro.AvroContainerOutputFormat",
        "csv": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
        "json": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
    }
    return format_map.get(format_type.lower(), format_map["parquet"])


def _get_serde_lib(format_type: str) -> str:
    """Get Glue SerDe library for format type."""
    serde_map = {
        "parquet": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
        "orc": "org.apache.hadoop.hive.ql.io.orc.OrcSerde",
        "avro": "org.apache.hadoop.hive.serde2.avro.AvroSerDe",
        "csv": "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
        "json": "org.openx.data.jsonserde.JsonSerDe",
    }
    return serde_map.get(format_type.lower(), serde_map["parquet"])


def _columns_equal(
    existing: List[Dict[str, str]],
    desired: List[Dict[str, str]],
) -> bool:
    """
    Compare Glue column definitions for equality.

    Compares Name and Type (case-insensitive type match).
    Ignores Comment differences to avoid unnecessary updates.
    """
    if len(existing) != len(desired):
        return False
    for a, b in zip(existing, desired):
        if a.get("Name", "").lower() != b.get("Name", "").lower():
            return False
        if a.get("Type", "").lower() != b.get("Type", "").lower():
            return False
    return True


def _get_iceberg_table_parameters(iceberg_config: Dict[str, Any]) -> Dict[str, str]:
    """
    Build Glue table parameters for Iceberg tables.

    These parameters identify the table as Iceberg and configure
    its behavior in Athena and other query engines.

    Args:
        iceberg_config: Iceberg configuration from contract

    Returns:
        Table parameters dict for Glue CreateTable/UpdateTable
    """
    write_version = iceberg_config.get("writeVersion", 2)
    file_format = iceberg_config.get("fileFormat", "parquet")
    properties = iceberg_config.get("properties", {})

    params = {
        "table_type": "ICEBERG",  # Critical: Identifies as Iceberg
        "format-version": str(write_version),
        "write.format.default": file_format,
    }

    # Add custom properties
    for key, value in properties.items():
        params[key] = str(value)

    return params


def ensure_iceberg_table(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create or update Iceberg table in Glue Catalog.

    Iceberg tables have special requirements:
    1. table_type parameter must be "ICEBERG"
    2. Uses Glue Catalog for metadata (not manual metadata files)
    3. Supports ACID operations via Athena v3 or EMR
    4. Partition transforms are hidden from users

    Unlike standard tables, Iceberg tables:
    - Support ACID transactions
    - Enable time travel queries
    - Provide automatic compaction
    - Allow schema evolution without rewrites

    Args:
        action: Table action with Iceberg configuration
            Required keys:
                database: Glue database name
                table: Table name
            Optional keys:
                columns: List of column definitions
                location: S3 location for table data
                icebergConfig: Iceberg-specific configuration
                description: Table description

    Returns:
        Action result with Iceberg table details
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
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
    description = action.get("description", "")

    if not database or not table:
        return {
            "status": "error",
            "error": "'database' and 'table' required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        glue = boto3.client("glue")

        normalized_table = normalize_table_name(table)

        # Build Iceberg table input
        file_format = iceberg_config.get("fileFormat", "parquet")

        storage_descriptor = {
            "Columns": columns,
            "Location": location or "",
            "InputFormat": "org.apache.iceberg.mr.hive.HiveIcebergInputFormat",
            "OutputFormat": "org.apache.iceberg.mr.hive.HiveIcebergOutputFormat",
            "SerdeInfo": {
                "SerializationLibrary": "org.apache.iceberg.mr.hive.HiveIcebergSerDe",
                "Parameters": {"serialization.format": "1"},
            },
        }

        # Iceberg table parameters
        table_parameters = _get_iceberg_table_parameters(iceberg_config)

        table_input = {
            "Name": normalized_table,
            "StorageDescriptor": storage_descriptor,
            "TableType": "EXTERNAL_TABLE",
            "Parameters": table_parameters,
        }

        if description:
            table_input["Description"] = description

        # Check if table exists
        try:
            response = glue.get_table(DatabaseName=database, Name=normalized_table)
            existing_table = response["Table"]

            # Validate it's an Iceberg table
            existing_type = existing_table.get("Parameters", {}).get("table_type")
            if existing_type != "ICEBERG":
                return {
                    "status": "error",
                    "error": f"Table exists but is not Iceberg (type: {existing_type})",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

            # Schema-diff: skip update if columns unchanged
            existing_cols = existing_table.get("StorageDescriptor", {}).get("Columns", [])
            if _columns_equal(existing_cols, columns):
                return {
                    "status": "ok",
                    "database": database,
                    "table": normalized_table,
                    "table_format": "iceberg",
                    "message": "Iceberg table already up-to-date",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

            # Schema evolved — apply update
            glue.update_table(DatabaseName=database, TableInput=table_input)

            return {
                "status": "changed",
                "database": database,
                "table": normalized_table,
                "table_format": "iceberg",
                "write_version": iceberg_config.get("writeVersion", 2),
                "file_format": file_format,
                "columns_count": len(columns),
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code != "EntityNotFoundException":
                raise

        # Table doesn't exist, create Iceberg table
        glue.create_table(DatabaseName=database, TableInput=table_input)

        return {
            "status": "changed",
            "database": database,
            "table": normalized_table,
            "table_format": "iceberg",
            "write_version": iceberg_config.get("writeVersion", 2),
            "file_format": file_format,
            "columns_count": len(columns),
            "location": location,
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


def ensure_job(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create or update an AWS Glue ETL job.

    Supports Spark and Python Shell job types with idempotent semantics.
    If the job already exists it is updated only when configuration differs.

    Args:
        action: Job configuration.
            Required keys:
                name: Job name
                role: IAM role ARN for the job
                script_location: S3 path to the ETL script
            Optional keys:
                command_name: "glueetl" | "pythonshell" (default "glueetl")
                glue_version: Glue version (default "4.0")
                python_version: Python version (default "3")
                worker_type: Worker type (default "G.1X")
                number_of_workers: Number of workers (default 10)
                timeout: Timeout in minutes (default 2880)
                max_retries: Max auto-retries (default 0)
                description: Job description
                default_arguments: Dict of default job arguments
                tags: Resource tags
                temp_dir: S3 temp directory
                extra_py_files: Additional Python files
                extra_jars: Additional JAR files
                connections: List of Glue connection names
                security_configuration: Security configuration name

    Returns:
        Action result with job details
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    name = action.get("name")
    role = action.get("role")
    command_name = action.get("command_name", "glueetl")
    script_location = action.get("script_location")

    if not name or not role or not script_location:
        return {
            "status": "error",
            "error": "'name', 'role', and 'script_location' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    glue_version = action.get("glue_version", "4.0")
    python_version = action.get("python_version", "3")
    worker_type = action.get("worker_type", "G.1X")
    number_of_workers = action.get("number_of_workers", 10)
    timeout = action.get("timeout", 2880)
    max_retries = action.get("max_retries", 0)
    description = action.get("description", "")
    default_arguments = action.get("default_arguments", {})
    tags = action.get("tags", {})
    temp_dir = action.get("temp_dir")
    extra_py_files = action.get("extra_py_files", [])
    extra_jars = action.get("extra_jars", [])
    connections = action.get("connections", [])
    security_configuration = action.get("security_configuration")

    # Build default arguments
    args: Dict[str, str] = {}
    if temp_dir:
        args["--TempDir"] = temp_dir
    if extra_py_files:
        args["--extra-py-files"] = ",".join(extra_py_files)
    if extra_jars:
        args["--extra-jars"] = ",".join(extra_jars)
    args["--enable-metrics"] = "true"
    args["--enable-continuous-cloudwatch-log"] = "true"
    args["--job-bookmark-option"] = "job-bookmark-enable"
    args.update(default_arguments)

    job_params: Dict[str, Any] = {
        "Name": name,
        "Role": role,
        "Command": {
            "Name": command_name,
            "ScriptLocation": script_location,
            "PythonVersion": python_version,
        },
        "GlueVersion": glue_version,
        "Timeout": timeout,
        "MaxRetries": max_retries,
        "DefaultArguments": args,
    }

    if description:
        job_params["Description"] = description

    if command_name == "glueetl":
        job_params["WorkerType"] = worker_type
        job_params["NumberOfWorkers"] = number_of_workers

    if connections:
        job_params["Connections"] = {"Connections": connections}

    if security_configuration:
        job_params["SecurityConfiguration"] = security_configuration

    try:
        glue = boto3.client("glue")

        # Check if job exists
        try:
            existing = glue.get_job(JobName=name)
            existing_job = existing["Job"]

            # Detect meaningful changes
            existing_cmd = existing_job.get("Command", {})
            existing_args = existing_job.get("DefaultArguments", {})
            needs_update = (
                existing_cmd.get("ScriptLocation") != script_location
                or existing_job.get("Role") != role
                or existing_job.get("GlueVersion") != glue_version
                or existing_job.get("Timeout") != timeout
                or existing_args != args
            )

            if not needs_update:
                return {
                    "status": "ok",
                    "job": name,
                    "message": "Job already up-to-date",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

            # Update existing job
            update_params = dict(job_params)
            update_params.pop("Name", None)
            glue.update_job(JobName=name, JobUpdate=update_params)

            return {
                "status": "changed",
                "job": name,
                "message": "Job updated",
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "EntityNotFoundException":
                raise

        # Create new job
        if tags:
            job_params["Tags"] = tags

        glue.create_job(**job_params)

        return {
            "status": "changed",
            "job": name,
            "message": "Job created",
            "glue_version": glue_version,
            "worker_type": worker_type,
            "number_of_workers": number_of_workers,
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "job": name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def start_job_run(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Start an AWS Glue job run.

    Args:
        action: Run configuration.
            Required keys:
                name: Glue job name
            Optional keys:
                arguments: Override default arguments
                timeout: Override timeout in minutes
                worker_type: Override worker type
                number_of_workers: Override number of workers

    Returns:
        Action result with job run ID
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

    name = action.get("name")
    if not name:
        return {
            "status": "error",
            "error": "'name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    run_params: Dict[str, Any] = {"JobName": name}

    arguments = action.get("arguments")
    if arguments:
        run_params["Arguments"] = arguments

    timeout = action.get("timeout")
    if timeout:
        run_params["Timeout"] = timeout

    worker_type = action.get("worker_type")
    number_of_workers = action.get("number_of_workers")
    if worker_type:
        run_params["WorkerType"] = worker_type
    if number_of_workers:
        run_params["NumberOfWorkers"] = number_of_workers

    try:
        glue = boto3.client("glue")
        response = glue.start_job_run(**run_params)

        return {
            "status": "changed",
            "job": name,
            "job_run_id": response["JobRunId"],
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "job": name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
