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
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import _pipeline
from .registry import Importer, ImportReport

TOOL = "airflow"


@dataclass
class AirflowImporter(Importer):
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
