"""Part B — resolve_llm_config + factory/registry wiring for coding agents.

The keyless gate (A1's ``_KEYLESS_PROVIDERS``) must exempt all four agents:
Claude Code is genuinely keyless, and codex/cursor/kiro validate their own key
*inside the provider* at call time (so the generic gate, which only knows
ANTHROPIC/OPENAI/GEMINI keys, must not reject them).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fluid_build.cli.forge_copilot_coding_agent import CodingAgentProvider
from fluid_build.cli.forge_copilot_llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    get_llm_provider,
    resolve_llm_config,
)

pytestmark = pytest.mark.unit


def _args(**kw):
    base = {"llm_provider": None, "llm_model": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_factory_dispatch_and_litellm_intact():
    assert isinstance(get_llm_provider("claude-code"), CodingAgentProvider)
    assert isinstance(get_llm_provider("cursor-agent"), CodingAgentProvider)
    assert get_llm_provider("cursor-agent").name == "cursor"
    # The litellm short-circuit still resolves real HTTP providers.
    assert type(get_llm_provider("anthropic")).__name__ == "LiteLLMProvider"


def test_registry_membership():
    for name in ("claude-code", "codex", "cursor", "kiro"):
        assert name in BUILTIN_LLM_PROVIDERS
        assert isinstance(BUILTIN_LLM_PROVIDERS[name], CodingAgentProvider)


def test_claude_code_resolves_keyless():
    cfg = resolve_llm_config(_args(llm_provider="claude-code"), environ={})
    assert cfg.provider == "claude-code"
    assert cfg.api_key is None
    assert cfg.agent_mode == "envelope"


def test_codex_resolves_without_standard_key():
    # codex needs CODEX_API_KEY, but that is validated at call time, not here.
    cfg = resolve_llm_config(_args(llm_provider="codex"), environ={})
    assert cfg.provider == "codex"
    assert cfg.api_key is None


def test_agent_mode_from_env():
    cfg = resolve_llm_config(
        _args(llm_provider="claude-code"), environ={"FLUID_FORGE_AGENT_MODE": "agentic"}
    )
    assert cfg.agent_mode == "agentic"


def test_agent_mode_invalid_falls_back_to_envelope():
    cfg = resolve_llm_config(
        _args(llm_provider="claude-code"), environ={"FLUID_FORGE_AGENT_MODE": "bogus"}
    )
    assert cfg.agent_mode == "envelope"


def test_forge_agent_env_selects_provider():
    cfg = resolve_llm_config(_args(), environ={"FLUID_FORGE_AGENT": "cursor"})
    assert cfg.provider == "cursor"


def test_forge_agent_mode_flag_beats_env():
    cfg = resolve_llm_config(
        _args(llm_provider="claude-code", forge_agent_mode="agentic"), environ={}
    )
    assert cfg.agent_mode == "agentic"
