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

"""Tests for fluid_build.cli.help_formatter – focusing on print_command_help."""

import argparse
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser_with_sub(
    command: str,
    description: str = "",
    epilog: str = "",
    add_option: bool = False,
    add_positional: bool = False,
):
    """Return an ArgumentParser that has *command* registered as a sub-command."""
    parser = argparse.ArgumentParser(prog="fluid")
    subs = parser.add_subparsers(dest="cmd")
    sub = subs.add_parser(command, description=description, epilog=epilog)
    if add_option:
        sub.add_argument("--env", default="dev", help="Environment name")
    if add_positional:
        sub.add_argument("contract", metavar="CONTRACT", help="Contract path")
    return parser


# ---------------------------------------------------------------------------
# Tests for print_command_help
# ---------------------------------------------------------------------------


class TestPrintCommandHelpRichUnavailable:
    """When RICH is not importable the function delegates to parser.print_help()."""

    def test_rich_unavailable_calls_print_help(self):
        parser = _make_parser_with_sub("plan")
        with patch(
            "fluid_build.cli.help_formatter.RICH_AVAILABLE",
            False,
        ):
            parser.print_help = MagicMock()
            from fluid_build.cli.help_formatter import print_command_help

            print_command_help(parser, "plan")
            parser.print_help.assert_called_once()


class TestPrintCommandHelpUnknownCommand:
    """An unknown command falls back to parser.print_help()."""

    def test_unknown_command_falls_back(self):
        parser = _make_parser_with_sub("plan")
        parser.print_help = MagicMock()

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            print_command_help(parser, "nonexistent-command")
            parser.print_help.assert_called_once()


class TestPrintCommandHelpForgeRouting:
    """The 'forge' command is routed to print_forge_help()."""

    def test_forge_delegates_to_forge_help(self):
        parser = _make_parser_with_sub("forge")

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            with patch(
                "fluid_build.cli.help_formatter.print_forge_help",
                return_value=True,
            ) as mock_forge:
                from fluid_build.cli.help_formatter import print_command_help

                print_command_help(parser, "forge")
                mock_forge.assert_called_once()


class TestPrintCommandHelpRendersContent:
    """
    With Rich available and a known command the function should produce
    Rich output without raising exceptions.
    """

    def test_known_command_renders_without_error(self):
        parser = _make_parser_with_sub(
            "plan",
            description="Generate execution plan",
            epilog="  fluid plan contract.fluid.yaml\n  fluid plan contract.fluid.yaml --verbose",
        )

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            # Should not raise
            print_command_help(parser, "plan")

    def test_command_with_options_renders(self):
        parser = _make_parser_with_sub("plan", add_option=True)

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            print_command_help(parser, "plan")

    def test_command_with_positional_arg_renders(self):
        parser = _make_parser_with_sub("validate", add_positional=True)

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            print_command_help(parser, "validate")

    def test_command_from_enrichment_dict_renders(self):
        """Commands present in _COMMAND_ENRICHMENT should fill description/epilog."""
        parser = _make_parser_with_sub("context")  # no description in parser

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            # Should not raise and should use enrichment data
            print_command_help(parser, "context")

    def test_epilog_with_backslash_continuation(self):
        """Lines ending with \\ trigger the continuation branch."""
        epilog = (
            "  fluid plan contract.fluid.yaml \\\n"
            "    --env prod\n"
            "  # This is a comment\n"
            "  fluid plan contract.fluid.yaml"
        )
        parser = _make_parser_with_sub("plan", epilog=epilog)

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            print_command_help(parser, "plan")

    def test_long_description_truncated(self):
        """Descriptions > 120 chars are truncated at the first sentence."""
        long_desc = "A" * 50 + ". " + "B" * 100
        parser = _make_parser_with_sub("plan", description=long_desc)

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            # Should not raise
            print_command_help(parser, "plan")

    def test_option_with_choices_appended(self):
        """Options with .choices should include choices in help text."""
        parser = argparse.ArgumentParser(prog="fluid")
        subs = parser.add_subparsers(dest="cmd")
        sub = subs.add_parser("plan")
        sub.add_argument(
            "--format",
            choices=["json", "text", "rich"],
            help="Output format",
        )

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            print_command_help(parser, "plan")

    def test_option_with_non_default_default_appended(self):
        """Options with non-None/False defaults should include default in help text."""
        parser = argparse.ArgumentParser(prog="fluid")
        subs = parser.add_subparsers(dest="cmd")
        sub = subs.add_parser("plan")
        sub.add_argument("--retries", type=int, default=3, help="Number of retries")

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_command_help

            print_command_help(parser, "plan")


# ---------------------------------------------------------------------------
# Tests for print_main_help
# ---------------------------------------------------------------------------


class TestPrintMainHelp:
    def test_rich_unavailable_calls_print_help(self):
        parser = argparse.ArgumentParser(prog="fluid")
        parser.print_help = MagicMock()

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", False):
            from fluid_build.cli.help_formatter import print_main_help

            print_main_help(parser)
            parser.print_help.assert_called_once()

    def test_rich_available_renders_without_error(self):
        parser = argparse.ArgumentParser(prog="fluid")

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_main_help

            print_main_help(parser)


# ---------------------------------------------------------------------------
# Tests for print_forge_help
# ---------------------------------------------------------------------------


class TestPrintForgeHelp:
    def test_rich_unavailable_returns_false(self):
        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", False):
            from fluid_build.cli.help_formatter import print_forge_help

            result = print_forge_help()
            assert result is False

    def test_rich_available_renders_without_error(self):
        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_forge_help

            # Should not raise
            print_forge_help()


# ---------------------------------------------------------------------------
# Tests for print_first_run_help
# ---------------------------------------------------------------------------


class TestPrintFirstRunHelp:
    def test_rich_unavailable_calls_print_help(self):
        parser = argparse.ArgumentParser(prog="fluid")
        parser.print_help = MagicMock()

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", False):
            from fluid_build.cli.help_formatter import print_first_run_help

            print_first_run_help(parser)
            parser.print_help.assert_called_once()

    def test_rich_available_renders_without_error(self):
        parser = argparse.ArgumentParser(prog="fluid")

        with patch("fluid_build.cli.help_formatter.RICH_AVAILABLE", True):
            from fluid_build.cli.help_formatter import print_first_run_help

            print_first_run_help(parser)
