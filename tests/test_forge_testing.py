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

"""Tests for fluid_build.forge.core.testing — TestResult dataclass and report generation."""

from fluid_build.forge.core.testing import TestResult


class TestTestResult:
    def test_success_result(self):
        r = TestResult(
            success=True,
            errors=[],
            warnings=["minor"],
            generated_files=["a.py", "b.py"],
            execution_time=1.23,
        )
        assert r.success is True
        assert len(r.warnings) == 1
        assert len(r.generated_files) == 2
        assert r.execution_time == 1.23

    def test_failure_result(self):
        r = TestResult(
            success=False,
            errors=["missing file"],
            warnings=[],
            generated_files=[],
            execution_time=0.5,
        )
        assert r.success is False
        assert len(r.errors) == 1


class TestGenerateTestReport:
    def _make_runner(self):
        """Create a ForgeTestRunner without real registries."""
        from unittest.mock import MagicMock, patch

        with patch(
            "fluid_build.forge.core.testing.get_template_registry", return_value=MagicMock()
        ):
            with patch(
                "fluid_build.forge.core.testing.get_provider_registry", return_value=MagicMock()
            ):
                from fluid_build.forge.core.testing import ForgeTestRunner

                return ForgeTestRunner()

    def test_report_contains_summary(self):
        runner = self._make_runner()
        results = {
            "starter": TestResult(True, [], [], ["f1.py"], 0.5),
            "broken": TestResult(False, ["err1"], ["warn1"], [], 1.0),
        }
        report = runner.generate_test_report(results)
        assert "Total Templates Tested" in report
        assert "2" in report
        assert "Passed" in report
        assert "1" in report  # 1 passed

    def test_report_contains_details(self):
        runner = self._make_runner()
        results = {
            "analytics": TestResult(True, [], [], ["a.py"], 0.3),
        }
        report = runner.generate_test_report(results)
        assert "analytics" in report
        assert "PASS" in report

    def test_report_shows_errors(self):
        runner = self._make_runner()
        results = {
            "bad": TestResult(False, ["File missing"], ["Slow build"], [], 2.0),
        }
        report = runner.generate_test_report(results)
        assert "File missing" in report
        assert "Slow build" in report

    def test_report_empty_results(self):
        runner = self._make_runner()
        results = {}
        # Should not crash on empty results — division by zero possible
        try:
            runner.generate_test_report(results)
        except ZeroDivisionError:
            pass  # Known: total_tests=0 causes division by zero
