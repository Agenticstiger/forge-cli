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

"""An env-templated bucket, through the acquisition dispatcher a run goes through.

``base._execute_acquisition_build`` resolves every ``{{ env.X }}`` before the
runner sees the contract, and a missing or empty ``X`` becomes ``""``. The
Iceberg sink preflight read only that resolved contract, so
``acme-{{ env.LAKE_ENV }}-lake`` with ``LAKE_ENV`` unset reached it as
``acme--lake``: the preflight passed and the sink was pushed a warehouse in a
bucket the contract does not name. A fully templated bucket reached it as
``""`` and was refused with a message asking for the bucket the contract sets.
The BigQuery deriver did not render the template at all, and pushed a literal
``gs://{{ env.DATA_LAKE_BUCKET }}`` warehouse even with the variable set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.build_runners import base
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import resolve_iceberg_catalog

pytestmark = [pytest.mark.unit]

_VAR = "FW2_LAKE_ENV"
_FULL = "{{ env.%s }}" % _VAR
_PARTIAL = "acme-{{ env.%s }}-lake" % _VAR


def _binding(catalog: str, bucket: str, **location: Any) -> Dict[str, Any]:
    platform = "gcp" if catalog == "bigquery" else "aws"
    loc: Dict[str, Any] = {"database": "streaming", "table": "orders", "catalog": catalog}
    loc["bucket"] = bucket
    if catalog == "bigquery":
        loc["project"] = "acme-proj"
    else:
        loc["region"] = "eu-west-1"
    loc.update(location)
    return {"platform": platform, "format": "iceberg", "location": loc}


def _contract(
    binding: Dict[str, Any],
    engine: str,
    *,
    auto_create: bool = False,
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
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
        if auto_create:
            props["kafka-connect"]["streamingSink"] = {"autoCreate": True}
        if overrides:
            props["kafka-connect"]["iceberg_catalog_overrides"] = dict(overrides)
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
                "binding": binding,
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


def _dispatch(contract: Dict[str, Any], tmp_path: Path, monkeypatch) -> int:
    monkeypatch.chdir(tmp_path)
    return base._execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=False)


def _sink_warehouse(pushed: List[Tuple[str, Dict[str, Any]]]) -> Optional[str]:
    sinks = [cfg for name, cfg in pushed if name.endswith("-sink")]
    assert len(sinks) == 1, pushed
    return sinks[0].get("iceberg.catalog.warehouse")


def _props_path(tmp_path: Path) -> Path:
    return tmp_path / ".fluid" / "debezium" / "bronze.o" / "b1" / "application.properties"


def _preflight_failure(caplog) -> str:
    # The runner returns 1; the message is in its error log.
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "iceberg sink preflight failed" in text, text
    return text


# ---------------------------------------------------------------------------
# Kafka Connect, through the dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("catalog", ["dynamodb", "jdbc", "bigquery"])
@pytest.mark.parametrize("bucket", [_FULL, _PARTIAL], ids=["full", "partial"])
@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_kc_dispatch_refuses_a_bucket_that_does_not_resolve(
    fake_connect, tmp_path, monkeypatch, caplog, catalog, bucket, value
):
    _set(monkeypatch, value)
    extra = {"uri": "jdbc:postgresql://pg:5432/iceberg"} if catalog == "jdbc" else {}
    # A Kafka Connect BigQuery sink reads the warehouse only to auto-create
    # tables, so it is refused only with auto-create on.
    binding = _binding(catalog, bucket, **extra)
    contract = _contract(binding, "kafka-connect", auto_create=catalog == "bigquery")
    with caplog.at_level(logging.INFO):
        rc = _dispatch(contract, tmp_path, monkeypatch)
    assert rc == 1
    # Nothing reached the cluster: no sink in another bucket, no source.
    assert fake_connect == []
    text = _preflight_failure(caplog)
    assert f"{_VAR} is unset or empty in the runner's environment" in text
    assert repr(bucket) in text
    assert "requires binding.location.warehouse" not in text
    # `fluid validate` only warns about this, so the refusal does not send
    # the operator there.
    assert "see `fluid validate`" not in text


@pytest.mark.parametrize(
    "catalog, bucket, expected",
    [
        ("dynamodb", _PARTIAL, "s3://acme-prod-lake/streaming/orders/"),
        ("dynamodb", _FULL, "s3://prod/streaming/orders/"),
        ("bigquery", _PARTIAL, "gs://acme-prod-lake"),
        ("bigquery", _FULL, "gs://prod"),
    ],
    ids=["ddb-partial", "ddb-full", "bq-partial", "bq-full"],
)
def test_kc_dispatch_derives_from_the_resolved_bucket(
    fake_connect, tmp_path, monkeypatch, catalog, bucket, expected
):
    _set(monkeypatch, "prod")
    rc = _dispatch(_contract(_binding(catalog, bucket), "kafka-connect"), tmp_path, monkeypatch)
    assert rc == 0
    assert _sink_warehouse(fake_connect) == expected


def test_kc_dispatch_a_set_warehouse_ignores_the_bucket_variable(
    fake_connect, tmp_path, monkeypatch
):
    _set(monkeypatch, None)
    binding = _binding("dynamodb", _PARTIAL, warehouse="s3://ops/wh/")
    assert _dispatch(_contract(binding, "kafka-connect"), tmp_path, monkeypatch) == 0
    assert _sink_warehouse(fake_connect) == "s3://ops/wh/"


def test_the_dispatcher_scope_ends_with_the_run(fake_connect, tmp_path, monkeypatch):
    # Outside the dispatcher the preflight reads the contract it is handed.
    _set(monkeypatch, None)
    contract = _contract(_binding("dynamodb", _PARTIAL), "kafka-connect")
    assert _dispatch(contract, tmp_path, monkeypatch) == 1
    resolved = base._resolve_env_placeholders(contract)
    assert iceberg_sink_preflight(resolved, "b1") is None


# ---------------------------------------------------------------------------
# Debezium Server (embedded), through the dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bucket", [_FULL, _PARTIAL], ids=["full", "partial"])
@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_debezium_dispatch_refuses_a_bucket_that_does_not_resolve(
    tmp_path, monkeypatch, caplog, bucket, value
):
    _set(monkeypatch, value)
    contract = _contract(_binding("dynamodb", bucket), "debezium")
    with caplog.at_level(logging.INFO):
        rc = _dispatch(contract, tmp_path, monkeypatch)
    assert rc == 1
    assert not _props_path(tmp_path).exists()
    text = _preflight_failure(caplog)
    assert f"{_VAR} is unset or empty in the runner's environment" in text
    assert "acme--lake" not in text


def test_debezium_dispatch_derives_from_the_resolved_bucket(tmp_path, monkeypatch):
    _set(monkeypatch, "prod")
    # No server binary on PATH: the runner fails after writing the config.
    _dispatch(_contract(_binding("dynamodb", _PARTIAL), "debezium"), tmp_path, monkeypatch)
    text = _props_path(tmp_path).read_text()
    assert "debezium.sink.iceberg.warehouse=s3://acme-prod-lake/streaming/orders/" in text


# ---------------------------------------------------------------------------
# BigQuery: the deriver renders the bucket, in all three states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, warehouse",
    [("mylake", "gs://mylake"), ("", ""), (None, "")],
    ids=["set", "empty", "unset"],
)
@pytest.mark.parametrize("auto_create", [False, True], ids=["no-autocreate", "autocreate"])
def test_bigquery_env_bucket_derive_and_validate(monkeypatch, value, warehouse, auto_create):
    _set(monkeypatch, value)
    binding = _binding("bigquery", _FULL)
    resolved = resolve_iceberg_catalog(binding)
    assert resolved.warehouse == warehouse
    assert "{{" not in resolved.warehouse
    assert resolved.extra_catalog_props["gcp.bigquery.project-id"] == "acme-proj"

    contract = _contract(binding, "kafka-connect", auto_create=auto_create)
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    named = [w for w in warnings if _VAR in w]
    refusal = iceberg_sink_preflight(contract, "b1")
    if value:
        assert named == [] and refusal is None
        assert not [w for w in warnings if "no gs:// warehouse" in w], warnings
    else:
        # One warning naming the variable, not the generic no-warehouse one.
        assert len(named) == 1, warnings
        assert f"{_VAR} is unset or empty here" in named[0]
        assert not [w for w in warnings if "no gs:// warehouse" in w], warnings
        # The run refuses only when the sink would read the warehouse.
        assert ("the runner refuses the build" in named[0]) is auto_create
        if auto_create:
            assert refusal is not None
            assert f"{_VAR} is unset or empty in the runner's environment" in refusal
        else:
            assert refusal is None


@pytest.mark.parametrize("bucket", [_FULL, _PARTIAL], ids=["full", "partial"])
@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_kc_bigquery_without_autocreate_runs_and_names_no_other_bucket(
    fake_connect, tmp_path, monkeypatch, caplog, bucket, value
):
    _set(monkeypatch, value)
    contract = _contract(_binding("bigquery", bucket), "kafka-connect")
    with caplog.at_level(logging.INFO):
        rc = _dispatch(contract, tmp_path, monkeypatch)
    assert rc == 0, caplog.text
    sinks = [cfg for _name, cfg in fake_connect if cfg.get("iceberg.catalog.type") == "bigquery"]
    assert len(sinks) == 1, fake_connect
    # No warehouse in a bucket the contract does not name (acme--lake, gs://).
    pushed = sinks[0].get("iceberg.catalog.warehouse", "")
    assert pushed in ("",), sinks[0]


def test_bigquery_partial_bucket_with_an_empty_variable_derives_none(monkeypatch):
    _set(monkeypatch, "")
    assert resolve_iceberg_catalog(_binding("bigquery", _PARTIAL)).warehouse == ""


def test_bigquery_gs_warehouse_wins_over_an_unresolved_bucket(monkeypatch):
    _set(monkeypatch, None)
    binding = _binding("bigquery", _FULL, warehouse="gs://wh/x")
    assert resolve_iceberg_catalog(binding).warehouse == "gs://wh/x"
    errors, warnings = validate_iceberg_sink(_contract(binding, "kafka-connect"))
    assert errors == [] and not [w for w in warnings if _VAR in w], warnings


def test_dynamodb_partial_bucket_with_an_empty_variable_derives_none(monkeypatch):
    # Rendering it would give s3://acme--lake/..., a bucket the contract does
    # not name; validate warns and names the variable instead.
    _set(monkeypatch, "")
    binding = _binding("dynamodb", _PARTIAL)
    assert resolve_iceberg_catalog(binding).warehouse == ""
    errors, warnings = validate_iceberg_sink(_contract(binding, "kafka-connect"))
    assert errors == []
    assert any(f"{_VAR} is unset or empty here" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# Debezium BigQuery with no warehouse: the server does not boot
# ---------------------------------------------------------------------------


def test_debezium_bigquery_no_warehouse_warning_says_the_server_refuses_to_boot():
    binding = _binding("bigquery", "")
    binding["location"].pop("bucket")
    errors, warnings = validate_iceberg_sink(_contract(binding, "debezium"))
    assert errors == []
    hit = [w for w in warnings if "no gs:// warehouse" in w]
    assert len(hit) == 1, warnings
    assert "refuses to boot" in hit[0]
    assert "debezium.sink.iceberg.warehouse" in hit[0]
    assert "tables that exist need none" not in hit[0]


def test_kafka_connect_bigquery_no_warehouse_warning_is_unchanged():
    binding = _binding("bigquery", "")
    binding["location"].pop("bucket")
    warnings = validate_iceberg_sink(_contract(binding, "kafka-connect"))[1]
    hit = [w for w in warnings if "no gs:// warehouse" in w]
    assert len(hit) == 1 and "without auto-create, tables that exist need none" in hit[0]


@pytest.mark.parametrize(
    "catalog, warehouse",
    [
        ("dynamodb", "s3://ops-lake/wh"),
        ("jdbc", "s3://ops-lake/wh"),
        ("bigquery", "gs://ops-lake/wh"),
    ],
)
@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_kc_dispatch_keeps_an_override_warehouse(
    fake_connect, tmp_path, monkeypatch, caplog, catalog, warehouse, value
):
    _set(monkeypatch, value)
    extra = {"uri": "jdbc:postgresql://pg:5432/iceberg"} if catalog == "jdbc" else {}
    contract = _contract(
        _binding(catalog, _FULL, **extra),
        "kafka-connect",
        overrides={"iceberg.catalog.warehouse": warehouse},
    )
    with caplog.at_level(logging.INFO):
        rc = _dispatch(contract, tmp_path, monkeypatch)
    assert rc == 0, caplog.text
    sinks = [cfg for _name, cfg in fake_connect if "iceberg.tables" in cfg]
    assert len(sinks) == 1, fake_connect
    assert sinks[0].get("iceberg.catalog.warehouse") == warehouse, sinks[0]
