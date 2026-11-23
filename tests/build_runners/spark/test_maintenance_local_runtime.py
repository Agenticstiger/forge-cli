# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``iceberg.maintenance.runtime: local``: a table no forge build writes, in an Iceberg REST
catalog on platform local, maintained with forge's local Spark runtime.

The run is a local build's: spark-submit on the machine ``fluid apply`` runs on, no role, no
target, artifacts under the local root and the operator's namespace list in force. OpenTofu
has nothing to emit for it and the native engine runs it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import pytest

from fluid_build.build_runners.spark._desired import LOCAL_ROOT_TOKEN
from fluid_build.build_runners.spark._maintenance import maintenance_runs
from fluid_build.cli import _spark_apply
from fluid_build.iac import get_iac_plugin
from fluid_build.maintenance.spec import (
    declares_unwritten_maintenance,
    read_maintenance,
)
from fluid_build.spark import names
from fluid_build.spark.compiler import LOCAL_NAMESPACE_ALLOWLIST_ENV, maintenance_sink
from fluid_build.spark.policy import SparkPolicyError
from tests.maintenance.conftest import EXPOSE, maintained, spark_block

pytestmark = pytest.mark.unit

BUILD = "maintain_order_lines"


def local_contract(rt: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """The example with no build, its expose in a Lakekeeper catalog on platform local."""
    block = spark_block(
        runtime=rt if rt is not None else {"runtime": "local"},
        removeOrphanFiles={"olderThan": "P7D"},
    )
    contract = maintained(block)
    contract["builds"] = []
    contract["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "iceberg",
        "location": {"catalog": "lakekeeper", "database": "commerce", "table": "order_lines"},
    }
    return contract


@pytest.fixture(autouse=True)
def listed_namespace(monkeypatch):
    monkeypatch.setenv(LOCAL_NAMESPACE_ALLOWLIST_ENV, "fluid/commerce")


def test_a_local_block_reads_clean_without_a_role_or_a_target() -> None:
    tables, errors = read_maintenance(local_contract())
    assert errors == []
    assert tables[0].runtime == {"runtime": "local"}


def test_the_aws_keys_are_accepted_and_ignored() -> None:
    rt = {
        "runtime": "local",
        "applicationId": "app0000000001",
        "executionRoleArn": "arn:aws:iam::123456789012:role/x",
        "releaseLabel": "emr-7.12.0",
    }
    _, errors = read_maintenance(local_contract(rt))
    assert errors == []
    (run,) = maintenance_runs(local_contract(rt))
    assert run.role_arn == "" and run.target == {} and run.release_label is None


def test_a_local_block_still_needs_an_explicit_orphan_age() -> None:
    contract = local_contract()
    contract["exposes"][0]["iceberg"]["maintenance"]["removeOrphanFiles"] = {}
    _, errors = read_maintenance(contract)
    assert any("removeOrphanFiles.olderThan is required" in e for e in errors)


def test_a_local_block_on_a_glue_expose_is_refused_by_the_sink_check() -> None:
    contract = local_contract()
    contract["exposes"][0]["binding"] = {
        "platform": "aws",
        "format": "iceberg",
        "location": {"database": "commerce", "table": "order_lines", "bucket": "commerce-lake"},
    }
    with pytest.raises(ValueError, match="runtime local writes a table in an Iceberg REST"):
        maintenance_runs(contract)


def test_the_run_is_a_local_one_under_the_local_root() -> None:
    (run,) = maintenance_runs(local_contract())
    assert run.local and run.maintenance_runtime and run.maintains == EXPOSE
    assert run.build == BUILD
    assert run.region == "" and run.role_arn == "" and run.target == {}
    assert run.release_label is None
    assert run.job_uri.startswith(f"{LOCAL_ROOT_TOKEN}/")
    assert run.job_uri.endswith(f"/{BUILD}/maintenance/{EXPOSE}/job.json")
    assert run.runner_uri.startswith(f"{LOCAL_ROOT_TOKEN}/")
    assert run.run_name == names.maintenance_run_name("commerce.orders-stream", EXPOSE)


def test_the_job_config_names_the_rest_table_with_no_create() -> None:
    (run,) = maintenance_runs(local_contract())
    sink = json.loads(run.job_text)["sink"]
    assert sink["type"] == "rest" and sink["kind"] == "lakekeeper"
    assert sink["table"] == "commerce.order_lines"
    assert "create" not in sink


def test_the_plan_lists_the_local_run() -> None:
    (action,) = _spark_apply.planned_actions(local_contract())
    assert action["op"] == "spark.maintain"
    assert action["provider"] == "local"
    assert action["payload"]["runtime"] == "local"
    assert action["payload"]["executionRoleArn"] is None
    assert "this machine" in action["payload"]["stateImpact"]


def test_an_unlisted_namespace_is_refused_when_the_policy_is_on(monkeypatch) -> None:
    monkeypatch.delenv(LOCAL_NAMESPACE_ALLOWLIST_ENV)
    with pytest.raises(SparkPolicyError):
        maintenance_runs(local_contract(), policy=True)
    assert maintenance_runs(local_contract(), policy=False)


def test_the_sink_gate_is_off_unless_asked(monkeypatch) -> None:
    monkeypatch.delenv(LOCAL_NAMESPACE_ALLOWLIST_ENV)
    sink = maintenance_sink(local_contract(), EXPOSE, local=True)
    assert sink["type"] == "rest"
    with pytest.raises(SparkPolicyError):
        maintenance_sink(local_contract(), EXPOSE, local=True, host_gate=True)


def test_a_local_only_contract_emits_nothing_on_aws() -> None:
    assert declares_unwritten_maintenance(local_contract())
    assert not declares_unwritten_maintenance(local_contract(), remote_only=True)
    plugin = get_iac_plugin("aws")
    resources = plugin.emit(local_contract())
    assert not any("maintain_" in k for k in (resources.get("aws_s3_object") or {}))


def test_the_native_engine_does_not_refuse_a_local_only_contract() -> None:
    _spark_apply.refuse_without_opentofu(local_contract(), "native")


def test_a_dry_run_names_the_local_machine(capsys) -> None:
    rc = _spark_apply.maintain_only(
        local_contract(), logging.getLogger("t"), expose_id=None, dry_run=True
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0
    assert f"Iceberg {EXPOSE} maintenance (local, on this machine, as this machine's user)" in out


def test_the_confirm_prompt_names_this_machines_user(monkeypatch) -> None:
    from types import SimpleNamespace

    from fluid_build.build_runners.spark import _reconcile

    monkeypatch.setattr(
        _reconcile,
        "run_once",
        lambda run, **_k: SimpleNamespace(
            action="succeeded", error=False, line=lambda: "ok", as_dict=lambda: {}
        ),
    )
    monkeypatch.setattr(_spark_apply, "_check_local", lambda *a, **k: None)
    asked = []
    _spark_apply.maintain_only(
        local_contract(),
        logging.getLogger("t"),
        expose_id=None,
        dry_run=False,
        confirm=lambda q: asked.append(q) or False,
    )
    assert asked and "on local as this machine's user" in asked[0]
