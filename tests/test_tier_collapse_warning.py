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

"""Pin the "tiered collapse with one-line warning" contract from the plan.

The plan's "Tiering stays, but **strictly within one provider**" section
promises:

> If the selected provider has no distinct tiers (e.g., Ollama, OpenAI
> where ``deep==balanced``), ``--tiered`` collapses to single-model
> automatically with a one-line warning. Never crashes, never asks for a
> second API key.

Until V2.5.4, that contract was implicit: ``BaseStageAgent.resolve_model``
silently fell through to the same model for every tier when the catalog
didn't distinguish them, but no warning fired and the per-stage banners
still claimed "deep" / "balanced" / "fast" — misleading the operator.

The fix lands two pieces:

* :func:`fluid_build.cli.forge_copilot_llm_providers.has_distinct_tier_models`
  — a pure catalog read that reports whether a provider has ≥2 distinct
  tier models. This is the single source of truth other code paths can
  consult without re-implementing the catalog walk.
* :func:`fluid_build.cli.forge_data_model._maybe_collapse_tiered_mode`
  — the policy gate that runs at session-build time and downgrades
  ``tiered=True`` to ``tiered=False`` when the catalog says collapse is
  the right move, emitting one ``logger.warning(...)`` line so the
  operator sees what happened.

The tests below pin both layers so a future catalog flip (e.g. adding
distinct Ollama tiers) doesn't silently stop the warning, and a future
refactor can't accidentally swallow the downgrade.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fluid_build.cli.forge_copilot_llm_providers import (
    has_distinct_tier_models,
)
from fluid_build.cli.forge_data_model import _maybe_collapse_tiered_mode

# ----------------------------------------------------------------------
# has_distinct_tier_models — pure catalog read
# ----------------------------------------------------------------------


class TestHasDistinctTierModels:
    def test_anthropic_has_distinct_tiers(self):
        """Anthropic ships with three distinct claude models — the
        canonical "tiered actually buys you something" case."""
        assert has_distinct_tier_models("anthropic") is True

    def test_openai_has_distinct_tiers(self):
        """OpenAI's tier map is ``deep=gpt-4.1, balanced=gpt-4.1-mini,
        fast=gpt-4.1-nano`` — three distinct models."""
        assert has_distinct_tier_models("openai") is True

    def test_gemini_has_distinct_tiers(self):
        """Gemini's tier map ships ``deep=gemini-2.5-pro,
        balanced=gemini-2.5-pro, fast=gemini-2.5-flash`` — two
        distinct models, which still qualifies as "tiered actually
        buys you something"."""
        assert has_distinct_tier_models("gemini") is True

    def test_unknown_provider_reports_false(self):
        """Conservative default: a provider absent from the catalog
        looks like "no distinct tiers" so ``--tiered`` collapses
        rather than silently falling through to whichever default
        the upstream resolver picks."""
        assert has_distinct_tier_models("does-not-exist") is False

    def test_provider_with_all_equal_tiers_reports_false(self, monkeypatch):
        """The collapse case — synthesise a catalog where ``ollama``
        has every tier pointing at the same model (the simplest
        regression of the plan's contract). Patch the cached loader
        so the test is hermetic."""

        synthetic_catalog = {
            "tiers": {
                "ollama": {
                    "deep": "llama3.1:8b",
                    "balanced": "llama3.1:8b",
                    "fast": "llama3.1:8b",
                }
            }
        }

        with patch(
            "fluid_build.cli.forge_copilot_llm_providers._load_model_catalog",
            return_value=synthetic_catalog,
        ):
            assert has_distinct_tier_models("ollama") is False

    def test_provider_with_two_equal_two_distinct_reports_true(self, monkeypatch):
        """Edge: ``deep == balanced`` but ``fast`` differs. That's
        still "tiered buys you something" because the routing tier
        is genuinely smaller — collapse is *not* warranted."""

        synthetic = {
            "tiers": {
                "vendorx": {
                    "deep": "x-large",
                    "balanced": "x-large",
                    "fast": "x-mini",
                }
            }
        }
        with patch(
            "fluid_build.cli.forge_copilot_llm_providers._load_model_catalog",
            return_value=synthetic,
        ):
            assert has_distinct_tier_models("vendorx") is True

    def test_provider_with_blank_tier_values_treated_as_missing(self):
        """Whitespace-only or empty-string tier values must not count
        as "distinct" — they're effectively unset and the collapse
        check should ignore them."""

        synthetic = {
            "tiers": {
                "vendory": {
                    "deep": "y-large",
                    "balanced": "  ",
                    "fast": "",
                }
            }
        }
        with patch(
            "fluid_build.cli.forge_copilot_llm_providers._load_model_catalog",
            return_value=synthetic,
        ):
            assert has_distinct_tier_models("vendory") is False


# ----------------------------------------------------------------------
# _maybe_collapse_tiered_mode — policy gate at session boundary
# ----------------------------------------------------------------------


def _make_logger(messages: list) -> logging.Logger:
    """Return a logger that appends every warning to ``messages``."""

    logger = logging.getLogger(f"test_tier_collapse_{id(messages)}")
    logger.setLevel(logging.WARNING)
    logger.handlers.clear()

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
            messages.append(record.getMessage())

    logger.addHandler(_ListHandler())
    logger.propagate = False
    return logger


class TestMaybeCollapseTieredMode:
    def test_returns_false_when_not_requested(self):
        """If the user didn't ask for tiered mode, nothing to collapse."""
        logger = _make_logger([])
        config = SimpleNamespace(provider="anthropic")
        assert _maybe_collapse_tiered_mode(False, config, logger) is False

    def test_returns_false_when_no_llm_config(self):
        """Heuristic-only runs (no LLM) cannot tier — the helper
        passes through ``requested_tiered`` unchanged so the heuristic
        path keeps its current behaviour. The actual stage code does
        not consult tiering when there's no LLM, so this is purely a
        boundary nicety."""
        logger = _make_logger([])
        # heuristic path: requested_tiered may legitimately be True
        # if the user passed --tiered but skipped the LLM; we don't
        # warn because there's nothing to warn about (no provider).
        assert _maybe_collapse_tiered_mode(True, None, logger) is True

    def test_keeps_tiered_for_distinct_provider(self):
        """Anthropic has three distinct tiers — no collapse, no warning."""
        messages: list = []
        logger = _make_logger(messages)
        config = SimpleNamespace(provider="anthropic")
        assert _maybe_collapse_tiered_mode(True, config, logger) is True
        assert messages == []

    def test_collapses_and_warns_for_synthetic_collapsed_provider(self):
        """The canonical regression — a provider whose catalog tiers
        all point at the same model. The helper must downgrade to
        ``False`` and emit exactly one warning."""
        messages: list = []
        logger = _make_logger(messages)
        config = SimpleNamespace(provider="ollama")

        synthetic = {
            "tiers": {
                "ollama": {
                    "deep": "llama3.1:8b",
                    "balanced": "llama3.1:8b",
                    "fast": "llama3.1:8b",
                }
            }
        }
        with patch(
            "fluid_build.cli.forge_copilot_llm_providers._load_model_catalog",
            return_value=synthetic,
        ):
            result = _maybe_collapse_tiered_mode(True, config, logger)
        assert result is False
        assert len(messages) == 1
        assert "ollama" in messages[0]
        assert "single-model" in messages[0]

    def test_unknown_provider_collapses_and_warns(self):
        """Unknown providers get the conservative downgrade so a typo
        in ``--llm-provider`` doesn't silently buy a deep tier the
        catalog can't actually deliver."""
        messages: list = []
        logger = _make_logger(messages)
        config = SimpleNamespace(provider="not-in-catalog")
        result = _maybe_collapse_tiered_mode(True, config, logger)
        assert result is False
        assert len(messages) == 1
        assert "not-in-catalog" in messages[0]


# ----------------------------------------------------------------------
# Catalog snapshot — guard against silent flips
# ----------------------------------------------------------------------


def test_catalog_ships_distinct_tiers_for_three_majors():
    """Sanity guard: at least Anthropic / OpenAI / Gemini must keep
    distinct tiers in ``llm_models.json``. A future PR that flattens
    them (e.g., setting ``balanced=deep`` for cost simplicity) flips
    the user-visible behaviour silently — this test forces an
    intentional decision."""
    for provider in ("anthropic", "openai", "gemini"):
        assert has_distinct_tier_models(provider) is True, (
            f"{provider} has no distinct tiers — if this is intentional, "
            "update test_catalog_ships_distinct_tiers_for_three_majors."
        )


def test_ollama_catalog_collapses_to_local_gemma4_today():
    """Ollama defaults to the single local Gemma 4 model.

    Local installs are model-inventory driven; pretending that Ollama
    has deep/balanced/fast tiers when only Gemma 4 is configured makes
    receipts misleading and can fail preflight on a user's machine.
    """
    assert has_distinct_tier_models("ollama") is False
