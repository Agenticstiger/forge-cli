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

"""``fluid_build.copilot.models.default_model_for`` is now a thin role
-> tier shim over ``cli/llm_models.json``. Pins the catalog-driven
behaviour so a future regression that re-introduces a hardcoded
``_DEFAULT_MODELS`` dict (or silently escalates to the flagship when
the requested tier is missing) is caught immediately.

The patch pattern mirrors
``tests/copilot/agents/test_judge_cheap_tier_default.py`` — we patch
``fluid_build.cli._llm_model_catalog._resolve_load_model_catalog`` to
inject a synthetic catalog rather than mutating the bundled JSON file.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from fluid_build.copilot.models import (
    _PROVIDER_ALIASES,
    _ROLE_TO_TIER,
    all_supported_models,
    default_model_for,
)


@contextmanager
def _patched_catalog(catalog: Dict[str, Any]) -> Iterator[None]:
    """Patch the catalog loader so :func:`default_model_for` reads
    *catalog* instead of the bundled JSON. Mirrors the pattern used by
    ``test_judge_cheap_tier_default.py``."""

    def _fake_loader():
        return catalog

    with patch(
        "fluid_build.cli._llm_model_catalog._resolve_load_model_catalog",
        return_value=_fake_loader,
    ):
        yield


class TestRoleToTierMapping:
    """The role -> tier translation is the only piece of logic this
    module owns. Pinned so a typo in a future refactor (e.g.
    ``"default" -> "default"`` instead of ``"balanced"``) is caught."""

    def test_role_default_maps_to_balanced_tier(self) -> None:
        assert _ROLE_TO_TIER["default"] == "balanced"

    def test_role_fast_maps_to_fast_tier(self) -> None:
        assert _ROLE_TO_TIER["fast"] == "fast"

    def test_role_deep_maps_to_deep_tier(self) -> None:
        assert _ROLE_TO_TIER["deep"] == "deep"


class TestProviderAliases:
    """``claude`` is the documented alias for ``anthropic``; pinning the
    alias dict so a future caller adding e.g. ``"sonnet" -> "anthropic"``
    doesn't accidentally drop the existing one."""

    def test_claude_aliases_to_anthropic(self) -> None:
        assert _PROVIDER_ALIASES["claude"] == "anthropic"


class TestDefaultModelForCatalogDriven:
    """``default_model_for`` returns the catalog-resolved model for each
    role. NO hardcoded fallback dict — a provider rename in
    ``llm_models.json`` must take effect immediately."""

    _CATALOG = {
        "tiers": {
            "openai": {
                "deep": "test-gpt-deep",
                "balanced": "test-gpt-balanced",
                "fast": "test-gpt-fast",
            },
            "anthropic": {
                "deep": "test-claude-deep",
                "balanced": "test-claude-balanced",
                "fast": "test-claude-fast",
            },
            "gemini": {
                "deep": "test-gemini-deep",
                "balanced": "test-gemini-balanced",
                "fast": "test-gemini-fast",
            },
        },
        "providers": {
            "openai": {},
            "anthropic": {},
            "gemini": {},
        },
    }

    def test_default_role_returns_balanced_tier(self) -> None:
        with _patched_catalog(self._CATALOG):
            assert default_model_for("openai", "default") == "test-gpt-balanced"
            assert default_model_for("anthropic", "default") == "test-claude-balanced"
            assert default_model_for("gemini", "default") == "test-gemini-balanced"

    def test_fast_role_returns_fast_tier(self) -> None:
        with _patched_catalog(self._CATALOG):
            assert default_model_for("openai", "fast") == "test-gpt-fast"
            assert default_model_for("anthropic", "fast") == "test-claude-fast"

    def test_deep_role_returns_deep_tier(self) -> None:
        with _patched_catalog(self._CATALOG):
            assert default_model_for("openai", "deep") == "test-gpt-deep"
            assert default_model_for("anthropic", "deep") == "test-claude-deep"

    def test_claude_alias_resolves_via_anthropic(self) -> None:
        """The ``"claude"`` alias must hit the ``"anthropic"`` row in
        the catalog, not require a duplicate entry."""
        with _patched_catalog(self._CATALOG):
            assert default_model_for("claude", "fast") == "test-claude-fast"
            assert default_model_for("claude", "deep") == "test-claude-deep"
            assert default_model_for("claude", "default") == "test-claude-balanced"

    def test_role_defaults_to_default(self) -> None:
        """No explicit role -> ``"default"`` (-> ``balanced`` tier)."""
        with _patched_catalog(self._CATALOG):
            assert default_model_for("openai") == "test-gpt-balanced"

    def test_unknown_provider_returns_fallback(self) -> None:
        """A provider missing from the catalog returns the caller-supplied
        fallback (default ``None``). Existing call sites pass
        ``fallback=`` strings where they need a guaranteed return value."""
        with _patched_catalog(self._CATALOG):
            assert default_model_for("cohere", "default") is None
            assert default_model_for("cohere", "default", fallback="gpt-4.1") == "gpt-4.1"


class TestNoSilentFlagshipFallback:
    """The whole point of moving off ``_DEFAULT_MODELS``: when the
    requested tier isn't defined, do NOT silently escalate to the
    provider's flagship. Either fall through to the ``balanced`` tier
    (the cheapest tool-use-capable rung) or return ``None``.

    Pinned because :func:`get_catalog_tier_model` (the non-explicit
    variant) DOES silently escalate, and an accidental swap of the
    catalog probe primitive would re-introduce the bug judge_agent.py
    just fixed."""

    def test_missing_fast_tier_falls_to_balanced_not_deep(self) -> None:
        """Catalog only declares ``deep`` + ``balanced``; asking for
        ``fast`` must fall to ``balanced`` (cheaper), not to ``deep``."""
        catalog = {
            "tiers": {
                "anthropic": {
                    "deep": "claude-opus-4-7-flagship",  # MUST NOT be returned
                    "balanced": "claude-sonnet-4-6-balanced",
                },
            },
            "providers": {"anthropic": {}},
        }
        with _patched_catalog(catalog):
            assert default_model_for("anthropic", "fast") == "claude-sonnet-4-6-balanced"

    def test_missing_deep_tier_falls_to_balanced_not_flagship_alias(self) -> None:
        """Asking for ``deep`` when only ``balanced`` is configured
        falls to ``balanced`` (same rationale — never silently escalate
        to a flagship-aliased entry the caller didn't ask for)."""
        catalog = {
            "tiers": {
                "anthropic": {"balanced": "claude-sonnet-4-6-balanced"},
            },
            "providers": {"anthropic": {}},
        }
        with _patched_catalog(catalog):
            assert default_model_for("anthropic", "deep") == "claude-sonnet-4-6-balanced"

    def test_all_tiers_missing_returns_fallback(self) -> None:
        """No tiers at all in the catalog for this provider -> the
        caller's fallback (default ``None``). NOT a flagship guess
        from elsewhere."""
        catalog = {
            "tiers": {"anthropic": {}},
            "providers": {"anthropic": {}},
        }
        with _patched_catalog(catalog):
            assert default_model_for("anthropic", "fast") is None
            assert (
                default_model_for("anthropic", "fast", fallback="explicit-caller-fallback")
                == "explicit-caller-fallback"
            )

    def test_explicit_balanced_request_with_only_balanced_set(self) -> None:
        """When the role IS ``balanced`` and the catalog has balanced,
        we return it without looping back to ourselves (no infinite
        recursion in the fall-through logic)."""
        catalog = {
            "tiers": {"anthropic": {"balanced": "claude-balanced-id"}},
            "providers": {"anthropic": {}},
        }
        with _patched_catalog(catalog):
            assert default_model_for("anthropic", "default") == "claude-balanced-id"


class TestUserOverrideCatalog:
    """An override file at ``~/.fluid/llm_models.json`` wins because
    :func:`_load_model_catalog` checks it FIRST. We pin the property
    via the same patch boundary used by the rest of the suite — the
    override-file lookup itself is covered exhaustively by
    ``tests/copilot/agents/test_judge_cheap_tier_default.py`` and the
    catalog-loader unit tests. Here we just confirm that whatever
    catalog the loader resolves to is what ``default_model_for``
    reads."""

    def test_user_override_catalog_wins(self) -> None:
        """An operator-supplied catalog (e.g. pinning to an internal
        proxy) is returned verbatim."""
        operator_catalog = {
            "tiers": {
                "anthropic": {
                    "fast": "internal-proxy-mirror-of-haiku",
                    "balanced": "internal-proxy-mirror-of-sonnet",
                    "deep": "internal-proxy-mirror-of-opus",
                },
            },
            "providers": {"anthropic": {}},
        }
        with _patched_catalog(operator_catalog):
            assert default_model_for("anthropic", "fast") == "internal-proxy-mirror-of-haiku"
            assert default_model_for("anthropic", "default") == "internal-proxy-mirror-of-sonnet"
            assert default_model_for("anthropic", "deep") == "internal-proxy-mirror-of-opus"


class TestEnvOverride:
    """``FLUID_LLM_DEFAULT_MODEL_<PROVIDER>_<ROLE>`` short-circuits the
    catalog probe — operators pin a specific model per workspace / per
    CI without editing the catalog file."""

    def test_env_var_overrides_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLUID_LLM_DEFAULT_MODEL_OPENAI_DEFAULT", "gpt-pinned-by-ci")
        with _patched_catalog(TestDefaultModelForCatalogDriven._CATALOG):
            assert default_model_for("openai", "default") == "gpt-pinned-by-ci"

    def test_env_var_overrides_for_fast_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLUID_LLM_DEFAULT_MODEL_ANTHROPIC_FAST", "claude-pinned-by-ci-cheap")
        with _patched_catalog(TestDefaultModelForCatalogDriven._CATALOG):
            assert default_model_for("anthropic", "fast") == "claude-pinned-by-ci-cheap"


class TestAllSupportedModelsCatalogDriven:
    """``all_supported_models`` snapshots whatever the catalog declares
    — no hardcoded provider list."""

    def test_snapshot_built_from_catalog(self) -> None:
        with _patched_catalog(TestDefaultModelForCatalogDriven._CATALOG):
            snap = all_supported_models()
            assert set(snap) == {"openai", "anthropic", "gemini"}
            assert snap["openai"]["default"] == "test-gpt-balanced"
            assert snap["anthropic"]["fast"] == "test-claude-fast"
            assert snap["gemini"]["deep"] == "test-gemini-deep"

    def test_snapshot_skips_providers_with_no_resolvable_tiers(self) -> None:
        """A provider declared in the catalog but with all tier values
        empty does not surface in the snapshot — better to omit than
        emit None placeholders."""
        catalog = {
            "tiers": {
                "anthropic": {
                    "deep": "claude-opus",
                    "balanced": "claude-sonnet",
                    "fast": "claude-haiku",
                },
                "phantom-provider": {},
            },
            "providers": {"anthropic": {}, "phantom-provider": {}},
        }
        with _patched_catalog(catalog):
            snap = all_supported_models()
            assert "anthropic" in snap
            assert "phantom-provider" not in snap


class TestBackwardCompatPublicSignature:
    """Existing callers must keep working. The function signature
    ``default_model_for(provider, role='default', *, fallback=None) -> Optional[str]``
    is the contract."""

    def test_positional_role_argument(self) -> None:
        with _patched_catalog(TestDefaultModelForCatalogDriven._CATALOG):
            assert default_model_for("openai", "fast") == "test-gpt-fast"

    def test_keyword_fallback_argument(self) -> None:
        with _patched_catalog(TestDefaultModelForCatalogDriven._CATALOG):
            # Unknown provider with a kwarg fallback.
            assert (
                default_model_for("cohere", "default", fallback="explicit-caller-default")
                == "explicit-caller-default"
            )

    def test_returns_optional_str(self) -> None:
        """The return type is ``Optional[str]`` — callers' existing
        None-handling kicks in for the catalog-misses-everything path."""
        with _patched_catalog({"tiers": {}, "providers": {}}):
            assert default_model_for("anything", "default") is None
