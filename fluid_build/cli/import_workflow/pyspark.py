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
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import _pipeline
from .registry import Importer, ImportReport

TOOL = "pyspark"


@dataclass
class PySparkImporter(Importer):
    name: str = TOOL
    #: A proposal is completed by hand, so a second import never replaces an edited file
    #: (``--force`` does).
    guards_overwrite: bool = True

    def can_import(self, source: str) -> bool:
        return Path(source).is_dir()

    def import_to_contracts(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[List[Dict[str, Any]], ImportReport]:
        return _pipeline.run_import(TOOL, source, options or {})

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[Dict[str, Any], ImportReport]:
        """The single-contract protocol: everything in one product (``project`` boundary)."""
        merged = dict(options or {})
        merged["split_by"] = "project"
        contracts, report = self.import_to_contracts(source, options=merged)
        return (contracts[0] if contracts else {}), report
