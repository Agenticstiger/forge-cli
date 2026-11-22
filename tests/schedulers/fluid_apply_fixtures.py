# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Contracts whose ``fluid apply`` DAGs ``test_fluid_apply_golden.py`` pins byte for byte.

:data:`CASES` declare neither ``builds[].dependencies`` nor a dataset trigger: each renders
what the renderer wrote before it learned either (one DAG per scheduled build, one task each),
plus the ``outlets`` of the exposes a build writes. :data:`GRAPH_CASES` use both.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Tuple

import yaml

from tests.cli._schedule_dag_fixtures import DEMO_CONTRACT, DEMO_CONTRACT_PATH

#: forge's native Spark shape: a stream that ``fluid apply`` keeps running (no DAG) and a
#: daily Spark SQL batch on an EMR cluster (a DAG whose task outlives the run's wait).
SPARK_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "platform.events",
    "name": "Events",
    "metadata": {"owner": {"team": "data-platform"}},
    "builds": [
        {
            "id": "stream-raw",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["events_raw"],
            "properties": {
                "stream": {
                    "kind": "kafka",
                    "topic": "source.events",
                    "format": "json",
                    "schema": [{"name": "event_id", "type": "string", "required": True}],
                    "trigger": {"processingTime": "PT1M"},
                },
                "parameters": {
                    "spark": {
                        "runtime": "emr-ec2",
                        "clusterId": "j-ABCDEFGH1234",
                        "mode": "streaming",
                    }
                },
            },
        },
        {
            "id": "daily-curated",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["events_daily"],
            "execution": {"trigger": {"type": "schedule", "schedule": "0 2 * * *"}},
            "properties": {
                "sql": "SELECT CAST(event_time AS DATE) AS event_date FROM raw.events",
                "parameters": {
                    "spark": {"runtime": "emr-ec2", "clusterId": "j-ABCDEFGH1234", "mode": "batch"}
                },
            },
        },
    ],
    "exposes": [
        {
            "exposeId": "events_raw",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "raw",
                    "table": "events",
                    "bucket": "lake",
                    "path": "iceberg/raw/events/",
                    "region": "eu-central-1",
                },
            },
        },
        {
            "exposeId": "events_daily",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "curated",
                    "table": "events_daily",
                    "bucket": "lake",
                    "path": "iceberg/curated/events_daily/",
                    "region": "eu-central-1",
                },
            },
        },
    ],
}


def _mixed_triggers() -> Dict[str, Any]:
    """The demo product with every trigger shape the renderer schedules or leaves out."""
    doc = yaml.safe_load(DEMO_CONTRACT)
    base = doc["builds"][0]

    def build(build_id: str, execution: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        out = copy.deepcopy(base)
        out["id"] = build_id
        if execution is None:
            out.pop("execution", None)
        else:
            out["execution"] = execution
        return out

    doc["builds"] = [
        build(
            "nightly.load",
            {
                "trigger": {
                    "type": "schedule",
                    "schedule": "30 1 * * 1-5",
                    "timezone": "Asia/Tokyo",
                },
                "retries": {"maxAttempts": 6},
            },
        ),
        build("hourly", {"trigger": {"cron": "@hourly"}}),
        build("no_trigger", None),
        build("by_hand", {"trigger": {"type": "manual"}}),
        build("always_on", {"trigger": {"type": "continuous"}}),
        build("custom_timetable", {"trigger": {"type": "timetable", "schedule": "0 1 * * *"}}),
        build(
            "opted_out",
            {
                "trigger": {"type": "schedule", "schedule": "0 4 * * *"},
                "orchestration": {"engine": "none"},
            },
        ),
    ]
    return doc


#: case name -> (contract, ``--env``, contract path relative to the project)
CASES: Dict[str, Tuple[Dict[str, Any], Optional[str], str]] = {
    "demo": (yaml.safe_load(DEMO_CONTRACT), None, DEMO_CONTRACT_PATH),
    "demo_env_aws": (yaml.safe_load(DEMO_CONTRACT), "aws", DEMO_CONTRACT_PATH),
    "spark": (SPARK_CONTRACT, None, "native.fluid.yaml"),
    "mixed_triggers": (_mixed_triggers(), "prod", "contracts/demo/contract.fluid.yaml"),
}


def _expose(expose_id: str, database: str, table: str, path: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": "iceberg",
            "location": {
                "database": database,
                "table": table,
                "bucket": "lake",
                "path": path,
                "region": "eu-central-1",
            },
        },
        "contract": {"schema": [{"name": "event_date", "type": "date", "required": True}]},
    }


def _spark_batch(build_id: str, output: str, **extra: Any) -> Dict[str, Any]:
    return {
        "id": build_id,
        "engine": "spark",
        "pattern": "embedded-logic",
        "outputs": [output],
        "properties": {
            "sql": "SELECT CAST(event_time AS DATE) AS event_date FROM raw.events",
            "parameters": {
                "spark": {"runtime": "emr-ec2", "clusterId": "j-ABCDEFGH1234", "mode": "batch"}
            },
        },
        **extra,
    }


def _with_builds(contract: Dict[str, Any], *builds: Dict[str, Any]) -> Dict[str, Any]:
    """``contract`` with ``builds`` and the two tables the graph cases add beside the Spark ones."""
    contract["builds"] = list(builds)
    contract["exposes"] += [
        _expose("users_daily", "curated", "users_daily", "iceberg/curated/users_daily/"),
        _expose("events_marts", "marts", "events_hourly", "marts/events_hourly/"),
    ]
    return contract


def events_pipeline() -> Dict[str, Any]:
    """A raw stream, then a daily Spark SQL batch and the build that runs after it, then an
    hourly dbt build that also runs whenever the daily table updates."""
    contract = copy.deepcopy(SPARK_CONTRACT)
    stream, daily = contract["builds"]
    daily["dependencies"] = [{"buildId": "stream-raw"}]
    users = _spark_batch(
        "daily-users",
        "users_daily",
        dependencies=[{"buildId": "daily-curated", "condition": "success"}],
    )
    hourly = {
        "id": "hourly-marts",
        "engine": "dbt",
        "pattern": "embedded-logic",
        "outputs": ["events_marts"],
        "properties": {"sql": "SELECT event_date FROM curated.events_daily"},
        "execution": {
            "trigger": {
                "type": "schedule_and_dataset",
                "schedule": "0 * * * *",
                "datasets": [{"productId": "platform.events", "exposeId": "events_daily"}],
            }
        },
    }
    return _with_builds(contract, stream, daily, users, hourly)


def rules_and_any() -> Dict[str, Any]:
    """Every dependency condition, a build with no trigger of its own whose dependent carries
    one, and a DAG that runs when any of two assets updates."""
    contract = copy.deepcopy(SPARK_CONTRACT)
    staging = _spark_batch("staging", "events_daily")
    staging["execution"] = {"retries": {"maxAttempts": 2}}
    mart = _spark_batch(
        "mart",
        "users_daily",
        dependencies=[{"buildId": "staging", "condition": "completion"}],
        execution={
            "trigger": {"type": "schedule", "schedule": "@daily", "timezone": "Europe/Zurich"}
        },
    )
    notify = {
        "id": "notify",
        "engine": "python",
        "pattern": "embedded-logic",
        "outputs": [],
        "dependencies": [{"buildId": "mart", "condition": "always"}],
    }
    report = {
        "id": "report",
        "engine": "dbt",
        "pattern": "embedded-logic",
        "outputs": ["events_marts"],
        "execution": {
            "trigger": {
                "type": "dataset",
                "datasetsOperator": "any",
                "datasets": [
                    {"productId": "platform.events", "exposeId": "users_daily"},
                    "bigquery://analytics-prj/sales/orders/",
                ],
            }
        },
    }
    return _with_builds(contract, contract["builds"][0], staging, mart, notify, report)


def kept_alive_stream() -> Dict[str, Any]:
    """The Spark contract whose stream declares execution.retries: a keepalive DAG beside the
    daily batch, on a cadence the build's Airflow DAG config sets."""
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"][0]["execution"] = {
        "retries": {"maxAttempts": 5},
        "orchestration": {
            "engine": "airflow",
            "airflow": {"dagId": "events_keepalive", "dagConfig": {"schedule": "*/10 * * * *"}},
        },
    }
    return contract


def _maintained(contract: Dict[str, Any], expose_id: str, **block: Any) -> Dict[str, Any]:
    """``contract`` with a Spark maintenance block on the expose ``expose_id``."""
    maintenance: Dict[str, Any] = {
        "schedule": "0 3 * * *",
        "compaction": {},
        "expireSnapshots": {"olderThan": "P5D"},
    }
    maintenance.update(block)
    for expose in contract["exposes"]:
        if expose["exposeId"] == expose_id:
            expose["iceberg"] = {"maintenance": maintenance}
    return contract


def kept_alive_stream_maintained() -> Dict[str, Any]:
    """The kept-alive stream's table also declares maintenance: a keepalive DAG, the daily
    batch's DAG and the table's maintenance DAG, on its own schedule and timezone."""
    return _maintained(
        kept_alive_stream(), "events_raw", schedule="30 4 * * 0", timezone="Europe/Zurich"
    )


def batch_maintained() -> Dict[str, Any]:
    """The daily batch's table declares maintenance: the batch's DAG and the table's."""
    return _maintained(copy.deepcopy(SPARK_CONTRACT), "events_daily")


#: Contracts that use ``builds[].dependencies``, dataset triggers or a kept-alive stream.
GRAPH_CASES: Dict[str, Tuple[Dict[str, Any], Optional[str], str]] = {
    "events_pipeline": (events_pipeline(), None, "contracts/events/contract.fluid.yaml"),
    "rules_and_any": (rules_and_any(), "aws", "contract.fluid.yaml"),
    "kept_alive_stream": (kept_alive_stream(), "aws", "native.fluid.yaml"),
    "kept_alive_stream_maintained": (kept_alive_stream_maintained(), "aws", "native.fluid.yaml"),
    "batch_maintained": (batch_maintained(), None, "native.fluid.yaml"),
}


#: The four built-in alert kinds, every event, an SLA (``alerts`` block, 0.7.7).
ALL_KINDS_ALERTS: Dict[str, Any] = {
    "sla": "PT4H",
    "routes": [
        {
            "name": "oncall",
            "kind": "webhook",
            "format": "zenduty",
            "connection": "zenduty_oncall",
            "events": ["failure", "success", "sla_miss"],
        },
        {
            "name": "team",
            "kind": "email",
            "to": ["data-platform@corp.example"],
            "from": "fluid@corp.example",
            "events": ["failure", "retry"],
        },
        {"name": "pager", "kind": "pagerduty", "events": ["failure", "success"]},
        {"name": "chat", "kind": "slack", "connection": "slack_data_alerts"},
        {
            "name": "ops",
            "kind": "webhook",
            "format": "opsgenie",
            "connection": "opsgenie_alerts",
            "severity": "error",
            "events": ["failure", "success"],
        },
        {"name": "audit", "kind": "webhook", "connection": "alerts_audit", "events": ["retry"]},
    ],
}


def _alerting(contract: Dict[str, Any], alerts: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(contract)
    out["alerts"] = copy.deepcopy(alerts)
    return out


#: Contracts with an ``alerts`` block: the notifiers on the DAG's callbacks.
ALERT_CASES: Dict[str, Tuple[Dict[str, Any], Optional[str], str]] = {
    "alerts_all_kinds": (
        _alerting(yaml.safe_load(DEMO_CONTRACT), ALL_KINDS_ALERTS),
        None,
        DEMO_CONTRACT_PATH,
    ),
    "alerts_graph_and_keepalive": (
        _alerting(
            kept_alive_stream(),
            {"routes": [{"name": "oncall", "kind": "webhook", "connection": "alerts_hook"}]},
        ),
        "aws",
        "native.fluid.yaml",
    ),
    "alerts_pipeline": (
        _alerting(
            events_pipeline(),
            {
                "sla": "PT90M",
                "routes": [
                    {
                        "name": "chat",
                        "kind": "slack",
                        "events": ["failure", "sla_miss"],
                    }
                ],
            },
        ),
        None,
        "contracts/events/contract.fluid.yaml",
    ),
}
