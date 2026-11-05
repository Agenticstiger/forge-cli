# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid verify``'s retention dimension for a Databricks expose with ``lifecycle.expire``.

The contract's retention must cover the table's live Delta history (the larger of
``delta.logRetentionDuration`` and ``delta.deletedFileRetentionDuration``, 30 and 7 days when
unset); predictive optimization off is a warning. Driven through ``verify_databricks_expose``
with the stub of ``test_verify_databricks.py``, and once over HTTP against the stand-in workspace.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

import pytest

from fluid_build.cli._verify_databricks import verify_databricks_expose
from tests.cli.test_verify_databricks import (
    CONTRACT,
    LIVE_COLUMNS,
    TABLE,
    StubWorkspace,
)

pytestmark = pytest.mark.unit

DAY = 86400


class RetentionWorkspace(StubWorkspace):
    """The stub, with Delta properties and a predictive optimization flag on GetTable."""

    def __init__(self) -> None:
        super().__init__()
        self.extras: Dict[str, Any] = {}
        #: GetTable serves a managed Delta table's properties (``delta.*``) with it.
        self.serves_properties = True

    def answer(self, method: str, path: str, query: Dict[str, Any]) -> Dict[str, Any]:
        out = super().answer(method, path, query)
        if path == f"/api/2.1/unity-catalog/tables/{TABLE}":
            served = {"properties": {"delta.minReaderVersion": "3"}}
            out = {**(served if self.serves_properties else {}), **out}
            out = {**out, **copy.deepcopy(self.extras)}
        return out


def _contract(retention: Optional[str] = "P365D", expire: bool = True) -> Dict[str, Any]:
    contract = copy.deepcopy(CONTRACT)
    if retention is not None:
        contract["exposes"][0]["lifecycle"] = {"retention": retention, "expire": expire}
    contract["builds"] = [
        {
            "id": "dbt_daily",
            "engine": "dbt",
            "repository": "./dbt",
            "properties": {"model": "orders"},
            "outputs": ["orders"],
            "execution": {"runtime": {"platform": "databricks"}},
        }
    ]
    return contract


def _verify(workspace: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
    return verify_databricks_expose(
        "orders",
        contract["exposes"][0],
        contract=contract,
        client_factory=lambda: workspace,
    )


@pytest.fixture
def workspace() -> RetentionWorkspace:
    ws = RetentionWorkspace()
    ws.add_jobs("fluid dbx.fake.orders dbt_daily", 1)
    return ws


def _dimension(result: Dict[str, Any]) -> Dict[str, Any]:
    assert "retention" in result["dimensions"], result
    return result["dimensions"]["retention"]


def test_no_expire_skips(workspace):
    result = _verify(workspace, _contract(None))
    assert _dimension(result)["status"] == "skip"
    assert result["status"] == "match"


def test_expire_false_skips(workspace):
    assert _dimension(_verify(workspace, _contract("P365D", expire=False)))["status"] == "skip"


def test_delta_defaults_inside_the_retention_pass(workspace):
    workspace.extras = {"effective_predictive_optimization_flag": {"value": "ENABLE"}}
    result = _verify(workspace, _contract())
    dim = _dimension(result)
    assert dim["status"] == "pass", dim
    assert dim["history_seconds"] == 30 * DAY and dim["retention"] == "P365D"
    assert dim["predictive_optimization"] == "ENABLE" and dim["warnings"] == []
    assert result["status"] == "match" and result["severity"]["level"] == "SUCCESS"


def test_a_history_exactly_the_retention_passes(workspace):
    assert _dimension(_verify(workspace, _contract("P30D")))["status"] == "pass"


def test_a_history_longer_than_the_retention_is_critical_with_the_fix(workspace):
    workspace.extras = {"properties": {"delta.logRetentionDuration": "interval 400 days"}}
    result = _verify(workspace, _contract("P365D"))
    dim = _dimension(result)
    assert dim["status"] == "fail" and "400 days" in dim["message"]
    assert "ALTER TABLE `main`.`sales`.`orders` SET TBLPROPERTIES" in dim["fix"]
    assert "'delta.logRetentionDuration' = 'interval 365 days'" in dim["fix"]
    assert result["status"] == "mismatch" and result["severity"]["level"] == "CRITICAL"
    assert any("ALTER TABLE" in action for action in result["severity"]["actions"])


def test_the_deleted_file_retention_counts(workspace):
    workspace.extras = {"properties": {"delta.deletedFileRetentionDuration": "interval 500 days"}}
    assert _dimension(_verify(workspace, _contract("P365D")))["status"] == "fail"


def test_a_history_property_of_the_runtime_wins(workspace):
    workspace.extras = {
        "properties": {"delta.logRetentionDuration": "interval 10 days"},
        "delta_runtime_properties_kvpairs": {
            "delta_runtime_properties": {"delta.logRetentionDuration": "interval 400 days"}
        },
    }
    assert _dimension(_verify(workspace, _contract("P365D")))["status"] == "fail"
    workspace.extras = {
        "delta_runtime_properties_kvpairs": {
            "delta_runtime_properties": {"delta.logRetentionDuration": "interval 60 days"}
        },
    }
    assert _dimension(_verify(workspace, _contract("P365D")))["status"] == "pass"


def test_an_unreadable_property_fails(workspace):
    workspace.extras = {"properties": {"delta.logRetentionDuration": "forever"}}
    dim = _dimension(_verify(workspace, _contract()))
    assert dim["status"] == "fail" and "forever" in dim["message"]


def test_predictive_optimization_off_is_a_warning_not_a_failure(workspace):
    workspace.extras = {"effective_predictive_optimization_flag": {"value": "DISABLE"}}
    result = _verify(workspace, _contract())
    dim = _dimension(result)
    assert dim["status"] == "warn" and dim["predictive_optimization"] == "DISABLE"
    (warning,) = dim["warnings"]
    assert "ALTER SCHEMA `main`.`sales` ENABLE PREDICTIVE OPTIMIZATION" in warning
    assert "VACUUM `main`.`sales`.`orders`" in warning
    assert result["status"] == "match" and result["severity"]["level"] == "SUCCESS"


def test_the_tables_own_flag_is_read_when_no_effective_one_is_served(workspace):
    workspace.extras = {"enable_predictive_optimization": "DISABLE"}
    assert _dimension(_verify(workspace, _contract()))["status"] == "warn"


@pytest.mark.parametrize(
    "extras",
    [
        {},
        {"enable_predictive_optimization": "INHERIT"},
        {"effective_predictive_optimization_flag": {}},
    ],
)
def test_a_flag_that_is_not_reported_is_not_a_warning(workspace, extras):
    workspace.extras = extras
    dim = _dimension(_verify(workspace, _contract()))
    assert dim["status"] == "pass" and dim["predictive_optimization"] is None


def test_a_failure_keeps_the_warning(workspace):
    workspace.extras = {
        "properties": {"delta.logRetentionDuration": "interval 400 days"},
        "effective_predictive_optimization_flag": {"value": "DISABLE"},
    }
    dim = _dimension(_verify(workspace, _contract("P60D")))
    assert dim["status"] == "fail" and len(dim["warnings"]) == 1


def test_the_other_dimensions_are_untouched(workspace):
    workspace.tables[TABLE] = copy.deepcopy(LIVE_COLUMNS)
    result = _verify(workspace, _contract())
    assert result["dimensions"]["structure"]["status"] == "pass"
    assert result["dimensions"]["jobs"]["status"] == "pass"


def test_a_table_the_module_refuses_to_expire_is_not_verified(workspace):
    contract = _contract()
    contract["builds"][0]["repository"] = "https://github.com/o/r?tag=v1"
    result = _verify(workspace, contract)
    assert result["status"] == "unsupported"


# ── over HTTP, against the stand-in workspace ───────────────────────────────
def test_the_stand_in_workspace_serves_the_properties_verify_reads(tmp_path):
    from tests.iac._fake_databricks import FakeDatabricks

    class Workspace(FakeDatabricks):
        def _table_doc(self, full: str) -> Dict[str, Any]:
            return {
                **super()._table_doc(full),
                "effective_predictive_optimization_flag": {"value": "ENABLE"},
            }

    fake = Workspace().start()
    try:
        fake.create_table(
            TABLE,
            [("id", "bigint", False), ("amount", "double", True), ("region", "string", True)],
            properties={"delta.logRetentionDuration": "interval 400 days"},
        )
        fake.grant_catalog("main", "analysts", "USE_CATALOG")
        fake.grant_catalog("main", "bi", "USE_CATALOG")
        failing = _verify(fake.client(), _contract("P365D"))
        fake.tables[TABLE]["properties"] = {"delta.logRetentionDuration": "interval 90 days"}
        passing = _verify(fake.client(), _contract("P365D"))
    finally:
        fake.stop()
    assert _dimension(failing)["status"] == "fail"
    assert _dimension(passing)["status"] == "pass"
    assert _dimension(passing)["predictive_optimization"] == "ENABLE"


def test_a_table_served_without_properties_is_a_warning_that_says_the_defaults_were_assumed(
    workspace,
):
    workspace.serves_properties = False
    workspace.extras = {"effective_predictive_optimization_flag": {"value": "ENABLE"}}
    dim = _dimension(_verify(workspace, _contract()))
    assert dim["status"] == "warn", dim
    assert "no table properties" in dim["warnings"][0]
    assert "SHOW TBLPROPERTIES" in dim["fix"]


def test_verify_reads_the_contract_as_apply_does_with_its_placeholders_substituted(
    workspace, monkeypatch
):
    monkeypatch.setenv("FLUID_TEST_RETENTION", "P365D")
    contract = _contract("{{ env.FLUID_TEST_RETENTION }}")
    workspace.extras = {"effective_predictive_optimization_flag": {"value": "ENABLE"}}
    dim = _dimension(_verify(workspace, contract))
    assert dim["status"] == "pass", dim
    assert dim["retention"] == "P365D"
