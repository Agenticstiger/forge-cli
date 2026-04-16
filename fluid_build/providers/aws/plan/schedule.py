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
Schedule Planning for AWS Provider.

Maps FLUID orchestration.schedule and orchestration.triggers to concrete
AWS resources: EventBridge Scheduler, MWAA (Managed Airflow), Lambda, Step Functions.

Supports:
- Cron-based scheduling (EventBridge Scheduler or MWAA)
- Event-driven triggers (EventBridge Rules + Lambda)
- Manual execution (Step Functions)
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SchedulePlanner:
    """
    Plans scheduling and orchestration resources for AWS.

    Converts FLUID orchestration configuration into AWS infrastructure
    for scheduled and event-driven execution.
    """

    def __init__(self, account_id: str, region: str, logger: Optional[logging.Logger] = None):
        """
        Initialize schedule planner.

        Args:
            account_id: AWS account ID
            region: AWS region
            logger: Optional logger
        """
        self.account_id = account_id
        self.region = region
        self.logger = logger or logging.getLogger(__name__)

    def plan_schedule_actions(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Generate AWS scheduling actions from contract.

        Args:
            contract: FLUID contract with orchestration section

        Returns:
            List of AWS scheduling actions
        """
        orchestration = contract.get("orchestration", {})
        if not orchestration:
            return []

        actions = []

        # 1. Handle schedule-based triggers
        if orchestration.get("schedule"):
            schedule_actions = self._plan_scheduled_execution(contract, orchestration)
            actions.extend(schedule_actions)

        # 2. Handle event-driven triggers
        if orchestration.get("triggers"):
            trigger_actions = self._plan_event_triggers(contract, orchestration)
            actions.extend(trigger_actions)

        # 3. Handle manual execution setup
        if orchestration.get("engine") == "step-functions":
            step_function_actions = self._plan_step_functions(contract, orchestration)
            actions.extend(step_function_actions)

        if actions:
            self.logger.info(f"Planned {len(actions)} scheduling actions")

        return actions

    def _plan_scheduled_execution(
        self, contract: Dict[str, Any], orchestration: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Plan scheduled execution via EventBridge Scheduler or MWAA.

        Choice logic:
        - If engine is "airflow" or "mwaa", use MWAA
        - Otherwise, use EventBridge Scheduler + Lambda
        """
        actions = []

        orchestration.get("schedule")
        engine = orchestration.get("engine", "").lower()

        if engine in ["airflow", "mwaa"]:
            # Use MWAA (Managed Airflow)
            actions.extend(self._plan_mwaa_schedule(contract, orchestration))
        else:
            # Use EventBridge Scheduler + Lambda
            actions.extend(self._plan_eventbridge_schedule(contract, orchestration))

        return actions

    def _plan_mwaa_schedule(
        self, contract: Dict[str, Any], orchestration: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Plan MWAA (Managed Airflow) scheduling.

        Note: MWAA DAGs are deployed separately via S3, but we can
        create the MWAA environment and supporting infrastructure.
        """
        actions = []

        contract_id = contract.get("id", "unknown")
        environment_name = f"mwaa-{contract_id}"

        # Check if MWAA environment already specified
        mwaa_config = orchestration.get("mwaa", {})
        if "environmentName" in mwaa_config:
            environment_name = mwaa_config["environmentName"]

        # 1. Ensure MWAA environment exists
        actions.append(
            {
                "id": f"mwaa_environment_{contract_id}",
                "op": "mwaa.ensure_environment",
                "environment_name": environment_name,
                "region": self.region,
                "airflow_version": mwaa_config.get("airflowVersion", "2.8.1"),
                "environment_class": mwaa_config.get("environmentClass", "mw1.small"),
                "dag_s3_path": mwaa_config.get("dagS3Path", f"s3://mwaa-{self.account_id}/dags/"),
                "execution_role_arn": mwaa_config.get("executionRoleArn"),
                "source_bucket_arn": mwaa_config.get("sourceBucketArn"),
                "schedulers": mwaa_config.get("schedulers", 2),
                "max_workers": mwaa_config.get("maxWorkers", 10),
                "min_workers": mwaa_config.get("minWorkers", 1),
                "tags": self._get_schedule_tags(contract),
            }
        )

        # 2. The actual DAG file will be generated by codegen/airflow.py
        # and uploaded to S3 separately

        self.logger.info(f"Planned MWAA environment: {environment_name}")
        return actions

    def _plan_eventbridge_schedule(
        self, contract: Dict[str, Any], orchestration: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Plan EventBridge Scheduler + Lambda execution.

        Creates:
        1. Lambda function to execute workflow
        2. EventBridge Scheduler rule
        3. IAM permissions
        """
        actions = []

        contract_id = contract.get("id", "unknown")
        schedule = orchestration.get("schedule")
        timezone = orchestration.get("timezone", "UTC")

        # 1. Create Lambda function for workflow execution
        function_name = f"fluid-workflow-{contract_id}"

        actions.append(
            {
                "id": f"lambda_workflow_{contract_id}",
                "op": "lambda.ensure_function",
                "function_name": function_name,
                "runtime": "python3.11",
                "handler": "index.handler",
                "role": f"arn:aws:iam::{self.account_id}:role/fluid-workflow-execution",
                "code": self._generate_workflow_lambda_code(contract, orchestration),
                "timeout": orchestration.get("timeout", 300),
                "memory_size": orchestration.get("memory", 256),
                "environment": {
                    "CONTRACT_ID": contract_id,
                    "ACCOUNT_ID": self.account_id,
                    "REGION": self.region,
                },
                "tags": self._get_schedule_tags(contract),
            }
        )

        # 2. Create EventBridge Scheduler rule
        schedule_name = f"fluid-schedule-{contract_id}"

        actions.append(
            {
                "id": f"eventbridge_schedule_{contract_id}",
                "op": "eventbridge.ensure_schedule",
                "schedule_name": schedule_name,
                "schedule_expression": schedule,  # Cron or rate expression
                "timezone": timezone,
                "target": {
                    "arn": f"arn:aws:lambda:{self.region}:{self.account_id}:function:{function_name}",
                    "role_arn": f"arn:aws:iam::{self.account_id}:role/EventBridgeSchedulerRole",
                    "input": json.dumps(
                        {"contract_id": contract_id, "execution_type": "scheduled"}
                    ),
                },
                "flexible_time_window": {"mode": "OFF"},  # Exact time execution
                "state": "ENABLED",
                "description": f"FLUID workflow schedule for {contract.get('name', contract_id)}",
                "tags": self._get_schedule_tags(contract),
            }
        )

        # 3. Grant EventBridge permission to invoke Lambda
        actions.append(
            {
                "id": f"lambda_permission_{contract_id}",
                "op": "lambda.add_permission",
                "function_name": function_name,
                "statement_id": f"AllowEventBridgeInvoke-{contract_id}",
                "action": "lambda:InvokeFunction",
                "principal": "scheduler.amazonaws.com",
                "source_arn": f"arn:aws:scheduler:{self.region}:{self.account_id}:schedule/default/{schedule_name}",
            }
        )

        self.logger.info(f"Planned EventBridge schedule: {schedule_name}")
        return actions

    def _plan_event_triggers(
        self, contract: Dict[str, Any], orchestration: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Plan event-driven triggers via EventBridge Rules.

        Supports:
        - S3 events (object created, deleted, etc.)
        - DynamoDB streams
        - Custom EventBridge events
        - SQS messages
        """
        actions = []

        triggers = orchestration.get("triggers", [])
        contract.get("id", "unknown")

        for i, trigger in enumerate(triggers):
            trigger_type = trigger.get("type")
            trigger_id = trigger.get("id", f"trigger_{i}")

            if trigger_type == "s3":
                actions.extend(self._plan_s3_trigger(contract, trigger, trigger_id))
            elif trigger_type == "dynamodb":
                actions.extend(self._plan_dynamodb_trigger(contract, trigger, trigger_id))
            elif trigger_type == "eventbridge":
                actions.extend(self._plan_eventbridge_trigger(contract, trigger, trigger_id))
            elif trigger_type == "sqs":
                actions.extend(self._plan_sqs_trigger(contract, trigger, trigger_id))
            else:
                self.logger.warning(f"Unknown trigger type: {trigger_type}")

        return actions

    def _plan_s3_trigger(
        self, contract: Dict[str, Any], trigger: Dict[str, Any], trigger_id: str
    ) -> List[Dict[str, Any]]:
        """Plan S3 event trigger."""
        actions = []

        contract_id = contract.get("id", "unknown")
        function_name = f"fluid-s3-trigger-{contract_id}-{trigger_id}"

        # 1. Create Lambda function
        actions.append(
            {
                "id": f"lambda_s3_trigger_{trigger_id}",
                "op": "lambda.ensure_function",
                "function_name": function_name,
                "runtime": "python3.11",
                "handler": "index.handler",
                "role": f"arn:aws:iam::{self.account_id}:role/fluid-workflow-execution",
                "code": self._generate_s3_trigger_lambda_code(contract, trigger),
                "timeout": 300,
                "tags": self._get_schedule_tags(contract),
            }
        )

        # 2. Add S3 bucket notification
        bucket = trigger.get("bucket")
        prefix = trigger.get("prefix", "")
        suffix = trigger.get("suffix", "")
        events = trigger.get("events", ["s3:ObjectCreated:*"])

        actions.append(
            {
                "id": f"s3_notification_{trigger_id}",
                "op": "s3.ensure_notification",
                "bucket": bucket,
                "notification_id": f"fluid-trigger-{trigger_id}",
                "lambda_function_arn": f"arn:aws:lambda:{self.region}:{self.account_id}:function:{function_name}",
                "events": events,
                "filter": {"prefix": prefix, "suffix": suffix},
            }
        )

        # 3. Grant S3 permission to invoke Lambda
        actions.append(
            {
                "id": f"lambda_permission_s3_{trigger_id}",
                "op": "lambda.add_permission",
                "function_name": function_name,
                "statement_id": f"AllowS3Invoke-{trigger_id}",
                "action": "lambda:InvokeFunction",
                "principal": "s3.amazonaws.com",
                "source_arn": f"arn:aws:s3:::{bucket}",
            }
        )

        return actions

    def _plan_dynamodb_trigger(
        self, contract: Dict[str, Any], trigger: Dict[str, Any], trigger_id: str
    ) -> List[Dict[str, Any]]:
        """Plan DynamoDB stream trigger."""
        actions = []

        contract_id = contract.get("id", "unknown")
        function_name = f"fluid-dynamodb-trigger-{contract_id}-{trigger_id}"

        # 1. Create Lambda function
        actions.append(
            {
                "id": f"lambda_dynamodb_trigger_{trigger_id}",
                "op": "lambda.ensure_function",
                "function_name": function_name,
                "runtime": "python3.11",
                "handler": "index.handler",
                "role": f"arn:aws:iam::{self.account_id}:role/fluid-workflow-execution",
                "code": self._generate_dynamodb_trigger_lambda_code(contract, trigger),
                "timeout": 300,
                "tags": self._get_schedule_tags(contract),
            }
        )

        # 2. Create event source mapping
        table_name = trigger.get("table")

        actions.append(
            {
                "id": f"lambda_event_source_{trigger_id}",
                "op": "lambda.create_event_source_mapping",
                "function_name": function_name,
                "event_source_arn": f"arn:aws:dynamodb:{self.region}:{self.account_id}:table/{table_name}/stream/*",
                "starting_position": trigger.get("startingPosition", "LATEST"),
                "batch_size": trigger.get("batchSize", 100),
                "maximum_batching_window_in_seconds": trigger.get("batchWindow", 0),
                "parallelization_factor": trigger.get("parallelization", 1),
            }
        )

        return actions

    def _plan_eventbridge_trigger(
        self, contract: Dict[str, Any], trigger: Dict[str, Any], trigger_id: str
    ) -> List[Dict[str, Any]]:
        """Plan custom EventBridge event trigger."""
        actions = []

        contract_id = contract.get("id", "unknown")
        rule_name = f"fluid-event-{contract_id}-{trigger_id}"
        function_name = f"fluid-event-trigger-{contract_id}-{trigger_id}"

        # 1. Create Lambda function
        actions.append(
            {
                "id": f"lambda_event_trigger_{trigger_id}",
                "op": "lambda.ensure_function",
                "function_name": function_name,
                "runtime": "python3.11",
                "handler": "index.handler",
                "role": f"arn:aws:iam::{self.account_id}:role/fluid-workflow-execution",
                "code": self._generate_event_trigger_lambda_code(contract, trigger),
                "timeout": 300,
                "tags": self._get_schedule_tags(contract),
            }
        )

        # 2. Create EventBridge rule
        event_pattern = trigger.get("eventPattern", {})

        actions.append(
            {
                "id": f"eventbridge_rule_{trigger_id}",
                "op": "eventbridge.ensure_rule",
                "rule_name": rule_name,
                "event_pattern": json.dumps(event_pattern),
                "state": "ENABLED",
                "description": f"FLUID event trigger for {contract.get('name', contract_id)}",
                "targets": [
                    {
                        "id": "1",
                        "arn": f"arn:aws:lambda:{self.region}:{self.account_id}:function:{function_name}",
                    }
                ],
            }
        )

        # 3. Grant EventBridge permission to invoke Lambda
        actions.append(
            {
                "id": f"lambda_permission_eventbridge_{trigger_id}",
                "op": "lambda.add_permission",
                "function_name": function_name,
                "statement_id": f"AllowEventBridgeInvoke-{trigger_id}",
                "action": "lambda:InvokeFunction",
                "principal": "events.amazonaws.com",
                "source_arn": f"arn:aws:events:{self.region}:{self.account_id}:rule/{rule_name}",
            }
        )

        return actions

    def _plan_sqs_trigger(
        self, contract: Dict[str, Any], trigger: Dict[str, Any], trigger_id: str
    ) -> List[Dict[str, Any]]:
        """Plan SQS queue trigger."""
        actions = []

        contract_id = contract.get("id", "unknown")
        function_name = f"fluid-sqs-trigger-{contract_id}-{trigger_id}"

        # 1. Create Lambda function
        actions.append(
            {
                "id": f"lambda_sqs_trigger_{trigger_id}",
                "op": "lambda.ensure_function",
                "function_name": function_name,
                "runtime": "python3.11",
                "handler": "index.handler",
                "role": f"arn:aws:iam::{self.account_id}:role/fluid-workflow-execution",
                "code": self._generate_sqs_trigger_lambda_code(contract, trigger),
                "timeout": 300,
                "tags": self._get_schedule_tags(contract),
            }
        )

        # 2. Create event source mapping
        queue_name = trigger.get("queue")

        actions.append(
            {
                "id": f"lambda_sqs_mapping_{trigger_id}",
                "op": "lambda.create_event_source_mapping",
                "function_name": function_name,
                "event_source_arn": f"arn:aws:sqs:{self.region}:{self.account_id}:{queue_name}",
                "batch_size": trigger.get("batchSize", 10),
                "maximum_batching_window_in_seconds": trigger.get("batchWindow", 0),
            }
        )

        return actions

    def _plan_step_functions(
        self, contract: Dict[str, Any], orchestration: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Plan AWS Step Functions state machine for manual execution.

        Useful for complex workflows with branching logic.
        """
        actions = []

        contract_id = contract.get("id", "unknown")
        state_machine_name = f"fluid-workflow-{contract_id}"

        # Generate state machine definition from tasks
        state_machine_def = self._generate_state_machine_definition(contract, orchestration)

        actions.append(
            {
                "id": f"step_functions_{contract_id}",
                "op": "stepfunctions.ensure_state_machine",
                "state_machine_name": state_machine_name,
                "definition": json.dumps(state_machine_def),
                "role_arn": f"arn:aws:iam::{self.account_id}:role/StepFunctionsExecutionRole",
                "type": "STANDARD",  # or "EXPRESS" for high-volume
                "tags": self._get_schedule_tags(contract),
            }
        )

        return actions

    def _generate_workflow_lambda_code(
        self, contract: Dict[str, Any], orchestration: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate Lambda function code for workflow execution."""
        # This would be a ZIP file with the workflow execution logic
        # For now, return inline code structure
        tasks = orchestration.get("tasks", [])
        task_entries = json.dumps(
            [
                {
                    "taskId": t.get("taskId"),
                    "action": t.get("action"),
                    "params": t.get("params", {}),
                    "dependsOn": t.get("dependsOn", []),
                }
                for t in tasks
                if t.get("type") == "provider_action"
            ]
        )
        return {
            "ZipFile": f"""
import json
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TASKS = json.loads('''{task_entries}''')

def handler(event, context):
    contract_id = event.get('contract_id', '{contract.get("id")}')
    logger.info(f"Executing FLUID workflow for contract: {{contract_id}}")

    completed = set()
    results = {{}}
    remaining = list(TASKS)

    while remaining:
        runnable = [
            t for t in remaining
            if all(d in completed for d in t.get('dependsOn', []))
        ]
        if not runnable:
            raise RuntimeError(f"Circular dependency detected among: {{[t['taskId'] for t in remaining]}}")
        for t in runnable:
            tid = t['taskId']
            logger.info(f"Running task: {{tid}} ({{t['action']}})")
            results[tid] = {{'status': 'ok'}}
            completed.add(tid)
            remaining.remove(t)

    return {{
        'statusCode': 200,
        'body': json.dumps({{'message': 'Workflow executed', 'tasks_completed': len(completed)}})
    }}
"""
        }

    def _generate_s3_trigger_lambda_code(
        self, contract: Dict[str, Any], trigger: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate Lambda code for S3 trigger."""
        contract_id = contract.get("id", "unknown")
        workflow_fn = f"fluid-workflow-{contract_id}"
        return {
            "ZipFile": f"""
import json
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    lam = boto3.client('lambda')
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']
        logger.info(f"S3 trigger: s3://{{bucket}}/{{key}}")
        lam.invoke(
            FunctionName='{workflow_fn}',
            InvocationType='Event',
            Payload=json.dumps({{
                'contract_id': '{contract_id}',
                'trigger': 's3',
                'bucket': bucket,
                'key': key,
            }}),
        )
    return {{'statusCode': 200}}
"""
        }

    def _generate_dynamodb_trigger_lambda_code(
        self, contract: Dict[str, Any], trigger: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate Lambda code for DynamoDB trigger."""
        contract_id = contract.get("id", "unknown")
        workflow_fn = f"fluid-workflow-{contract_id}"
        return {
            "ZipFile": f"""
import json
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    lam = boto3.client('lambda')
    for record in event.get('Records', []):
        event_name = record.get('eventName')
        logger.info(f"DynamoDB trigger: {{event_name}}")
        lam.invoke(
            FunctionName='{workflow_fn}',
            InvocationType='Event',
            Payload=json.dumps({{
                'contract_id': '{contract_id}',
                'trigger': 'dynamodb',
                'event_name': event_name,
                'keys': record.get('dynamodb', {{}}).get('Keys', {{}}),
            }}),
        )
    return {{'statusCode': 200}}
"""
        }

    def _generate_event_trigger_lambda_code(
        self, contract: Dict[str, Any], trigger: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate Lambda code for EventBridge trigger."""
        contract_id = contract.get("id", "unknown")
        workflow_fn = f"fluid-workflow-{contract_id}"
        return {
            "ZipFile": f"""
import json
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    logger.info(f"EventBridge trigger: {{json.dumps(event)}}")
    lam = boto3.client('lambda')
    lam.invoke(
        FunctionName='{workflow_fn}',
        InvocationType='Event',
        Payload=json.dumps({{
            'contract_id': '{contract_id}',
            'trigger': 'eventbridge',
            'detail_type': event.get('detail-type', ''),
            'detail': event.get('detail', {{}}),
        }}),
    )
    return {{'statusCode': 200}}
"""
        }

    def _generate_sqs_trigger_lambda_code(
        self, contract: Dict[str, Any], trigger: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate Lambda code for SQS trigger."""
        contract_id = contract.get("id", "unknown")
        workflow_fn = f"fluid-workflow-{contract_id}"
        return {
            "ZipFile": f"""
import json
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    lam = boto3.client('lambda')
    for record in event.get('Records', []):
        body = json.loads(record['body'])
        logger.info(f"SQS trigger: {{body}}")
        lam.invoke(
            FunctionName='{workflow_fn}',
            InvocationType='Event',
            Payload=json.dumps({{
                'contract_id': '{contract_id}',
                'trigger': 'sqs',
                'body': body,
            }}),
        )
    return {{'statusCode': 200}}
"""
        }

    def _generate_state_machine_definition(
        self, contract: Dict[str, Any], orchestration: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate Step Functions state machine definition."""
        tasks = orchestration.get("tasks", [])

        # Simple sequential workflow
        states = {}

        for i, task in enumerate(tasks):
            task_id = task.get("taskId")
            is_last = i == len(tasks) - 1

            states[task_id] = {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": f"fluid-task-{task_id}", "Payload.$": "$"},
                "Next": tasks[i + 1].get("taskId") if not is_last else None,
                "End": is_last,
            }

            if not is_last:
                del states[task_id]["End"]

        return {
            "Comment": f"FLUID workflow for {contract.get('name', 'contract')}",
            "StartAt": tasks[0].get("taskId") if tasks else "End",
            "States": states if states else {"End": {"Type": "Succeed"}},
        }

    def _get_schedule_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """Get tags for scheduling resources."""
        return {
            "fluid:contract_id": contract.get("id", "unknown"),
            "fluid:contract_name": contract.get("name", "unknown"),
            "fluid:component": "scheduling",
            "fluid:managed_by": "fluid-forge",
        }


def plan_schedule_actions(
    contract: Dict[str, Any], account_id: str, region: str, logger: Optional[logging.Logger] = None
) -> List[Dict[str, Any]]:
    """
    Convenience function to plan scheduling actions.

    Args:
        contract: FLUID contract with orchestration section
        account_id: AWS account ID
        region: AWS region
        logger: Optional logger

    Returns:
        List of AWS scheduling actions
    """
    planner = SchedulePlanner(account_id, region, logger)
    return planner.plan_schedule_actions(contract)
