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

# fluid_build/provider/snowflake/types.py
"""
Enhanced type definitions for Snowflake provider with production features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AuthenticationType(Enum):
    """Supported authentication types."""

    PASSWORD = "password"
    KEY_PAIR = "key_pair"
    OAUTH = "oauth"
    SSO = "sso"
    EXTERNAL_BROWSER = "externalbrowser"


class DeploymentStrategy(Enum):
    """Deployment strategies."""

    DIRECT = "direct"
    BLUE_GREEN = "blue_green"
    ROLLING = "rolling"
    CANARY = "canary"


@dataclass(frozen=True)
class SnowflakeIdentifier:
    """Snowflake object identifier with validation."""

    database: str
    schema: str
    name: str

    def fqtn(self) -> str:
        """Fully-qualified table name with proper quoting."""

        def q(x: str) -> str:
            return f'"{x}"'

        return f"{q(self.database)}.{q(self.schema)}.{q(self.name)}"

    def validate(self) -> None:
        """Validate identifier components."""
        if not all([self.database, self.schema, self.name]):
            raise ValueError("All identifier components (database, schema, name) must be non-empty")

        # Check for valid Snowflake identifier characters
        for component in [self.database, self.schema, self.name]:
            if not self._is_valid_identifier(component):
                raise ValueError(f"Invalid Snowflake identifier: {component}")

    def _is_valid_identifier(self, identifier: str) -> bool:
        """Check if identifier is valid for Snowflake."""
        if not identifier:
            return False

        # Basic validation - can be enhanced with more rules
        invalid_chars = ["/", "\\", "?", "#", "[", "]", "@"]
        return not any(char in identifier for char in invalid_chars)


@dataclass
class TableColumn:
    """Enhanced table column definition."""

    name: str
    type: str
    nullable: bool = True
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    default_value: Optional[str] = None
    primary_key: bool = False
    unique: bool = False

    # Data classification and privacy
    data_classification: Optional[str] = None  # PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED
    pii_category: Optional[str] = None  # EMAIL, PHONE, SSN, etc.
    masking_policy: Optional[str] = None

    # Column-level constraints
    check_constraint: Optional[str] = None
    comment: Optional[str] = None

    def to_ddl_fragment(self) -> str:
        """Generate DDL fragment for this column."""
        parts = [f'"{self.name}" {self.type}']

        if not self.nullable:
            parts.append("NOT NULL")

        if self.default_value:
            parts.append(f"DEFAULT {self.default_value}")

        if self.check_constraint:
            parts.append(f"CHECK ({self.check_constraint})")

        if self.comment or self.description:
            comment_text = self.comment or self.description
            escaped_comment = comment_text.replace("'", "''")
            parts.append(f"COMMENT '{escaped_comment}'")

        return " ".join(parts)


@dataclass
class TableSpec:
    """Comprehensive table specification."""

    ident: SnowflakeIdentifier
    columns: List[TableColumn]

    # Table properties
    cluster_by: Optional[List[str]] = None
    partition_by: Optional[str] = None
    time_travel_retention: str = "1 days"
    change_tracking: bool = False
    search_optimization: bool = False
    materialization: str = "table"  # table, view, materialized_view

    # Security properties
    row_access_policy: Optional[str] = None
    column_masking_policies: Optional[Dict[str, str]] = None
    data_classification: Optional[Dict[str, str]] = None

    # Metadata
    tags: Optional[Dict[str, str]] = None
    comment: Optional[str] = None
    owner: Optional[str] = None

    # Performance optimization
    auto_clustering: bool = False
    compression: Optional[str] = None


@dataclass
class ProviderOptions:
    """Enhanced provider options with production features."""

    # Core connection settings
    account: str
    user: str
    password: Optional[str] = None
    private_key_path: Optional[str] = None
    private_key_passphrase: Optional[str] = None
    role: Optional[str] = None
    warehouse: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None

    # Authentication options
    oauth_token: Optional[str] = None
    authenticator: Optional[str] = None

    # Connection optimization
    connection_timeout: int = 60
    login_timeout: int = 30
    client_session_keep_alive: bool = True
    session_params: Optional[Dict[str, Any]] = None
    query_tag: str = "fluid-forge"

    # Connection pooling
    pool_size: int = 5
    max_overflow: int = 10

    # Feature flags
    enable_monitoring: bool = True
    enable_security_logs: bool = False
    enable_drift_detection: bool = True
    enable_cost_monitoring: bool = True
    enable_performance_monitoring: bool = True
    enable_backup_integration: bool = False

    # Environment configuration
    environment: str = "development"
    region: Optional[str] = None


@dataclass
class SecurityConfig:
    """Security configuration for Snowflake resources."""

    # Access control
    rbac_enabled: bool = True
    row_level_security: bool = False
    column_level_security: bool = False

    # Data protection
    encryption_at_rest: bool = True
    encryption_in_transit: bool = True
    data_masking_enabled: bool = False

    # Audit and compliance
    audit_logging: bool = True
    compliance_mode: Optional[str] = None  # SOX, PCI, HIPAA, GDPR

    # Network security
    network_policy: Optional[str] = None
    private_connectivity: bool = False

    # Authentication policies
    mfa_required: bool = False
    password_policy: Optional[str] = None
    session_timeout: int = 3600


@dataclass
class PerformanceConfig:
    """Performance optimization configuration."""

    # Warehouse settings
    auto_suspend: int = 60  # seconds
    auto_resume: bool = True
    min_cluster_count: int = 1
    max_cluster_count: int = 10
    scaling_policy: str = "STANDARD"  # STANDARD, ECONOMY

    # Query optimization
    query_acceleration: bool = False
    result_cache: bool = True
    warehouse_cache: bool = True
    metadata_cache: bool = True

    # Resource monitoring
    resource_monitor: Optional[str] = None
    credit_quota: Optional[int] = None

    # Performance thresholds
    slow_query_threshold: int = 60  # seconds
    expensive_query_threshold: int = 100  # credits


@dataclass
class MonitoringConfig:
    """Monitoring and alerting configuration."""

    # Metrics collection
    collect_performance_metrics: bool = True
    collect_cost_metrics: bool = True
    collect_security_metrics: bool = True
    collect_data_quality_metrics: bool = True

    # Alerting
    alert_on_failures: bool = True
    alert_on_performance_degradation: bool = True
    alert_on_cost_overrun: bool = True
    alert_on_security_events: bool = True

    # Integration settings
    slack_webhook: Optional[str] = None
    pagerduty_integration_key: Optional[str] = None
    datadog_api_key: Optional[str] = None

    # Retention settings
    metrics_retention_days: int = 30
    logs_retention_days: int = 90


@dataclass
class BackupConfig:
    """Backup and disaster recovery configuration."""

    # Time Travel settings
    time_travel_retention: int = 1  # days
    fail_safe_period: int = 7  # days

    # Backup strategy
    automated_backups: bool = True
    backup_frequency: str = "daily"  # hourly, daily, weekly
    backup_retention: int = 30  # days

    # Cross-region replication
    cross_region_backup: bool = False
    backup_regions: Optional[List[str]] = None

    # Recovery settings
    rto_minutes: int = 60  # Recovery Time Objective
    rpo_minutes: int = 15  # Recovery Point Objective


@dataclass
class DeploymentConfig:
    """Deployment configuration."""

    strategy: DeploymentStrategy = DeploymentStrategy.DIRECT

    # Blue-green deployment
    clone_for_testing: bool = False
    validation_queries: Optional[List[str]] = None

    # Rollback configuration
    auto_rollback: bool = False
    rollback_triggers: Optional[List[str]] = None

    # Health checks
    health_check_enabled: bool = True
    health_check_queries: Optional[List[str]] = None
    health_check_timeout: int = 30

    # Deployment validation
    validate_schema: bool = True
    validate_data_quality: bool = True
    validate_performance: bool = True

    # Approval workflow
    approval_required: bool = False
    approvers: Optional[List[str]] = None


@dataclass
class ContractMetadata:
    """Enhanced contract metadata."""

    # Basic metadata
    version: str
    created_at: str
    updated_at: str
    created_by: str

    # Ownership
    owner_team: str
    owner_email: str
    business_owner: Optional[str] = None

    # Classification
    data_classification: str = "internal"  # public, internal, confidential, restricted
    data_sensitivity: str = "medium"  # low, medium, high, critical

    # Compliance
    compliance_tags: Optional[List[str]] = None
    retention_policy: Optional[str] = None

    # Operational
    sla_tier: str = "standard"  # bronze, silver, gold, platinum
    support_level: str = "standard"  # basic, standard, premium, enterprise

    # Dependencies
    upstream_dependencies: Optional[List[str]] = None
    downstream_dependencies: Optional[List[str]] = None


@dataclass
class ValidationResult:
    """Result of validation operations."""

    valid: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    def add_warning(self, message: str):
        """Add a warning message."""
        self.warnings.append(message)

    def add_error(self, message: str):
        """Add an error message."""
        self.errors.append(message)
        self.valid = False

    def add_info(self, message: str):
        """Add an info message."""
        self.info.append(message)


@dataclass
class OperationResult:
    """Result of provider operations."""

    success: bool
    operation: str
    duration: float
    message: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    errors: Optional[List[str]] = None

    # Resource tracking
    resources_created: int = 0
    resources_updated: int = 0
    resources_deleted: int = 0

    # Cost tracking
    credits_consumed: float = 0.0
    estimated_cost: float = 0.0
