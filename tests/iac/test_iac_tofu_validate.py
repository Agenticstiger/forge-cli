# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Integration: emitted .tf.json is accepted by the real ``tofu`` binary.

Skipped unless ``tofu`` is on PATH. ``tofu validate`` checks config
syntax and provider-schema correctness — it needs no cloud credentials
(only registry network access during ``tofu init`` to fetch providers).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from fluid_build.iac import IAC_PLUGINS, build_module

pytestmark = [pytest.mark.integration, pytest.mark.provider]

_TOFU = shutil.which("tofu")


# Registry/network failure signatures during ``tofu init`` — these mean a
# transient infra outage (e.g. registry.opentofu.org 504s while fetching
# providers), NOT a contract/tfjson error. These tests exist to validate the
# EMITTED tfjson against the real ``tofu`` schema (only ``tofu validate``
# exercises that); a provider *download* failure during init is orthogonal, so
# skip rather than red the build on an upstream registry outage.
_REGISTRY_FAILURE_MARKERS = (
    "could not query provider",
    "failed to retrieve",
    "failed to query available provider",
    "authentication checksums for provider",
    "cryptographic signature for provider",
    "request failed after",
    "504",
    "context deadline exceeded",
    "no such host",
    "connection reset",
    "i/o timeout",
    "tls handshake timeout",
)


def _tofu_init_or_skip(cwd) -> None:
    """Run ``tofu init`` (no backend), tolerating transient registry outages.

    Retries once, then ``pytest.skip``s if init keeps failing for a
    network/registry reason; a non-network init failure is a real error and is
    asserted as before.
    """
    import time

    last = None
    for attempt in range(2):
        last = subprocess.run(
            [_TOFU, "init", "-backend=false", "-input=false", "-no-color"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if last.returncode == 0:
            return
        if attempt == 0:
            time.sleep(2)
    output = ((last.stderr or "") + (last.stdout or "")).lower()
    if any(marker in output for marker in _REGISTRY_FAILURE_MARKERS):
        pytest.skip(
            "tofu init could not reach the provider registry (transient "
            f"network/504, not a tfjson error): {(last.stderr or last.stdout)[:200]}"
        )
    assert last.returncode == 0, last.stderr or last.stdout


# One representative contract per cloud. New plugins add an entry here.
_SAMPLE_CONTRACTS = {
    "aws": {
        "id": "demo.aws",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "database": "demo",
                        "table": "orders",
                        "bucket": "demo-fluid-lake",
                        "path": "orders/",
                        "stream": "demo-events",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    },
    "gcp": {
        "id": "demo.gcp",
        "metadata": {
            "policies": {
                "readers": {
                    "principals": ["analyst@example.com"],
                    "permissions": ["read"],
                }
            }
        },
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "format": "bigquery_table",
                    "location": {"dataset": "demo", "table": "events"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            },
            {
                "exposeId": "stream",
                "binding": {
                    "platform": "gcp",
                    "format": "pubsub_topic",
                    "location": {"topic": "demo-topic", "subscription": "demo-sub"},
                },
            },
            {
                "exposeId": "lake",
                "binding": {
                    "platform": "gcp",
                    "format": "gcs_bucket",
                    "location": {"bucket": "demo-fluid-gcs"},
                },
            },
        ],
    },
    "snowflake": {
        "id": "demo.snowflake",
        "security": {
            "access_control": {
                "grants": [
                    {
                        "role": "ANALYST",
                        "privilege": "SELECT",
                        "object_type": "TABLE",
                        "object_name": "DEMO_DB.PUBLIC.EVENTS",
                    },
                    {
                        "role": "LOADER",
                        "privilege": "USAGE",
                        "object_type": "DATABASE",
                        "object_name": "DEMO_DB",
                    },
                ]
            },
            "policies": {
                "masking": [
                    {
                        "name": "MASK_EMAIL",
                        "body": "CASE WHEN CURRENT_ROLE() = 'ADMIN' THEN val ELSE '***' END",
                        "signature": "(val VARCHAR) RETURNS VARCHAR",
                    }
                ],
                "row_access": [
                    {
                        "name": "TENANT_ISOLATION",
                        "condition": "tenant_id = CURRENT_ACCOUNT()",
                        "signature": "(tenant_id VARCHAR) RETURNS BOOLEAN",
                    }
                ],
            },
        },
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "DEMO_DB", "schema": "PUBLIC", "table": "EVENTS"},
                },
                "contract": {
                    "schema": [
                        {"name": "ID", "type": "integer", "required": True},
                        {"name": "MSG", "type": "string"},
                    ]
                },
            }
        ],
    },
}


# Per-cloud native-planner actions — exercises the `emit(contract, actions)`
# path (schedule / orchestration resources the planner interprets).
_SAMPLE_ACTIONS = {
    "snowflake": [
        {
            "op": "sf.stream.ensure",
            "database": "DEMO_DB",
            "schema": "PUBLIC",
            "name": "EVENTS_STREAM",
            "source_table": "EVENTS",
            "append_only": True,
        },
        {
            "op": "sf.task.ensure",
            "database": "DEMO_DB",
            "schema": "PUBLIC",
            "name": "DAILY_ROLLUP",
            "sql": "INSERT INTO DEMO_DB.PUBLIC.AGG SELECT COUNT(*) FROM DEMO_DB.PUBLIC.EVENTS",
            "schedule": "USING CRON 0 2 * * * UTC",
            "warehouse": "COMPUTE_WH",
            "after": [],
        },
        {"op": "sf.task.resume", "name": "DAILY_ROLLUP"},
        {
            "op": "sf.view.materialized.ensure",
            "database": "DEMO_DB",
            "schema": "PUBLIC",
            "name": "EVENTS_RECENT",
            "query": "SELECT * FROM DEMO_DB.PUBLIC.EVENTS",
            "secure": True,
        },
        {
            "op": "sf.procedure.ensure",
            "database": "DEMO_DB",
            "schema": "PUBLIC",
            "name": "REFRESH_AGG",
            "language": "SQL",
            "parameters": [],
            "body": "BEGIN INSERT INTO DEMO_DB.PUBLIC.AGG SELECT 1; RETURN 'ok'; END;",
        },
        {
            "op": "sf.udf.ensure",
            "database": "DEMO_DB",
            "schema": "PUBLIC",
            "name": "MASK_EMAIL_FN",
            "language": "SQL",
            "return_type": "VARCHAR",
            "parameters": [{"name": "email", "type": "VARCHAR"}],
            "body": "REGEXP_REPLACE(email, '.+@', '***@')",
        },
    ],
    "gcp": [
        {
            "op": "run.ensure_service",
            "project": "demo",
            "region": "us-central1",
            "service_name": "fluid-demo",
            "image": "gcr.io/fluid-forge/runner:latest",
            "cpu": "1",
            "memory": "512Mi",
            "concurrency": 1,
            "timeout": 300,
            "env_vars": {"FLUID_CONTRACT_ID": "demo.gcp"},
            "labels": {"managed-by": "fluid-forge"},
            "max_instances": 10,
            "min_instances": 0,
        },
        {
            "op": "scheduler.ensure_job",
            "project": "demo",
            "location": "us-central1",
            "job_name": "fluid-demo-job",
            "description": "Scheduled execution",
            "schedule": "0 2 * * *",
            "timezone": "UTC",
            "target": {
                "http_target": {
                    "uri": "https://fluid-demo-abc-us-central1.a.run.app/execute",
                    "http_method": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "oidc_token": {
                        "service_account_email": "sched@demo.iam.gserviceaccount.com",
                        "audience": "https://fluid-demo-abc-us-central1.a.run.app",
                    },
                }
            },
            "retry_config": {"retry_count": 3, "max_doublings": 3},
            "attempt_deadline": "300s",
        },
        {
            "op": "ps.ensure_topic",
            "project": "demo",
            "topic": "fluid-demo-events",
            "labels": {"managed-by": "fluid-forge"},
            "message_retention_duration": "604800s",
        },
        {
            "op": "ps.ensure_subscription",
            "project": "demo",
            "topic": "fluid-demo-events",
            "subscription": "fluid-demo-sub",
            "ack_deadline_seconds": 60,
            "push_config": {
                "push_endpoint": "https://fluid-demo-abc-us-central1.a.run.app/pubsub",
                "attributes": {"x-goog-version": "v1"},
            },
        },
        {
            "op": "iam.bind_bq_table",
            "project": "demo",
            "dataset": "demo",
            "table": "events",
            "policies": {
                "readers": {"principals": ["analyst@example.com"], "permissions": ["read"]}
            },
        },
        {
            "op": "composer.deploy_dag",
            "project": "demo",
            "location": "us-central1",
            "environment": "fluid-composer",
            "dag_id": "fluid_demo",
            "dag_bucket": "us-central1-fluid-composer-abc123-bucket",
            "dag_content": (
                "from airflow import DAG\n"
                "import datetime\n"
                "with DAG('fluid_demo', start_date=datetime.datetime(2024, 1, 1),\n"
                "         schedule_interval='@daily', catchup=False) as dag:\n"
                "    pass\n"
            ),
        },
    ],
    "aws": [
        {
            "op": "glue.ensure_job",
            "name": "demo-etl",
            "role": "arn:aws:iam::123456789012:role/GlueETLRole",
            "script_location": "s3://demo-fluid-staging/scripts/etl.py",
            "command_name": "glueetl",
            "glue_version": "4.0",
            "worker_type": "G.1X",
            "number_of_workers": 10,
            "timeout": 2880,
            "default_arguments": {"--enable-metrics": "true"},
            "tags": {"managed_by": "fluid"},
        },
        {
            "op": "stepfunctions.ensure_state_machine",
            "state_machine_name": "fluid-workflow-demo",
            "definition": '{"StartAt":"Done","States":{"Done":{"Type":"Pass","End":true}}}',
            "role_arn": "arn:aws:iam::123456789012:role/StepFunctionsExecutionRole",
            "type": "STANDARD",
            "tags": {"managed_by": "fluid"},
        },
        {
            "op": "lambda.ensure_function",
            "function_name": "fluid-workflow-demo-fn",
            "runtime": "python3.11",
            "handler": "index.handler",
            "role": "arn:aws:iam::123456789012:role/fluid-workflow-execution",
            "code": {"ZipFile": "def handler(event, context):\n    return {'ok': True}\n"},
            "timeout": 300,
            "memory_size": 256,
            "environment": {"CONTRACT_ID": "demo.aws"},
            "tags": {"managed_by": "fluid"},
        },
        {
            "op": "eventbridge.ensure_schedule",
            "schedule_name": "fluid-demo-schedule",
            "schedule_expression": "rate(1 hour)",
            "timezone": "UTC",
            "state": "ENABLED",
            "flexible_time_window": {"mode": "OFF"},
            "target": {
                "arn": "arn:aws:lambda:us-east-1:123456789012:function:fluid-workflow-demo-fn",
                "role_arn": "arn:aws:iam::123456789012:role/EventBridgeSchedulerRole",
                "input": '{"execution_type": "scheduled"}',
            },
        },
        {
            "op": "lambda.add_permission",
            "function_name": "fluid-workflow-demo-fn",
            "statement_id": "AllowSchedulerInvoke",
            "action": "lambda:InvokeFunction",
            "principal": "scheduler.amazonaws.com",
            "source_arn": "arn:aws:scheduler:us-east-1:123456789012:schedule/default/fluid-demo-schedule",
        },
    ],
}


@pytest.mark.skipif(_TOFU is None, reason="tofu binary not installed")
@pytest.mark.parametrize("cloud", sorted(_SAMPLE_CONTRACTS))
def test_emitted_tfjson_passes_tofu_validate(cloud, tmp_path):
    plugin = IAC_PLUGINS.get(cloud)
    if plugin is None:
        pytest.skip(f"no IaC plugin registered for {cloud}")

    (tmp_path / "main.tf.json").write_text(
        build_module(plugin, _SAMPLE_CONTRACTS[cloud], actions=_SAMPLE_ACTIONS.get(cloud, ()))
    )

    _tofu_init_or_skip(tmp_path)

    validate = subprocess.run(
        [_TOFU, "validate", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert validate.returncode == 0, validate.stderr or validate.stdout


# Per-cloud cross-account / cross-project shapes. Stage 1 unit tests
# pin the dict; this pins the *.tf.json against the real ``tofu``
# binary — catches any provider-schema mismatch in the new
# aws_s3_bucket_policy + google_bigquery_dataset_iam_member emits.
_CROSS_ACCOUNT_CONTRACTS = {
    "aws": {
        "id": "demo.aws.xacc",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "database": "demo",
                        "table": "orders",
                        "bucket": "demo-fluid-xacc",
                        "path": "orders/",
                    },
                    "governance": {
                        "lakeFormation": {
                            "grants": [
                                {
                                    "principal": "arn:aws:iam::222222222222:role/consumer",
                                    "permissions": ["SELECT", "DESCRIBE"],
                                }
                            ]
                        }
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    },
    "gcp": {
        "id": "demo.gcp.xproj",
        "metadata": {
            # Cross-project access via the existing metadata.policies
            # surface — _bq_access_entries maps the SA to a user_by_email
            # row on the dataset's access[] block. BQ accepts cross-project
            # SA emails verbatim via user_by_email. Zero new schema
            # fields needed for cross-project sharing.
            "policies": {
                "consumers": {
                    "principals": ["consumer@other-project.iam.gserviceaccount.com"],
                    "permissions": ["read"],
                }
            },
        },
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": "demo_xproj", "table": "events"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    },
}


@pytest.mark.skipif(_TOFU is None, reason="tofu binary not installed")
@pytest.mark.parametrize("cloud", sorted(_CROSS_ACCOUNT_CONTRACTS))
def test_cross_account_emit_passes_tofu_validate(cloud, tmp_path):
    """The cross-account/cross-project emit must produce ``.tf.json``
    that real ``tofu`` accepts. Catches provider-schema drift on
    ``aws_s3_bucket_policy`` and ``google_bigquery_dataset_iam_member``
    that the dict-level Stage 1 tests can't surface."""
    plugin = IAC_PLUGINS.get(cloud)
    if plugin is None:
        pytest.skip(f"no IaC plugin registered for {cloud}")

    (tmp_path / "main.tf.json").write_text(build_module(plugin, _CROSS_ACCOUNT_CONTRACTS[cloud]))

    _tofu_init_or_skip(tmp_path)

    validate = subprocess.run(
        [_TOFU, "validate", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert validate.returncode == 0, validate.stderr or validate.stdout


# Snowflake Horizon declarative tag governance was scoped OUT in the
# "minimum schema changes" pass — defining tags / binding masking
# policies to tags requires either new schema surface (which we don't
# want) or out-of-band Snowsight setup (which contracts shouldn't own).
# Catalog-style enrichment (table COMMENT + per-column comments) is
# covered without any schema change at
# ``tests/iac/test_iac_snowflake.py::TestSnowflakeCatalogEnrichment``.
