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

"""Tests for fluid_build.tools (visualizer, diff, diagnostics)."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest


class TestVisualizer:
    """Tests for fluid_build.tools.visualizer.to_dot()."""

    def test_single_action(self):
        from fluid_build.tools.visualizer import to_dot

        plan = [{"op": "create", "resource_type": "table", "resource_id": "users"}]
        result = to_dot(plan)
        assert "digraph PLAN" in result
        assert "create\\ntable\\nusers" in result
        assert "->" not in result  # single node, no edges

    def test_multiple_actions_produce_edges(self):
        from fluid_build.tools.visualizer import to_dot

        plan = [
            {"op": "create", "resource_type": "dataset", "resource_id": "raw"},
            {"op": "create", "resource_type": "table", "resource_id": "events"},
        ]
        result = to_dot(plan)
        assert "n0 -> n1" in result

    def test_empty_plan(self):
        from fluid_build.tools.visualizer import to_dot

        result = to_dot([])
        assert "digraph PLAN" in result
        assert "n0" not in result

    def test_three_actions_chain(self):
        from fluid_build.tools.visualizer import to_dot

        plan = [
            {"op": "create", "resource_type": "dataset", "resource_id": "a"},
            {"op": "create", "resource_type": "table", "resource_id": "b"},
            {"op": "grant", "resource_type": "role", "resource_id": "c"},
        ]
        result = to_dot(plan)
        assert "n0 -> n1" in result
        assert "n1 -> n2" in result
        assert result.endswith("}")


class TestDiff:
    """Tests for fluid_build.tools.diff.plan_diff()."""

    def test_identical_plans(self, tmp_path):
        from fluid_build.tools.diff import plan_diff

        data = {"actions": [{"op": "create"}]}
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps(data))
        b.write_text(json.dumps(data))
        result = plan_diff(str(a), str(b))
        assert result == ""

    def test_different_plans(self, tmp_path):
        from fluid_build.tools.diff import plan_diff

        a_data = {"actions": [{"op": "create"}]}
        b_data = {"actions": [{"op": "delete"}]}
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps(a_data))
        b.write_text(json.dumps(b_data))
        result = plan_diff(str(a), str(b))
        assert "create" in result
        assert "delete" in result
        assert "---" in result or "+++" in result

    def test_file_not_found(self, tmp_path):
        from fluid_build.tools.diff import plan_diff

        with pytest.raises(FileNotFoundError):
            plan_diff(str(tmp_path / "missing.json"), str(tmp_path / "also_missing.json"))

    def test_plan_diff_closes_opened_files(self):
        from fluid_build.tools.diff import plan_diff

        opened_files = [
            io.StringIO(json.dumps({"actions": [{"op": "create"}]})),
            io.StringIO(json.dumps({"actions": [{"op": "delete"}]})),
        ]

        with patch("builtins.open", side_effect=opened_files):
            result = plan_diff("plan-a.json", "plan-b.json")

        assert "create" in result
        assert "delete" in result
        assert opened_files[0].closed is True
        assert opened_files[1].closed is True


class TestDiagnostics:
    """Tests for fluid_build.tools.diagnostics.doctor()."""

    @patch("fluid_build.tools.diagnostics.auth_doctor")
    def test_doctor_returns_dict(self, mock_auth):
        from fluid_build.tools.diagnostics import doctor

        mock_auth.return_value = {"status": "ok"}
        result = doctor("gcp", "my-project")
        assert isinstance(result, dict)
        assert result["provider"] == "gcp"
        assert "timestamp" in result
        assert "python" in result
        assert "platform" in result
        assert "env" in result
        assert result["auth"] == {"status": "ok"}

    @patch("fluid_build.tools.diagnostics.auth_doctor")
    def test_doctor_env_keys(self, mock_auth):
        from fluid_build.tools.diagnostics import doctor

        mock_auth.return_value = {}
        result = doctor("gcp")
        env = result["env"]
        assert "GOOGLE_APPLICATION_CREDENTIALS" in env
        assert "FLUID_LOG_LEVEL" in env

    @patch("fluid_build.tools.diagnostics.auth_doctor")
    @patch.dict("os.environ", {"FLUID_LOG_LEVEL": "DEBUG"}, clear=False)
    def test_doctor_reads_env_values(self, mock_auth):
        from fluid_build.tools.diagnostics import doctor

        mock_auth.return_value = {}
        result = doctor("local")
        assert result["env"]["FLUID_LOG_LEVEL"] == "DEBUG"
