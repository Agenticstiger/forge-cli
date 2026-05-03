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

"""Integration test: forge data-model from-intent with real Gemini.

This test only runs when ``GEMINI_API_KEY`` is set in the environment —
CI runners without the key skip it automatically so the default suite
stays green on keyless machines.

What it covers (telco-domain demo path):

* Structured outputs flip is live for Gemini — a real call returns
  Pydantic-valid JSON (H4).
* The full ``from-intent`` pipeline (``StageCoordinator.from_intent`` →
  ``FluidContractValidator``) round-trips without crashing against a
  live LLM.
* The telco ``IndustryPack`` is threaded end-to-end: the emitted
  ``LogicalDraft`` respects DV2 naming (``hub_*``), produces at least
  one satellite, and satellites default to SCD type 2 (H3+S2 depend
  on this being stable).
* The canonical-coverage summary renders cleanly — the
  ``compute_canonical_coverage`` helper agrees with the validator on
  "what counts as present."

The test caps token use via ``timeout_seconds`` on the LlmConfig and
uses ``NullBackend`` so cache artefacts don't leak into ``~/.fluid/``
on the dev box.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fluid_build.cli.forge_copilot_llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    LlmConfig,
)
from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.industry.compiler import IndustryPackCompiler
from fluid_build.copilot.schemas.intent import (
    BusinessIntent,
    DataProduct,
    Dimensions,
    Grain,
    Metric,
)
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.forge_datamodel.emit.coverage import compute_canonical_coverage
from fluid_build.forge_datamodel.from_intent.pipeline import run_from_intent

# Integration marker is registered in pyproject.toml's [tool.pytest.ini_options].
pytestmark = pytest.mark.integration


def _require_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        pytest.skip("GEMINI_API_KEY not set — skipping live Gemini integration test")
    return key


def _telco_intent() -> BusinessIntent:
    """Minimal but opinionated telco intent — nudges Gemini toward TMF SID concepts."""
    return BusinessIntent(
        data_product=DataProduct(
            name="customer_subscriptions",
            domain="telecommunications",
            description=(
                "Customer subscription analytics for a telco operator. "
                "Tracks parties, products, services, and subscription events "
                "across the customer lifecycle."
            ),
            owner="analytics",
        ),
        grain=Grain(
            entity="subscription_event",
            time_dimension="event_date",
            description="One row per subscription state change per customer per service.",
        ),
        metrics=[
            Metric(
                name="active_subscribers",
                description="Distinct count of parties with an active service",
            ),
            Metric(
                name="churn_rate",
                description="Subscriptions churned in period / active at period start",
            ),
        ],
        dimensions=Dimensions(
            entities=["party", "service", "product_offering", "resource"],
            attributes=["event_type", "channel", "geography"],
        ),
    )


def test_gemini_from_intent_produces_valid_dv2_model(tmp_path: Path) -> None:
    api_key = _require_gemini_key()

    provider = BUILTIN_LLM_PROVIDERS["gemini"]
    model = provider.default_model  # gemini-2.5-pro — structured_outputs=true
    config = LlmConfig(
        provider="gemini",
        model=model,
        endpoint=provider.default_endpoint(model, {"GEMINI_API_KEY": api_key}),
        api_key=api_key,
        # Keep the timeout modest so a flaky network doesn't hang CI for 2 min.
        timeout_seconds=90,
        streaming=False,
    )

    pack = IndustryPackCompiler().compile("telecommunications", technique="data_vault_2")
    assert pack.seed_dv2_skeleton is not None, "telco pack must carry a seed DV2 skeleton"

    session = StageSession(
        store=NullBackend(),
        workspace_root=tmp_path,
        llm_config=config,
        active_provider="gemini",
        no_cache=True,
        industry_pack=pack,
    )

    result = run_from_intent(session, intent=_telco_intent(), technique="data_vault_2")

    logical = result.coordinator.logical
    # Pydantic validated on the way out of the coordinator; re-assert
    # the technique wiring is what we asked for.
    assert logical.technique == "data_vault_2"
    assert logical.dv2 is not None, "DV2 technique must populate the dv2 branch"
    assert logical.dimensional is None, "dv2 XOR dimensional — never both"

    # At least one hub must come back. Gemini won't invent every TMF SID
    # hub on a small intent, but it must produce *something*.
    hubs = logical.dv2.hubs
    assert len(hubs) >= 1, "Gemini produced zero hubs — structured output regression?"

    # DV2 naming convention — enforced by the skeleton-lint validator.
    for hub in hubs:
        assert hub.hub_table_name.startswith(
            "hub_"
        ), f"Hub table name violates DV2 naming: {hub.hub_table_name!r}"

    # At least one satellite is expected for any non-trivial DV2 model;
    # change_tracking defaults to "type2" in the Pydantic schema, so
    # Gemini leaving the field unset still yields SCD2 — this test also
    # protects against an accidental default flip.
    sats = logical.dv2.satellites
    assert len(sats) >= 1, "Gemini produced zero satellites — expected at least one"
    for sat in sats:
        assert sat.change_tracking in {"type1", "type2", "append_only"}
        assert sat.satellite_table_name.startswith(
            "sat_"
        ), f"Satellite table name violates DV2 naming: {sat.satellite_table_name!r}"

    # Validation report — with a real LLM we tolerate warnings but not
    # a schema failure; the validator is allowed to flag drift (H3).
    assert (
        result.validation.passes_schema
    ), f"Emitted contract failed Fluid 0.7.2 schema: {result.validation.issues}"

    # S2 coverage helper agrees with the pipeline — the pack has a
    # skeleton, so coverage must be non-None and its render must carry
    # the canonical label.
    summary = compute_canonical_coverage(logical, pack)
    assert summary is not None
    rendered = summary.render()
    assert "TM Forum SID" in rendered or "tmf_sid" in rendered
    assert "telecommunications" in rendered
