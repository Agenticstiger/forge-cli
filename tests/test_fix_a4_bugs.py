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

"""Regression tests for the six apply/verify/acquisition lifecycle bugs.

Bug IDs and what each test covers:

* A4-1  verify.py: csv/parquet format dispatch → _verify_local_file
* A4-2  local provider: format + path from contract binding honoured
* A4-3  verify.py: verify_acquisition uses contract parent dir, not cwd
* A4-4  _acquisition_stage_ext.py: run_state_succeeded is case-insensitive
* A4-A  build_runners/base.py: embedded-SQL builds don't emit "Script not found"
* A5-3  provider.py: _aggregate_sub_status counts ok/changed correctly
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bug A4-4: run_state_succeeded is case-insensitive
# ---------------------------------------------------------------------------


class TestAcquisitionStateCheck:
    """A4-4 — verify_acquisition treats state values case-insensitively."""

    def _make_run_record(self, state: str) -> Dict[str, Any]:
        return {
            "state": state,
            "records_total": 42,
            "dlq_records": 0,
        }

    def _make_contract(self, product_id: str = "prod.x") -> Dict[str, Any]:
        return {
            "id": product_id,
            "builds": [
                {
                    "id": "ingest_build",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                }
            ],
        }

    def _write_run_record(
        self, workdir: Path, product_id: str, build_id: str, record: Dict[str, Any]
    ) -> None:
        runs_dir = workdir / ".fluid" / "runs" / product_id / build_id / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run_001.json").write_text(json.dumps(record))

    @pytest.mark.parametrize(
        "state,should_pass",
        [
            ("succeeded", True),  # lowercase — this was the broken case
            ("SUCCEEDED", True),  # uppercase — worked before the fix
            ("partial", True),  # lowercase partial
            ("PARTIAL", True),  # uppercase partial
            ("failed", False),
            ("FAILED", False),
            ("running", False),
        ],
    )
    def test_run_state_case_insensitive(self, state: str, should_pass: bool, tmp_path: Path):
        from fluid_build.cli._acquisition_stage_ext import verify_acquisition

        product_id = "prod.x"
        build_id = "ingest_build"
        contract = self._make_contract(product_id)
        record = self._make_run_record(state)
        self._write_run_record(tmp_path, product_id, build_id, record)

        results = verify_acquisition(contract, tmp_path)
        assert len(results) == 1
        check_map = {c.name: c for c in results[0].checks}
        assert "run_state_succeeded" in check_map
        assert check_map["run_state_succeeded"].passed is should_pass, (
            f"state={state!r} should_pass={should_pass} but got "
            f"passed={check_map['run_state_succeeded'].passed}"
        )


# ---------------------------------------------------------------------------
# Bug A4-3: verify_acquisition receives contract parent dir, not cwd()
# ---------------------------------------------------------------------------


class TestVerifyAcquisitionContractDir:
    """A4-3 — run-record lookup resolves from the contract's parent directory."""

    def test_acquisition_workdir_is_contract_parent_not_cwd(self, tmp_path: Path):
        """Regression: verify_acquisition(contract, Path.cwd()) fails when the
        contract lives in a different directory from cwd.  Fix: use
        Path(contract_path).resolve().parent.
        """
        from fluid_build.cli._acquisition_stage_ext import verify_acquisition

        product_id = "myproduct"
        build_id = "myingest"

        # Place the run record under the contract's parent (not cwd).
        contract_dir = tmp_path / "contracts" / "myproduct"
        contract_dir.mkdir(parents=True)
        runs_dir = contract_dir / ".fluid" / "runs" / product_id / build_id / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run_001.json").write_text(
            json.dumps({"state": "succeeded", "records_total": 10, "dlq_records": 0})
        )

        contract = {
            "id": product_id,
            "builds": [{"id": build_id, "pattern": "acquisition", "engine": "duckdb"}],
        }

        # Calling with contract_dir (the correct fix) should find the record.
        results = verify_acquisition(contract, contract_dir)
        assert len(results) == 1
        check_map = {c.name: c for c in results[0].checks}
        assert check_map["run_state_succeeded"].passed is True

        # Calling with a different dir (the old cwd() bug) should NOT find it.
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        results_bug = verify_acquisition(contract, other_dir)
        check_map_bug = {c.name: c for c in results_bug[0].checks}
        # run_record_present fails because there's no record under other_dir
        assert "run_record_present" in check_map_bug
        assert check_map_bug["run_record_present"].passed is False


# ---------------------------------------------------------------------------
# Bug A4-1: _verify_local_file for csv and parquet
# ---------------------------------------------------------------------------


class TestVerifyLocalFile:
    """A4-1 — verify.py dispatches csv/parquet formats to _verify_local_file."""

    def _write_csv(self, path: Path, rows: int = 5) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["id,name,value"]
        for i in range(rows):
            lines.append(f"{i},item_{i},{i * 10}")
        path.write_text("\n".join(lines))

    def test_csv_file_exists_and_readable(self, tmp_path: Path):
        from fluid_build.cli.verify import _verify_local_file

        csv_path = tmp_path / "out.csv"
        self._write_csv(csv_path, rows=3)

        expose_config = {
            "binding": {
                "format": "csv",
                "location": {"path": str(csv_path)},
            },
        }
        result = _verify_local_file("my_expose", expose_config, "csv")
        assert result["status"] == "match"
        assert result["exists"] is True
        assert result["row_count"] == 3
        assert "id" in result["actual_columns"]

    def test_parquet_file_exists_and_readable(self, tmp_path: Path):
        pytest.importorskip("duckdb")
        import duckdb

        from fluid_build.cli.verify import _verify_local_file

        parquet_path = tmp_path / "out.parquet"
        parquet_path.parent.mkdir(parents=True, exist_ok=True)

        # Write a parquet file via duckdb
        con = duckdb.connect(":memory:")
        con.execute(
            f"COPY (SELECT 1 AS id, 'hello' AS name) TO {str(parquet_path)!r} (FORMAT PARQUET)"
        )
        con.close()

        expose_config = {
            "binding": {
                "format": "parquet",
                "location": {"path": str(parquet_path)},
            },
        }
        result = _verify_local_file("my_parquet_expose", expose_config, "parquet")
        assert result["status"] == "match"
        assert result["row_count"] == 1
        assert "id" in result["actual_columns"]

    def test_missing_file_returns_error(self, tmp_path: Path):
        from fluid_build.cli.verify import _verify_local_file

        expose_config = {
            "binding": {
                "format": "csv",
                "location": {"path": str(tmp_path / "missing.csv")},
            }
        }
        result = _verify_local_file("x", expose_config, "csv")
        assert result["status"] == "error"
        assert result["exists"] is False

    def test_no_path_declared_returns_error(self):
        from fluid_build.cli.verify import _verify_local_file

        expose_config = {"binding": {"format": "csv", "location": {}}}
        result = _verify_local_file("x", expose_config, "csv")
        assert result["status"] == "error"
        assert "no location.path" in result["error"]

    def test_schema_drift_detected(self, tmp_path: Path):
        from fluid_build.cli.verify import _verify_local_file

        csv_path = tmp_path / "out.csv"
        csv_path.write_text("a,b\n1,2\n3,4\n")

        expose_config = {
            "binding": {"format": "csv", "location": {"path": str(csv_path)}},
            "contract": {
                "schema": [
                    {"name": "a", "type": "integer"},
                    {"name": "b", "type": "integer"},
                    {"name": "c", "type": "string"},  # extra expected column
                ]
            },
        }
        result = _verify_local_file("x", expose_config, "csv")
        assert result["status"] == "mismatch"
        dim = result["dimensions"]["schema_structure"]
        assert "c" in dim["missing_fields"]


# ---------------------------------------------------------------------------
# Bug A4-2: local provider honours declared format + path from binding
# ---------------------------------------------------------------------------


class TestLocalProviderOutputFormat:
    """A4-2 — local provider writes parquet when contract says format: parquet."""

    def _parquet_contract(self, out_path: str) -> Dict[str, Any]:
        return {
            "id": "test.product",
            "builds": [
                {
                    "id": "transform",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {"sql": "SELECT 1 AS id, 'hello' AS name"},
                }
            ],
            "exposes": [
                {
                    "exposeId": "out",
                    "binding": {
                        "format": "parquet",
                        "location": {"path": out_path},
                    },
                }
            ],
        }

    def test_parquet_contract_writes_parquet(self, tmp_path: Path):
        pytest.importorskip("duckdb")
        import duckdb

        from fluid_build.providers.local.local import LocalProvider

        out_path = str(tmp_path / "output.parquet")
        contract = self._parquet_contract(out_path)

        provider = LocalProvider(project="local", region="local")
        actions = provider._derive_actions_from_contract(contract)
        assert len(actions) == 1
        action = actions[0]

        # Output spec must honour declared path and format.
        outputs = action["outputs"]
        assert len(outputs) == 1
        out_spec = outputs[0]
        # Can be dict with 'path' and 'format' keys.
        if isinstance(out_spec, dict):
            assert out_spec["format"] == "parquet"
            assert out_spec["path"] == out_path
        else:
            # String path must end in .parquet
            assert str(out_spec).endswith(".parquet")

    def test_csv_contract_writes_csv(self, tmp_path: Path):
        pytest.importorskip("duckdb")

        from fluid_build.providers.local.local import LocalProvider

        out_path = str(tmp_path / "output.csv")
        contract = {
            "id": "test.csv",
            "builds": [
                {
                    "id": "t",
                    "engine": "sql",
                    "properties": {"sql": "SELECT 1 AS x"},
                }
            ],
            "exposes": [
                {
                    "exposeId": "out",
                    "binding": {
                        "format": "csv",
                        "location": {"path": out_path},
                    },
                }
            ],
        }
        provider = LocalProvider(project="local", region="local")
        actions = provider._derive_actions_from_contract(contract)
        out_spec = actions[0]["outputs"][0]
        if isinstance(out_spec, dict):
            assert out_spec["format"] == "csv"
        else:
            assert not str(out_spec).endswith(".parquet")

    def test_parquet_apply_creates_parquet_file(self, tmp_path: Path):
        """End-to-end: apply writes actual parquet bytes."""
        pytest.importorskip("duckdb")
        import duckdb

        from fluid_build.providers.local.local import LocalProvider

        out_path = str(tmp_path / "result.parquet")
        contract = self._parquet_contract(out_path)
        provider = LocalProvider(project="local", region="local")
        result = provider.apply(actions=None, plan={"contract": contract})
        assert result.get("failed", 1) == 0
        assert Path(out_path).exists(), "parquet file must exist after apply"
        # Confirm it's readable parquet
        con = duckdb.connect(":memory:")
        rows = con.sql(f"SELECT COUNT(*) FROM read_parquet({out_path!r})").fetchone()[0]
        assert rows >= 1


# ---------------------------------------------------------------------------
# Bug A4-A: embedded-SQL builds don't emit "Script not found"
# ---------------------------------------------------------------------------


class TestEmbeddedSqlBuildRunner:
    """A4-A — is_embedded_sql_build detected; no "Script not found" emitted."""

    def test_engine_sql_detected_as_embedded(self):
        from fluid_build.build_runners.base import is_embedded_sql_build

        build = {"id": "x", "engine": "sql", "properties": {"sql": "SELECT 1"}}
        assert is_embedded_sql_build(build) is True

    def test_pattern_embedded_logic_with_sql_detected(self):
        from fluid_build.build_runners.base import is_embedded_sql_build

        build = {
            "id": "x",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1"},
        }
        assert is_embedded_sql_build(build) is True

    def test_pattern_embedded_logic_without_sql_not_detected(self):
        from fluid_build.build_runners.base import is_embedded_sql_build

        # No inline sql — should fall through to python runner
        build = {"id": "x", "pattern": "embedded-logic", "properties": {}}
        assert is_embedded_sql_build(build) is False

    def test_acquisition_not_embedded(self):
        from fluid_build.build_runners.base import is_embedded_sql_build

        build = {"id": "x", "pattern": "acquisition", "engine": "duckdb"}
        assert is_embedded_sql_build(build) is False

    def test_dbt_not_embedded(self):
        from fluid_build.build_runners.base import is_embedded_sql_build

        build = {"id": "x", "engine": "dbt"}
        assert is_embedded_sql_build(build) is False

    def test_execute_embedded_sql_build_success(self, tmp_path: Path):
        pytest.importorskip("duckdb")

        from fluid_build.build_runners.base import _execute_embedded_sql_build

        build = {
            "id": "inline",
            "engine": "sql",
            "properties": {"sql": "SELECT 42 AS answer"},
        }
        contract = {
            "id": "test.embedded",
            "builds": [build],
            "consumes": [],
            "exposes": [
                {
                    "exposeId": "out",
                    "binding": {"format": "csv", "location": {"path": str(tmp_path / "out.csv")}},
                }
            ],
        }
        result = _execute_embedded_sql_build(build, contract, tmp_path, dry_run=False)
        assert result == 0

    def test_execute_embedded_sql_build_dry_run(self, tmp_path: Path):
        from fluid_build.build_runners.base import _execute_embedded_sql_build

        build = {
            "id": "inline",
            "engine": "sql",
            "properties": {"sql": "SELECT 99 AS x"},
        }
        contract = {"id": "test", "builds": [build], "exposes": []}
        result = _execute_embedded_sql_build(build, contract, tmp_path, dry_run=True)
        assert result == 0  # dry run always succeeds without writing files


# ---------------------------------------------------------------------------
