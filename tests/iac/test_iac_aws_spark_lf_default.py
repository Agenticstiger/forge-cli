# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A forge Spark job's own Lake Formation grants, for an output whose binding declares no
``governance.lakeFormation``: granted where the account's Lake Formation settings govern new
tables, decided at plan by a data source, and nowhere else."""

from __future__ import annotations

import copy
import json
import subprocess

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.module import build_module
from tests.iac.test_iac_aws_spark_infra import (  # noqa: F401 - operator_policy is a fixture
    _TOFU,
    CONTRACT,
    KEY,
    _runtime,
    emit,
    operator_policy,
)

pytestmark = [pytest.mark.unit, pytest.mark.provider]

SETTINGS = "commerce_orders_stream_lf_settings"
GRANT = "commerce_orders_stream_lf_spark_writer_order_lines_0"
DEFAULTS = f"data.aws_lakeformation_data_lake_settings.{SETTINGS}.create_table_default_permissions"


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


def test_the_job_role_is_granted_its_table_where_lake_formation_governs(contract):
    resources, data = emit(contract)
    assert data["aws_lakeformation_data_lake_settings"] == {SETTINGS: {}}
    grant = resources["aws_lakeformation_permissions"][GRANT]
    # 1 where no IAM_ALLOWED_PRINCIPALS default is left for new tables, else 0.
    assert grant["count"] == (
        f'${{length([for d in {DEFAULTS} : d if d.principal == "IAM_ALLOWED_PRINCIPALS"]) '
        "== 0 ? 1 : 0}"
    )
    assert grant["principal"] == f"${{aws_iam_role.{KEY}.arn}}"
    assert grant["permissions"] == ["ALTER", "DESCRIBE", "INSERT", "SELECT"]  # a stream appends
    assert grant["table"] == [
        {
            "database_name": "${aws_glue_catalog_table.commerce_orders_stream_commerce_order_lines"
            ".database_name}",
            "name": "${aws_glue_catalog_table.commerce_orders_stream_commerce_order_lines.name}",
        }
    ]


def test_a_declared_lake_formation_block_keeps_its_grants_unconditional(contract):
    contract["exposes"][0]["binding"]["governance"] = {"lakeFormation": {"grants": []}}
    resources, data = emit(contract)
    assert "count" not in resources["aws_lakeformation_permissions"][GRANT]
    assert "aws_lakeformation_data_lake_settings" not in data


def test_an_emr_on_ec2_job_runs_as_the_clusters_role_and_is_granted_nothing(contract):
    resources, data = emit(_runtime(contract, "emr-ec2"))
    assert GRANT not in resources.get("aws_lakeformation_permissions", {})
    assert "aws_lakeformation_data_lake_settings" not in data


def test_a_contract_without_a_spark_build_reads_no_settings(contract):
    contract.pop("builds")
    _, data = emit(contract)
    assert "aws_lakeformation_data_lake_settings" not in data


def test_a_table_in_another_catalog_gets_no_lake_formation_grant():
    from fluid_build.iac.providers import aws, aws_ingest

    binding = {"platform": "aws", "format": "iceberg", "location": {"catalog": "lakekeeper"}}
    loc = {"database": "commerce", "table": "order_lines", "catalog": "lakekeeper"}
    resources: dict = {}
    writers = [aws_ingest.Writer("${aws_iam_role.r.arn}", False)]
    aws._emit_default_spark_writer_grants(
        resources, binding, loc, "iceberg", "c", writers, SETTINGS
    )
    assert resources == {}


def test_the_module_text_is_the_same_in_every_account(contract):
    """The settings are read at plan: nothing forge emits depends on the account."""
    first = build_module(get_iac_plugin("aws"), copy.deepcopy(contract))
    second = build_module(get_iac_plugin("aws"), copy.deepcopy(contract))
    assert first == second
    assert "aws_lakeformation_data_lake_settings" in json.loads(first)["data"]


@pytest.mark.integration
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
def test_the_counted_grant_passes_tofu_validate(contract, tmp_path):
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    (tmp_path / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), contract))
    _tofu_init_or_skip(tmp_path)
    done = subprocess.run(
        [_TOFU, "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr or done.stdout
