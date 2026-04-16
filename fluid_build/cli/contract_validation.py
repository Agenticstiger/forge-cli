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
FLUID Contract Validation Command

Validates that exposed data products match their contract specifications.
Ensures compliance with the FLUID specification by verifying:
- Schema consistency between contract and actual data
- Data types and field definitions
- Quality metrics and SLAs
- Access patterns and bindings
- Provider-specific configurations

This command connects to the actual deployed resources (BigQuery, Snowflake, etc.)
and validates that they match what's declared in the FLUID contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

# Rich imports for enhanced output
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from ..providers.validation_cache import ValidationCache, ValidationResultHistory
from ..providers.validation_provider import ValidationProvider
from ..schema_manager import FluidSchemaManager
from ._common import load_contract_with_overlay

try:
    from ..providers.bigquery_validation import BigQueryValidationProvider

    BIGQUERY_AVAILABLE = True
except ImportError:
    BigQueryValidationProvider = None  # type: ignore[assignment,misc]
    BIGQUERY_AVAILABLE = False

try:
    from ..providers.snowflake_validation import SnowflakeValidationProvider

    SNOWFLAKE_VALIDATION_AVAILABLE = True
except ImportError:
    SnowflakeValidationProvider = None  # type: ignore[assignment,misc]
    SNOWFLAKE_VALIDATION_AVAILABLE = False

try:
    from ..providers.aws_validation import AWSValidationProvider

    AWS_VALIDATION_AVAILABLE = True
except ImportError:
    AWSValidationProvider = None  # type: ignore[assignment,misc]
    AWS_VALIDATION_AVAILABLE = False

try:
    from ..providers.local_validation import LocalValidationProvider

    LOCAL_VALIDATION_AVAILABLE = True
except ImportError:
    LocalValidationProvider = None  # type: ignore[assignment,misc]
    LOCAL_VALIDATION_AVAILABLE = False

LOG = logging.getLogger("fluid.cli.contract_validation")
COMMAND = "contract-validation"


@dataclass
class ValidationIssue:
    """Represents a validation issue found during contract validation."""

    severity: str  # 'error', 'warning', 'info'
    category: str  # 'schema', 'binding', 'quality', 'metadata'
    message: str
    path: str
    expected: Optional[Any] = None
    actual: Optional[Any] = None
    suggestion: Optional[str] = None
    documentation_url: Optional[str] = None

    def __str__(self) -> str:
        result = f"[{self.severity.upper()}] {self.category}: {self.message}"
        if self.path:
            result += f"\n  Path: {self.path}"
        if self.expected is not None:
            result += f"\n  Expected: {self.expected}"
        if self.actual is not None:
            result += f"\n  Actual: {self.actual}"
        if self.suggestion:
            result += f"\n  💡 Suggestion: {self.suggestion}"
        if self.documentation_url:
            result += f"\n  📚 Docs: {self.documentation_url}"
        return result


@dataclass
class ValidationReport:
    """Complete validation report for a FLUID contract."""

    contract_path: str
    contract_id: str
    contract_version: str
    validation_time: datetime
    duration: float
    issues: List[ValidationIssue] = field(default_factory=list)
    exposes_validated: int = 0
    consumes_validated: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    provider_name: Optional[str] = None

    def add_issue(
        self,
        severity: str,
        category: str,
        message: str,
        path: str = "",
        expected: Any = None,
        actual: Any = None,
        suggestion: Optional[str] = None,
        documentation_url: Optional[str] = None,
    ) -> None:
        """Add a validation issue to the report."""
        issue = ValidationIssue(
            severity=severity,
            category=category,
            message=message,
            path=path,
            expected=expected,
            actual=actual,
            suggestion=suggestion,
            documentation_url=documentation_url,
        )
        self.issues.append(issue)
        if severity == "error":
            self.checks_failed += 1
        else:
            self.checks_passed += 1

    def get_errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def get_warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def is_valid(self) -> bool:
        return len(self.get_errors()) == 0

    def get_summary(self) -> str:
        """Get a human-readable summary of the validation."""
        status = "✅ VALID" if self.is_valid() else "❌ INVALID"
        errors = len(self.get_errors())
        warnings = len(self.get_warnings())

        summary = f"{status}: {self.contract_id} v{self.contract_version}\n"
        summary += f"Validated {self.exposes_validated} exposed data product(s)\n"
        summary += f"Validated {self.consumes_validated} consumed data product(s)\n"
        summary += f"{self.checks_passed} checks passed, {self.checks_failed} checks failed\n"
        summary += f"{errors} error(s), {warnings} warning(s)\n"
        summary += f"Completed in {self.duration:.2f}s"
        return summary


class ContractValidator:
    """Validates FLUID contracts against actual deployed resources."""

    def __init__(
        self,
        contract_path: Path,
        env: Optional[str] = None,
        provider_name: Optional[str] = None,
        project: Optional[str] = None,
        region: Optional[str] = None,
        strict: bool = False,
        check_data: bool = True,
        use_cache: bool = True,
        cache_ttl: int = 3600,
        cache_clear: bool = False,
        track_history: bool = True,
        check_drift: bool = False,
        server: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.contract_path = contract_path
        self.env = env
        self.provider_name = provider_name
        self.project = project
        self.region = region
        self.server = server
        self.strict = strict
        self.check_data = check_data
        self.logger = logger or LOG
        self.contract: Optional[Dict[str, Any]] = None
        self.report: Optional[ValidationReport] = None

        # Initialize caching
        self.use_cache = use_cache
        self.cache = ValidationCache(ttl=cache_ttl) if use_cache else None
        if cache_clear and self.cache:
            self.cache.clear()
            self.logger.info("Cache cleared")

        # Initialize history tracking
        self.track_history = track_history
        self.check_drift = check_drift
        self.history = ValidationResultHistory() if track_history else None

        # Provider instance
        self.validation_provider: Optional[ValidationProvider] = None

    def validate(self) -> ValidationReport:
        """Execute comprehensive contract validation."""
        start_time = time.time()

        # Load contract
        try:
            self.contract = load_contract_with_overlay(
                str(self.contract_path), self.env, self.logger
            )
        except FileNotFoundError:
            from .core import ContractNotFoundError

            raise ContractNotFoundError(str(self.contract_path))
        except Exception as e:
            LOG.error(f"Failed to load contract: {e}")
            from .core import ContractLoadError

            raise ContractLoadError(str(self.contract_path), str(e))

        # Initialize report
        contract_id = self.contract.get("id", "unknown")
        contract_version = self.contract.get("version", "0.0.0")

        self.report = ValidationReport(
            contract_path=str(self.contract_path),
            contract_id=contract_id,
            contract_version=contract_version,
            validation_time=datetime.now(),
            duration=0.0,
        )

        # Step 1: Validate contract syntax against schema
        self._validate_contract_schema()

        # Step 2: Detect and validate provider
        self._detect_and_validate_provider()

        # Step 3: Validate exposed data products
        self._validate_exposes()

        # Step 4: Validate consumed data products (references)
        self._validate_consumes()

        # Step 5: Validate quality metrics and SLAs
        self._validate_quality_specs()

        # Step 6: Validate metadata and governance
        self._validate_metadata()

        # Finalize report
        self.report.duration = time.time() - start_time

        return self.report

    def _validate_contract_schema(self) -> None:
        """Validate contract against FLUID schema."""
        LOG.info("Validating contract against FLUID schema...")

        try:
            schema_manager = FluidSchemaManager()
            result = schema_manager.validate_contract(self.contract)

            if not result.is_valid:
                for err in result.errors:
                    self.report.add_issue("error", "schema", err, "")

            for warn in result.warnings:
                self.report.add_issue("warning", "schema", warn, "")

        except Exception as e:
            self.report.add_issue("error", "schema", f"Schema validation failed: {e}", "")

    def _detect_and_validate_provider(self) -> None:
        """Detect provider from contract and validate configuration."""
        LOG.info("Detecting and validating provider configuration...")

        # Try to detect provider from exposes
        detected_provider = None
        detected_project = None

        for expose in self.contract.get("exposes", []):
            binding = expose.get("binding", {})
            if "platform" in binding:
                detected_provider = binding["platform"]
                location = binding.get("location", {})
                detected_project = location.get("project") or location.get("properties", {}).get(
                    "project"
                )
                break

        # Fallback to builds section
        if not detected_provider:
            for build in self.contract.get("builds", []):
                execution = build.get("execution", {})
                runtime = execution.get("runtime", {})
                if "platform" in runtime:
                    detected_provider = runtime["platform"]
                    break

        if not detected_provider:
            self.report.add_issue(
                "warning",
                "binding",
                "No provider platform detected in contract",
                "exposes[].binding.platform or builds[].execution.runtime.platform",
            )
            return

        # Use detected or override with CLI args
        self.provider_name = self.provider_name or detected_provider
        self.project = self.project or detected_project

        LOG.info(f"Detected provider: {self.provider_name}, project: {self.project}")
        self.report.provider_name = self.provider_name

        # Instantiate the appropriate validation provider
        provider_config = {"project_id": self.project, "region": self.region}

        if self.provider_name == "gcp":
            if not BIGQUERY_AVAILABLE:
                self.report.add_issue(
                    "error",
                    "binding",
                    "google-cloud-bigquery package is required for GCP validation",
                    "binding.platform",
                    suggestion="Install it with: pip install google-cloud-bigquery",
                )
                return
            # --server can override the GCP project
            if self.server:
                provider_config["project_id"] = self.server
            self.validation_provider = BigQueryValidationProvider(provider_config)
        elif self.provider_name == "snowflake":
            if not SNOWFLAKE_VALIDATION_AVAILABLE:
                self.report.add_issue(
                    "error",
                    "binding",
                    "snowflake-connector-python package is required for Snowflake validation",
                    "binding.platform",
                    suggestion="Install it with: pip install snowflake-connector-python",
                )
                return
            # Build Snowflake config from contract bindings + CLI overrides
            sf_config = self._build_snowflake_config()
            self.validation_provider = SnowflakeValidationProvider(sf_config)
        elif self.provider_name == "aws":
            if not AWS_VALIDATION_AVAILABLE:
                self.report.add_issue(
                    "error",
                    "binding",
                    "boto3 package is required for AWS validation",
                    "binding.platform",
                    suggestion="Install it with: pip install boto3",
                )
                return
            aws_config = {"region": self.region or "us-east-1"}
            self.validation_provider = AWSValidationProvider(aws_config)
        elif self.provider_name == "local":
            if not LOCAL_VALIDATION_AVAILABLE:
                self.report.add_issue(
                    "error",
                    "binding",
                    "duckdb package is required for local validation",
                    "binding.platform",
                    suggestion="Install it with: pip install duckdb",
                )
                return
            base_dir = str(self.contract_path.parent) if self.contract_path else "."
            self.validation_provider = LocalValidationProvider({"base_dir": base_dir})
        elif self.provider_name in ["databricks", "azure"]:
            self.report.add_issue(
                "warning",
                "binding",
                f"Provider '{self.provider_name}' validation not yet implemented",
                "binding.platform",
                suggestion="Supported providers: gcp, snowflake, aws, local. Others coming soon.",
            )
            return
        else:
            supported = ["gcp", "snowflake", "aws", "local", "databricks", "azure"]
            self.report.add_issue(
                "error",
                "binding",
                f"Unknown provider '{self.provider_name}'",
                "binding.platform",
                suggestion=f"Valid providers are: {', '.join(supported)}",
                documentation_url="https://docs.fluid.io/providers",
            )
            return

        # Test provider connection
        try:
            if not self.validation_provider.validate_connection():
                self.report.add_issue(
                    "error",
                    "connection",
                    f"Failed to connect to {self.provider_name}",
                    "binding.platform",
                    suggestion="Check your credentials and network connectivity. For GCP, ensure GOOGLE_APPLICATION_CREDENTIALS is set or run 'gcloud auth application-default login'",
                    documentation_url="https://docs.fluid.io/validation/authentication",
                )
        except Exception as e:
            self.report.add_issue(
                "error",
                "connection",
                f"Error connecting to {self.provider_name}: {str(e)}",
                "binding.platform",
                suggestion="Verify provider credentials are configured correctly and you have necessary permissions",
                documentation_url="https://docs.fluid.io/validation/troubleshooting",
            )

    def _validate_exposes(self) -> None:
        """Validate all exposed data products against actual resources."""
        LOG.info("Validating exposed data products...")

        exposes = self.contract.get("exposes", [])
        if not exposes:
            self.report.add_issue(
                "warning", "metadata", "No data products exposed in contract", "exposes"
            )
            return

        for idx, expose in enumerate(exposes):
            self._validate_single_expose(expose, idx)
            self.report.exposes_validated += 1

    def _validate_single_expose(self, expose: Dict[str, Any], idx: int) -> None:
        """Validate a single exposed data product."""
        # Support FLUID DSL aliases: exposeId -> id, kind -> type, contract.schema -> schema
        if "id" not in expose and "exposeId" in expose:
            expose = dict(expose, id=expose["exposeId"])
        if "type" not in expose and "kind" in expose:
            expose = dict(expose, type=expose["kind"])
        if (
            "schema" not in expose
            and "contract" in expose
            and "schema" in expose.get("contract", {})
        ):
            expose = dict(expose, schema=expose["contract"]["schema"])

        expose_id = expose.get("id", f"expose_{idx}")
        path_prefix = f"exposes[{idx}]"

        LOG.debug(f"Validating expose: {expose_id}")

        # Validate required fields
        required_fields = ["id", "type", "binding", "schema"]
        for field in required_fields:
            if field not in expose:
                self.report.add_issue(
                    "error",
                    "schema",
                    f"Missing required field '{field}' in exposed data product",
                    f"{path_prefix}.{field}",
                )

        # Validate binding
        binding = expose.get("binding", {})
        if binding:
            self._validate_binding(binding, f"{path_prefix}.binding", expose_id)

        # Validate schema definition
        schema = expose.get("schema", [])
        if schema:
            self._validate_schema_definition(schema, f"{path_prefix}.schema", expose_id)

        # Run data-quality checks if DQ rules defined and provider available
        dq_rules = expose.get("dq", {}).get("rules", [])
        # Also check nested contract.dq.rules pattern
        if not dq_rules:
            dq_rules = expose.get("contract", {}).get("dq", {}).get("rules", [])
        if dq_rules and self.validation_provider and self.check_data:
            self._run_expose_quality_checks(expose, dq_rules, path_prefix)

        # Validate against actual resource (if provider available)
        if self.check_data and self.provider_name:
            self._validate_against_actual_resource(expose, path_prefix)

    def _validate_binding(self, binding: Dict[str, Any], path: str, expose_id: str) -> None:
        """Validate binding configuration."""
        # Check required binding fields
        if "platform" not in binding:
            self.report.add_issue(
                "error", "binding", f"Missing 'platform' in binding for {expose_id}", path
            )

        if "location" not in binding:
            self.report.add_issue(
                "error", "binding", f"Missing 'location' in binding for {expose_id}", path
            )
        else:
            location = binding["location"]

            # Validate location has required fields
            if "format" not in location:
                self.report.add_issue(
                    "warning",
                    "binding",
                    f"Missing 'format' in location for {expose_id}",
                    f"{path}.location",
                )

            if "properties" not in location:
                self.report.add_issue(
                    "warning",
                    "binding",
                    f"Missing 'properties' in location for {expose_id}",
                    f"{path}.location",
                )
            else:
                props = location["properties"]
                # Provider-specific validation
                if self.provider_name == "gcp":
                    required_props = ["project", "dataset", "table"]
                    for prop in required_props:
                        if prop not in props:
                            self.report.add_issue(
                                "error",
                                "binding",
                                f"Missing required property '{prop}' for GCP binding",
                                f"{path}.location.properties.{prop}",
                            )

    def _run_expose_quality_checks(
        self,
        expose: Dict[str, Any],
        rules: List[Dict[str, Any]],
        path_prefix: str,
    ) -> None:
        """Execute data-quality rules against the live resource via the provider."""
        expose_id = expose.get("id", "unknown")
        LOG.debug("Running %d quality checks for %s", len(rules), expose_id)
        try:
            issues = self.validation_provider.run_quality_checks(expose, rules)
            for issue in issues:
                # Prefix the issue path with the expose path
                issue_path = f"{path_prefix}.dq.{issue.path}" if issue.path else f"{path_prefix}.dq"
                self.report.add_issue(issue.severity, issue.category, issue.message, issue_path)
        except Exception as exc:
            LOG.warning("Quality check execution failed for %s: %s", expose_id, exc)
            self.report.add_issue(
                "warning",
                "quality",
                f"Quality check execution failed: {exc}",
                f"{path_prefix}.dq",
            )

    def _validate_schema_definition(
        self, schema: List[Dict[str, Any]], path: str, expose_id: str
    ) -> None:
        """Validate schema definition structure."""
        if not schema:
            self.report.add_issue("warning", "schema", f"Empty schema for {expose_id}", path)
            return

        for idx, column in enumerate(schema):
            col_path = f"{path}[{idx}]"

            # Validate column has required fields
            if "name" not in column:
                self.report.add_issue(
                    "error", "schema", "Missing 'name' in column definition", col_path
                )

            if "type" not in column:
                self.report.add_issue(
                    "error", "schema", "Missing 'type' in column definition", col_path
                )

            # Validate column type is valid
            valid_types = [
                "VARCHAR",
                "STRING",
                "TEXT",
                "INT",
                "INTEGER",
                "BIGINT",
                "SMALLINT",
                "FLOAT",
                "DOUBLE",
                "DECIMAL",
                "NUMERIC",
                "BOOL",
                "BOOLEAN",
                "DATE",
                "DATETIME",
                "TIMESTAMP",
                "TIME",
                "JSON",
                "JSONB",
                "ARRAY",
                "STRUCT",
                "BYTES",
                "BINARY",
            ]

            col_type = column.get("type", "").upper()
            if col_type and col_type not in valid_types:
                self.report.add_issue(
                    "warning",
                    "schema",
                    f"Non-standard column type '{col_type}' in {expose_id}",
                    f"{col_path}.type",
                )

    def _validate_against_actual_resource(self, expose: Dict[str, Any], path: str) -> None:
        """Validate contract schema against actual deployed resource."""
        expose_id = expose.get("id") or expose.get("exposeId") or "unknown"

        LOG.debug("Validating actual resource for %s", expose_id)

        # Provider-specific validation
        if self.provider_name == "gcp":
            binding = expose.get("binding", {})
            location = binding.get("location", {})
            props = location.get("properties", {})
            self._validate_bigquery_resource(expose, path, props)
        elif self.provider_name in ("snowflake", "aws", "local"):
            self._validate_generic_resource(expose, path)
        else:
            LOG.debug("Provider '%s' resource validation not yet implemented", self.provider_name)

    def _validate_bigquery_resource(
        self, expose: Dict[str, Any], path: str, props: Dict[str, Any]
    ) -> None:
        """Validate against actual BigQuery table using provider abstraction."""
        if not self.validation_provider:
            self.report.add_issue(
                "warning",
                "binding",
                "No validation provider initialized, skipping resource validation",
                path,
            )
            return

        project = props.get("project", self.project)
        dataset = props.get("dataset")
        table = props.get("table")

        if not all([project, dataset, table]):
            missing = []
            if not project:
                missing.append("project")
            if not dataset:
                missing.append("dataset")
            if not table:
                missing.append("table")

            self.report.add_issue(
                "error",
                "binding",
                f"Incomplete BigQuery location: missing {', '.join(missing)}",
                f"{path}.binding.location.properties",
                suggestion=f"Add the following properties to binding.location.properties: {', '.join(missing)}",
                documentation_url="https://docs.fluid.io/contracts/bindings/bigquery",
            )
            return

        table_fqn = f"{project}.{dataset}.{table}"

        try:
            # Try to get schema from cache first
            actual_schema = None
            if self.cache:
                actual_schema = self.cache.get_schema(table_fqn, self.provider_name)
                if actual_schema:
                    LOG.info(f"✨ Using cached schema for {table_fqn}")

            # If not cached, fetch from provider
            if actual_schema is None:
                actual_schema = self.validation_provider.get_resource_schema(expose)

                # Cache the result if caching is enabled
                if self.cache and actual_schema:
                    self.cache.set_schema(table_fqn, self.provider_name, actual_schema)

            # Validate the resource using the provider
            result = self.validation_provider.validate_resource(expose, actual_schema)

            # Convert provider validation issues to report issues
            for issue in result.issues:
                # Enhanced error messages with suggestions and documentation
                self.report.add_issue(
                    severity=issue.severity,
                    category=issue.category,
                    message=issue.message,
                    path=issue.path or path,
                    expected=issue.expected,
                    actual=issue.actual,
                    suggestion=issue.suggestion,
                    documentation_url=issue.documentation_url,
                )

            # Track history if enabled
            if self.history:
                try:
                    self.history.record_validation(
                        contract_path=str(self.contract_path),
                        result=result,
                        provider=self.provider_name,
                    )
                except Exception as e:
                    LOG.warning(f"Failed to record validation history: {e}")

            # Check for drift if requested
            if self.check_drift and self.history:
                resource_name = expose.get("id") or expose.get("name") or table
                try:
                    drift_result = self.history.detect_drift(
                        contract_path=str(self.contract_path), resource_name=resource_name
                    )

                    if drift_result["drift_detected"]:
                        drift_type = drift_result.get("type", "unknown")
                        drift_msg = drift_result["message"]

                        # Add detailed drift information
                        if drift_type == "degradation":
                            suggestion = f"Investigate why error count increased from {drift_result.get('previous_errors', 0)} to {drift_result.get('current_errors', 0)}"
                        elif drift_type == "new_issues":
                            new_cats = drift_result.get("new_categories", [])
                            suggestion = f"New issue categories detected: {', '.join(new_cats)}. Review recent schema changes."
                        else:
                            suggestion = "Review validation history to understand the drift"

                        self.report.add_issue(
                            "warning",
                            "drift",
                            f"Validation drift detected: {drift_msg}",
                            path,
                            suggestion=suggestion,
                            documentation_url="https://docs.fluid.io/validation/drift-detection",
                        )
                        LOG.warning(f"⚠️  Drift detected for {resource_name}: {drift_msg}")
                except Exception as e:
                    LOG.warning(f"Failed to check drift: {e}")

            # Log success
            if result.success:
                LOG.info(f"✅ Resource '{table_fqn}' validated successfully")
            else:
                LOG.warning(f"⚠️  Resource '{table_fqn}' validation found issues")

        except Exception as e:
            self.report.add_issue(
                "error",
                "binding",
                f"Failed to validate resource: {str(e)}",
                path,
                suggestion="Check provider credentials and network connectivity",
                documentation_url="https://docs.fluid.io/validation/troubleshooting",
            )

    def _validate_generic_resource(self, expose: Dict[str, Any], path: str) -> None:
        """Validate a resource using the active validation provider (Snowflake, AWS, local)."""
        if not self.validation_provider:
            self.report.add_issue(
                "warning",
                "binding",
                "No validation provider initialized, skipping resource validation",
                path,
            )
            return

        expose_id = expose.get("id") or expose.get("exposeId") or "unknown"

        try:
            # Cache lookup
            actual_schema = None
            cache_key = f"{self.provider_name}:{expose_id}"
            if self.cache:
                actual_schema = self.cache.get_schema(cache_key, self.provider_name)
                if actual_schema:
                    LOG.info("Using cached schema for %s", cache_key)

            # Fetch from provider
            if actual_schema is None:
                actual_schema = self.validation_provider.get_resource_schema(expose)
                if self.cache and actual_schema:
                    self.cache.set_schema(cache_key, self.provider_name, actual_schema)

            # Validate
            result = self.validation_provider.validate_resource(expose, actual_schema)

            for issue in result.issues:
                self.report.add_issue(
                    severity=issue.severity,
                    category=issue.category,
                    message=issue.message,
                    path=issue.path or path,
                    expected=issue.expected,
                    actual=issue.actual,
                    suggestion=issue.suggestion,
                    documentation_url=issue.documentation_url,
                )

            # History tracking
            if self.history:
                try:
                    self.history.record_validation(
                        contract_path=str(self.contract_path),
                        result=result,
                        provider=self.provider_name,
                    )
                except Exception as e:
                    LOG.warning("Failed to record validation history: %s", e)

            # Drift detection
            if self.check_drift and self.history:
                resource_name = expose.get("id") or expose.get("name") or expose_id
                try:
                    drift_result = self.history.detect_drift(
                        contract_path=str(self.contract_path),
                        resource_name=resource_name,
                    )
                    if drift_result["drift_detected"]:
                        self.report.add_issue(
                            "warning",
                            "drift",
                            "Validation drift detected: {}".format(drift_result["message"]),
                            path,
                            suggestion="Review validation history to understand the drift",
                            documentation_url="https://docs.fluid.io/validation/drift-detection",
                        )
                except Exception as e:
                    LOG.warning("Failed to check drift: %s", e)

            if result.success:
                LOG.info("Resource '%s' validated successfully", expose_id)
            else:
                LOG.warning("Resource '%s' validation found issues", expose_id)

        except Exception as e:
            self.report.add_issue(
                "error",
                "binding",
                f"Failed to validate resource: {str(e)}",
                path,
                suggestion="Check provider credentials and connectivity",
            )

    def _build_snowflake_config(self) -> Dict[str, Any]:
        """Build Snowflake provider config from contract bindings + env vars."""
        from fluid_build.providers.snowflake.util.config import resolve_snowflake_settings

        return resolve_snowflake_settings(
            contract=self.contract,
            account=self.server,
            project_root=self.contract_path.parent if self.contract_path else None,
            environment=self.env,
        )

    def _validate_consumes(self) -> None:
        """Validate consumed data products (dependencies)."""
        LOG.info("Validating consumed data products...")

        consumes = self.contract.get("consumes", [])
        if not consumes:
            LOG.debug("No consumed data products declared")
            return

        for idx, consume in enumerate(consumes):
            consume_id = consume.get("id", f"consume_{idx}")
            # Accept FLUID DSL format (productId+exposeId) OR legacy ref field
            consume_ref = consume.get("ref") or consume.get("productId") or consume.get("provider")

            path = f"consumes[{idx}]"

            if not consume_ref:
                self.report.add_issue(
                    "error",
                    "metadata",
                    f"Missing 'ref' for consumed data product '{consume_id}'",
                    f"{path}.ref",
                )
            else:
                # Could validate that ref is resolvable, but that requires catalog access
                LOG.debug(f"Consume {consume_id} references: {consume_ref}")

            self.report.consumes_validated += 1

    def _validate_quality_specs(self) -> None:
        """Validate quality specifications and SLAs."""
        LOG.info("Validating quality specifications...")

        quality = self.contract.get("quality", {})

        if not quality:
            self.report.add_issue("info", "quality", "No quality specifications defined", "quality")
            return

        # Validate SLA if present
        sla = quality.get("sla", {})
        if sla:
            if "freshness" in sla:
                freshness = sla["freshness"]
                if not isinstance(freshness, str):
                    self.report.add_issue(
                        "warning",
                        "quality",
                        "SLA freshness should be a string (e.g., '1h', '1d')",
                        "quality.sla.freshness",
                    )

        # Validate tests if present
        tests = quality.get("tests", [])
        for idx, test in enumerate(tests):
            if "name" not in test:
                self.report.add_issue(
                    "warning",
                    "quality",
                    f"Test at index {idx} missing 'name'",
                    f"quality.tests[{idx}]",
                )

    def _validate_metadata(self) -> None:
        """Validate metadata and governance fields."""
        LOG.info("Validating metadata and governance...")

        metadata = self.contract.get("metadata", {})

        if not metadata:
            self.report.add_issue("warning", "metadata", "No metadata section defined", "metadata")
            return

        # Check recommended metadata fields
        recommended_fields = ["owner", "layer", "domain", "tags"]
        for field in recommended_fields:
            if field not in metadata:
                self.report.add_issue(
                    "info",
                    "metadata",
                    f"Recommended metadata field '{field}' not present",
                    f"metadata.{field}",
                )

        # Validate layer if present
        layer = metadata.get("layer")
        if layer:
            valid_layers = ["Bronze", "Silver", "Gold", "Platinum"]
            if layer not in valid_layers:
                self.report.add_issue(
                    "warning",
                    "metadata",
                    f"Layer '{layer}' not in standard set: {valid_layers}",
                    "metadata.layer",
                )


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the contract-validation command."""
    p = subparsers.add_parser(
        COMMAND,
        help="Validate contract compliance with actual deployed resources",
        description="""
        Validates that exposed data products match their FLUID contract specifications.
        
        This command performs comprehensive validation:
        - Contract syntax validation against FLUID schema
        - Schema consistency between contract and actual data
        - Data types and field definitions
        - Quality metrics and SLAs
        - Access patterns and bindings
        - Provider-specific configurations
        
        Connects to actual deployed resources (BigQuery, Snowflake, etc.)
        to ensure contract accuracy and compliance.
        """,
        epilog="""
Examples:
  # Validate contract against deployed resources
  fluid contract-validation contract.fluid.yaml
  
  # Validate with specific environment
  fluid contract-validation contract.fluid.yaml --env prod
  
  # Validate with explicit provider and project
  fluid contract-validation contract.fluid.yaml --provider gcp --project my-project
  
  # Strict validation (warnings as errors)
  fluid contract-validation contract.fluid.yaml --strict
  
  # Skip data validation, only check contract structure
  fluid contract-validation contract.fluid.yaml --no-data
  
  # Output report as JSON
  fluid contract-validation contract.fluid.yaml --output-format json
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Required arguments
    p.add_argument("contract", help="Path to contract.fluid.(yaml|json)")

    # Optional arguments
    p.add_argument("--env", help="Environment overlay (dev/test/prod)")

    p.add_argument(
        "--provider", help="Override provider platform (gcp, snowflake, databricks, aws, azure)"
    )

    p.add_argument("--project", help="Override project/account ID")

    p.add_argument("--region", help="Override region/location")

    p.add_argument("--strict", action="store_true", default=False, help="Treat warnings as errors")

    p.add_argument(
        "--no-data",
        action="store_true",
        default=False,
        help="Skip data validation checks (structure only)",
    )

    p.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format for validation report",
    )

    p.add_argument("--output-file", help="Write validation report to file")

    # Caching arguments
    p.add_argument(
        "--cache",
        action="store_true",
        default=True,
        help="Enable result caching (default: enabled)",
    )

    p.add_argument("--no-cache", dest="cache", action="store_false", help="Disable result caching")

    p.add_argument(
        "--cache-ttl",
        type=int,
        default=3600,
        help="Cache time-to-live in seconds (default: 3600 = 1 hour)",
    )

    p.add_argument(
        "--cache-clear", action="store_true", help="Clear validation cache before running"
    )

    p.add_argument("--cache-stats", action="store_true", help="Show cache statistics")

    # History and drift detection
    p.add_argument(
        "--track-history",
        action="store_true",
        default=True,
        help="Track validation history for drift detection (default: enabled)",
    )

    p.add_argument(
        "--check-drift",
        action="store_true",
        help="Check for validation drift compared to historical results",
    )

    p.set_defaults(cmd=COMMAND, func=run)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Execute the contract-validation command."""
    contract_path = Path(args.contract)

    if not contract_path.exists():
        console_error(f"Contract file not found: {contract_path}")
        return 1

    # Handle cache stats request
    if hasattr(args, "cache_stats") and args.cache_stats:
        cache = ValidationCache(ttl=args.cache_ttl)
        stats = cache.get_cache_stats()
        cprint("\n📊 Cache Statistics")
        cprint("=" * 50)
        cprint(f"Total entries: {stats['total_entries']}")
        cprint(f"Fresh entries: {stats['fresh_entries']}")
        cprint(f"Stale entries: {stats['stale_entries']}")
        cprint(f"Total size: {stats['total_size_bytes']:,} bytes")
        cprint(f"Cache directory: {stats['cache_dir']}")
        cprint(f"TTL: {stats['ttl_seconds']} seconds")
        return 0

    cprint(f"Validating FLUID contract: {contract_path}")

    # Create validator
    validator = ContractValidator(
        contract_path=contract_path,
        env=getattr(args, "env", None),
        provider_name=getattr(args, "provider", None),
        project=getattr(args, "project", None),
        region=getattr(args, "region", None),
        strict=getattr(args, "strict", False),
        check_data=not getattr(args, "no_data", False),
        use_cache=getattr(args, "cache", True),
        cache_ttl=getattr(args, "cache_ttl", 3600),
        cache_clear=getattr(args, "cache_clear", False),
        track_history=getattr(args, "track_history", True),
        check_drift=getattr(args, "check_drift", False),
        logger=logger,
    )

    # Execute validation
    try:
        report = validator.validate()
    except Exception as e:
        console_error(f"Validation failed: {e}")
        LOG.exception("Validation error")
        return 1

    # Output report
    if args.output_format == "json":
        output_json_report(report, args.output_file)
    else:
        output_text_report(report, args.output_file)

    # Return exit code
    if not report.is_valid():
        return 1

    if args.strict and report.get_warnings():
        console_error("Validation failed: warnings treated as errors (--strict mode)")
        return 1

    return 0


def output_text_report(report: ValidationReport, output_file: Optional[str] = None) -> None:
    """Output validation report in text format."""
    if RICH_AVAILABLE:
        output_rich_report(report, output_file)
    else:
        output_plain_report(report, output_file)


def output_rich_report(report: ValidationReport, output_file: Optional[str] = None) -> None:
    """Output validation report using Rich formatting."""
    console = Console()

    # Summary panel
    status_color = "green" if report.is_valid() else "red"
    status_icon = "✅" if report.is_valid() else "❌"

    summary_text = f"""[bold]{status_icon} Contract Validation: {report.contract_id}[/bold]
Version: {report.contract_version}
Validated: {report.exposes_validated} exposed, {report.consumes_validated} consumed
Results: {report.checks_passed} passed, {report.checks_failed} failed
Duration: {report.duration:.2f}s"""

    console.print(Panel(summary_text, title="Validation Summary", border_style=status_color))

    # Issues table
    if report.issues:
        table = Table(title="Validation Issues", show_header=True)
        table.add_column("Severity", style="bold")
        table.add_column("Category")
        table.add_column("Message", overflow="fold")
        table.add_column("Path", style="dim")

        for issue in report.issues:
            severity_style = {
                "error": "[red]ERROR[/red]",
                "warning": "[yellow]WARNING[/yellow]",
                "info": "[blue]INFO[/blue]",
            }.get(issue.severity, issue.severity.upper())

            table.add_row(severity_style, issue.category, issue.message, issue.path)

        console.print(table)
    else:
        console.print("[green]✅ No issues found - contract is valid![/green]")

    # Save to file if requested
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(report.get_summary())
            f.write("\n\n")
            for issue in report.issues:
                f.write(str(issue))
                f.write("\n\n")
        success(f"Report saved to: {output_file}")


def output_plain_report(report: ValidationReport, output_file: Optional[str] = None) -> None:
    """Output validation report in plain text format."""
    output = []
    output.append("=" * 60)
    output.append(report.get_summary())
    output.append("=" * 60)
    output.append("")

    if report.issues:
        output.append("Issues:")
        output.append("-" * 60)
        for issue in report.issues:
            output.append(str(issue))
            output.append("")
    else:
        output.append("✅ No issues found - contract is valid!")

    report_text = "\n".join(output)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(report_text)
        success(f"Report saved to: {output_file}")
    else:
        cprint(report_text)


def output_json_report(report: ValidationReport, output_file: Optional[str] = None) -> None:
    """Output validation report in JSON format."""
    report_dict = {
        "contract_path": report.contract_path,
        "contract_id": report.contract_id,
        "contract_version": report.contract_version,
        "validation_time": report.validation_time.isoformat(),
        "duration": report.duration,
        "is_valid": report.is_valid(),
        "exposes_validated": report.exposes_validated,
        "consumes_validated": report.consumes_validated,
        "checks_passed": report.checks_passed,
        "checks_failed": report.checks_failed,
        "error_count": len(report.get_errors()),
        "warning_count": len(report.get_warnings()),
        "issues": [
            {
                "severity": issue.severity,
                "category": issue.category,
                "message": issue.message,
                "path": issue.path,
                "expected": issue.expected,
                "actual": issue.actual,
                "suggestion": issue.suggestion,
                "documentation_url": issue.documentation_url,
            }
            for issue in report.issues
        ],
    }

    json_output = json.dumps(report_dict, indent=2)

    if output_file:
        with open(output_file, "w") as f:
            f.write(json_output)
        success(f"Report saved to: {output_file}")
    else:
        cprint(json_output)
