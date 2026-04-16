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

"""Tests for forge.py CopilotAgent analyze_requirements and suggestions engine."""

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.forge import CopilotAgent


@pytest.fixture
def agent():
    with patch.object(CopilotAgent, "__init__", lambda self, *a, **k: None):
        a = CopilotAgent.__new__(CopilotAgent)
        a.console = None
        a.logger = MagicMock()
        return a


class TestAnalyzeRequirements:
    def test_analytics_use_case(self, agent):
        result = agent.analyze_requirements({"use_case": "analytics"})
        assert result["recommended_template"] == "analytics"
        assert result["recommended_provider"] == "local"

    def test_reporting_use_case(self, agent):
        result = agent.analyze_requirements({"use_case": "reporting"})
        assert result["recommended_template"] == "analytics"

    def test_data_platform_use_case(self, agent):
        result = agent.analyze_requirements({"use_case": "data_platform"})
        assert result["recommended_template"] == "etl_pipeline"

    def test_data_lake_legacy_alias(self, agent):
        result = agent.analyze_requirements({"use_case": "data_lake"})
        assert result["recommended_template"] == "etl_pipeline"

    def test_ml_pipeline(self, agent):
        result = agent.analyze_requirements({"use_case": "ml_pipeline"})
        assert result["recommended_template"] == "ml_pipeline"

    def test_ml_keyword(self, agent):
        result = agent.analyze_requirements({"use_case": "machine learning model"})
        assert result["recommended_template"] == "ml_pipeline"

    def test_streaming(self, agent):
        result = agent.analyze_requirements({"use_case": "streaming"})
        assert result["recommended_template"] == "streaming"

    def test_etl(self, agent):
        result = agent.analyze_requirements({"use_case": "etl"})
        assert result["recommended_template"] == "etl_pipeline"

    def test_pipeline(self, agent):
        result = agent.analyze_requirements({"use_case": "pipeline"})
        assert result["recommended_template"] == "etl_pipeline"

    def test_unknown_use_case(self, agent):
        result = agent.analyze_requirements({"use_case": "something_else"})
        assert result["recommended_template"] == "starter"

    def test_other_use_case_uses_follow_up_text(self, agent):
        result = agent.analyze_requirements(
            {
                "use_case": "other",
                "use_case_other": "CDC sync for warehouse loads",
            }
        )
        assert result["recommended_template"] == "etl_pipeline"

    def test_default_use_case(self, agent):
        result = agent.analyze_requirements({})
        assert result["recommended_template"] == "analytics"

    def test_gcp_provider(self, agent):
        result = agent.analyze_requirements({"data_sources": "bigquery tables"})
        assert result["recommended_provider"] == "gcp"

    def test_gcp_keyword(self, agent):
        result = agent.analyze_requirements({"data_sources": "gcp storage"})
        assert result["recommended_provider"] == "gcp"

    def test_snowflake_provider(self, agent):
        result = agent.analyze_requirements({"data_sources": "snowflake warehouse"})
        assert result["recommended_provider"] == "snowflake"

    def test_aws_provider(self, agent):
        result = agent.analyze_requirements({"data_sources": "aws s3 buckets"})
        assert result["recommended_provider"] == "aws"

    def test_local_default_provider(self, agent):
        result = agent.analyze_requirements({"data_sources": "csv files"})
        assert result["recommended_provider"] == "local"

    def test_advanced_complexity(self, agent):
        result = agent.analyze_requirements({"complexity": "advanced"})
        assert "data_mesh" in result["recommended_patterns"]

    def test_intermediate_complexity(self, agent):
        result = agent.analyze_requirements({"complexity": "intermediate"})
        assert "layered_architecture" in result["recommended_patterns"]

    def test_simple_complexity(self, agent):
        result = agent.analyze_requirements({"complexity": "simple"})
        assert "simple_pipeline" in result["recommended_patterns"]

    def test_best_practices_included(self, agent):
        result = agent.analyze_requirements({})
        assert len(result["best_practices"]) > 0
        assert any("version control" in bp.lower() for bp in result["best_practices"])

    def test_architecture_suggestions(self, agent):
        result = agent.analyze_requirements({})
        assert len(result["architecture_suggestions"]) > 0
