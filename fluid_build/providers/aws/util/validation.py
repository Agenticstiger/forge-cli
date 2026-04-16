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

# fluid_build/providers/aws/util/validation.py
"""
Resource validation framework for AWS provider.

Provides pre-flight validation checks to catch errors before execution:
- AWS naming conventions
- Resource quotas and limits
- Required permissions
- Dependency availability
"""

import re
from typing import Any, Dict, List, Optional, Tuple


class ValidationError(Exception):
    """Raised when resource validation fails."""

    pass


class ResourceValidator:
    """Validates AWS resource configurations before execution."""

    # AWS naming rules
    S3_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$")
    GLUE_DATABASE_PATTERN = re.compile(r"^[a-z0-9_]+$")
    GLUE_TABLE_PATTERN = re.compile(r"^[a-z0-9_]+$")
    LAMBDA_FUNCTION_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]+$")

    # AWS service quotas (defaults - can be increased)
    DEFAULT_QUOTAS = {
        "s3.buckets_per_account": 1000,
        "glue.databases_per_account": 10000,
        "glue.tables_per_database": 200000,
        "lambda.concurrent_executions": 1000,
        "athena.workgroups_per_account": 1000,
    }

    def __init__(self, account_id: str, region: str):
        """
        Initialize validator.

        Args:
            account_id: AWS account ID
            region: AWS region
        """
        self.account_id = account_id
        self.region = region
        self.quotas = self.DEFAULT_QUOTAS.copy()

    def validate_s3_bucket_name(self, bucket: str) -> Tuple[bool, Optional[str]]:
        """
        Validate S3 bucket name against AWS rules.

        Rules:
        - 3-63 characters
        - Lowercase letters, numbers, hyphens
        - Must start and end with letter or number
        - No consecutive hyphens
        - No IP address format

        Args:
            bucket: Bucket name to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not bucket:
            return False, "Bucket name cannot be empty"

        if len(bucket) < 3:
            return False, f"Bucket name must be at least 3 characters, got {len(bucket)}"

        if len(bucket) > 63:
            return False, f"Bucket name must be at most 63 characters, got {len(bucket)}"

        if not self.S3_BUCKET_PATTERN.match(bucket):
            return False, (
                f"Bucket name '{bucket}' is invalid. "
                "Must contain only lowercase letters, numbers, and hyphens. "
                "Must start and end with letter or number."
            )

        if "--" in bucket:
            return False, "Bucket name cannot contain consecutive hyphens"

        # Check for IP address format (e.g., 192.168.0.1)
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", bucket):
            return False, "Bucket name cannot be formatted as an IP address"

        # Check for reserved prefixes
        if bucket.startswith("xn--"):
            return False, "Bucket name cannot start with 'xn--'"

        if bucket.endswith("-s3alias"):
            return False, "Bucket name cannot end with '-s3alias'"

        return True, None

    def validate_glue_database_name(self, database: str) -> Tuple[bool, Optional[str]]:
        """
        Validate Glue database name.

        Rules:
        - 1-255 characters
        - Lowercase letters, numbers, underscores
        - No special characters or spaces

        Args:
            database: Database name to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not database:
            return False, "Database name cannot be empty"

        if len(database) < 1 or len(database) > 255:
            return False, f"Database name must be 1-255 characters, got {len(database)}"

        if not self.GLUE_DATABASE_PATTERN.match(database):
            return False, (
                f"Database name '{database}' is invalid. "
                "Must contain only lowercase letters, numbers, and underscores."
            )

        return True, None

    def validate_glue_table_name(self, table: str) -> Tuple[bool, Optional[str]]:
        """
        Validate Glue table name.

        Rules:
        - 1-255 characters
        - Lowercase letters, numbers, underscores
        - No special characters or spaces

        Args:
            table: Table name to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not table:
            return False, "Table name cannot be empty"

        if len(table) < 1 or len(table) > 255:
            return False, f"Table name must be 1-255 characters, got {len(table)}"

        if not self.GLUE_TABLE_PATTERN.match(table):
            return False, (
                f"Table name '{table}' is invalid. "
                "Must contain only lowercase letters, numbers, and underscores."
            )

        return True, None

    def validate_lambda_function_name(self, function: str) -> Tuple[bool, Optional[str]]:
        """
        Validate Lambda function name.

        Rules:
        - 1-64 characters
        - Letters, numbers, hyphens, underscores
        - No special characters

        Args:
            function: Function name to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not function:
            return False, "Function name cannot be empty"

        if len(function) < 1 or len(function) > 64:
            return False, f"Function name must be 1-64 characters, got {len(function)}"

        if not self.LAMBDA_FUNCTION_PATTERN.match(function):
            return False, (
                f"Function name '{function}' is invalid. "
                "Must contain only letters, numbers, hyphens, and underscores."
            )

        return True, None

    def validate_action(self, action: Dict[str, Any]) -> List[str]:
        """
        Validate a single action before execution.

        Args:
            action: Action to validate

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        op = action.get("op", "")

        if op == "s3.ensure_bucket":
            bucket = action.get("bucket")
            is_valid, error = self.validate_s3_bucket_name(bucket)
            if not is_valid:
                errors.append(f"[{op}] {error}")

        elif op == "glue.ensure_database":
            database = action.get("database")
            is_valid, error = self.validate_glue_database_name(database)
            if not is_valid:
                errors.append(f"[{op}] {error}")

        elif op in ["glue.ensure_table", "glue.ensure_iceberg_table"]:
            database = action.get("database")
            table = action.get("table")

            is_valid, error = self.validate_glue_database_name(database)
            if not is_valid:
                errors.append(f"[{op}] Database: {error}")

            is_valid, error = self.validate_glue_table_name(table)
            if not is_valid:
                errors.append(f"[{op}] Table: {error}")

        elif op == "lambda.ensure_function":
            function = action.get("function_name")
            is_valid, error = self.validate_lambda_function_name(function)
            if not is_valid:
                errors.append(f"[{op}] {error}")

        elif op == "athena.ensure_table":
            database = action.get("database")
            table = action.get("table")

            is_valid, error = self.validate_glue_database_name(database)
            if not is_valid:
                errors.append(f"[{op}] Database: {error}")

            is_valid, error = self.validate_glue_table_name(table)
            if not is_valid:
                errors.append(f"[{op}] Table: {error}")

        return errors

    def validate_actions(self, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Validate all actions before execution.

        Args:
            actions: List of actions to validate

        Returns:
            Validation result with errors and warnings
        """
        all_errors = []
        warnings = []

        # Track resource counts for quota validation
        resource_counts = {
            "s3_buckets": 0,
            "glue_databases": 0,
            "glue_tables": 0,
            "lambda_functions": 0,
        }

        for i, action in enumerate(actions):
            # Validate individual action
            errors = self.validate_action(action)
            for error in errors:
                all_errors.append(f"Action {i+1}: {error}")

            # Count resources
            op = action.get("op", "")
            if op == "s3.ensure_bucket":
                resource_counts["s3_buckets"] += 1
            elif op == "glue.ensure_database":
                resource_counts["glue_databases"] += 1
            elif op in ["glue.ensure_table", "glue.ensure_iceberg_table"]:
                resource_counts["glue_tables"] += 1
            elif op == "lambda.ensure_function":
                resource_counts["lambda_functions"] += 1

        # Check quotas
        if resource_counts["s3_buckets"] > 100:
            warnings.append(
                f"Creating {resource_counts['s3_buckets']} buckets. "
                f"AWS default quota is {self.quotas['s3.buckets_per_account']} per account."
            )

        if resource_counts["glue_tables"] > 1000:
            warnings.append(
                f"Creating {resource_counts['glue_tables']} tables. "
                "Consider consolidating into fewer tables for better performance."
            )

        return {
            "valid": len(all_errors) == 0,
            "errors": all_errors,
            "warnings": warnings,
            "resource_counts": resource_counts,
        }


def validate_actions_strict(actions: List[Dict[str, Any]], account_id: str, region: str) -> None:
    """
    Validate actions and raise exception if invalid.

    Args:
        actions: Actions to validate
        account_id: AWS account ID
        region: AWS region

    Raises:
        ValidationError: If validation fails
    """
    validator = ResourceValidator(account_id, region)
    result = validator.validate_actions(actions)

    if not result["valid"]:
        error_msg = "Action validation failed:\n" + "\n".join(result["errors"])
        raise ValidationError(error_msg)
