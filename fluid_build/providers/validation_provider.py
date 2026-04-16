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
Provider abstraction for contract validation.

This module defines the interface that all validation providers must implement,
enabling extensible validation across different cloud platforms and data systems.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class ResourceType(Enum):
    """Types of resources that can be validated"""

    TABLE = "table"
    VIEW = "view"
    DATASET = "dataset"
    WAREHOUSE = "warehouse"
    DATABASE = "database"
    SCHEMA = "schema"


@dataclass
class FieldSchema:
    """Schema definition for a field/column"""

    name: str
    type: str
    mode: str = "NULLABLE"
    description: Optional[str] = None

    def __eq__(self, other):
        if not isinstance(other, FieldSchema):
            return False
        return (
            self.name.lower() == other.name.lower()
            and self.type.upper() == other.type.upper()
            and self.mode.upper() == other.mode.upper()
        )


@dataclass
class ResourceSchema:
    """Complete schema for a resource"""

    resource_type: ResourceType
    fully_qualified_name: str
    fields: List[FieldSchema]
    row_count: Optional[int] = None
    size_bytes: Optional[int] = None
    last_modified: Optional[str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class ValidationIssue:
    """Represents a validation issue found during validation"""

    severity: str  # "error", "warning", "info"
    category: str  # "missing_resource", "schema_mismatch", "type_mismatch", etc.
    message: str
    path: str  # Path in contract where issue was found
    expected: Optional[Any] = None
    actual: Optional[Any] = None
    suggestion: Optional[str] = None
    documentation_url: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of validating a single resource"""

    resource_name: str
    success: bool
    issues: List[ValidationIssue]
    schema: Optional[ResourceSchema] = None

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(issue.severity == "warning" for issue in self.issues)


class ValidationProvider(ABC):
    """
    Abstract base class for all validation providers.

    Each cloud platform (GCP, Snowflake, AWS, Azure, etc.) should implement
    this interface to enable contract validation for their specific platform.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the provider with configuration.

        Args:
            config: Provider-specific configuration (project_id, account, etc.)
        """
        self.config = config

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the name of this provider (e.g., 'gcp', 'snowflake')"""
        pass

    @abstractmethod
    def validate_connection(self) -> bool:
        """
        Test connection to the provider.

        Returns:
            True if connection is successful, False otherwise
        """
        pass

    @abstractmethod
    def get_resource_schema(self, resource_spec: Dict[str, Any]) -> Optional[ResourceSchema]:
        """
        Retrieve the actual schema of a resource from the provider.

        Args:
            resource_spec: Resource specification from the contract

        Returns:
            ResourceSchema if resource exists, None if not found

        Raises:
            Exception: If there's an error accessing the provider
        """
        pass

    @abstractmethod
    def validate_resource(
        self, contract_spec: Dict[str, Any], actual_schema: Optional[ResourceSchema]
    ) -> ValidationResult:
        """
        Validate a resource against its contract specification.

        Args:
            contract_spec: Expected resource specification from contract
            actual_schema: Actual schema retrieved from provider (or None if not found)

        Returns:
            ValidationResult containing all validation issues
        """
        pass

    def normalize_type(self, provider_type: str) -> str:
        """
        Normalize provider-specific type to standard type.

        Args:
            provider_type: Provider-specific type name

        Returns:
            Normalized type name
        """
        # Common type aliases - override for provider-specific mappings
        type_upper = provider_type.upper()

        # Handle common aliases
        aliases = {
            "VARCHAR": "STRING",
            "TEXT": "STRING",
            "INT": "INTEGER",
            "BIGINT": "INTEGER",
            "SMALLINT": "INTEGER",
            "BOOL": "BOOLEAN",
            "DOUBLE": "FLOAT",
        }

        return aliases.get(type_upper, type_upper)

    def compare_schemas(
        self, expected: List[FieldSchema], actual: List[FieldSchema]
    ) -> List[ValidationIssue]:
        """
        Compare expected and actual schemas, returning validation issues.

        Args:
            expected: Expected field schemas from contract
            actual: Actual field schemas from provider

        Returns:
            List of validation issues found
        """
        issues = []

        # Create lookup maps
        expected_map = {f.name.lower(): f for f in expected}
        actual_map = {f.name.lower(): f for f in actual}

        # Check for missing fields
        for field_name, expected_field in expected_map.items():
            if field_name not in actual_map:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="missing_field",
                        message=f"Field '{expected_field.name}' is missing from actual schema",
                        path=f"fields.{expected_field.name}",
                        expected=expected_field.name,
                        actual=None,
                        suggestion=f"Add field '{expected_field.name}' of type {expected_field.type} to the table",
                    )
                )
            else:
                # Check field properties
                actual_field = actual_map[field_name]

                # Type mismatch
                if self.normalize_type(expected_field.type) != self.normalize_type(
                    actual_field.type
                ):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="type_mismatch",
                            message=f"Field '{expected_field.name}' has incorrect type",
                            path=f"fields.{expected_field.name}.type",
                            expected=expected_field.type,
                            actual=actual_field.type,
                            suggestion=f"Change field type from {actual_field.type} to {expected_field.type}",
                        )
                    )

                # Mode mismatch (nullable vs required)
                if expected_field.mode.upper() != actual_field.mode.upper():
                    severity = "warning"  # Mode mismatches are typically warnings
                    issues.append(
                        ValidationIssue(
                            severity=severity,
                            category="mode_mismatch",
                            message=f"Field '{expected_field.name}' has incorrect mode",
                            path=f"fields.{expected_field.name}.mode",
                            expected=expected_field.mode,
                            actual=actual_field.mode,
                            suggestion=f"Change field mode from {actual_field.mode} to {expected_field.mode}",
                        )
                    )

        # Check for extra fields (info level)
        for field_name, actual_field in actual_map.items():
            if field_name not in expected_map:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="extra_field",
                        message=f"Field '{actual_field.name}' exists in actual schema but not in contract",
                        path=f"fields.{actual_field.name}",
                        expected=None,
                        actual=actual_field.name,
                        suggestion=f"Consider adding field '{actual_field.name}' to the contract if it's part of the API",
                    )
                )

        return issues

    def create_resource_identifier(self, resource_spec: Dict[str, Any]) -> str:
        """
        Create a fully qualified resource identifier from resource spec.

        Args:
            resource_spec: Resource specification from contract

        Returns:
            Fully qualified resource name
        """
        # Default implementation - override for provider-specific formatting
        binding = resource_spec.get("binding", {})
        resource = binding.get("resource", {})

        if isinstance(resource, str):
            return resource

        # Try common patterns
        dataset = resource.get("dataset") or resource.get("database") or resource.get("schema")
        table = resource.get("table") or resource.get("view")

        if dataset and table:
            return f"{dataset}.{table}"

        return resource.get("name", "unknown")

    def run_quality_checks(
        self,
        resource_spec: Dict[str, Any],
        rules: List[Dict[str, Any]],
    ) -> List["ValidationIssue"]:
        """
        Execute data quality rules against a live resource.

        Override in subclasses that support SQL-based quality checks.
        The default implementation returns a warning that DQ checks are
        not supported by this provider.

        Args:
            resource_spec: Expose spec from the contract.
            rules: List of DQ rule dicts from ``contract.dq.rules``.

        Returns:
            List of ValidationIssue for any failing checks.
        """
        return [
            ValidationIssue(
                severity="info",
                category="quality",
                message=f"Quality check execution not supported by '{self.provider_name}' provider",
                path="contract.dq.rules",
            )
        ]
