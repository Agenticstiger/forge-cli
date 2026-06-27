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

"""Pins for the context-aware first-run menu default (init quickstart card).

When no LLM provider is configured, pressing Enter on the creation menu must
land on **Quickstart** (a working customer-360 project, no API key) rather than
the AI path that would dead-end on a missing key. When AI *is* configured, the
AI path stays the default.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fluid_build.cli import _init_interactive_helpers as helpers


class _PromptEchoesDefault:
    """Stand-in for rich.Prompt: 'pressing Enter' returns the offered default."""

    @staticmethod
    def ask(*_args, **kwargs):
        return kwargs.get("default")


def _menu_with(ai_available: bool) -> str:
    with (
        patch.object(helpers, "_rich_available", return_value=True),
        patch.object(helpers, "_get_console", return_value=MagicMock()),
        patch.object(helpers, "_get_panel", return_value=MagicMock()),
        patch.object(helpers, "_get_prompt", return_value=_PromptEchoesDefault),
    ):
        return helpers._ask_creation_mode(ai_available=ai_available)


def test_enter_defaults_to_quickstart_when_no_ai_configured():
    # No key → the Enter-key default must be the always-works Quickstart path.
    assert _menu_with(ai_available=False) == "quickstart"


def test_enter_defaults_to_ai_when_configured():
    # Key present → the richer AI-design path stays the default.
    assert _menu_with(ai_available=True) == "ai"


def test_non_rich_terminal_falls_back_to_quickstart():
    with patch.object(helpers, "_rich_available", return_value=False):
        assert helpers._ask_creation_mode() == "quickstart"


def test_detect_ai_available_true_when_probe_configured():
    with patch(
        "fluid_build.cli._welcome_scan._probe_ai_credentials",
        return_value={"ai_configured": True, "ai_provider_hint": "openai"},
    ):
        assert helpers._detect_ai_available() is True


def test_detect_ai_available_false_when_probe_unconfigured():
    with patch(
        "fluid_build.cli._welcome_scan._probe_ai_credentials",
        return_value={"ai_configured": False},
    ):
        assert helpers._detect_ai_available() is False


def test_detect_ai_available_false_on_probe_error():
    # Detection must never block the menu — a raising probe resolves to False
    # (the safe direction: default to the no-key Quickstart path).
    with patch(
        "fluid_build.cli._welcome_scan._probe_ai_credentials",
        side_effect=RuntimeError("boom"),
    ):
        assert helpers._detect_ai_available() is False
