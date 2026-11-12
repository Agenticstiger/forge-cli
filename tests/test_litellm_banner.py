# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""litellm prints nothing of its own to stdout, wherever forge loads it.

On a failed call litellm prints a "Give Feedback / Get Help" banner to stdout
unless ``litellm.suppress_debug_info`` is set, which put text ahead of
``fluid ai test --json``'s report. These tests use the installed litellm, not a
fake: the point is that the switch forge sets is the one this litellm reads.
``mock_response=Exception(...)`` makes litellm fail inside its own error mapping,
where the banner is printed, with no network call.
"""

from __future__ import annotations

import argparse
import json
from unittest import mock

import pytest

_MESSAGES = [{"role": "user", "content": "Say ok"}]
_DUMMY_KEY = "not-a-real-key"  # pragma: allowlist secret


@pytest.fixture
def litellm(monkeypatch):
    """The installed litellm, its banner switch back at litellm's default (on)."""
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no price-map fetch
    module = pytest.importorskip("litellm")
    monkeypatch.setattr(module, "suppress_debug_info", False)
    return module


@pytest.fixture
def failing_completion(litellm, monkeypatch, capsys):
    """Make every ``litellm.completion`` fail inside litellm, as a provider error does.

    It first checks that this litellm prints its banner with the switch off: one
    that never prints has nothing to switch off, and the test skips.
    """
    real = litellm.completion

    def completion(*args, **kwargs):
        return real(*args, **{**kwargs, "mock_response": Exception("provider down")})

    monkeypatch.setattr(litellm, "completion", completion)
    with pytest.raises(Exception):
        litellm.completion(model="openai/gpt-4o-mini", messages=_MESSAGES, api_key=_DUMMY_KEY)
    if "Give Feedback / Get Help" not in capsys.readouterr().out:
        pytest.skip("this litellm prints no banner on a failed call: nothing to switch off")


def test_get_litellm_switches_the_banner_off(litellm, failing_completion, capsys):
    from fluid_build.llm.litellm_backend import _get_litellm

    loaded = _get_litellm()
    with pytest.raises(Exception):
        loaded.completion(model="openai/gpt-4o-mini", messages=_MESSAGES, api_key=_DUMMY_KEY)

    assert capsys.readouterr().out == ""


def test_ai_test_json_stdout_is_the_report_alone_when_the_call_fails(
    litellm, failing_completion, monkeypatch, capsys, tmp_path
):
    """``fluid ai test --json`` on a failed call: one JSON document, nothing before it."""
    import fluid_build.cli.ai_setup as ai_setup

    monkeypatch.chdir(tmp_path)
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "FLUID_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", _DUMMY_KEY)
    args = argparse.Namespace(
        llm_provider="openai",
        llm_model=None,
        llm_endpoint=None,
        llm_timeout_seconds=None,
        json=True,
    )
    with (
        mock.patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
        mock.patch(
            "fluid_build.cli.ai_setup._check_ai_test_model_availability",
            return_value=("unavailable", None),
        ),
    ):
        code, report = ai_setup.run_ai_test(None, args)

    assert code != 0
    assert report["error"]["code"] == "copilot_litellm_request_failed"
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(report))


def test_ai_setup_key_check_prints_no_banner(
    litellm, failing_completion, monkeypatch, capsys, tmp_path
):
    """``fluid ai setup`` loads litellm itself to check a key."""
    import fluid_build.cli.ai_setup as ai_setup
    from fluid_build.cli.forge_copilot_llm_litellm import get_litellm_provider

    monkeypatch.chdir(tmp_path)  # no project dotenv to hold the check back
    error = ai_setup._validate_api_key(get_litellm_provider("openai"), _DUMMY_KEY)

    assert error  # the check failed, and said so
    assert capsys.readouterr().out == ""


def test_agent_loop_tool_call_prints_no_banner(litellm, failing_completion, capsys):
    """The agent loop's tool-use call imports litellm directly."""
    from fluid_build.cli.forge_copilot_agent_loop import _call_litellm_with_tools

    payload = {"model": "openai/gpt-4o-mini", "messages": _MESSAGES, "api_key": _DUMMY_KEY}
    with pytest.raises(Exception):
        _call_litellm_with_tools(payload)

    assert capsys.readouterr().out == ""
