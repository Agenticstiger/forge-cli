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

"""Pins for the central error catalog + slug enrichment (Error-UX card).

Guarantees:
* every catalogued event ships a non-empty, actionable suggestions list;
* ``slug_for`` produces a stable ``ERR_<EVENT>`` code;
* ``CLIError`` and its ``FluidCLIError`` / ``CopilotGenerationError`` subclasses
  auto-enrich ``error_slug`` / ``suggestions`` / ``docs_url`` from the catalog,
  with caller-supplied values always winning;
* the rendered failure surface carries the slug.
"""

from __future__ import annotations

import pytest

from fluid_build.cli import _error_catalog as cat
from fluid_build.cli._common import CLIError
from fluid_build.cli.core import FluidCLIError


# ── slug_for ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("provider_not_specified", "ERR_PROVIDER_NOT_SPECIFIED"),
        ("contract_not_found", "ERR_CONTRACT_NOT_FOUND"),
        ("copilot_missing_llm_api_key", "ERR_COPILOT_MISSING_LLM_API_KEY"),
        ("opentofu_plan_failed", "ERR_OPENTOFU_PLAN_FAILED"),
    ],
)
def test_slug_for_is_stable_upper_err_code(event, expected):
    assert cat.slug_for(event) == expected


def test_slug_for_normalizes_messy_input():
    assert cat.slug_for("  weird--event name ") == "ERR_WEIRD_EVENT_NAME"
    assert cat.slug_for("") == "ERR_UNKNOWN"


# ── catalog completeness ────────────────────────────────────────────────────
def test_every_catalogued_event_has_nonempty_suggestions():
    """The card's contract: every error code has at least one suggestion."""
    missing = [e for e in cat.catalogued_events() if not cat.suggestions_for(e)]
    assert missing == [], f"catalogued events with no suggestions: {missing}"


def test_every_catalogued_event_has_a_docs_url():
    missing = [e for e in cat.catalogued_events() if not cat.docs_url_for(e)]
    assert missing == [], f"catalogued events with no docs_url: {missing}"


def test_docs_urls_are_well_formed_under_doc_base():
    for event in cat.catalogued_events():
        url = cat.docs_url_for(event)
        assert url is not None and url.startswith(cat.DOC_BASE + "/")


# ── enrich() caller-wins semantics ──────────────────────────────────────────
def test_enrich_fills_blanks_from_catalog():
    suggestions, docs = cat.enrich("provider_not_specified", None, None)
    assert suggestions and any("fluid providers" in s for s in suggestions)
    assert docs and docs.startswith(cat.DOC_BASE)


def test_enrich_caller_values_win():
    suggestions, docs = cat.enrich("provider_not_specified", ["do X"], "http://x")
    assert suggestions == ["do X"]
    assert docs == "http://x"


def test_enrich_unknown_event_returns_empty():
    suggestions, docs = cat.enrich("totally_made_up_event", None, None)
    assert suggestions == []
    assert docs is None


# ── CLIError auto-enrichment ────────────────────────────────────────────────
def test_cli_error_auto_enriches_catalogued_event():
    err = CLIError(2, "provider_not_specified")
    assert err.error_slug == "ERR_PROVIDER_NOT_SPECIFIED"
    assert err.suggestions  # populated from the catalog
    assert err.docs_url and err.docs_url.startswith(cat.DOC_BASE)


def test_cli_error_uncatalogued_event_still_gets_a_slug():
    err = CLIError(1, "some_unlisted_event")
    assert err.error_slug == "ERR_SOME_UNLISTED_EVENT"
    assert err.suggestions == []
    assert err.docs_url is None


# ── FluidCLIError uses the catalog as single source ─────────────────────────
def test_fluid_cli_error_fills_from_catalog_when_caller_omits():
    err = FluidCLIError(1, "contract_not_found", "Contract file not found: x.yaml")
    assert err.error_slug == "ERR_CONTRACT_NOT_FOUND"
    assert err.suggestions  # from the catalog, not a hardcoded dict
    assert err.docs_url


def test_fluid_cli_error_caller_suggestions_win_over_catalog():
    err = FluidCLIError(1, "provider_not_specified", "boom", suggestions=["my custom hint"])
    assert err.suggestions == ["my custom hint"]


def test_fluid_cli_error_format_renders_slug(capsys):
    from rich.console import Console

    console = Console(force_terminal=False, no_color=True)
    FluidCLIError(1, "provider_not_specified", "no provider").format_for_user(console)
    out = capsys.readouterr().out
    assert "ERR_PROVIDER_NOT_SPECIFIED" in out
    assert "💡 Suggestions" in out
    assert cat.DOC_BASE in out


# ── CopilotGenerationError (CLIError subclass) ──────────────────────────────
def test_copilot_error_keeps_caller_suggestions_and_gains_slug_and_docs():
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

    err = CopilotGenerationError(
        "copilot_missing_llm_api_key",
        "No API key configured",
        suggestions=["Run 'fluid ai setup'"],
    )
    # caller suggestions preserved …
    assert err.suggestions == ["Run 'fluid ai setup'"]
    # … and the stable slug + catalog docs_url are still attached
    assert err.error_slug == "ERR_COPILOT_MISSING_LLM_API_KEY"
    assert err.docs_url and err.docs_url.startswith(cat.DOC_BASE)


def test_copilot_error_fills_suggestions_from_catalog_when_omitted():
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

    err = CopilotGenerationError("copilot_missing_llm_api_key", "boom")
    assert err.suggestions  # catalog fallback, not []
    assert any("fluid ai setup" in s for s in err.suggestions)


def test_catalog_covers_common_command_failures():
    """Coverage breadth: the high-traffic command-failure events ship guidance.

    Extends #302's initial 24 events to the common plan/apply/generate/policy/
    product/loader/bundle/market/signing/schedule failures (Error-UX coverage
    follow-up). Each must carry a non-empty suggestion + a docs_url.
    """
    expected = {
        "generate_iac_failed",
        "policy_apply_failed",
        "product_add_failed",
        "loader_import_failed",
        "market_discovery_failed",
        "signing_bundle_missing",
        "schedule_sync_dags_dir_missing",
        "no_builds",
        "model_not_found",
        "missing_contract",
    }
    catalogued = set(cat.catalogued_events())
    missing = expected - catalogued
    assert not missing, f"expected catalogued: {missing}"
    assert len(catalogued) >= 45, f"catalog regressed to {len(catalogued)} events"
    for ev in expected:
        assert cat.suggestions_for(ev), ev
        assert cat.docs_url_for(ev), ev
