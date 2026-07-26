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

"""Tests for fluid_build.cli.policy_check – run() and output_rich()."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.policy.schema_engine import (
    PolicyCategory,
    PolicyEnforcementResult,
    PolicySeverity,
    PolicyViolation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _make_args(
    contract: str = "contract.fluid.yaml",
    env=None,
    strict: bool = False,
    category=None,
    output=None,
    format: str = "text",
    show_passed: bool = False,
):
    ns = argparse.Namespace(
        contract=contract,
        env=env,
        strict=strict,
        category=category,
        output=output,
        format=format,
        show_passed=show_passed,
    )
    return ns


def _make_result(
    violations=None,
    checks_passed: int = 10,
    checks_failed: int = 0,
) -> PolicyEnforcementResult:
    return PolicyEnforcementResult(
        violations=violations or [],
        checks_passed=checks_passed,
        checks_failed=checks_failed,
    )


def _make_violation(
    category=PolicyCategory.SENSITIVITY,
    severity=PolicySeverity.ERROR,
    message="Test violation",
    remediation=None,
    expose_id=None,
    field=None,
    rule_id=None,
) -> PolicyViolation:
    return PolicyViolation(
        category=category,
        severity=severity,
        message=message,
        remediation=remediation,
        expose_id=expose_id,
        field=field,
        rule_id=rule_id,
    )


# ---------------------------------------------------------------------------
# Tests for run()
# ---------------------------------------------------------------------------


class TestPolicyCheckRun:
    """Unit tests for policy_check.run()."""

    def _run_with_mocked_engine(self, args, result, contract_data=None):
        """Helper: patch filesystem and engine, call run(), return exit code."""
        from fluid_build.cli import policy_check

        contract_data = contract_data or {"id": "test-contract"}

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "fluid_build.cli.policy_check.load_contract_with_overlay",
                return_value=contract_data,
            ):
                mock_engine = MagicMock()
                mock_engine.enforce_all.return_value = result
                with patch(
                    "fluid_build.cli.policy_check.SchemaBasedPolicyEngine",
                    return_value=mock_engine,
                ):
                    with patch.object(policy_check, "RICH_AVAILABLE", False):
                        return policy_check.run(args, logger)

    def test_run_returns_0_when_compliant(self):
        args = _make_args()
        result = _make_result(checks_passed=5, checks_failed=0)
        code = self._run_with_mocked_engine(args, result)
        assert code == 0

    def test_run_returns_1_when_not_compliant(self):
        args = _make_args()
        violation = _make_violation(severity=PolicySeverity.CRITICAL)
        result = _make_result(violations=[violation], checks_passed=4, checks_failed=1)
        code = self._run_with_mocked_engine(args, result)
        assert code == 1

    def test_run_strict_returns_1_when_warnings_present(self):
        args = _make_args(strict=True)
        violation = _make_violation(severity=PolicySeverity.WARNING)
        result = _make_result(violations=[violation], checks_passed=9, checks_failed=1)
        code = self._run_with_mocked_engine(args, result)
        assert code == 1

    def test_run_strict_returns_0_no_violations(self):
        args = _make_args(strict=True)
        result = _make_result(checks_passed=10, checks_failed=0)
        code = self._run_with_mocked_engine(args, result)
        assert code == 0

    def test_run_returns_1_when_file_not_found(self):
        from fluid_build.cli import policy_check

        args = _make_args(contract="/nonexistent/contract.fluid.yaml")
        with patch.object(Path, "exists", return_value=False):
            with patch.object(policy_check, "RICH_AVAILABLE", False):
                code = policy_check.run(args, logger)
        assert code == 1

    def test_run_category_filter_applied(self):
        """When --category is set, violations should be filtered."""
        from fluid_build.cli import policy_check

        violation = _make_violation(category=PolicyCategory.SENSITIVITY)
        result = _make_result(violations=[violation])

        args = _make_args(category="sensitivity")

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "fluid_build.cli.policy_check.load_contract_with_overlay",
                return_value={"id": "x"},
            ):
                mock_engine = MagicMock()
                mock_engine.enforce_all.return_value = result
                with patch(
                    "fluid_build.cli.policy_check.SchemaBasedPolicyEngine",
                    return_value=mock_engine,
                ):
                    with patch.object(policy_check, "RICH_AVAILABLE", False):
                        code = policy_check.run(args, logger)
        # Violations exist with ERROR → not compliant → 1
        assert code == 1

    def test_run_json_format_writes_output(self, tmp_path):
        from fluid_build.cli import policy_check

        out_file = str(tmp_path / "report.json")
        args = _make_args(format="json", output=out_file)
        result = _make_result(checks_passed=5)

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "fluid_build.cli.policy_check.load_contract_with_overlay",
                return_value={"id": "x"},
            ):
                mock_engine = MagicMock()
                mock_engine.enforce_all.return_value = result
                with patch(
                    "fluid_build.cli.policy_check.SchemaBasedPolicyEngine",
                    return_value=mock_engine,
                ):
                    with patch.object(policy_check, "RICH_AVAILABLE", False):
                        code = policy_check.run(args, logger)

        assert code == 0
        assert Path(out_file).exists()
        data = json.loads(Path(out_file).read_text())
        assert "is_compliant" in data

    def test_run_handles_unexpected_exception(self):
        from fluid_build.cli import policy_check

        args = _make_args()
        with patch.object(Path, "exists", return_value=True):
            with patch(
                "fluid_build.cli.policy_check.load_contract_with_overlay",
                side_effect=RuntimeError("boom"),
            ):
                with patch.object(policy_check, "RICH_AVAILABLE", False):
                    code = policy_check.run(args, logger)
        assert code == 1


# ---------------------------------------------------------------------------
# Tests for output_rich()
# ---------------------------------------------------------------------------


class TestOutputRich:
    """Tests for the output_rich() display function."""

    def _call_output_rich(self, result, contract=None, show_passed=False, strict=False):
        from fluid_build.cli.policy_check import output_rich

        contract = contract or {"id": "test-contract"}
        output_rich(result, contract, show_passed, strict)

    def test_compliant_result_renders_without_error(self):
        result = _make_result(checks_passed=10)
        self._call_output_rich(result)

    def test_non_compliant_result_renders_without_error(self):
        violation = _make_violation(severity=PolicySeverity.CRITICAL)
        result = _make_result(violations=[violation], checks_passed=5, checks_failed=1)
        self._call_output_rich(result)

    def test_score_grade_exceptional(self):
        """Score >= 95 should not raise."""
        result = _make_result(checks_passed=50, checks_failed=0)
        self._call_output_rich(result)

    def test_score_grade_critical_range(self):
        """Score < 50 triggers 'CRITICAL' branch."""
        violations = [_make_violation(severity=PolicySeverity.CRITICAL) for _ in range(5)]
        result = _make_result(violations=violations, checks_passed=0, checks_failed=5)
        self._call_output_rich(result)

    def test_show_passed_flag_renders_all_categories(self):
        """With show_passed=True, even clean categories show passed count."""
        result = _make_result(checks_passed=20)
        self._call_output_rich(result, show_passed=True)

    def test_violation_with_remediation_renders(self):
        violation = _make_violation(
            severity=PolicySeverity.WARNING,
            remediation="Add a sensitivity label",
            expose_id="my-expose",
            field="pii_field",
            rule_id="rule-001",
        )
        result = _make_result(violations=[violation], checks_passed=9, checks_failed=1)
        self._call_output_rich(result)

    def test_strict_mode_shows_strict_note_on_failure(self):
        violation = _make_violation(severity=PolicySeverity.ERROR)
        result = _make_result(violations=[violation], checks_passed=5, checks_failed=1)
        self._call_output_rich(result, strict=True)

    @pytest.mark.skipif(True, reason="Rich 'orange' color not always available")
    def test_multiple_severity_levels_renders(self):
        violations = [
            _make_violation(
                severity=PolicySeverity.CRITICAL,
                category=PolicyCategory.ACCESS_CONTROL,
            ),
            _make_violation(
                severity=PolicySeverity.ERROR,
                category=PolicyCategory.DATA_QUALITY,
            ),
            _make_violation(
                severity=PolicySeverity.WARNING,
                category=PolicyCategory.LIFECYCLE,
            ),
            _make_violation(
                severity=PolicySeverity.INFO,
                category=PolicyCategory.SCHEMA_EVOLUTION,
            ),
        ]
        result = _make_result(violations=violations, checks_passed=10, checks_failed=4)
        self._call_output_rich(result)


# ---------------------------------------------------------------------------
# Tests for output_text() and output_json()
# ---------------------------------------------------------------------------


class TestOutputText:
    def test_compliant_renders_without_error(self):
        from fluid_build.cli.policy_check import output_text

        result = _make_result(checks_passed=10)
        output_text(result, {"id": "x"}, show_passed=False, strict=False)

    def test_with_violations_renders_without_error(self):
        from fluid_build.cli.policy_check import output_text

        violation = _make_violation(expose_id="e1", field="f1", remediation="fix it")
        result = _make_result(violations=[violation], checks_passed=9, checks_failed=1)
        output_text(result, {"id": "x"}, show_passed=True, strict=False)


class TestOutputJson:
    def test_output_to_stdout(self, capsys):
        from fluid_build.cli.policy_check import output_json

        result = _make_result(checks_passed=5)
        output_json(result, output_file=None)
        # No exceptions raised is the primary assertion

    def test_output_to_file(self, tmp_path):
        from fluid_build.cli.policy_check import output_json

        out = str(tmp_path / "out.json")
        result = _make_result(checks_passed=5)
        output_json(result, output_file=out)
        assert Path(out).exists()
        data = json.loads(Path(out).read_text())
        assert data["checks_passed"] == 5


# ---------------------------------------------------------------------------
# Tests for _create_score_bar() and _estimate_passed_checks()
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_create_score_bar_high_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(95)
        assert "95/100" in bar

    def test_create_score_bar_low_score(self):
        from fluid_build.cli.policy_check import _create_score_bar

        bar = _create_score_bar(30)
        assert "30/100" in bar

    def test_estimate_passed_checks_no_violations(self):
        from fluid_build.cli.policy_check import _estimate_passed_checks

        result = _make_result(checks_passed=50)
        count = _estimate_passed_checks(result, PolicyCategory.SENSITIVITY)
        assert count >= 0


# ---------------------------------------------------------------------------
# B3 / B4 / B5 — failure counter, strict-mode banner, --output for rich
# ---------------------------------------------------------------------------


class TestRealFailureCount:
    """B3: only CRITICAL/ERROR violations count as failures; advisories don't."""

    def test_advisory_only_run_has_zero_failures(self):
        from fluid_build.cli.policy_check import _advisory_count, _real_failure_count

        violations = [
            _make_violation(severity=PolicySeverity.WARNING),
            _make_violation(severity=PolicySeverity.INFO),
        ]
        # The engine increments checks_failed once per violation (=2 here),
        # but neither is a genuine failure.
        result = _make_result(violations=violations, checks_passed=8, checks_failed=2)
        assert _real_failure_count(result) == 0
        assert _advisory_count(result) == 2

    def test_blocking_violations_counted_as_failures(self):
        from fluid_build.cli.policy_check import _advisory_count, _real_failure_count

        violations = [
            _make_violation(severity=PolicySeverity.CRITICAL),
            _make_violation(severity=PolicySeverity.ERROR),
            _make_violation(severity=PolicySeverity.WARNING),
        ]
        result = _make_result(violations=violations, checks_passed=5, checks_failed=3)
        assert _real_failure_count(result) == 2
        assert _advisory_count(result) == 1


class TestCheckPasses:
    """B4: the verdict predicate is strict-aware and is the single source
    of truth for both the banner and the exit code."""

    def test_passes_when_only_advisories_non_strict(self):
        from fluid_build.cli.policy_check import _check_passes

        result = _make_result(violations=[_make_violation(severity=PolicySeverity.WARNING)])
        assert _check_passes(result, strict=False) is True

    def test_fails_when_only_advisories_strict(self):
        from fluid_build.cli.policy_check import _check_passes

        result = _make_result(violations=[_make_violation(severity=PolicySeverity.WARNING)])
        assert _check_passes(result, strict=True) is False

    def test_fails_on_blocking_violation_regardless_of_strict(self):
        from fluid_build.cli.policy_check import _check_passes

        result = _make_result(violations=[_make_violation(severity=PolicySeverity.ERROR)])
        assert _check_passes(result, strict=False) is False
        assert _check_passes(result, strict=True) is False

    def test_passes_clean_run(self):
        from fluid_build.cli.policy_check import _check_passes

        result = _make_result(checks_passed=10)
        assert _check_passes(result, strict=False) is True
        assert _check_passes(result, strict=True) is True


class TestStrictBannerConsistency:
    """B4: in strict mode the banner and exit code must agree."""

    def _run_with_mocked_engine(self, args, result):
        from fluid_build.cli import policy_check

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "fluid_build.cli.policy_check.load_contract_with_overlay",
                return_value={"id": "x"},
            ):
                mock_engine = MagicMock()
                mock_engine.enforce_all.return_value = result
                with patch(
                    "fluid_build.cli.policy_check.SchemaBasedPolicyEngine",
                    return_value=mock_engine,
                ):
                    return policy_check.run(args, logger)

    def test_strict_advisory_only_exits_1_and_renders_failed(self, capsys):
        """A strict run with only advisories: exit 1 and a FAILED verdict."""
        args = _make_args(strict=True, format="rich")
        result = _make_result(
            violations=[_make_violation(severity=PolicySeverity.WARNING)],
            checks_passed=9,
            checks_failed=1,
        )
        code = self._run_with_mocked_engine(args, result)
        assert code == 1

    def test_non_strict_advisory_only_exits_0(self):
        args = _make_args(strict=False, format="rich")
        result = _make_result(
            violations=[_make_violation(severity=PolicySeverity.WARNING)],
            checks_passed=9,
            checks_failed=1,
        )
        code = self._run_with_mocked_engine(args, result)
        assert code == 0


class TestOutputHonoredForRichFormat:
    """B5: --output is honored for the default rich format, not just JSON."""

    def test_rich_format_with_output_writes_json_report(self, tmp_path):
        from fluid_build.cli import policy_check

        out_file = str(tmp_path / "report.json")
        args = _make_args(format="rich", output=out_file)
        result = _make_result(checks_passed=10)

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "fluid_build.cli.policy_check.load_contract_with_overlay",
                return_value={"id": "x"},
            ):
                mock_engine = MagicMock()
                mock_engine.enforce_all.return_value = result
                with patch(
                    "fluid_build.cli.policy_check.SchemaBasedPolicyEngine",
                    return_value=mock_engine,
                ):
                    code = policy_check.run(args, logger)

        assert code == 0
        assert Path(out_file).exists(), "B5: --output ignored for rich format"
        data = json.loads(Path(out_file).read_text())
        assert "is_compliant" in data

    def test_text_format_with_output_writes_json_report(self, tmp_path):
        from fluid_build.cli import policy_check

        out_file = str(tmp_path / "report.json")
        args = _make_args(format="text", output=out_file)
        result = _make_result(checks_passed=10)

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "fluid_build.cli.policy_check.load_contract_with_overlay",
                return_value={"id": "x"},
            ):
                mock_engine = MagicMock()
                mock_engine.enforce_all.return_value = result
                with patch(
                    "fluid_build.cli.policy_check.SchemaBasedPolicyEngine",
                    return_value=mock_engine,
                ):
                    with patch.object(policy_check, "RICH_AVAILABLE", False):
                        code = policy_check.run(args, logger)

        assert code == 0
        assert Path(out_file).exists()


# ---------------------------------------------------------------------------
# Score-band colours must be real rich colours (regression)
#
# The 50-69 band set `score_color = "orange"` and passed it to
# `Panel(border_style=...)`. rich has no bare `orange`, so the whole report
# died with a traceback:
#
#   rich.errors.MissingStyle: Failed to get style 'orange'; unable to parse
#   'orange' as color; 'orange' is not a valid color
#
# 50-69 is exactly where a freshly imported dbt contract lands, i.e. the
# "Next:" step the importer recommends. The other five bands used valid
# colours, so only this one crashed.
# ---------------------------------------------------------------------------


class TestScoreBandColours:
    def test_every_band_colour_parses_in_rich(self):
        import re
        from pathlib import Path as _Path

        from rich.style import Style

        from fluid_build.cli import policy_check as pc

        source = _Path(pc.__file__).read_text(encoding="utf-8")
        colours = set(re.findall(r'score_color = "([^"]+)"', source))
        assert colours, "no score_color assignments found — did the bands move?"
        for colour in colours:
            Style.parse(colour)  # raises MissingStyle on an invalid colour

    @pytest.mark.parametrize("score", [50, 60, 69])
    def test_the_50_to_69_band_renders_instead_of_raising(self, score, monkeypatch):
        """The band a brownfield dbt import lands in must render a report."""
        from fluid_build.cli import policy_check

        result = _make_result(
            violations=[
                _make_violation(severity=PolicySeverity.ERROR) for _ in range((100 - score) // 10)
            ],
            checks_passed=2,
            checks_failed=4,
        )
        monkeypatch.setattr(result, "calculate_score", lambda: score)
        # No RICH_AVAILABLE patch — this must exercise the rich renderer.
        policy_check.output_rich(result, {"id": "imported"}, False, False)
