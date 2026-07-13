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

"""Pins the 8 high-impact vertical domain agents.

Each vertical is grounded in an authoritative industry standard:

* manufacturing — ISA-95 / IEC 62264 + ISO 22400 (OEE)
* logistics     — GS1 EPCIS/CBV + EDI X12 + Incoterms 2020
* energy        — IEC CIM (61968/61970) + NERC CIP + Green Button
* government    — NIEM + FedRAMP/NIST 800-53 + DCAT-US
* insurance     — ACORD + IFRS 17 / Solvency II / NAIC
* pharma        — GxP + 21 CFR Part 11 + CDISC (SDTM/ADaM) + IDMP
* education      — Ed-Fi + 1EdTech OneRoster/Caliper + FERPA
* media         — EIDR + MovieLabs Ontology + SMPTE + QoE

The suite asserts, for every vertical:
  (a) the agent class + spec LOAD via the same loader the shipped
      agents use (``DOMAIN_AGENTS`` + ``load_builtin_agent_spec``),
  (b) its keywords make ``detect_domain`` return that vertical for a
      representative prompt (data-driven via ``domain_keywords.yaml``),
  (c) driving the agent's recommended template end-to-end produces a
      contract that PASSES ``fluid validate`` (real CLI subprocess).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
import yaml

from fluid_build.cli.forge_agent_specs import load_builtin_agent_spec
from fluid_build.cli.forge_agents import (
    DOMAIN_AGENTS,
    EducationAgent,
    EnergyAgent,
    GovernmentAgent,
    InsuranceAgent,
    LogisticsAgent,
    ManufacturingAgent,
    MediaAgent,
    PharmaAgent,
    get_agent,
)
from fluid_build.cli.forge_domain_agent_base import DeclarativeDomainAgent
from fluid_build.cli.forge_domain_enrichment import _load_domain_keywords, detect_domain

# Templates registered by ``forge.templates.register_templates``.
_REGISTERED_TEMPLATES = {"starter", "analytics", "ml_pipeline", "etl_pipeline", "streaming"}
_KNOWN_PROVIDERS = {"local", "gcp", "aws", "snowflake", "odps", "odcs"}

# vertical -> (agent class, canonical_model, representative detect prompt)
VERTICALS = {
    "manufacturing": (
        ManufacturingAgent,
        "isa95_iec62264",
        "Build an OEE and throughput analytics product from MES shop-floor and "
        "SCADA historian data aligned to the ISA-95 plant hierarchy",
    ),
    "logistics": (
        LogisticsAgent,
        "gs1_epcis_cbv",
        "A shipment visibility and carrier scorecard product from TMS/WMS feeds, "
        "EDI ASN transactions and GS1 EPCIS scan events across the supply chain",
    ),
    "energy": (
        EnergyAgent,
        "iec_cim",
        "AMI smart-meter analytics and grid outage reliability from interval meter "
        "reads and SCADA telemetry for a utility under NERC CIP scope",
    ),
    "government": (
        GovernmentAgent,
        "niem",
        "Open-data publication for a public-sector agency using NIEM and DCAT with "
        "FedRAMP controls and FOIA disclosure review",
    ),
    "insurance": (
        InsuranceAgent,
        "acord",
        "Claims analytics and actuarial reserving for a P&C insurer with ACORD "
        "policyholder data, premium and reinsurance reconciliation",
    ),
    "pharma": (
        PharmaAgent,
        "cdisc",
        "A GxP-validated clinical-trial CDISC SDTM pipeline for pharmaceutical data "
        "with 21 CFR Part 11 controls and LIMS quality records",
    ),
    "education": (
        EducationAgent,
        "ed_fi",
        "Student SIS analytics and learning analytics using Ed-Fi and OneRoster with "
        "FERPA governance for a K-12 school district",
    ),
    "media": (
        MediaAgent,
        "movielabs_omc",
        "Streaming QoE and audience viewership analytics with EIDR content metadata "
        "and playback rebuffer telemetry for our OTT service",
    ),
}

ALL_VERTICALS = sorted(VERTICALS)


# ---------------------------------------------------------------------------
# (a) Registry + spec loading
# ---------------------------------------------------------------------------


class TestVerticalRegistry:
    def test_all_eight_registered(self):
        for vertical in ALL_VERTICALS:
            assert vertical in DOMAIN_AGENTS, vertical

    def test_domain_agents_has_twelve(self):
        # 4 original (finance/healthcare/retail/telco) + 8 verticals + the
        # cross-cutting ``ai_ready`` agent = 13.
        assert len(DOMAIN_AGENTS) == 13
        assert "ai_ready" in DOMAIN_AGENTS

    @pytest.mark.parametrize("vertical", ALL_VERTICALS)
    def test_get_agent_resolves_builtin_class(self, vertical):
        expected_cls = VERTICALS[vertical][0]
        agent = get_agent(vertical)
        assert isinstance(agent, expected_cls)
        assert agent.domain == vertical
        assert agent.name == vertical
        assert isinstance(agent, DeclarativeDomainAgent)

    @pytest.mark.parametrize("vertical", ALL_VERTICALS)
    def test_spec_loads_via_shared_loader(self, vertical):
        spec = load_builtin_agent_spec(vertical)
        assert spec.name == vertical
        assert spec.domain == vertical
        assert spec.description
        # Every vertical spans the full medallion (SDP/ADP/CDP).
        assert spec.supported_data_product_types == ["SDP", "ADP", "CDP"]
        # At least a product-type choice + a data-sources question.
        keys = [q["key"] for q in spec.questions]
        assert "product_type" in keys
        assert "data_sources" in keys


# ---------------------------------------------------------------------------
# (b) Data-driven detection via domain_keywords.yaml
# ---------------------------------------------------------------------------


class TestVerticalDetection:
    @pytest.mark.parametrize("vertical", ALL_VERTICALS)
    def test_detect_domain_returns_vertical(self, vertical):
        _load_domain_keywords.cache_clear()
        try:
            prompt = VERTICALS[vertical][2]
            detected = detect_domain({"project_goal": prompt})
            assert detected == vertical, f"{vertical!r} prompt detected as {detected!r}"
        finally:
            _load_domain_keywords.cache_clear()

    def test_keywords_are_data_driven_not_hardcoded(self):
        """Each vertical must have a keyword block in domain_keywords.yaml."""
        from fluid_build.cli import forge_domain_enrichment as _fde

        raw = yaml.safe_load(Path(_fde._KEYWORDS_PATH).read_text(encoding="utf-8"))
        domains = raw.get("domains") or {}
        for vertical in ALL_VERTICALS:
            assert vertical in domains, f"{vertical} missing from domain_keywords.yaml"
            assert len(domains[vertical]) >= 8, f"{vertical} has too few keywords"

    def test_existing_domains_still_detect(self):
        """The 8 new keyword blocks must not steal hits from shipped domains."""
        _load_domain_keywords.cache_clear()
        try:
            cases = {
                "finance": "banking fraud and risk compliance for a trading platform",
                "healthcare": "patient clinical EHR HL7 FHIR hospital claims",
                "retail": "ecommerce inventory product catalog store fulfillment",
                "telco": "telecom subscriber CDR network OSS BSS billing roaming",
            }
            for domain, prompt in cases.items():
                assert detect_domain({"project_goal": prompt}) == domain
        finally:
            _load_domain_keywords.cache_clear()


# ---------------------------------------------------------------------------
# Deterministic analysis (no LLM) — recommendations are sane + grounded
# ---------------------------------------------------------------------------


class TestVerticalAnalysis:
    @pytest.mark.parametrize("vertical", ALL_VERTICALS)
    def test_default_recommendation_is_actionable(self, vertical):
        agent = get_agent(vertical)
        result = agent.analyze_requirements({})
        assert result["recommended_template"] in _REGISTERED_TEMPLATES
        assert result["recommended_provider"] in _KNOWN_PROVIDERS
        # Standard grounding: canonical model + supporting standards present.
        assert result["canonical_model"] == VERTICALS[vertical][1]
        assert result.get("supporting_standards"), vertical
        assert result.get("recommended_patterns"), vertical

    @pytest.mark.parametrize("vertical", ALL_VERTICALS)
    def test_global_security_baseline_applies(self, vertical):
        result = get_agent(vertical).analyze_requirements({})
        assert any(
            "least-privilege RBAC" in item for item in result["security_requirements"]
        ), vertical

    def test_ml_routing_rules_fire(self):
        """Product types that imply modeling route to the ml_pipeline template."""
        cases = [
            ("manufacturing", "predictive_maintenance"),
            ("logistics", "route_optimization"),
            ("energy", "load_forecasting"),
            ("insurance", "underwriting_pricing"),
            ("education", "early_warning"),
        ]
        for vertical, product_type in cases:
            result = get_agent(vertical).analyze_requirements({"product_type": product_type})
            assert result["recommended_template"] == "ml_pipeline", (vertical, product_type)

    def test_compliance_rules_inject_domain_controls(self):
        """Domain-specific compliance selections surface real control language."""
        checks = [
            ("manufacturing", {"compliance_requirements": "cfr_part_11"}, "Part 11"),
            ("energy", {"nerc_cip_scope": "yes"}, "NERC CIP"),
            ("government", {"compliance_requirements": "fedramp"}, "FedRAMP"),
            ("insurance", {"compliance_requirements": "ifrs_17"}, "lineage"),
            ("pharma", {"gxp_scope": "yes"}, "ALCOA+"),
            ("education", {"student_pii_scope": "yes"}, "FERPA"),
            ("logistics", {"incoterms_scope": "yes"}, "Incoterms 2020"),
        ]
        for vertical, ctx, needle in checks:
            result = get_agent(vertical).analyze_requirements(ctx)
            blob = " ".join(
                result.get("security_requirements", [])
                + result.get("architecture_suggestions", [])
                + result.get("recommended_patterns", [])
            )
            assert needle in blob, f"{vertical}: expected {needle!r} in analysis"


# ---------------------------------------------------------------------------
# (c) End-to-end: the agent's recommended template validates via the real CLI
# ---------------------------------------------------------------------------


def _fluid(*cli_args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["FLUID_FORGE_NO_PREVIEW"] = "1"
    env["FLUID_FORGE_NO_PICKER"] = "1"
    env["FLUID_FORGE_NO_WELCOME"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *cli_args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )


def _find_contract(cwd: Path) -> Optional[Path]:
    candidates = sorted(cwd.rglob("contract.fluid.yaml"))
    return candidates[0] if candidates else None


@pytest.mark.integration
@pytest.mark.parametrize("vertical", ALL_VERTICALS)
def test_vertical_produces_valid_contract_e2e(tmp_path, vertical):
    """Drive the domain agent's recommended template through the real
    ``fluid forge`` + ``fluid validate`` CLI and assert a clean pass.

    Deterministic (no LLM): the template + provider come from the domain
    agent's own ``analyze_requirements`` output.
    """
    agent = get_agent(vertical)
    suggestions = agent.analyze_requirements({})
    template = suggestions["recommended_template"]
    assert template in _REGISTERED_TEMPLATES

    forge = _fluid(
        "forge",
        "--template",
        template,
        "--target-dir",
        ".",
        "--provider",
        "local",
        "--non-interactive",
        cwd=tmp_path,
    )
    assert forge.returncode == 0, (
        f"{vertical}: forge --template {template} failed:\n"
        f"stdout={forge.stdout}\nstderr={forge.stderr}"
    )

    contract = _find_contract(tmp_path)
    assert contract is not None, f"{vertical}: no contract.fluid.yaml written"

    validate = _fluid("validate", str(contract), cwd=tmp_path)
    combined = (validate.stdout or "") + (validate.stderr or "")
    assert validate.returncode == 0, f"{vertical}: validate failed:\n{combined[-1500:]}"
