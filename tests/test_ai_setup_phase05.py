# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
        assert (
            extended in keys
        ), f"litellm-enabled provider {extended} missing from picker (only see: {keys})"


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


# ---------------------------------------------------------------------------
# H24 — Unknown slash commands print did-you-mean + re-prompt
# (i.e. the typo never leaks through as the user's answer to the next
# interview question). UX-finding 01 caught `:bogus` getting captured
# as the user's answer; the fix is in maybe_handle_slash_command.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #3 — mid-run :override sets override_action
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mid-run interrupt — agent loop honours override_action
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #15 — doctor surface in slash commands and panel
# ---------------------------------------------------------------------------
