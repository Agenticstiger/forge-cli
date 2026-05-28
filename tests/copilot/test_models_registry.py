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

"""Unit coverage for ``fluid_build.copilot.models``.

The registry is now a thin role -> tier shim over ``cli/llm_models.json``
(see :mod:`tests.copilot.test_models_catalog_driven` for the
catalog-driven behaviour pins). This file covers the litellm-backed
``deprecation_hint`` orchestration plus the static helpers
(``_provider_prefix_for`` / ``_is_deprecated_in_litellm`` /
``_suggest_replacement_via_litellm``) that don't read the catalog.

The previous ``TestDefaultModelFor`` / ``TestAllSupportedModels``
classes that asserted hardcoded model names (e.g. ``"claude-haiku-4"``)
moved to ``test_models_catalog_driven.py`` with synthetic patched
catalogs, because hardcoding the bundled catalog's current contents
made the suite brittle to the weekly auto-update workflow.
"""

from __future__ import annotations

import pytest

from fluid_build.copilot import models as models_mod
from fluid_build.copilot.models import (
    _is_deprecated_in_litellm,
    _provider_prefix_for,
    _suggest_replacement_via_litellm,
    default_model_for,
    deprecation_hint,
)


class TestProviderPrefixFor:
    """``_provider_prefix_for`` — model id -> litellm provider prefix."""

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("gpt-4o", "openai"),
            ("gpt-4.1-mini", "openai"),
            ("o1-preview", "openai"),
            ("o3-mini", "openai"),
            ("claude-sonnet-4-6", "anthropic"),
            ("claude-3-5-sonnet", "anthropic"),
            ("gemini-2.5-flash", "gemini"),
            ("gemini-2.0-flash", "gemini"),
        ],
    )
    def test_known_families(self, model: str, expected: str) -> None:
        assert _provider_prefix_for(model) == expected

    @pytest.mark.parametrize("model", ["llama3.1:70b", "gemma4", "mistral-large"])
    def test_unrecognised_model_yields_empty_prefix(self, model: str) -> None:
        assert _provider_prefix_for(model) == ""


class TestDeprecationHint:
    """``deprecation_hint`` — explicit override > litellm registry > default."""

    def test_explicit_override_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            models_mod, "_DEPRECATION_OVERRIDES", {"legacy-model-x": "current-model-y"}
        )
        assert deprecation_hint("legacy-model-x") == "current-model-y"

    def test_non_deprecated_model_yields_no_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A model litellm does not flag as deprecated -> no hint.
        monkeypatch.setattr(models_mod, "_is_deprecated_in_litellm", lambda _model: False)
        assert deprecation_hint("model-that-is-current") is None

    def test_deprecated_model_uses_sibling_suggestion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(models_mod, "_is_deprecated_in_litellm", lambda _model: True)
        monkeypatch.setattr(
            models_mod, "_suggest_replacement_via_litellm", lambda _model: "gemini-2.5-flash"
        )
        assert deprecation_hint("gemini-1.5-flash") == "gemini-2.5-flash"

    def test_deprecated_model_falls_back_to_provider_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deprecated, but no clean sibling -> the provider's canonical default.
        monkeypatch.setattr(models_mod, "_is_deprecated_in_litellm", lambda _model: True)
        monkeypatch.setattr(models_mod, "_suggest_replacement_via_litellm", lambda _model: None)
        assert deprecation_hint("gemini-1.0-pro") == default_model_for("gemini", "default")

    def test_deprecated_unknown_provider_yields_no_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(models_mod, "_is_deprecated_in_litellm", lambda _model: True)
        monkeypatch.setattr(models_mod, "_suggest_replacement_via_litellm", lambda _model: None)
        # No provider prefix can be inferred -> nothing to suggest.
        assert deprecation_hint("mystery-retired-model") is None


class TestLitellmHelperBoundaries:
    """The litellm-backed helpers degrade gracefully on unknown input."""

    def test_unknown_model_is_not_flagged_deprecated(self) -> None:
        # Not present in litellm.model_cost -> rec is None -> False.
        assert _is_deprecated_in_litellm("definitely-not-a-real-model-zzz") is False

    def test_suggest_replacement_without_provider_returns_none(self) -> None:
        # No provider prefix -> the sibling search cannot run.
        assert _suggest_replacement_via_litellm("mystery-model-no-prefix") is None
