# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A partitioned Glue table through a real ``tofu plan`` / ``apply``.

A moto ``ThreadedMotoServer`` stands in for S3 and Glue (the harness of
``test_iac_aws_retention_encryption_moto.py``), so no AWS account and no credentials
are involved. What is measured, with OpenTofu and ``hashicorp/aws`` 6:

* a table emitted with ``partition_keys`` and Athena projection applies, and plans
  clean afterwards (the projection sits in ``parameters``, which are ignored);
* a live table whose keys or projection change, or that gets its first keys, is
  replaced (the ``terraform_data`` trigger: its update, and its creation, fire
  ``replace_triggered_by``), so the data-loss gate counts the removal;
* a live table that loses its keys is updated in place (destroying the trigger fires
  nothing), which ``plan_data_changes`` reports, so the gate holds that apply too.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest import mock

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner

from . import _tofu_moto

pytestmark = [pytest.mark.integration, pytest.mark.aws, pytest.mark.provider]

REGION = "us-east-1"
TABLE = "aws_glue_catalog_table.partition_moto_sales_orders"
TRIGGER = "terraform_data.partition_moto_sales_orders_partitioning"


pytestmark.append(
    pytest.mark.skipif(
        runner.tofu_path() is None or not _tofu_moto.have_moto_server(),
        reason="needs `tofu` on PATH + moto server extra (pip install 'moto[glue,server]')",
    )
)


@pytest.fixture
def moto_endpoint() -> Iterator[str]:
    yield from _tofu_moto.moto_endpoint()


@pytest.fixture(scope="module")
def tofu_env(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, str]:
    return _tofu_moto.isolated_env(tmp_path_factory.mktemp("tofu-plugin-cache"))


def _contract(partition_by: Optional[List[str]], *, regions: str = "eu | us") -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "bucket": "partition-moto-lake",
        "path": "bronze/orders/",
        "database": "sales",
        "table": "orders",
    }
    if partition_by is not None:
        location["partitionBy"] = partition_by
    schema = [
        {"name": "id", "type": "string"},
        {"name": "order_date", "type": "date", "required": True},
        {
            "name": "region",
            "type": "string",
            "required": True,
            "validationRules": [{"type": "enum", "constraint": regions}],
        },
    ]
    return {
        "fluidVersion": "0.7.7",
        "id": "partition.moto",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "aws", "format": "parquet", "location": location},
                "contract": {"schema": schema},
            }
        ],
    }


def _write(contract: Dict[str, Any], workdir: Path, endpoint: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, {"AWS_ENDPOINT_URL": endpoint}):
        module = json.loads(build_module(get_iac_plugin("aws"), contract))
    module["provider"] = {
        "aws": {
            "region": REGION,
            "access_key": "testing",
            "secret_key": "testing",  # pragma: allowlist secret — moto dummy
            "skip_credentials_validation": True,
            "skip_metadata_api_check": True,
            "skip_requesting_account_id": True,
            "s3_use_path_style": True,
            "endpoints": {svc: endpoint for svc in ("s3", "sts", "iam", "glue")},
        }
    }
    (workdir / "main.tf.json").write_text(json.dumps(module, indent=2))


def _plan(workdir: Path, env: Dict[str, str]) -> Dict[str, Any]:
    done = _tofu_moto.tofu(workdir, env, "plan", "-input=false", "-no-color", "-out=plan.bin")
    assert done.returncode == 0, f"tofu plan failed:\n{done.stdout}\n{done.stderr}"
    shown = _tofu_moto.tofu(workdir, env, "show", "-json", "plan.bin")
    assert shown.returncode == 0, shown.stderr
    return json.loads(shown.stdout)


def _actions(plan: Dict[str, Any]) -> Dict[str, List[str]]:
    return {c["address"]: c["change"]["actions"] for c in plan.get("resource_changes") or []}


def _apply(workdir: Path, env: Dict[str, str]) -> None:
    done = _tofu_moto.tofu(workdir, env, "apply", "-input=false", "-no-color", "plan.bin")
    assert done.returncode == 0, f"tofu apply failed:\n{done.stdout}\n{done.stderr}"


def _applied(contract: Dict[str, Any], workdir: Path, env: Dict[str, str], endpoint: str) -> None:
    _write(contract, workdir, endpoint)
    _tofu_moto.init(workdir, env)
    _plan(workdir, env)
    _apply(workdir, env)


def _gate_lines(plan: Dict[str, Any]) -> List[str]:
    return get_iac_plugin("aws").plan_data_changes(plan)


def test_partitioned_table_applies_and_plans_clean(tmp_path, tofu_env, moto_endpoint):
    _applied(_contract(["order_date", "region"]), tmp_path, tofu_env, moto_endpoint)
    import boto3

    glue = boto3.client(
        "glue",
        region_name=REGION,
        endpoint_url=moto_endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # pragma: allowlist secret — moto dummy
    )
    table = glue.get_table(DatabaseName="sales", Name="orders")["Table"]
    assert [(k["Name"], k["Type"]) for k in table["PartitionKeys"]] == [
        ("order_date", "date"),
        ("region", "string"),
    ]
    assert [c["Name"] for c in table["StorageDescriptor"]["Columns"]] == ["id"]
    assert table["Parameters"]["projection.enabled"] == "true"
    assert table["Parameters"]["projection.region.values"] == "eu,us"
    assert table["Parameters"]["projection.order_date.range"] == "1970-01-01,NOW"
    again = _plan(tmp_path, tofu_env)
    assert {a for acts in _actions(again).values() for a in acts} <= {"no-op"}


def test_changed_keys_replace_the_table(tmp_path, tofu_env, moto_endpoint):
    _applied(_contract(["order_date", "region"]), tmp_path, tofu_env, moto_endpoint)
    _write(_contract(["region", "order_date"]), tmp_path, moto_endpoint)
    actions = _actions(_plan(tmp_path, tofu_env))
    assert actions[TABLE] == ["delete", "create"]
    # ``input`` updates in place; ``replace_triggered_by`` fires on that update.
    assert actions[TRIGGER] == ["update"]


def test_changed_projection_replaces_the_table(tmp_path, tofu_env, moto_endpoint):
    """A new region a writer lays out must reach Athena's projection, which sits in the
    ignored ``parameters``: the table is replaced instead of keeping the old values."""
    _applied(_contract(["order_date", "region"]), tmp_path, tofu_env, moto_endpoint)
    _write(_contract(["order_date", "region"], regions="eu | us | apac"), tmp_path, moto_endpoint)
    assert _actions(_plan(tmp_path, tofu_env))[TABLE] == ["delete", "create"]


def test_first_keys_on_a_live_table_replace_it(tmp_path, tofu_env, moto_endpoint):
    """Creating the trigger fires ``replace_triggered_by`` too: the removal is gated."""
    _applied(_contract(None), tmp_path, tofu_env, moto_endpoint)
    _write(_contract(["order_date"]), tmp_path, moto_endpoint)
    actions = _actions(_plan(tmp_path, tofu_env))
    assert actions[TRIGGER] == ["create"]
    assert actions[TABLE] == ["delete", "create"]


def test_dropped_keys_on_a_live_table_are_gated(tmp_path, tofu_env, moto_endpoint):
    """Destroying the trigger fires nothing: the table is updated in place, and
    ``plan_data_changes`` reports it for the gate."""
    _applied(_contract(["order_date"]), tmp_path, tofu_env, moto_endpoint)
    _write(_contract(None), tmp_path, moto_endpoint)
    plan = _plan(tmp_path, tofu_env)
    actions = _actions(plan)
    assert actions[TRIGGER] == ["delete"]
    assert actions[TABLE] == ["update"]
    (line,) = _gate_lines(plan)
    assert line.startswith(
        "Glue table sales.orders: partition keys change in place ((order_date date) -> none)"
    )
    assert "order_date" in line.split("->", 1)[1]
