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

# tests/providers/test_phase5_tightening.py
"""
Phase 5: Provider system tightening
====================================

Tests cover:
  - Payload flattening in _execute_action
  - SQL identifier validation (_validate_ident)
  - Planner topological sort with table-name dependencies
  - Planner build-input load_data generation
  - Planner expose path resolution (binding.location.path)
  - Materialize error logging (not silently swallowed)
  - End-to-end plan+apply for multi-step contracts
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

try:
    import fluid_provider_sdk  # noqa: F401

    HAS_SDK = True
except ImportError:
    HAS_SDK = False

pytestmark = pytest.mark.skipif(not HAS_SDK, reason="fluid-provider-sdk not installed")

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
from fluid_build.providers.local.local import LocalProvider, _validate_ident

# ===========================================================================
# Test: SQL identifier validation
# ===========================================================================


class TestIdentifierValidation:
    """Test _validate_ident prevents SQL injection."""

    def test_valid_simple(self):
        assert _validate_ident("customers") == "customers"

    def test_valid_with_underscore(self):
        assert _validate_ident("result_clean_customers") == "result_clean_customers"

    def test_valid_with_digits(self):
        assert _validate_ident("table_123") == "table_123"

    def test_valid_single_char(self):
        assert _validate_ident("t") == "t"

    def test_rejects_sql_injection(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _validate_ident("table; DROP TABLE users;--")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _validate_ident("my table")

    def test_rejects_semicolons(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _validate_ident("foo;bar")

    def test_rejects_quotes(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _validate_ident("foo'bar")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _validate_ident("")

    def test_rejects_starts_with_digit(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _validate_ident("123table")


# ===========================================================================
# Test: Payload flattening in _execute_action
# ===========================================================================


class TestPayloadFlattening:
    """Test that _execute_action flattens payload into action dict."""

    def _make_provider(self):
        return LocalProvider(project="test", region="local")

    def test_sql_payload_flattened(self):
        """execute_sql with SQL in payload should work."""
        pytest.importorskip("duckdb")
        p = self._make_provider()
        action = {
            "op": "execute_sql",
            "resource_id": "test_sql",
            "payload": {"sql": "SELECT 42 AS answer", "inputs": [], "output_table": "test_output"},
        }
        # Set up session DB
        session_dir = tempfile.mkdtemp(prefix="fluid_test_")
        p._session_db = str(Path(session_dir) / "test.duckdb")

        try:
            result = p._execute_action(0, action)
            assert result["op"] == "sql"
            assert result.get("rows") is not None
        finally:
            import shutil

            shutil.rmtree(session_dir, ignore_errors=True)
            if hasattr(p, "_session_db"):
                del p._session_db

    def test_noop_action_passthrough(self):
        p = self._make_provider()
        action = {"op": "noop", "original_op": "ensure_dataset"}
        result = p._execute_action(0, action)
        assert result["op"] == "noop"
        assert result["skipped"] is True

    def test_unknown_op_becomes_noop(self):
        p = self._make_provider()
        action = {"op": "unknown_crazy_op"}
        result = p._execute_action(0, action)
        assert result["skipped"] is True

    def test_payload_keys_dont_overwrite_top_level(self):
        """Top-level keys should win over payload keys."""
        p = self._make_provider()
        action = {
            "op": "noop",  # top level
            "original_op": "my_op",
            "payload": {
                "op": "execute_sql",  # should NOT overwrite
            },
        }
        result = p._execute_action(0, action)
        # Should be noop, not execute_sql
        assert result["op"] == "noop"


# ===========================================================================
# Test: Planner topological sort
# ===========================================================================


class TestPlannerTopologicalSort:
    """Test that the planner orders actions correctly."""

    def test_hello_world_ordering(self):
        """Hello world: execute_sql before materialize."""
        from fluid_build.providers.local.planner import plan_actions

        contract = {
            "id": "test.hello",
            "name": "Test",
            "builds": [
                {
                    "id": "sql_build",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {"sql": "SELECT 1 AS value"},
                }
            ],
            "exposes": [
                {
                    "exposeId": "output",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": "runtime/out/test.csv"},
                    },
                }
            ],
        }

        actions = plan_actions(contract, "test", "local")
        ops = [a["op"] for a in actions]

        # execute_sql must come before materialize
        sql_idx = ops.index("execute_sql")
        mat_idx = ops.index("materialize")
        assert (
            sql_idx < mat_idx
        ), f"execute_sql({sql_idx}) should come before materialize({mat_idx})"

    def test_csv_input_ordering(self):
        """CSV input: load_data → execute_sql → materialize."""
        from fluid_build.providers.local.planner import plan_actions

        contract = {
            "id": "test.csv_input",
            "name": "CSV Test",
            "builds": [
                {
                    "id": "transform",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {
                        "sql": "SELECT * FROM input_table",
                        "parameters": {
                            "inputs": [
                                {"name": "input_table", "path": "/tmp/test.csv", "format": "csv"}
                            ]
                        },
                    },
                }
            ],
            "exposes": [
                {
                    "exposeId": "output",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": "runtime/out/test.csv"},
                    },
                }
            ],
        }

        actions = plan_actions(contract, "test", "local")
        ops = [a["op"] for a in actions]

        assert ops.index("load_data") < ops.index(
            "execute_sql"
        ), "load_data should come before execute_sql"
        assert ops.index("execute_sql") < ops.index(
            "materialize"
        ), "execute_sql should come before materialize"

    def test_build_input_generates_load_data(self):
        """Build inputs with file paths should generate load_data actions."""
        from fluid_build.providers.local.planner import plan_actions

        contract = {
            "id": "test.build_input",
            "name": "Build Input Test",
            "builds": [
                {
                    "id": "transform",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {
                        "sql": "SELECT * FROM my_data",
                        "parameters": {
                            "inputs": [
                                {"name": "my_data", "path": "/tmp/data.csv", "format": "csv"}
                            ]
                        },
                    },
                }
            ],
            "exposes": [
                {
                    "exposeId": "output",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": "runtime/out/test.csv"},
                    },
                }
            ],
        }

        actions = plan_actions(contract, "test", "local")
        load_actions = [a for a in actions if a["op"] == "load_data"]
        assert len(load_actions) == 1
        assert load_actions[0]["table_name"] == "my_data"
        assert load_actions[0]["payload"]["path"] == "/tmp/data.csv"


# ===========================================================================
# Test: Planner expose path resolution
# ===========================================================================


class TestPlannerExposePathResolution:
    """Test that the planner correctly extracts expose output paths."""

    def test_binding_location_path(self):
        """binding.location.path should be found."""
        from fluid_build.providers.local.planner import plan_actions

        contract = {
            "id": "test.expose",
            "name": "Test",
            "builds": [
                {
                    "id": "b",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {"sql": "SELECT 1"},
                }
            ],
            "exposes": [
                {
                    "exposeId": "out",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": "runtime/out/custom.csv"},
                    },
                }
            ],
        }

        actions = plan_actions(contract, "test", "local")
        mat_actions = [a for a in actions if a["op"] == "materialize"]
        assert len(mat_actions) == 1
        assert mat_actions[0]["payload"]["path"] == "runtime/out/custom.csv"

    def test_missing_path_uses_default(self):
        """Missing path should generate a sensible default."""
        from fluid_build.providers.local.planner import plan_actions

        contract = {
            "id": "test.default_path",
            "name": "Test",
            "builds": [
                {
                    "id": "b",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {"sql": "SELECT 1"},
                }
            ],
            "exposes": [
                {"exposeId": "my_output", "kind": "table", "binding": {"platform": "local"}}
            ],
        }

        actions = plan_actions(contract, "test", "local")
        mat_actions = [a for a in actions if a["op"] == "materialize"]
        assert len(mat_actions) == 1
        # Should use default path based on expose ID
        path = mat_actions[0]["payload"]["path"]
        assert "my_output" in path
        assert path.endswith(".csv")


# ===========================================================================
# Test: End-to-end plan + apply
# ===========================================================================


class TestEndToEndPlanApply:
    """Test full plan → apply round-trip."""

    def test_hello_world_e2e(self):
        """Simple hello-world contract should plan and apply successfully."""
        pytest.importorskip("duckdb")
        p = LocalProvider(project="test", region="local")

        contract = {
            "id": "test.e2e",
            "name": "E2E Test",
            "builds": [
                {
                    "id": "hello",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {"sql": "SELECT 'hello' AS message, 42 AS answer"},
                }
            ],
            "exposes": [
                {
                    "exposeId": "result",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": "runtime/out/e2e_test.csv"},
                    },
                }
            ],
        }

        actions = p.plan(contract)
        assert len(actions) >= 2

        result = p.apply(actions=actions, plan={"contract": contract})
        assert result["failed"] == 0
        assert result["applied"] >= 2

    def test_csv_input_e2e(self, tmp_path):
        """Contract with CSV input should load, transform, and materialize."""
        pytest.importorskip("duckdb")
        # Create a temp CSV file
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("id,name,value\n1,alpha,100\n2,beta,200\n3,gamma,300\n")

        p = LocalProvider(project="test", region="local")

        contract = {
            "id": "test.csv_e2e",
            "name": "CSV E2E",
            "builds": [
                {
                    "id": "transform",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {
                        "sql": "SELECT id, UPPER(name) AS name, value * 2 AS doubled FROM input_data",
                        "parameters": {
                            "inputs": [
                                {"name": "input_data", "path": str(csv_file), "format": "csv"}
                            ]
                        },
                    },
                }
            ],
            "exposes": [
                {
                    "exposeId": "output",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": str(tmp_path / "output.csv")},
                    },
                }
            ],
        }

        actions = p.plan(contract)
        result = p.apply(actions=actions, plan={"contract": contract})

        assert result["failed"] == 0
        assert result["applied"] >= 3  # load_data + execute_sql + materialize

        # Check output file
        out_file = tmp_path / "output.csv"
        assert out_file.exists()
        content = out_file.read_text()
        assert "ALPHA" in content
        assert "BETA" in content
        assert "600" in content  # 300 * 2


# ===========================================================================
# Test: ApplyResult dict-like access (regression from earlier crash)
# ===========================================================================


class TestApplyResultAccess:
    """Test that ApplyResult supports dict-like access correctly."""

    def test_getitem_by_string(self):
        from fluid_provider_sdk.types import ApplyResult

        r = ApplyResult(
            provider="local", applied=2, failed=0, duration_sec=0.1, timestamp="t", results=[]
        )
        assert r["applied"] == 2
        assert r["failed"] == 0

    def test_get_method(self):
        from fluid_provider_sdk.types import ApplyResult

        r = ApplyResult(
            provider="local", applied=1, failed=0, duration_sec=0.1, timestamp="t", results=[]
        )
        assert r.get("applied") == 1
        assert r.get("nonexistent", "default") == "default"
