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

"""Tests for apply.py report generators: _generate_json_report, _generate_markdown_report, _generate_html_report."""

import json
from unittest.mock import MagicMock

from fluid_build.cli.apply import (
    _generate_html_report,
    _generate_json_report,
    _generate_markdown_report,
)
from fluid_build.cli.orchestration import ExecutionContext, ExecutionPlan


def _make_context():
    plan = MagicMock(spec=ExecutionPlan)
    plan.contract_path = "/test/contract.yaml"
    plan.environment = "dev"
    ctx = MagicMock(spec=ExecutionContext)
    ctx.execution_id = "exec-123"
    ctx.plan = plan
    return ctx


def _make_result(success=True):
    return {
        "success": success,
        "metrics": {
            "total_actions": 5,
            "successful_actions": 4,
            "failed_actions": 1 if not success else 0,
            "skipped_actions": 0 if success else 1,
            "total_duration_seconds": 12.5,
        },
        "phases": [
            {"phase": "validate", "status": "success", "action_count": 2, "duration": 3.0},
            {
                "phase": "deploy",
                "status": "success" if success else "failed",
                "action_count": 3,
                "duration": 9.5,
            },
        ],
    }


class TestGenerateJsonReport:
    def test_creates_file(self, tmp_path):
        out = tmp_path / "report.json"
        ctx = _make_context()
        _generate_json_report(_make_result(), out, ctx)
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["execution_id"] == "exec-123"
        assert data["contract_path"] == "/test/contract.yaml"
        assert data["environment"] == "dev"
        assert data["result"]["success"] is True

    def test_failed_result(self, tmp_path):
        out = tmp_path / "report.json"
        _generate_json_report(_make_result(success=False), out, _make_context())
        data = json.loads(out.read_text())
        assert data["result"]["success"] is False


class TestGenerateMarkdownReport:
    def test_creates_file(self, tmp_path):
        out = tmp_path / "report.md"
        _generate_markdown_report(_make_result(), out, _make_context())
        assert out.exists()
        content = out.read_text()
        assert "exec-123" in content
        assert "Success" in content
        assert "| Total Actions | 5 |" in content

    def test_phase_details(self, tmp_path):
        out = tmp_path / "report.md"
        _generate_markdown_report(_make_result(), out, _make_context())
        content = out.read_text()
        assert "Validate" in content
        assert "Deploy" in content

    def test_failed_status(self, tmp_path):
        out = tmp_path / "report.md"
        _generate_markdown_report(_make_result(success=False), out, _make_context())
        content = out.read_text()
        assert "Failed" in content


class TestGenerateHtmlReport:
    def test_creates_file(self, tmp_path):
        out = tmp_path / "report.html"
        _generate_html_report(_make_result(), out, _make_context())
        assert out.exists()
        content = out.read_text()
        assert "<html>" in content
        assert "exec-123" in content
        assert "FLUID Apply Execution Report" in content

    def test_metrics_in_html(self, tmp_path):
        out = tmp_path / "report.html"
        _generate_html_report(_make_result(), out, _make_context())
        content = out.read_text()
        assert "Total Actions" in content
        assert "Successful" in content

    def test_failed_html(self, tmp_path):
        out = tmp_path / "report.html"
        _generate_html_report(_make_result(success=False), out, _make_context())
        content = out.read_text()
        assert "Failed" in content
