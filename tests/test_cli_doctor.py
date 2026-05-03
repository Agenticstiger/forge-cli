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

"""Tests for fluid_build.cli.doctor."""

import argparse
from unittest.mock import MagicMock, patch

import pytest


class TestRegister:
    def test_registers_doctor_command(self):
        from fluid_build.cli.doctor import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

        # Verify the subparser was created by parsing a known argument
        args = parser.parse_args(["doctor"])
        assert args.cmd == "doctor"

    def test_registers_with_default_out_dir(self):
        from fluid_build.cli.doctor import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

        args = parser.parse_args(["doctor"])
        assert args.out_dir == "runtime/diag"

    def test_registers_features_only_flag(self):
        from fluid_build.cli.doctor import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

        args = parser.parse_args(["doctor", "--features-only"])
        assert args.features_only is True

    def test_registers_extended_flag(self):
        from fluid_build.cli.doctor import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

        args = parser.parse_args(["doctor", "--extended"])
        assert args.extended is True

    def test_registers_comprehensive_alias(self):
        from fluid_build.cli.doctor import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

        args = parser.parse_args(["doctor", "--comprehensive"])
        assert args.extended is True


class TestCheckFluidFeatures:
    def test_returns_checks_list(self):
        from fluid_build.cli.doctor import _check_fluid_features

        all_ok, checks = _check_fluid_features()
        assert isinstance(checks, list)
        assert len(checks) > 0
        assert all(isinstance(c, dict) for c in checks)
        assert all("check" in c and "status" in c and "ok" in c for c in checks)

    def test_checks_have_categories(self):
        from fluid_build.cli.doctor import _check_fluid_features

        all_ok, checks = _check_fluid_features()
        categories = {c.get("category") for c in checks}
        assert "core" in categories

    def test_schema_manager_check_present(self):
        from fluid_build.cli.doctor import _check_fluid_features

        all_ok, checks = _check_fluid_features()
        names = [c["check"] for c in checks]
        assert "FLUID Schema Manager" in names

    def test_schema_manager_check_is_available(self):
        """The schema-manager doctor check enumerates the bundled
        v0.7.x schemas. Pre-0.7 schemas (0.4.0, 0.5.x) were dropped."""
        from fluid_build.cli.doctor import _check_fluid_features

        all_ok, checks = _check_fluid_features()
        schema_check = next(c for c in checks if c["check"] == "FLUID Schema Manager")

        assert all_ok is True
        assert schema_check["ok"] is True
        assert schema_check["status"] == "✅ Available"
        # The details string lists the bundled v0.7.x schemas.
        assert "0.7.3" in schema_check["details"]


class TestRun:
    @patch("fluid_build.cli.doctor._check_fluid_features")
    @patch("fluid_build.cli.doctor._print_feature_checks")
    def test_features_only_mode(self, mock_print, mock_check):
        from fluid_build.cli.doctor import run

        mock_check.return_value = (True, [{"check": "test", "status": "ok", "ok": True}])
        args = MagicMock()
        args.features_only = True
        args.verbose = False
        args.scope = None  # default to legacy doctor flow (no scoped checks)
        logger = MagicMock()

        result = run(args, logger)
        assert result == 0
        mock_print.assert_called_once()

    @patch("fluid_build.cli.doctor._check_fluid_features")
    @patch("fluid_build.cli.doctor._print_feature_checks")
    def test_features_only_returns_1_on_failure(self, _mock_print, mock_check):
        from fluid_build.cli.doctor import run

        mock_check.return_value = (False, [{"check": "test", "status": "fail", "ok": False}])
        args = MagicMock()
        args.features_only = True
        args.verbose = False
        args.scope = None  # default to legacy doctor flow (no scoped checks)
        logger = MagicMock()

        result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.doctor._check_copilot_readiness")
    @patch("fluid_build.cli.doctor._check_fluid_features")
    @patch("fluid_build.cli.doctor._print_doctor_summary")
    @patch("fluid_build.cli.doctor._print_copilot_readiness")
    @patch("fluid_build.cli.doctor._print_doctor_next_steps")
    @patch("fluid_build.cli.doctor._resolve_extended_diagnostic_script", return_value=None)
    def test_default_run_ignores_missing_diagnostic_script(
        self,
        mock_resolve_script,
        _mock_next_steps,
        mock_print_readiness,
        mock_print_summary,
        mock_check,
        mock_check_readiness,
    ):
        from fluid_build.cli.doctor import run
        from fluid_build.cli.forge_copilot_llm_providers import LlmReadinessCheck

        mock_check.return_value = (True, [])
        mock_check_readiness.return_value = LlmReadinessCheck(
            ready=True,
            provider="ollama",
            model="llama3.2",
            endpoint="http://localhost:11434/api/chat",
            auth_available=True,
        )
        args = MagicMock()
        args.features_only = False
        args.verbose = False
        args.extended = False
        args.scope = None  # default to legacy doctor flow (no scoped checks)
        args.out_dir = "/tmp/diag"
        logger = MagicMock()

        result = run(args, logger)
        assert result == 0
        mock_print_summary.assert_called_once()
        mock_print_readiness.assert_called_once()
        mock_resolve_script.assert_called_once()

    @patch("fluid_build.cli.doctor._check_copilot_readiness")
    @patch("fluid_build.cli.doctor._check_fluid_features")
    @patch("fluid_build.cli.doctor._print_copilot_readiness")
    @patch("fluid_build.cli.doctor._resolve_extended_diagnostic_script", return_value=None)
    def test_normal_run_prints_copilot_readiness(
        self,
        _mock_resolve_script,
        mock_print_readiness,
        mock_check_features,
        mock_check_readiness,
    ):
        from fluid_build.cli.doctor import run
        from fluid_build.cli.forge_copilot_llm_providers import LlmReadinessCheck

        mock_check_features.return_value = (True, [])
        mock_check_readiness.return_value = LlmReadinessCheck(
            ready=False,
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            auth_available=False,
        )
        args = MagicMock()
        args.features_only = False
        args.verbose = False
        args.extended = False
        args.scope = None  # default to legacy doctor flow (no scoped checks)
        args.out_dir = "/tmp/diag"
        logger = MagicMock()

        result = run(args, logger)

        assert result == 0
        mock_print_readiness.assert_called_once()

    @patch("fluid_build.cli.doctor._check_copilot_readiness")
    @patch("fluid_build.cli.doctor._check_fluid_features")
    @patch("fluid_build.cli.doctor._resolve_extended_diagnostic_script", return_value=None)
    def test_extended_missing_script_bubbles_up_as_error(
        self,
        mock_resolve_script,
        mock_check_features,
        mock_check_readiness,
    ):
        from fluid_build.cli._common import CLIError
        from fluid_build.cli.doctor import run
        from fluid_build.cli.forge_copilot_llm_providers import LlmReadinessCheck

        mock_check_features.return_value = (True, [])
        mock_check_readiness.return_value = LlmReadinessCheck(
            ready=True,
            provider="ollama",
            model="llama3.2",
            endpoint="http://localhost:11434/api/chat",
            auth_available=True,
        )

        args = MagicMock()
        args.features_only = False
        args.verbose = False
        args.extended = True
        args.scope = None  # default to legacy doctor flow
        args.out_dir = "/tmp/diag"

        with pytest.raises(CLIError) as exc:
            run(args, MagicMock())

        assert exc.value.message == "Extended diagnostics are not installed in this checkout."


class TestPrintDoctorNextStepsDualShape:
    """`_print_doctor_next_steps` must accept both shapes of
    ``LlmReadinessCheck.error`` — a bare string *and* an older structured
    error object with a ``.suggestions`` list. A drive-by refactor could
    narrow the accepted shape and silently lose the suggestion text in CI.
    """

    def _make_unready_check(self, error_value):
        from fluid_build.cli.forge_copilot_llm_providers import LlmReadinessCheck

        return LlmReadinessCheck(
            ready=False,
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            auth_available=False,
            error=error_value,
        )

    @patch("fluid_build.cli.doctor.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.doctor.cprint")
    def test_string_error_appended_verbatim(self, mock_cprint):
        """A plain-string error must end up as a bullet in the suggestions."""
        from fluid_build.cli.doctor import _print_doctor_next_steps

        _print_doctor_next_steps(
            feature_checks_ok=True,
            copilot_readiness=self._make_unready_check("Set OPENAI_API_KEY"),
        )

        printed = " ".join(str(call) for call in mock_cprint.call_args_list)
        assert "Set OPENAI_API_KEY" in printed

    @patch("fluid_build.cli.doctor.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.doctor.cprint")
    def test_structured_error_with_suggestions_is_expanded(self, mock_cprint):
        """A structured error (``.suggestions`` list) must expand to multiple bullets."""
        from fluid_build.cli.doctor import _print_doctor_next_steps

        class StructuredError:
            suggestions = ["Run `fluid login`", "Export FLUID_API_KEY"]

        _print_doctor_next_steps(
            feature_checks_ok=True,
            copilot_readiness=self._make_unready_check(StructuredError()),
        )

        printed = " ".join(str(call) for call in mock_cprint.call_args_list)
        assert "Run `fluid login`" in printed
        assert "Export FLUID_API_KEY" in printed

    @patch("fluid_build.cli.doctor.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.doctor.cprint")
    def test_none_error_produces_no_output(self, mock_cprint):
        """When the check is ready (error is None), next-steps stays silent."""
        from fluid_build.cli.doctor import _print_doctor_next_steps
        from fluid_build.cli.forge_copilot_llm_providers import LlmReadinessCheck

        ready = LlmReadinessCheck(
            ready=True,
            provider="ollama",
            model="llama3.2",
            endpoint="http://localhost:11434/api/chat",
            auth_available=True,
        )
        _print_doctor_next_steps(feature_checks_ok=True, copilot_readiness=ready)
        mock_cprint.assert_not_called()

    @patch("fluid_build.cli.doctor.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.doctor.cprint")
    def test_non_string_non_structured_error_ignored(self, mock_cprint):
        """A value that is neither a string nor has ``.suggestions`` must not
        raise — it's a defensive fallback for unexpected provider shapes."""
        from fluid_build.cli.doctor import _print_doctor_next_steps

        # An integer has neither .suggestions nor is a str; must no-op quietly.
        _print_doctor_next_steps(
            feature_checks_ok=True,
            copilot_readiness=self._make_unready_check(12345),
        )
        # No suggestions emitted → cprint not called (short-circuits at the
        # "if not suggestions: return" branch).
        mock_cprint.assert_not_called()
