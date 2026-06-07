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

"""Part B — ai_setup offers keyless Claude Code (offer, don't force).

Covers the two non-prompt entry points: loading a saved coding-agent provider,
and the step-4.5 auto-select when Claude Code is the only thing available in a
non-interactive run. The interactive confirm path is exercised by the cross-
agent matrix in Part C.
"""

from __future__ import annotations

import types

import pytest

from fluid_build.cli import ai_setup

pytestmark = pytest.mark.unit


def test_make_coding_agent_config_is_keyless():
    cfg = ai_setup._make_coding_agent_config("claude-code")
    assert cfg.provider == "claude-code"
    assert cfg.api_key is None
    assert cfg.endpoint.startswith("coding-agent://")


def test_inline_setup_loads_saved_coding_agent_keyless(monkeypatch):
    monkeypatch.setattr(ai_setup, "_ai_setup_skipped", False, raising=False)
    monkeypatch.setattr(
        ai_setup,
        "_load_ai_config",
        lambda: {"provider": "claude-code", "model": "claude-code"},
    )
    cfg = ai_setup.run_ai_setup_inline(console=None)
    assert cfg is not None
    assert cfg.provider == "claude-code"
    assert cfg.api_key is None


def test_inline_setup_auto_selects_claude_code_when_only_agent(monkeypatch):
    # No saved config, no cloud key, no Ollama, claude on PATH, non-interactive
    # -> step 4.5 auto-selects keyless Claude Code (mirrors the Ollama branch).
    monkeypatch.setattr(ai_setup, "_ai_setup_skipped", False, raising=False)
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda: None)
    monkeypatch.setattr(ai_setup, "PROVIDER_ENV_VARS", {}, raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(ai_setup, "detect_ollama_available", lambda env: False)
    monkeypatch.setattr(
        ai_setup.shutil, "which", lambda b: "/usr/local/bin/claude" if b == "claude" else None
    )
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))

    cfg = ai_setup.run_ai_setup_inline(console=None)
    assert cfg is not None
    assert cfg.provider == "claude-code"
    assert cfg.api_key is None


def _stub_console():
    import types as _t

    printed = []
    return (
        _t.SimpleNamespace(print=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        printed,
    )


def test_offer_import_env_key_skips_non_tty(monkeypatch):
    # Non-interactive: never persist (CI safety).
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    called = {"save": False}
    monkeypatch.setattr(
        ai_setup, "_save_ai_config", lambda *a, **k: called.__setitem__("save", True) or True
    )
    console, _ = _stub_console()
    ai_setup._offer_import_env_api_key(console, "openai", "sk-live-xxx", "OPENAI_API_KEY")
    assert called["save"] is False


def test_offer_import_env_key_persists_on_yes(monkeypatch):
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True, raising=False)
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda: None)
    monkeypatch.setattr(ai_setup, "ask_confirmation", lambda *a, **k: True)
    saved = {}
    monkeypatch.setattr(
        ai_setup,
        "_save_ai_config",
        lambda provider, model, *, api_key=None, **k: saved.update(
            provider=provider, api_key=api_key
        )
        or True,
    )
    console, _ = _stub_console()
    ai_setup._offer_import_env_api_key(console, "openai", "sk-live-abc", "OPENAI_API_KEY")
    assert saved == {"provider": "openai", "api_key": "sk-live-abc"}


def test_offer_import_env_key_declined_does_not_persist(monkeypatch):
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True, raising=False)
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda: None)
    monkeypatch.setattr(ai_setup, "ask_confirmation", lambda *a, **k: False)
    called = {"save": False}
    monkeypatch.setattr(
        ai_setup, "_save_ai_config", lambda *a, **k: called.__setitem__("save", True) or True
    )
    console, _ = _stub_console()
    ai_setup._offer_import_env_api_key(console, "openai", "sk-live-abc", "OPENAI_API_KEY")
    assert called["save"] is False


def test_offer_import_env_key_skips_when_already_configured(monkeypatch):
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True, raising=False)
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda: {"provider": "openai"})
    called = {"asked": False}
    monkeypatch.setattr(
        ai_setup, "ask_confirmation", lambda *a, **k: called.__setitem__("asked", True) or True
    )
    console, _ = _stub_console()
    ai_setup._offer_import_env_api_key(console, "openai", "sk-live-abc", "OPENAI_API_KEY")
    assert called["asked"] is False


def test_offer_import_env_key_skips_on_shape_mismatch(monkeypatch):
    # An Anthropic-shaped key (sk-ant-) sitting in OPENAI_API_KEY must not be
    # persisted as openai.
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True, raising=False)
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda: None)
    called = {"asked": False}
    monkeypatch.setattr(
        ai_setup, "ask_confirmation", lambda *a, **k: called.__setitem__("asked", True) or True
    )
    console, _ = _stub_console()
    ai_setup._offer_import_env_api_key(console, "openai", "sk-ant-xxxxxxxx", "OPENAI_API_KEY")
    assert called["asked"] is False


def test_inline_setup_no_agent_no_key_returns_none(monkeypatch):
    # Nothing available + non-interactive -> None (existing behavior preserved).
    monkeypatch.setattr(ai_setup, "_ai_setup_skipped", False, raising=False)
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda: None)
    monkeypatch.setattr(ai_setup, "PROVIDER_ENV_VARS", {}, raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(ai_setup, "detect_ollama_available", lambda env: False)
    monkeypatch.setattr(ai_setup.shutil, "which", lambda b: None)
    monkeypatch.setattr(ai_setup.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))

    assert ai_setup.run_ai_setup_inline(console=None) is None
