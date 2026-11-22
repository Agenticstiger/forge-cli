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


#: Dot segments Iceberg keeps as written and a file listing resolves: a table at ``<wh>/x/..``
#: deleted every file of its sibling ``<wh>/y`` (Spark 3.5, Iceberg 1.6) before they were refused.
@pytest.mark.parametrize("backend", sorted(BLOCKS))
@pytest.mark.parametrize(
    ("path", "other"),
    [
        (".", None),  # the bucket root, spelled with a dot
        ("x/..", None),  # likewise
        ("x/./t", "x/t/y"),  # holds x/t/y once resolved
        ("q/../x", "x/t"),  # holds x/t once resolved
    ],
)
def test_a_dot_segment_in_the_tables_location_is_refused(
    backend: str, path: str, other: Optional[str]
) -> None:
    (error,) = _scope_errors(_with(BLOCKS[backend](), path=path, other_path=other))
    assert "the table's location holds a '.' or '..' segment" in error


@pytest.mark.parametrize("backend", sorted(BLOCKS))
@pytest.mark.parametrize("other", ["q/../iceberg/commerce/order_lines/x", "./iceberg"])
def test_a_dot_segment_in_another_exposes_location_is_refused(backend: str, other: str) -> None:
    (error,) = _scope_errors(_with(BLOCKS[backend](), other_path=other))
    assert "expose archive's location in bucket commerce-lake holds a '.' or '..' segment" in error


def _runner() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "fluid_build/spark/runner/forge_spark_runner.py"
    spec = importlib.util.spec_from_file_location("forge_spark_runner_orphans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("location", "problem"),
    [
        ("s3://lake", "is a bucket root"),
        ("s3://lake/", "is a bucket root"),
        ("s3://lake//", "is a bucket root"),
        ("s3://lake/x/..", "holds a '.' or '..' segment"),
        ("s3://lake/.", "holds a '.' or '..' segment"),
        ("s3://lake/x/./t", "holds a '.' or '..' segment"),
        ("s3://lake/q/../x", "holds a '.' or '..' segment"),
        ("s3://lake/iceberg/commerce/order_lines", ""),
        ("file:/tmp/wh/commerce/order_lines", ""),
        ("s3://lake/x/..t/y", ""),  # a name that starts with dots is a name
        # a parser ends the path at ``#`` and ``?``: what follows is still the location's own
        ("s3://lake/x#/..", "holds a '#', a '?' or a control character"),
        ("s3://lake/x?/../", "holds a '#', a '?' or a control character"),
        ("s3://lake/x\n/..", "holds a '#', a '?' or a control character"),
        ("s3://lake/x\x00/y", "holds a '#', a '?' or a control character"),
    ],
)
def test_the_runner_refuses_a_root_or_a_dot_segment(location: str, problem: str) -> None:
    assert _runner().orphan_location_problem(location) == problem
