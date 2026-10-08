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

"""An env-templated bucket is an explicit bucket, set or not where validate runs.

DynamoDB and JDBC derive their warehouse from ``location.bucket``. A bucket
written as ``{{ env.DATA_LAKE_BUCKET }}`` (the form
``examples/aws-glue-data-lake/contract-iceberg.fluid.yaml`` uses) derives
nothing in a shell without that variable, and check 4 used to treat it as
absent: ``fluid validate`` in such a shell (a CI job, say) refused the
contract and asked for the bucket it already sets, while the same command with
the variable set accepted it. Validate now warns and names the variable; the
runner's preflight, in the environment the sink config is derived in, still
refuses when it does not resolve there.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from fluid_build.build_runners.debezium.iceberg_sink import emit_debezium_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import resolve_iceberg_catalog, unset_bucket_env_vars

pytestmark = [pytest.mark.unit]

_VAR = "FW2_DATA_LAKE_BUCKET"
_TEMPLATE = "{{ env.%s }}" % _VAR
_JDBC_URI = "jdbc:postgresql://pg:5432/iceberg"


def _binding(catalog: str, platform: str = "aws", **location: Any) -> Dict[str, Any]:
    loc = {"catalog": catalog, "database": "streaming", "table": "orders", **location}
    return {"platform": platform, "format": "iceberg", "location": loc}


def _contract(
    binding: Dict[str, Any], *, engine: str = "kafka-connect", props: Optional[Dict] = None
) -> Dict[str, Any]:
    build_props: Dict[str, Any] = {
        "source": {"kind": "postgres", "mode": "incremental_append"},
        "sink": {"format": "iceberg"},
    }
    if engine == "debezium":
        build_props["debezium"] = props or {
            "deployment": {"mode": "embedded"},
            "server": {"sink": {}},
        }
    else:
        build_props["kafka-connect"] = props or {}
    return {
        "id": "bronze.orders_stream",
        "builds": [{"id": "ingest", "engine": engine, "properties": build_props}],
        "exposes": [{"exposeId": "orders", "kind": "table", "binding": binding}],
    }


_TEMPLATED = [
    _binding("dynamodb", bucket=_TEMPLATE, region="eu-west-1"),
    _binding("jdbc", bucket=_TEMPLATE, region="eu-west-1", uri=_JDBC_URI),
]
_IDS = ["dynamodb", "jdbc"]


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)


@pytest.mark.parametrize("binding", _TEMPLATED, ids=_IDS)
@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_unresolved_bucket_template_warns_at_validate(unset, binding, engine):
    kind = binding["location"]["catalog"]
    errors, warnings = validate_iceberg_sink(_contract(binding, engine=engine))
    assert errors == []
    hit = [w for w in warnings if _VAR in w]
    assert len(hit) == 1, warnings
    assert f"the {kind} catalog's warehouse derives from binding.location.bucket" in hit[0]
    assert repr(_TEMPLATE) in hit[0]
    assert f"{_VAR} is unset or empty here" in hit[0]
    # It does not ask for the bucket the contract already sets.
    assert "requires binding.location.warehouse" not in hit[0]


@pytest.mark.parametrize("binding", _TEMPLATED, ids=_IDS)
@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_unresolved_bucket_template_is_refused_by_the_preflight(unset, binding, engine):
    refusal = iceberg_sink_preflight(_contract(binding, engine=engine), "ingest")
    assert refusal is not None
    assert f"{_VAR} is unset or empty in the runner's environment" in refusal
    assert "binding.location.warehouse" in refusal


def test_the_preflight_leaves_validate_in_validate_mode(unset):
    contract = _contract(_TEMPLATED[0])
    assert iceberg_sink_preflight(contract, "ingest") is not None
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == [] and any(_VAR in w for w in warnings)


@pytest.mark.parametrize("binding", _TEMPLATED, ids=_IDS)
@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_resolved_bucket_template_validates_clean_and_derives(monkeypatch, binding, engine):
    monkeypatch.setenv(_VAR, "acme-lake")
    contract = _contract(binding, engine=engine)
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert not [w for w in warnings if _VAR in w or "warehouse" in w], warnings
    assert iceberg_sink_preflight(contract, "ingest") is None
    resolved = resolve_iceberg_catalog(binding, account_ref="123456789012")
    if engine == "debezium":
        warehouse = emit_debezium_iceberg_sink_config(resolved)["warehouse"]
    else:
        cfg = emit_iceberg_sink_config(resolved, product_id="bronze.orders_stream", topics=["t"])
        warehouse = cfg["iceberg.catalog.warehouse"]
    assert warehouse == "s3://acme-lake/streaming/orders/"


def test_resolved_bucket_template_on_gcp_derives_gs(monkeypatch):
    monkeypatch.setenv(_VAR, "acme-lake")
    binding = _binding("jdbc", "gcp", bucket=_TEMPLATE, uri=_JDBC_URI)
    assert resolve_iceberg_catalog(binding).warehouse == "gs://acme-lake/streaming/orders/"


def test_several_unset_variables_are_all_named(monkeypatch):
    monkeypatch.delenv("FW2_A", raising=False)
    monkeypatch.delenv("FW2_B", raising=False)
    binding = _binding("dynamodb", bucket="{{ env.FW2_A }}-{{ env.FW2_B }}", region="eu-west-1")
    assert unset_bucket_env_vars(binding) == ("FW2_A", "FW2_B")
    warnings = validate_iceberg_sink(_contract(binding))[1]
    assert any("FW2_A, FW2_B are unset or empty here" in w and "Set them" in w for w in warnings)


def test_only_the_variables_without_a_value_are_named(monkeypatch):
    monkeypatch.setenv("FW2_A", "acme")
    monkeypatch.delenv("FW2_B", raising=False)
    binding = _binding("dynamodb", bucket="{{ env.FW2_B }}", region="eu-west-1")
    assert unset_bucket_env_vars(binding) == ("FW2_B",)
    binding = _binding("dynamodb", bucket="{{ env.FW2_A }}", region="eu-west-1")
    assert unset_bucket_env_vars(binding) == ()


@pytest.mark.parametrize("binding", _TEMPLATED, ids=_IDS)
def test_an_empty_variable_is_treated_like_an_unset_one(monkeypatch, binding):
    # An empty value resolves the bucket to "", which derives no warehouse; the
    # message still names the variable instead of asking for a bucket.
    monkeypatch.setenv(_VAR, "")
    contract = _contract(binding)
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert any(f"{_VAR} is unset or empty here" in w for w in warnings), warnings
    refusal = iceberg_sink_preflight(contract, "ingest")
    assert refusal is not None
    assert f"{_VAR} is unset or empty in the runner's environment" in refusal
    assert "requires binding.location.warehouse" not in refusal


@pytest.mark.parametrize(
    "binding",
    [
        # nothing resolves a template that is not {{ env.* }}
        _binding("dynamodb", bucket="{{ var.lake }}", region="eu-west-1"),
        _binding("dynamodb", bucket="{{ env.%s }}-{{ var.x }}" % _VAR, region="eu-west-1"),
        # no scheme to derive with off aws / gcp, template or not
        _binding("dynamodb", "local", bucket=_TEMPLATE),
    ],
    ids=["non-env-template", "mixed-template", "local-platform"],
)
def test_a_bucket_that_cannot_derive_anywhere_stays_a_hard_error(unset, binding):
    assert unset_bucket_env_vars(binding) == ()
    errors = validate_iceberg_sink(_contract(binding))[0]
    assert any("dynamodb catalog requires binding.location.warehouse" in e for e in errors)


def test_an_override_warehouse_needs_no_bucket_warning(unset):
    contract = _contract(
        _TEMPLATED[0],
        props={"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://ops/wh/"}},
    )
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == [] and not [w for w in warnings if _VAR in w], warnings
    assert iceberg_sink_preflight(contract, "ingest") is None


def test_glue_with_an_unresolved_template_is_unchanged(unset):
    binding = _binding("glue", bucket=_TEMPLATE, region="eu-west-1")
    contract = _contract(binding)
    assert validate_iceberg_sink(contract) == ([], [])
    assert iceberg_sink_preflight(contract, "ingest") is None


_CONTRACT_YAML = (
    """\
fluidVersion: 0.7.6
kind: DataProduct
domain: sales
metadata:
  layer: Bronze
  owner:
    team: dp
id: bronze.o
name: o
exposes:
- exposeId: orders
  kind: table
  binding:
    platform: aws
    format: iceberg
    location:
      database: streaming
      table: orders
      catalog: dynamodb
      bucket: '{{ env.%s }}'
      region: eu-west-1
  contract:
    schema:
    - name: id
      type: integer
builds:
- id: b1
  pattern: acquisition
  engine: kafka-connect
  outputs:
  - orders
  properties:
    source:
      kind: postgres
      mode: incremental_append
      streams:
      - public.o
    sink:
      format: iceberg
    kafka-connect:
      deployment:
        mode: bring-your-own
        server_url: http://c:8083
"""
    % _VAR
)


@pytest.mark.parametrize("value", [None, "", "acme-lake"], ids=["unset", "empty", "set"])
def test_cli_validate_accepts_the_contract_with_or_without_the_variable(tmp_path, value):
    path = tmp_path / "kc_ddb_envbucket.fluid.yaml"
    path.write_text(textwrap.dedent(_CONTRACT_YAML), encoding="utf-8")
    env = os.environ.copy()
    env.pop(_VAR, None)
    if value is not None:
        env[_VAR] = value
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["FLUID_NONINTERACTIVE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "fluid_build", "validate", str(path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "requires binding.location.warehouse" not in out
    assert (_VAR in out) is (not value), out
