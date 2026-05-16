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

"""LiteLLM backend adapter — shape parity + dispatcher routing.

These tests run with a fake ``litellm`` module so they don't require the
real ~50 MB dependency. They prove:

* Default routing: every provider name resolves to a ``LiteLLMProvider``
  shim. The native per-provider classes were deleted.
* Shape parity: every ``extract_*`` returns the canonical dict the
  rest of the codebase asserts on
* Cost integration: ``invoke_blocking`` stashes
  ``litellm.completion_cost`` on the thread-local read by the
  staged pipeline
* Streaming: yields chunks AND populates ``_streaming_usage_state``
* Missing dep: typed error with install-hint suggestion
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Fake litellm — injected per-test
# ---------------------------------------------------------------------------


def _fake_litellm_module(*, completion_response=None, completion_cost=0.0042):
    """Build a stand-in ``litellm`` module with the bits we use."""
    module = mock.MagicMock(spec=["completion", "completion_cost"])
    if completion_response is None:
        completion_response = {
            "choices": [
                {
                    "message": {"content": "hello world"},
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        }
    module.completion.return_value = completion_response
    module.completion_cost.return_value = completion_cost
    return module


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------


def test_default_uses_litellm():
    """Every provider is a :class:`LiteLLMProvider`. The native
    per-provider classes were deleted; ``FLUID_LLM_BACKEND`` no longer
    exists as an opt-out switch.

    Import order matters: load ``llm_providers`` first (it lazily
    imports ``llm_litellm`` at provider-construction time), then read
    the ``LiteLLMProvider`` symbol for the isinstance check.
    Reversing this order triggers a circular import in cold cache.
    """
    from fluid_build.cli.forge_copilot_llm_providers import get_llm_provider

    provider = get_llm_provider("openai")
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    assert isinstance(provider, LiteLLMProvider)
    assert provider.name == "openai"


def test_anthropic_alias_routes_to_anthropic():
    """``claude`` alias normalises to ``anthropic``."""
    from fluid_build.cli.forge_copilot_llm_providers import get_llm_provider

    provider = get_llm_provider("claude")
    assert provider.name == "anthropic"


def test_provider_lookup_caches():
    """Repeated lookups return the same instance (litellm cache)."""
    from fluid_build.cli.forge_copilot_llm_providers import get_llm_provider

    a = get_llm_provider("openai")
    b = get_llm_provider("openai")
    assert a is b


# ---------------------------------------------------------------------------
# Model name resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_name,model,expected",
    [
        ("openai", "gpt-4o", "openai/gpt-4o"),
        ("anthropic", "claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"),
        ("gemini", "gemini-2.5-flash", "gemini/gemini-2.5-flash"),
        ("groq", "llama-3.1-70b-versatile", "groq/llama-3.1-70b-versatile"),
        ("bedrock", "anthropic.claude-3-sonnet-v1:0", "bedrock/anthropic.claude-3-sonnet-v1:0"),
        ("github", "gpt-4o-mini", "github/gpt-4o-mini"),
    ],
)
def test_model_name_translation(provider_name, model, expected):
    from fluid_build.cli.forge_copilot_llm_litellm import _litellm_model_for

    assert _litellm_model_for(provider_name, model) == expected


def test_model_prefix_override_via_env(monkeypatch):
    """FLUID_LITELLM_MODEL_PREFIX wins for unusual providers."""
    monkeypatch.setenv("FLUID_LITELLM_MODEL_PREFIX", "azure")
    from fluid_build.cli.forge_copilot_llm_litellm import _litellm_model_for

    assert _litellm_model_for("openai", "gpt-4o") == "azure/gpt-4o"


# ---------------------------------------------------------------------------
# GitHub Models — litellm `github/` provider, zero-API-key in CI
# ---------------------------------------------------------------------------


def test_github_provider_routes_to_litellm():
    """``github`` resolves to a LiteLLMProvider — GitHub Models needs no
    bespoke provider class, just litellm's ``github/`` prefix."""
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import get_llm_provider

    provider = get_llm_provider("github")
    assert isinstance(provider, LiteLLMProvider)
    assert provider.name == "github"


def test_github_api_key_env_var_is_registered():
    """``GITHUB_API_KEY`` is the resolver env var for the github provider —
    in GitHub Actions this is the built-in ``GITHUB_TOKEN``."""
    from fluid_build.cli.forge_copilot_llm_providers import PROVIDER_ENV_VARS

    assert PROVIDER_ENV_VARS["github"] == "GITHUB_API_KEY"


def test_github_api_key_resolves_from_env():
    """``_resolve_api_key`` picks up ``GITHUB_API_KEY`` for the github provider."""
    from fluid_build.cli.forge_copilot_llm_providers import _resolve_api_key

    assert _resolve_api_key("github", {"GITHUB_API_KEY": "ghp-fake-token"}) == "ghp-fake-token"


def test_github_provider_is_not_auto_inferred():
    """A stray ``GITHUB_API_KEY`` must NOT auto-select the github provider.

    GitHub Actions sets ``GITHUB_TOKEN`` in every run, so github is
    opt-in only (``FLUID_LLM_PROVIDER=github`` / ``--llm-provider github``).
    """
    from fluid_build.cli.forge_copilot_llm_providers import _infer_provider_from_env

    assert _infer_provider_from_env({"GITHUB_API_KEY": "ghp-fake-token"}) != "github"


# ---------------------------------------------------------------------------
# Shape parity — every extract_* returns the canonical dict
# ---------------------------------------------------------------------------


def test_extract_text_returns_first_choice_content():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("openai")
    resp = {"choices": [{"message": {"content": "hello"}}]}
    assert p.extract_text(resp) == "hello"


def test_extract_text_handles_malformed_payload():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("openai")
    assert p.extract_text({}) == ""
    assert p.extract_text(None) == ""  # type: ignore[arg-type]


def test_extract_usage_returns_canonical_dict():
    """`{input_tokens, output_tokens, total_tokens}` — the contract every test asserts on."""
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("anthropic")
    usage = p.extract_usage(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
    )
    assert usage == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


def test_extract_usage_zero_defaults_on_missing():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("openai")
    usage = p.extract_usage({})
    assert usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def test_extract_usage_computes_total_when_missing():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("openai")
    usage = p.extract_usage({"usage": {"prompt_tokens": 30, "completion_tokens": 70}})
    assert usage["total_tokens"] == 100


def test_extract_prompt_cache_reads_normalised_field():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("anthropic")
    metrics = p.extract_prompt_cache(
        {
            "usage": {
                "prompt_tokens": 1000,
                "prompt_tokens_details": {"cached_tokens": 700},
            }
        }
    )
    assert metrics["read_tokens"] == 700
    assert metrics["total_tokens"] == 1000
    assert metrics["hit_rate"] == pytest.approx(0.7)


def test_extract_tool_calls_canonical_shape():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("openai")
    resp = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "discover_workspace",
                                "arguments": '{"workspace_path":"."}',
                            },
                        }
                    ]
                }
            }
        ]
    }
    calls = p.extract_tool_calls(resp)
    assert calls == [
        {
            "id": "call_123",
            "name": "discover_workspace",
            "arguments": {"workspace_path": "."},
        }
    ]


def test_extract_tool_calls_handles_malformed_arguments():
    """Bad JSON in arguments must not raise — yields empty dict + LLM retries."""
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    p = LiteLLMProvider("openai")
    resp = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "1",
                            "function": {"name": "x", "arguments": "not-json"},
                        }
                    ]
                }
            }
        ]
    }
    calls = p.extract_tool_calls(resp)
    assert calls == [{"id": "1", "name": "x", "arguments": {}}]


# ---------------------------------------------------------------------------
# invoke_blocking — calls litellm + updates cumulative usage + cost
# ---------------------------------------------------------------------------


def test_invoke_blocking_calls_litellm_and_records_usage(monkeypatch):
    fake = _fake_litellm_module()
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from fluid_build.cli.forge_copilot_llm_litellm import (
        LiteLLMProvider,
        get_last_litellm_cost_usd,
    )
    from fluid_build.cli.forge_copilot_llm_providers import (
        LlmConfig,
        _cumulative_usage,
        reset_token_usage,
    )

    reset_token_usage()
    provider = LiteLLMProvider("openai")
    config = LlmConfig(
        provider="openai",
        model="gpt-4o",
        endpoint="litellm://openai/gpt-4o",
        api_key="sk-test",
    )
    text = provider.invoke_blocking(config, "system", "user")

    fake.completion.assert_called_once()
    kwargs = fake.completion.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-4o"
    assert kwargs["api_key"] == "sk-test"
    assert text == "hello world"
    assert _cumulative_usage["input_tokens"] >= 100
    assert _cumulative_usage["output_tokens"] >= 50
    # Cost stashed for the staged pipeline to read
    assert get_last_litellm_cost_usd() == pytest.approx(0.0042)


def test_invoke_blocking_translates_exceptions(monkeypatch):
    fake = mock.MagicMock(spec=["completion", "completion_cost"])
    fake.completion.side_effect = RuntimeError("boom")
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import (
        CopilotGenerationError,
        LlmConfig,
    )

    p = LiteLLMProvider("openai")
    cfg = LlmConfig(provider="openai", model="gpt-4o", endpoint="x", api_key="sk-x")
    with pytest.raises(CopilotGenerationError) as excinfo:
        p.invoke_blocking(cfg, "s", "u")
    assert excinfo.value.event == "copilot_litellm_request_failed"


# ---------------------------------------------------------------------------
# invoke_streaming — yields chunks + populates _streaming_usage_state
# ---------------------------------------------------------------------------


def test_invoke_streaming_yields_chunks_and_records_usage(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " world"}}]},
        {
            "choices": [{"delta": {"content": ""}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    ]
    fake = mock.MagicMock(spec=["completion", "completion_cost"])
    fake.completion.return_value = iter(chunks)
    fake.completion_cost.return_value = 0.0001
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import (
        LlmConfig,
        consume_streaming_usage,
        reset_token_usage,
    )

    reset_token_usage()
    p = LiteLLMProvider("openai")
    cfg = LlmConfig(provider="openai", model="gpt-4o", endpoint="x", api_key="sk-x")
    out = list(p.invoke_streaming(cfg, "s", "u"))
    assert "".join(out) == "Hello world"
    usage = consume_streaming_usage()
    assert usage is not None
    assert usage["input_tokens"] == 5
    assert usage["output_tokens"] == 2


# ---------------------------------------------------------------------------
# Missing-dep path
# ---------------------------------------------------------------------------


def test_missing_litellm_raises_typed_error(monkeypatch):
    """Without litellm installed, invoke_blocking raises a typed error
    with the install-hint suggestion."""
    monkeypatch.setitem(sys.modules, "litellm", None)
    # Force re-import path to hit ImportError
    import builtins as _builtins

    real_import = _builtins.__import__

    def _bad_import(name, *args, **kwargs):
        if name == "litellm":
            raise ImportError("no litellm")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_builtins, "__import__", _bad_import)

    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import (
        CopilotGenerationError,
        LlmConfig,
    )

    p = LiteLLMProvider("openai")
    cfg = LlmConfig(provider="openai", model="gpt-4o", endpoint="x", api_key="sk-x")
    with pytest.raises(CopilotGenerationError) as excinfo:
        p.invoke_blocking(cfg, "s", "u")
    assert excinfo.value.event == "copilot_litellm_unavailable"
    suggestions = excinfo.value.suggestions
    assert any("pip install" in s for s in suggestions)
    # The hint points at re-installing fluid-build (litellm is hard dep).
    assert any("fluid-build" in s or "litellm" in s for s in suggestions)


# ---------------------------------------------------------------------------
# Cost integration — usd_override flows through RunCostTracker
# ---------------------------------------------------------------------------


def test_run_cost_tracker_honours_usd_override():
    """litellm-derived USD wins over the embedded MODEL_PRICES_USD."""
    from fluid_build.copilot.cost import RunCostTracker

    tr = RunCostTracker()
    tr.record_call(
        provider="bedrock",  # Not in MODEL_PRICES_USD
        model="custom-bedrock-model",
        input_tokens=100,
        output_tokens=50,
        usd_override=0.0123,
    )
    bd = tr.breakdown()
    # The override produces an authoritative total even though the
    # legacy MODEL_PRICES_USD doesn't know this model.
    assert bd.total_usd == pytest.approx(0.0123)


def test_run_cost_tracker_no_override_falls_back_to_table():
    """Backward compatibility: no override → legacy table lookup."""
    from fluid_build.copilot.cost import RunCostTracker

    tr = RunCostTracker()
    tr.record_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=1000,
        output_tokens=500,
    )
    bd = tr.breakdown()
    # OpenAI gpt-4o is in MODEL_PRICES_USD → known cost
    assert bd.total_usd is not None and bd.total_usd > 0


# ---------------------------------------------------------------------------
# call_llm short-circuit
# ---------------------------------------------------------------------------


def test_call_llm_routes_to_litellm_provider(monkeypatch):
    fake = _fake_litellm_module(
        completion_response={
            "choices": [{"message": {"content": "routed"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
    )
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from fluid_build.cli.forge_copilot_llm_providers import (
        LlmConfig,
        call_llm,
        get_llm_provider,
        reset_token_usage,
    )

    reset_token_usage()
    provider = get_llm_provider("openai")
    cfg = LlmConfig(provider="openai", model="gpt-4o", endpoint="x", api_key="sk-x")
    text = call_llm(provider, cfg, "system", "user")
    assert text == "routed"
    fake.completion.assert_called_once()  # didn't go through httpx
