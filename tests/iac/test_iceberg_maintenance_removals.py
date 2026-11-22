# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Dropping a table's maintenance from the contract deletes no data, so the data-loss gate does
not count it: the maintenance job config forge uploads beside a Spark build's (backend spark)
and the Glue Data Catalog's table optimizers (backend glue)."""

from __future__ import annotations

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.providers.aws_ingest import spark_definition

MAINTENANCE_CONFIG = (
    "aws_s3_object.shop_orders_ingest_spark_maintain_order_lines",
    "aws_s3_object",
)
OPTIMIZERS = [
    (
        f"aws_glue_catalog_table_optimizer.shop_silver_orders_{kind}",
        "aws_glue_catalog_table_optimizer",
    )
    for kind in ("compaction", "retention", "orphan_file_deletion")
]


def _summary(remove: int) -> dict:
    return {"add": 0, "change": 0, "remove": remove}


def test_a_maintenance_job_config_is_a_spark_definition() -> None:
    assert spark_definition(*MAINTENANCE_CONFIG)
    # A data object of the same type is not, whatever its name holds.
    assert not spark_definition("aws_s3_object.shop_maintain_order_lines", "aws_s3_object")
    assert not spark_definition("aws_s3_object.shop_spark_maintain_", "aws_s3_object")


def test_dropping_a_tables_maintenance_does_not_reach_the_gate() -> None:
    removed = [MAINTENANCE_CONFIG, *OPTIMIZERS]
    counted, _ = engine._data_bearing_changes(
        _summary(len(removed)), removed, get_iac_plugin("aws")
    )
    assert counted["remove"] == 0


def test_the_optimizers_alone_are_run_definitions_on_any_provider() -> None:
    counted, _ = engine._data_bearing_changes(_summary(len(OPTIMIZERS)), OPTIMIZERS)
    assert counted["remove"] == 0
    # A Glue table beside them still counts.
    table = ("aws_glue_catalog_table.shop_silver_orders", "aws_glue_catalog_table")
    counted, _ = engine._data_bearing_changes(_summary(len(OPTIMIZERS) + 1), [*OPTIMIZERS, table])
    assert counted["remove"] == 1
