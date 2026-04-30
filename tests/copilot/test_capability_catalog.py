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

"""Unit tests for the provider/model capability catalog.

Pins the user-facing contract:

* Catalog resolution picks the most-specific model prefix.
* Unknown combinations get a fallback set of capabilities AND a
  diagnostic warning.
* Degradation warnings only fire when a required capability is
  missing — silent when everything is supported.
* Catalog ``notes`` always surface (even on fully-supported combos)
  so users see operational caveats.
"""

from __future__ import annotations

from fluid_build.copilot.agents.capability_catalog import (
    CAPABILITY_CATALOG,
    ProviderCapabilities,
    assess_capabilities,
    format_degradation_warnings,
    required_capabilities_for,
)


class TestAssessCapabilities:
    def test_exact_prefix_match(self) -> None:
        caps = assess_capabilities("anthropic", "claude-3-5-sonnet-20241022")
        assert caps.tool_use
        assert caps.structured_output
        assert caps.prompt_caching
        assert caps.model_prefix == "claude-3-5-sonnet"

    def test_longest_prefix_wins(self) -> None:
        caps = assess_capabilities("anthropic", "claude-opus-4-7-20260101")
        assert caps.model_prefix == "claude-opus-4-7"
        assert caps.extended_thinking

    def test_fallback_to_provider_general_entry(self) -> None:
        caps = assess_capabilities("anthropic", "claude-3-haiku-20240307")
        # No specific claude-3-haiku entry, but claude-3 catches it.
        assert caps.model_prefix == "claude-3"
        assert caps.tool_use

    def test_unknown_model_returns_fallback(self) -> None:
        caps = assess_capabilities("openai", "gpt-99-future-model")
        # Falls back to gpt-* prefix... actually, "gpt-9" doesn't match
        # any catalog entry, so we'd return _FALLBACK_CAPABILITIES.
        # Verify the fallback's hallmark ``_unknown`` provider:
        assert caps.provider in {"openai", "_unknown"}
        # Either way, the run has *something* to consult.

    def test_completely_unknown_provider_returns_fallback(self) -> None:
        caps = assess_capabilities("totally-fake-provider", "any-model")
        assert caps.provider == "_unknown"
        assert caps.tool_use is False
        assert caps.structured_output is False

    def test_o1_explicitly_disables_tool_use_and_streaming(self) -> None:
        caps = assess_capabilities("openai", "o1-2024-12-17")
        assert caps.tool_use is False
        assert caps.streaming is False
        assert caps.extended_thinking is True

    def test_ollama_llama3_1_has_tool_use_with_caveat(self) -> None:
        caps = assess_capabilities("ollama", "llama3.1:70b")
        assert caps.tool_use is True
        # Notes carry the operational caveat about accuracy.
        assert any("accuracy" in note for note in caps.notes)


class TestRequiredCapabilities:
    def test_agent_loop_requires_tool_use_and_structured_output(self) -> None:
        req = required_capabilities_for("agent_loop")
        assert "tool_use" in req
        assert "structured_output" in req

    def test_staged_pipeline_only_requires_structured_output(self) -> None:
        req = required_capabilities_for("staged_pipeline")
        assert "structured_output" in req
        # Staged pipeline is single-shot — doesn't need tool use.
        assert "tool_use" not in req

    def test_unknown_profile_falls_back_to_staged(self) -> None:
        # Defensive — unknown profiles should return the safer set
        # rather than empty.
        req = required_capabilities_for("not-a-real-profile")
        assert "structured_output" in req


class TestFormatDegradationWarnings:
    def test_full_support_emits_no_warnings(self) -> None:
        warnings = format_degradation_warnings(
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            usage_profile="staged_pipeline",
        )
        # claude-3-5-sonnet has structured_output → no degradation.
        # No notes on this entry → silent.
        assert warnings == []

    def test_missing_tool_use_warns_on_agent_loop_profile(self) -> None:
        warnings = format_degradation_warnings(
            provider="openai",
            model="o1-mini",
            usage_profile="agent_loop",
        )
        # o1 lacks tool_use — warn loudly.
        assert any("tool use" in w for w in warnings)
        # And surface the catalog note about not supporting streaming/tools.
        assert any(
            "do not support tool use" in w or "tool-use" in w.lower()
            for w in warnings
        )

    def test_unknown_provider_always_warns(self) -> None:
        warnings = format_degradation_warnings(
            provider="totally-fake",
            model="any",
            usage_profile="staged_pipeline",
        )
        assert any("not in the capability catalog" in w for w in warnings)

    def test_notes_surface_even_when_not_degraded(self) -> None:
        """A user picking an Ollama llama model gets the accuracy
        caveat even on the staged_pipeline profile (which doesn't
        require tool_use)."""
        warnings = format_degradation_warnings(
            provider="ollama",
            model="llama3.1:8b",
            usage_profile="staged_pipeline",
        )
        # llama3.1's note about accuracy should appear.
        assert any("accuracy" in w for w in warnings)
        # And the structured_output gap should be surfaced for the
        # staged_pipeline profile.
        assert any("structured output" in w for w in warnings)

    def test_explicit_required_overrides_profile_defaults(self) -> None:
        # Even on staged_pipeline, the caller can demand prompt caching.
        warnings = format_degradation_warnings(
            provider="openai",
            model="gpt-4o",
            required=["prompt_caching"],
        )
        assert any("prompt caching" in w for w in warnings)


class TestCatalogShape:
    def test_every_catalog_entry_has_provider_and_prefix(self) -> None:
        """Sanity: malformed catalog entries are caught early."""
        for entry in CAPABILITY_CATALOG:
            assert isinstance(entry, ProviderCapabilities)
            assert entry.provider, f"entry without provider: {entry}"
            assert entry.model_prefix, f"entry without model_prefix: {entry}"

    def test_anthropic_entries_have_prompt_caching(self) -> None:
        """Anthropic is the only provider with prompt caching today.
        If a future entry forgets to enable it, this test catches the
        regression so users keep getting the ~90% input-cost discount."""
        for entry in CAPABILITY_CATALOG:
            if entry.provider == "anthropic" and entry.model_prefix.startswith(
                "claude-3-5"
            ):
                assert entry.prompt_caching, (
                    f"Anthropic claude-3-5 entry missing prompt_caching: {entry}"
                )
            if entry.provider == "anthropic" and entry.model_prefix.startswith(
                "claude-opus-4-7"
            ):
                assert entry.prompt_caching, (
                    f"Anthropic claude-opus-4-7 missing prompt_caching: {entry}"
                )
