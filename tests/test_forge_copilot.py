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

"""Tests for fluid_build.cli.forge — exceptions, ForgeMode, CopilotAgent pure methods."""

from pathlib import Path

import pytest

from fluid_build.cli.forge import (
    ContextValidationError,
    CopilotAgent,
    ForgeError,
    ForgeMode,
    InvalidProjectNameError,
    ProjectGenerationError,
)

# ── Custom exceptions ──


class TestForgeExceptions:
    def test_invalid_project_name(self):
        assert issubclass(InvalidProjectNameError, ForgeError)

    def test_project_generation_error(self):
        assert issubclass(ProjectGenerationError, ForgeError)

    def test_context_validation_error(self):
        assert issubclass(ContextValidationError, ForgeError)


# ── ForgeMode ──


class TestForgeMode:
    def test_values(self):
        assert ForgeMode.AI_COPILOT.value == "copilot"
        assert ForgeMode.BLANK.value == "blank"


# ── CopilotAgent.analyze_requirements ──


class TestCopilotAgentAnalyze:
    def _agent(self):
        return CopilotAgent()

    def test_get_questions_exposes_friendly_use_case_choices(self):
        agent = self._agent()
        questions = agent.get_questions()
        use_case_question = next(q for q in questions if q["key"] == "use_case")
        assert use_case_question["choices"][0]["label"] == "Analytics & BI"
        assert use_case_question["choices"][0]["value"] == "analytics"
        assert use_case_question["choices"][-1]["value"] == "other"
        assert use_case_question["follow_up"]["key"] == "use_case_other"

    def test_default_suggestions(self):
        agent = self._agent()
        s = agent.analyze_requirements({})
        assert "local" in s["recommended_provider"].lower()
        assert "recommended_template" in s

    def test_ml_use_case(self):
        agent = self._agent()
        s = agent.analyze_requirements({"use_case": "ml_pipeline"})
        assert (
            "ml" in s["recommended_template"].lower()
            or "pipeline" in s["recommended_template"].lower()
        )

    def test_streaming_use_case(self):
        agent = self._agent()
        s = agent.analyze_requirements({"use_case": "streaming"})
        assert "stream" in s["recommended_template"].lower()

    def test_analytics_use_case(self):
        agent = self._agent()
        s = agent.analyze_requirements({"use_case": "analytics"})
        assert "analytic" in s["recommended_template"].lower()

    def test_data_platform_use_case(self):
        agent = self._agent()
        s = agent.analyze_requirements({"use_case": "data_platform"})
        assert s["recommended_template"] == "etl_pipeline"

    def test_other_use_case_infers_from_follow_up(self):
        agent = self._agent()
        s = agent.analyze_requirements({"use_case": "other", "use_case_other": "customer CDC sync"})
        assert s["recommended_template"] == "etl_pipeline"

    def test_bigquery_provider(self):
        agent = self._agent()
        s = agent.analyze_requirements({"data_sources": "BigQuery tables"})
        assert (
            "gcp" in s["recommended_provider"].lower()
            or "google" in s["recommended_provider"].lower()
        )

    def test_snowflake_provider(self):
        agent = self._agent()
        s = agent.analyze_requirements({"data_sources": "Snowflake warehouse"})
        assert (
            "snowflake" in s["recommended_provider"].lower()
            or "snow" in s["recommended_provider"].lower()
        )

    def test_aws_provider(self):
        agent = self._agent()
        s = agent.analyze_requirements({"data_sources": "AWS S3"})
        assert "aws" in s["recommended_provider"].lower()

    def test_advanced_complexity(self):
        agent = self._agent()
        s = agent.analyze_requirements({"complexity": "advanced"})
        assert "data_mesh" in s["recommended_patterns"]

    def test_simple_complexity(self):
        agent = self._agent()
        s = agent.analyze_requirements({"complexity": "simple"})
        assert "simple_pipeline" in s["recommended_patterns"]

    def test_best_practices_populated(self):
        agent = self._agent()
        s = agent.analyze_requirements({})
        assert len(s["best_practices"]) > 0


# ── CopilotAgent._analyze_requirements (private deeper analysis) ──


class TestCopilotAgentDeepAnalyze:
    def _agent(self):
        return CopilotAgent()

    def test_ml_keywords(self):
        agent = self._agent()
        s = agent._analyze_requirements({"project_goal": "ML prediction model"})
        assert s["recommended_template"] == "ml_pipeline"
        assert "feature_store" in s["recommended_patterns"]

    def test_dashboard_keywords(self):
        agent = self._agent()
        s = agent._analyze_requirements({"project_goal": "dashboard for reporting"})
        assert s["recommended_template"] == "analytics"
        assert "dimensional_modeling" in s["recommended_patterns"]

    def test_streaming_keywords(self):
        agent = self._agent()
        s = agent._analyze_requirements({"project_goal": "real-time event processing"})
        assert s["recommended_template"] == "streaming"

    def test_data_source_gcp(self):
        agent = self._agent()
        s = agent._analyze_requirements({"data_sources": "bigquery tables"})
        assert s["recommended_provider"] == "gcp"

    def test_advanced_complexity_patterns(self):
        agent = self._agent()
        s = agent._analyze_requirements({"complexity": "advanced"})
        assert "data_mesh" in s["recommended_patterns"]
        assert "event_driven" in s["recommended_patterns"]


# ── CopilotAgent._create_forge_config ──


class TestCopilotAgentForgeConfig:
    def _agent(self):
        return CopilotAgent()

    def test_config_structure(self):
        agent = self._agent()
        suggestions = agent.analyze_requirements({"use_case": "analytics"})
        config = agent._create_forge_config(
            Path("/tmp/test"),
            {"project_goal": "Test Product"},
            suggestions,
        )
        assert "name" in config
        assert config["template"] == suggestions["recommended_template"]
        assert config["provider"] == suggestions["recommended_provider"]
        assert config["target_dir"] == "/tmp/test"

    def test_project_name_sanitized(self):
        agent = self._agent()
        suggestions = agent.analyze_requirements({})
        config = agent._create_forge_config(
            Path("/tmp"),
            {"project_goal": "My #Awesome! Project"},
            suggestions,
        )
        # name should be lowercase, no special chars
        assert config["name"].islower() or config["name"] == "my-data-product"
        assert "#" not in config["name"]
