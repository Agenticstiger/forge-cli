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

"""Branch-coverage tests for fluid_build.cli.forge_agents"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.forge_agents import (
    DOMAIN_AGENTS,
    AIAgentBase,
    FinanceAgent,
    HealthcareAgent,
    RetailAgent,
    TelcoAgent,
    get_agent,
    list_agents,
)

# ===================== AIAgentBase =====================


class TestAIAgentBase:
    def test_init_with_rich(self):
        agent = AIAgentBase("test", "desc", "domain")
        assert agent.name == "test"
        assert agent.domain == "domain"

    @patch("fluid_build.cli.forge_agents.AIAgentBase.__init__", return_value=None)
    def test_init_no_rich(self, _mock_init):
        agent = AIAgentBase.__new__(AIAgentBase)
        agent.console = None
        agent.name = "test"
        agent.domain = "domain"
        assert agent.console is None

    def test_get_questions_raises(self):
        agent = AIAgentBase("t", "d", "dom")
        with pytest.raises(NotImplementedError):
            agent.get_questions()

    def test_analyze_requirements_raises(self):
        agent = AIAgentBase("t", "d", "dom")
        with pytest.raises(NotImplementedError):
            agent.analyze_requirements({})

    def test_sanitize_project_name_basic(self):
        agent = AIAgentBase("t", "d", "dom")
        assert agent._sanitize_project_name("My Project!") == "my-project"

    def test_sanitize_project_name_numeric_start(self):
        agent = AIAgentBase("t", "d", "dom")
        result = agent._sanitize_project_name("123 test")
        assert result.startswith("project-")

    def test_sanitize_project_name_empty(self):
        agent = AIAgentBase("t", "d", "dom")
        result = agent._sanitize_project_name("!!!")
        assert result == "dom-data-product"

    def test_sanitize_project_name_extra_dashes(self):
        agent = AIAgentBase("t", "d", "dom")
        result = agent._sanitize_project_name("a---b---c")
        assert result == "a-b-c"

    @patch("fluid_build.cli.forge_agents.ForgeEngine", create=True)
    def test_create_project_success(self, _MockForge):
        agent = AIAgentBase("t", "desc", "dom")
        agent.console = None  # No Rich
        agent.analyze_requirements = MagicMock(
            return_value={
                "recommended_template": "tpl",
                "recommended_provider": "gcp",
                "recommended_patterns": [],
            }
        )

        with (
            patch.object(agent, "_create_with_forge_engine", return_value=True),
            patch.object(agent, "_show_next_steps"),
        ):
            result = agent.create_project(Path("/tmp/test"), {"project_goal": "My Goal"})
        assert result is True

    @patch("fluid_build.cli.forge_agents.ForgeEngine", create=True)
    def test_create_project_failure(self, _MockForge):
        agent = AIAgentBase("t", "desc", "dom")
        agent.console = MagicMock()
        agent.analyze_requirements = MagicMock(
            return_value={
                "recommended_template": "tpl",
                "recommended_provider": "gcp",
                "recommended_patterns": [],
            }
        )

        with patch.object(agent, "_create_with_forge_engine", return_value=False):
            result = agent.create_project(Path("/tmp/test"), {"project_goal": "Goal"})
        assert result is False

    def test_create_project_exception_with_console(self):
        agent = AIAgentBase("t", "desc", "dom")
        agent.console = MagicMock()
        agent.analyze_requirements = MagicMock(side_effect=RuntimeError("boom"))
        result = agent.create_project(Path("/tmp/test"), {"project_goal": "Goal"})
        assert result is False

    def test_create_project_exception_no_console(self):
        agent = AIAgentBase("t", "desc", "dom")
        agent.console = None
        agent.analyze_requirements = MagicMock(side_effect=RuntimeError("boom"))
        result = agent.create_project(Path("/tmp/test"), {"project_goal": "Goal"})
        assert result is False

    def test_create_forge_config(self):
        agent = AIAgentBase("t", "desc", "dom")
        context = {"project_goal": "My Analytics"}
        suggestions = {
            "recommended_template": "tpl",
            "recommended_provider": "gcp",
            "recommended_patterns": [],
        }
        cfg = agent._create_forge_config(Path("/tmp"), context, suggestions)
        assert cfg["template"] == "tpl"
        assert cfg["provider"] == "gcp"
        assert "my-analytics" in cfg["name"]

    def test_create_with_forge_engine_no_console(self):
        agent = AIAgentBase("t", "desc", "dom")
        agent.console = None
        with patch("fluid_build.forge.ForgeEngine") as MockFE:
            MockFE.return_value.run_with_config.return_value = True
            result = agent._create_with_forge_engine({"name": "test"})
        assert result is True

    def test_create_with_forge_engine_exception(self):
        agent = AIAgentBase("t", "desc", "dom")
        agent.console = MagicMock()
        with patch("fluid_build.forge.ForgeEngine", side_effect=RuntimeError("no")):
            result = agent._create_with_forge_engine({"name": "test"})
        assert result is False

    def test_show_ai_analysis_no_console(self):
        agent = AIAgentBase("t", "d", "dom")
        agent.console = None
        agent._show_ai_analysis(
            {},
            {"recommended_template": "t", "recommended_provider": "p", "recommended_patterns": []},
        )

    def test_show_ai_analysis_with_console(self):
        agent = AIAgentBase("t", "d", "dom")
        agent.console = MagicMock()
        agent._show_ai_analysis(
            {"project_goal": "Forecasting", "data_sources": "warehouse"},
            {
                "recommended_template": "t",
                "recommended_provider": "p",
                "recommended_patterns": ["x"],
            },
        )
        agent.console.print.assert_called()
        panel = agent.console.print.call_args.args[0]
        assert panel.title == "🧠 AI Analysis"

    def test_show_next_steps_no_console(self):
        agent = AIAgentBase("t", "d", "dom")
        agent.console = None
        agent._show_next_steps(Path("/tmp"), {}, {"recommended_provider": "gcp"})

    def test_show_next_steps_finance(self):
        agent = AIAgentBase("t", "d", "finance")
        agent.console = MagicMock()
        agent._show_next_steps(
            Path("/tmp"), {}, {"recommended_provider": "gcp", "security_requirements": ["x"]}
        )
        agent.console.print.assert_called()
        panel = agent.console.print.call_args.args[0]
        text = str(panel.renderable)
        assert "fluid validate contract.fluid.yaml" in text
        assert "/tmp" in text
        assert "contract.fluid.yaml" in text

    def test_show_next_steps_healthcare(self):
        agent = AIAgentBase("t", "d", "healthcare")
        agent.console = MagicMock()
        agent._show_next_steps(Path("/tmp"), {}, {"recommended_provider": "gcp"})

    def test_show_next_steps_retail(self):
        agent = AIAgentBase("t", "d", "retail")
        agent.console = MagicMock()
        agent._show_next_steps(Path("/tmp"), {}, {"recommended_provider": "gcp"})


# ===================== FinanceAgent =====================


class TestFinanceAgent:
    def test_init(self):
        agent = FinanceAgent()
        assert agent.domain == "finance"

    def test_get_questions(self):
        agent = FinanceAgent()
        qs = agent.get_questions()
        assert len(qs) >= 3
        assert qs[0]["key"] == "product_type"
        assert qs[0]["choices"][0]["label"] == "Risk Analytics"
        assert qs[0]["choices"][0]["value"] == "risk_analytics"

    def test_analyze_risk_analytics(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"product_type": "risk_analytics"})
        assert "risk" in result["recommended_template"]

    def test_analyze_trading_platform(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"product_type": "trading_platform"})
        assert "trading" in result["recommended_template"]

    def test_analyze_fraud_detection(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"product_type": "fraud_detection"})
        assert "ml" in result["recommended_template"]

    def test_analyze_friendly_product_phrase(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"product_type": "fraud analytics"})
        assert "ml" in result["recommended_template"]

    def test_analyze_default(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"product_type": "customer_analytics"})
        assert "analytics" in result["recommended_template"]

    def test_analyze_real_time(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"real_time": "yes"})
        assert "streaming_pipeline" in result["recommended_patterns"]

    def test_analyze_sox_compliance(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"compliance_requirements": "sox"})
        assert any("SOX" in r for r in result["security_requirements"])

    def test_analyze_gdpr_compliance(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"compliance_requirements": "gdpr"})
        assert any("portability" in r for r in result["security_requirements"])

    def test_analyze_pci_dss_compliance(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"compliance_requirements": "pci_dss"})
        assert any("Tokenize" in r for r in result["security_requirements"])

    def test_analyze_no_compliance(self):
        agent = FinanceAgent()
        result = agent.analyze_requirements({"compliance_requirements": "none"})
        assert any("GDPR-aligned retention" in item for item in result["security_requirements"])


# ===================== HealthcareAgent =====================


class TestHealthcareAgent:
    def test_init(self):
        agent = HealthcareAgent()
        assert agent.domain == "healthcare"

    def test_get_questions(self):
        qs = HealthcareAgent().get_questions()
        assert len(qs) >= 3
        assert qs[0]["choices"][0]["label"] == "Patient Analytics"

    def test_analyze_clinical_research(self):
        result = HealthcareAgent().analyze_requirements({"product_type": "clinical_research"})
        assert "clinical" in result["recommended_template"]
        assert "Jupyter" in result["technology_stack"]

    def test_analyze_population_health(self):
        result = HealthcareAgent().analyze_requirements({"product_type": "population_health"})
        assert "cohort_analysis" in result["recommended_patterns"]

    def test_analyze_default(self):
        result = HealthcareAgent().analyze_requirements({"product_type": "patient_analytics"})
        assert "analytics" in result["recommended_template"]
        assert result["canonical_model"] == "omop_cdm"

    def test_analyze_ehr_integration_prefers_fhir(self):
        result = HealthcareAgent().analyze_requirements({"product_type": "ehr_integration"})
        assert result["canonical_model"] == "hl7_fhir"
        assert "hl7_fhir_resource_model" in result["recommended_patterns"]

    def test_analyze_hipaa_required(self):
        result = HealthcareAgent().analyze_requirements({"hipaa_required": "yes"})
        assert any("HIPAA" in r for r in result["security_requirements"])

    def test_analyze_phi_yes(self):
        result = HealthcareAgent().analyze_requirements({"phi_handling": "yes"})
        assert any("PHI" in r for r in result["security_requirements"])

    def test_analyze_phi_deidentified(self):
        result = HealthcareAgent().analyze_requirements({"phi_handling": "deidentified_only"})
        assert any("de-identification" in s for s in result["architecture_suggestions"])

    def test_analyze_friendly_phi_phrase(self):
        result = HealthcareAgent().analyze_requirements({"phi_handling": "de identified"})
        assert any("de-identification" in s for s in result["architecture_suggestions"])


# ===================== RetailAgent =====================


class TestRetailAgent:
    def test_init(self):
        agent = RetailAgent()
        assert agent.domain == "retail"

    def test_get_questions(self):
        qs = RetailAgent().get_questions()
        assert len(qs) >= 3
        assert qs[0]["choices"][0]["label"] == "Customer 360"

    def test_analyze_recommendation_engine(self):
        result = RetailAgent().analyze_requirements({"product_type": "recommendation_engine"})
        assert "ml" in result["recommended_template"]

    def test_analyze_inventory_optimization(self):
        result = RetailAgent().analyze_requirements({"product_type": "inventory_optimization"})
        assert "optimization" in result["recommended_template"]

    def test_analyze_customer_360(self):
        result = RetailAgent().analyze_requirements({"product_type": "customer_360"})
        assert "customer360" in result["recommended_template"]
        assert result["canonical_model"] == "nrf_arts"
        assert "gs1_gdm" in result["supporting_standards"]

    def test_analyze_digital_retail_modeling_context(self):
        result = RetailAgent().analyze_requirements({"modeling_context": "digital experience"})
        assert result["canonical_model"] == "adobe_xdm"
        assert "adobe_xdm_experience_events" in result["recommended_patterns"]

    def test_analyze_traceability_modeling_context(self):
        result = RetailAgent().analyze_requirements(
            {"modeling_context": "traceability", "product_type": "inventory_optimization"}
        )
        assert result["canonical_model"] == "gs1_gdm"
        assert "gs1_epcis_cbv" in result["supporting_standards"]
        assert "epcis_event_modeling" in result["recommended_patterns"]

    def test_analyze_friendly_scale_phrase(self):
        result = RetailAgent().analyze_requirements({"scale": "enterprise scale"})
        assert result["recommended_provider"] == "gcp"

    def test_analyze_default(self):
        result = RetailAgent().analyze_requirements({"product_type": "price_optimization"})
        assert "analytics" in result["recommended_template"]
        assert result["canonical_model"] == "nrf_arts"

    def test_analyze_real_time(self):
        result = RetailAgent().analyze_requirements({"real_time_personalization": "yes"})
        assert "streaming_pipeline" in result["recommended_patterns"]

    def test_analyze_large_scale(self):
        result = RetailAgent().analyze_requirements({"scale": "large (>100M)"})
        assert result["recommended_provider"] == "gcp"
        assert len(result["performance_optimization"]) > 0

    def test_analyze_small_scale(self):
        result = RetailAgent().analyze_requirements({"scale": "small (<1M records)"})
        assert result["recommended_provider"] == "local"

    def test_analyze_medium_scale(self):
        result = RetailAgent().analyze_requirements({"scale": "medium (1M-100M)"})
        assert result["recommended_provider"] == "gcp"
        assert any("least-privilege RBAC" in item for item in result["security_requirements"])


# ===================== TelcoAgent =====================


class TestTelcoAgent:
    def test_init(self):
        agent = TelcoAgent()
        assert agent.domain == "telco"

    def test_get_questions(self):
        qs = TelcoAgent().get_questions()
        assert len(qs) >= 4
        assert qs[0]["choices"][0]["label"] == "Customer & Billing Intelligence"

    def test_analyze_customer_billing(self):
        result = TelcoAgent().analyze_requirements(
            {"product_type": "customer_billing_intelligence"}
        )
        assert "customer360" in result["recommended_template"]
        assert "party_account_model" in result["recommended_patterns"]

    def test_analyze_friendly_telco_phrase(self):
        result = TelcoAgent().analyze_requirements({"product_type": "network assurance"})
        assert "network_topology_analytics" in result["recommended_patterns"]

    def test_analyze_sid_resource_focus(self):
        result = TelcoAgent().analyze_requirements({"sid_domain_focus": "network inventory"})
        assert any("ResourceSpecification" in item for item in result["architecture_suggestions"])

    def test_analyze_real_time_operations(self):
        result = TelcoAgent().analyze_requirements({"real_time_operations": "yes"})
        assert "streaming_pipeline" in result["recommended_patterns"]
        assert result["canonical_model"] == "tmf_sid"
        assert any("GDPR-aligned retention" in item for item in result["security_requirements"])


# ===================== Registry functions =====================


class TestRegistry:
    def test_domain_agents_keys(self):
        assert "finance" in DOMAIN_AGENTS
        assert "healthcare" in DOMAIN_AGENTS
        assert "retail" in DOMAIN_AGENTS
        assert "telco" in DOMAIN_AGENTS

    def test_get_agent_finance(self):
        agent = get_agent("finance")
        assert isinstance(agent, FinanceAgent)

    def test_get_agent_healthcare(self):
        agent = get_agent("healthcare")
        assert isinstance(agent, HealthcareAgent)

    def test_get_agent_retail(self):
        agent = get_agent("retail")
        assert isinstance(agent, RetailAgent)

    def test_get_agent_telco(self):
        agent = get_agent("telco")
        assert isinstance(agent, TelcoAgent)

    def test_get_agent_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            get_agent("unknown")

    def test_list_agents(self):
        agents = list_agents()
        # 4 original built-ins + 8 vertical domain agents + the cross-cutting
        # ``ai_ready`` agent = 13.
        assert len(agents) == 13
        names = [a["name"] for a in agents]
        for expected in (
            "finance",
            "healthcare",
            "retail",
            "telco",
            "manufacturing",
            "logistics",
            "energy",
            "government",
            "insurance",
            "pharma",
            "education",
            "media",
            "ai_ready",
        ):
            assert expected in names, expected
        for a in agents:
            assert "description" in a
            assert "domain" in a
