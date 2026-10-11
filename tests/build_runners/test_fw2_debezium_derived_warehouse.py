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

"""An embedded Debezium sink over DynamoDB / JDBC exposes with derived warehouses.

A warehouse derived from ``location.bucket`` and ``path`` is that expose's
own table prefix, and the catalog creates every missing table under the one
warehouse the sink is given (apache-iceberg-1.10.0 DynamoDbCatalog.java:
163-186, JdbcCatalog.java:282-284). Writing two such exposes through the
first one's prefix put the second one's data inside the first one's prefix,
so differing derived warehouses are refused.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.api.runner import RunState
from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
from fluid_build.build_runners.debezium.runner import DebeziumRunner
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)

pytestmark = [pytest.mark.unit]

_JDBC_URI = "jdbc:postgresql://catalog-db:5432/iceberg"
_REFUSED = "derive different warehouses from binding.location.bucket and path"


def _expose(eid: str, kind: str, **location: Any) -> Dict[str, Any]:
    loc: Dict[str, Any] = {
        "database": "sales",
        "table": eid,
        "bucket": "lake",
        "region": "us-east-1",
        "catalog": kind,
    }
    if kind == "jdbc":
        loc["uri"] = _JDBC_URI
    loc.update(location)
    return {
        "exposeId": eid,
        "kind": "table",
        "binding": {"platform": "aws", "format": "iceberg", "location": loc},
        "contract": {"schema": [{"name": "id", "type": "integer"}]},
    }


def _contract(exposes: List[Dict[str, Any]], outputs: Optional[List[str]]) -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": "ingest",
        "pattern": "acquisition",
        "engine": "debezium",
        "capabilities": ["cdc", "streaming"],
        "properties": {
            "source": {
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "app",
                    "user": "u",
                    "password": "p",
                },
                "mode": "cdc",
                "streams": [f"public.{e['exposeId']}" for e in exposes],
                "watermark": {"strategy": "high_water_mark", "allowedLateness": "PT5M"},
            },
            "sink": {"format": "iceberg"},
            "debezium": {"deployment": {"mode": "embedded"}, "server": {"sink": {}}},
        },
    }
    if outputs is not None:
        build["outputs"] = outputs
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.cdc",
        "name": "Sales CDC",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "exposes": copy.deepcopy(exposes),
        "builds": [build],
    }


def _props(contract: Dict[str, Any], tmp_path: Path) -> Path:
    return tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"


def _run(contract: Dict[str, Any], tmp_path: Path):
    ctx = build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    return DebeziumRunner().run(ctx)


@pytest.mark.parametrize("kind", ["dynamodb", "jdbc"])
def test_a_restricted_expose_is_not_written_under_a_public_prefix(tmp_path: Path, kind):
    contract = _contract(
        [
            _expose("orders", kind, path="public/orders/"),
            _expose("payroll", kind, path="restricted/payroll/"),
        ],
        ["orders", "payroll"],
    )
    errors, _ = validate_iceberg_sink(contract)
    refusal = next((e for e in errors if _REFUSED in e), "")
    assert "'orders': 's3://lake/public/orders/'" in refusal, errors
    assert "'payroll': 's3://lake/restricted/payroll/'" in refusal, errors
    assert "Set one binding.location.warehouse on them" in refusal
    assert _REFUSED in (iceberg_sink_preflight(contract, "ingest") or "")
    result = _run(contract, tmp_path)
    assert result.state == RunState.FAILED
    assert _REFUSED in (result.error or "")
    assert not _props(contract, tmp_path).exists()


@pytest.mark.parametrize("kind", ["dynamodb", "jdbc"])
@pytest.mark.parametrize("outputs", [["orders", "refunds"], None], ids=["both", "no-outputs"])
def test_default_paths_of_one_database_are_refused(tmp_path: Path, kind, outputs):
    # ``path`` defaults to ``<database>/<table>/``: sales/orders/ and sales/refunds/.
    contract = _contract([_expose("orders", kind), _expose("refunds", kind)], outputs)
    errors, _ = validate_iceberg_sink(contract)
    assert any(_REFUSED in e for e in errors), errors
    assert _run(contract, tmp_path).state == RunState.FAILED
    assert not _props(contract, tmp_path).exists()


@pytest.mark.parametrize("kind", ["dynamodb", "jdbc"])
def test_one_shared_path_derives_one_warehouse(tmp_path: Path, kind):
    contract = _contract(
        [_expose("orders", kind, path="sales/"), _expose("refunds", kind, path="sales/")],
        None,
    )
    assert validate_iceberg_sink(contract) == ([], [])
    assert iceberg_sink_preflight(contract, "ingest") is None
    _run(contract, tmp_path)  # no server binary: fails after writing the config
    text = _props(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.warehouse=s3://lake/sales/" in text
    assert "debezium.sink.iceberg.table-namespace=sales" in text


@pytest.mark.parametrize("kind", ["dynamodb", "jdbc"])
def test_a_single_expose_keeps_its_derived_warehouse(tmp_path: Path, kind):
    contract = _contract([_expose("orders", kind)], ["orders"])
    assert validate_iceberg_sink(contract) == ([], [])
    _run(contract, tmp_path)
    text = _props(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.warehouse=s3://lake/sales/orders/" in text
