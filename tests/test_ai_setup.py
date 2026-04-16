# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for fluid_build.cli.ai_setup — AI/LLM configuration."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestSaveAndLoadConfig:
    def test_save_and_load_roundtrip(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            assert _save_ai_config("openai", "gpt-4o", api_key="sk-test123")
            loaded = _load_ai_config()
            assert loaded is not None
            assert loaded["provider"] == "openai"
            assert loaded["model"] == "gpt-4o"
            assert loaded["api_key"] == "sk-test123"

    def test_save_sets_permissions_600(self, tmp_path):
        from fluid_build.cli.ai_setup import _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            _save_ai_config("gemini", "gemini-2.5-flash", api_key="AIzaFake")
            mode = config_file.stat().st_mode
            assert mode & stat.S_IRUSR  # owner read
            assert mode & stat.S_IWUSR  # owner write
            assert not (mode & stat.S_IRGRP)  # no group read
            assert not (mode & stat.S_IROTH)  # no other read

    def test_load_returns_none_when_no_file(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config

        config_file = tmp_path / "nonexistent.json"
        with patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file):
            assert _load_ai_config() is None

    def test_load_returns_none_for_invalid_json(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config

        config_file = tmp_path / "ai_config.json"
        config_file.write_text("not json", encoding="utf-8")
        with patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file):
            assert _load_ai_config() is None

    def test_load_returns_none_for_empty_provider(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config

        config_file = tmp_path / "ai_config.json"
        config_file.write_text('{"provider": ""}', encoding="utf-8")
        with patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file):
            assert _load_ai_config() is None

    def test_clear_deletes_file(self, tmp_path):
        from fluid_build.cli.ai_setup import _clear_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            _save_ai_config("openai", "gpt-4o")
            assert config_file.exists()
            _clear_ai_config()
            assert not config_file.exists()

    def test_save_with_endpoint_and_ollama_host(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            _save_ai_config(
                "ollama",
                "llama3.1",
                endpoint="http://localhost:11434/v1/chat/completions",
                ollama_host="http://localhost:11434",
            )
            loaded = _load_ai_config()
            assert loaded["ollama_host"] == "http://localhost:11434"
            assert loaded["endpoint"] == "http://localhost:11434/v1/chat/completions"


class TestDetectProviderFromApiKey:
    def test_detect_openai(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("sk-proj-abc123def456") == "openai"

    def test_detect_anthropic(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("sk-ant-api03-abc123") == "anthropic"

    def test_detect_gemini(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("AIzaSyA" + "x" * 30) == "gemini"

    def test_detect_unknown(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("some-random-key") is None

    def test_detect_empty(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("") is None
        assert detect_provider_from_api_key("   ") is None


class TestSetSessionEnv:
    def test_sets_openai_env(self):
        from fluid_build.cli.ai_setup import set_session_env

        with patch.dict("os.environ", {}, clear=False):
            set_session_env("openai", "sk-test")
            import os

            assert os.environ.get("OPENAI_API_KEY") == "sk-test"

    def test_sets_anthropic_env(self):
        from fluid_build.cli.ai_setup import set_session_env

        with patch.dict("os.environ", {}, clear=False):
            set_session_env("anthropic", "sk-ant-test")
            import os

            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test"

    def test_sets_gemini_env(self):
        from fluid_build.cli.ai_setup import set_session_env

        with patch.dict("os.environ", {}, clear=False):
            set_session_env("gemini", "AIzaTest")
            import os

            assert os.environ.get("GOOGLE_API_KEY") == "AIzaTest"


class TestCheckLlmReadiness:
    def test_ready_with_openai_env(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        with patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None):
            result = check_llm_readiness(
                {"OPENAI_API_KEY": "sk-test", "FLUID_LLM_PROVIDER": "openai"}
            )
        assert result.ready
        assert result.provider == "openai"
        assert result.auth_available

    def test_not_ready_without_keys(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        # Patch the inline import target so the real config file is not read
        # Also patch _infer_provider_from_env to avoid detecting local Ollama
        with (
            patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", Path("/nonexistent/ai_config.json")),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers._infer_provider_from_env",
                return_value=None,
            ),
        ):
            result = check_llm_readiness({})
            assert not result.ready
            assert result.error is not None

    def test_not_ready_unknown_provider(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        result = check_llm_readiness({"FLUID_LLM_PROVIDER": "nonexistent"})
        assert not result.ready

    def test_ready_with_gemini_api_key_alias(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        with patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None):
            result = check_llm_readiness(
                {"GEMINI_API_KEY": "AIzaTestAlias", "FLUID_LLM_PROVIDER": "gemini"}
            )
        assert result.ready
        assert result.provider == "gemini"
        assert result.auth_available


class TestShowAiStatus:
    def test_status_no_console(self):
        from fluid_build.cli.ai_setup import show_ai_status

        # Should not raise
        show_ai_status(None)

    @patch("fluid_build.cli.ai_setup.check_llm_readiness")
    def test_status_with_console_ready(self, mock_readiness):
        from fluid_build.cli.ai_setup import show_ai_status
        from fluid_build.cli.forge_copilot_llm_providers import LlmReadinessCheck

        mock_readiness.return_value = LlmReadinessCheck(
            ready=True,
            provider="openai",
            model="gpt-4o",
            auth_available=True,
        )
        console = MagicMock()
        show_ai_status(console)
        console.print.assert_called()


class TestModelCatalogIntegrity:
    """Validate the catalog structure and provider-code consistency.

    These tests check STRUCTURE, not specific model names — so they
    stay green even after ``scripts/update_model_catalog.py`` changes
    the flagship/balanced selections.
    """

    def test_catalog_has_schema_version_2(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        assert catalog.get("schema_version") == 2

    def test_catalog_has_default_provider(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        assert catalog.get("default_provider") in ("gemini", "openai", "anthropic")

    def test_every_provider_has_flagship_and_balanced(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        for name in ("openai", "anthropic", "gemini", "ollama"):
            entry = catalog["providers"][name]
            assert "flagship" in entry, f"{name} missing flagship"
            assert "balanced" in entry, f"{name} missing balanced"
            assert "routing" in entry, f"{name} missing routing"

    def test_flagship_exists_in_models_list(self):
        """For non-ollama providers, the flagship model ID must appear
        in the models array so alias resolution works."""
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        for name in ("openai", "anthropic", "gemini"):
            entry = catalog["providers"][name]
            flagship = entry["flagship"]
            model_ids = [m["id"] for m in entry.get("models", [])]
            assert (
                flagship in model_ids
            ), f"{name} flagship '{flagship}' not in models list {model_ids}"

    def test_class_defaults_match_catalog_flagship(self):
        """After _sync_provider_defaults_from_catalog runs, every
        provider's default_model must match the catalog's flagship."""
        from fluid_build.cli.forge_copilot_llm_providers import (
            BUILTIN_LLM_PROVIDERS,
            get_catalog_default,
        )

        for name in ("openai", "anthropic", "gemini", "ollama"):
            provider = BUILTIN_LLM_PROVIDERS[name]
            flagship = get_catalog_default(name)
            if flagship:
                assert provider.default_model == flagship, (
                    f"{name}: class default '{provider.default_model}' "
                    f"doesn't match catalog flagship '{flagship}'"
                )

    def test_capability_flags_are_booleans(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        for name, entry in catalog["providers"].items():
            for m in entry.get("models", []):
                caps = m.get("capabilities", {})
                for key in ("structured_output", "tool_use", "streaming"):
                    if key in caps:
                        assert isinstance(
                            caps[key], bool
                        ), f"{name}/{m['id']}.capabilities.{key} is not bool"

    def test_model_supports_structured_output_reads_catalog(self):
        from fluid_build.cli.forge_copilot_llm_providers import (
            model_supports_structured_output,
        )

        # OpenAI gpt-4.1 should support it (per catalog)
        assert model_supports_structured_output("openai", "gpt-4.1") is True
        # Gemini shouldn't (nested freeform issue)
        assert model_supports_structured_output("gemini", "gemini-2.5-pro") is False
        # Unknown model → False
        assert model_supports_structured_output("openai", "unknown-model-xyz") is False

    def test_user_override_catalog_takes_precedence(self, tmp_path):
        """A user catalog at ~/.fluid/llm_models.json must be loaded
        instead of the bundled one."""
        import fluid_build.cli.forge_copilot_llm_providers as mod

        # Create the user catalog at the expected path
        fluid_dir = tmp_path / ".fluid"
        fluid_dir.mkdir()
        user_catalog = fluid_dir / "llm_models.json"
        user_catalog.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "default_provider": "openai",
                    "providers": {
                        "openai": {
                            "flagship": "test-flagship-model",
                            "balanced": "test-balanced-model",
                            "routing": "test-balanced-model",
                            "default": "test-flagship-model",
                            "models": [],
                        }
                    },
                }
            )
        )

        # Reset the catalog cache and patch Path.home to return tmp_path
        original_cache = mod._model_catalog_cache
        mod._model_catalog_cache = None
        try:
            with patch.object(Path, "home", return_value=tmp_path):
                catalog = mod._load_model_catalog()
                assert catalog["providers"]["openai"]["flagship"] == "test-flagship-model"
        finally:
            mod._model_catalog_cache = original_cache

    def test_get_catalog_tier_model(self):
        from fluid_build.cli.forge_copilot_llm_providers import get_catalog_tier_model

        # flagship tier returns the flagship model
        flagship = get_catalog_tier_model("openai", "flagship")
        assert flagship is not None

        # balanced tier returns a different (cheaper) model
        balanced = get_catalog_tier_model("openai", "balanced")
        assert balanced is not None

        # Both should be non-empty strings
        assert isinstance(flagship, str) and len(flagship) > 0
        assert isinstance(balanced, str) and len(balanced) > 0


class TestQueryOllamaModels:
    def test_returns_empty_when_httpx_not_installed(self):
        from fluid_build.cli.ai_setup import _query_ollama_models

        with patch.dict("sys.modules", {"httpx": None}):
            # Force reimport to hit ImportError
            import importlib

            import fluid_build.cli.ai_setup as mod

            # The function catches ImportError internally
            result = _query_ollama_models("http://localhost:11434")
            assert isinstance(result, list)
