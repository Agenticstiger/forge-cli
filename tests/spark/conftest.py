# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import copy
import importlib.util
from pathlib import Path

import pytest

from fluid_build.secrets.policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

RUNNER_PATH = Path(__file__).resolve().parents[2] / "fluid_build/spark/runner/forge_spark_runner.py"


def col(name, type_, **kw):
    return {"name": name, "type": type_, **kw}


#: An order event as the topic carries it: a nested struct, an array of structs, a map.
VALUE = [
    col("order_id", "string", required=True),
    col(
        "customer",
        "struct",
        fields=[col("id", "string"), col("email", "string")],
    ),
    col(
        "items",
        "array",
        element={"type": "struct", "fields": [col("sku", "string"), col("qty", "int")]},
    ),
    col("event_time", "timestamp_ltz(3)"),
]

OUTPUT = [
    col("order_id", "string", required=True),
    col("customer_id", "string"),
    col("email_hash", "string"),
    col("sku", "string"),
    col("qty", "int"),
    col("event_time", "timestamp_ltz(3)"),
]

BROKERS = "b-1.msk.example:9098,b-2.msk.example:9098"
MASK_KEY_ARN = "arn:aws:secretsmanager:eu-west-1:000000000005:secret:commerce/mask-key-AbCdEf"

CONTRACT = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "commerce.orders-stream",
    "name": "Orders stream",
    "metadata": {"owner": {"team": "commerce-data"}},
    "builds": [
        {
            "id": "ingest-orders",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["order_lines"],
            "properties": {
                "stream": {
                    "kind": "kafka",
                    "topic": "commerce.orders",
                    "format": "json",
                    "schema": VALUE,
                    "trigger": {"processingTime": "PT1M"},
                    "watermark": {"column": "event_time", "delay": "PT10M"},
                    "startingOffsets": "earliest",
                },
                "mapping": {
                    "explode": {"path": "items", "as": "item"},
                    "columns": [
                        {"name": "customer_id", "from": "customer.id"},
                        {"name": "email_hash", "from": "customer.email", "maskAtIngest": "hash"},
                        {"name": "sku", "from": "item.sku"},
                        {"name": "qty", "from": "item.qty"},
                    ],
                },
                "parameters": {
                    "spark": {
                        "runtime": "emr-serverless",
                        "mode": "streaming",
                        "source": {"bootstrapServers": BROKERS, "auth": "msk-iam"},
                        "maskKeySecretArn": MASK_KEY_ARN,
                    }
                },
            },
        }
    ],
    "exposes": [
        {
            "exposeId": "order_lines",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "commerce",
                    "table": "order_lines",
                    "bucket": "commerce-lake",
                    "path": "iceberg/commerce/order_lines/",
                    "region": "eu-west-1",
                },
            },
            "contract": {"schema": OUTPUT},
        }
    ],
}


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


@pytest.fixture
def build(contract):
    return contract["builds"][0]


@pytest.fixture(autouse=True)
def operator_policy(monkeypatch):
    """The operator lists the example's broker and registry domains, and nothing else:
    no test inherits an operator's list from the machine it runs on."""
    monkeypatch.delenv(CONTRACT_SQL_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(SECRET_REF_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", raising=False)
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example,example.internal")


@pytest.fixture(scope="session")
def runner():
    """The runner module, loaded from its file as spark-submit runs it (never imported)."""
    spec = importlib.util.spec_from_file_location("forge_spark_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
