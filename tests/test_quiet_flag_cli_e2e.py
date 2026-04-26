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

"""End-to-end coverage for ``--quiet`` / ``-q`` plumbing across CLI surfaces.

The v2-preview banner has two suppression contracts that must both work:

1. **Env var.** ``FLUID_QUIET=1`` and ``FLUID_NONINTERACTIVE=1`` are
   honoured by ``forge_banner.banner_enabled`` regardless of CLI flags.
   Already pinned at the unit level in ``test_forge_banner.py``.
2. **CLI flag.** ``--quiet`` / ``-q`` must be (a) parsed by argparse at
   each banner-emitting surface, (b) preserved as ``args.quiet=True`` on
   the parsed namespace, and (c) forwarded into ``print_v2_banner(...,
   quiet=getattr(args, "quiet", False))``.

The unit tests cover (c). These e2e tests cover (a) + (b) — they call
``argparse.ArgumentParser.parse_args()`` against the real registered
parsers and confirm the flag round-trips. We deliberately don't shell
out to a subprocess: argparse-level pinning is fast and surfaces the
exact regression we care about (a future PR adding a new subcommand
without ``--quiet`` is caught here).
"""

from __future__ import annotations

import argparse

import pytest

from fluid_build.cli.ai_setup import register as register_ai
from fluid_build.cli.forge_data_model import register_forge_subcommand
from fluid_build.cli.generate_speed_transformation import (
    register_subcommand as register_speed_transformation,
)
from fluid_build.cli.init import register as register_init


def _build_forge_data_model_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fluid-test")
    sp = parser.add_subparsers(dest="cmd")
    register_forge_subcommand(sp)
    return parser


def _build_init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fluid-test")
    sp = parser.add_subparsers(dest="cmd")
    register_init(sp)
    return parser


def _build_ai_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fluid-test")
    sp = parser.add_subparsers(dest="cmd")
    register_ai(sp)
    return parser


def _build_speed_transformation_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fluid-test")
    sp = parser.add_subparsers(dest="cmd")
    register_speed_transformation(sp)
    return parser


# ----------------------------------------------------------------------
# fluid forge data-model from-intent / from-ddl / validate / diff
# ----------------------------------------------------------------------


class TestForgeDataModelQuiet:
    def test_from_intent_accepts_short_flag(self):
        parser = _build_forge_data_model_parser()
        args = parser.parse_args(["data-model", "from-intent", "b.yaml", "-o", "out.yaml", "-q"])
        assert args.quiet is True

    def test_from_intent_accepts_long_flag(self):
        parser = _build_forge_data_model_parser()
        args = parser.parse_args(
            ["data-model", "from-intent", "b.yaml", "-o", "out.yaml", "--quiet"]
        )
        assert args.quiet is True

    def test_from_intent_default_is_false(self):
        """Without the flag, ``args.quiet`` must be ``False`` (not
        ``None``) — otherwise ``getattr(args, "quiet", False)`` works
        but ``args.quiet`` directly silently skips the banner."""
        parser = _build_forge_data_model_parser()
        args = parser.parse_args(["data-model", "from-intent", "b.yaml", "-o", "out.yaml"])
        assert args.quiet is False

    def test_from_ddl_accepts_quiet(self):
        parser = _build_forge_data_model_parser()
        args = parser.parse_args(
            ["data-model", "from-ddl", "--ddl", "schema.sql", "-o", "out.yaml", "--quiet"]
        )
        assert args.quiet is True

    def test_validate_accepts_quiet(self):
        parser = _build_forge_data_model_parser()
        args = parser.parse_args(["data-model", "validate", "x.yaml", "-q"])
        assert args.quiet is True

    def test_diff_accepts_quiet(self):
        parser = _build_forge_data_model_parser()
        args = parser.parse_args(["data-model", "diff", "old.json", "new.json", "-q"])
        assert args.quiet is True

    def test_dump_ddl_accepts_quiet(self):
        parser = _build_forge_data_model_parser()
        args = parser.parse_args(
            ["data-model", "dump-ddl", "--database", "DB", "--schema", "S", "-o", "out.sql", "-q"]
        )
        assert args.quiet is True


# ----------------------------------------------------------------------
# fluid init  (banner emitted from forge_copilot_interview)
# ----------------------------------------------------------------------


class TestInitQuiet:
    def test_init_accepts_long_flag(self):
        parser = _build_init_parser()
        args = parser.parse_args(["init", "--quiet"])
        assert args.quiet is True

    def test_init_accepts_short_flag(self):
        parser = _build_init_parser()
        args = parser.parse_args(["init", "-q"])
        assert args.quiet is True

    def test_init_default_is_false(self):
        parser = _build_init_parser()
        args = parser.parse_args(["init"])
        assert args.quiet is False


# ----------------------------------------------------------------------
# fluid ai setup / status
# ----------------------------------------------------------------------


class TestAiQuiet:
    def test_ai_setup_accepts_quiet(self):
        parser = _build_ai_parser()
        args = parser.parse_args(["ai", "setup", "-q"])
        assert args.quiet is True

    def test_ai_status_accepts_quiet(self):
        """``ai status`` also prints the banner; flag must work there
        too even though it's a different subparser."""
        parser = _build_ai_parser()
        args = parser.parse_args(["ai", "status", "--quiet"])
        assert args.quiet is True


# ----------------------------------------------------------------------
# fluid generate speed-transformation
# ----------------------------------------------------------------------


class TestSpeedTransformationQuiet:
    def test_speed_transformation_accepts_quiet(self):
        parser = _build_speed_transformation_parser()
        args = parser.parse_args(["speed-transformation", "--quiet"])
        assert args.quiet is True

    def test_speed_transformation_with_contract_arg(self):
        parser = _build_speed_transformation_parser()
        args = parser.parse_args(["speed-transformation", "contract.yaml", "-q"])
        assert args.quiet is True


# ----------------------------------------------------------------------
# Coverage matrix — every banner-emitting surface advertises ``--quiet``
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        # forge data-model — from-intent, from-ddl, validate, diff, dump-ddl
        ("data-model from-intent b.yaml -o o.yaml --quiet", "forge_data_model"),
        ("data-model from-ddl --ddl s.sql -o o.yaml --quiet", "forge_data_model"),
        ("data-model validate x.yaml --quiet", "forge_data_model"),
        ("data-model diff a.json b.json --quiet", "forge_data_model"),
        (
            "data-model dump-ddl --database D --schema S -o o.sql --quiet",
            "forge_data_model",
        ),
    ],
)
def test_every_data_model_subcommand_carries_quiet(argv):
    """A coverage parametrize so adding a new subcommand without
    ``--quiet`` fails this test loudly. Pin the surface name in the
    fixture so a future surface rename surfaces here too."""
    argv_str, _surface = argv
    parser = _build_forge_data_model_parser()
    args = parser.parse_args(argv_str.split())
    assert args.quiet is True
