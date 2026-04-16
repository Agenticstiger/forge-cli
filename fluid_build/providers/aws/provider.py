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

# fluid_build/providers/aws/provider.py
"""
Production-grade AWS Provider for FLUID Build.

Implements comprehensive AWS integration across S3, Glue, Athena, Redshift,
EventBridge, Lambda, and more. Supports planning, idempotent application,
and rich error reporting with proper auth handling.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fluid_build.providers.base import (
    ApplyResult,
    BaseProvider,
    ProviderError,
)

from .plan.planner import plan_actions
from .util.auth import get_auth_report
from .util.circuit_breaker import CircuitBreakerOpenError, get_circuit_breaker
from .util.dependencies import order_actions_by_dependencies
from .util.logging import redact_dict
from .util.retry import with_retry
from .util.validation import ResourceValidator, validate_actions_strict


class AwsProvider(BaseProvider):
    """
    Production AWS provider with comprehensive service support.

    Features:
    - Complete S3 integration (buckets, lifecycle policies, versioning)
    - AWS Glue Data Catalog (databases, tables, crawlers)
    - Amazon Athena query execution
    - Amazon Redshift data warehousing
    - AWS Lambda function deployment
    - EventBridge scheduling and events
    - IAM policy compilation and binding
    - Comprehensive monitoring and error handling
    """

    name = "aws"

    @classmethod
    def get_provider_info(cls):
        from fluid_build.providers.base import ProviderMetadata

        return ProviderMetadata(
            name="aws",
            display_name="Amazon Web Services",
            description="Production AWS provider — S3, Glue, Athena, Redshift, Lambda, EventBridge, IAM",
            version="0.7.1",
            author="Agentics AI / DustLabs",
            supported_platforms=["aws", "s3", "redshift", "athena", "glue"],
            tags=["aws", "cloud", "s3", "glue", "athena", "redshift"],
        )

    def __init__(
        self,
        *,
        account_id: Optional[str] = None,
        region: Optional[str] = None,
        project: Optional[str] = None,  # Alias for account_id
        logger=None,
        **kwargs: Any,
    ) -> None:
        # Normalize account_id/project (for compatibility with GCP patterns)
        account_id = account_id or project

        # Import auth utilities to validate configuration early
        from .util.config import resolve_account_and_region

        self.account_id, self.region = resolve_account_and_region(account_id, region)

        super().__init__(project=account_id, region=self.region, logger=logger, **kwargs)

        self.info_kv(
            event="provider_initialized",
            provider="aws",
            account_id=self.account_id,
            region=self.region,
        )

    def capabilities(self) -> Mapping[str, bool]:
        """Advertise comprehensive AWS provider capabilities."""
        return {
            "planning": True,
            "apply": True,
            "render": True,  # OPDS export support
            "graph": True,  # Resource dependency graphing
            "auth": True,  # Auth context reporting
        }

    def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Generate AWS actions from FLUID contract.

        Converts contract specifications into concrete AWS resource operations:
        - S3 buckets and objects
        - Glue databases, tables, crawlers
        - Athena queries and workgroups
        - Redshift clusters and schemas
        - Lambda functions
        - EventBridge rules and schedules
        - IAM roles and policies
        """
        self.debug_kv(
            event="plan_started", contract_id=contract.get("id"), contract_name=contract.get("name")
        )

        try:
            # Validate sovereignty constraints (FLUID 0.7.1)
            self._validate_sovereignty(contract)

            actions = plan_actions(contract, self.account_id, self.region, self.logger)

            # Add orchestration actions (FLUID 0.7.1)
            orchestration_actions = self._plan_orchestration(contract)
            actions.extend(orchestration_actions)

            # Add schedule actions (FLUID 0.7.1)
            schedule_actions = self._plan_schedule(contract)
            actions.extend(schedule_actions)

            # Order actions by dependencies
            actions = order_actions_by_dependencies(actions)

            # Validate actions before returning
            validator = ResourceValidator(self.account_id, self.region)
            validation_result = validator.validate_actions(actions)

            if not validation_result["valid"]:
                error_msg = "Plan validation failed:\n" + "\n".join(validation_result["errors"])
                raise ProviderError(error_msg)

            # Log warnings if any
            for warning in validation_result.get("warnings", []):
                self.warn_kv(event="validation_warning", message=warning)

            self.info_kv(
                event="plan_completed",
                contract_id=contract.get("id"),
                actions_count=len(actions),
                validated=True,
                resource_counts=validation_result.get("resource_counts", {}),
            )

            return actions

        except ProviderError:
            # Re-raise ProviderError as-is
            raise
        except Exception as e:
            self.err_kv(event="plan_failed", contract_id=contract.get("id"), error=str(e))
            # Wrap all other exceptions in ProviderError
            raise ProviderError(f"Failed to plan AWS deployment: {e}") from e

    def apply(self, actions: List[Dict[str, Any]], **kwargs: Any) -> ApplyResult:
        """
        Execute AWS actions with idempotent semantics.

        Dispatches actions to appropriate service handlers with:
        - Retry logic for transient failures
        - Proper error categorization
        - Structured result reporting
        - Secret redaction in logs
        - Dry-run mode support

        Args:
            actions: List of actions to execute
            **kwargs: Additional parameters:
                - dry_run (bool): If True, show what would change without executing
                - validate (bool): If True, validate actions before execution (default: True)
        """
        start_time = time.time()
        results: List[Dict[str, Any]] = []
        applied = 0
        failed = 0

        dry_run = kwargs.get("dry_run", False)
        validate = kwargs.get("validate", True)

        # Validate actions if requested
        if validate:
            try:
                validate_actions_strict(actions, self.account_id, self.region)
            except Exception as e:
                self.err_kv(event="validation_failed", error=str(e))
                raise ProviderError(f"Action validation failed: {e}")

        self.info_kv(
            event="apply_started", actions_count=len(actions), provider="aws", dry_run=dry_run
        )

        for i, action in enumerate(actions):
            op = action.get("op")
            action_id = action.get("action_id") or action.get("id", f"action_{i}")

            try:
                # Prepare redacted action for logging — strip keys passed as
                # explicit kwargs to avoid "got multiple values" TypeError.
                redacted_action = redact_dict(action)
                for _pop_key in ("op", "action_id", "id", "event", "dry_run"):
                    redacted_action.pop(_pop_key, None)

                self.debug_kv(
                    event="action_started",
                    action_id=action_id,
                    op=op,
                    dry_run=dry_run,
                    **redacted_action,
                )

                if dry_run:
                    # In dry-run mode, check if resource already exists
                    result = self._dry_run_check(action, op, redacted_action)
                else:
                    result = self._execute_action(action)

                result["action_id"] = action_id
                result["index"] = i

                results.append(result)

                if result.get("status") == "changed" or (
                    result.get("status") == "ok" and not result.get("skipped", False)
                ):
                    applied += 1

                self.debug_kv(
                    event="action_completed",
                    action_id=action_id,
                    status=result.get("status"),
                    changed=result.get("changed", False),
                    duration_ms=result.get("duration_ms", 0),
                )

            except Exception as e:
                failed += 1
                error_result = {
                    "action_id": action_id,
                    "index": i,
                    "status": "error",
                    "op": op,
                    "error": str(e),
                    "changed": False,
                }
                results.append(error_result)

                self.err_kv(event="action_failed", action_id=action_id, op=op, error=str(e))

        duration_sec = round(time.time() - start_time, 3)

        apply_result = ApplyResult(
            provider="aws",
            applied=applied,
            failed=failed,
            duration_sec=duration_sec,
            timestamp=self._utc_timestamp(),
            results=results,
        )

        self.info_kv(
            event="apply_completed", applied=applied, failed=failed, duration_sec=duration_sec
        )

        return apply_result

    def render(
        self,
        src: Mapping[str, Any] | List[Mapping[str, Any]],
        *,
        out: Optional[str] = None,
        fmt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Export FLUID contracts to external formats.

        Supported formats:
        - 'opds': Open Data Product Standard JSON
        - 'dot': GraphViz dependency graph
        """
        if fmt == "opds":
            from .plan.export import export_opds

            return export_opds(src)
        elif fmt == "dot":
            from .plan.export import export_dot_graph

            return export_dot_graph(src)
        else:
            raise ProviderError(f"Unsupported render format: {fmt}. Supported: opds, dot")

    def auth_report(self) -> Dict[str, Any]:
        """Generate authentication and environment report for diagnostics."""
        report = get_auth_report(self.account_id, self.region)
        # Ensure status is set if there's an error
        if "error" in report and "status" not in report:
            report["status"] = "error"
        return report

    def _execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch action to appropriate service handler with circuit breaker protection.

        Routes actions based on operation prefix:
        - s3.*: S3 operations
        - glue.*: Glue Data Catalog operations
        - athena.*: Athena query operations
        - redshift.*: Redshift operations
        - lambda.*: Lambda function operations
        - events.*: EventBridge operations
        - step.*: Step Functions operations
        - kinesis.*: Kinesis streams
        - iam.*: IAM operations
        - cloudwatch.*: CloudWatch logs and metrics
        """
        op = action.get("op")

        if not op:
            raise ProviderError("Action missing required 'op' field")

        # Determine service from operation
        service = op.split(".")[0] if "." in op else "unknown"

        # Get circuit breaker for this service
        breaker = get_circuit_breaker(service)

        try:
            # Execute through circuit breaker
            return breaker.call(self._dispatch_action, action)
        except CircuitBreakerOpenError as e:
            # Circuit is open - service is down
            self.err_kv(
                event="circuit_breaker_open",
                service=service,
                timeout_remaining=e.timeout_remaining,
                message=str(e),
            )
            return {
                "status": "error",
                "op": op,
                "error": f"Service {service} unavailable (circuit breaker open)",
                "circuit_open": True,
                "retry_after": e.timeout_remaining,
                "changed": False,
            }

    def _dry_run_check(
        self, action: Dict[str, Any], op: str, redacted_action: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Lightweight existence check for dry-run mode.

        Uses read-only API calls to report whether the target resource
        already exists so the user can preview what *would* change.
        """
        import time

        start = time.time()

        exists: bool | None = None  # None = couldn't determine
        detail = ""

        try:
            import boto3
            from botocore.exceptions import ClientError

            if op == "s3.ensure_bucket":
                bucket = action.get("bucket")
                if bucket:
                    try:
                        boto3.client("s3").head_bucket(Bucket=bucket)
                        exists = True
                        detail = f"Bucket s3://{bucket} already exists"
                    except ClientError as e:
                        code = e.response["Error"]["Code"]
                        if code in ("404", "NoSuchBucket"):
                            exists = False
                            detail = f"Bucket s3://{bucket} will be created"
                        else:
                            detail = f"Cannot verify bucket: {code}"

            elif op == "glue.ensure_database":
                db = action.get("database")
                if db:
                    try:
                        boto3.client("glue").get_database(Name=db)
                        exists = True
                        detail = f"Database '{db}' already exists"
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "EntityNotFoundException":
                            exists = False
                            detail = f"Database '{db}' will be created"

            elif op in ("glue.ensure_table", "glue.ensure_iceberg_table"):
                db = action.get("database")
                tbl = action.get("table")
                if db and tbl:
                    try:
                        boto3.client("glue").get_table(DatabaseName=db, Name=tbl)
                        exists = True
                        detail = f"Table '{db}.{tbl}' already exists"
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "EntityNotFoundException":
                            exists = False
                            detail = f"Table '{db}.{tbl}' will be created"

            elif op == "iam.ensure_role":
                role = action.get("role_name")
                if role:
                    try:
                        boto3.client("iam").get_role(RoleName=role)
                        exists = True
                        detail = f"Role '{role}' already exists"
                    except ClientError:
                        exists = False
                        detail = f"Role '{role}' will be created"

            elif op == "glue.ensure_job":
                job_name = action.get("name")
                if job_name:
                    try:
                        boto3.client("glue").get_job(JobName=job_name)
                        exists = True
                        detail = f"Glue job '{job_name}' already exists"
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "EntityNotFoundException":
                            exists = False
                            detail = f"Glue job '{job_name}' will be created"

            elif op == "athena.create_iceberg_table":
                db = action.get("database")
                tbl = action.get("table")
                if db and tbl:
                    try:
                        boto3.client("glue").get_table(DatabaseName=db, Name=tbl)
                        exists = True
                        detail = f"Iceberg table '{db}.{tbl}' already exists"
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "EntityNotFoundException":
                            exists = False
                            detail = f"Iceberg table '{db}.{tbl}' will be created via Athena"

        except ImportError:
            detail = "boto3 not available — cannot verify"

        from .util.logging import duration_ms

        would_change = not exists if exists is not None else True
        return {
            "status": "dry_run",
            "op": op,
            "exists": exists,
            "would_change": would_change,
            "message": detail or f"Would execute: {op}",
            "action": redacted_action,
            "changed": would_change,
            "duration_ms": duration_ms(start),
        }

    def _dispatch_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Internal action dispatcher (called through circuit breaker).

        Routes to service-specific handlers.
        """
        op = action.get("op")

        # Route to service-specific handlers
        if op.startswith("s3."):
            return self._execute_s3_action(action)
        elif op.startswith("glue."):
            return self._execute_glue_action(action)
        elif op.startswith("athena."):
            return self._execute_athena_action(action)
        elif op.startswith("redshift."):
            return self._execute_redshift_action(action)
        elif op.startswith("lambda."):
            return self._execute_lambda_action(action)
        elif op.startswith("events."):
            return self._execute_events_action(action)
        elif op.startswith("step."):
            return self._execute_stepfunctions_action(action)
        elif op.startswith("kinesis."):
            return self._execute_kinesis_action(action)
        elif op.startswith("iam."):
            return self._execute_iam_action(action)
        elif op.startswith("cloudwatch."):
            return self._execute_cloudwatch_action(action)
        elif op.startswith("sqs."):
            return self._execute_sqs_action(action)
        elif op.startswith("sns."):
            return self._execute_sns_action(action)
        elif op.startswith("secretsmanager."):
            return self._execute_secretsmanager_action(action)
        elif op.startswith("dbt."):
            return self._execute_dbt_action(action)
        else:
            self.warn_kv(event="unknown_action_op", op=op, action_id=action.get("id"))
            return {
                "status": "skipped",
                "op": op,
                "reason": f"Unknown operation: {op}",
                "changed": False,
            }

    def _execute_s3_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute S3 operations."""
        from .actions import s3

        op = action.get("op")

        if op == "s3.ensure_bucket":
            return with_retry(lambda: s3.ensure_bucket(action), self.logger)
        elif op == "s3.ensure_prefix":
            return with_retry(lambda: s3.ensure_prefix(action), self.logger)
        elif op == "s3.ensure_lifecycle":
            return with_retry(lambda: s3.ensure_lifecycle(action), self.logger)
        elif op == "s3.ensure_versioning":
            return with_retry(lambda: s3.ensure_versioning(action), self.logger)
        else:
            raise ProviderError(f"Unknown S3 operation: {op}")

    def _execute_glue_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Glue Data Catalog operations."""
        from .actions import glue

        op = action.get("op")

        if op == "glue.ensure_database":
            return with_retry(lambda: glue.ensure_database(action), self.logger)
        elif op == "glue.ensure_table":
            return with_retry(lambda: glue.ensure_table(action), self.logger)
        elif op == "glue.ensure_iceberg_table":
            return with_retry(lambda: glue.ensure_iceberg_table(action), self.logger)
        elif op == "glue.ensure_crawler":
            return with_retry(lambda: glue.ensure_crawler(action), self.logger)
        elif op == "glue.run_crawler":
            return glue.run_crawler(action)
        elif op == "glue.ensure_job":
            return with_retry(lambda: glue.ensure_job(action), self.logger)
        elif op == "glue.start_job_run":
            return glue.start_job_run(action)
        else:
            raise ProviderError(f"Unknown Glue operation: {op}")

    def _execute_athena_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Athena operations."""
        from .actions import athena

        op = action.get("op")

        if op == "athena.ensure_workgroup":
            return with_retry(lambda: athena.ensure_workgroup(action), self.logger)
        elif op == "athena.ensure_table":
            return with_retry(lambda: athena.ensure_table(action), self.logger)
        elif op == "athena.execute_query":
            return athena.execute_query(action)
        elif op == "athena.create_view":
            return athena.create_view(action)
        elif op == "athena.create_iceberg_table":
            return athena.create_iceberg_table(action)
        """Execute Redshift operations."""
        from .actions import redshift

        op = action.get("op")

        if op == "redshift.ensure_schema":
            return with_retry(lambda: redshift.ensure_schema(action), self.logger)
        elif op == "redshift.ensure_table":
            return with_retry(lambda: redshift.ensure_table(action), self.logger)
        elif op == "redshift.execute_sql":
            return redshift.execute_sql(action)
        elif op == "redshift.ensure_view":
            return redshift.ensure_view(action)
        else:
            raise ProviderError(f"Unknown Redshift operation: {op}")

    def _execute_lambda_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Lambda operations."""
        from .actions import lambda_fn

        op = action.get("op")

        if op == "lambda.ensure_function":
            return lambda_fn.ensure_function(action)
        elif op == "lambda.invoke":
            return lambda_fn.invoke_function(action)
        elif op == "lambda.update_code":
            return lambda_fn.update_function_code(action)
        else:
            raise ProviderError(f"Unknown Lambda operation: {op}")

    def _execute_events_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute EventBridge operations."""
        from .actions import events

        op = action.get("op")

        if op == "events.ensure_rule":
            return events.ensure_rule(action)
        elif op == "events.ensure_schedule":
            return events.ensure_schedule(action)
        elif op == "events.put_target":
            return events.put_target(action)
        else:
            raise ProviderError(f"Unknown EventBridge operation: {op}")

    def _execute_stepfunctions_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Step Functions operations."""
        from .actions import stepfunctions

        op = action.get("op")

        if op == "step.ensure_state_machine":
            return stepfunctions.ensure_state_machine(action)
        elif op == "step.start_execution":
            return stepfunctions.start_execution(action)
        else:
            raise ProviderError(f"Unknown Step Functions operation: {op}")

    def _execute_kinesis_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Kinesis operations."""
        from .actions import kinesis

        op = action.get("op")

        if op == "kinesis.ensure_stream":
            return kinesis.ensure_stream(action)
        elif op == "kinesis.ensure_firehose":
            return kinesis.ensure_firehose(action)
        else:
            raise ProviderError(f"Unknown Kinesis operation: {op}")

    def _execute_iam_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute IAM operations."""
        from .actions import iam

        op = action.get("op")

        if op == "iam.ensure_role":
            return iam.ensure_role(action)
        elif op == "iam.attach_policy":
            return iam.attach_policy(action)
        elif op == "iam.ensure_policy":
            return iam.ensure_policy(action)
        elif op == "iam.bind_s3_bucket":
            return iam.bind_s3_bucket(action)
        elif op == "iam.bind_glue_database":
            return iam.bind_glue_database(action)
        else:
            raise ProviderError(f"Unknown IAM operation: {op}")

    def _execute_cloudwatch_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute CloudWatch operations."""
        from .actions import cloudwatch

        op = action.get("op")

        if op == "cloudwatch.ensure_log_group":
            return with_retry(lambda: cloudwatch.ensure_log_group(action), self.logger)
        elif op == "cloudwatch.ensure_metric_alarm":
            return cloudwatch.ensure_metric_alarm(action)
        else:
            raise ProviderError(f"Unknown CloudWatch operation: {op}")

    def _execute_sqs_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute SQS operations."""
        from .actions import sqs

        op = action.get("op")

        if op == "sqs.ensure_queue":
            return with_retry(lambda: sqs.ensure_queue(action), self.logger)
        elif op == "sqs.send_message":
            return sqs.send_message(action)
        else:
            raise ProviderError(f"Unknown SQS operation: {op}")

    def _execute_sns_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute SNS operations."""
        from .actions import sns

        op = action.get("op")

        if op == "sns.ensure_topic":
            return with_retry(lambda: sns.ensure_topic(action), self.logger)
        elif op == "sns.ensure_subscription":
            return sns.ensure_subscription(action)
        elif op == "sns.publish_message":
            return sns.publish_message(action)
        else:
            raise ProviderError(f"Unknown SNS operation: {op}")

    def _execute_secretsmanager_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Secrets Manager operations."""
        from .actions import secretsmanager

        op = action.get("op")

        if op == "secretsmanager.ensure_secret":
            return with_retry(lambda: secretsmanager.ensure_secret(action), self.logger)
        elif op == "secretsmanager.get_secret_value":
            return secretsmanager.get_secret_value(action)
        else:
            raise ProviderError(f"Unknown Secrets Manager operation: {op}")

    def _execute_dbt_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute dbt operations (Redshift/Athena targets)."""
        from .runtime import dbt_runner

        op = action.get("op")

        if op == "dbt.prepare_profile":
            return dbt_runner.prepare_profile(action)
        elif op == "dbt.run":
            return dbt_runner.run_dbt(action)
        elif op == "dbt.test":
            return dbt_runner.test_dbt(action)
        else:
            raise ProviderError(f"Unknown dbt operation: {op}")

    def _validate_sovereignty(self, contract: Mapping[str, Any]) -> None:
        """
        Validate sovereignty constraints (FLUID 0.7.1).

        Ensures AWS region matches jurisdiction and data residency requirements.
        """
        sovereignty = contract.get("sovereignty")
        if not sovereignty:
            return

        from .util.sovereignty import SovereigntyViolationError, validate_sovereignty

        # Build binding from provider configuration
        binding = {"location": {"region": self.region}}

        try:
            validate_sovereignty(contract, binding)
            self.info_kv(
                event="sovereignty_validated",
                region=self.region,
                jurisdiction=sovereignty.get("jurisdiction"),
                data_residency=sovereignty.get("dataResidency"),
            )
        except SovereigntyViolationError as e:
            self.err_kv(event="sovereignty_violation", error=str(e))
            raise ProviderError(str(e)) from e

    def _plan_orchestration(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Plan orchestration tasks from contract (FLUID 0.7.1).

        Parses orchestration.tasks with type: provider_action and
        converts them to AWS provider actions.
        """
        orchestration = contract.get("orchestration")
        if not orchestration:
            return []

        from .plan.orchestration import OrchestrationError, plan_orchestration_tasks

        try:
            actions = plan_orchestration_tasks(contract, self.account_id, self.region, self.logger)

            if actions:
                self.info_kv(
                    event="orchestration_planned",
                    task_count=len(actions),
                    engine=orchestration.get("engine"),
                )

            return actions

        except OrchestrationError as e:
            self.err_kv(event="orchestration_planning_failed", error=str(e))
            raise ProviderError(f"Orchestration planning failed: {e}") from e

    def _plan_schedule(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Plan scheduling actions from contract (FLUID 0.7.1).

        Parses orchestration.schedule and orchestration.triggers to create
        AWS scheduling infrastructure (EventBridge, MWAA, Lambda).
        """
        orchestration = contract.get("orchestration")
        if not orchestration:
            return []

        # Only plan schedules if schedule or triggers are present
        if not orchestration.get("schedule") and not orchestration.get("triggers"):
            return []

        from .plan.schedule import plan_schedule_actions

        try:
            actions = plan_schedule_actions(contract, self.account_id, self.region, self.logger)

            if actions:
                self.info_kv(
                    event="schedule_planned",
                    action_count=len(actions),
                    has_schedule=bool(orchestration.get("schedule")),
                    has_triggers=bool(orchestration.get("triggers")),
                )

            return actions

        except Exception as e:
            self.err_kv(event="schedule_planning_failed", error=str(e))
            raise ProviderError(f"Schedule planning failed: {e}") from e

    def _utc_timestamp(self) -> str:
        """Generate UTC timestamp string."""
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def export(
        self,
        contract: Mapping[str, Any],
        engine: str = "airflow",
        output_dir: str = ".",
        **kwargs: Any,
    ) -> str:
        """
        Export contract as executable DAG/pipeline code.

        Generates ready-to-run orchestration code for the specified engine.
        Supports Airflow, Dagster, and Prefect workflows.

        Args:
            contract: FLUID contract with orchestration section
            engine: Target orchestration engine ("airflow", "dagster", "prefect")
            output_dir: Directory to write generated file (default: current directory)
            **kwargs: Additional parameters for code generation

        Returns:
            Path to generated file

        Raises:
            ProviderError: If export fails or engine is unsupported

        Example:
            >>> provider = AwsProvider(account_id="YOUR_AWS_ACCOUNT_ID", region="us-east-1")
            >>> dag_file = provider.export(contract, engine="airflow", output_dir="./dags")
            >>> print(f"Generated: {dag_file}")
        """
        import os

        from fluid_build.providers.common.codegen_utils import (
            detect_circular_dependencies,
            validate_contract_for_export,
        )

        # Validate contract structure
        try:
            validate_contract_for_export(contract)
        except ValueError as e:
            raise ProviderError(f"Invalid contract: {e}") from e

        # Check for circular dependencies
        tasks = contract["orchestration"]["tasks"]
        cycles = detect_circular_dependencies(tasks)
        if cycles:
            raise ProviderError(f"Circular dependencies detected in tasks: {', '.join(cycles)}")

        orchestration = contract.get("orchestration")
        if not orchestration:
            raise ProviderError("Contract missing orchestration section - cannot export DAG")

        contract_id = contract.get("id", "unnamed")
        # Sanitize contract_id for safe use in filenames (prevent path traversal)
        import re

        safe_id = re.sub(r"[^a-zA-Z0-9_\-.]", "_", contract_id)

        self.info_kv(
            event="export_started", contract_id=contract_id, engine=engine, output_dir=output_dir
        )

        try:
            # Generate code based on engine
            if engine == "airflow" or engine == "mwaa":
                from .codegen import generate_airflow_dag

                code = generate_airflow_dag(contract, self.account_id, self.region)
                filename = f"{safe_id}_dag.py"

            elif engine == "dagster":
                from .codegen import generate_dagster_pipeline

                code = generate_dagster_pipeline(contract, self.account_id, self.region)
                filename = f"{safe_id}_pipeline.py"

            elif engine == "prefect":
                from .codegen import generate_prefect_flow

                code = generate_prefect_flow(contract, self.account_id, self.region)
                filename = f"{safe_id}_flow.py"

            else:
                raise ProviderError(
                    f"Unsupported orchestration engine: {engine}. "
                    f"Supported: airflow, dagster, prefect"
                )

            # Ensure output directory exists
            os.makedirs(output_dir, exist_ok=True)

            # Write generated code to file
            output_path = os.path.join(output_dir, filename)
            with open(output_path, "w") as f:
                f.write(code)

            self.info_kv(
                event="export_completed",
                contract_id=contract_id,
                engine=engine,
                output_file=output_path,
                code_lines=len(code.splitlines()),
            )

            return output_path

        except ProviderError:
            raise
        except Exception as e:
            self.err_kv(event="export_failed", contract_id=contract_id, engine=engine, error=str(e))
            raise ProviderError(f"Failed to export {engine} DAG: {e}") from e
