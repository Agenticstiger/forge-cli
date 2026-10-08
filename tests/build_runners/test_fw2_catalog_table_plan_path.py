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

"""An env-templated bucket on the plan path, and a sink that never reads it.

``fluid apply <plan>.json --mode amend-and-build`` runs
``run_builds_from_args(args, plan_data=...)``, which resolves the plan's
embedded contract with ``resolve_contract_env_templates`` before dispatching.
That resolver renders an empty variable to ``""``, so
``acme-{{ env.LAKE_ENV }}-lake`` reached the dispatcher as ``acme--lake`` and
the sink was pushed a warehouse in a bucket the contract does not name.

A hand-written (non-deriving) sink config never reads the bucket, but the
preflight refused it when a bucket variable was unset.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.build_runners import base
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)

pytestmark = [pytest.mark.unit]

_VAR = "FW2_PLAN_LAKE_ENV"
_PARTIAL = "acme-{{ env.%s }}-lake" % _VAR
_FULL = "{{ env.%s }}" % _VAR


def _contract(engine: str, catalog: str, bucket: str) -> Dict[str, Any]:
    platform = "gcp" if catalog == "bigquery" else "aws"
    loc: Dict[str, Any] = {
        "database": "streaming",
        "table": "orders",
        "catalog": catalog,
        "bucket": bucket,
    }
    if catalog == "bigquery":
        loc["project"] = "acme-proj"
    else:
        loc["region"] = "eu-west-1"
    props: Dict[str, Any] = {
        "source": {"kind": "postgres", "mode": "incremental_append", "streams": ["public.o"]},
        "sink": {"format": "iceberg"},
    }
    if engine == "debezium":
        props["debezium"] = {"deployment": {"mode": "embedded"}, "server": {"sink": {}}}
    else:
        props["kafka-connect"] = {
            "deployment": {"mode": "bring-your-own", "server_url": "http://c:8083"}
        }
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "bronze.o",
        "name": "o",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": platform, "format": "iceberg", "location": loc},
                "contract": {"schema": [{"name": "id", "type": "integer"}]},
            }
        ],
        "builds": [
            {
                "id": "b1",
                "pattern": "acquisition",
                "engine": engine,
                "outputs": ["orders"],
                "properties": props,
            }
        ],
    }


class _FakeConnect:
    """Records the connector configs the Kafka Connect runner pushes."""

    pushed: List[Tuple[str, Dict[str, Any]]] = []

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def get_connector(self, _name: str) -> None:
        return None

    def create_connector(self, name: str, cfg: Dict[str, Any]) -> None:
        type(self).pushed.append((name, dict(cfg)))

    update_config = create_connector

    def __getattr__(self, _name: str) -> Any:
        return lambda *a, **k: {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}]}


@pytest.fixture
def fake_connect(monkeypatch):
    from fluid_build.build_runners.kafka_connect import runner as kc_runner

    _FakeConnect.pushed = []
    monkeypatch.setattr(kc_runner, "KafkaConnectRestClient", _FakeConnect)
    return _FakeConnect.pushed


def _set(monkeypatch, value: Optional[str]) -> None:
    if value is None:
        monkeypatch.delenv(_VAR, raising=False)
    else:
        monkeypatch.setenv(_VAR, value)


def _run_plan(contract: Dict[str, Any], tmp_path: Path, monkeypatch) -> int:
    """``run_builds_from_args`` as ``fluid apply <plan>.json`` calls it."""
    monkeypatch.chdir(tmp_path)
    plan_data = {"contract": contract}
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(plan_data), encoding="utf-8")
    args = argparse.Namespace(
        contract=str(plan),
        build_id=None,
        dry_run=False,
        env=None,
        fail_fast=False,
        sample_rows=None,
    )
    return base.run_builds_from_args(
        args, logging.getLogger("fw2.plan"), force_run=True, plan_data=plan_data
    )


def _log(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _props_path(tmp_path: Path) -> Path:
    return tmp_path / ".fluid" / "debezium" / "bronze.o" / "b1" / "application.properties"


# ---------------------------------------------------------------------------
# The plan path reads the bucket template as the plan wrote it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("catalog", ["dynamodb", "jdbc", "bigquery"])
@pytest.mark.parametrize("bucket", [_PARTIAL, _FULL], ids=["partial", "full"])
@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_kc_plan_path_refuses_a_bucket_that_does_not_resolve(
    fake_connect, tmp_path, monkeypatch, caplog, catalog, bucket, value
):
    _set(monkeypatch, value)
    contract = _contract("kafka-connect", catalog, bucket)
    if catalog == "jdbc":
        contract["exposes"][0]["binding"]["location"]["uri"] = "jdbc:postgresql://pg/ice"
    with caplog.at_level(logging.INFO):
        rc = _run_plan(contract, tmp_path, monkeypatch)
    assert rc == 1
    # Nothing reached the cluster: no sink in a bucket the contract does not name.
    assert fake_connect == []
    text = _log(caplog)
    assert "iceberg sink preflight failed" in text
    assert f"{_VAR} is unset or empty in the runner's environment" in text
    assert "acme--lake" not in text


def test_kc_plan_path_derives_from_the_resolved_bucket(fake_connect, tmp_path, monkeypatch):
    _set(monkeypatch, "prod")
    rc = _run_plan(_contract("kafka-connect", "dynamodb", _PARTIAL), tmp_path, monkeypatch)
    assert rc == 0
    sinks = [cfg for name, cfg in fake_connect if name.endswith("-sink")]
    assert len(sinks) == 1
    assert sinks[0]["iceberg.catalog.warehouse"] == "s3://acme-prod-lake/streaming/orders/"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_debezium_plan_path_refuses_a_bucket_that_does_not_resolve(
    tmp_path, monkeypatch, caplog, value
):
    _set(monkeypatch, value)
    with caplog.at_level(logging.INFO):
        rc = _run_plan(_contract("debezium", "dynamodb", _PARTIAL), tmp_path, monkeypatch)
    assert rc == 1
    assert not _props_path(tmp_path).exists()
    text = _log(caplog)
    assert "iceberg sink preflight failed" in text
    assert f"{_VAR} is unset or empty in the runner's environment" in text
    assert "acme--lake" not in text


def test_debezium_plan_path_derives_from_the_resolved_bucket(tmp_path, monkeypatch):
    _set(monkeypatch, "prod")
    # No server binary on PATH: the runner fails after writing the config.
    _run_plan(_contract("debezium", "dynamodb", _PARTIAL), tmp_path, monkeypatch)
    text = _props_path(tmp_path).read_text()
    assert "debezium.sink.iceberg.warehouse=s3://acme-prod-lake/streaming/orders/" in text


def test_plan_path_matches_a_binding_the_plan_resolver_stripped(
    fake_connect, tmp_path, monkeypatch, caplog
):
    # ``resolve_contract_env_templates`` strips every string it renders; the
    # binding is still found in the plan as written.
    _set(monkeypatch, "")
    contract = _contract("kafka-connect", "dynamodb", " %s " % _PARTIAL)
    with caplog.at_level(logging.INFO):
        assert _run_plan(contract, tmp_path, monkeypatch) == 1
    assert fake_connect == []
    assert f"{_VAR} is unset or empty in the runner's environment" in _log(caplog)


# ---------------------------------------------------------------------------
# A hand-written sink config never reads the bucket
# ---------------------------------------------------------------------------


_HANDWRITTEN_BQ = {
    "connector.class": "org.apache.iceberg.connect.IcebergSinkConnector",
    "topics": "orders",
    "iceberg.tables": "streaming.orders",
    "iceberg.catalog.type": "bigquery",
    "iceberg.catalog.gcp.bigquery.project-id": "acme-proj",
}


def _handwritten_kc(catalog: str = "bigquery") -> Dict[str, Any]:
    contract = _contract("kafka-connect", catalog, _FULL)
    cfg = dict(_HANDWRITTEN_BQ)
    if catalog == "dynamodb":
        cfg.pop("iceberg.catalog.type")
        cfg.pop("iceberg.catalog.gcp.bigquery.project-id")
        cfg["iceberg.catalog.catalog-impl"] = "org.apache.iceberg.aws.dynamodb.DynamoDbCatalog"
        cfg["iceberg.catalog.warehouse"] = "s3://ops/wh/"
    contract["builds"][0]["properties"]["kafka-connect"]["sink_connector_config"] = cfg
    return contract


@pytest.mark.parametrize("catalog", ["bigquery", "dynamodb"])
def test_handwritten_kc_config_ignores_an_unset_bucket_variable(
    fake_connect, tmp_path, monkeypatch, catalog
):
    _set(monkeypatch, None)
    contract = _handwritten_kc(catalog)
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert not [w for w in warnings if _VAR in w], warnings
    assert iceberg_sink_preflight(contract, "b1") is None

    monkeypatch.chdir(tmp_path)
    rc = base._execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 0
    sinks = [cfg for name, cfg in fake_connect if name.endswith("-sink")]
    assert len(sinks) == 1
    pushed = {k: v for k, v in sinks[0].items() if k.startswith("iceberg.catalog.")}
    written = contract["builds"][0]["properties"]["kafka-connect"]["sink_connector_config"]
    assert pushed == {k: v for k, v in written.items() if k.startswith("iceberg.catalog.")}


def test_handwritten_debezium_config_ignores_an_unset_bucket_variable(monkeypatch):
    _set(monkeypatch, None)
    contract = _contract("debezium", "dynamodb", _FULL)
    contract["builds"][0]["properties"]["debezium"]["server"]["sink"] = {
        "config": {
            "catalog-impl": "org.apache.iceberg.aws.dynamodb.DynamoDbCatalog",
            "warehouse": "s3://ops/wh/",
        }
    }
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert not [w for w in warnings if _VAR in w], warnings
    assert iceberg_sink_preflight(contract, "b1") is None


def test_a_deriving_build_still_waits_on_the_bucket_variable(monkeypatch):
    # Control for the tests above: the same contract, derived, is named.
    _set(monkeypatch, None)
    contract = _contract("kafka-connect", "bigquery", _FULL)
    warnings = validate_iceberg_sink(contract)[1]
    assert [w for w in warnings if _VAR in w], warnings
