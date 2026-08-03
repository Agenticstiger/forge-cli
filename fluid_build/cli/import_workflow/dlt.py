# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""dlt pipeline → FLUID contract importer.

Reads a dlt pipeline state directory (``~/.dlt/pipelines/<name>/``) and
emits a Bronze contract referencing the same source/destination.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .registry import Importer, ImportReport


@dataclass
class DltImporter(Importer):
    name: str = "dlt"

    def can_import(self, source: str) -> bool:
        path = self._resolve_pipeline_path(source)
        return path.exists()

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[Dict[str, Any], ImportReport]:
        path = self._resolve_pipeline_path(source)
        if not path.exists():
            raise FileNotFoundError(path)
        report = ImportReport()
        # dlt pipeline state is a JSON file under <pipeline>/state.json
        state_file = path / "state.json"
        state: Dict[str, Any] = {}
        if state_file.exists():
            with state_file.open(encoding="utf-8") as f:
                state = json.load(f)
            report.mapped_one_to_one.append("pipeline.state")
        else:
            report.required_defaults.append("pipeline.state (no state.json found)")

        destination_type = state.get("destination_type") or "duckdb"
        dataset_name = state.get("dataset_name") or "bronze"
        source_name = state.get("source_name") or source

        # FLUID identifier regex: ``^[a-z0-9_][a-z0-9_.-]*[a-z0-9_]$``. When
        # the source is an absolute path we slug the basename, not the full
        # path, so the id stays readable.
        basename = Path(source).name if "/" in source else source
        slug = "".join(c.lower() if (c.isalnum() or c in "_.-") else "_" for c in basename).strip(
            "_-."
        )
        if not slug:
            slug = "pipeline"
        product_id = f"bronze.dlt_{slug}"
        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": product_id,
            "name": f"Imported from dlt: {source}",
            "domain": "imported",
            "description": f"Auto-converted from dlt pipeline {source}",
            "metadata": {
                "layer": "Bronze",
                "productType": "SDP",
                "owner": {"team": "imported", "email": "import@forge.local"},
            },
            "builds": [
                {
                    "id": "ingest",
                    "pattern": "acquisition",
                    "engine": "dlt",
                    "capabilities": ["full_refresh"],
                    "properties": {
                        "source": {
                            "kind": source_name,
                            "connection": {},
                            "mode": "full_refresh",
                            "streams": list(state.get("schemas", {}).keys()),
                        },
                        "sink": {"format": "parquet"},
                        "dlt": {
                            "pipeline_name": source,
                            "destination": destination_type,
                            "dataset_name": dataset_name,
                        },
                    },
                    "outputs": ["data"],
                }
            ],
            "exposes": [
                {
                    "exposeId": "data",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "./out/data.duckdb"},
                    },
                    "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
                }
            ],
        }
        return contract, report

    def _resolve_pipeline_path(self, source: str) -> Path:
        """``source`` may be an absolute path or a pipeline name. We honor
        ``DLT_DATA_DIR`` env var for the latter.
        """
        p = Path(source)
        if p.is_absolute() or "/" in source:
            return p
        root = Path(os.environ.get("DLT_DATA_DIR", str(Path.home() / ".dlt"))) / "pipelines"
        return root / source
