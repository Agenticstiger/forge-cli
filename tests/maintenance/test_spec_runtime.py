# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``iceberg.maintenance.runtime``: a table no forge build writes, maintained on an EMR runtime
its contract names (``maintenance/spec.py``).

The block stands in for ``build``: it carries the runtime, its target and the role the run
executes as. These tests pin what reads clean, each refusal and its wording, the operator-list
findings and the warning about a short ``removeOrphanFiles.olderThan``.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build.maintenance.spec import (
    declares_unwritten_maintenance,
    maintenance_warnings,
    read_maintenance,
    runtime_build,
    runtime_findings,
)
from tests.maintenance.conftest import EXPOSE, glue_block, maintained, spark_block
from tests.spark.conftest import CONTRACT

pytestmark = pytest.mark.unit

ROLE = "arn:aws:iam::123456789012:role/orders-maint"
APP = "00f1abcd2345"


def runtime(**extra: Any) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "runtime": "emr-serverless",
        "applicationId": APP,
        "executionRoleArn": ROLE,
    }
    block.update(extra)
    return block


def unwritten_block(rt: Any = None, **extra: Any) -> Dict[str, Any]:
    """A maintenance block with ``runtime`` in place of ``build``."""
    block = spark_block(
        runtime=rt if rt is not None else runtime(),
        removeOrphanFiles={"olderThan": "P7D"},
    )
    block.update(extra)
    return block


def unwritten(block: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """The example contract with no build at all: nothing writes ``order_lines``."""
    contract = maintained(block if block is not None else unwritten_block())
    contract["builds"] = []
    return contract


def read(contract: Dict[str, Any]):
    return read_maintenance(contract)


def has(errors: List[str], *words: str) -> bool:
    return any(all(w in e for w in words) for e in errors)


def test_a_runtime_block_reads_clean() -> None:
    tables, errors = read(unwritten())
    assert errors == []
    (tm,) = tables
    assert tm.expose_id == EXPOSE and tm.backend == "spark"
    assert tm.build_id is None
    assert tm.runtime == runtime()
    assert tm.schedule == "0 3 * * *"
    assert tm.orphans is not None and tm.orphans.older_than_seconds == 7 * 86400


def test_declares_unwritten_maintenance() -> None:
    assert declares_unwritten_maintenance(unwritten()) is True
    assert declares_unwritten_maintenance(maintained(spark_block())) is False
    assert declares_unwritten_maintenance(copy.deepcopy(CONTRACT)) is False
    glue = maintained(glue_block())
    assert declares_unwritten_maintenance(glue) is False


@pytest.mark.parametrize(
    ("rt", "target"),
    [
        (runtime(), {}),
        (
            {
                "runtime": "emr-ec2",
                "clusterId": "j-1ABCDEF",
                "executionRoleArn": ROLE,
            },
            {},
        ),
        (
            {
                "runtime": "emr-eks",
                "virtualClusterId": "abcdefghij0123456789klmno",
                "executionRoleArn": ROLE,
            },
            {},
        ),
    ],
)
def test_each_emr_runtime_reads_with_its_target(rt: Dict[str, Any], target: Dict[str, Any]) -> None:
    tables, errors = read(unwritten(unwritten_block(rt)))
    assert errors == []
    assert tables[0].runtime == rt


def test_release_label_and_an_allowed_spark_conf_read() -> None:
    rt = runtime(releaseLabel="emr-7.12.0", sparkConf={"spark.executor.memory": "4g"})
    tables, errors = read(unwritten(unwritten_block(rt)))
    assert errors == []
    assert tables[0].runtime["sparkConf"] == {"spark.executor.memory": "4g"}


def test_build_and_runtime_together_are_refused() -> None:
    contract = unwritten(unwritten_block(build="ingest-orders"))
    _, errors = read(contract)
    assert has(errors, "build", "runtime", "not both")


def test_runtime_while_a_forge_build_writes_the_expose_is_refused() -> None:
    contract = maintained(unwritten_block())  # the example's build writes order_lines
    _, errors = read(contract)
    assert has(errors, "iceberg.maintenance.runtime is for a table no forge build writes")
    assert has(errors, "ingest-orders")


def test_backend_glue_with_runtime_is_refused() -> None:
    block = glue_block(runtime=runtime())
    contract = maintained(block)
    contract["builds"] = []
    _, errors = read(contract)
    assert has(errors, "runtime is backend spark's")


@pytest.mark.parametrize("name", ["local", "glue", "emr-lambda", None])
def test_a_runtime_that_is_not_an_emr_one_is_refused(name: Any) -> None:
    rt = runtime()
    rt["runtime"] = name
    _, errors = read(unwritten(unwritten_block(rt)))
    assert has(errors, "runtime.runtime must be one of emr-serverless, emr-ec2, emr-eks")


def test_a_runtime_that_is_not_a_mapping_is_refused() -> None:
    _, errors = read(unwritten(unwritten_block("emr-serverless")))
    assert has(errors, "runtime must be a mapping")


def test_a_missing_execution_role_is_refused() -> None:
    rt = runtime()
    del rt["executionRoleArn"]
    _, errors = read(unwritten(unwritten_block(rt)))
    assert has(errors, "runtime.executionRoleArn is required")


def test_emr_ec2_without_a_cluster_is_refused_with_the_runtime_blocks_name() -> None:
    rt = {"runtime": "emr-ec2", "executionRoleArn": ROLE}
    _, errors = read(unwritten(unwritten_block(rt)))
    assert has(
        errors,
        "runtime emr-ec2 runs on a cluster that is running",
        f"exposes[{EXPOSE}].iceberg.maintenance.runtime.clusterId",
    )


def test_emr_serverless_without_an_application_is_refused() -> None:
    rt = runtime()
    del rt["applicationId"]
    _, errors = read(unwritten(unwritten_block(rt)))
    assert has(errors, "runtime.applicationId is required on runtime emr-serverless")


def test_emr_eks_without_a_virtual_cluster_is_refused() -> None:
    rt = {"runtime": "emr-eks", "executionRoleArn": ROLE}
    _, errors = read(unwritten(unwritten_block(rt)))
    assert has(errors, "runtime.virtualClusterId is required on runtime emr-eks")


def test_an_unknown_runtime_key_is_refused() -> None:
    _, errors = read(unwritten(unwritten_block(runtime(region_hint="eu-west-1"))))
    assert has(errors, "region_hint")


def test_spark_conf_must_be_a_mapping_of_strings() -> None:
    _, errors = read(unwritten(unwritten_block(runtime(sparkConf=["a"]))))
    assert has(errors, "sparkConf must be a mapping")
    _, errors = read(unwritten(unwritten_block(runtime(sparkConf={"spark.x": 3}))))
    assert has(errors, "sparkConf values must be strings")


def test_a_denied_spark_conf_key_names_the_runtime_block() -> None:
    rt = runtime(sparkConf={"spark.jars": "s3://elsewhere/x.jar"})
    _, errors = read(unwritten(unwritten_block(rt)))
    assert has(errors, "spark.jars", "exposes[order_lines].iceberg.maintenance.runtime.")
    assert not has(errors, "parameters.spark")


def test_remove_orphan_files_without_older_than_is_refused() -> None:
    block = unwritten_block(removeOrphanFiles={})
    _, errors = read(unwritten(block))
    assert has(errors, "removeOrphanFiles.olderThan is required with runtime")


def test_an_orphan_step_may_be_left_out_without_older_than() -> None:
    block = unwritten_block()
    del block["removeOrphanFiles"]
    tables, errors = read(unwritten(block))
    assert errors == [] and tables[0].orphans is None


def test_an_orphan_window_under_a_day_is_still_an_error() -> None:
    block = unwritten_block(removeOrphanFiles={"olderThan": "PT12H"})
    _, errors = read(unwritten(block))
    assert has(errors, "at least P1D")


def test_retain_last_zero_is_an_error() -> None:
    block = unwritten_block(expireSnapshots={"olderThan": "P5D", "retainLast": 0})
    _, errors = read(unwritten(block))
    assert has(errors, "retainLast")


@pytest.mark.parametrize("name", ["maintain_order_lines", "maintain-order-lines"])
def test_a_build_with_the_maintenance_name_is_refused(name: str) -> None:
    contract = unwritten()
    other = copy.deepcopy(CONTRACT["builds"][0])
    other["id"] = name
    other["outputs"] = ["other_table"]
    contract["builds"] = [other]
    _, errors = read(contract)
    assert has(errors, f"build {name} has the name this table's maintenance takes")


# --- warnings -----------------------------------------------------------------------


@pytest.mark.parametrize("older", ["P1D", "P2D", "PT36H"])
def test_a_short_orphan_window_warns(older: str) -> None:
    block = unwritten_block(removeOrphanFiles={"olderThan": older})
    tables, errors = read(unwritten(block))
    assert errors == []
    (warning,) = maintenance_warnings(tables)
    assert "exposes[order_lines].iceberg.maintenance.removeOrphanFiles.olderThan" in warning
    assert "under P3D" in warning


@pytest.mark.parametrize("older", ["P3D", "P7D", "P30D"])
def test_three_days_and_longer_do_not_warn(older: str) -> None:
    block = unwritten_block(removeOrphanFiles={"olderThan": older})
    tables, errors = read(unwritten(block))
    assert errors == []
    assert maintenance_warnings(tables) == []


def test_a_table_a_forge_build_writes_gets_no_orphan_warning() -> None:
    block = spark_block(removeOrphanFiles={"olderThan": "P2D"})
    tables, errors = read(maintained(block))
    assert errors == []
    assert maintenance_warnings(tables) == []


# --- the runtime as a build, and the operator's lists --------------------------------


def test_runtime_build_is_a_spark_build_named_after_the_expose() -> None:
    build = runtime_build(EXPOSE, runtime())
    assert build["id"] == "maintain_order_lines"
    assert build["engine"] == "spark" and build["outputs"] == [EXPOSE]
    assert build["properties"]["parameters"]["spark"] == runtime()


def test_runtime_findings_ask_for_the_operators_entries(monkeypatch) -> None:
    monkeypatch.delenv("FLUID_SPARK_EXECUTION_ROLE_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_SPARK_TARGET_ALLOWLIST", raising=False)
    errors, missing = runtime_findings(EXPOSE, runtime())
    assert errors == []
    assert len(missing) >= 2
    assert all("exposes[order_lines].iceberg.maintenance.runtime." in m for m in missing)
    assert not any("parameters.spark" in m for m in missing)


def test_runtime_findings_are_clean_when_the_operator_lists_them(monkeypatch) -> None:
    monkeypatch.setenv("FLUID_SPARK_EXECUTION_ROLE_ALLOWLIST", ROLE)
    monkeypatch.setenv("FLUID_SPARK_TARGET_ALLOWLIST", f"emr-serverless:{APP}")
    assert runtime_findings(EXPOSE, runtime()) == ([], [])
