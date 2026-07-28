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

"""Exit-code contract.

The plan promised: ``0`` success, ``1`` user error, ``2`` partial,
``3`` transient, ``4`` internal. Every CLI helper that returns an exit
code must respect this. This file asserts the contract for the
acquisition runners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners.duckdb.runner import execute_duckdb_build


def _contract(uri: str, out: str) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.x",
        "name": "x",
        "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": uri},
                        "mode": "full_refresh",
                        "reader": {"format": "csv", "options": {"header": True}},
                    },
                    "sink": {"format": "parquet"},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {"platform": "local", "format": "parquet", "location": {"path": out}},
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


class TestExitCodes:
    def test_zero_on_success(self, tmp_path: Path):
        in_csv = tmp_path / "in" / "x.csv"
        in_csv.parent.mkdir(parents=True)
        in_csv.write_text("id\n1\n", encoding="utf-8")
        out = tmp_path / "out.parquet"
        contract = _contract(str(in_csv), str(out))
        rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0

    def test_nonzero_on_user_error(self, tmp_path: Path):
        # User error: missing source.
        contract = _contract("/tmp/missing", str(tmp_path / "x.parquet"))
        del contract["builds"][0]["properties"]["source"]
        rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0
        # User errors are categorized as exit code 1 in our runner.
        assert rc == 1

    def test_zero_on_dry_run(self, tmp_path: Path):
        contract = _contract(str(tmp_path / "missing-input.csv"), str(tmp_path / "x.parquet"))
        rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=True)
        assert rc == 0
