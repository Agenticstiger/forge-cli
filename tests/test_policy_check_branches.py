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

"""Branch-coverage tests for fluid_build/cli/policy_check.py"""

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---- Helper: create mock PolicyEnforcementResult ----


def _mock_result(compliant=True, score=95, violations=None, passed=10, failed=0):
    r = MagicMock()
    r.is_compliant.return_value = compliant
    r.calculate_score.return_value = score
    r.violations = violations or []
    r.checks_passed = passed
    r.checks_failed = failed
    r.get_blocking_violations.return_value = [
        v
        for v in (violations or [])
        if getattr(v, "severity", None) and v.severity.value == "critical"
    ]
    r.get_by_category.return_value = []
    r.to_dict.return_value = {"score": score, "violations": []}
    return r


# ---- _create_score_bar ----


class TestCreateScoreBar:
    def test_high_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(95)
        assert "100" in bar
        assert "🏆" in bar

    def test_good_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(85)
        assert "✨" in bar

    def test_fair_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(75)
        assert "👍" in bar

    def test_needs_work_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(55)
        assert "⚠️" in bar

    def test_critical_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(30)
        assert "🚨" in bar

    def test_zero_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(0)
        assert "0/100" in bar

    def test_perfect_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(100)
        assert "100/100" in bar


# ---- _estimate_passed_checks ----


class TestEstimatePassedChecks:
    def test_no_violations(self):
        from fluid_build.cli.policy_check import _estimate_passed_checks
        from fluid_build.policy.schema_engine import PolicyCategory

        result = MagicMock()
        result.checks_passed = 25
        result.get_by_category.return_value = []
        count = _estimate_passed_checks(result, PolicyCategory.SENSITIVITY)
        assert count == 5  # 25 // 5 categories - 0

    def test_with_violations(self):
        from fluid_build.cli.policy_check import _estimate_passed_checks
        from fluid_build.policy.schema_engine import PolicyCategory

        result = MagicMock()
        result.checks_passed = 25
        result.get_by_category.return_value = [MagicMock(), MagicMock()]  # 2 violations
        count = _estimate_passed_checks(result, PolicyCategory.DATA_QUALITY)
        assert count == 3  # 25 // 5 - 2

    def test_clamp_zero(self):
        from fluid_build.cli.policy_check import _estimate_passed_checks
        from fluid_build.policy.schema_engine import PolicyCategory

        result = MagicMock()
        result.checks_passed = 0
        result.get_by_category.return_value = [MagicMock()] * 5
        count = _estimate_passed_checks(result, PolicyCategory.LIFECYCLE)
        assert count == 0


# ---- register ----


class TestPolicyCheckRegister:
    def test_register(self):
        from fluid_build.cli.policy_check import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

    def test_command_name(self):
        from fluid_build.cli.policy_check import COMMAND

        assert COMMAND == "policy-check"


# ---- output_json ----


class TestOutputJson:
    def test_to_stdout(self, capsys):
        from fluid_build.cli.policy_check import output_json

        result = _mock_result()
        output_json(result, None)

    def test_to_file(self, tmp_path):
        from fluid_build.cli.policy_check import output_json

        result = _mock_result()
        out_file = str(tmp_path / "report.json")
        output_json(result, out_file)
        data = json.loads(Path(out_file).read_text())
        assert "score" in data


# ---- output_text ----


class TestOutputText:
    def test_compliant(self, capsys):
        from fluid_build.cli.policy_check import output_text

        result = _mock_result(compliant=True)
        output_text(result, {"id": "test-contract"}, False, False)

    def test_not_compliant(self, capsys):
        from fluid_build.cli.policy_check import output_text

        result = _mock_result(compliant=False)
        v = MagicMock()
        v.severity = MagicMock()
        v.severity.value = "error"
        v.message = "Missing encryption"
        v.expose_id = "exp1"
        v.field = "ssn"
        v.remediation = "Add encryption"
        result.violations = [v]
        result.get_by_category.return_value = [v]
        output_text(result, {"id": "test"}, False, False)


# ---- run ----


class TestPolicyCheckRun:
    def _make_args(self, contract_path, **extra):
        defaults = dict(
            contract=str(contract_path),
            env=None,
            strict=False,
            category=None,
            output=None,
            format="text",
            show_passed=False,
        )
        defaults.update(extra)
        return argparse.Namespace(**defaults)

    @patch("fluid_build.cli.policy_check.SchemaBasedPolicyEngine")
    @patch("fluid_build.cli.policy_check.load_contract_with_overlay")
    def test_run_compliant(self, mock_load, mock_engine, tmp_path):
        from fluid_build.cli.policy_check import run

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text("id: test")
        mock_load.return_value = {"id": "test"}
        mock_engine.return_value.enforce_all.return_value = _mock_result(compliant=True)
        args = self._make_args(contract_file)
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.policy_check.SchemaBasedPolicyEngine")
    @patch("fluid_build.cli.policy_check.load_contract_with_overlay")
    def test_run_not_compliant(self, mock_load, mock_engine, tmp_path):
        from fluid_build.cli.policy_check import run

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text("id: test")
        mock_load.return_value = {"id": "test"}
        mock_engine.return_value.enforce_all.return_value = _mock_result(compliant=False, failed=2)
        args = self._make_args(contract_file)
        assert run(args, logging.getLogger()) == 1

    @patch("fluid_build.cli.policy_check.SchemaBasedPolicyEngine")
    @patch("fluid_build.cli.policy_check.load_contract_with_overlay")
    def test_run_strict_with_violations(self, mock_load, mock_engine, tmp_path):
        from fluid_build.cli.policy_check import run

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text("id: test")
        mock_load.return_value = {"id": "test"}
        v = MagicMock()
        v.severity = MagicMock(value="warning")
        result = _mock_result(compliant=True, violations=[v])
        mock_engine.return_value.enforce_all.return_value = result
        args = self._make_args(contract_file, strict=True)
        assert run(args, logging.getLogger()) == 1

    def test_run_missing_contract(self, tmp_path):
        from fluid_build.cli.policy_check import run

        args = self._make_args(tmp_path / "nonexistent.yaml", format="text")
        assert run(args, logging.getLogger()) == 1

    @patch("fluid_build.cli.policy_check.SchemaBasedPolicyEngine")
    @patch("fluid_build.cli.policy_check.load_contract_with_overlay")
    def test_run_json_output(self, mock_load, mock_engine, tmp_path):
        from fluid_build.cli.policy_check import run

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text("id: test")
        mock_load.return_value = {"id": "test"}
        mock_engine.return_value.enforce_all.return_value = _mock_result()
        args = self._make_args(contract_file, format="json")
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.policy_check.SchemaBasedPolicyEngine")
    @patch("fluid_build.cli.policy_check.load_contract_with_overlay")
    def test_run_with_category_filter(self, mock_load, mock_engine, tmp_path):
        from fluid_build.cli.policy_check import run

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text("id: test")
        mock_load.return_value = {"id": "test"}
        result = _mock_result()
        mock_engine.return_value.enforce_all.return_value = result
        args = self._make_args(contract_file, category="sensitivity")
        assert run(args, logging.getLogger()) == 0

    @patch("fluid_build.cli.policy_check.SchemaBasedPolicyEngine")
    @patch("fluid_build.cli.policy_check.load_contract_with_overlay")
    def test_run_exception(self, mock_load, _mock_engine, tmp_path):
        from fluid_build.cli.policy_check import run

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text("id: test")
        mock_load.side_effect = RuntimeError("file error")
        args = self._make_args(contract_file)
        assert run(args, logging.getLogger()) == 1
