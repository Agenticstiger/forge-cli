# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Keyless LLM providers must not trip the api-key gate.

``resolve_llm_config`` raises ``copilot_missing_llm_api_key`` for any provider
that needs a key and has none. ``ollama`` is keyless by design and must still
resolve a config with ``api_key=None``; a keyed provider must still be refused.

The OS keyring is hermetic here (autouse ``_hermetic_keyring`` fixture in
``tests/conftest.py``), so an empty ``environ`` genuinely means
"no key configured" and ``_resolve_api_key`` cannot leak a real key.
"""

from types import SimpleNamespace

import pytest

from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    resolve_llm_config,
)

pytestmark = pytest.mark.unit


def _args(provider=None, model=None):
    """Minimal argparse-like namespace; unset attrs default to None via getattr."""
    return SimpleNamespace(llm_provider=provider, llm_model=model)


def test_ollama_remains_keyless():
    # Regression guard: ollama was the original (and only) exemption.
    cfg = resolve_llm_config(_args(provider="ollama", model="llama3"), environ={})
    assert cfg.provider == "ollama"
    assert cfg.api_key is None


def test_keyed_provider_still_requires_a_key():
    # The gate must still fire for real HTTP providers with no key.
    with pytest.raises(CopilotGenerationError) as excinfo:
        resolve_llm_config(_args(provider="anthropic"), environ={})
    assert excinfo.value.event == "copilot_missing_llm_api_key"
