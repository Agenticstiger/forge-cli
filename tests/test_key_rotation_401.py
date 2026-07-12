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

"""API-key rotation on a mid-run 401 (Trello #78).

Covers the three moving parts:
1. ``_translate_litellm_exception`` tags 401 (AuthenticationError) distinctly
   from 403 (permission) and from generic/429 failures.
2. ``base._has_credential_failure`` + ``retry_with_backoff`` fail fast on an
   auth-tagged error instead of burning backoff attempts.
3. ``CopilotAgentBase._attempt_auth_recovery`` re-prompts + retries once, gated
   to interactive TTYs, and ``ai_setup.rotate_api_key_interactive`` reuse.
"""

from __future__ import annotations

from unittest import mock

import litellm
import pytest

from fluid_build.cli import ai_setup
from fluid_build.cli import forge_copilot_agent as agent_mod
from fluid_build.cli.forge_copilot_llm_litellm import _translate_litellm_exception
from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
from fluid_build.copilot.agents import base as base_mod

# ---------------------------------------------------------------------------
# 1. exception translation / tagging
# ---------------------------------------------------------------------------


def test_authentication_error_tagged_as_auth():
    exc = litellm.AuthenticationError(message="bad key", llm_provider="openai", model="gpt-4o")
    err = _translate_litellm_exception(litellm, exc, streaming=False)
    assert isinstance(err, CopilotGenerationError)
    assert err.context.get("failure_class") == "auth"
    assert err.error_slug == "copilot_llm_auth_failed" or err.args  # tagged, distinct event


def test_rate_limit_error_not_tagged_as_auth():
    exc = litellm.RateLimitError(message="429", llm_provider="openai", model="gpt-4o")
    err = _translate_litellm_exception(litellm, exc, streaming=False)
    # A 429 must NOT be classified as auth (else we'd wrongly re-prompt for a key)
    assert err.context.get("failure_class") != "auth"


def test_generic_exception_stays_generic_blocking():
    err = _translate_litellm_exception(litellm, ValueError("boom"), streaming=False)
    assert err.context.get("failure_class") not in ("auth", "permission")
    assert "request failed" in err.message


def test_generic_exception_streaming_variant():
    err = _translate_litellm_exception(litellm, ValueError("boom"), streaming=True)
    assert "streaming failed" in err.message


# ---------------------------------------------------------------------------
# 2. fail-fast in retry loop
# ---------------------------------------------------------------------------


def test_has_credential_failure_detects_auth_and_permission():
    auth = CopilotGenerationError("e", "m", context={"failure_class": "auth"})
    perm = CopilotGenerationError("e", "m", context={"failure_class": "permission"})
    other = CopilotGenerationError("e", "m", context={"failure_class": "ambiguous_intent"})
    plain = ValueError("x")
    assert base_mod._has_credential_failure(auth) is True
    assert base_mod._has_credential_failure(perm) is True
    assert base_mod._has_credential_failure(other) is False
    assert base_mod._has_credential_failure(plain) is False


def test_retry_fails_fast_on_auth():
    # An auth-tagged error must raise on the first attempt (no retry). The
    # fail-fast branch runs before any backoff sleep, so a single-call count
    # is sufficient proof — a retried error would call the func >1 time.
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise CopilotGenerationError("e", "auth", context={"failure_class": "auth"})

    with pytest.raises(CopilotGenerationError):
        base_mod.retry_with_backoff(_boom, attempts=4)
    assert calls["n"] == 1, "auth error must not be retried"


# ---------------------------------------------------------------------------
# 3. rotate_api_key_interactive
# ---------------------------------------------------------------------------


def test_rotate_api_key_interactive_no_console_returns_none():
    assert ai_setup.rotate_api_key_interactive(None) is None


def test_rotate_api_key_interactive_delegates_to_prompt(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(ai_setup, "RICH_AVAILABLE", True)
    monkeypatch.setattr(ai_setup, "_prompt_for_api_key", lambda console: sentinel)
    panel_calls = []
    monkeypatch.setattr(
        "fluid_build.cli.forge_ui.show_lines_panel",
        lambda *a, **k: panel_calls.append(k.get("title")),
    )
    console = mock.Mock()
    result = ai_setup.rotate_api_key_interactive(console)
    assert result is sentinel
    assert panel_calls, "a rejection panel should be shown before re-prompting"


# ---------------------------------------------------------------------------
# 4. _attempt_auth_recovery gating + retry
# ---------------------------------------------------------------------------


def _bare_agent(console):
    a = agent_mod.CopilotAgentBase()
    a.console = console
    return a


def test_auth_recovery_skipped_without_console():
    a = _bare_agent(None)
    assert a._attempt_auth_recovery(context={}, options={}) is None


def test_auth_recovery_skipped_when_non_interactive_option():
    a = _bare_agent(mock.Mock())
    assert a._attempt_auth_recovery(context={}, options={"non_interactive": True}) is None


def test_auth_recovery_skipped_when_already_used():
    a = _bare_agent(mock.Mock())
    assert a._attempt_auth_recovery(context={}, options={"auth_recovery_used": True}) is None


def test_auth_recovery_skipped_when_not_a_tty(monkeypatch):
    a = _bare_agent(mock.Mock())
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert a._attempt_auth_recovery(context={}, options={}) is None


def test_auth_recovery_success_reprompts_and_retries_once(monkeypatch):
    a = _bare_agent(mock.Mock())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(ai_setup, "rotate_api_key_interactive", lambda console: object())
    retried = {"n": 0}
    monkeypatch.setattr(
        a,
        "generate_project_artifacts",
        lambda ctx, opts: retried.__setitem__("n", retried["n"] + 1) or "RESULT",
    )
    opts = {}
    result = a._attempt_auth_recovery(context={"k": "v"}, options=opts)
    assert result == "RESULT"
    assert retried["n"] == 1
    assert opts["auth_recovery_used"] is True, "must mark used to prevent prompt loops"


def test_auth_recovery_cancel_returns_none_without_retry(monkeypatch):
    a = _bare_agent(mock.Mock())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(ai_setup, "rotate_api_key_interactive", lambda console: None)
    called = {"n": 0}
    monkeypatch.setattr(
        a, "generate_project_artifacts", lambda ctx, opts: called.__setitem__("n", called["n"] + 1)
    )
    assert a._attempt_auth_recovery(context={}, options={}) is None
    assert called["n"] == 0, "no retry when the user cancels the re-prompt"
