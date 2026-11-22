# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract whose Spark build runs a prebuilt PySpark package by digest, and a package
to run: a zip built in memory, so its sha256 is known to the tests."""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import Dict

#: The package's own module: the transform the runner calls, and helpers the tests swap in.
JOBS = '''\
from pyspark.sql import functions as F


def merge_orders(spark, context):
    """The orders, one row each, as the contract's output declares them."""
    return spark.table(context.arguments["source"]).select(
        F.col("order_id"),
        F.struct(F.col("customer_id").alias("id"), F.col("email")).alias("customer"),
        F.col("amount").cast("decimal(18,2)").alias("amount"),
        F.to_date("ordered_at").alias("order_date"),
    )
'''


def package_zip(files: Dict[str, str]) -> bytes:
    """A zip of ``files`` (name to text), with fixed timestamps so the bytes never change."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, files[name])
    return buffer.getvalue()


PACKAGE = package_zip({"orders_pkg/__init__.py": "", "orders_pkg/jobs.py": JOBS})
SHA = hashlib.sha256(PACKAGE).hexdigest()
SOURCE = f"s3://commerce-artifacts/code/orders/sha256-{SHA}/orders_pkg-1.4.0-py3-none-any.whl"


def col(name, type_, **kw):
    return {"name": name, "type": type_, **kw}


OUTPUT = [
    col("order_id", "string", required=True),
    col(
        "customer",
        "struct",
        required=True,
        fields=[col("id", "string", required=True), col("email", "string")],
    ),
    col("amount", "decimal(18,2)"),
    col("order_date", "date"),
]

PACKAGE_CONTRACT = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "commerce.orders-silver",
    "name": "Orders, cleaned",
    "metadata": {"owner": {"team": "commerce-data"}},
    "builds": [
        {
            "id": "merge-orders",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["orders"],
            "properties": {
                "language": "pyspark",
                "package": {
                    "uri": SOURCE,
                    "sha256": SHA,
                    "entryPoint": "orders_pkg.jobs:merge_orders",
                    "arguments": {"source": "commerce.orders_raw"},
                    "deadLetter": {"maxRecordsBeforeAbort": 100},
                },
                "parameters": {
                    "spark": {"runtime": "emr-ec2", "clusterId": "j-2AXXXXXXGAPLF", "mode": "batch"}
                },
            },
        }
    ],
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "commerce",
                    "table": "orders",
                    "bucket": "commerce-lake",
                    "path": "iceberg/commerce/orders/",
                    "region": "eu-west-1",
                },
            },
            "contract": {"schema": OUTPUT},
        }
    ],
}
