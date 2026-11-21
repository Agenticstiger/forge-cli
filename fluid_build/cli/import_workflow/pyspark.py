# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid import pyspark <jobs-dir> [--dags <dags-dir>]``: PySpark jobs to FLUID products.

A static read of batch and Structured Streaming jobs (:mod:`._spark_io`): the tables, paths and
Kafka topics each reads and writes, and a stream's checkpoint and trigger. With ``--dags`` the DAG
files are read too (:mod:`._airflow_dags`) and each task is joined to the script it submits, which
adds the schedule, the owner and the dependencies. The proposal is one product per output with a
single writer and one owner, and upstream reads as ``consumes[]`` (:mod:`._proposal`).

Nothing is imported or run. ``--conventions`` carries the organisation's layout as data
(:mod:`._conventions`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._pipeline import CodeImporter

TOOL = "pyspark"


@dataclass
class PySparkImporter(CodeImporter):
    tool: ClassVar[str] = TOOL
    name: str = TOOL
