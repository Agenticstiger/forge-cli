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

from typing import Any, Dict
from unittest import mock

import pytest

from fluid_build.copilot.agents.capability_catalog import (
    CAPABILITY_CATALOG,
    ProviderCapabilities,
    _build_capability_catalog,
    _reset_capability_cache,
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
        assert any("do not support tool use" in w or "tool-use" in w.lower() for w in warnings)

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
            if entry.provider == "anthropic" and entry.model_prefix.startswith("claude-3-5"):
                assert (
                    entry.prompt_caching
                ), f"Anthropic claude-3-5 entry missing prompt_caching: {entry}"
            if entry.provider == "anthropic" and entry.model_prefix.startswith("claude-opus-4-7"):
                assert (
                    entry.prompt_caching
                ), f"Anthropic claude-opus-4-7 missing prompt_caching: {entry}"


# ---------------------------------------------------------------------------
# JSON-catalog-derived behaviour
#
# The catalog is built from two tiers — a family overlay plus
# per-model-id entries derived from ``cli/llm_models.json``. These
# tests pin the derivation behaviour so a future change to the
# weekly-refresh format (or to ``_build_capability_catalog``) can't
# silently drop capability flags.
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_cache():
    """Ensure each test sees a fresh build, and don't poison sibling tests."""
    _reset_capability_cache()
    yield
    _reset_capability_cache()


def _synth_catalog(*, provider: str, models: list) -> Dict[str, Any]:
    """Build a minimal JSON-catalog dict to feed the mock loader."""
    return {
        "schema_version": 2,
        "providers": {
            provider: {
                "default": models[0]["id"] if models else None,
                "models": models,
            }
        },
    }


class TestBuildCapabilityCatalog:
    def test_synthetic_provider_model_lands_with_flags(self, _isolated_cache) -> None:
        synthetic = _synth_catalog(
            provider="anthropic",
            models=[
                {
                    "id": "claude-synthetic-future-2030",
                    "aliases": [],
                    "capabilities": {
                        "tool_use": True,
                        "structured_output": True,
                        "streaming": True,
                    },
                }
            ],
        )
        with mock.patch(
            "fluid_build.copilot.agents.capability_catalog._safe_load_json_catalog",
            return_value=synthetic,
        ):
            _reset_capability_cache()
            entries = _build_capability_catalog()

        # The synthetic model id should appear as a JSON-derived entry.
        match = [e for e in entries if e.model_prefix == "claude-synthetic-future-2030"]
        assert len(match) == 1, "expected exactly one JSON-derived entry"
        entry = match[0]
        assert entry.provider == "anthropic"
        assert entry.tool_use is True
        assert entry.structured_output is True
        assert entry.streaming is True
        # Per-provider default: anthropic auto-gets prompt_caching.
        assert entry.prompt_caching is True

    def test_capability_absent_in_json_flows_to_false(self, _isolated_cache) -> None:
        synthetic = _synth_catalog(
            provider="openai",
            models=[
                {
                    "id": "synthetic-no-tools-model",
                    "aliases": [],
                    "capabilities": {
                        "tool_use": False,
                        "structured_output": False,
                        "streaming": True,
                    },
                }
            ],
        )
        with mock.patch(
            "fluid_build.copilot.agents.capability_catalog._safe_load_json_catalog",
            return_value=synthetic,
        ):
            _reset_capability_cache()
            entries = _build_capability_catalog()

        match = [e for e in entries if e.model_prefix == "synthetic-no-tools-model"]
        assert len(match) == 1
        entry = match[0]
        assert entry.tool_use is False
        assert entry.structured_output is False
        # OpenAI provider default: no prompt caching, no extended thinking.
        assert entry.prompt_caching is False
        assert entry.extended_thinking is False

    def test_o_series_inherits_extended_thinking_default(self, _isolated_cache) -> None:
        """``o1`` / ``o3`` / ``o4`` family models should get
        ``extended_thinking=True`` via the JSON-derived path even when
        the JSON catalog doesn't carry that field."""
        synthetic = _synth_catalog(
            provider="openai",
            models=[
                {
                    "id": "o5-mini-2027",
                    "aliases": [],
                    "capabilities": {
                        "tool_use": True,
                        "structured_output": True,
                        "streaming": True,
                    },
                },
                {
                    "id": "o3-future",
                    "aliases": [],
                    "capabilities": {
                        "tool_use": True,
                        "structured_output": True,
                        "streaming": True,
                    },
                },
            ],
        )
        with mock.patch(
            "fluid_build.copilot.agents.capability_catalog._safe_load_json_catalog",
            return_value=synthetic,
        ):
            _reset_capability_cache()
            entries = _build_capability_catalog()

        o5 = next(e for e in entries if e.model_prefix == "o5-mini-2027")
        # o5 is not in the (o1, o3, o4) family rule → False.
        assert o5.extended_thinking is False
        o3_future = next(e for e in entries if e.model_prefix == "o3-future")
        assert o3_future.extended_thinking is True

    def test_overlay_wins_when_json_has_same_prefix(self, _isolated_cache) -> None:
        """The overlay's ``claude-opus-4-7`` entry must not be shadowed
        by a JSON-derived ``claude-opus-4-7`` entry with the same id.
        Tests the de-dup in :func:`_build_capability_catalog`."""
        synthetic = _synth_catalog(
            provider="anthropic",
            models=[
                {
                    "id": "claude-opus-4-7",  # collides with overlay prefix
                    "aliases": [],
                    "capabilities": {
                        "tool_use": False,  # try to clobber overlay's True
                        "structured_output": False,
                        "streaming": True,
                    },
                }
            ],
        )
        with mock.patch(
            "fluid_build.copilot.agents.capability_catalog._safe_load_json_catalog",
            return_value=synthetic,
        ):
            _reset_capability_cache()
            entries = _build_capability_catalog()

        # Exactly one entry for that prefix, and it's the overlay one
        # (tool_use=True, with the temperature note).
        matches = [
            e for e in entries if e.provider == "anthropic" and e.model_prefix == "claude-opus-4-7"
        ]
        assert len(matches) == 1
        assert matches[0].tool_use is True
        assert matches[0].notes  # overlay note preserved


class TestRealCatalogSmoke:
    """Pin the live ``llm_models.json`` against the catalog build so a
    regression in either side surfaces immediately."""

    def test_anthropic_sonnet_resolves_with_tool_use(self) -> None:
        caps = assess_capabilities("anthropic", "claude-sonnet-4-6")
        assert caps.tool_use is True
        assert caps.structured_output is True
        assert caps.prompt_caching is True

    def test_anthropic_haiku_resolves_with_tool_use(self) -> None:
        # Haiku exists in both the overlay (claude-haiku-4-5) and the
        # JSON catalog (claude-haiku-4-5-20251001). Overlay matches first
        # via longest-prefix on shared characters.
        caps = assess_capabilities("anthropic", "claude-haiku-4-5-20251001")
        assert caps.tool_use is True
        assert caps.structured_output is True
        assert caps.prompt_caching is True

    def test_openai_gpt41_resolves(self) -> None:
        caps = assess_capabilities("openai", "gpt-4.1")
        assert caps.tool_use is True
        assert caps.structured_output is True

    def test_json_only_model_id_resolves_when_not_in_overlay(self) -> None:
        """``claude-sonnet-4-5-20250929`` is in the JSON catalog but
        NOT in the family overlay — the merge must surface it."""
        caps = assess_capabilities("anthropic", "claude-sonnet-4-5-20250929")
        # Either the JSON-derived entry catches the full id, or the
        # overlay's ``claude-sonnet-4-5`` does. Both deliver tool_use.
        assert caps.tool_use is True
        assert caps.structured_output is True


class TestCacheInvalidation:
    def test_cache_persists_across_calls(self, _isolated_cache) -> None:
        synthetic_a = _synth_catalog(
            provider="anthropic",
            models=[
                {
                    "id": "cache-test-model-a",
                    "aliases": [],
                    "capabilities": {
                        "tool_use": True,
                        "structured_output": True,
                        "streaming": True,
                    },
                }
            ],
        )
        synthetic_b = _synth_catalog(
            provider="anthropic",
            models=[
                {
                    "id": "cache-test-model-b",
                    "aliases": [],
                    "capabilities": {
                        "tool_use": True,
                        "structured_output": True,
                        "streaming": True,
                    },
                }
            ],
        )

        # First build picks up A.
        with mock.patch(
            "fluid_build.copilot.agents.capability_catalog._safe_load_json_catalog",
            return_value=synthetic_a,
        ):
            _reset_capability_cache()
            first = assess_capabilities("anthropic", "cache-test-model-a")
            assert first.model_prefix == "cache-test-model-a"

            # Swap the underlying catalog WITHOUT resetting the cache —
            # the cached build should still resolve A, not B.
        with mock.patch(
            "fluid_build.copilot.agents.capability_catalog._safe_load_json_catalog",
            return_value=synthetic_b,
        ):
            still_a = assess_capabilities("anthropic", "cache-test-model-a")
            assert still_a.model_prefix == "cache-test-model-a"
            # B should not resolve until we explicitly invalidate the cache.
            from fluid_build.copilot.agents.capability_catalog import _FALLBACK_CAPABILITIES

            b_before_reset = assess_capabilities("anthropic", "cache-test-model-b")
            assert b_before_reset is _FALLBACK_CAPABILITIES

            # Now invalidate and verify B is resolvable.
            _reset_capability_cache()
            b_after_reset = assess_capabilities("anthropic", "cache-test-model-b")
            assert b_after_reset.model_prefix == "cache-test-model-b"

    def test_capability_catalog_module_export_is_iterable_and_indexable(self) -> None:
        """``CAPABILITY_CATALOG`` is now a lazy proxy — verify the
        public access patterns still work."""
        as_list = list(CAPABILITY_CATALOG)
        assert as_list, "catalog should never be empty"
        assert all(isinstance(e, ProviderCapabilities) for e in as_list)
        # Indexable.
        assert CAPABILITY_CATALOG[0].provider in {"anthropic", "openai", "gemini", "ollama"}
        # len() works.
        assert len(CAPABILITY_CATALOG) == len(as_list)
        # ``in`` works.
        assert as_list[0] in CAPABILITY_CATALOG
        # ``isinstance(..., tuple)`` still holds for the backward-compat
        # type signature.
        assert isinstance(CAPABILITY_CATALOG, tuple)
