# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Removing a Spark build is not data loss: what it ran with (its runner and job config, role,
policies, log group and the key its logs are encrypted with, its Glue job or EMR Serverless
application) holds no data, so the data-loss gate does not count it (``aws_ingest.
spark_definition``, ``AwsIacPlugin.retired_definitions``). The table it wrote stays gated."""

from __future__ import annotations

import copy

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.providers.aws_ingest import spark_definition
from tests.iac.test_iac_aws_spark_infra import (  # noqa: F401 - operator_policy is a fixture
    CONTRACT,
    _runtime,
    emit,
    operator_policy,
)

pytestmark = [pytest.mark.unit, pytest.mark.provider]


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


def _addresses(resources):
    return {f"{kind}.{name}" for kind, bodies in resources.items() for name in bodies}


@pytest.mark.parametrize("runtime", ["emr-serverless", "glue", "emr-eks", "emr-ec2"])
def test_everything_a_spark_build_adds_is_a_definition(contract, runtime):
    """Every resource the build adds to the module is one the gate does not count: a new
    Spark resource must be named so, or be decided here."""
    with_build, _ = emit(_runtime(copy.deepcopy(contract), runtime))
    without = copy.deepcopy(contract)
    without.pop("builds")
    added = _addresses(with_build) - _addresses(emit(without)[0])
    assert added
    assert sorted(a for a in added if not spark_definition(a, a.split(".", 1)[0])) == []


def test_the_lake_formation_writer_role_and_the_caller_policy_are_definitions(contract):
    contract["builds"][0]["properties"]["stream"]["trigger"] = {"availableNow": True}
    contract["exposes"][0]["binding"]["governance"] = {
        "lakeFormation": {"registerLocation": True, "grants": []}
    }
    with_build, _ = emit(contract)
    without = copy.deepcopy(contract)
    without.pop("builds")
    added = _addresses(with_build) - _addresses(emit(without)[0])
    gated = sorted(a for a in added if not spark_definition(a, a.split(".", 1)[0]))
    # The writer's Lake Formation grant is access only, which the gate already skips.
    assert all(a.startswith("aws_lakeformation_permissions.") for a in gated), gated
    assert any("_lf_writer_" in a for a in added)
    assert any(a.endswith("_spark_caller") for a in added)


@pytest.mark.parametrize(
    "address",
    [
        "aws_kms_key.commerce_orders_stream_commerce_lake_kms",  # a bucket's own key
        "aws_glue_catalog_table.commerce_orders_stream_commerce_spark",  # a table
        "aws_s3_object.commerce_orders_stream_seed_data",
        "aws_s3_bucket.commerce_orders_stream_spark",
        "aws_iam_role.commerce_orders_stream_lambda",
        "module.other.aws_iam_role.x_spark",
    ],
)
def test_what_holds_data_or_is_not_forges_spark_stays_gated(address):
    kind = address.split(".")[0] if not address.startswith("module.") else "aws_iam_role"
    assert not spark_definition(address, kind)


REMOVED = [
    ("aws_s3_object.shop_orders_ingest_spark_runner", "aws_s3_object"),
    ("aws_s3_object.shop_orders_ingest_spark_config", "aws_s3_object"),
    ("aws_cloudwatch_log_group.shop_orders_ingest_spark", "aws_cloudwatch_log_group"),
    ("aws_iam_role.shop_orders_ingest_spark", "aws_iam_role"),
    ("aws_iam_role_policy.shop_orders_ingest_spark", "aws_iam_role_policy"),
    ("aws_emrserverless_application.shop_orders_spark", "aws_emrserverless_application"),
    ("aws_kms_key.shop_orders_spark_logs", "aws_kms_key"),
]


def _summary(remove):
    return {"add": 0, "change": 0, "remove": remove}


def test_a_removed_build_alone_does_not_reach_the_gate():
    plugin = get_iac_plugin("aws")
    counted, _ = engine._data_bearing_changes(_summary(len(REMOVED)), REMOVED, plugin)
    assert counted["remove"] == 0
    # Without the plugin's word, every removal counts, as before.
    counted, _ = engine._data_bearing_changes(_summary(len(REMOVED)), REMOVED)
    assert counted["remove"] == len(REMOVED)


def test_its_table_still_reaches_the_gate():
    table = ("aws_glue_catalog_table.shop_orders_shop_lines", "aws_glue_catalog_table")
    counted, _ = engine._data_bearing_changes(
        _summary(len(REMOVED) + 1), [*REMOVED, table], get_iac_plugin("aws")
    )
    assert counted["remove"] == 1


def test_the_gate_fails_closed_when_the_events_miss_a_removal():
    counted, _ = engine._data_bearing_changes(
        _summary(len(REMOVED) + 1), REMOVED, get_iac_plugin("aws")
    )
    assert counted["remove"] == len(REMOVED) + 1
    assert engine._build_definitions(get_iac_plugin("aws"), _summary(9), REMOVED) == []


def test_a_plugin_cannot_exempt_what_the_plan_does_not_remove():
    class Greedy:
        def retired_definitions(self, removals):
            return ["aws_s3_bucket.lake", *(a for a, _ in removals)]

    named = engine._build_definitions(Greedy(), _summary(len(REMOVED)), REMOVED)
    assert named == [a for a, _ in REMOVED]
