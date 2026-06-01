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
