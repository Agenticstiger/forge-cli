# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Claude Code driver: failure reporting on stdout, and isolation from user config.

No real CLI is spawned: ``_run_agent`` (the agent call) and ``_run_help`` (the
one-shot ``--help`` probe) are the two seams, both monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.llm import coding_agent as cap
from fluid_build.llm.providers import CopilotGenerationError, LlmConfig

pytestmark = pytest.mark.unit

_CLAUDE_AUTH_FAILURE = (
    '{"type":"result","subtype":"success","is_error":true,'
    '"result":"Failed to authenticate: OAuth session expired and could not be refreshed",'
    '"num_turns":1,"duration_ms":105}'
)

_ISOLATION_FLAGS = (
    "--tools",
    "--strict-mcp-config",
    "--setting-sources",
    "--no-session-persistence",
)

_FULL_HELP = """\
Usage: claude [options] [prompt]
  -p, --print                     Print response and exit
  --system-prompt <prompt>        System prompt to use for the session
  --append-system-prompt <prompt> Append a system prompt
  --allowedTools <tools...>       Tools to allow
  --tools <tools...>              Specify the list of available tools
  --strict-mcp-config             Only use MCP servers from --mcp-config
  --setting-sources <sources>     Comma-separated list of setting sources
  --no-session-persistence        Disable session persistence
"""


class _Recorder:
    def __init__(self, *, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.calls = []

    def __call__(self, argv, *, stdin, timeout, cwd, env):
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": env})
        return (self.rc, self.stdout, self.stderr)


def _cfg():
    return LlmConfig(
        provider="claude-code",
        model="claude-code",
        endpoint="coding-agent://claude-code",
        api_key=None,
    )


@pytest.fixture
def run(monkeypatch):
    """Install a recorder; returns ``(recorder, set_help, help_probe_calls)``."""
    cap._cli_help.cache_clear()
    probes = []
    state = {"help": ""}

    def _help(binary):
        probes.append(binary)
        return state["help"]

    monkeypatch.setattr(cap, "_run_help", _help)
    monkeypatch.setattr(cap.shutil, "which", lambda b: f"/usr/local/bin/{b}")

    def _install(recorder, help_text=""):
        state["help"] = help_text
        monkeypatch.setattr(cap, "_run_agent", recorder)
        return recorder

    yield _install, probes
    cap._cli_help.cache_clear()


def _invoke(system="SYS", user="USER"):
    return cap.get_coding_agent_provider("claude-code").invoke_blocking(_cfg(), system, user)


# ---------------------------------------------------------------------------
# Failures reported on stdout
# ---------------------------------------------------------------------------


def test_stdout_auth_failure_is_not_authenticated_with_hint(run):
    install, _ = run
    install(_Recorder(rc=1, stdout=_CLAUDE_AUTH_FAILURE, stderr=""))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_not_authenticated"
    assert cap._AGENT_SPECS["claude-code"].login_hint in exc.value.suggestions


def test_stdout_error_with_other_text_is_failed_and_carries_the_text(run):
    install, _ = run
    out = json.dumps({"type": "result", "is_error": True, "result": "Prompt is too long"})
    install(_Recorder(rc=1, stdout=out))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_failed"
    assert "Prompt is too long" in exc.value.message


def test_stdout_error_text_is_redacted_and_capped(run):
    install, _ = run
    out = json.dumps({"is_error": True, "result": "word " * 400})
    install(_Recorder(rc=1, stdout=out))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_failed"
    # "claude-code exited with code 1: " + at most 500 characters of reason
    assert len(exc.value.message) <= len("claude-code exited with code 1: ") + 500


def test_exit_zero_with_is_error_is_a_clean_failure(run):
    install, _ = run
    out = json.dumps({"is_error": True, "result": "Something broke"})
    install(_Recorder(rc=0, stdout=out))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_failed"
    assert "Something broke" in exc.value.message


def test_exit_zero_auth_is_error_is_not_authenticated(run):
    install, _ = run
    install(_Recorder(rc=0, stdout=_CLAUDE_AUTH_FAILURE))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_not_authenticated"


def test_stderr_is_combined_with_the_reported_reason(run):
    install, _ = run
    out = json.dumps({"is_error": True, "result": "boom"})
    install(_Recorder(rc=1, stdout=out, stderr="Please run /login"))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_not_authenticated"


def test_stderr_only_failure_still_works(run):
    install, _ = run
    install(_Recorder(rc=1, stderr="Error: invalid API key"))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_not_authenticated"
    install(_Recorder(rc=2, stderr="some unexpected explosion"))
    with pytest.raises(CopilotGenerationError) as exc:
        _invoke()
    assert exc.value.event == "coding_agent_failed"
    assert "some unexpected explosion" in exc.value.message


@pytest.mark.parametrize(
    "text",
    [
        "Failed to authenticate",
        "OAuth session expired",
        "Not logged in",
        "Invalid API key",
        "run /login to continue",
    ],
)
def test_claude_auth_markers_cover_the_known_wordings(text):
    markers = cap._AGENT_SPECS["claude-code"].auth_markers
    assert any(m in text.lower() for m in markers)


def test_is_error_false_is_not_a_failure(run):
    install, _ = run
    install(_Recorder(stdout=json.dumps({"is_error": False, "result": "{}"})))
    assert _invoke() == "{}"


# ---------------------------------------------------------------------------
# Isolation flags
# ---------------------------------------------------------------------------


def test_all_isolation_flags_present_when_help_lists_them(run):
    install, _ = run
    rec = install(_Recorder(stdout=json.dumps({"result": "{}"})), _FULL_HELP)
    _invoke("THE-SYSTEM-PROMPT", "THE-USER-PROMPT")
    argv = rec.calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    # the system prompt replaces the default one instead of being appended
    assert argv[argv.index("--system-prompt") + 1] == "THE-SYSTEM-PROMPT"
    assert "--append-system-prompt" not in argv
    # unchanged: stdin prompt, json output, schema
    assert rec.calls[0]["stdin"] == "THE-USER-PROMPT"
    assert "--json-schema" in argv and "--output-format" in argv


def test_setting_sources_is_one_token(run):
    install, _ = run
    rec = install(_Recorder(stdout=json.dumps({"result": "{}"})), _FULL_HELP)
    _invoke()
    argv = rec.calls[0]["argv"]
    assert "--setting-sources=" in argv
    assert "--setting-sources" not in argv


@pytest.mark.parametrize("missing", _ISOLATION_FLAGS)
def test_a_flag_help_lacks_is_omitted(run, missing):
    install, _ = run
    help_text = "\n".join(line for line in _FULL_HELP.splitlines() if missing not in line)
    rec = install(_Recorder(stdout=json.dumps({"result": "{}"})), help_text)
    _invoke()
    argv = rec.calls[0]["argv"]
    assert not any(tok.split("=")[0] == missing for tok in argv)
    for flag in _ISOLATION_FLAGS:
        if flag != missing:
            assert any(tok.split("=")[0] == flag for tok in argv)


def test_tools_is_not_matched_by_allowed_tools(run):
    install, _ = run
    rec = install(
        _Recorder(stdout=json.dumps({"result": "{}"})), "  --allowedTools <tools...>  Tools\n"
    )
    _invoke()
    assert "--tools" not in rec.calls[0]["argv"]


def test_old_claude_without_system_prompt_flag_keeps_append_fallback(run):
    install, _ = run
    rec = install(
        _Recorder(stdout=json.dumps({"result": "{}"})),
        "  --append-system-prompt <prompt>  Append\n",
    )
    _invoke("SYS")
    argv = rec.calls[0]["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert "--system-prompt" not in argv


def test_child_env_lacks_claudecode(run, monkeypatch):
    install, _ = run
    monkeypatch.setenv("CLAUDECODE", "1")
    rec = install(_Recorder(stdout=json.dumps({"result": "{}"})), _FULL_HELP)
    _invoke()
    assert "CLAUDECODE" not in rec.calls[0]["env"]


def test_other_agents_keep_their_env_and_argv(run, monkeypatch):
    install, probes = run
    monkeypatch.setenv("CLAUDECODE", "1")
    rec = install(_Recorder(stdout="TEXT"), _FULL_HELP)
    cap.get_coding_agent_provider("codex").invoke_blocking(
        LlmConfig(provider="codex", model="codex", endpoint="x", api_key=None), "SYS", "USER"
    )
    argv = rec.calls[0]["argv"]
    assert rec.calls[0]["env"].get("CLAUDECODE") == "1"
    assert not any(tok.startswith("--setting-sources") for tok in argv)
    assert "--strict-mcp-config" not in argv
    assert probes == []  # no help probe for an agent without isolation flags


def test_help_probe_runs_once_across_two_invocations(run):
    install, probes = run
    install(_Recorder(stdout=json.dumps({"result": "{}"})), _FULL_HELP)
    _invoke()
    _invoke()
    assert probes == ["/usr/local/bin/claude"]
