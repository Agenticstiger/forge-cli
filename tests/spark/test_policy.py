# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Contract SQL runs on Spark only for a build the operator lists, as on Flink."""

import pytest

from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from fluid_build.spark.policy import SparkPolicyError, refuse_unless_contract_sql_allowed
from fluid_build.streaming.policy import contract_sql_entry

BUILD = {"id": "compact", "properties": {"sql": "  SELECT 1 AS x  "}}


def allowed(entries):
    refuse_unless_contract_sql_allowed("p", BUILD, {CONTRACT_SQL_ALLOWLIST_ENV: entries})


def test_an_unset_list_allows_nothing():
    with pytest.raises(SparkPolicyError) as refused:
        refuse_unless_contract_sql_allowed("p", BUILD, {})
    assert refused.value.code == "SparkPolicyError"
    assert "p/compact is not in" in refused.value.why
    # The fix names the pinned entry; it never echoes the SQL.
    assert contract_sql_entry("p", BUILD) in refused.value.fix
    assert "SELECT" not in refused.value.fix


@pytest.mark.parametrize("entries", ["p", "p/compact", " other , p/compact "])
def test_a_product_or_build_entry_allows_its_sql(entries):
    allowed(entries)


def test_a_pinned_entry_allows_only_the_reviewed_sql():
    allowed(contract_sql_entry("p", BUILD))
    changed = {"id": "compact", "properties": {"sql": "SELECT 2 AS x"}}
    with pytest.raises(SparkPolicyError) as refused:
        refuse_unless_contract_sql_allowed(
            "p", changed, {CONTRACT_SQL_ALLOWLIST_ENV: contract_sql_entry("p", BUILD)}
        )
    assert "changed after it was listed" in refused.value.why


def test_the_flink_list_does_not_allow_spark_sql():
    with pytest.raises(SparkPolicyError):
        refuse_unless_contract_sql_allowed("p", BUILD, {"FLUID_FLINK_CONTRACT_SQL_ALLOWLIST": "p"})


def test_a_build_without_sql_needs_no_entry():
    refuse_unless_contract_sql_allowed("p", {"id": "x", "properties": {"mapping": {}}}, {})
