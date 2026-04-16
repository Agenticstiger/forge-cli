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
AWS Provider Types - Production-grade type definitions for AWS services integration.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class AWSRegion(Enum):
    """AWS regions supported by FLUID."""

    US_EAST_1 = "us-east-1"
    US_EAST_2 = "us-east-2"
    US_WEST_1 = "us-west-1"
    US_WEST_2 = "us-west-2"
    EU_WEST_1 = "eu-west-1"
    EU_WEST_2 = "eu-west-2"
    EU_CENTRAL_1 = "eu-central-1"
    AP_SOUTHEAST_1 = "ap-southeast-1"
    AP_SOUTHEAST_2 = "ap-southeast-2"
    AP_NORTHEAST_1 = "ap-northeast-1"


class AuthenticationMethod(Enum):
    """AWS authentication methods."""

    IAM_ROLE = "iam_role"
    ACCESS_KEYS = "access_keys"
    SSO = "sso"
    ASSUME_ROLE = "assume_role"
    EC2_INSTANCE_PROFILE = "ec2_instance_profile"
    EKS_SERVICE_ACCOUNT = "eks_service_account"


class StorageClass(Enum):
    """S3 storage classes for cost optimization."""

    STANDARD = "STANDARD"
    STANDARD_IA = "STANDARD_IA"
    ONEZONE_IA = "ONEZONE_IA"
    REDUCED_REDUNDANCY = "REDUCED_REDUNDANCY"
    GLACIER = "GLACIER"
    DEEP_ARCHIVE = "DEEP_ARCHIVE"
    INTELLIGENT_TIERING = "INTELLIGENT_TIERING"


@dataclass
class IAMConfiguration:
    """IAM-specific configuration."""

    role_arn: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    session_token: Optional[str] = None
    profile_name: Optional[str] = None
    external_id: Optional[str] = None
    duration_seconds: int = 3600
    mfa_serial: Optional[str] = None
    mfa_token: Optional[str] = None


@dataclass
class S3Configuration:
    """S3-specific configuration."""

    bucket_name: str
    region: AWSRegion
    encryption_type: str = "AES256"
    kms_key_id: Optional[str] = None
    versioning_enabled: bool = False
    lifecycle_policies: List[Dict[str, Any]] = field(default_factory=list)
    public_access_blocked: bool = True
    transfer_acceleration: bool = False
    intelligent_tiering: bool = False
    object_lock_enabled: bool = False
    cors_configuration: Optional[Dict[str, Any]] = None
    website_configuration: Optional[Dict[str, Any]] = None


@dataclass
class RedshiftConfiguration:
    """Redshift-specific configuration."""

    cluster_identifier: str
    node_type: str = "dc2.large"
    number_of_nodes: int = 1
    database_name: str = "analytics"
    master_username: str = "admin"
    master_user_password: Optional[str] = None
    port: int = 5439
    vpc_security_group_ids: List[str] = field(default_factory=list)
    subnet_group_name: Optional[str] = None
    publicly_accessible: bool = False
    encrypted: bool = True
    kms_key_id: Optional[str] = None
    automated_snapshot_retention_period: int = 7
    preferred_maintenance_window: str = "sun:05:00-sun:06:00"
    preferred_backup_window: str = "03:00-04:00"
    skip_final_snapshot: bool = False
    final_snapshot_identifier: Optional[str] = None
    allow_version_upgrade: bool = True
    auto_minor_version_upgrade: bool = True
    enable_logging: bool = True
    log_destination_type: str = "cloudwatch"
    parameter_group_name: Optional[str] = None


@dataclass
class GlueConfiguration:
    """Glue-specific configuration."""

    role_arn: str
    glue_version: str = "4.0"
    python_version: str = "3.9"
    worker_type: str = "G.1X"
    number_of_workers: int = 10
    max_capacity: Optional[int] = None
    timeout: int = 2880  # minutes
    max_retries: int = 0
    security_configuration: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    job_bookmark_option: str = "job-bookmark-enable"
    enable_metrics: bool = True
    enable_continuous_cloudwatch_log: bool = True
    enable_spark_ui: bool = False
    spark_event_logs_path: Optional[str] = None
    temp_dir: Optional[str] = None
    extra_py_files: List[str] = field(default_factory=list)
    extra_jars: List[str] = field(default_factory=list)
    extra_files: List[str] = field(default_factory=list)
    job_parameters: Dict[str, str] = field(default_factory=dict)


@dataclass
class AthenaConfiguration:
    """Athena-specific configuration."""

    workgroup: str = "primary"
    database: str = "default"
    output_location: str = ""
    encryption_configuration: Optional[Dict[str, str]] = None
    bytes_scanned_cutoff_per_query: Optional[int] = None
    enforce_workgroup_configuration: bool = False
    publish_cloudwatch_metrics: bool = True
    result_configuration_updates: Dict[str, Any] = field(default_factory=dict)
    query_execution_context: Dict[str, str] = field(default_factory=dict)


@dataclass
class KinesisConfiguration:
    """Kinesis-specific configuration."""

    stream_name: str
    shard_count: int = 1
    retention_period: int = 24  # hours
    shard_level_metrics: List[str] = field(default_factory=list)
    encryption_type: str = "KMS"
    kms_key_id: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    stream_mode: str = "PROVISIONED"  # or ON_DEMAND


@dataclass
class LambdaConfiguration:
    """Lambda-specific configuration."""

    function_name: str
    runtime: str = "python3.9"
    handler: str = "lambda_function.lambda_handler"
    code: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    timeout: int = 300
    memory_size: int = 128
    publish: bool = False
    vpc_config: Optional[Dict[str, Any]] = None
    environment: Dict[str, Any] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)
    kms_key_arn: Optional[str] = None
    tracing_config: Dict[str, str] = field(default_factory=lambda: {"Mode": "PassThrough"})
    dead_letter_config: Optional[Dict[str, str]] = None
    layers: List[str] = field(default_factory=list)
    file_system_configs: List[Dict[str, Any]] = field(default_factory=list)
    image_config: Optional[Dict[str, Any]] = None
    code_signing_config_arn: Optional[str] = None
    architectures: List[str] = field(default_factory=lambda: ["x86_64"])
    ephemeral_storage: Dict[str, int] = field(default_factory=lambda: {"Size": 512})


@dataclass
class EMRConfiguration:
    """EMR-specific configuration."""

    cluster_name: str
    release_label: str = "emr-6.15.0"
    applications: List[str] = field(default_factory=lambda: ["Spark", "Hadoop"])
    log_uri: Optional[str] = None
    service_role: str = "EMR_DefaultRole"
    job_flow_role: str = "EMR_EC2_DefaultRole"
    security_configuration: Optional[str] = None
    auto_scaling_role: Optional[str] = None
    scale_down_behavior: str = "TERMINATE_AT_TASK_COMPLETION"
    custom_ami_id: Optional[str] = None
    ebs_root_volume_size: Optional[int] = None
    repo_upgrade_on_boot: str = "SECURITY"
    kerberos_attributes: Optional[Dict[str, Any]] = None
    step_concurrency_level: int = 1
    placement_group_configs: List[Dict[str, Any]] = field(default_factory=list)
    managed_scaling_policy: Optional[Dict[str, Any]] = None
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class SageMakerConfiguration:
    """SageMaker-specific configuration."""

    model_name: str
    role_arn: str
    primary_container: Dict[str, Any] = field(default_factory=dict)
    containers: List[Dict[str, Any]] = field(default_factory=list)
    vpc_config: Optional[Dict[str, Any]] = None
    enable_network_isolation: bool = False
    tags: Dict[str, str] = field(default_factory=dict)
    inference_execution_config: Optional[Dict[str, str]] = None


@dataclass
class SecurityConfig:
    """Comprehensive security configuration for AWS services."""

    encryption_at_rest: bool = True
    encryption_in_transit: bool = True
    kms_key_id: Optional[str] = None
    vpc_config: Optional[Dict[str, Any]] = None
    security_groups: List[str] = field(default_factory=list)
    subnet_ids: List[str] = field(default_factory=list)
    iam_policies: List[Dict[str, Any]] = field(default_factory=list)
    resource_policies: List[Dict[str, Any]] = field(default_factory=list)
    network_acls: List[Dict[str, Any]] = field(default_factory=list)
    cloudtrail_enabled: bool = True
    guardduty_enabled: bool = True
    config_enabled: bool = True
    secrets_manager_integration: bool = False
    waf_enabled: bool = False
    shield_advanced: bool = False


@dataclass
class MonitoringConfig:
    """Comprehensive monitoring configuration."""

    cloudwatch_enabled: bool = True
    xray_tracing_enabled: bool = False
    custom_metrics: List[Dict[str, Any]] = field(default_factory=list)
    alarms: List[Dict[str, Any]] = field(default_factory=list)
    dashboards: List[Dict[str, Any]] = field(default_factory=list)
    log_groups: List[Dict[str, Any]] = field(default_factory=list)
    sns_topics: List[Dict[str, Any]] = field(default_factory=list)
    eventbridge_rules: List[Dict[str, Any]] = field(default_factory=list)
    application_insights: bool = False
    performance_insights: bool = False
    detailed_monitoring: bool = False


@dataclass
class CostOptimizationConfig:
    """Cost optimization configuration."""

    reserved_instances: List[Dict[str, Any]] = field(default_factory=list)
    spot_instances: List[Dict[str, Any]] = field(default_factory=list)
    s3_intelligent_tiering: bool = True
    s3_lifecycle_policies: List[Dict[str, Any]] = field(default_factory=list)
    auto_scaling_policies: List[Dict[str, Any]] = field(default_factory=list)
    budgets: List[Dict[str, Any]] = field(default_factory=list)
    cost_allocation_tags: Dict[str, str] = field(default_factory=dict)
    trusted_advisor_enabled: bool = True
    right_sizing_enabled: bool = True


@dataclass
class ServiceConfig:
    """Service-specific configurations."""

    s3: Optional[S3Configuration] = None
    redshift: Optional[RedshiftConfiguration] = None
    glue: Optional[GlueConfiguration] = None
    athena: Optional[AthenaConfiguration] = None
    kinesis: Optional[KinesisConfiguration] = None
    lambda_: Optional[LambdaConfiguration] = None
    emr: Optional[EMRConfiguration] = None
    sagemaker: Optional[SageMakerConfiguration] = None


@dataclass
class AWSProviderOptions:
    """Comprehensive AWS provider configuration options."""

    # Basic configuration
    region: AWSRegion = AWSRegion.US_EAST_1
    account_id: Optional[str] = None

    # Authentication
    authentication: IAMConfiguration = field(default_factory=IAMConfiguration)
    auth_method: AuthenticationMethod = AuthenticationMethod.IAM_ROLE

    # Service configurations
    services: ServiceConfig = field(default_factory=ServiceConfig)

    # Security
    security: SecurityConfig = field(default_factory=SecurityConfig)

    # Monitoring and observability
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    # Cost optimization
    cost_optimization: CostOptimizationConfig = field(default_factory=CostOptimizationConfig)

    # Global tags
    tags: Dict[str, str] = field(default_factory=dict)

    # Multi-region support
    multi_region: Dict[str, "AWSProviderOptions"] = field(default_factory=dict)

    # Environment-specific overrides
    environment_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Advanced configurations
    connection_timeout: int = 60
    read_timeout: int = 60
    max_retries: int = 3
    retry_backoff_factor: float = 2.0

    # Feature flags
    enable_cost_monitoring: bool = True
    enable_security_scanning: bool = True
    enable_performance_monitoring: bool = True
    enable_compliance_checking: bool = True


@dataclass
class TableSpec:
    """AWS table specification for various services."""

    name: str
    service: str  # 'redshift', 'athena', 'dynamodb'
    columns: List[Dict[str, Any]] = field(default_factory=list)
    primary_keys: List[str] = field(default_factory=list)
    foreign_keys: List[Dict[str, Any]] = field(default_factory=list)
    indexes: List[Dict[str, Any]] = field(default_factory=list)
    distribution_key: Optional[str] = None
    sort_keys: List[str] = field(default_factory=list)
    partition_keys: List[str] = field(default_factory=list)
    compression: Optional[str] = None
    encryption_enabled: bool = True
    backup_enabled: bool = True
    point_in_time_recovery: bool = False
    tags: Dict[str, str] = field(default_factory=dict)
    lifecycle_policy: Optional[Dict[str, Any]] = None


@dataclass
class APISpec:
    """AWS API specification for API Gateway."""

    name: str
    description: str = ""
    protocol_type: str = "REST"  # REST, HTTP, WEBSOCKET
    cors_configuration: Optional[Dict[str, Any]] = None
    authentication: Optional[Dict[str, Any]] = None
    throttling: Optional[Dict[str, Any]] = None
    caching: Optional[Dict[str, Any]] = None
    logging: Optional[Dict[str, Any]] = None
    monitoring: Optional[Dict[str, Any]] = None
    custom_domain: Optional[Dict[str, Any]] = None
    stages: List[Dict[str, Any]] = field(default_factory=list)
    resources: List[Dict[str, Any]] = field(default_factory=list)
    authorizers: List[Dict[str, Any]] = field(default_factory=list)
    models: List[Dict[str, Any]] = field(default_factory=list)
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class StreamSpec:
    """AWS stream specification for Kinesis/MSK."""

    name: str
    service: str  # 'kinesis', 'msk', 'kinesis_firehose'
    shard_count: Optional[int] = None
    retention_period: int = 24
    encryption_enabled: bool = True
    compression_format: Optional[str] = None
    buffering_configuration: Optional[Dict[str, Any]] = None
    destination_configuration: Optional[Dict[str, Any]] = None
    error_output_prefix: Optional[str] = None
    processing_configuration: Optional[Dict[str, Any]] = None
    cloudwatch_logging_options: Optional[Dict[str, Any]] = None
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class ModelSpec:
    """AWS ML model specification for SageMaker."""

    name: str
    description: str = ""
    model_artifacts: str = ""
    inference_image: str = ""
    instance_type: str = "ml.t2.medium"
    instance_count: int = 1
    endpoint_config: Optional[Dict[str, Any]] = None
    auto_scaling: Optional[Dict[str, Any]] = None
    data_capture: Optional[Dict[str, Any]] = None
    explainability: Optional[Dict[str, Any]] = None
    bias_monitoring: Optional[Dict[str, Any]] = None
    model_quality_monitoring: Optional[Dict[str, Any]] = None
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class DeploymentMetadata:
    """Deployment metadata for tracking and rollback."""

    deployment_id: str
    timestamp: datetime
    strategy: str  # 'blue_green', 'rolling', 'canary'
    environment: str
    version: str
    rollback_enabled: bool = True
    health_checks: List[Dict[str, Any]] = field(default_factory=list)
    performance_baselines: Dict[str, Any] = field(default_factory=dict)
    cost_baselines: Dict[str, Any] = field(default_factory=dict)
    compliance_checks: List[Dict[str, Any]] = field(default_factory=list)


# Type aliases for convenience
AWSResource = Union[TableSpec, APISpec, StreamSpec, ModelSpec]
ConfigDict = Dict[str, Any]
TagDict = Dict[str, str]
