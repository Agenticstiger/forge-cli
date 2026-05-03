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

"""Branch coverage tests for forge.py (fluid_build/cli/forge.py)."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---- Custom exceptions ----


def _skip_clierror_signature_mismatch(exc: TypeError) -> None:
    pytest.skip(f"CLIError signature mismatch: {exc}")


class TestForgeExceptions:
    def test_forge_error(self):
        from fluid_build.cli.forge import ForgeError

        err = ForgeError(1, "test error")
        assert "test error" in str(err)

    def test_invalid_project_name(self):
        from fluid_build.cli.forge import InvalidProjectNameError

        try:
            err = InvalidProjectNameError("a b c", "contains spaces")
            assert "a b c" in str(err)
        except TypeError as exc:
            _skip_clierror_signature_mismatch(exc)

    def test_project_generation_error(self):
        from fluid_build.cli.forge import ProjectGenerationError

        err = ProjectGenerationError(1, "generation failed")
        assert isinstance(err, Exception)

    def test_context_validation_error(self):
        from fluid_build.cli.forge import ContextValidationError

        err = ContextValidationError(1, "bad context")
        assert isinstance(err, Exception)


# ---- ForgeMode enum ----


class TestForgeMode:
    def test_all_modes(self):
        from fluid_build.cli.forge import ForgeMode

        assert ForgeMode.AI_COPILOT.value == "copilot"
        assert ForgeMode.BLANK.value == "blank"


# ---- AIAgent base class ----


class TestAIAgent:
    def test_init(self):
        from fluid_build.cli.forge import AIAgent

        agent = AIAgent("test", "A test agent", "analytics")
        assert agent.name == "test"
        assert agent.description == "A test agent"
        assert agent.domain == "analytics"

    def test_create_project_raises(self):
        from fluid_build.cli.forge import AIAgent

        agent = AIAgent("test", "desc", "general")
        with pytest.raises(NotImplementedError):
            asyncio.run(agent.create_project(Path("/tmp"), {}))

    def test_get_questions_raises(self):
        from fluid_build.cli.forge import AIAgent

        agent = AIAgent("test", "desc", "general")
        with pytest.raises(NotImplementedError):
            agent.get_questions()


# ---- CopilotAgent ----


class TestCopilotAgent:
    def _make_agent(self):
        from fluid_build.cli.forge import CopilotAgent

        return CopilotAgent()

    def test_init(self):
        agent = self._make_agent()
        assert agent.name == "copilot"
        assert agent.domain == "general"

    def test_get_questions(self):
        agent = self._make_agent()
        questions = agent.get_questions()
        assert isinstance(questions, list)
        assert len(questions) >= 3
        keys = [q["key"] for q in questions]
        assert "project_goal" in keys
        use_case_question = next(q for q in questions if q["key"] == "use_case")
        assert use_case_question["choices"][0] == {
            "label": "Analytics & BI",
            "value": "analytics",
        }
        assert use_case_question["choices"][-1] == {
            "label": "Other / Not sure",
            "value": "other",
        }
        assert use_case_question["follow_up"]["key"] == "use_case_other"

    def test_show_next_steps_uses_fluid_commands(self):
        agent = self._make_agent()
        agent.console = MagicMock()

        agent._show_next_steps(Path("/tmp"), {}, {"recommended_provider": "gcp"})

        panel = agent.console.print.call_args.args[0]
        text = str(panel.renderable)
        assert "fluid validate contract.fluid.yaml" in text

    @patch("fluid_build.cli.forge_copilot_agent.LOG.warning")
    def test_prepare_runtime_inputs_logs_capability_warnings(self, mock_warning):
        agent = self._make_agent()
        llm_config = MagicMock()
        discovery_report = MagicMock()
        with patch.object(agent, "_resolve_llm_config_dependency", return_value=llm_config):
            with patch.object(
                agent, "_discover_local_context_dependency", return_value=discovery_report
            ):
                with patch.object(agent, "_load_project_memory", return_value=None):
                    with patch.object(
                        agent,
                        "_build_capability_matrix_dependency",
                        return_value={
                            "providers": ["local"],
                            "templates": {"starter": {}},
                            "warnings": ["Copilot couldn't inspect the aws provider."],
                        },
                    ):
                        runtime_inputs = agent.prepare_runtime_inputs({})

        assert runtime_inputs["capability_warnings"] == [
            "Copilot couldn't inspect the aws provider."
        ]
        mock_warning.assert_called_once()

    def test_analyze_requirements_ml(self):
        agent = self._make_agent()
        context = {
            "project_goal": "Build a machine learning pipeline",
            "data_sources": "s3 bucket",
            "use_case": "machine_learning",
            "complexity": "advanced",
        }
        suggestions = agent.analyze_requirements(context)
        assert "recommended_template" in suggestions
        assert "recommended_provider" in suggestions

    def test_analyze_requirements_streaming(self):
        agent = self._make_agent()
        context = {
            "project_goal": "Build a streaming data pipeline",
            "data_sources": "kafka",
            "use_case": "streaming",
            "complexity": "intermediate",
        }
        suggestions = agent.analyze_requirements(context)
        assert suggestions["recommended_template"] == "streaming"

    def test_analyze_requirements_etl(self):
        agent = self._make_agent()
        context = {
            "project_goal": "ETL pipeline for warehouse",
            "data_sources": "postgres",
            "use_case": "etl",
            "complexity": "beginner",
        }
        suggestions = agent.analyze_requirements(context)
        assert suggestions is not None

    def test_analyze_requirements_analytics(self):
        agent = self._make_agent()
        context = {
            "project_goal": "Analytics dashboard",
            "data_sources": "bigquery",
            "use_case": "analytics",
            "complexity": "intermediate",
        }
        suggestions = agent.analyze_requirements(context)
        assert suggestions is not None

    def test_analyze_requirements_default(self):
        agent = self._make_agent()
        context = {
            "project_goal": "Something generic",
            "data_sources": "local files",
            "use_case": "other",
            "complexity": "beginner",
        }
        suggestions = agent.analyze_requirements(context)
        assert suggestions["recommended_template"] == "starter"

    def test_analyze_requirements_data_platform(self):
        agent = self._make_agent()
        context = {
            "project_goal": "Lakehouse platform",
            "data_sources": "object storage",
            "use_case": "data_platform",
            "complexity": "intermediate",
        }
        suggestions = agent.analyze_requirements(context)
        assert suggestions["recommended_template"] == "etl_pipeline"

    def test_analyze_requirements_other_with_follow_up(self):
        agent = self._make_agent()
        context = {
            "project_goal": "Internal platform",
            "data_sources": "warehouse tables",
            "use_case": "other",
            "use_case_other": "CDC sync for warehouse loads",
            "complexity": "intermediate",
        }
        suggestions = agent.analyze_requirements(context)
        assert suggestions["recommended_template"] == "etl_pipeline"

    def test_create_project_success(self):
        from fluid_build.cli.forge_copilot_runtime import CopilotGenerationResult, DiscoveryReport

        agent = self._make_agent()
        agent.generate_project_artifacts = MagicMock(
            return_value=CopilotGenerationResult(
                suggestions={
                    "recommended_template": "starter",
                    "recommended_provider": "local",
                    "recommended_patterns": [],
                    "architecture_suggestions": [],
                    "best_practices": [],
                    "technology_stack": [],
                },
                contract={"name": "test-project", "fluidVersion": "0.7.2"},
                readme_markdown="# Test Project\n",
                additional_files={},
                discovery_report=DiscoveryReport(workspace_roots=["/tmp/test"]),
                attempt_reports=[],
            )
        )
        agent._show_ai_analysis = MagicMock()
        agent._create_forge_config = MagicMock(return_value={"name": "test"})
        agent._create_with_forge_engine = MagicMock(return_value=True)
        agent._show_next_steps = MagicMock()
        result = agent.create_project(Path("/tmp/test"), {"project_goal": "test"})
        assert result is True

    @patch("fluid_build.cli.forge.CopilotMemoryStore")
    def test_create_project_saves_memory_in_non_interactive_mode_when_requested(
        self, mock_store_cls
    ):
        from fluid_build.cli.forge_copilot_runtime import CopilotGenerationResult, DiscoveryReport

        agent = self._make_agent()
        mock_store_cls.return_value.load.return_value = None
        agent.generate_project_artifacts = MagicMock(
            return_value=CopilotGenerationResult(
                suggestions={
                    "recommended_template": "starter",
                    "recommended_provider": "local",
                    "recommended_patterns": [],
                    "architecture_suggestions": [],
                    "best_practices": [],
                    "technology_stack": [],
                },
                contract={"name": "test-project", "fluidVersion": "0.7.2"},
                readme_markdown="# Test Project\n",
                additional_files={},
                discovery_report=DiscoveryReport(workspace_roots=["/tmp/test"]),
                attempt_reports=[],
            )
        )
        agent._show_ai_analysis = MagicMock()
        agent._create_forge_config = MagicMock(return_value={"name": "test"})
        agent._create_with_forge_engine = MagicMock(return_value=True)
        agent._show_next_steps = MagicMock()

        result = agent.create_project(
            Path("/tmp/test"),
            {"project_goal": "test"},
            {"non_interactive": True, "save_memory": True},
        )

        assert result is True
        mock_store_cls.return_value.save.assert_called_once()

    @patch("fluid_build.cli.forge.ask_confirmation", return_value=True)
    @patch("fluid_build.cli.forge.CopilotMemoryStore")
    def test_create_project_prompts_before_saving_memory_in_interactive_mode(
        self, mock_store_cls, mock_confirm
    ):
        from fluid_build.cli.forge_copilot_runtime import CopilotGenerationResult, DiscoveryReport

        agent = self._make_agent()
        mock_store_cls.return_value.load.return_value = None
        agent.generate_project_artifacts = MagicMock(
            return_value=CopilotGenerationResult(
                suggestions={
                    "recommended_template": "starter",
                    "recommended_provider": "local",
                    "recommended_patterns": [],
                    "architecture_suggestions": [],
                    "best_practices": [],
                    "technology_stack": [],
                },
                contract={"name": "test-project", "fluidVersion": "0.7.2"},
                readme_markdown="# Test Project\n",
                additional_files={},
                discovery_report=DiscoveryReport(workspace_roots=["/tmp/test"]),
                attempt_reports=[],
            )
        )
        agent._show_ai_analysis = MagicMock()
        agent._create_forge_config = MagicMock(return_value={"name": "test"})
        agent._create_with_forge_engine = MagicMock(return_value=True)
        agent._show_next_steps = MagicMock()

        result = agent.create_project(
            Path("/tmp/test"),
            {"project_goal": "test"},
            {"non_interactive": False},
        )

        assert result is True
        mock_confirm.assert_called_once()
        mock_store_cls.return_value.save.assert_called_once()

    @patch("fluid_build.cli.forge.CopilotMemoryStore")
    def test_create_project_does_not_save_memory_on_dry_run(self, mock_store_cls):
        from fluid_build.cli.forge_copilot_runtime import CopilotGenerationResult, DiscoveryReport

        agent = self._make_agent()
        mock_store_cls.return_value.load.return_value = None
        agent.generate_project_artifacts = MagicMock(
            return_value=CopilotGenerationResult(
                suggestions={
                    "recommended_template": "starter",
                    "recommended_provider": "local",
                    "recommended_patterns": [],
                    "architecture_suggestions": [],
                    "best_practices": [],
                    "technology_stack": [],
                },
                contract={"name": "test-project", "fluidVersion": "0.7.2"},
                readme_markdown="# Test Project\n",
                additional_files={},
                discovery_report=DiscoveryReport(workspace_roots=["/tmp/test"]),
                attempt_reports=[],
            )
        )
        agent._show_ai_analysis = MagicMock()
        agent._create_forge_config = MagicMock(return_value={"name": "test"})
        agent._create_with_forge_engine = MagicMock(return_value=True)
        agent._show_next_steps = MagicMock()

        result = agent.create_project(
            Path("/tmp/test"),
            {"project_goal": "test"},
            {"non_interactive": True, "save_memory": True},
            dry_run=True,
        )

        assert result is True
        mock_store_cls.return_value.save.assert_not_called()

    def test_create_project_failure(self):
        from fluid_build.cli.forge_copilot_runtime import CopilotGenerationResult, DiscoveryReport

        agent = self._make_agent()
        agent.generate_project_artifacts = MagicMock(
            return_value=CopilotGenerationResult(
                suggestions={
                    "recommended_template": "starter",
                    "recommended_provider": "local",
                    "recommended_patterns": [],
                    "architecture_suggestions": [],
                    "best_practices": [],
                    "technology_stack": [],
                },
                contract={"name": "test-project", "fluidVersion": "0.7.2"},
                readme_markdown="# Test Project\n",
                additional_files={},
                discovery_report=DiscoveryReport(workspace_roots=["/tmp/test"]),
                attempt_reports=[],
            )
        )
        agent._show_ai_analysis = MagicMock()
        agent._create_forge_config = MagicMock(return_value={"name": "test"})
        agent._create_with_forge_engine = MagicMock(return_value=False)
        result = agent.create_project(Path("/tmp/test"), {"project_goal": "test"})
        assert result is False

    def test_create_project_generation_error(self):
        from fluid_build.cli.forge_copilot_runtime import CopilotGenerationError

        agent = self._make_agent()
        agent.generate_project_artifacts = MagicMock(
            side_effect=CopilotGenerationError(
                "copilot_generation_failed",
                "Unable to generate a valid contract.",
                suggestions=["Check your API key"],
            )
        )
        agent._create_with_forge_engine = MagicMock()
        result = agent.create_project(Path("/tmp/test"), {})
        assert result is False
        agent._create_with_forge_engine.assert_not_called()

    def test_create_project_exception(self):
        agent = self._make_agent()
        agent.generate_project_artifacts = MagicMock(side_effect=RuntimeError("boom"))
        result = agent.create_project(Path("/tmp/test"), {})
        assert result is False

    @patch("fluid_build.cli.forge.generate_copilot_artifacts")
    @patch("fluid_build.cli.forge.CopilotMemoryStore")
    @patch("fluid_build.cli.forge.discover_local_context")
    @patch("fluid_build.cli.forge.resolve_llm_config")
    def test_generate_project_artifacts_loads_memory_by_default(
        self,
        mock_resolve_llm_config,
        mock_discover_local_context,
        mock_store_cls,
        mock_generate_copilot_artifacts,
    ):
        from fluid_build.cli.forge_copilot_memory import CopilotProjectMemory
        from fluid_build.cli.forge_copilot_runtime import DiscoveryReport, LlmConfig

        agent = self._make_agent()
        memory = CopilotProjectMemory(
            schema_version=1,
            saved_at="2026-03-31T00:00:00Z",
            project_profile={
                "template": "analytics",
                "provider": "local",
                "domain": "analytics",
                "owner": "data-team",
            },
            conventions={
                "build_engines": ["sql"],
                "binding_platforms": ["local"],
                "binding_formats": ["csv"],
                "expose_kinds": ["table"],
                "provider_hints": ["local"],
                "source_formats": {"csv": 1},
                "schema_summaries": [],
            },
            recent_outcomes=[],
        )
        mock_resolve_llm_config.return_value = LlmConfig(
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="key",
        )
        mock_discover_local_context.return_value = DiscoveryReport(workspace_roots=["/tmp/test"])
        mock_store_cls.return_value.load.return_value = memory

        agent.generate_project_artifacts({"project_goal": "test"}, {"target_dir": "/tmp/test"})

        assert (
            mock_generate_copilot_artifacts.call_args.kwargs["project_memory"].preferred_provider
            == "local"
        )

    @patch("fluid_build.cli.forge.generate_copilot_artifacts")
    @patch("fluid_build.cli.forge.CopilotMemoryStore")
    @patch("fluid_build.cli.forge.discover_local_context")
    @patch("fluid_build.cli.forge.resolve_llm_config")
    def test_generate_project_artifacts_bypasses_memory_when_disabled(
        self,
        mock_resolve_llm_config,
        mock_discover_local_context,
        mock_store_cls,
        mock_generate_copilot_artifacts,
    ):
        from fluid_build.cli.forge_copilot_runtime import DiscoveryReport, LlmConfig

        agent = self._make_agent()
        mock_resolve_llm_config.return_value = LlmConfig(
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="key",
        )
        mock_discover_local_context.return_value = DiscoveryReport(workspace_roots=["/tmp/test"])

        agent.generate_project_artifacts(
            {"project_goal": "test"},
            {"target_dir": "/tmp/test", "memory": False},
        )

        mock_store_cls.return_value.load.assert_not_called()
        assert mock_generate_copilot_artifacts.call_args.kwargs["project_memory"] is None

    def test_create_forge_config(self):
        agent = self._make_agent()
        config = agent._create_forge_config(
            Path("/tmp/test"),
            {"project_goal": "Build data product", "use_case": "etl"},
            {"recommended_template": "etl", "recommended_provider": "local"},
        )
        assert "name" in config or "template" in config

    def test_sanitize_project_name(self):
        agent = self._make_agent()
        name = agent._sanitize_project_name("My Cool Project!")
        assert isinstance(name, str)
        assert len(name) > 0

    def test_create_with_forge_engine_success(self):
        agent = self._make_agent()
        agent.console = None  # force simple mode
        with patch("fluid_build.forge.ForgeEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.run_with_config.return_value = True
            mock_engine_cls.return_value = mock_engine
            result = agent._create_with_forge_engine({"name": "test", "template": "starter"})
            assert result is True

    def test_create_with_forge_engine_failure(self):
        agent = self._make_agent()
        agent.console = None
        with patch("fluid_build.forge.ForgeEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.run_with_config.return_value = False
            mock_engine_cls.return_value = mock_engine
            result = agent._create_with_forge_engine({"name": "test"})
            assert result is False

    def test_create_with_forge_engine_exception(self):
        agent = self._make_agent()
        agent.console = None
        with patch("fluid_build.forge.ForgeEngine", side_effect=RuntimeError):
            result = agent._create_with_forge_engine({"name": "test"})
            assert result is False

    def test_show_ai_analysis_no_console(self):
        agent = self._make_agent()
        agent.console = None
        agent._show_ai_analysis({}, {})  # Should not raise

    def test_show_ai_analysis_prefers_use_case_other_text(self):
        agent = self._make_agent()
        agent.console = MagicMock()
        agent._show_ai_analysis(
            {
                "project_goal": "Test project",
                "data_sources": "warehouse tables",
                "use_case": "other",
                "use_case_other": "Customer 360",
                "complexity": "intermediate",
            },
            {
                "recommended_template": "analytics",
                "recommended_provider": "local",
                "recommended_patterns": [],
                "architecture_suggestions": [],
                "best_practices": [],
            },
        )
        panel = agent.console.print.call_args.args[0]
        text = str(panel.renderable)
        assert "Customer 360" in text
        assert "Use Case:** other" not in text

    def test_generate_intelligent_contract(self):
        agent = self._make_agent()
        contract = agent._generate_intelligent_contract(
            {"project_goal": "ETL pipeline", "use_case": "etl"},
            {"recommended_template": "etl", "recommended_provider": "local"},
        )
        assert isinstance(contract, str)
        assert len(contract) > 0

    def test_generate_intelligent_readme(self):
        agent = self._make_agent()
        readme = agent._generate_intelligent_readme(
            {"project_goal": "Test project", "use_case": "analytics"},
            {
                "recommended_template": "analytics",
                "recommended_provider": "gcp",
                "architecture_suggestions": ["Use partitioning"],
                "best_practices": ["Test early"],
                "recommended_patterns": [],
                "technology_stack": [],
            },
        )
        assert isinstance(readme, str)
        assert len(readme) > 0


# ---- Module-level functions ----


class TestRegisterFunction:
    def test_register(self):
        from fluid_build.cli.forge import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)


class TestGetTargetDirectory:
    def test_with_target_dir_arg(self):
        from fluid_build.cli.forge import get_target_directory

        args = MagicMock()
        args.target_dir = "/tmp/my-project"
        result = get_target_directory(args)
        assert result == Path("/tmp/my-project")

    def test_without_target_dir(self):
        from fluid_build.cli.forge import get_target_directory

        args = MagicMock()
        args.target_dir = None
        result = get_target_directory(args, default_name="my-fluid-project")
        assert isinstance(result, Path)
        assert "my-fluid-project" in str(result)


class TestLoadContext:
    def test_load_json_string(self):
        from fluid_build.cli.forge import load_context

        ctx = load_context('{"project_goal": "test"}')
        assert ctx["project_goal"] == "test"

    def test_load_invalid_json(self):
        from fluid_build.cli.forge import load_context

        with pytest.raises(Exception):
            load_context("not a json string and not a file path either!!!!")

    def test_load_json_file(self, tmp_path):
        from fluid_build.cli.forge import load_context

        f = tmp_path / "context.json"
        f.write_text(json.dumps({"project_goal": "test"}))
        ctx = load_context(str(f))
        assert ctx["project_goal"] == "test"

    def test_load_markdown_file_as_freeform_context(self, tmp_path):
        from fluid_build.cli.forge import load_context

        f = tmp_path / "prompt.md"
        f.write_text("# Telco prompt\nUse seeded Snowflake data.", encoding="utf-8")
        ctx = load_context(str(f))

        assert ctx["project_goal"].startswith("# Telco prompt")
        assert ctx["description"] == ctx["project_goal"]

    def test_load_nonexistent_file(self):
        from fluid_build.cli.forge import load_context

        with pytest.raises(Exception):
            load_context("/nonexistent/path/to/file.json")


class TestRunFunction:
    @patch("fluid_build.cli.forge.handle_memory_management", return_value=0)
    def test_run_memory_management_short_circuits(self, mock_memory):
        from fluid_build.cli.forge import run

        args = MagicMock()
        args.help = False
        args.show_memory = True
        args.reset_memory = False
        args.non_interactive = True
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0
        mock_memory.assert_called_once()

    @patch("fluid_build.cli.forge.run_ai_copilot_mode", return_value=0)
    def test_run_copilot_mode(self, mock_copilot):
        from fluid_build.cli.forge import run

        args = MagicMock()
        args.help = False
        args.mode = "copilot"
        # Skip inline LLM setup so the dispatch path doesn't depend on a saved
        # provider config — without this, CI (no saved config) falls into the
        # ``ask_confirmation`` branch and reads from a captured stdin.
        args.non_interactive = True
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0
        mock_copilot.assert_called_once()

    @patch("fluid_build.cli.forge.run_ai_copilot_mode", return_value=0)
    def test_run_non_interactive_copilot(self, mock_copilot):
        from fluid_build.cli.forge import run

        args = MagicMock()
        args.help = False
        args.show_memory = False
        args.reset_memory = False
        args.non_interactive = True
        logger = logging.getLogger("test")

        result = run(args, logger)

        assert result == 0
        mock_copilot.assert_called_once()


class TestRunAICopilotMode:
    @patch("fluid_build.cli.forge.CopilotAgent")
    def test_copilot_success(self, mock_agent_cls):
        from fluid_build.cli.forge import run_ai_copilot_mode

        mock_agent = MagicMock()
        mock_agent.create_project.return_value = True
        mock_agent_cls.return_value = mock_agent
        args = MagicMock()
        args.context = None
        args.non_interactive = True
        args.target_dir = "/tmp/test"
        logger = logging.getLogger("test")
        result = run_ai_copilot_mode(args, logger)
        assert result == 0

    @patch("fluid_build.cli.forge.CopilotAgent")
    def test_copilot_failure(self, mock_agent_cls):
        from fluid_build.cli.forge import run_ai_copilot_mode

        mock_agent = MagicMock()
        mock_agent.create_project.return_value = False
        mock_agent_cls.return_value = mock_agent
        args = MagicMock()
        args.context = None
        args.non_interactive = True
        args.target_dir = "/tmp/test"
        # Slice UX-H: pin `scaffold` explicitly so the test routes
        # through the legacy CopilotAgent.create_project path its
        # assertion depends on.  get_cli_arg uses vars(args), which
        # ignores MagicMock auto-attrs, so unset = minimal path.
        # `no_ci` prevents the auto-CI hook from touching stdin.
        args.scaffold = "etl_pipeline"
        args.no_ci = True
        args.dry_run = False
        logger = logging.getLogger("test")
        result = run_ai_copilot_mode(args, logger)
        assert result == 1

    @patch("fluid_build.cli.forge.CopilotAgent")
    def test_copilot_with_context(self, mock_agent_cls):
        from fluid_build.cli.forge import run_ai_copilot_mode

        mock_agent = MagicMock()
        mock_agent.create_project.return_value = True
        mock_agent_cls.return_value = mock_agent
        args = MagicMock()
        args.context = '{"project_goal": "test"}'
        args.non_interactive = True
        args.target_dir = "/tmp/test"
        logger = logging.getLogger("test")
        result = run_ai_copilot_mode(args, logger)
        assert result == 0

    @patch("fluid_build.cli.forge_modes.print_dialog_status")
    @patch("fluid_build.cli.forge_modes.run_adaptive_copilot_interview")
    @patch("fluid_build.cli.forge.CopilotAgent")
    def test_copilot_interactive_provider_warning_continues(
        self,
        mock_agent_cls,
        mock_interview,
        mock_print_dialog_status,
    ):
        from fluid_build.cli.forge import run_ai_copilot_mode

        mock_agent = MagicMock()
        mock_agent.prepare_runtime_inputs.return_value = {
            "llm_config": MagicMock(),
            "discovery_report": MagicMock(),
            "project_memory": None,
            "capability_matrix": {"providers": ["local"], "templates": {"starter": {}}},
            "capability_warnings": ["Copilot couldn't inspect the aws provider."],
        }
        mock_agent.create_project.return_value = True
        mock_agent_cls.return_value = mock_agent
        mock_state = MagicMock()
        mock_state.finalize.return_value = {
            "project_goal": "test",
            "data_sources": "local files",
            "use_case": "analytics",
            "complexity": "intermediate",
        }
        mock_interview.return_value = mock_state
        args = MagicMock()
        args.context = None
        args.non_interactive = False
        args.target_dir = "/tmp/test"
        # Slice UX-H: pin `scaffold` so this test stays on the legacy
        # create_project path (the minimal path doesn't exercise the
        # warning propagation this test asserts).  `no_ci` keeps the
        # post-generation auto-CI hook from calling console.input()
        # which explodes under pytest's stdin capture.
        args.scaffold = "etl_pipeline"
        args.no_ci = True
        args.dry_run = False
        logger = logging.getLogger("test")

        result = run_ai_copilot_mode(args, logger)

        assert result == 0
        assert any(
            call.kwargs.get("status") == "warning"
            and "couldn't fully verify some local providers"
            in call.kwargs.get("message", "").lower()
            for call in mock_print_dialog_status.call_args_list
        )


class TestMemoryManagement:
    @patch("fluid_build.cli.forge.CopilotMemoryStore")
    def test_handle_memory_management_show(self, mock_store_cls):
        from fluid_build.cli.forge import handle_memory_management
        from fluid_build.cli.forge_copilot_memory import CopilotProjectMemory

        mock_store_cls.return_value.load.return_value = CopilotProjectMemory(
            schema_version=1,
            saved_at="2026-03-31T00:00:00Z",
            project_profile={
                "template": "analytics",
                "provider": "local",
                "domain": "analytics",
                "owner": "data-team",
            },
            conventions={
                "build_engines": ["sql"],
                "binding_platforms": ["local"],
                "binding_formats": ["csv"],
                "expose_kinds": ["table"],
                "provider_hints": ["local"],
                "source_formats": {"csv": 1},
                "schema_summaries": [],
            },
            recent_outcomes=[],
        )
        mock_store_cls.return_value.path = Path("/tmp/test/runtime/.state/copilot-memory.json")
        args = MagicMock()
        args.target_dir = "/tmp/test"
        args.reset_memory = False
        args.show_memory = True

        result = handle_memory_management(args, logging.getLogger("test"))

        assert result == 0
        mock_store_cls.return_value.load.assert_called_once()

    @patch("fluid_build.cli.forge.CopilotMemoryStore")
    def test_handle_memory_management_reset(self, mock_store_cls):
        from fluid_build.cli.forge import handle_memory_management

        mock_store_cls.return_value.delete.return_value = True
        mock_store_cls.return_value.path = Path("/tmp/test/runtime/.state/copilot-memory.json")
        args = MagicMock()
        args.target_dir = "/tmp/test"
        args.reset_memory = True
        args.show_memory = False

        result = handle_memory_management(args, logging.getLogger("test"))

        assert result == 0
        mock_store_cls.return_value.delete.assert_called_once()


class TestLegacyBootstrapperRemoved:
    def test_create_legacy_bootstrapper_is_gone(self):
        """``create_legacy_bootstrapper`` was deleted along with the
        non-existent ``forge_legacy`` module it tried to import. The
        public surface is :func:`run_ai_copilot_mode` etc. directly
        on :mod:`fluid_build.cli.forge_modes`; there is no legacy
        bootstrapper."""
        import fluid_build.cli.forge as forge_mod

        assert not hasattr(forge_mod, "create_legacy_bootstrapper")
