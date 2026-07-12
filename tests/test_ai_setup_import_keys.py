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

"""Import API key from other CLIs' credential files (opt-in onboarding).

Covers ``ai_setup._discover_imported_api_keys`` (HOME-confined discovery of
provider-shaped keys and ``gh`` ``oauth_token``s) and
``_offer_import_from_other_clis`` (the opt-in prompt → validate → persist
flow). Everything is fully offline: ``_validate_api_key`` and the keyring are
monkeypatched, no real CLI or network is touched.
"""

from __future__ import annotations

import logging
import types

import pytest

from fluid_build.cli import ai_setup

pytestmark = pytest.mark.unit

# Distinctive canary values so a leak assertion can pinpoint the source.
_OPENAI_KEY = "sk-proj-CODEXCANARY0123456789abcdefghij"
_ANTHROPIC_KEY = "sk-ant-api03-ANTHCANARY0123456789abcdefghijkl"
_GEMINI_KEY = "AIzaSyGEMINICANARY0123456789abcdefghij"  # 38 chars, AIza + 34
_GH_TOKEN = "gho_GITHUBCANARY0123456789abcdefghijkl"


def _write_home(home):
    """Populate a tmp HOME with the well-known credential files we scan."""
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text(
        '{"OPENAI_API_KEY": "%s", "tokens": {"id_token": "not.a.key"}}' % _OPENAI_KEY,
        encoding="utf-8",
    )
    anthropic = home / ".config" / "anthropic"
    anthropic.mkdir(parents=True)
    (anthropic / "credentials.json").write_text(
        '{"apiKey": "%s"}' % _ANTHROPIC_KEY, encoding="utf-8"
    )
    openai_dir = home / ".openai"
    openai_dir.mkdir(parents=True)
    (openai_dir / "credentials").write_text(
        "[default]\napi_key = %s\n" % _GEMINI_KEY, encoding="utf-8"
    )
    gh = home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "hosts.yml").write_text(
        "github.com:\n"
        f"    oauth_token: {_GH_TOKEN}\n"
        "    user: octocat\n"
        "    git_protocol: https\n",
        encoding="utf-8",
    )


def _stub_console():
    printed = []
    console = types.SimpleNamespace(
        print=lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    )
    return console, printed


class _FakeProvider:
    """Minimal provider stand-in so persistence never needs the real AI stack."""

    def __init__(self, name):
        self.name = name
        self.default_model = "test-model"

    def default_endpoint(self, model, env):  # noqa: ARG002
        return "https://example.invalid/v1"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscoverImportedKeys:
    def test_finds_all_providers_by_provider_and_source(self, tmp_path):
        _write_home(tmp_path)
        found = ai_setup._discover_imported_api_keys(home=tmp_path)
        by_provider = {c.provider: c for c in found}
        assert set(by_provider) == {"openai", "anthropic", "gemini", "github"}
        # Each candidate carries a human-readable source label and the secret.
        assert "~/.codex/auth.json" in by_provider["openai"].source_label
        assert "gh" in by_provider["github"].source_label.lower()
        assert by_provider["openai"].key == _OPENAI_KEY
        assert by_provider["gemini"].key == _GEMINI_KEY

    def test_gh_oauth_token_maps_to_github_not_openai(self, tmp_path):
        gh = tmp_path / ".config" / "gh"
        gh.mkdir(parents=True)
        (gh / "hosts.yml").write_text(
            f"github.com:\n    oauth_token: {_GH_TOKEN}\n    user: octocat\n",
            encoding="utf-8",
        )
        found = ai_setup._discover_imported_api_keys(home=tmp_path)
        assert [c.provider for c in found] == ["github"]
        # The GitHub token is NEVER classified as an OpenAI/Anthropic key.
        assert found[0].provider == "github"

    def test_source_label_never_contains_the_secret(self, tmp_path):
        _write_home(tmp_path)
        for cand in ai_setup._discover_imported_api_keys(home=tmp_path):
            assert cand.key not in cand.source_label

    def test_returns_empty_when_no_credential_files(self, tmp_path):
        assert ai_setup._discover_imported_api_keys(home=tmp_path) == []

    def test_ignores_non_key_shaped_strings(self, tmp_path):
        # A credential file with only OAuth/JWT junk yields nothing.
        codex = tmp_path / ".codex"
        codex.mkdir(parents=True)
        (codex / "auth.json").write_text(
            '{"id_token": "eyJhbGciOiJID.someJwtNoiseValue.signaturePart"}',
            encoding="utf-8",
        )
        assert ai_setup._discover_imported_api_keys(home=tmp_path) == []

    def test_dedupes_same_provider_and_key_across_sources(self, tmp_path):
        # Same openai key in two scanned files → one candidate.
        codex = tmp_path / ".codex"
        codex.mkdir(parents=True)
        (codex / "auth.json").write_text('{"OPENAI_API_KEY": "%s"}' % _OPENAI_KEY, encoding="utf-8")
        openai_dir = tmp_path / ".openai"
        openai_dir.mkdir(parents=True)
        (openai_dir / "credentials").write_text("api_key = %s\n" % _OPENAI_KEY, encoding="utf-8")
        found = ai_setup._discover_imported_api_keys(home=tmp_path)
        assert len(found) == 1
        assert found[0].provider == "openai"

    def test_never_reads_a_symlink_escaping_home(self, tmp_path):
        # A dotfile symlinked OUTSIDE home must not be read (HOME confinement).
        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "leak"
        secret.write_text('{"OPENAI_API_KEY": "%s"}' % _OPENAI_KEY, encoding="utf-8")
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "auth.json").symlink_to(secret)
        found = ai_setup._discover_imported_api_keys(home=home)
        assert found == []

    def test_is_within_home_rejects_paths_outside_home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # A sibling directory outside home is rejected.
        assert ai_setup._is_within_home(outside / "x", home) is False
        # A plain (non-symlinked) path under home is accepted.
        assert ai_setup._is_within_home(home / ".config" / "gh" / "hosts.yml", home) is True


class TestGithubLabel:
    def test_github_gets_a_distinct_display_label(self):
        assert ai_setup._import_provider_label("github") == "GitHub Models"

    def test_known_provider_uses_display_name(self):
        assert ai_setup._import_provider_label("openai") == "OpenAI"


# ---------------------------------------------------------------------------
# Offer (opt-in) flow
# ---------------------------------------------------------------------------


def _interactive(monkeypatch):
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True, raising=False)


class TestOfferImportFromOtherClis:
    def test_non_tty_discovers_and_persists_nothing(self, monkeypatch):
        monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
        called = {"discover": False}
        monkeypatch.setattr(
            ai_setup,
            "_discover_imported_api_keys",
            lambda home=None: called.__setitem__("discover", True) or [],
        )
        console, _ = _stub_console()
        assert ai_setup._offer_import_from_other_clis(console) is None
        assert called["discover"] is False

    def test_declining_persists_nothing(self, monkeypatch):
        _interactive(monkeypatch)
        monkeypatch.setattr(
            ai_setup,
            "_discover_imported_api_keys",
            lambda home=None: [ai_setup._ImportedKey("openai", "the Codex CLI (x)", _OPENAI_KEY)],
        )
        monkeypatch.setattr(ai_setup, "_list_configured_providers", lambda: [])
        monkeypatch.setattr(ai_setup, "ask_confirmation", lambda *a, **k: False)
        persisted = {"save": False}
        monkeypatch.setattr(
            ai_setup, "_save_ai_config", lambda *a, **k: persisted.__setitem__("save", True) or True
        )
        console, _ = _stub_console()
        assert ai_setup._offer_import_from_other_clis(console) is None
        assert persisted["save"] is False

    def test_accepting_validates_then_persists(self, monkeypatch):
        _interactive(monkeypatch)
        monkeypatch.setattr(
            ai_setup,
            "_discover_imported_api_keys",
            lambda home=None: [ai_setup._ImportedKey("openai", "the Codex CLI (x)", _OPENAI_KEY)],
        )
        monkeypatch.setattr(ai_setup, "_list_configured_providers", lambda: [])
        monkeypatch.setattr(ai_setup, "ask_confirmation", lambda *a, **k: True)
        validated = {"key": None}
        monkeypatch.setattr(
            ai_setup,
            "_validate_api_key",
            lambda provider, key: validated.__setitem__("key", key) or None,
        )
        monkeypatch.setattr(
            ai_setup, "_resolve_import_provider_obj", lambda p: _FakeProvider("openai")
        )
        monkeypatch.setattr(ai_setup, "_save_key_to_keyring", lambda p, k: True)
        saved = {}
        monkeypatch.setattr(
            ai_setup,
            "_save_ai_config",
            lambda provider, model, *, api_key=None, **k: saved.update(provider=provider) or True,
        )
        monkeypatch.setattr(ai_setup, "get_catalog_tier_model", lambda p, t: None, raising=False)
        console, _ = _stub_console()
        cfg = ai_setup._offer_import_from_other_clis(console)
        assert cfg is not None
        assert cfg.provider == "openai"
        assert validated["key"] == _OPENAI_KEY  # validated before persist
        assert saved == {"provider": "openai"}

    def test_validation_failure_skips_candidate(self, monkeypatch):
        _interactive(monkeypatch)
        monkeypatch.setattr(
            ai_setup,
            "_discover_imported_api_keys",
            lambda home=None: [ai_setup._ImportedKey("openai", "the Codex CLI (x)", _OPENAI_KEY)],
        )
        monkeypatch.setattr(ai_setup, "_list_configured_providers", lambda: [])
        monkeypatch.setattr(ai_setup, "ask_confirmation", lambda *a, **k: True)
        monkeypatch.setattr(
            ai_setup, "_resolve_import_provider_obj", lambda p: _FakeProvider("openai")
        )
        monkeypatch.setattr(ai_setup, "_validate_api_key", lambda provider, key: "Invalid key")
        persisted = {"save": False}
        monkeypatch.setattr(
            ai_setup, "_save_ai_config", lambda *a, **k: persisted.__setitem__("save", True) or True
        )
        console, _ = _stub_console()
        assert ai_setup._offer_import_from_other_clis(console) is None
        assert persisted["save"] is False

    def test_skips_already_configured_provider(self, monkeypatch):
        _interactive(monkeypatch)
        monkeypatch.setattr(
            ai_setup,
            "_discover_imported_api_keys",
            lambda home=None: [ai_setup._ImportedKey("openai", "the Codex CLI (x)", _OPENAI_KEY)],
        )
        monkeypatch.setattr(ai_setup, "_list_configured_providers", lambda: ["openai"])
        asked = {"n": 0}
        monkeypatch.setattr(
            ai_setup, "ask_confirmation", lambda *a, **k: asked.__setitem__("n", asked["n"] + 1)
        )
        console, _ = _stub_console()
        assert ai_setup._offer_import_from_other_clis(console) is None
        assert asked["n"] == 0

    def test_key_value_never_printed_or_logged(self, monkeypatch, caplog):
        _interactive(monkeypatch)
        monkeypatch.setattr(
            ai_setup,
            "_discover_imported_api_keys",
            lambda home=None: [ai_setup._ImportedKey("openai", "the Codex CLI (x)", _OPENAI_KEY)],
        )
        monkeypatch.setattr(ai_setup, "_list_configured_providers", lambda: [])
        monkeypatch.setattr(ai_setup, "ask_confirmation", lambda *a, **k: True)
        monkeypatch.setattr(ai_setup, "_validate_api_key", lambda provider, key: None)
        monkeypatch.setattr(
            ai_setup, "_resolve_import_provider_obj", lambda p: _FakeProvider("openai")
        )
        monkeypatch.setattr(ai_setup, "_save_key_to_keyring", lambda p, k: True)
        monkeypatch.setattr(ai_setup, "_save_ai_config", lambda *a, **k: True)
        monkeypatch.setattr(ai_setup, "get_catalog_tier_model", lambda p, t: None, raising=False)
        console, printed = _stub_console()
        with caplog.at_level(logging.DEBUG, logger="fluid.cli.ai_setup"):
            cfg = ai_setup._offer_import_from_other_clis(console)
        assert cfg is not None and cfg.api_key == _OPENAI_KEY  # returned for the session
        joined = "\n".join(printed)
        assert _OPENAI_KEY not in joined  # never displayed to the user
        assert _OPENAI_KEY not in caplog.text  # never logged

    def test_preserves_other_saved_providers(self, monkeypatch, tmp_path):
        # Real _save_ai_config against a tmp config file: importing openai must
        # NOT erase a previously-saved gemini provider (multi-provider path).
        config_file = tmp_path / "ai_config.json"
        monkeypatch.setattr(ai_setup, "_CONFIG_FILE", config_file, raising=False)
        monkeypatch.setattr(ai_setup, "_CONFIG_DIR", tmp_path, raising=False)
        monkeypatch.setattr(ai_setup, "_save_key_to_keyring", lambda p, k: True)
        # Pre-seed a saved gemini provider.
        assert ai_setup._save_ai_config("gemini", "gemini-x", api_key=_GEMINI_KEY)

        _interactive(monkeypatch)
        monkeypatch.setattr(
            ai_setup,
            "_discover_imported_api_keys",
            lambda home=None: [ai_setup._ImportedKey("openai", "the Codex CLI (x)", _OPENAI_KEY)],
        )
        monkeypatch.setattr(ai_setup, "ask_confirmation", lambda *a, **k: True)
        monkeypatch.setattr(ai_setup, "_validate_api_key", lambda provider, key: None)
        monkeypatch.setattr(
            ai_setup, "_resolve_import_provider_obj", lambda p: _FakeProvider("openai")
        )
        monkeypatch.setattr(ai_setup, "get_catalog_tier_model", lambda p, t: None, raising=False)
        cfg = ai_setup._offer_import_from_other_clis(console=_stub_console()[0])
        assert cfg is not None and cfg.provider == "openai"
        # Both providers survive; openai is now active.
        assert ai_setup._list_configured_providers() == ["gemini", "openai"]


# ---------------------------------------------------------------------------
# Wiring into the setup entry points
# ---------------------------------------------------------------------------


class TestWiring:
    def test_inline_offers_import_before_interactive_picker(self, monkeypatch):
        monkeypatch.setattr(ai_setup, "_ai_setup_skipped", False, raising=False)
        monkeypatch.setattr(ai_setup, "_load_ai_config", lambda: None)
        monkeypatch.setattr(ai_setup, "PROVIDER_ENV_VARS", {}, raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.setattr(ai_setup, "detect_ollama_available", lambda env: False)
        monkeypatch.setattr(ai_setup.shutil, "which", lambda b: None)
        monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
        sentinel = object()
        monkeypatch.setattr(ai_setup, "_offer_import_from_other_clis", lambda console: sentinel)
        # If import wins, the manual picker must NOT be reached.
        monkeypatch.setattr(
            ai_setup,
            "_prompt_for_api_key",
            lambda *a, **k: pytest.fail("picker reached despite an accepted import"),
        )
        console, _ = _stub_console()
        assert ai_setup.run_ai_setup_inline(console) is sentinel

    def test_interactive_offers_import_when_no_preselected_provider(self, monkeypatch):
        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

        monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True, raising=False)
        readiness = types.SimpleNamespace(ready=False, provider=None, model=None, error=None)
        monkeypatch.setattr(ai_setup, "check_llm_readiness", lambda: readiness, raising=False)
        imported = LlmConfig(
            provider="openai", model="gpt-4o", endpoint="https://x", api_key=_OPENAI_KEY
        )
        offered = {"n": 0}
        monkeypatch.setattr(
            ai_setup,
            "_offer_import_from_other_clis",
            lambda console: offered.__setitem__("n", offered["n"] + 1) or imported,
        )
        monkeypatch.setattr(
            ai_setup,
            "_prompt_for_api_key",
            lambda *a, **k: pytest.fail("picker reached despite an accepted import"),
        )
        console, _ = _stub_console()
        result = ai_setup.run_ai_setup_interactive(console)
        assert result is imported
        assert offered["n"] == 1

    def test_interactive_skips_import_when_provider_preselected(self, monkeypatch):
        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

        monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True, raising=False)
        readiness = types.SimpleNamespace(ready=False, provider=None, model=None, error=None)
        monkeypatch.setattr(ai_setup, "check_llm_readiness", lambda: readiness, raising=False)
        monkeypatch.setattr(
            ai_setup,
            "_offer_import_from_other_clis",
            lambda console: pytest.fail("import offered despite explicit --provider"),
        )
        cfg = LlmConfig(provider="openai", model="gpt-4o", endpoint="https://x", api_key=None)
        monkeypatch.setattr(ai_setup, "_prompt_for_api_key", lambda *a, **k: cfg)
        console, _ = _stub_console()
        result = ai_setup.run_ai_setup_interactive(console, preselected_provider="openai")
        assert result is cfg
