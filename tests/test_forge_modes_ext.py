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

"""Tests for fluid_build.cli.forge_modes."""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.forge_modes import (
    run_domain_agent_mode,
    run_template_mode,
)

# ── helpers ────────────────────────────────────────────────────────────


def _args(**kwargs):
    defaults = {
        "non_interactive": True,
        "dry_run": False,
        "agent": None,
        "context": None,
        "template": None,
        "provider": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _logger():
    return logging.getLogger("test.forge_modes")


# ── run_domain_agent_mode ─────────────────────────────────────────────


class TestRunDomainAgentMode:
    def _make_agent_class(self, name="finance", domain="Finance", description="Finance agent"):
        agent = MagicMock()
        agent.name = name
        agent.domain = domain
        agent.description = description
        agent.analyze_requirements.return_value = {
            "recommended_template": "etl",
            "recommended_provider": "gcp",
            "security_requirements": [],
        }
        agent.create_project.return_value = True
        agent_class = MagicMock(return_value=agent)
        return agent_class

    def test_unknown_agent_name_returns_one(self):
        args = _args(agent="nonexistent")
        ai_agents = {"finance": self._make_agent_class()}

        result = run_domain_agent_mode(
            args,
            _logger(),
            ai_agents=ai_agents,
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
            context_error_cls=Exception,
            console_factory=None,
        )
        assert result == 1

    def test_no_agent_name_non_interactive_defaults_to_copilot(self):
        args = _args(agent=None, non_interactive=True)
        copilot_class = self._make_agent_class()
        copilot_class.return_value.create_project.return_value = True

        result = run_domain_agent_mode(
            args,
            _logger(),
            ai_agents={"copilot": copilot_class},
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
            context_error_cls=Exception,
            console_factory=None,
        )
        assert result == 0

    def test_successful_agent_returns_zero(self):
        agent_class = self._make_agent_class()
        args = _args(agent="finance", non_interactive=True, context=None)

        result = run_domain_agent_mode(
            args,
            _logger(),
            ai_agents={"finance": agent_class},
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
            context_error_cls=Exception,
            console_factory=None,
        )
        assert result == 0

    def test_create_project_returns_false_returns_one(self):
        agent_class = self._make_agent_class()
        agent_class.return_value.create_project.return_value = False
        args = _args(agent="finance", non_interactive=True, context=None)

        result = run_domain_agent_mode(
            args,
            _logger(),
            ai_agents={"finance": agent_class},
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
            context_error_cls=Exception,
            console_factory=None,
        )
        assert result == 1

    def test_exception_in_agent_returns_one(self):
        agent_class = MagicMock(side_effect=RuntimeError("agent error"))
        args = _args(agent="finance", non_interactive=True, context=None)

        result = run_domain_agent_mode(
            args,
            _logger(),
            ai_agents={"finance": agent_class},
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
            context_error_cls=Exception,
            console_factory=None,
        )
        assert result == 1

    def test_context_loading_failure_is_caught(self):
        """context_error_cls exception during load_context_fn should be caught."""
        agent_class = self._make_agent_class()
        args = _args(agent="finance", non_interactive=True, context="ctx.yaml")

        class MyContextError(Exception):
            pass

        def bad_load(*a, **kw):
            raise MyContextError("ctx error")

        result = run_domain_agent_mode(
            args,
            _logger(),
            ai_agents={"finance": agent_class},
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=bad_load,
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
            context_error_cls=MyContextError,
            console_factory=None,
        )
        # Should still succeed since context error is handled gracefully
        assert result == 0


# ── run_template_mode ─────────────────────────────────────────────────


class TestRunTemplateMode:
    def test_missing_template_returns_one(self):
        args = _args(template="nonexistent", provider="local", non_interactive=True)

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        mock_registry.list_available.return_value = ["starter", "analytics"]

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
                console_factory=None,
            )
        assert result == 1

    def test_dry_run_returns_zero(self, tmp_path):
        args = _args(template="starter", provider="local", dry_run=True, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(
            name="starter", description="Starter template"
        )
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template
        mock_registry.list_available.return_value = ["starter"]

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0

    def test_successful_template_creation(self, tmp_path):
        args = _args(template="starter", provider="local", dry_run=False, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(
            name="starter", description="Starter template"
        )
        mock_template.generate_contract.return_value = {}
        mock_template.generate_structure.return_value = {}
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template
        mock_registry.list_available.return_value = ["starter"]

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
            patch("builtins.open", MagicMock()),
            patch("yaml.dump"),
            patch("fluid_build.cli.forge_modes.success"),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0

    def test_exception_returns_one(self):
        args = _args(template="starter", provider="local", non_interactive=True)

        # get_target_directory_fn raising causes generic exception handler to catch it
        result = run_template_mode(
            args,
            _logger(),
            get_target_directory_fn=MagicMock(side_effect=RuntimeError("oops")),
            console_factory=None,
        )
        assert result == 1


# ── run_ai_copilot_mode ───────────────────────────────────────────────


class TestRunAiCopilotMode:
    """Test the run_ai_copilot_mode function (lines 50-171)."""

    def _make_copilot_class(self, success=True):
        copilot = MagicMock()
        copilot.prepare_runtime_inputs.return_value = {
            "llm_config": {"provider": "openai"},
            "discovery_report": {},
            "capability_matrix": {},
            "project_memory": {},
            "capability_warnings": [],
        }
        copilot.create_project.return_value = success
        copilot_class = MagicMock(return_value=copilot)
        return copilot_class

    def test_non_interactive_success(self):
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = _args(non_interactive=True, dry_run=False)
        args.provider = None
        args.template = None
        args.domain = None
        args.context = None
        args.target_dir = None
        args.llm_provider = None
        args.llm_model = None
        args.llm_endpoint = None

        copilot_class = self._make_copilot_class(success=True)

        def _get_arg(a, name, default=None):
            return getattr(a, name, default)

        with patch(
            "fluid_build.cli.forge_modes.normalize_copilot_context", side_effect=lambda x: x
        ):
            with patch("fluid_build.cli.forge_modes.build_interview_summary_fn", create=True):
                result = run_ai_copilot_mode(
                    args,
                    _logger(),
                    copilot_class=copilot_class,
                    get_cli_arg_fn=_get_arg,
                    load_context_fn=MagicMock(),
                    get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                    context_error_cls=Exception,
                    build_interview_summary_fn=MagicMock(return_value={}),
                    console_factory=None,
                )
        assert result == 0

    def test_non_interactive_create_project_fails(self):
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = _args(non_interactive=True, dry_run=False)
        args.provider = None
        args.template = None
        args.domain = None
        args.context = None
        args.target_dir = None
        args.llm_provider = None
        args.llm_model = None
        args.llm_endpoint = None
        # Slice UX-H: `--scaffold` forces the legacy
        # CopilotAgent.create_project path so this test's assertion
        # (success=False → rc == 1) still exercises the engine code.
        # `--no-ci` suppresses the interactive auto-CI hook which
        # would otherwise prompt for a provider and break under
        # pytest's stdin capture.
        args.scaffold = "etl_pipeline"
        args.no_ci = True

        copilot_class = self._make_copilot_class(success=False)

        def _get_arg(a, name, default=None):
            return getattr(a, name, default)

        with patch(
            "fluid_build.cli.forge_modes.normalize_copilot_context", side_effect=lambda x: x
        ):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=_get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=None,
            )
        assert result == 1

    def test_exception_in_copilot_returns_one(self):
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = _args(non_interactive=True)
        args.provider = None
        args.template = None
        args.domain = None
        args.context = None
        args.target_dir = None

        def _get_arg(a, name, default=None):
            return getattr(a, name, default)

        result = run_ai_copilot_mode(
            args,
            _logger(),
            copilot_class=MagicMock(side_effect=RuntimeError("copilot init failed")),
            get_cli_arg_fn=_get_arg,
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
            context_error_cls=Exception,
            build_interview_summary_fn=MagicMock(return_value={}),
            console_factory=None,
        )
        assert result == 1

    def test_with_provider_template_domain_in_context(self):
        """Lines 108-117: provider/template/domain set in context from args."""
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = _args(non_interactive=True, dry_run=False)
        args.provider = "gcp"
        args.template = "analytics"
        args.domain = "finance"
        args.context = None
        args.target_dir = "/tmp/my-dir"
        args.llm_provider = None
        args.llm_model = None
        args.llm_endpoint = None

        copilot_class = self._make_copilot_class(success=True)

        def _get_arg(a, name, default=None):
            return getattr(a, name, default)

        with patch(
            "fluid_build.cli.forge_modes.normalize_copilot_context", side_effect=lambda x: x
        ):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=_get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=None,
            )
        assert result == 0

    def test_context_loaded_successfully(self):
        """Lines 88-96: context_arg provided and loaded successfully."""
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = _args(non_interactive=True, dry_run=False)
        args.provider = None
        args.template = None
        args.domain = None
        args.context = "ctx.yaml"
        args.target_dir = None
        args.llm_provider = None
        args.llm_model = None
        args.llm_endpoint = None

        copilot_class = self._make_copilot_class(success=True)

        def _get_arg(a, name, default=None):
            return getattr(a, name, default)

        mock_load = MagicMock(return_value={"extra_key": "extra_val"})

        with patch(
            "fluid_build.cli.forge_modes.normalize_copilot_context", side_effect=lambda x: x
        ):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=_get_arg,
                load_context_fn=mock_load,
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=None,
            )
        assert result == 0

    def test_context_error_is_caught(self):
        """Lines 97-106: context_error_cls exception is caught without crashing."""
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = _args(non_interactive=True, dry_run=False)
        args.provider = None
        args.template = None
        args.domain = None
        args.context = "bad_ctx.yaml"
        args.target_dir = None
        args.llm_provider = None
        args.llm_model = None
        args.llm_endpoint = None

        copilot_class = self._make_copilot_class(success=True)

        class ContextErr(Exception):
            pass

        def _get_arg(a, name, default=None):
            return getattr(a, name, default)

        def bad_load(*a, **kw):
            raise ContextErr("bad context")

        with patch(
            "fluid_build.cli.forge_modes.normalize_copilot_context", side_effect=lambda x: x
        ):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=_get_arg,
                load_context_fn=bad_load,
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=ContextErr,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=None,
            )
        assert result == 0


class TestRunAiCopilotModeRecovery:
    def _get_arg(self, args, name, default=None):
        return getattr(args, name, default)

    def _interactive_args(self):
        args = _args(non_interactive=False, dry_run=False)
        args.provider = None
        args.template = None
        args.domain = None
        args.context = None
        args.target_dir = None
        args.llm_provider = None
        args.llm_model = None
        args.llm_endpoint = None
        args._enable_copilot_recovery = True
        return args

    def test_recovery_can_collect_session_only_llm_config(self):
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = self._interactive_args()
        console = MagicMock()
        copilot = MagicMock()
        prepared_inputs = {}

        def _prepare_runtime_inputs(options):
            prepared_inputs.update(dict(options))
            return {
                "llm_config": MagicMock(),
                "discovery_report": {},
                "capability_matrix": {},
                "project_memory": {},
                "capability_warnings": [],
            }

        copilot.prepare_runtime_inputs.side_effect = _prepare_runtime_inputs
        copilot.create_project.return_value = True
        copilot_class = MagicMock(return_value=copilot)
        mock_state = MagicMock()
        mock_state.finalize.return_value = {
            "project_goal": "test project",
            "data_sources": "csv files",
            "use_case": "analytics",
            "complexity": "intermediate",
        }

        readiness = MagicMock(
            ready=False,
            provider="openai",
            error=CopilotGenerationError(
                "copilot_missing_llm_api_key",
                "No API key was configured for the openai copilot adapter.",
                suggestions=["Set OPENAI_API_KEY"],
            ),
        )
        ask_dialog_question_fn = MagicMock(return_value=MagicMock(value="openai"))

        with (
            patch("fluid_build.cli.forge_modes.ask_confirmation", return_value=True),
            patch(
                "fluid_build.cli.forge_modes.run_adaptive_copilot_interview",
                return_value=mock_state,
            ),
            patch("fluid_build.cli.forge_modes.normalize_copilot_context", side_effect=lambda x: x),
        ):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=self._get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=lambda: console,
                llm_readiness_fn=MagicMock(return_value=readiness),
                ask_dialog_question_fn=ask_dialog_question_fn,
                ask_secret_text_fn=MagicMock(return_value="session-key"),
                route_mode_fn=MagicMock(return_value=99),
                fallback_mode_choices=[{"label": "Template", "value": "template"}],
            )

        assert result == 0
        assert prepared_inputs["llm_config"].provider == "openai"
        assert prepared_inputs["llm_config"].api_key == "session-key"

    def test_recovery_can_fallback_to_other_mode(self):
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = self._interactive_args()
        console = MagicMock()
        copilot_class = MagicMock(return_value=MagicMock())
        readiness = MagicMock(
            ready=False,
            provider="openai",
            error=CopilotGenerationError(
                "copilot_missing_llm_api_key",
                "No API key was configured for the openai copilot adapter.",
                suggestions=["Set OPENAI_API_KEY"],
            ),
        )
        route_mode_fn = MagicMock(return_value=7)

        with patch("fluid_build.cli.forge_modes.ask_confirmation", return_value=False):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=self._get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=lambda: console,
                llm_readiness_fn=MagicMock(return_value=readiness),
                ask_dialog_question_fn=MagicMock(return_value=MagicMock(value="template")),
                ask_secret_text_fn=MagicMock(),
                route_mode_fn=route_mode_fn,
                fallback_mode_choices=[{"label": "Template", "value": "template"}],
            )

        assert result == 7
        route_mode_fn.assert_called_once_with("template")

    def test_explicit_copilot_error_shows_friendly_message(self):
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = self._interactive_args()
        args._enable_copilot_recovery = False
        console = MagicMock()
        copilot = MagicMock()
        copilot.prepare_runtime_inputs.side_effect = CopilotGenerationError(
            "copilot_missing_llm_api_key",
            "No API key was configured for the openai copilot adapter.",
            suggestions=["Set OPENAI_API_KEY"],
        )

        result = run_ai_copilot_mode(
            args,
            _logger(),
            copilot_class=MagicMock(return_value=copilot),
            get_cli_arg_fn=self._get_arg,
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
            context_error_cls=Exception,
            build_interview_summary_fn=MagicMock(return_value={}),
            console_factory=lambda: console,
        )

        assert result == 1
        printed = "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)
        assert "No API key was configured for the openai copilot adapter." in printed
        assert "copilot_missing_llm_api_key" not in printed

    def test_ready_recovery_path_starts_copilot_without_mode_chooser(self):
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = self._interactive_args()
        console = MagicMock()
        copilot = MagicMock()
        copilot.prepare_runtime_inputs.return_value = {
            "llm_config": MagicMock(),
            "discovery_report": {},
            "capability_matrix": {},
            "project_memory": {},
            "capability_warnings": [],
        }
        copilot.create_project.return_value = True
        mock_state = MagicMock()
        mock_state.finalize.return_value = {
            "project_goal": "test project",
            "data_sources": "csv files",
            "use_case": "analytics",
            "complexity": "intermediate",
        }

        with (
            patch(
                "fluid_build.cli.forge_modes.run_adaptive_copilot_interview",
                return_value=mock_state,
            ),
            patch("fluid_build.cli.forge_modes.normalize_copilot_context", side_effect=lambda x: x),
            patch("fluid_build.cli.forge_modes.print_copilot_intro_panel") as mock_intro,
            patch("fluid_build.cli.forge_modes.print_welcome_panel") as mock_welcome,
        ):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=MagicMock(return_value=copilot),
                get_cli_arg_fn=self._get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=lambda: console,
                llm_readiness_fn=MagicMock(return_value=MagicMock(ready=True)),
            )

        assert result == 0
        mock_intro.assert_called_once()
        mock_welcome.assert_not_called()

    def test_recovery_returns_1_when_setup_skipped_and_no_fallback(self):
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = self._interactive_args()
        console = MagicMock()
        copilot_class = MagicMock(return_value=MagicMock())
        readiness = MagicMock(
            ready=False,
            provider="openai",
            error=CopilotGenerationError(
                "copilot_missing_llm_api_key",
                "No API key was configured for the openai copilot adapter.",
                suggestions=["Set OPENAI_API_KEY"],
            ),
        )

        with patch("fluid_build.cli.forge_modes.ask_confirmation", return_value=False):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=self._get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=lambda: console,
                llm_readiness_fn=MagicMock(return_value=readiness),
                ask_dialog_question_fn=MagicMock(return_value=MagicMock(value=None)),
                ask_secret_text_fn=MagicMock(),
                route_mode_fn=None,
                fallback_mode_choices=[],
            )

        assert result == 1

    def test_recovery_returns_1_when_session_config_returns_none(self):
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = self._interactive_args()
        console = MagicMock()
        copilot_class = MagicMock(return_value=MagicMock())
        readiness = MagicMock(
            ready=False,
            provider="openai",
            error=CopilotGenerationError(
                "copilot_missing_llm_api_key",
                "No API key was configured.",
                suggestions=["Set OPENAI_API_KEY"],
            ),
        )

        with patch("fluid_build.cli.forge_modes.ask_confirmation", return_value=True):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=self._get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=lambda: console,
                llm_readiness_fn=MagicMock(return_value=readiness),
                ask_dialog_question_fn=MagicMock(return_value=MagicMock(value="openai")),
                ask_secret_text_fn=MagicMock(return_value=None),
                route_mode_fn=None,
                fallback_mode_choices=[],
            )

        assert result == 1

    def test_recovery_works_without_console(self):
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
        from fluid_build.cli.forge_modes import run_ai_copilot_mode

        args = self._interactive_args()
        copilot_class = MagicMock(return_value=MagicMock())
        readiness = MagicMock(
            ready=False,
            provider="openai",
            error=CopilotGenerationError(
                "copilot_missing_llm_api_key",
                "No API key.",
                suggestions=[],
            ),
        )

        with patch("fluid_build.cli.forge_modes.ask_confirmation", return_value=False):
            result = run_ai_copilot_mode(
                args,
                _logger(),
                copilot_class=copilot_class,
                get_cli_arg_fn=self._get_arg,
                load_context_fn=MagicMock(),
                get_target_directory_fn=MagicMock(return_value=Path("/tmp/proj")),
                context_error_cls=Exception,
                build_interview_summary_fn=MagicMock(return_value={}),
                console_factory=None,
                llm_readiness_fn=MagicMock(return_value=readiness),
                ask_dialog_question_fn=MagicMock(return_value=MagicMock(value=None)),
                ask_secret_text_fn=MagicMock(),
                route_mode_fn=None,
                fallback_mode_choices=[],
            )

        assert result == 1


# ── run_domain_agent_mode – additional branches ───────────────────────


class TestRunDomainAgentModeAdditional:
    """Cover lines 193-302 not yet hit by the base test class."""

    def _make_agent_class(self):
        agent = MagicMock()
        agent.name = "analytics"
        agent.domain = "analytics"
        agent.description = "Analytics agent"
        agent.analyze_requirements.return_value = {
            "recommended_template": "etl",
            "recommended_provider": "gcp",
            "security_requirements": [],
        }
        agent.create_project.return_value = True
        return MagicMock(return_value=agent)

    def test_analyze_requirements_with_security(self):
        """Lines 285-289: security_requirements shown (console path skipped)."""
        agent_class = self._make_agent_class()
        agent = agent_class.return_value
        agent.analyze_requirements.return_value = {
            "recommended_template": "secure",
            "recommended_provider": "aws",
            "security_requirements": ["mfa", "encryption", "audit-logs"],
        }
        args = _args(agent="analytics", non_interactive=True, context=None)

        result = run_domain_agent_mode(
            args,
            _logger(),
            ai_agents={"analytics": agent_class},
            gather_context_fn=MagicMock(return_value={}),
            load_context_fn=MagicMock(),
            get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
            context_error_cls=Exception,
            console_factory=None,
        )
        assert result == 0

    def test_context_dict_loaded_with_valid_context(self):
        """Lines 238-257: valid context dict loaded."""
        agent_class = self._make_agent_class()
        args = _args(agent="analytics", non_interactive=True, context="ctx.yaml")

        valid_context = {"project_goal": "Test", "data_sources": "DB"}

        def _load(*a, **kw):
            return valid_context

        with (
            patch(
                "fluid_build.cli.forge_validation.validate_context_dict", return_value=(True, None)
            )
            if False
            else patch(
                "fluid_build.cli.forge_modes.run_domain_agent_mode",
                wraps=run_domain_agent_mode,
            )
        ):
            # We just exercise the real function with a load that succeeds
            with patch(
                "fluid_build.cli.forge_validation.validate_context_dict",
                return_value=(True, None),
            ):
                result = run_domain_agent_mode(
                    args,
                    _logger(),
                    ai_agents={"analytics": agent_class},
                    gather_context_fn=MagicMock(return_value={}),
                    load_context_fn=_load,
                    get_target_directory_fn=MagicMock(return_value=Path("/tmp/out")),
                    context_error_cls=Exception,
                    console_factory=None,
                )
        assert result == 0


# ── run_template_mode – additional branches ───────────────────────────


class TestRunTemplateModeAdditional:
    """Cover lines 318-379, 382-397."""

    def test_template_creates_directory_structure(self, tmp_path):
        """Lines 365-379: generate_structure returns dir entries."""
        from fluid_build.cli.forge_modes import run_template_mode

        args = _args(template="starter", provider="local", dry_run=False, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(name="starter", description="A starter")
        mock_template.generate_contract.return_value = {}
        # One directory entry and one file entry
        mock_template.generate_structure.return_value = {
            "src/": None,
            "src/main.py": "print('hello')",
        }
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
            patch("builtins.open", MagicMock()),
            patch("yaml.dump"),
            patch("fluid_build.cli.forge_modes.success"),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0

    def test_template_readme_attribute_error_is_swallowed(self, tmp_path):
        """Lines 376-378: AttributeError on _create_readme is handled."""
        from fluid_build.cli.forge_modes import run_template_mode

        args = _args(template="starter", provider="local", dry_run=False, non_interactive=True)
        target = tmp_path / "starter-project"

        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(name="starter", description="Starter")
        mock_template.generate_contract.return_value = {}
        mock_template.generate_structure.return_value = {}
        mock_template._create_readme.side_effect = AttributeError("no readme")
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_template

        with (
            patch("fluid_build.forge.core.registry.template_registry", mock_registry),
            patch("fluid_build.forge.core.engine.ForgeEngine", MagicMock()),
            patch("fluid_build.forge.core.engine.GenerationContext", MagicMock()),
            patch("builtins.open", MagicMock()),
            patch("yaml.dump"),
            patch("fluid_build.cli.forge_modes.success"),
        ):
            result = run_template_mode(
                args,
                _logger(),
                get_target_directory_fn=MagicMock(return_value=target),
                console_factory=None,
            )
        assert result == 0


# ── _create_session_llm_config (API-key-first flow) ─────────────────


from fluid_build.cli.forge_modes import _create_session_llm_config


class TestCreateSessionLlmConfig:
    """Tests for the API-key-first onboarding wizard."""

    def _stub_dialog(self, *values):
        """Return a callable that returns successive DialogQuestionResult stubs."""
        from fluid_build.cli.forge_dialogs import DialogQuestionResult

        results = iter(values)

        def _fn(console, question):
            val = next(results)
            return DialogQuestionResult(value=val, raw_input=str(val or ""))

        return _fn

    def test_anthropic_key_auto_detected(self):
        console = MagicMock()
        config = _create_session_llm_config(
            console,
            ask_secret_text_fn=lambda *a, **kw: "sk-ant-api03-test123",
        )
        assert config is not None
        assert config.provider == "anthropic"
        assert config.api_key == "sk-ant-api03-test123"
        assert config.model == "claude-sonnet-4-5-20250514"

    def test_openai_key_auto_detected(self):
        from fluid_build.cli.forge_copilot_llm_providers import get_catalog_default

        console = MagicMock()
        config = _create_session_llm_config(
            console,
            ask_secret_text_fn=lambda *a, **kw: "sk-proj-test456",
        )
        assert config is not None
        assert config.provider == "openai"
        # Model comes from the catalog flagship, not a hardcoded string.
        expected = get_catalog_default("openai")
        assert config.model == expected

    def test_gemini_key_auto_detected(self):
        from fluid_build.cli.forge_copilot_llm_providers import get_catalog_default

        console = MagicMock()
        key = "AIzaSyD" + "x" * 30
        config = _create_session_llm_config(
            console,
            ask_secret_text_fn=lambda *a, **kw: key,
        )
        assert config is not None
        assert config.provider == "gemini"
        expected = get_catalog_default("gemini")
        assert config.model == expected

    def test_ollama_shortcut(self):
        console = MagicMock()
        config = _create_session_llm_config(
            console,
            ask_secret_text_fn=lambda *a, **kw: "ollama",
        )
        assert config is not None
        assert config.provider == "ollama"
        assert config.api_key is None

    def test_unrecognized_key_asks_provider(self):
        console = MagicMock()
        config = _create_session_llm_config(
            console,
            ask_secret_text_fn=lambda *a, **kw: "unknown-key-format",
            ask_dialog_question_fn=self._stub_dialog("anthropic"),
        )
        assert config is not None
        assert config.provider == "anthropic"
        assert config.api_key == "unknown-key-format"

    def test_empty_key_returns_none(self):
        console = MagicMock()
        config = _create_session_llm_config(
            console,
            ask_secret_text_fn=lambda *a, **kw: "",
        )
        assert config is None


# ── print_interview_phase ──────────────────────────────────────────────


class TestPrintInterviewPhase:
    """Regression tests for the interview phase breadcrumb helper."""

    def test_prints_breadcrumb_with_rich(self):
        from fluid_build.cli.forge_ui import print_interview_phase

        console = MagicMock()
        with patch("fluid_build.cli.forge_ui.RICH_AVAILABLE", True):
            print_interview_phase(
                console, phase=1, total=3, label="Understanding your project"
            )
        console.print.assert_called()
        printed = " ".join(str(c) for c in console.print.call_args_list)
        assert "Phase 1/3" in printed
        assert "Understanding your project" in printed

    def test_prints_plain_breadcrumb_without_rich(self):
        from fluid_build.cli.forge_ui import print_interview_phase

        console = MagicMock()
        with patch("fluid_build.cli.forge_ui.RICH_AVAILABLE", False):
            print_interview_phase(console, phase=2, total=3, label="Clarifying details")
        console.print.assert_called()
        printed = " ".join(str(c) for c in console.print.call_args_list)
        assert "Phase 2/3" in printed
        assert "Clarifying details" in printed

    def test_no_console_is_noop(self):
        from fluid_build.cli.forge_ui import print_interview_phase

        # Should not raise even if console is None.
        print_interview_phase(None, phase=1, total=3, label="x")
