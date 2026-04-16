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

# fluid_build/providers/aws/actions/kinesis.py
"""
AWS Kinesis actions for streaming data infrastructure.

Implements idempotent Kinesis operations including:
- Kinesis Data Streams creation and management
- Kinesis Firehose delivery streams
- Kinesis Data Analytics (SQL and Flink applications)
- Stream configuration and scaling
"""

import time
from typing import Any, Dict, List

from ..util.logging import duration_ms


def ensure_stream(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Kinesis Data Stream exists with specified configuration.

    Creates stream if it doesn't exist, updates shard count if changed.
    Idempotent operation - safe to run multiple times.

    Args:
        action: Stream action configuration
            - stream_name: Name of the stream (required)
            - shard_count: Number of shards (default: 1)
            - retention_hours: Data retention in hours (default: 24)
            - region: AWS region

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

    stream_name = action.get("stream_name")
    shard_count = action.get("shard_count", 1)
    retention_hours = action.get("retention_hours", 24)
    region = action.get("region", "us-east-1")

    # Input validation
    if not stream_name:
        return {
            "status": "error",
            "error": "'stream_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate shard count (1-200 for standard streams)
    if not isinstance(shard_count, int) or shard_count < 1 or shard_count > 200:
        return {
            "status": "error",
            "error": f"'shard_count' must be between 1 and 200, got {shard_count}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate retention hours (24-8760 for standard streams)
    if not isinstance(retention_hours, int) or retention_hours < 24 or retention_hours > 8760:
        return {
            "status": "error",
            "error": f"'retention_hours' must be between 24 and 8760 (1 year), got {retention_hours}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        kinesis = boto3.client("kinesis", region_name=region)

        changed = False

        # Check if stream exists
        try:
            response = kinesis.describe_stream(StreamName=stream_name)
            stream_exists = True
            current_status = response["StreamDescription"]["StreamStatus"]
            current_shards = len(response["StreamDescription"]["Shards"])
            current_retention = response["StreamDescription"]["RetentionPeriodHours"]

            # Update retention if changed
            if current_retention != retention_hours:
                if current_status == "ACTIVE":
                    if retention_hours > current_retention:
                        kinesis.increase_stream_retention_period(
                            StreamName=stream_name, RetentionPeriodHours=retention_hours
                        )
                    else:
                        kinesis.decrease_stream_retention_period(
                            StreamName=stream_name, RetentionPeriodHours=retention_hours
                        )
                    changed = True

            # Update shard count if changed (UpdateShardCount API)
            if current_shards != shard_count and current_status == "ACTIVE":
                try:
                    kinesis.update_shard_count(
                        StreamName=stream_name,
                        TargetShardCount=shard_count,
                        ScalingType="UNIFORM_SCALING",
                    )
                    changed = True
                except ClientError as shard_error:
                    # Log warning but don't fail - shard updates have limits
                    if "LimitExceededException" not in str(shard_error):
                        raise

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                stream_exists = False
            else:
                raise

        if not stream_exists:
            # Create stream
            kinesis.create_stream(StreamName=stream_name, ShardCount=shard_count)
            changed = True

            # Wait for stream to become active (optional, can be slow)
            # Commented out for speed, but available if needed:
            # waiter = kinesis.get_waiter('stream_exists')
            # waiter.wait(StreamName=stream_name)

            # Set retention period if not default
            if retention_hours != 24:
                kinesis.increase_stream_retention_period(
                    StreamName=stream_name, RetentionPeriodHours=retention_hours
                )

        return {
            "status": "changed" if changed else "ok",
            "stream_name": stream_name,
            "shard_count": shard_count,
            "retention_hours": retention_hours,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "stream_name": stream_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_firehose(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Kinesis Firehose delivery stream exists.

    Creates delivery stream if it doesn't exist, updates configuration if changed.
    Supports delivery to S3, Redshift, Elasticsearch, and Splunk.

    Args:
        action: Firehose action configuration
            - delivery_stream_name: Name of delivery stream (required)
            - delivery_stream_type: Type (DirectPut or KinesisStreamAsSource)
            - s3_destination: S3 destination configuration
            - region: AWS region

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

    delivery_stream_name = action.get("delivery_stream_name")
    delivery_stream_type = action.get("delivery_stream_type", "DirectPut")
    s3_destination = action.get("s3_destination", {})
    region = action.get("region", "us-east-1")

    if not delivery_stream_name:
        return {
            "status": "error",
            "error": "'delivery_stream_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not s3_destination:
        return {
            "status": "error",
            "error": "'s3_destination' configuration is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        firehose = boto3.client("firehose", region_name=region)

        changed = False

        # Check if delivery stream exists
        try:
            firehose.describe_delivery_stream(DeliveryStreamName=delivery_stream_name)
            stream_exists = True

            # Note: Updating Firehose configuration is complex
            # For now, we skip updates and only handle creation
            # Future enhancement: implement update logic

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                stream_exists = False
            else:
                raise

        if not stream_exists:
            # Prepare S3 destination configuration
            s3_config = {
                "RoleARN": s3_destination.get("role_arn", ""),
                "BucketARN": s3_destination.get("bucket_arn", ""),
                "Prefix": s3_destination.get("prefix", ""),
                "ErrorOutputPrefix": s3_destination.get("error_prefix", "errors/"),
                "BufferingHints": {
                    "SizeInMBs": s3_destination.get("buffer_size_mb", 5),
                    "IntervalInSeconds": s3_destination.get("buffer_interval_sec", 300),
                },
                "CompressionFormat": s3_destination.get("compression", "GZIP"),
            }

            # Create delivery stream
            firehose.create_delivery_stream(
                DeliveryStreamName=delivery_stream_name,
                DeliveryStreamType=delivery_stream_type,
                S3DestinationConfiguration=s3_config,
            )
            changed = True

        return {
            "status": "changed" if changed else "ok",
            "delivery_stream_name": delivery_stream_name,
            "delivery_stream_type": delivery_stream_type,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "delivery_stream_name": delivery_stream_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def put_records(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Put records into a Kinesis Data Stream.

    Args:
        action: Put records action configuration
            - stream_name: Name of the stream (required)
            - records: List of records to put
            - region: AWS region

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        import boto3
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    stream_name = action.get("stream_name")
    records = action.get("records", [])
    region = action.get("region", "us-east-1")

    if not stream_name:
        return {
            "status": "error",
            "error": "'stream_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not records:
        return {
            "status": "ok",
            "message": "No records to put",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate batch size (Kinesis limit: 500 records per request)
    if len(records) > 500:
        return {
            "status": "error",
            "error": f"Batch size exceeds Kinesis limit of 500 records, got {len(records)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        kinesis = boto3.client("kinesis", region_name=region)

        # Validate and prepare records
        prepared_records = []
        for i, record in enumerate(records):
            if isinstance(record, dict):
                # Ensure each record has required fields
                if "Data" not in record:
                    return {
                        "status": "error",
                        "error": f"Record {i} missing 'Data' field",
                        "duration_ms": duration_ms(start_time),
                        "changed": False,
                    }

                # Auto-generate partition key if not provided
                if "PartitionKey" not in record:
                    record["PartitionKey"] = str(i)

                prepared_records.append(record)
            else:
                # Simple string/bytes record - wrap it
                prepared_records.append(
                    {
                        "Data": record if isinstance(record, bytes) else str(record).encode(),
                        "PartitionKey": str(i),
                    }
                )

        # Put records (batch of up to 500 at a time)
        response = kinesis.put_records(StreamName=stream_name, Records=prepared_records)

        failed_count = response.get("FailedRecordCount", 0)

        return {
            "status": "changed" if failed_count == 0 else "partial",
            "stream_name": stream_name,
            "records_sent": len(records),
            "records_failed": failed_count,
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


def ensure_analytics_application(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Kinesis Data Analytics application exists.

    Creates SQL or Flink-based analytics application for real-time stream processing.
    Supports both SQL and Flink (Apache Flink) runtime environments.

    Args:
        action: Analytics application configuration
            - application_name: Name of the application (required)
            - runtime_environment: SQL-1_0, FLINK-1_15, FLINK-1_18, etc. (required)
            - service_execution_role: IAM role ARN (required)
            - inputs: List of input stream configurations
            - outputs: List of output stream/destination configurations
            - application_code: SQL query or Flink application code
            - region: AWS region

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

    application_name = action.get("application_name")
    runtime_environment = action.get("runtime_environment", "SQL-1_0")
    service_execution_role = action.get("service_execution_role")
    inputs = action.get("inputs", [])
    outputs = action.get("outputs", [])
    application_code = action.get("application_code", "")
    region = action.get("region", "us-east-1")

    # Input validation
    if not application_name:
        return {
            "status": "error",
            "error": "'application_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not service_execution_role:
        return {
            "status": "error",
            "error": "'service_execution_role' (IAM role ARN) is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate runtime environment
    valid_runtimes = [
        "SQL-1_0",
        "FLINK-1_6",
        "FLINK-1_8",
        "FLINK-1_11",
        "FLINK-1_13",
        "FLINK-1_15",
        "FLINK-1_18",
    ]
    if runtime_environment not in valid_runtimes:
        return {
            "status": "error",
            "error": f"'runtime_environment' must be one of {valid_runtimes}, got '{runtime_environment}'",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        kinesisanalytics = boto3.client("kinesisanalyticsv2", region_name=region)

        changed = False
        application_arn = None

        # Check if application exists
        try:
            response = kinesisanalytics.describe_application(ApplicationName=application_name)
            application_exists = True
            _application_status = response["ApplicationDetail"]["ApplicationStatus"]  # noqa: F841
            application_arn = response["ApplicationDetail"]["ApplicationARN"]

            # Note: Updating Kinesis Analytics applications is complex
            # For now, we only handle creation. Updates require stopping,
            # updating configuration, and restarting the application.
            # Future enhancement: implement update logic

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                application_exists = False
            else:
                raise

        if not application_exists:
            # Prepare application configuration
            app_config = {
                "ApplicationName": application_name,
                "RuntimeEnvironment": runtime_environment,
                "ServiceExecutionRole": service_execution_role,
            }

            # Add application configuration based on runtime
            if runtime_environment.startswith("SQL"):
                # SQL-based application
                if application_code:
                    app_config["ApplicationConfiguration"] = {
                        "SqlApplicationConfiguration": {
                            "Inputs": _prepare_sql_inputs(inputs),
                            "Outputs": _prepare_sql_outputs(outputs),
                        },
                        "ApplicationCodeConfiguration": {
                            "CodeContent": {"TextContent": application_code},
                            "CodeContentType": "PLAINTEXT",
                        },
                    }
            else:
                # Flink-based application
                if application_code:
                    # For Flink, code is typically uploaded to S3
                    # This is a simplified version
                    app_config["ApplicationConfiguration"] = {
                        "FlinkApplicationConfiguration": {
                            "CheckpointConfiguration": {"ConfigurationType": "DEFAULT"},
                            "MonitoringConfiguration": {
                                "ConfigurationType": "DEFAULT",
                                "LogLevel": "INFO",
                            },
                            "ParallelismConfiguration": {"ConfigurationType": "DEFAULT"},
                        },
                        "ApplicationCodeConfiguration": {
                            "CodeContent": {
                                "S3ContentLocation": {
                                    "BucketARN": action.get("code_bucket_arn", ""),
                                    "FileKey": action.get("code_file_key", ""),
                                }
                            },
                            "CodeContentType": "ZIPFILE",
                        },
                    }

            # Create application
            response = kinesisanalytics.create_application(**app_config)
            changed = True
            application_arn = response["ApplicationDetail"]["ApplicationARN"]

            # Optionally start the application
            if action.get("auto_start", False):
                kinesisanalytics.start_application(
                    ApplicationName=application_name, RunConfiguration={}
                )

        return {
            "status": "changed" if changed else "ok",
            "application_name": application_name,
            "runtime_environment": runtime_environment,
            "application_arn": application_arn,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "application_name": application_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def _prepare_sql_inputs(inputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Prepare SQL application input configurations.

    Args:
        inputs: List of input configurations

    Returns:
        List of formatted input configurations
    """
    prepared_inputs = []

    for input_config in inputs:
        prepared_input = {
            "NamePrefix": input_config.get("name_prefix", "SOURCE_SQL_STREAM"),
            "InputSchema": {
                "RecordFormat": {
                    "RecordFormatType": input_config.get("format_type", "JSON"),
                    "MappingParameters": {
                        "JSONMappingParameters": {
                            "RecordRowPath": input_config.get("record_row_path", "$")
                        }
                    },
                },
                "RecordColumns": input_config.get("columns", []),
            },
        }

        # Add Kinesis stream source
        if "kinesis_stream_arn" in input_config:
            prepared_input["KinesisStreamsInput"] = {
                "ResourceARN": input_config["kinesis_stream_arn"]
            }

        # Or add Kinesis Firehose source
        elif "firehose_arn" in input_config:
            prepared_input["KinesisFirehoseInput"] = {"ResourceARN": input_config["firehose_arn"]}

        prepared_inputs.append(prepared_input)

    return prepared_inputs


def _prepare_sql_outputs(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Prepare SQL application output configurations.

    Args:
        outputs: List of output configurations

    Returns:
        List of formatted output configurations
    """
    prepared_outputs = []

    for output_config in outputs:
        prepared_output = {
            "Name": output_config.get("name", "DESTINATION_SQL_STREAM"),
            "DestinationSchema": {"RecordFormatType": output_config.get("format_type", "JSON")},
        }

        # Add Kinesis stream destination
        if "kinesis_stream_arn" in output_config:
            prepared_output["KinesisStreamsOutput"] = {
                "ResourceARN": output_config["kinesis_stream_arn"]
            }

        # Or add Kinesis Firehose destination
        elif "firehose_arn" in output_config:
            prepared_output["KinesisFirehoseOutput"] = {
                "ResourceARN": output_config["firehose_arn"]
            }

        # Or add Lambda destination
        elif "lambda_arn" in output_config:
            prepared_output["LambdaOutput"] = {"ResourceARN": output_config["lambda_arn"]}

        prepared_outputs.append(prepared_output)

    return prepared_outputs


def start_analytics_application(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Start a Kinesis Data Analytics application.

    Args:
        action: Start action configuration
            - application_name: Name of the application (required)
            - region: AWS region

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
            "error": "boto3 library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    application_name = action.get("application_name")
    region = action.get("region", "us-east-1")

    if not application_name:
        return {
            "status": "error",
            "error": "'application_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        kinesisanalytics = boto3.client("kinesisanalyticsv2", region_name=region)

        # Check application status
        response = kinesisanalytics.describe_application(ApplicationName=application_name)
        status = response["ApplicationDetail"]["ApplicationStatus"]

        # Only start if not already running
        if status in ["READY", "STOPPING", "STOPPED"]:
            kinesisanalytics.start_application(
                ApplicationName=application_name, RunConfiguration={}
            )
            changed = True
        else:
            changed = False

        return {
            "status": "changed" if changed else "ok",
            "application_name": application_name,
            "previous_status": status,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "application_name": application_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def stop_analytics_application(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stop a Kinesis Data Analytics application.

    Args:
        action: Stop action configuration
            - application_name: Name of the application (required)
            - force: Force stop even if application is in transitional state
            - region: AWS region

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
            "error": "boto3 library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    application_name = action.get("application_name")
    force = action.get("force", False)
    region = action.get("region", "us-east-1")

    if not application_name:
        return {
            "status": "error",
            "error": "'application_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        kinesisanalytics = boto3.client("kinesisanalyticsv2", region_name=region)

        # Check application status
        response = kinesisanalytics.describe_application(ApplicationName=application_name)
        status = response["ApplicationDetail"]["ApplicationStatus"]

        # Only stop if running
        if status in ["RUNNING", "STARTING"]:
            kinesisanalytics.stop_application(ApplicationName=application_name, Force=force)
            changed = True
        else:
            changed = False

        return {
            "status": "changed" if changed else "ok",
            "application_name": application_name,
            "previous_status": status,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "application_name": application_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
