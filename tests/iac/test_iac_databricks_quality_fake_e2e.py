# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Databricks data quality monitoring through the real ``fluid`` CLI, ``tofu`` and provider,
against a stand-in.

Pinned (Stage 1: real ``tofu`` and ``databricks/databricks`` 1.140.0, the stand-in workspace of
``_fake_databricks.py``, its data quality endpoints shaped as Free Edition answered them): an
expose with a freshness SLO turns monitoring on for its schema, watching it by the schema's
``schema_id`` and excluding the product's other table there; a second plan is empty; a monitor
someone turned on outside forge-cli is adopted, not created over; the last exclusion leaving
clears the list and the plan converges; a monitor Databricks refuses fails the apply with the
opt-out named, its tables kept; opting out deletes only the monitor, not gated; and OpenTofu
deletes the monitor before its schema.

Not proven here: what real Databricks scans and reports (the live Free Edition run,
HONESTLY_TESTED.md).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from fluid_build.iac import runner

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    write_contract,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]

CREATE = ("POST", "/api/data-quality/v1/monitors")
_TRANSPORT = re.compile(r"broken pipe|connection reset by peer")


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=plugin_cache,
    )
    return tmp_path


def _monitored(enabled: bool = True) -> Dict[str, Any]:
    """``make_contract``'s orders with a freshness SLO, and a notes table without one."""
    contract = make_contract()
    orders = contract["exposes"][0]
    orders["qos"] = {"freshnessSLO": "PT15M", "completenessTarget": 0.98}
    if not enabled:
        orders["contract"]["dq"] = {"monitoring": {"enabled": False}}
    notes = copy.deepcopy(orders)
    notes.pop("qos")
    notes["contract"].pop("dq", None)
    notes["exposeId"] = "notes"
    notes["binding"]["location"]["table"] = "notes"
    contract["exposes"].append(notes)
    return contract


def _monitor(fake: FakeDatabricks) -> Dict[str, Any]:
    return fake.quality_monitors[("schema", fake.schemas["main.sales"]["schema_id"])]


def test_an_slo_turns_monitoring_on_for_the_schema_and_a_second_plan_is_empty(
    workspace, fake, capfd
):
    path = write_contract(workspace, _monitored())
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert list(fake.quality_monitors) == [("schema", "sid-main.sales")]
    config = _monitor(fake)["anomaly_detection_config"]
    assert config["excluded_table_full_names"] == ["main.sales.notes"]
    assert (
        "data quality monitoring is on for schema main.sales (watched: main.sales.orders; "
        "excluded: main.sales.notes)" in out
    )
    assert fake.unknown == []

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out


def test_a_monitor_turned_on_outside_forge_cli_is_adopted_not_created_over(workspace, fake, capfd):
    fake.create_table(
        "main.sales.orders",
        [("id", "bigint", False), ("amount", "double", True), ("region", "string", True)],
        properties={"delta.columnMapping.mode": "name"},
    )
    key = ("schema", fake.schemas["main.sales"]["schema_id"])
    fake.quality_monitors[key] = {
        "object_type": "schema",
        "object_id": key[1],
        "anomaly_detection_config": {"anomaly_detection_workflow_id": 7},
    }
    path = write_contract(workspace, _monitored())

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "adopted" in out
    assert CREATE not in fake.requests
    assert _monitor(fake)["anomaly_detection_config"]["excluded_table_full_names"] == [
        "main.sales.notes"
    ]
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_the_last_exclusion_leaving_clears_the_list_and_the_plan_converges(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _monitored()))[0] == 0
    both = _monitored()
    both["exposes"][1]["qos"] = {"freshnessSLO": "PT1H"}
    path = write_contract(workspace, both)
    fake.queries.clear()

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "tofu plan: +0 ~1 -0" in out
    # provider 1.140.0 replaces the whole config: the mask names it, the body has no list
    (patch,) = [query for method, _path, query in fake.queries if method == "PATCH"]
    assert "anomaly_detection_config" in patch
    assert "excluded_table_full_names" not in _monitor(fake)["anomaly_detection_config"]
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_a_refused_monitor_fails_the_apply_with_the_way_out(workspace, fake, capfd):
    fake.fail_route("POST", r"^/api/data-quality/v1/monitors$", 400, "feature not enabled")

    rc, out = apply_contract(capfd, write_contract(workspace, _monitored()))
    assert rc != 0
    assert "Databricks refused the data quality monitor on this schema" in out
    assert "set contract.dq.monitoring.enabled: false, then re-run fluid apply" in out
    # created first, and kept: a re-run converges
    assert {"main.sales.orders", "main.sales.notes"} <= set(fake.tables)


def test_opting_out_deletes_only_the_monitor_and_is_not_gated(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _monitored()))[0] == 0
    marker = fake.tables["main.sales.orders"]["marker"]

    rc, out = apply_contract(capfd, write_contract(workspace, _monitored(enabled=False)))
    assert rc == 0, out
    assert fake.quality_monitors == {}
    assert "data quality monitor(s) removed (no table data lost; not gated" in out
    assert fake.tables["main.sales.orders"]["marker"] == marker and fake.replaced == []
    assert "data quality monitoring is on" not in out


def test_a_destroy_deletes_the_monitor_before_its_schema(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _monitored()))[0] == 0
    (workdir,) = (workspace / ".fluid" / "iac" / "databricks").iterdir()
    sid = fake.schemas["main.sales"]["schema_id"]
    fake.requests.clear()

    result = runner.tofu_destroy(str(workdir))
    if not result.ok and _TRANSPORT.search(result.stdout + result.stderr):
        # The stand-in closes every connection (HTTP/1.0); under load the provider can
        # write to one it closed. State keeps what went, so the order holds across runs.
        result = runner.tofu_destroy(str(workdir))
    assert result.ok, result.stderr or result.stdout
    deletes = [path for method, path in fake.requests if method == "DELETE"]
    monitor = deletes.index(f"/api/data-quality/v1/monitors/schema/{sid}")
    schema = deletes.index("/api/2.1/unity-catalog/schemas/main.sales")
    assert monitor < schema
    assert fake.quality_monitors == {} and "main.sales" not in fake.schemas
