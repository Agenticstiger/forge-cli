# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid import airflow <dags-dir>``: Airflow DAGs to FLUID products.

A static read of DAG files (:mod:`._airflow_dags`): Airflow is never imported and no DAG file is
run. Each task that submits a Spark script is followed to the job file it runs, through the
conventions' ``script_roots`` (the deployed prefix of the script to the local directory that holds
it), and the job is read as ``fluid import pyspark`` reads it. The proposal is one product per
output with a single writer and one owner, with the DAG's schedule and the tasks'
dependencies recorded on the build, and upstream reads as ``consumes[]`` (:mod:`._proposal`).

A task whose script is not readable here is listed in the report with how to make it readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._pipeline import CodeImporter

TOOL = "airflow"


@dataclass
class AirflowImporter(CodeImporter):
    tool: ClassVar[str] = TOOL
    name: str = TOOL
