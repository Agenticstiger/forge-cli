# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Removing orphan files on a REST-catalog table lists through the table's FileIO.

Iceberg's orphan action lists through Hadoop's S3A unless told to use prefix listing. A REST
catalog vends its credentials to the table's FileIO, so S3A holds none for the table's bucket
and the step waits on credential lookups (live run, 2026-11-23, Lakekeeper over SeaweedFS). A
Glue table keeps the action's default listing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

LOCATION = "s3://lake/wh/0001"


class _Orphans:
    def __init__(self) -> None:
        self.prefix_listing = False
        self.cutoff = None
        self.scope = None

    def location(self, scope):
        self.scope = scope
        return self

    def olderThan(self, cutoff):  # noqa: N802 - Iceberg's name
        self.cutoff = cutoff
        return self

    def usePrefixListing(self, enabled):  # noqa: N802 - Iceberg's name
        self.prefix_listing = enabled
        return self

    def execute(self):
        return SimpleNamespace(orphanFileLocations=lambda: [])  # noqa: N803


def _spark(orphans: _Orphans, location: str = LOCATION):
    spark = MagicMock()
    spark.conf.get.return_value = "declared"
    jvm = spark._jvm
    actions = jvm.org.apache.iceberg.spark.actions.SparkActions.get.return_value
    actions.deleteOrphanFiles.return_value = orphans
    table = jvm.org.apache.iceberg.spark.Spark3Util.loadIcebergTable.return_value
    table.location.return_value = location
    return spark


def _config(sink_type: str) -> Dict[str, Any]:
    sink: Dict[str, Any] = {"catalog": "fluid", "type": sink_type, "table": "db.t"}
    if sink_type == "glue":
        sink["warehouse"] = "s3://lake/wh"
    return {
        "product": "p",
        "build": "b",
        "sink": sink,
        "maintenance": {"steps": [{"step": "remove_orphan_files", "olderThanSeconds": 259200}]},
    }


def _run(runner, sink_type: str, location: str = LOCATION) -> _Orphans:
    orphans = _Orphans()
    steps: List[Any] = runner.run_maintenance(
        _spark(orphans, location), _config(sink_type), now=lambda: 1e6
    )
    assert steps[0]["step"] == "remove_orphan_files"
    assert orphans.cutoff == int((1e6 - 259200) * 1000)
    return orphans


def test_a_rest_table_is_listed_through_its_file_io(runner) -> None:
    assert _run(runner, "rest").prefix_listing is True


@pytest.mark.parametrize("location", [LOCATION, LOCATION + "/"])
def test_a_prefix_listing_never_reaches_a_sibling_table(runner, location) -> None:
    # s3://lake/wh/0001 as a bare prefix also lists s3://lake/wh/00010/...
    assert _run(runner, "rest", location).scope == LOCATION + "/"


def test_a_glue_table_keeps_the_default_listing(runner) -> None:
    orphans = _run(runner, "glue")
    assert orphans.prefix_listing is False and orphans.scope is None
