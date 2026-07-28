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

"""Pin the live-LLM provider-resolution contract.

The live-LLM e2e tests use a ``_resolve_provider()`` helper that
prefers Anthropic Haiku 4.5 over OpenAI gpt-4o-mini when both keys
are present. We can't run the real Haiku LLM without a paid key, but
we CAN regression-test the dispatch logic so the day someone sets
``ANTHROPIC_API_KEY`` they get the Haiku path, not silently the
OpenAI fallback.

Skip-with-clear-message coverage is also pinned: when no key is set
the e2e tests must skip cleanly (not crash, not silently no-op).
"""

from __future__ import annotations

import importlib
import sys

import pytest

# Force-reimport the live-LLM module so per-test monkeypatch of
# os.environ is visible to module-level helpers.
LIVE_LLM_MODULE = "tests.integration.test_mcp_output_port_live_llm"

# Every provider key _resolve_provider() consults. Cleared before each test so
# resolution is deterministic regardless of the developer's ambient env (a real
# GEMINI_API_KEY / GOOGLE_API_KEY in the shell otherwise leaks into the
# no-keys / single-provider cases).
_PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


@pytest.fixture(autouse=True)
def _clear_provider_keys(monkeypatch: pytest.MonkeyPatch):
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)


def _fresh_module():
    if LIVE_LLM_MODULE in sys.modules:
        del sys.modules[LIVE_LLM_MODULE]
    return importlib.import_module(LIVE_LLM_MODULE)


def test_anthropic_preferred_when_both_keys_present(monkeypatch: pytest.MonkeyPatch):
    """When operators have BOTH keys configured, the test suite
    routes to Anthropic Haiku — this is the explicit preference
    declared in the live-test docstring."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-fake-for-test")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-fake-for-test")
    mod = _fresh_module()
    model, bare = mod._resolve_provider()
    assert model == "anthropic/claude-haiku-4-5-20251001"
    assert bare == "claude-haiku-4-5-20251001"


def test_openai_fallback_when_only_openai_key_present(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-fake-for-test")
    mod = _fresh_module()
    model, bare = mod._resolve_provider()
    assert model == "openai/gpt-4o-mini"
    assert bare == "gpt-4o-mini"


def test_gemini_fallback_when_only_gemini_key_present(monkeypatch: pytest.MonkeyPatch):
    """Gemini is the third provider (after Anthropic + OpenAI) — set when the
    project's ai_config uses gemini-2.5-flash and only GEMINI_API_KEY is present."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-fake-for-test")
    mod = _fresh_module()
    model, bare = mod._resolve_provider()
    assert model == "gemini/gemini-2.5-flash"
    assert bare == "gemini-2.5-flash"


def test_skip_when_no_keys_present(monkeypatch: pytest.MonkeyPatch):
    """Production CI without any LLM secrets must SKIP, not FAIL.
    pytest.skip raises pytest.skip.Exception under the hood. The autouse
    fixture has cleared every provider key (incl. GEMINI/GOOGLE)."""
    mod = _fresh_module()
    with pytest.raises(pytest.skip.Exception):
        mod._resolve_provider()


def test_anthropic_only_key_resolves_to_haiku(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-fake-for-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mod = _fresh_module()
    model, bare = mod._resolve_provider()
    assert model == "anthropic/claude-haiku-4-5-20251001"
