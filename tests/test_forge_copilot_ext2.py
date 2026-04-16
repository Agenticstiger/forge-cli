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

"""Extended tests for CopilotAgent._analyze_requirements and contract generation."""

import pytest

from fluid_build.cli.forge import CopilotAgent


@pytest.fixture
def agent():
    return CopilotAgent.__new__(CopilotAgent)


class TestAnalyzeRequirementsDeep:
    """Thorough tests for _analyze_requirements keyword matching."""

    def test_ml_goal(self, agent):
        ctx = {
            "project_goal": "Build ML prediction model",
            "data_sources": "",
            "complexity": "intermediate",
        }
        s = agent._analyze_requirements(ctx)
        assert s["recommended_template"] == "ml_pipeline"
        assert "feature_store" in s["recommended_patterns"]
        assert any("MLflow" in a for a in s["architecture_suggestions"])

    def test_dashboard_goal(self, agent):
        ctx = {
            "project_goal": "Create analytics dashboard",
            "data_sources": "",
            "complexity": "intermediate",
        }
        s = agent._analyze_requirements(ctx)
        assert s["recommended_template"] == "analytics"
        assert "dimensional_modeling" in s["recommended_patterns"]

    def test_streaming_goal(self, agent):
        ctx = {
            "project_goal": "Build real-time streaming pipeline",
            "data_sources": "",
            "complexity": "intermediate",
        }
        s = agent._analyze_requirements(ctx)
        assert s["recommended_template"] == "streaming"
        assert "event_sourcing" in s["recommended_patterns"]
        assert any("Kafka" in a for a in s["architecture_suggestions"])

    def test_bigquery_source(self, agent):
        ctx = {
            "project_goal": "analytics",
            "data_sources": "bigquery tables",
            "complexity": "intermediate",
        }
        s = agent._analyze_requirements(ctx)
        assert "gcp" in s["recommended_provider"].lower()
        assert any("BigQuery" in bp for bp in s["best_practices"])

    def test_snowflake_source(self, agent):
        ctx = {
            "project_goal": "analytics",
            "data_sources": "snowflake warehouse",
            "complexity": "intermediate",
        }
        s = agent._analyze_requirements(ctx)
        assert "snowflake" in s["recommended_provider"].lower()

    def test_aws_source_s3(self, agent):
        ctx = {
            "project_goal": "analytics",
            "data_sources": "data on s3",
            "complexity": "intermediate",
        }
        s = agent._analyze_requirements(ctx)
        assert "aws" in s["recommended_provider"].lower()
        assert any("S3" in bp for bp in s["best_practices"])

    def test_aws_source_redshift(self, agent):
        ctx = {
            "project_goal": "analytics",
            "data_sources": "redshift cluster",
            "complexity": "intermediate",
        }
        s = agent._analyze_requirements(ctx)
        assert "aws" in s["recommended_provider"].lower()

    def test_simple_complexity(self, agent):
        ctx = {"project_goal": "basics", "data_sources": "", "complexity": "simple"}
        s = agent._analyze_requirements(ctx)
        assert any("single-layer" in a for a in s["architecture_suggestions"])
        assert any("essential" in bp for bp in s["best_practices"])

    def test_advanced_complexity(self, agent):
        ctx = {"project_goal": "basics", "data_sources": "", "complexity": "advanced"}
        s = agent._analyze_requirements(ctx)
        assert "data_mesh" in s["recommended_patterns"]
        assert "event_driven" in s["recommended_patterns"]
        assert any("microservices" in a for a in s["architecture_suggestions"])

    def test_default_recommendations(self, agent):
        ctx = {"project_goal": "", "data_sources": "", "complexity": "intermediate"}
        s = agent._analyze_requirements(ctx)
        assert s["recommended_template"] == "analytics"
        assert s["recommended_provider"] == "local"
        assert s["recommended_patterns"] == []


class TestGenerateIntelligentContract:
    def test_basic_contract(self, agent):
        ctx = {"project_goal": "Price Tracker", "use_case": "analytics"}
        suggestions = {"recommended_provider": "local"}
        result = agent._generate_intelligent_contract(ctx, suggestions)
        assert "price-tracker" in result
        assert '"Analytics & BI"' in result
        assert "provider:" in result
        assert "type: local" in result

    def test_contract_has_quality_section(self, agent):
        ctx = {"project_goal": "Test", "use_case": "reporting"}
        suggestions = {"recommended_provider": "gcp"}
        result = agent._generate_intelligent_contract(ctx, suggestions)
        assert "quality:" in result
        assert "data_freshness" in result

    def test_gcp_specific_comments(self, agent):
        ctx = {"project_goal": "Test", "use_case": "analytics"}
        suggestions = {"recommended_provider": "gcp"}
        result = agent._generate_intelligent_contract(ctx, suggestions)
        assert "GCP" in result or "gcp" in result

    def test_contract_uses_use_case_other_instead_of_other_slug(self, agent):
        ctx = {"project_goal": "Test", "use_case": "other", "use_case_other": "Customer 360"}
        suggestions = {"recommended_provider": "local"}
        result = agent._generate_intelligent_contract(ctx, suggestions)
        assert '"Customer 360"' in result
        assert "domain: other" not in result


class TestGenerateIntelligentReadme:
    def test_basic_readme(self, agent):
        ctx = {"project_goal": "My Project", "use_case": "analytics"}
        suggestions = {
            "recommended_template": "analytics",
            "recommended_provider": "local",
            "recommended_patterns": ["dimensional_modeling"],
            "architecture_suggestions": ["Use layered architecture"],
            "best_practices": ["Focus on essentials"],
        }
        result = agent._generate_intelligent_readme(ctx, suggestions)
        assert "# My Project" in result
        assert "Analytics & BI" in result
        assert "FLUID" in result

    def test_readme_contains_quickstart(self, agent):
        ctx = {"project_goal": "Test", "use_case": "reporting"}
        suggestions = {
            "recommended_template": "t1",
            "recommended_provider": "local",
            "recommended_patterns": [],
            "architecture_suggestions": [],
            "best_practices": [],
        }
        result = agent._generate_intelligent_readme(ctx, suggestions)
        # README should have some kind of instructions
        assert (
            "validate" in result.lower()
            or "getting started" in result.lower()
            or "quick" in result.lower()
        )

    def test_readme_prefers_use_case_other_text(self, agent):
        ctx = {"project_goal": "Test", "use_case": "other", "use_case_other": "CDC sync"}
        suggestions = {
            "recommended_template": "etl_pipeline",
            "recommended_provider": "local",
            "recommended_patterns": [],
            "architecture_suggestions": [],
            "best_practices": [],
        }
        result = agent._generate_intelligent_readme(ctx, suggestions)
        assert "CDC sync" in result
        assert "- **Use Case:** other" not in result
