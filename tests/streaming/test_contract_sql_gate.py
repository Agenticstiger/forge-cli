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
    contract_sql_entry,
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
    pinned = contract_sql_entry(contract["id"], build)
    assert f"other.product,{pinned}" in caught.value.fix


def test_a_pinned_listing_approves_the_reviewed_sql_only(contract):
    build = contract["builds"][0]
    _props(contract)["sql"] = "SELECT * FROM source_events"
    pinned = contract_sql_entry(contract["id"], build)
    assert pinned.startswith("commerce.nested-events/ingest-orders@sha256:")
    refuse_unless_contract_sql_allowed(contract["id"], build, {CONTRACT_SQL_ALLOWLIST_ENV: pinned})
    # Whitespace around the SQL is not a change; anything else is.
    _props(contract)["sql"] = "\n  SELECT * FROM source_events  \n"
    refuse_unless_contract_sql_allowed(contract["id"], build, {CONTRACT_SQL_ALLOWLIST_ENV: pinned})
    _props(contract)["sql"] = "SELECT * FROM source_events UNION ALL SELECT * FROM lake.hr.pay"
    with pytest.raises(FlinkPolicyError) as caught:
        refuse_unless_contract_sql_allowed(
            contract["id"], build, {CONTRACT_SQL_ALLOWLIST_ENV: pinned}
        )
    assert "changed after it was listed" in caught.value.why
    assert pinned not in caught.value.fix
    assert contract_sql_entry(contract["id"], build) in caught.value.fix


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


def test_nothing_is_applied_before_the_gate_and_a_listed_build_proceeds(contract, monkeypatch):
    from unittest.mock import patch

    from fluid_build.streaming.compiler import compile_contract
    from fluid_build.streaming.runtime import Kubectl

    _props(contract)["sql"] = "SELECT * FROM source_events"
    monkeypatch.delenv(CONTRACT_SQL_ALLOWLIST_ENV, raising=False)
    with patch.object(Kubectl, "get") as get, patch.object(Kubectl, "apply") as apply:
        with pytest.raises(FlinkPolicyError):
            reconcile(contract)
    assert get.call_count == 0 and apply.call_count == 0
    (artifact,) = compile_contract(contract)
    running = copy.deepcopy(artifact.resources[1])
    running["metadata"]["generation"] = 2
    running["status"] = {
        "observedGeneration": 2,
        "jobManagerDeploymentStatus": "READY",
        "reconciliationStatus": {"state": "DEPLOYED"},
        "jobStatus": {"state": "RUNNING", "jobId": "a" * 32},
    }
    entry = contract_sql_entry(contract["id"], contract["builds"][0])
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, entry)
    with (
        patch.object(Kubectl, "get", side_effect=[None, None, running]),
        patch.object(Kubectl, "apply") as apply,
    ):
        assert reconcile(contract)[0]["status"] == "running"
    assert [c.args[0]["kind"] for c in apply.call_args_list] == ["ConfigMap", "FlinkDeployment"]
