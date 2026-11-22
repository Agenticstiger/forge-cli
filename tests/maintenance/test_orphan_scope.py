# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Removing orphan files deletes every file under the table's location that no snapshot of that
table references, so the location must be the table's alone: never a bucket root, never the
parent or the child of another expose's location (compared segment by segment), on both
backends. Removing orphans on ``db.a`` deleted every file of a nested ``db.a.b`` on Spark 3.5
with Iceberg 1.6 before this check."""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

import pytest

from fluid_build.maintenance.spec import read_maintenance, table_location

from .conftest import glue_block, maintained, spark_block

BUCKET = "commerce-lake"
#: The example table's own location (tests/spark/conftest.py CONTRACT).
PATH = "iceberg/commerce/order_lines/"
BLOCKS = {
    "spark": lambda: spark_block(removeOrphanFiles={}),
    "glue": lambda: glue_block(removeOrphanFiles={}),
}


def _with(
    block: Dict[str, Any], path: Optional[str] = PATH, other_path: Optional[str] = None
) -> Dict[str, Any]:
    contract = maintained(block)
    loc = contract["exposes"][0]["binding"]["location"]
    if path is None:
        loc.pop("path", None)
    else:
        loc["path"] = path
    if other_path is not None:
        other = copy.deepcopy(contract["exposes"][0])
        other["exposeId"] = "archive"
        other.pop("iceberg", None)
        other["binding"] = {
            "platform": "aws",
            "format": "parquet",
            "location": {"bucket": BUCKET, "path": other_path, "region": "eu-west-1"},
        }
        contract["exposes"].append(other)
    return contract


def _scope_errors(contract):
    return [e for e in read_maintenance(contract)[1] if ".removeOrphanFiles: " in e]


@pytest.mark.parametrize("backend", sorted(BLOCKS))
def test_a_bucket_root_is_refused(backend: str) -> None:
    (error,) = _scope_errors(_with(BLOCKS[backend](), path=""))
    assert "the root of bucket commerce-lake" in error


@pytest.mark.parametrize("backend", sorted(BLOCKS))
@pytest.mark.parametrize(
    "other",
    [
        PATH + "archive/",  # another table inside this one
        "iceberg/commerce/",  # this table inside another
        PATH,  # the same location
        "iceberg/commerce/order_lines",  # the same, without its slash
    ],
)
def test_a_location_that_holds_or_sits_inside_another_is_refused(backend: str, other: str) -> None:
    (error,) = _scope_errors(_with(BLOCKS[backend](), other_path=other))
    assert "expose archive's" in error and "hold one another" in error


@pytest.mark.parametrize("backend", sorted(BLOCKS))
@pytest.mark.parametrize(
    "other",
    [
        "iceberg/commerce/order_lines_archive/",
        "iceberg/commerce/order",
        "iceberg/other/order_lines/",
    ],
)
def test_a_sibling_prefix_is_not_nesting(backend: str, other: str) -> None:
    assert _scope_errors(_with(BLOCKS[backend](), other_path=other)) == []


def test_a_nested_location_is_fine_when_no_orphans_are_removed() -> None:
    contract = _with(spark_block(), other_path=PATH + "archive/")
    assert read_maintenance(contract)[1] == []


def test_the_default_location_counts_too() -> None:
    """With no ``path``, the table sits at ``<database>/<table>/``: a location under it is
    nested all the same."""
    contract = _with(
        spark_block(removeOrphanFiles={}), path=None, other_path="commerce/order_lines/x/"
    )
    (error,) = _scope_errors(contract)
    assert "s3://commerce-lake/commerce/order_lines/" in error


def test_the_table_location_names_the_place() -> None:
    assert table_location(_with(spark_block()), "order_lines") == f"s3://{BUCKET}/{PATH}"
    assert table_location(_with(spark_block()), "nothing") is None
