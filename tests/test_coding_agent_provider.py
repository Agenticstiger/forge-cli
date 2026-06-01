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

"""Part B — CodingAgentProvider unit tests (no network, no real CLIs).

Everything routes through the single subprocess seam
``forge_copilot_coding_agent._run_agent``, which we monkeypatch to return
canned ``(rc, stdout, stderr)`` — the same isolation pattern
``tests/test_litellm_backend.py`` uses to fake the ``litellm`` module. We
assert per-agent argv construction, output parsing, cost capture, and the
typed error paths.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from fluid_build.cli import forge_copilot_coding_agent as cap
from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    LlmConfig,
    get_llm_provider,
)

pytestmark = pytest.mark.unit


class _Recorder:
    """Stand-in for ``_run_agent``: records the call, returns a canned result."""

    def __init__(self, *, rc=0, stdout="", stderr="", capture_schema_file=False):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.capture_schema_file = capture_schema_file
        self.calls = []
        self.schema_file_contents = None

    def __call__(self, argv, *, stdin, timeout, cwd, env):
        self.calls.append(
            {"argv": list(argv), "stdin": stdin, "timeout": timeout, "cwd": cwd, "env": env}
        )
        if self.capture_schema_file and "--output-schema" in argv:
            path = argv[argv.index("--output-schema") + 1]
            try:
                with open(path, encoding="utf-8") as fh:
                    self.schema_file_contents = fh.read()
            except OSError:  # pragma: no cover
                pass
        return (self.rc, self.stdout, self.stderr)


def _cfg(provider_name, model=None):
    return LlmConfig(
        provider=provider_name,
        model=model or provider_name,
        endpoint=f"coding-agent://{provider_name}",
        api_key=None,
    )


def _install(monkeypatch, recorder, *, present=True):
    """Patch the binary resolver and the subprocess seam."""
    monkeypatch.setattr(
        cap.shutil, "which", (lambda b: f"/usr/local/bin/{b}") if present else (lambda b: None)
    )
    monkeypatch.setattr(cap, "_run_agent", recorder)


def _argv(rec):
    return rec.calls[0]["argv"]


# ---------------------------------------------------------------------------
# Per-agent argv + output parsing
# ---------------------------------------------------------------------------


def test_claude_code_argv_and_structured_output(monkeypatch):
    structured = {"recommended_template": "starter", "contract": {"id": "x"}}
    stdout = json.dumps(
        {
            "result": "ignored when structured_output present",
            "structured_output": structured,
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )
    rec = _Recorder(stdout=stdout)
    _install(monkeypatch, rec)

    provider = get_llm_provider("claude-code")
    result = provider.invoke_blocking(_cfg("claude-code"), "SYS-PROMPT", "USER-PROMPT")

    argv = _argv(rec)
    assert argv[0].endswith("claude")
    assert "-p" in argv and "--output-format" in argv and "json" in argv
    assert "--bare" not in argv  # bare mode would demand ANTHROPIC_API_KEY
    # schema is passed inline as a JSON string mentioning the envelope shape
    assert "--json-schema" in argv
    schema_str = argv[argv.index("--json-schema") + 1]
    assert "contract" in schema_str and "recommended_template" in schema_str
    # system via flag, user via stdin
    assert "--append-system-prompt" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS-PROMPT"
    assert rec.calls[0]["stdin"] == "USER-PROMPT"
    # no explicit model -> no --model flag
    assert "--model" not in argv
    # envelope = the structured_output, re-encoded as a JSON string
    assert json.loads(result) == structured
    assert cap.get_last_agent_cost_usd() == pytest.approx(0.0123)


def test_claude_code_result_fallback_without_structured_output(monkeypatch):
    inner = json.dumps({"contract": {"id": "y"}})
    rec = _Recorder(stdout=json.dumps({"result": inner, "total_cost_usd": 0.01}))
    _install(monkeypatch, rec)

    provider = get_llm_provider("claude-code")
    result = provider.invoke_blocking(_cfg("claude-code"), "SYS", "USER")
    assert result == inner


def test_claude_code_model_passthrough_when_explicit(monkeypatch):
    rec = _Recorder(stdout=json.dumps({"result": "{}", "total_cost_usd": 0.0}))
    _install(monkeypatch, rec)
    provider = get_llm_provider("claude-code")
    provider.invoke_blocking(_cfg("claude-code", model="claude-opus-4-8"), "SYS", "USER")
    argv = _argv(rec)
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-opus-4-8"


def test_codex_argv_schema_file_and_raw_output(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "ck-test")
    rec = _Recorder(stdout="ENVELOPE_TEXT_FROM_CODEX", capture_schema_file=True)
    _install(monkeypatch, rec)

    provider = get_llm_provider("codex")
    result = provider.invoke_blocking(_cfg("codex"), "SYS-PROMPT", "USER-PROMPT")

    argv = _argv(rec)
    assert argv[0].endswith("codex") and "exec" in argv
    assert "--output-schema" in argv
    assert "--sandbox" not in argv  # envelope mode stays read-only
    # codex has no system-prompt flag: system is prepended into the positional
    assert rec.calls[0]["stdin"] is None
    assert argv[-1].startswith("SYS-PROMPT") and "USER-PROMPT" in argv[-1]
    # the schema file actually contained the envelope schema
    assert rec.schema_file_contents and "contract" in rec.schema_file_contents
    assert result == "ENVELOPE_TEXT_FROM_CODEX"
    # non-claude agents report no cost
    assert cap.get_last_agent_cost_usd() is None


def test_cursor_argv_and_result_json(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cur-test")
    rec = _Recorder(stdout=json.dumps({"result": "ENVELOPE_FROM_CURSOR"}))
    _install(monkeypatch, rec)

    provider = get_llm_provider("cursor-agent")
    result = provider.invoke_blocking(_cfg("cursor"), "SYS-PROMPT", "USER-PROMPT")

    argv = _argv(rec)
    assert argv[0].endswith("cursor-agent")
    assert "-p" in argv and "--output-format" in argv and "json" in argv
    assert "--json-schema" not in argv and "--output-schema" not in argv  # no schema flag
    assert argv[-1].startswith("SYS-PROMPT") and "USER-PROMPT" in argv[-1]
    assert result == "ENVELOPE_FROM_CURSOR"


def test_kiro_argv_and_ansi_stripped_output(monkeypatch):
    # kiro-cli decorates stdout with ANSI colour/cursor codes (verified live);
    # the provider must strip them so the envelope JSON survives extraction.
    rec = _Recorder(stdout='\x1b[38;5;141m> \x1b[0m{"contract": {"id": "k"}}\x1b[0m')
    _install(monkeypatch, rec)

    provider = get_llm_provider("kiro")
    result = provider.invoke_blocking(_cfg("kiro"), "SYS", "USER")

    argv = _argv(rec)
    assert argv[0].endswith("kiro-cli")
    assert "chat" in argv and "--no-interactive" in argv
    assert "\x1b" not in result  # ANSI stripped
    assert '{"contract": {"id": "k"}}' in result


# ---------------------------------------------------------------------------
# Error paths — all typed CopilotGenerationError with actionable events
# ---------------------------------------------------------------------------


def test_not_installed(monkeypatch):
    rec = _Recorder()
    _install(monkeypatch, rec, present=False)
    provider = get_llm_provider("claude-code")
    with pytest.raises(CopilotGenerationError) as exc:
        provider.invoke_blocking(_cfg("claude-code"), "SYS", "USER")
    assert exc.value.event == "coding_agent_not_installed"


def test_keyed_agent_without_key_is_not_preblocked(monkeypatch):
    # No hard key pre-check: an agent the user logged into works headless
    # without its key env — verified live, a current `kiro-cli login` runs
    # `chat --no-interactive` with no KIRO_API_KEY. So a missing key must NOT
    # pre-block; the CLI is invoked and its own auth (login or key) decides.
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    rec = _Recorder(stdout="ENVELOPE_TEXT")
    _install(monkeypatch, rec)
    provider = get_llm_provider("codex")
    result = provider.invoke_blocking(_cfg("codex"), "SYS", "USER")
    assert rec.calls  # invoked despite no key env (auth is the CLI's job)
    assert result == "ENVELOPE_TEXT"


def test_not_authenticated_marker(monkeypatch):
    rec = _Recorder(rc=1, stderr="Error: not logged in. Please run /login")
    _install(monkeypatch, rec)
    provider = get_llm_provider("claude-code")
    with pytest.raises(CopilotGenerationError) as exc:
        provider.invoke_blocking(_cfg("claude-code"), "SYS", "USER")
    assert exc.value.event == "coding_agent_not_authenticated"


def test_generic_failure(monkeypatch):
    rec = _Recorder(rc=2, stderr="some unexpected explosion")
    _install(monkeypatch, rec)
    provider = get_llm_provider("claude-code")
    with pytest.raises(CopilotGenerationError) as exc:
        provider.invoke_blocking(_cfg("claude-code"), "SYS", "USER")
    assert exc.value.event == "coding_agent_failed"


def test_timeout(monkeypatch):
    def _boom(argv, *, stdin, timeout, cwd, env):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(cap.shutil, "which", lambda b: f"/usr/local/bin/{b}")
    monkeypatch.setattr(cap, "_run_agent", _boom)
    provider = get_llm_provider("claude-code")
    with pytest.raises(CopilotGenerationError) as exc:
        provider.invoke_blocking(_cfg("claude-code"), "SYS", "USER")
    assert exc.value.event == "coding_agent_timeout"


# ---------------------------------------------------------------------------
# Robustness: non-JSON stdout falls through to the runtime's repair loop
# ---------------------------------------------------------------------------


def test_claude_non_json_stdout_returned_verbatim(monkeypatch):
    # If the agent emits prose/fenced JSON instead of the json wrapper, return
    # it verbatim so the runtime's extract_json_object + repair loop handles it.
    rec = _Recorder(stdout='```json\n{"contract": {}}\n```')
    _install(monkeypatch, rec)
    provider = get_llm_provider("claude-code")
    result = provider.invoke_blocking(_cfg("claude-code"), "SYS", "USER")
    assert "contract" in result


def test_streaming_yields_blocking_result_once(monkeypatch):
    rec = _Recorder(stdout=json.dumps({"result": "STREAM_ENVELOPE", "total_cost_usd": 0.0}))
    _install(monkeypatch, rec)
    provider = get_llm_provider("claude-code")
    chunks = list(provider.invoke_streaming(_cfg("claude-code"), "SYS", "USER"))
    assert chunks == ["STREAM_ENVELOPE"]


# ---------------------------------------------------------------------------
# Integration: the top-level call_llm entry point + cost bridge
# ---------------------------------------------------------------------------


def test_call_llm_integration_returns_envelope_and_does_not_raise(monkeypatch):
    # Proves the runtime's entry point (call_llm -> invoke_blocking ->
    # _record_call_in_run_tracker cost bridge) flows the envelope through and
    # the Claude cost fallback wiring doesn't blow up the call.
    from fluid_build.cli.forge_copilot_llm_providers import call_llm

    structured = {"recommended_template": "starter", "contract": {"id": "z"}}
    rec = _Recorder(
        stdout=json.dumps(
            {
                "structured_output": structured,
                "total_cost_usd": 0.02,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
    )
    _install(monkeypatch, rec)
    provider = get_llm_provider("claude-code")
    out = call_llm(provider, _cfg("claude-code"), "SYS", "USER")
    assert json.loads(out) == structured
