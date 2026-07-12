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

"""Tests for the rate-limit (429) observable-retry envelope in
``fluid_build.cli.forge_copilot_llm_litellm`` (Trello: Forge UX rate-limit
awareness).

The card: on a 429 the litellm path retries silently — the user stares at a
frozen spinner. We add a stderr notice ``Rate limited. Waiting {N}s before
retrying...`` before each retry wait, deriving N from the server's Retry-After
(reusing ``error_classification.parse_retry_after``) with a sane default.

Harness mirrors ``tests/test_litellm_router.py``: a fake ``litellm`` module is
dropped into ``sys.modules`` and ``forge_llm_router.get_router`` is patched so
the bare-completion vs Router paths are exercised without a real litellm. All
sleeps are monkeypatched to no-ops so the suite stays fast.

Borrow receipt: litellm exposes NO native pre-retry callback hook
(BerriAI/litellm#19806 — "Closed as not planned"), so a thin adapt-a-wrapper is
the right call rather than depending on a hook that does not exist.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, Iterator, List
from unittest import mock

import pytest


class _FakeRateLimitError(Exception):
    """Stand-in for ``litellm.RateLimitError`` — a real class so the
    ``isinstance(<cls>, type)`` guard in ``_is_rate_limit_error`` accepts it."""


def _success_response(content: str = "recovered") -> Dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _fake_litellm(*, extra_spec: List[str] | None = None) -> Any:
    """Fake ``litellm`` module carrying a real ``RateLimitError`` class."""
    spec = ["completion", "completion_cost", "RateLimitError"] + (extra_spec or [])
    fake = mock.MagicMock(spec=spec)
    fake.RateLimitError = _FakeRateLimitError
    fake.completion_cost.return_value = 0.0
    return fake


def _make_cfg(provider: str, model: str):
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    return LlmConfig(
        provider=provider,
        model=model,
        endpoint=f"litellm://{provider}/{model}",
        api_key="sk-test",
    )


def _force_direct_path(monkeypatch) -> None:
    """Route ``_completion_via_router_or_direct`` at the bare litellm.completion."""
    from fluid_build.cli import forge_llm_router

    monkeypatch.setattr(forge_llm_router, "get_router", lambda model: None)


def _capture_sleeps(monkeypatch) -> List[float]:
    """Replace ``time.sleep`` with a no-op that records the requested waits."""
    slept: List[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


# ---------------------------------------------------------------------------
# _is_rate_limit_error — mocked-litellm safety (the CI-caught #78-style bug)
# ---------------------------------------------------------------------------


def test_is_rate_limit_error_true_for_real_class():
    from fluid_build.cli.forge_copilot_llm_litellm import _is_rate_limit_error

    fake = _fake_litellm()
    assert _is_rate_limit_error(fake, _FakeRateLimitError("429")) is True
    assert _is_rate_limit_error(fake, ValueError("nope")) is False


def test_is_rate_limit_error_guards_bare_magicmock_module():
    """A fully-mocked litellm module exposes ``RateLimitError`` as a Mock
    attribute (not a real class). ``isinstance(exc, <Mock>)`` would TypeError;
    the guard must return False instead of raising."""
    from fluid_build.cli.forge_copilot_llm_litellm import _is_rate_limit_error

    fake = mock.MagicMock()  # RateLimitError is an auto-Mock attr, not a type
    assert _is_rate_limit_error(fake, ValueError("x")) is False


# ---------------------------------------------------------------------------
# _resolve_rate_limit_wait — Retry-After sourcing + default fallback
# ---------------------------------------------------------------------------


def test_resolve_wait_prefers_exc_retry_after():
    from fluid_build.cli.forge_copilot_llm_litellm import _resolve_rate_limit_wait

    exc = _FakeRateLimitError("429")
    exc.retry_after = 12
    assert _resolve_rate_limit_wait(exc) == 12.0


def test_resolve_wait_falls_back_to_response_headers():
    from fluid_build.cli.forge_copilot_llm_litellm import _resolve_rate_limit_wait

    class _Resp:
        headers = {"retry-after": "9"}

    exc = _FakeRateLimitError("429")
    exc.response = _Resp()
    assert _resolve_rate_limit_wait(exc) == 9.0


def test_resolve_wait_default_when_no_hint():
    from fluid_build.cli.forge_copilot_llm_litellm import (
        _DEFAULT_RATE_LIMIT_WAIT_S,
        _resolve_rate_limit_wait,
    )

    assert _resolve_rate_limit_wait(_FakeRateLimitError("429")) == _DEFAULT_RATE_LIMIT_WAIT_S


# ---------------------------------------------------------------------------
# invoke_blocking — bare (direct) litellm.completion path
# ---------------------------------------------------------------------------


def test_invoke_blocking_bare_path_notice_and_recovery(monkeypatch, capsys):
    """429 on the first bare completion → notice on stderr honoring the
    server's Retry-After, then the second attempt succeeds and its text is
    returned. Machine stdout stays clean."""
    fake = _fake_litellm()
    err = _FakeRateLimitError("429 slow down")
    err.retry_after = 7
    fake.completion.side_effect = [err, _success_response("recovered")]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    _force_direct_path(monkeypatch)
    slept = _capture_sleeps(monkeypatch)

    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    provider = LiteLLMProvider("openai")
    text = provider.invoke_blocking(_make_cfg("openai", "gpt-4.1-mini"), "system", "user")

    assert text == "recovered"
    assert fake.completion.call_count == 2
    assert slept == [7.0]  # honored exc.retry_after, not the default
    captured = capsys.readouterr()
    assert "Rate limited. Waiting 7s before retrying..." in captured.err
    assert "Rate limited" not in captured.out  # notice never pollutes stdout


def test_invoke_blocking_default_wait_when_no_retry_after(monkeypatch, capsys):
    from fluid_build.cli.forge_copilot_llm_litellm import (
        _DEFAULT_RATE_LIMIT_WAIT_S,
        LiteLLMProvider,
    )

    fake = _fake_litellm()
    fake.completion.side_effect = [_FakeRateLimitError("429"), _success_response("ok")]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    _force_direct_path(monkeypatch)
    slept = _capture_sleeps(monkeypatch)

    provider = LiteLLMProvider("openai")
    text = provider.invoke_blocking(_make_cfg("openai", "gpt-4.1-mini"), "s", "u")

    assert text == "ok"
    assert slept == [_DEFAULT_RATE_LIMIT_WAIT_S]
    assert "Rate limited. Waiting 2s before retrying..." in capsys.readouterr().err


# ---------------------------------------------------------------------------
# invoke_blocking — Router path (retry surfaces via router.completion)
# ---------------------------------------------------------------------------


def test_invoke_blocking_router_path_notice_and_recovery(monkeypatch, capsys):
    """The wrapper is the single observable envelope — it makes the Router
    path's 429 visible too, without the bare litellm.completion ever running."""
    fake = _fake_litellm(extra_spec=["Router"])
    fake.completion.side_effect = AssertionError(
        "bare litellm.completion must not run on the Router path"
    )
    monkeypatch.setitem(sys.modules, "litellm", fake)

    router_instance = mock.MagicMock()
    router_instance.completion.side_effect = [
        _FakeRateLimitError("429"),
        _success_response("router-recovered"),
    ]
    from fluid_build.cli import forge_llm_router

    monkeypatch.setattr(forge_llm_router, "get_router", lambda model: router_instance)
    _capture_sleeps(monkeypatch)

    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    provider = LiteLLMProvider("anthropic")
    text = provider.invoke_blocking(_make_cfg("anthropic", "claude-sonnet-4-6"), "s", "u")

    assert text == "router-recovered"
    assert router_instance.completion.call_count == 2
    fake.completion.assert_not_called()
    assert "Rate limited. Waiting 2s before retrying..." in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Exhaustion + non-retryable semantics (count / success unchanged)
# ---------------------------------------------------------------------------


def test_invoke_blocking_exhausts_then_raises_translated(monkeypatch, capsys):
    """A persistent 429 retries up to the bounded envelope, prints a notice
    before each wait, then surfaces the standard translated error."""
    from fluid_build.cli.forge_copilot_llm_litellm import _RATE_LIMIT_MAX_ATTEMPTS, LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

    fake = _fake_litellm()
    err = _FakeRateLimitError("429 forever")
    err.retry_after = 1
    fake.completion.side_effect = err  # every call raises the same 429
    monkeypatch.setitem(sys.modules, "litellm", fake)
    _force_direct_path(monkeypatch)
    _capture_sleeps(monkeypatch)

    provider = LiteLLMProvider("openai")
    with pytest.raises(CopilotGenerationError):
        provider.invoke_blocking(_make_cfg("openai", "gpt-4.1-mini"), "s", "u")

    assert fake.completion.call_count == _RATE_LIMIT_MAX_ATTEMPTS
    notices = capsys.readouterr().err.count("Rate limited. Waiting")
    assert notices == _RATE_LIMIT_MAX_ATTEMPTS - 1  # one notice before each wait


def test_non_rate_limit_error_is_not_retried(monkeypatch, capsys):
    """A non-429 error fails fast: one attempt, no wait, no notice — the
    retry COUNT / success semantics for other errors are unchanged."""
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

    fake = _fake_litellm()
    fake.completion.side_effect = ValueError("boom, not a rate limit")
    monkeypatch.setitem(sys.modules, "litellm", fake)
    _force_direct_path(monkeypatch)
    slept = _capture_sleeps(monkeypatch)

    provider = LiteLLMProvider("openai")
    with pytest.raises(CopilotGenerationError):
        provider.invoke_blocking(_make_cfg("openai", "gpt-4.1-mini"), "s", "u")

    assert fake.completion.call_count == 1
    assert slept == []
    assert "Rate limited" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# invoke_streaming — same single envelope covers the streaming path
# ---------------------------------------------------------------------------


def test_invoke_streaming_notice_and_recovery(monkeypatch, capsys):
    def _stream() -> Iterator[Dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "hi"}}]}
        yield {
            "choices": [{"delta": {"content": " there"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    err = _FakeRateLimitError("429")
    err.retry_after = 3
    fake = _fake_litellm()
    fake.completion.side_effect = [err, _stream()]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    _force_direct_path(monkeypatch)
    slept = _capture_sleeps(monkeypatch)

    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    provider = LiteLLMProvider("openai")
    chunks = list(provider.invoke_streaming(_make_cfg("openai", "gpt-4.1-mini"), "s", "u"))

    assert "".join(chunks) == "hi there"
    assert fake.completion.call_count == 2
    assert slept == [3.0]
    assert "Rate limited. Waiting 3s before retrying..." in capsys.readouterr().err
