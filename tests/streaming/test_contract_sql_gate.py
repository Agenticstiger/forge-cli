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

"""A contract's own SQL runs only when the operator lists it.

forge-cli writes the query from ``properties.mapping`` and that query reads only the
stream. A contract's ``properties.sql`` runs with the job's catalog identity and can read
any table that identity can, so apply refuses it unless the operator lists the product.
"""

from __future__ import annotations

import copy

import pytest

from fluid_build.streaming.compiler import validation_errors, validation_warnings
from fluid_build.streaming.policy import (
    CONTRACT_SQL_ALLOWLIST_ENV,
    FlinkPolicyError,
    refuse_unless_contract_sql_allowed,
)
from fluid_build.streaming.runtime import reconcile


def _props(contract):
    return contract["builds"][0]["properties"]


def test_contract_sql_is_warned_about(contract):
    _props(contract)["sql"] = "SELECT * FROM source_events"
    (warning,) = validation_warnings(contract)
    assert CONTRACT_SQL_ALLOWLIST_ENV in warning and "commerce.nested-events" in warning


def test_apply_refuses_contract_sql_the_operator_did_not_list(contract, monkeypatch):
    _props(contract)["sql"] = "SELECT * FROM source_events"
    monkeypatch.delenv(CONTRACT_SQL_ALLOWLIST_ENV, raising=False)
    with pytest.raises(FlinkPolicyError, match="runs SQL written in the contract"):
        reconcile(contract, dry_run=True)
    build = contract["builds"][0]
    for entry in ("commerce.nested-events", "commerce.nested-events/ingest-orders"):
        refuse_unless_contract_sql_allowed(
            contract["id"], build, {CONTRACT_SQL_ALLOWLIST_ENV: entry}
        )
    with pytest.raises(FlinkPolicyError) as caught:
        refuse_unless_contract_sql_allowed(
            contract["id"], build, {CONTRACT_SQL_ALLOWLIST_ENV: "other.product"}
        )
    assert "other.product,commerce.nested-events/ingest-orders" in caught.value.fix


def test_a_mapped_build_needs_no_listing(contract, monkeypatch):
    monkeypatch.delenv(CONTRACT_SQL_ALLOWLIST_ENV, raising=False)
    refuse_unless_contract_sql_allowed(contract["id"], contract["builds"][0])
    assert validation_warnings(contract) == []


def test_a_second_build_with_the_same_id_cannot_vouch_for_the_first(contract, monkeypatch):
    # The SQL build comes first; a clean build reuses its id. Builds were looked up
    # by id at apply, so every check ran on the clean one and passed the other.
    monkeypatch.delenv(CONTRACT_SQL_ALLOWLIST_ENV, raising=False)
    first = contract["builds"][0]
    second = copy.deepcopy(first)
    first["properties"]["sql"] = "SELECT * FROM lake.secret_ns.payroll"
    deployment = second["properties"]["parameters"]["flink"]["deployment"]
    deployment["name"] = "commerce-orders-2"
    for key in ("checkpointUri", "savepointUri", "haUri"):
        deployment[key] = deployment[key].replace("/orders/", "/orders-2/")
    second["outputs"] = ["orders2"]
    expose = copy.deepcopy(contract["exposes"][0])
    expose["exposeId"] = "orders2"
    expose["binding"]["location"]["table"] = "orders2"
    contract["builds"].append(second)
    contract["exposes"].append(expose)
    with pytest.raises(ValueError, match="'ingest-orders' is used twice"):
        reconcile(contract, dry_run=True)
    assert any("used twice" in e for e in validation_errors(contract))
    # With distinct ids, the SQL build is judged as itself.
    second["id"] = "ingest-orders-2"
    with pytest.raises(FlinkPolicyError, match="'ingest-orders' runs SQL written in the contract"):
        reconcile(contract, dry_run=True)
