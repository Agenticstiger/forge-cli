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

"""Guard tests: ensure ``fluid -h`` stays in sync with registered commands.

If a command is added/removed or init flags change, these tests will fail
until the help_formatter is updated to match — preventing stale help output.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest


def _capture_main_help() -> str:
    """Render ``print_main_help`` to a plain string (Rich markup stripped)."""
    from fluid_build.cli import build_parser
    from fluid_build.cli.help_formatter import RICH_AVAILABLE

    if not RICH_AVAILABLE:
        pytest.skip("Rich is required for this test")

    from rich.console import Console

    from fluid_build.cli.help_formatter import print_main_help

    parser = build_parser()
    buf = StringIO()
    with patch(
        "fluid_build.cli.help_formatter.Console",
        return_value=Console(file=buf, width=120, no_color=True),
    ):
        print_main_help(parser)
    return buf.getvalue()


def _capture_first_run_help() -> str:
    """Render ``print_first_run_help`` to a plain string."""
    from fluid_build.cli import build_parser
    from fluid_build.cli.help_formatter import RICH_AVAILABLE

    if not RICH_AVAILABLE:
        pytest.skip("Rich is required for this test")

    from rich.console import Console

    from fluid_build.cli.help_formatter import print_first_run_help

    parser = build_parser()
    buf = StringIO()
    with patch(
        "fluid_build.cli.help_formatter.Console",
        return_value=Console(file=buf, width=120, no_color=True),
    ):
        print_first_run_help(parser)
    return buf.getvalue()


class TestMainHelpCoreCommands:
    """Every core-workflow command must appear in the help output."""

    @pytest.mark.parametrize("cmd", ["init", "validate", "plan", "apply"])
    def test_core_command_present(self, cmd: str):
        text = _capture_main_help()
        assert cmd in text, f"Core command '{cmd}' missing from fluid -h output"

    @pytest.mark.parametrize("cmd", ["forge", "generate"])
    def test_generation_command_present(self, cmd: str):
        text = _capture_main_help()
        assert cmd in text, f"Generation command '{cmd}' missing from fluid -h output"

    @pytest.mark.parametrize("cmd", ["doctor", "auth", "version"])
    def test_utility_command_present(self, cmd: str):
        text = _capture_main_help()
        assert cmd in text, f"Utility command '{cmd}' missing from fluid -h output"


class TestMainHelpNoStaleFlags:
    """Stale flags must NOT appear in the init description line."""

    def test_init_no_wizard_flag(self):
        text = _capture_main_help()
        # Find the init line and check it doesn't mention --wizard
        for line in text.splitlines():
            if line.strip().startswith("init"):
                assert "--wizard" not in line, "Stale --wizard flag found in init help line"
                break

    def test_init_no_scan_flag(self):
        text = _capture_main_help()
        for line in text.splitlines():
            if line.strip().startswith("init"):
                assert "--scan" not in line, "Stale --scan flag found in init help line"
                break

    def test_init_shows_template_flag(self):
        text = _capture_main_help()
        for line in text.splitlines():
            if line.strip().startswith("init"):
                assert "--template" in line, "Expected --template in init help line"
                break

    def test_forge_shows_modes(self):
        text = _capture_main_help()
        for line in text.splitlines():
            if line.strip().startswith("forge"):
                assert (
                    "copilot" in line.lower() or "AI" in line
                ), "Expected copilot/AI mention in forge help line"
                break


class TestFirstRunHelp:
    """First-run help must surface the current recommended fast paths."""

    def test_shows_demo_and_quickstart(self):
        """First-run help should recommend `fluid demo` and `--quickstart` as the fast paths."""
        text = _capture_first_run_help()
        assert "fluid demo" in text, "First-run help should surface `fluid demo`"
        assert "--quickstart" in text, "First-run help should surface `--quickstart`"

    def test_mentions_fluid_forge(self):
        """First-run help should tell workspace users about `fluid forge`."""
        text = _capture_first_run_help()
        assert "fluid forge" in text, (
            "First-run help should mention `fluid forge` so workspace users know "
            "how to add more products"
        )


class TestAdvertisedFlagsExist:
    """Every ``--flag`` named in ``fluid -h`` must be a real flag.

    Regression: the apply line advertised ``--build``, which does not exist.
    argparse prefix-matched it to ``--build-id``, so a user following the
    help got ``fluid apply: error: argument --build-id: expected one
    argument`` for a flag they never typed. The working spelling is
    ``--mode amend-and-build``.
    """

    @staticmethod
    def _subparser_actions(command: str):
        from fluid_build.cli import build_parser

        parser = build_parser()
        for action in parser._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict) and command in choices:
                return choices[command]._actions
        pytest.skip(f"subcommand {command!r} not registered")

    @staticmethod
    def _flags_on_line(text: str, command: str):
        import re

        for line in text.splitlines():
            if line.strip().startswith(command + " ") or line.strip() == command:
                return re.findall(r"--[a-z][a-z0-9-]*", line)
        return []

    @pytest.mark.parametrize("command", ["init", "plan", "apply", "forge"])
    def test_every_advertised_flag_is_registered(self, command: str):
        text = _capture_main_help()
        advertised = self._flags_on_line(text, command)
        if not advertised:
            pytest.skip(f"no flags advertised for {command!r}")
        registered = set()
        for action in self._subparser_actions(command):
            registered.update(action.option_strings)
        missing = [f for f in advertised if f not in registered]
        assert not missing, (
            f"`fluid -h` advertises {missing} for `fluid {command}`, but the "
            f"subparser registers {sorted(registered)}"
        )

    def test_apply_build_flag_is_not_advertised(self):
        """Pin the specific stale flag — it prefix-matches --build-id."""
        text = _capture_main_help()
        assert "--build " not in text and not text.rstrip().endswith("--build")
