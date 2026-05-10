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

"""Phase 0.5 — pin every AI-setup-unification fix.

Covers:
  #6  unified panel module (``_ai_setup_prompt``)
  #7  single Gemini entry with sub-flow
  #9  tier-first picker
  #11 3-attempt rescue dialog
  #12 shape-detect on bad keys
  #15 doctor promotion (rendered hint + slash command)
  #18 --browser OAuth scaffold
  #19 :ai-setup slash command
  #3  mid-run interrupt via :override
"""

from __future__ import annotations

from typing import Any, List
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _CapturingConsole:
    """Rich-aware capturing console — renders Panels / Tables to text."""

    def __init__(self):
        from io import StringIO

        from rich.console import Console as _RC

        self._buf = StringIO()
        self._inner = _RC(file=self._buf, force_terminal=False, color_system=None, width=120)
        self.lines: List[str] = []

    def print(self, *args, **kwargs):
        self._inner.print(*args, **kwargs)
        # Flush what was just rendered into our line list
        rendered = self._buf.getvalue()
        if rendered:
            self.lines.append(rendered)
            self._buf.seek(0)
            self._buf.truncate()


@pytest.fixture
def console():
    return _CapturingConsole()


# ---------------------------------------------------------------------------
# #6 — unified _ai_setup_prompt panel
# ---------------------------------------------------------------------------


def test_unified_panel_renders_for_each_reason(console):
    from fluid_build.cli._ai_setup_prompt import render

    for reason in ("missing", "invalid", "skipped", "rescue"):
        console.lines.clear()
        render(reason=reason, console=console, show_doctor_hint=False)
        assert console.lines, f"render() printed nothing for reason={reason}"


def test_unified_panel_promotes_doctor_when_hint_enabled(console):
    """Gap #15 — doctor is a first-class call-out, not dim aside."""
    from fluid_build.cli._ai_setup_prompt import render

    render(reason="missing", console=console, show_doctor_hint=True)
    full = "\n".join(console.lines)
    assert "doctor" in full.lower()


# ---------------------------------------------------------------------------
# #7 — single Gemini entry
# ---------------------------------------------------------------------------


def test_picker_has_one_gemini_entry(console):
    """The picker presented to the user must show exactly one Gemini row.

    World-class invariant: litellm is invisible plumbing — never a
    user-facing choice. The picker shows providers; whichever the user
    picks routes through litellm under the hood.
    """
    captured: List[List[str]] = []

    def _fake_choice(_console, _prompt, choices, default=1):
        captured.append([k for k, _label in choices])
        return "skip"

    from fluid_build.cli import ai_setup as _ai

    with mock.patch(
        "fluid_build.cli.forge_ui.ask_numbered_choice",
        _fake_choice,
    ):
        result = _ai._pick_provider(console)

    keys = captured[0]
    assert keys.count("gemini") == 1, f"Expected one Gemini entry, got: {keys}"
    assert "gemini_free" not in keys
    assert "litellm" not in keys, (
        "litellm must NOT appear as a user-facing choice — it's the "
        "invisible backend behind every provider"
    )
    assert result is None  # because we returned "skip"


def test_picker_surfaces_extended_providers_when_litellm_installed(console):
    """When litellm is available, the picker exposes Groq/Bedrock/Azure/
    Vertex/Mistral/Cohere — they 'just work' via litellm without any
    extra subclass code in our codebase."""
    captured: List[List[str]] = []

    def _fake_choice(_console, _prompt, choices, default=1):
        captured.append([k for k, _label in choices])
        return "skip"

    from fluid_build.cli import ai_setup as _ai

    with mock.patch("fluid_build.cli.forge_ui.ask_numbered_choice", _fake_choice):
        _ai._pick_provider(console)

    keys = captured[0]
    # litellm is installed in the dev venv → the extended set MUST appear
    for extended in ("groq", "bedrock", "azure", "vertex_ai"):
        assert extended in keys, (
            f"litellm-enabled provider {extended} missing from picker (only see: {keys})"
        )


# ---------------------------------------------------------------------------
# #9 — tier picker comes first
# ---------------------------------------------------------------------------


def test_tier_picker_offers_three_tiers(console):
    """Tier picker MUST present flagship / balanced / fast."""
    captured = []

    def _fake_choice(_console, _prompt, choices, default=1):
        captured.append([k for k, _ in choices])
        return choices[default - 1][0]

    with mock.patch("fluid_build.cli.forge_ui.ask_numbered_choice", _fake_choice):
        from fluid_build.cli.ai_setup import _pick_tier

        chosen = _pick_tier(console)

    assert {"flagship", "balanced", "fast"} == set(captured[0])
    assert chosen == "balanced"  # the world-class default


# ---------------------------------------------------------------------------
# #12 — shape-detect on bad keys
# ---------------------------------------------------------------------------


def test_shape_detect_classifies_known_prefixes():
    from fluid_build.cli.ai_setup import _classify_key_shape

    assert _classify_key_shape("sk-1234567890abcdef") == "openai"
    assert _classify_key_shape("sk-ant-1234abcd") == "anthropic"
    assert _classify_key_shape("AIzaSyA12345678901234567890123456789012") == "gemini"
    assert _classify_key_shape("gibberish") == "unknown"
    assert _classify_key_shape("") == "unknown"


# ---------------------------------------------------------------------------
# #11 — 3-attempt rescue dialog returns one of the rescue choices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rescue_choice", ["switch", "ollama", "skip"])
def test_rescue_dialog_returns_user_choice(console, rescue_choice):
    """Rescue dialog must offer switch / ollama / skip — never dead-end."""
    seen_options = []

    def _fake_choice(_console, _prompt, choices, default=1):
        seen_options.append([k for k, _ in choices])
        return rescue_choice

    with mock.patch("fluid_build.cli.forge_ui.ask_numbered_choice", _fake_choice):
        from fluid_build.cli.ai_setup import _rescue_after_attempts

        result = _rescue_after_attempts(console)

    assert {"switch", "ollama", "skip"}.issubset(set(seen_options[0]))
    assert result == (None if rescue_choice == "skip" else rescue_choice)


# ---------------------------------------------------------------------------
# #18 — browser OAuth scaffold
# ---------------------------------------------------------------------------


def test_browser_oauth_opens_browser_and_falls_back_to_key_entry(console):
    from fluid_build.cli.ai_setup_browser import open_oauth_flow

    opened: List[str] = []

    def _stub_open(url, *_a, **_k):
        opened.append(url)

    def _fake_choice(_c, _p, choices, default=1):
        # _pick_tier is called inside open_oauth_flow → after browser
        return "balanced"

    with (
        mock.patch("webbrowser.open", _stub_open),
        mock.patch("fluid_build.cli.forge_ui.ask_numbered_choice", _fake_choice),
        mock.patch(
            "fluid_build.cli.ai_setup._collect_and_validate_api_key",
            lambda *_a, **_k: None,
        ),
    ):
        result = open_oauth_flow(console, provider="openai")

    assert opened, "Browser OAuth must attempt to open a browser"
    # Use urlparse to validate the host (CodeQL
    # py/incomplete-url-substring-sanitization rejects raw `in` matches).
    from urllib.parse import urlparse

    assert urlparse(opened[0]).netloc == "platform.openai.com"
    assert result is None  # _collect stub returns None


def test_browser_oauth_raises_for_unknown_provider(console):
    from fluid_build.cli.ai_setup_browser import open_oauth_flow

    with pytest.raises(NotImplementedError):
        open_oauth_flow(console, provider="unknown")


# ---------------------------------------------------------------------------
# #19 — :ai-setup slash command
# ---------------------------------------------------------------------------


def test_slash_command_detected():
    from fluid_build.cli._interview_slash_commands import is_slash_command

    assert is_slash_command(":ai-setup")
    assert is_slash_command(":override")
    assert not is_slash_command("ai-setup")
    assert not is_slash_command("")


def test_slash_command_handler_dispatches(console):
    """:ai-setup invokes run_ai_setup_inline."""
    from fluid_build.cli._interview_slash_commands import maybe_handle_slash_command

    with mock.patch(
        "fluid_build.cli.ai_setup.run_ai_setup_inline",
        return_value=None,
    ) as mocked:
        handled = maybe_handle_slash_command(":ai-setup", console=console)

    assert handled
    mocked.assert_called_once()


def test_slash_command_unknown_reports(console):
    from fluid_build.cli._interview_slash_commands import maybe_handle_slash_command

    handled = maybe_handle_slash_command(":nope", console=console)
    assert handled  # still True — it was a slash command, just unrecognised
    assert any("Unknown command" in line for line in console.lines)


def test_slash_command_help_lists_commands(console):
    from fluid_build.cli._interview_slash_commands import maybe_handle_slash_command

    maybe_handle_slash_command(":help", console=console)
    full = "\n".join(console.lines)
    for cmd in (":ai-setup", ":override", ":show-work", ":doctor", ":quit"):
        assert cmd in full


def test_slash_command_quit_aborts(console):
    from fluid_build.cli._interview_slash_commands import maybe_handle_slash_command

    state = type("S", (), {"override_action": None})()
    with pytest.raises(KeyboardInterrupt):
        maybe_handle_slash_command(":quit", console=console, state=state)
    assert state.override_action == "quit"


# ---------------------------------------------------------------------------
# #3 — mid-run :override sets override_action
# ---------------------------------------------------------------------------


def test_override_slash_records_action_on_state(console):
    from fluid_build.cli._interview_slash_commands import maybe_handle_slash_command

    state = type("S", (), {"override_action": None})()

    with mock.patch(
        "fluid_build.cli.forge_ui.ask_numbered_choice",
        return_value="switch_engine",
    ):
        maybe_handle_slash_command(":override", console=console, state=state)
    assert state.override_action == "switch_engine"


# ---------------------------------------------------------------------------
# Mid-run interrupt — agent loop honours override_action
# ---------------------------------------------------------------------------


def test_agent_loop_aborts_on_override(tmp_path):
    """Loop checks override_action at each iteration; non-cancel exits."""
    from fluid_build.cli import forge_copilot_agent_loop as loop_mod
    from fluid_build.cli._preview_panel import PreviewPanel, new_run_id
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

    panel = PreviewPanel(run_id=new_run_id(), target_dir=tmp_path)
    panel.override_action = "switch_engine"

    class _Provider:
        def call(self, *_a, **_k):
            return {}

        def extract_tool_calls(self, _r):
            return []

        def extract_text_from_tool_response(self, _r):
            return ""

        def build_tool_result_messages(self, *_a, **_k):
            return []

        def extract_prompt_cache(self, _r):
            return {}

    with (
        mock.patch.object(loop_mod, "_call_llm_with_tools", lambda *_a, **_k: {}),
        mock.patch.object(loop_mod, "get_llm_provider", lambda *_a, **_k: _Provider()),
    ):
        with pytest.raises(CopilotGenerationError) as excinfo:
            loop_mod.run_copilot_agent_loop(
                context={"project_goal": "x"},
                llm_config=mock.MagicMock(provider="openai", model="gpt-4o"),
                preview_panel=panel,
                max_iterations=3,
            )

    # Error event is `copilot_agent_loop_overridden`; message text confirms
    # the override action so callers can react.
    assert excinfo.value.event == "copilot_agent_loop_overridden"
    assert "switch_engine" in str(excinfo.value.message).lower()


# ---------------------------------------------------------------------------
# #15 — doctor surface in slash commands and panel
# ---------------------------------------------------------------------------


def test_doctor_slash_command_runs_inline(console):
    from fluid_build.cli._interview_slash_commands import maybe_handle_slash_command

    with mock.patch("fluid_build.cli.doctor.run", return_value=0) as mocked:
        maybe_handle_slash_command(":doctor", console=console)
    mocked.assert_called_once()
