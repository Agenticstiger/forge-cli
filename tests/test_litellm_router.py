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

"""Tests for ``fluid_build.cli.forge_llm_router``.

Covers the four scenarios spelled out in the Wave-1 spec:

* env var set → router constructed from the env-supplied chain;
* env var unset, Claude primary → default fallback chain built;
* env var unset, GPT primary → ``get_router`` returns ``None``;
* 429 on primary → router falls over to the next deployment.

The router is patched at module level so we never spin up a real
litellm.Router (heavy + needs creds). Receipts: searches for
``litellm Router`` docs, ``fallbacks=[{...}]`` shape, and the
``cooldown_time``/``num_retries`` constructor parameters were run
before writing this file.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List
from unittest import mock

import pytest


def _reset_router_singleton() -> None:
    from fluid_build.cli import forge_llm_router

    forge_llm_router._reset_for_testing()


@pytest.fixture(autouse=True)
def _clean_router_singleton(monkeypatch):
    """Reset the module-level Router singleton between tests so each
    test starts from a clean slate (otherwise the first test's
    constructed router would survive into subsequent tests)."""
    _reset_router_singleton()
    monkeypatch.delenv("FLUID_LLM_FALLBACK_CHAIN", raising=False)
    yield
    _reset_router_singleton()


def _install_fake_litellm(monkeypatch, *, router_factory=None):
    """Drop a fake ``litellm`` module into ``sys.modules``.

    ``router_factory`` is a callable that returns the Router instance to
    use. By default it returns a MagicMock that records calls.
    """
    fake = mock.MagicMock(spec=["Router", "completion", "completion_cost"])
    if router_factory is None:
        router_instance = mock.MagicMock()
        router_instance.completion.return_value = {
            "choices": [{"message": {"content": "from-router"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        fake.Router.return_value = router_instance
    else:
        fake.Router.side_effect = router_factory
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


# ---------------------------------------------------------------------------
# should_use_router heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", True),
        ("claude-opus-4-7", True),
        ("anthropic/claude-3-5-sonnet-latest", True),
        ("gpt-4o", False),
        ("gpt-4.1-mini", False),
        ("gemini-2.5-pro", False),
        ("", False),
    ],
)
def test_should_use_router_heuristic(model, expected):
    from fluid_build.cli.forge_llm_router import should_use_router

    assert should_use_router(model) is expected


def test_should_use_router_env_var_forces_true(monkeypatch):
    monkeypatch.setenv("FLUID_LLM_FALLBACK_CHAIN", "openai/gpt-4o,anthropic/claude-haiku-4-5")
    from fluid_build.cli.forge_llm_router import should_use_router

    # Even a non-Claude primary now routes (operator opted in).
    assert should_use_router("gpt-4o") is True


# ---------------------------------------------------------------------------
# get_router — env var path
# ---------------------------------------------------------------------------


def test_env_var_set_builds_router_with_that_model_list(monkeypatch):
    monkeypatch.setenv(
        "FLUID_LLM_FALLBACK_CHAIN",
        "anthropic/claude-sonnet-4-6, bedrock/anthropic.claude-3-5-sonnet-v1:0",
    )
    fake = _install_fake_litellm(monkeypatch)

    from fluid_build.cli.forge_llm_router import get_router

    router = get_router("claude-sonnet-4-6")
    assert router is not None

    fake.Router.assert_called_once()
    kwargs = fake.Router.call_args.kwargs
    model_list: List[Dict[str, Any]] = kwargs["model_list"]
    # Both deployments are grouped under the first entry's id as
    # model_name so router.completion(model=<group>) rotates across them.
    assert [m["litellm_params"]["model"] for m in model_list] == [
        "anthropic/claude-sonnet-4-6",
        "bedrock/anthropic.claude-3-5-sonnet-v1:0",
    ]
    assert all(m["model_name"] == "anthropic/claude-sonnet-4-6" for m in model_list)
    # Operational defaults — match the litellm docs' recommendations.
    assert kwargs["cooldown_time"] == 60
    assert kwargs["num_retries"] == 3
    assert kwargs["retry_after"] == 2
    assert kwargs["set_verbose"] is False


# ---------------------------------------------------------------------------
# get_router — default chain (Claude primary, no env var)
# ---------------------------------------------------------------------------


def test_claude_primary_no_env_var_builds_default_chain(monkeypatch):
    fake = _install_fake_litellm(monkeypatch)

    from fluid_build.cli.forge_llm_router import _default_model_list_for, get_router

    router = get_router("claude-sonnet-4-6")
    assert router is not None

    fake.Router.assert_called_once()
    model_list = fake.Router.call_args.kwargs["model_list"]
    # Default chain has three deployments: anthropic → bedrock → vertex_ai.
    assert len(model_list) == 3
    models = [m["litellm_params"]["model"] for m in model_list]
    assert any(m.startswith("anthropic/") for m in models)
    assert any(m.startswith("bedrock/") for m in models)
    assert any(m.startswith("vertex_ai/") for m in models)
    # _default_model_list_for is the pure-function building block.
    assert _default_model_list_for("claude-sonnet-4-6") == model_list


def test_default_chain_groups_under_primary_model_name():
    """All three deployments share one ``model_name`` so the Router
    treats them as a single group with three peers — required for the
    rotation semantic that gives us free cross-cloud failover."""
    from fluid_build.cli.forge_llm_router import _default_model_list_for

    model_list = _default_model_list_for("claude-haiku-4-5")
    names = {m["model_name"] for m in model_list}
    assert names == {"claude-haiku-4-5"}


# ---------------------------------------------------------------------------
# get_router — non-Claude primary returns None
# ---------------------------------------------------------------------------


def test_gpt_primary_no_env_var_returns_none(monkeypatch):
    fake = _install_fake_litellm(monkeypatch)

    from fluid_build.cli.forge_llm_router import get_router

    router = get_router("gpt-4o")
    assert router is None
    fake.Router.assert_not_called()


def test_gemini_primary_no_env_var_returns_none(monkeypatch):
    fake = _install_fake_litellm(monkeypatch)

    from fluid_build.cli.forge_llm_router import get_router

    assert get_router("gemini-2.5-pro") is None
    fake.Router.assert_not_called()


# ---------------------------------------------------------------------------
# Singleton: repeated calls with the same primary return the same instance
# ---------------------------------------------------------------------------


def test_get_router_caches_per_primary(monkeypatch):
    _install_fake_litellm(monkeypatch)

    from fluid_build.cli.forge_llm_router import get_router

    a = get_router("claude-sonnet-4-6")
    b = get_router("claude-sonnet-4-6")
    assert a is b


def test_router_construction_failure_returns_none(monkeypatch):
    """A litellm.Router constructor blow-up should not kill the run —
    we fall back to the direct ``litellm.completion`` path. Pattern
    matches every other "never block the forge" branch in the file."""
    fake = mock.MagicMock(spec=["Router", "completion", "completion_cost"])
    fake.Router.side_effect = RuntimeError("router construction failed")
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from fluid_build.cli.forge_llm_router import get_router

    assert get_router("claude-sonnet-4-6") is None


# ---------------------------------------------------------------------------
# Fallback semantics — primary 429 → fallback receives the call
# ---------------------------------------------------------------------------


def test_router_fallback_on_429(monkeypatch):
    """When the primary deployment 429s, the Router rotates to the
    next deployment in the same model_name group. We don't reimplement
    litellm's retry/cooldown — we just assert the wrapper hands the
    call to router.completion() and the response surfaces unchanged.

    The Router itself owns the retry loop; mocking it lets us emulate
    the post-fallback state (primary failed, fallback succeeded) and
    confirm the wrapper returns the fallback's response.
    """
    # Configure the fake Router so .completion() returns a stub
    # response shaped like the second (fallback) deployment served the
    # request after the primary's 429. The Router doesn't surface
    # which deployment served the request in the response shape — it
    # just returns whichever model worked. We confirm the response
    # propagates back through the litellm adapter unchanged.
    fallback_response = {
        "choices": [{"message": {"content": "served by fallback"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    router_instance = mock.MagicMock()
    router_instance.completion.return_value = fallback_response

    fake = mock.MagicMock(spec=["Router", "completion", "completion_cost"])
    fake.Router.return_value = router_instance
    fake.completion.side_effect = AssertionError(
        "litellm.completion should NOT be called when Router handles routing"
    )
    fake.completion_cost.return_value = 0.0001
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    provider = LiteLLMProvider("anthropic")
    cfg = LlmConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        endpoint="litellm://anthropic/claude-sonnet-4-6",
        api_key="sk-test",
    )
    text = provider.invoke_blocking(cfg, "system", "user")
    assert text == "served by fallback"

    # The Router was used (not bare litellm.completion).
    router_instance.completion.assert_called_once()
    fake.completion.assert_not_called()
