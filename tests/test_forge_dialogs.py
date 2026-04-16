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

"""Tests for shared Forge dialog helpers."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fluid_build.cli.forge_dialogs import (
    ask_confirmation,
    ask_dialog_question,
    build_choice,
    normalize_prompt_choices,
    resolve_choice_input,
)
from fluid_build.cli.help_formatter import print_forge_help


class FakeConsole:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.index = 0
        self.printed = []

    def input(self, _prompt=""):
        if self.index >= len(self.answers):
            return ""
        answer = self.answers[self.index]
        self.index += 1
        return answer

    def print(self, *args, **_kwargs):
        self.printed.extend(args)


class TestForgeDialogs:
    def test_normalize_prompt_choices_preserves_aliases(self):
        choices = normalize_prompt_choices(
            [
                {
                    "label": "Fraud Detection",
                    "value": "fraud_detection",
                    "aliases": ["fraud", "fraud analytics"],
                }
            ]
        )

        assert choices[0]["aliases"] == ["fraud", "fraud analytics"]

    def test_resolve_choice_supports_choice_aliases(self):
        result = resolve_choice_input(
            field_name="product_type",
            raw_input="fraud analytics",
            choices=[
                build_choice(
                    "Fraud Detection", "fraud_detection", aliases=["fraud", "fraud analytics"]
                ),
                build_choice("Trading Platform", "trading_platform"),
            ],
            allow_skip=True,
        )

        assert result.status == "matched"
        assert result.value == "fraud_detection"

    def test_ask_dialog_question_handles_custom_use_case(self):
        console = FakeConsole(["customer graph workspace"])
        question = SimpleNamespace(
            field="use_case",
            prompt="What's your primary use case?",
            type="choice",
            choices=[
                build_choice("Analytics & BI", "analytics"),
                build_choice("Other / Not sure", "other"),
            ],
            required=True,
            allow_skip=True,
            default=None,
        )

        result = ask_dialog_question(console, question)

        assert result.context_patch["use_case"] == "other"
        assert result.context_patch["use_case_other"] == "customer graph workspace"

    def test_ask_confirmation_accepts_short_confirmation(self):
        console = FakeConsole(["sure"])
        confirmed = ask_confirmation(console, "Continue?", default=False)
        assert confirmed is True

    def test_ask_confirmation_uses_default_on_blank(self):
        console = FakeConsole([""])
        confirmed = ask_confirmation(console, "Continue?", default=False)
        assert confirmed is False

    def test_interactive_modules_do_not_call_rich_prompt_helpers(self):
        repo_root = Path(__file__).resolve().parents[1]
        for relative_path in (
            "fluid_build/cli/forge.py",
            "fluid_build/cli/forge_agents.py",
            "fluid_build/cli/forge_copilot_interview.py",
            "fluid_build/cli/forge_dialogs.py",
        ):
            text = (repo_root / relative_path).read_text(encoding="utf-8")
            # Allow Prompt.ask() only for password=True (secret input)
            non_password_prompt_calls = [
                line
                for line in text.splitlines()
                if "Prompt.ask(" in line and "password=True" not in line
            ]
            assert not non_password_prompt_calls, (
                f"{relative_path} uses Prompt.ask() outside password input: "
                f"{non_password_prompt_calls}"
            )
            assert "Confirm.ask(" not in text

    def test_print_forge_help_mentions_flexible_answers(self):
        console = MagicMock()
        with patch("fluid_build.cli.help_formatter.Console", return_value=console):
            print_forge_help()

        rendered_text = []
        for call in console.print.call_args_list:
            for arg in call.args:
                if hasattr(arg, "renderable"):
                    rendered_text.append(str(arg.renderable))
                else:
                    rendered_text.append(str(arg))
        combined = "\n".join(rendered_text)
        assert "fluid forge" in combined
        assert "AI-powered data product creation" in combined
