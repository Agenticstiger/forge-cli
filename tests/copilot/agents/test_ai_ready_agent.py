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

"""Tests for the built-in ``ai_ready`` Forge agent + metadata-enforcement core.

Two surfaces are pinned here:

* the **metadata-enforcement core** (``enforce_ai_ready``) — a deterministic,
  no-LLM pass that annotates a contract to be AI-ready (agentPolicy on every
  output port, PII/sensitivity column flags, embedding hints, description
  completeness);
* the **registration** as a first-class built-in domain agent alongside the
  finance / healthcare / … verticals.
"""

from __future__ import annotations

import copy

import pytest

from fluid_build.copilot.agents.ai_ready_agent import (
    AI_READY_ENV,
    AiReadyError,
    AiReadyReport,
    ai_ready_enabled,
    enforce_ai_ready,
)


def _contract(**overrides):
    """A minimal FLUID v0.7.5-shaped contract with one output port."""
    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "sales.customer_360",
        "name": "Customer 360",
        "description": "Consumption-aligned customer profiles for downstream AI.",
        "domain": "sales",
        "metadata": {
            "productType": "CDP",
            "owner": {"team": "data-platform", "email": "dp@example.com"},
        },
        "exposes": [
            {
                "exposeId": "customer_profiles",
                "kind": "table",
                "version": "1.0.0",
                "description": "One row per customer with churn features.",
                "contract": {
                    "schema": [
                        {
                            "name": "customer_id",
                            "type": "string",
                            "required": True,
                            "description": "Stable customer identifier.",
                        },
                        {
                            "name": "customer_email",
                            "type": "string",
                            "description": "Primary contact email.",
                        },
                        {
                            "name": "lifetime_summary",
                            "type": "text",
                            "description": "Free-text narrative of the customer relationship.",
                        },
                    ]
                },
            }
        ],
    }
    contract.update(overrides)
    return contract


class TestEnforceCore:
    def test_returns_report(self):
        report = enforce_ai_ready(_contract())
        assert isinstance(report, AiReadyReport)
        assert report.enabled is True

    def test_sets_agent_policy_on_every_expose(self):
        contract = _contract()
        enforce_ai_ready(contract)
        pol = contract["exposes"][0]["policy"]["agentPolicy"]
        # Audit is always on for AI consumption.
        assert pol["auditRequired"] is True
        # Sensible default read use-cases for AI consumption.
        assert "rag" in pol["allowedUseCases"]
        assert "customer_profiles" in enforce_ai_ready(_contract()).exposes_annotated

    def test_pii_columns_flagged(self):
        contract = _contract()
        report = enforce_ai_ready(contract)
        cols = {c["name"]: c for c in contract["exposes"][0]["contract"]["schema"]}
        assert cols["customer_email"]["sensitivity"] == "pii"
        assert any(t.startswith("pii-") for t in cols["customer_email"]["tags"])
        assert report.pii_columns >= 1

    def test_sensitive_expose_denies_training(self):
        contract = _contract()
        enforce_ai_ready(contract)
        pol = contract["exposes"][0]["policy"]["agentPolicy"]
        # The port carries PII (customer_email) → reporting-yes / training-no.
        assert "training" in pol["deniedUseCases"]
        assert "fine_tuning" in pol["deniedUseCases"]
        assert pol["canStore"] is False

    def test_non_sensitive_expose_allows_storage(self):
        contract = _contract()
        # Strip the PII column so the port is non-sensitive.
        contract["exposes"][0]["contract"]["schema"] = [
            {"name": "region", "type": "string", "description": "Sales region."}
        ]
        enforce_ai_ready(contract)
        pol = contract["exposes"][0]["policy"]["agentPolicy"]
        assert pol.get("deniedUseCases", []) == []
        assert pol["canStore"] is True

    def test_embeddable_columns_reported_and_labelled(self):
        contract = _contract()
        report = enforce_ai_ready(contract)
        # Text column with a description, not sensitive → embeddable.
        assert any("lifetime_summary" in c for c in report.embeddable_columns)
        col = next(
            c
            for c in contract["exposes"][0]["contract"]["schema"]
            if c["name"] == "lifetime_summary"
        )
        assert col["labels"]["ai-embeddable"] == "true"
        # PII text columns must NOT be marked embeddable.
        email = next(
            c for c in contract["exposes"][0]["contract"]["schema"] if c["name"] == "customer_email"
        )
        assert "ai-embeddable" not in (email.get("labels") or {})
        # Identifier columns must NOT be marked embeddable.
        cid = next(
            c for c in contract["exposes"][0]["contract"]["schema"] if c["name"] == "customer_id"
        )
        assert "ai-embeddable" not in (cid.get("labels") or {})
        assert not any("customer_id" in c for c in report.embeddable_columns)

    def test_missing_descriptions_reported(self):
        contract = _contract()
        # Blank out one column description.
        contract["exposes"][0]["contract"]["schema"][0]["description"] = ""
        report = enforce_ai_ready(contract)
        assert any("customer_id" in m for m in report.missing_descriptions)
        assert report.is_ai_ready is False

    def test_strict_raises_on_missing_description(self):
        contract = _contract()
        contract["exposes"][0]["contract"]["schema"][0]["description"] = ""
        with pytest.raises(AiReadyError):
            enforce_ai_ready(contract, strict=True)

    def test_allowed_models_normalised_and_set(self):
        contract = _contract()
        enforce_ai_ready(contract, allowed_models=["GPT-4", "Claude 3 Opus", "gemini-pro"])
        pol = contract["exposes"][0]["policy"]["agentPolicy"]
        assert "gpt-4" in pol["allowedModels"]
        assert "claude-3-opus" in pol["allowedModels"]
        assert "gemini-pro" in pol["allowedModels"]

    def test_root_ai_ready_label(self):
        contract = _contract()
        enforce_ai_ready(contract)
        assert contract["labels"]["ai-ready"] == "true"

    def test_idempotent(self):
        contract = _contract()
        enforce_ai_ready(contract)
        once = copy.deepcopy(contract)
        enforce_ai_ready(contract)
        assert contract == once

    def test_conservative_preserves_existing_agent_policy(self):
        contract = _contract()
        contract["exposes"][0].setdefault("policy", {})["agentPolicy"] = {
            "allowedModels": ["my-inhouse-llm"],
            "canStore": True,
        }
        enforce_ai_ready(contract)
        pol = contract["exposes"][0]["policy"]["agentPolicy"]
        # Existing operator-set values are never stomped.
        assert pol["allowedModels"] == ["my-inhouse-llm"]
        assert pol["canStore"] is True

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv(AI_READY_ENV, "0")
        assert ai_ready_enabled() is False
        contract = _contract()
        report = enforce_ai_ready(contract)
        assert report.enabled is False
        assert "policy" not in contract["exposes"][0]

    def test_no_exposes_is_safe(self):
        report = enforce_ai_ready({"fluidVersion": "0.7.5", "exposes": []})
        assert report.exposes_annotated == []

    def test_report_to_dict(self):
        report = enforce_ai_ready(_contract())
        d = report.to_dict()
        assert d["enabled"] is True
        assert "pii_columns" in d
        assert "embeddable_columns" in d


class TestRegistration:
    def test_ai_ready_in_domain_agents(self):
        from fluid_build.cli.forge_agents import DOMAIN_AGENTS

        assert "ai_ready" in DOMAIN_AGENTS

    def test_get_agent_resolves(self):
        from fluid_build.cli.forge_agents import AiReadyAgent, get_agent

        agent = get_agent("ai_ready")
        assert isinstance(agent, AiReadyAgent)
        assert agent.name == "ai_ready"

    def test_listed_as_builtin(self):
        from fluid_build.cli.forge_agents import get_all_domain_names, list_agents

        assert "ai_ready" in get_all_domain_names()
        entry = next(a for a in list_agents() if a["name"] == "ai_ready")
        assert entry["source"] == "built-in"

    def test_agent_enforce_delegates_to_core(self):
        from fluid_build.cli.forge_agents import AiReadyAgent

        contract = _contract()
        report = AiReadyAgent().enforce_ai_ready(contract)
        assert isinstance(report, AiReadyReport)
        assert contract["exposes"][0]["policy"]["agentPolicy"]["auditRequired"] is True

    def test_spec_loads_and_validates(self):
        from fluid_build.cli.forge_agent_specs import load_builtin_agent_spec

        spec = load_builtin_agent_spec("ai_ready")
        assert spec.name == "ai_ready"
        assert spec.questions  # non-empty
        assert spec.suggestion_defaults["recommended_template"]


def _full_contract():
    """A complete, schema-valid v0.7.5 contract (exposes carry a binding)."""
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "sales.customer_360",
        "name": "Customer 360",
        "description": "Consumption-aligned customer profiles for downstream AI.",
        "domain": "sales",
        "metadata": {
            "productType": "CDP",
            "owner": {"team": "data-platform", "email": "dp@example.com"},
        },
        "exposes": [
            {
                "exposeId": "customer_profiles",
                "kind": "table",
                "version": "1.0.0",
                "description": "One row per customer with churn features.",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "project": "acme-analytics",
                        "dataset": "sales",
                        "table": "customer_profiles",
                    },
                },
                "contract": {
                    "schema": [
                        {
                            "name": "customer_id",
                            "type": "string",
                            "required": True,
                            "description": "Stable customer identifier.",
                        },
                        {
                            "name": "customer_email",
                            "type": "string",
                            "description": "Primary contact email.",
                        },
                        {
                            "name": "lifetime_summary",
                            "type": "text",
                            "description": "Free-text narrative of the relationship.",
                        },
                    ]
                },
            }
        ],
    }


class TestSchemaValidity:
    def test_enforced_contract_validates_against_schema(self):
        from fluid_build.schema_manager import FluidSchemaManager

        contract = _full_contract()
        enforce_ai_ready(contract, allowed_models=["gpt-4", "claude-3-opus"])
        result = FluidSchemaManager().validate_contract(
            contract, schema_version="0.7.5", offline_only=True
        )
        assert result.is_valid, [getattr(e, "message", str(e)) for e in result.errors]

    def test_enforced_contract_carries_agent_policy(self):
        contract = _full_contract()
        enforce_ai_ready(contract, allowed_models=["gpt-4"])
        ap = contract["exposes"][0]["policy"]["agentPolicy"]
        assert ap["allowedModels"] == ["gpt-4"]
        assert "training" in ap["deniedUseCases"]
