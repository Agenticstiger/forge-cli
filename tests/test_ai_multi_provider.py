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

"""Multi-provider AI config: several saved providers coexist and resolve.

Trello 69d4c9c5 — ``fluid ai setup --provider openai`` AND
``--provider gemini`` must both persist, and ``fluid forge --llm-provider
gemini`` (via :func:`resolve_llm_config`) must resolve the requested
provider's saved key even when its env var is not set. The old single
``{"provider": .., "model": ..}`` shape must keep loading (back-compat).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def keyring_mem(monkeypatch):
    """Back both keyring key-schemes with a single in-memory dict.

    ``fluid ai setup`` writes under ``llm_api_key.<provider>`` while the
    resolve path reads under ``llm.<provider>.api_key``; sharing one dict
    lets a test assert the cross-scheme bridge end-to-end.
    """
    from fluid_build.credentials.keyring_store import KeyringCredentialStore

    store: dict = {}
    monkeypatch.setattr(
        KeyringCredentialStore, "set_credential", lambda key, value: store.__setitem__(key, value)
    )
    monkeypatch.setattr(KeyringCredentialStore, "get_credential", lambda key: store.get(key))
    monkeypatch.setattr(
        KeyringCredentialStore, "delete_credential", lambda key: store.pop(key, None)
    )
    return store


@pytest.fixture
def iso_config(tmp_path, monkeypatch):
    """Isolate ``~/.fluid/ai_config.json`` to a tmp file + neutralise the
    unified-config short-circuit so tests read the JSON we write."""
    config_file = tmp_path / "ai_config.json"
    monkeypatch.setattr("fluid_build.cli.ai_setup._CONFIG_FILE", config_file)
    monkeypatch.setattr("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "fluid_build.copilot.unified_config.load_unified_config", lambda *a, **k: None
    )
    monkeypatch.delenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", raising=False)
    return config_file


class TestMultiProviderPersistence:
    def test_second_save_does_not_clobber_first_in_config_file(self, iso_config, keyring_mem):
        """RED→GREEN reproducer: the 2nd ``_save_ai_config`` must not erase the 1st."""
        from fluid_build.cli.ai_setup import _save_ai_config

        assert _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        assert _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        # Distinct per-provider keyring entries (both survive).
        assert keyring_mem.get("llm_api_key.openai") == "sk-openai"
        assert keyring_mem.get("llm_api_key.gemini") == "AIza-gemini"

        # The regression: ai_config.json must hold a multi-provider MAP
        # with BOTH providers, not just the last-written one.
        data = json.loads(iso_config.read_text(encoding="utf-8"))
        providers = data.get("providers") or {}
        assert set(providers) == {"openai", "gemini"}, data
        assert data.get("active") == "gemini"
        assert providers["openai"]["model"] == "gpt-4o"
        assert providers["gemini"]["model"] == "gemini-2.5-pro"
        # Keyring succeeded → no plaintext key on disk for either.
        assert "api_key" not in providers["openai"]
        assert "api_key" not in providers["gemini"]

    def test_load_ai_config_returns_active_provider(self, iso_config, keyring_mem):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        loaded = _load_ai_config()
        assert loaded is not None
        # Back-compat flat shape, pointing at the active (last-configured).
        assert loaded["provider"] == "gemini"
        assert loaded["model"] == "gemini-2.5-pro"

    def test_load_ai_config_map_lists_all(self, iso_config, keyring_mem):
        from fluid_build.cli.ai_setup import _load_ai_config_map, _save_ai_config

        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        cfg_map = _load_ai_config_map()
        assert cfg_map is not None
        assert cfg_map["active"] == "gemini"
        assert set(cfg_map["providers"]) == {"openai", "gemini"}
        # Each entry is flattened to carry its own provider name.
        assert cfg_map["providers"]["openai"]["provider"] == "openai"
        assert cfg_map["providers"]["gemini"]["model"] == "gemini-2.5-pro"

    def test_load_ai_config_for_specific_provider(self, iso_config, keyring_mem):
        from fluid_build.cli.ai_setup import _load_ai_config_for, _save_ai_config

        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        openai_cfg = _load_ai_config_for("openai")
        assert openai_cfg == {"provider": "openai", "model": "gpt-4o"}
        assert _load_ai_config_for("anthropic") is None

    def test_list_configured_providers(self, iso_config, keyring_mem):
        from fluid_build.cli.ai_setup import _list_configured_providers, _save_ai_config

        assert _list_configured_providers() == []
        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")
        assert _list_configured_providers() == ["gemini", "openai"]

    def test_set_active_provider_switches_default(self, iso_config, keyring_mem):
        from fluid_build.cli.ai_setup import (
            _load_ai_config,
            _save_ai_config,
            _set_active_provider,
        )

        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")
        assert _load_ai_config()["provider"] == "gemini"

        assert _set_active_provider("openai") is True
        assert _load_ai_config()["provider"] == "openai"
        # Both entries still present after the switch.
        assert set(_load_ai_config_map_providers(iso_config)) == {"openai", "gemini"}

        # Unknown provider is a no-op that reports failure.
        assert _set_active_provider("anthropic") is False
        assert _load_ai_config()["provider"] == "openai"

    def test_plaintext_fallback_is_per_provider(self, iso_config, keyring_mem, monkeypatch):
        """With keyring unavailable + plaintext opt-in, each provider keeps its
        own opt-in key without clobbering a sibling."""
        from fluid_build.cli.ai_setup import _load_ai_config_for, _save_ai_config

        monkeypatch.setenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", "1")
        # Force keyring "unavailable" so the plaintext branch is taken.
        # ``_save_ai_config`` resolves the saver via the ``ai_setup``
        # namespace, so that is the patch target.
        monkeypatch.setattr("fluid_build.cli.ai_setup._save_key_to_keyring", lambda p, k: False)
        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        assert _load_ai_config_for("openai")["api_key"] == "sk-openai"
        assert _load_ai_config_for("gemini")["api_key"] == "AIza-gemini"


def _load_ai_config_map_providers(config_file):
    data = json.loads(config_file.read_text(encoding="utf-8"))
    return list((data.get("providers") or {}).keys())


class TestBackCompat:
    def test_old_single_provider_shape_loads(self, iso_config, keyring_mem):
        """An OLD ``{"provider": .., "model": ..}`` file must still load."""
        from fluid_build.cli.ai_setup import (
            _load_ai_config,
            _load_ai_config_for,
            _load_ai_config_map,
        )

        iso_config.write_text(
            json.dumps({"provider": "openai", "model": "gpt-4o"}), encoding="utf-8"
        )
        loaded = _load_ai_config()
        assert loaded == {"provider": "openai", "model": "gpt-4o"}

        cfg_map = _load_ai_config_map()
        assert cfg_map["active"] == "openai"
        assert set(cfg_map["providers"]) == {"openai"}
        assert _load_ai_config_for("openai")["model"] == "gpt-4o"

    def test_old_shape_with_plaintext_key_migrates_on_next_save(self, iso_config, keyring_mem):
        """Saving a 2nd provider onto a legacy single-provider file upgrades it
        to the v2 map WITHOUT losing the original provider."""
        from fluid_build.cli.ai_setup import _load_ai_config_map, _save_ai_config

        iso_config.write_text(
            json.dumps({"provider": "openai", "model": "gpt-4o", "api_key": "sk-old"}),
            encoding="utf-8",
        )
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        cfg_map = _load_ai_config_map()
        assert set(cfg_map["providers"]) == {"openai", "gemini"}
        assert cfg_map["active"] == "gemini"
        # The legacy openai entry (incl. its opt-in plaintext key) is preserved.
        assert cfg_map["providers"]["openai"]["api_key"] == "sk-old"


class TestMultiProviderResolution:
    def test_resolve_llm_config_reads_saved_key_for_requested_provider(
        self, iso_config, keyring_mem
    ):
        """LIVE-TEST item 3: no env vars, both providers seeded → ``--llm-provider
        gemini`` resolves gemini's saved key (not openai's, not the default)."""
        from fluid_build.cli.ai_setup import _save_ai_config
        from fluid_build.cli.forge_copilot_llm_providers import resolve_llm_config

        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        # A truthy-but-provider-free env so resolve_llm_config uses it
        # as-is (an empty dict would fall back to os.environ).
        clean_env = {"PATH": "/usr/bin"}
        args = SimpleNamespace(llm_provider="gemini", llm_model=None, llm_endpoint=None)
        config = resolve_llm_config(args, environ=clean_env)
        assert config.provider == "gemini"
        assert config.api_key == "AIza-gemini"

        # And the sibling still resolves independently.
        args_openai = SimpleNamespace(llm_provider="openai", llm_model=None, llm_endpoint=None)
        config_openai = resolve_llm_config(args_openai, environ=clean_env)
        assert config_openai.provider == "openai"
        assert config_openai.api_key == "sk-openai"

    def test_keyring_bridge_reads_ai_setup_scheme(self, keyring_mem):
        """``_get_api_key_from_keyring`` must also find keys stored by
        ``fluid ai setup`` under the ``llm_api_key.<provider>`` scheme."""
        from fluid_build.cli.forge_copilot_llm_providers import _get_api_key_from_keyring

        # Only the ai-setup scheme is populated (no ``llm.gemini.api_key``).
        keyring_mem["llm_api_key.gemini"] = "AIza-bridge"
        assert _get_api_key_from_keyring("gemini") == "AIza-bridge"

    def test_resolve_prefers_env_over_saved(self, iso_config, keyring_mem):
        from fluid_build.cli.ai_setup import _save_ai_config
        from fluid_build.cli.forge_copilot_llm_providers import resolve_llm_config

        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-saved")
        args = SimpleNamespace(llm_provider="gemini", llm_model=None, llm_endpoint=None)
        config = resolve_llm_config(args, environ={"GEMINI_API_KEY": "AIza-env"})
        assert config.api_key == "AIza-env"

    def test_resolve_reads_plaintext_only_when_opted_in(self, iso_config, monkeypatch):
        """The opt-in plaintext key resolves for the requested provider ONLY
        when FLUID_ALLOW_PLAINTEXT_AI_SECRETS is set (keyring-first posture)."""
        from fluid_build.cli.ai_setup import _save_ai_config
        from fluid_build.cli.forge_copilot_llm_providers import (
            _get_api_key_from_keyring,
            resolve_llm_config,
        )

        # Keyring genuinely unavailable → save falls back to plaintext.
        monkeypatch.setenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", "1")
        monkeypatch.setattr("fluid_build.cli.ai_setup._save_key_to_keyring", lambda p, k: False)
        monkeypatch.setattr(
            "fluid_build.cli.forge_copilot_llm_providers._get_api_key_from_keyring",
            lambda provider: None,
        )
        _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-plain")

        args = SimpleNamespace(llm_provider="gemini", llm_model=None, llm_endpoint=None)
        # Opted in → resolves the plaintext key.
        config = resolve_llm_config(args, environ={"FLUID_ALLOW_PLAINTEXT_AI_SECRETS": "1"})
        assert config.api_key == "AIza-plain"

        # NOT opted in at resolve time → plaintext is ignored (no key found).
        # (A truthy env without the flag — an empty dict would fall back
        # to os.environ, which the fixture cannot fully control.)
        with pytest.raises(Exception) as exc:
            resolve_llm_config(args, environ={"FLUID_ALLOW_PLAINTEXT_AI_SECRETS": "0"})
        assert "copilot_missing_llm_api_key" in str(exc.value)


class TestAiSetupProviderFlag:
    def test_setup_with_preselected_provider_does_not_erase_others(
        self, iso_config, keyring_mem, monkeypatch
    ):
        """LIVE-TEST item 2: ``fluid ai setup --provider gemini`` records gemini
        without erasing a previously-configured openai."""
        import fluid_build.cli.ai_setup as ai_setup
        from fluid_build.cli.ai_setup import (
            _load_ai_config_map,
            _save_ai_config,
            run_ai_setup_interactive,
        )

        # openai already configured.
        _save_ai_config("openai", "gpt-4o", api_key="sk-openai")

        # Drive the interactive setup non-interactively for gemini.
        monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True)
        monkeypatch.setattr(ai_setup, "check_llm_readiness", lambda: SimpleNamespace(ready=False))
        monkeypatch.setattr(ai_setup, "_pick_tier", lambda console: "balanced")
        monkeypatch.setattr(ai_setup, "_validate_api_key", lambda provider, key: None)
        monkeypatch.setattr(ai_setup, "set_session_env", lambda p, k: None)
        monkeypatch.setattr(ai_setup.Prompt, "ask", staticmethod(lambda *a, **k: "AIzaGeminiKey01"))

        console = MagicMock()
        config = run_ai_setup_interactive(console, preselected_provider="gemini")
        assert config is not None
        assert config.provider == "gemini"

        cfg_map = _load_ai_config_map()
        assert set(cfg_map["providers"]) == {"openai", "gemini"}
        assert cfg_map["active"] == "gemini"
        assert keyring_mem.get("llm_api_key.gemini") == "AIzaGeminiKey01"
        assert keyring_mem.get("llm_api_key.openai") == "sk-openai"
