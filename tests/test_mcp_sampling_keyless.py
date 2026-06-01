"""A1 — keyless LLM providers must not trip the api-key gate.

``resolve_llm_config`` historically raised ``copilot_missing_llm_api_key``
for any non-``ollama`` provider that lacked an API key. ``mcp-sampling`` is
keyless by design — the MCP client (the IDE) pays for the LLM via
``sampling/createMessage`` and forge never sees a key — so
``fluid forge --agent --llm-provider mcp-sampling`` must resolve a config
with ``api_key=None`` instead of raising. Before this fix the in-IDE keyless
path only "worked" when an API key happened to be present in the environment.

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


def test_mcp_sampling_resolves_without_api_key():
    cfg = resolve_llm_config(_args(provider="mcp-sampling"), environ={})
    assert cfg.provider == "mcp-sampling"
    assert cfg.api_key is None


def test_mcp_sampling_underscore_alias_resolves_without_api_key():
    # ``get_llm_provider`` canonicalises the underscore alias to the hyphen
    # form; the keyless gate must still exempt it.
    cfg = resolve_llm_config(_args(provider="mcp_sampling"), environ={})
    assert cfg.provider == "mcp-sampling"
    assert cfg.api_key is None


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
