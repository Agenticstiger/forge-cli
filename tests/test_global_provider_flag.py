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

"""The global ``--provider`` flag must agree with the provider registry.

`fluid providers` (help text: "List all discoverable infrastructure providers")
returned aws / datamesh_manager / gcp / local / redshift / snowflake, while the
global flag carried a hardcoded ``choices=["local","gcp","snowflake","aws",
"azure"]``. The two disagreed in BOTH directions:

* ``fluid --provider redshift`` -> ``invalid choice: 'redshift'`` for a
  provider the registry ships, and no pip-installed provider plugin could ever
  be passed at all.
* ``fluid --provider azure`` was accepted for a provider that is not registered.

Separately, ``fluid --provider local apply <snowflake contract>`` was accepted
and then silently discarded: the ``apply`` subparser re-registers ``--provider``
with ``default=None``, and argparse writes a subparser default onto the SAME
namespace the global parser already populated. The run went on to print
"OpenTofu engine — provider: snowflake" and apply to Snowflake.
"""

from __future__ import annotations

import pytest

from fluid_build.cli import _known_provider_names, build_parser


@pytest.fixture(scope="module")
def parser():
    return build_parser()


class TestChoicesComeFromTheRegistry:
    def test_the_flag_declares_no_stale_enum(self, parser):
        action = next(a for a in parser._actions if "--provider" in a.option_strings)
        assert action.choices is None, (
            "a hardcoded choices list cannot track a pluggable registry — "
            "validation belongs in _validate_global_args"
        )

    def test_every_registered_provider_is_accepted(self, parser):
        for name in _known_provider_names():
            args = parser.parse_args(["--provider", name, "providers"])
            assert args.provider == name

    def test_a_registry_provider_the_old_enum_rejected_now_parses(self, parser):
        # 'redshift' and 'datamesh_manager' ship in the registry and were
        # rejected by the enum.
        known = _known_provider_names()
        for name in ("redshift", "datamesh_manager"):
            if name in known:
                assert parser.parse_args(["--provider", name, "providers"]).provider == name

    def test_an_unregistered_provider_is_reported_as_unknown(self):
        from fluid_build.cli import _validate_global_args

        class _Log:
            def __init__(self):
                self.errors = []

            def log_safe(self, level, message, **_kw):
                if level == "error":
                    self.errors.append(message)

        args = build_parser().parse_args(["--provider", "azure", "providers"])
        log = _Log()
        _validate_global_args(args, log, known_providers=["aws", "gcp", "local", "snowflake"])
        assert any("Unknown provider 'azure'" in e for e in log.errors)

    def test_validation_fails_open_when_the_registry_is_unavailable(self):
        from fluid_build.cli import _validate_global_args

        class _Log:
            def __init__(self):
                self.errors = []

            def log_safe(self, level, message, **_kw):
                if level == "error":
                    self.errors.append(message)

        args = build_parser().parse_args(["--provider", "whatever", "providers"])
        log = _Log()
        _validate_global_args(args, log, known_providers=[])
        assert not any("Unknown provider" in e for e in log.errors)


class TestTheGlobalValueReachesTheSubcommand:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--provider", "local", "apply", "c.fluid.yaml", "--yes"],
            ["--provider", "local", "plan", "c.fluid.yaml"],
        ],
    )
    def test_the_subparser_default_no_longer_clobbers_it(self, parser, argv):
        assert parser.parse_args(argv).provider == "local"

    @pytest.mark.parametrize(
        "argv",
        [
            ["apply", "c.fluid.yaml", "--provider", "aws", "--yes"],
            ["plan", "c.fluid.yaml", "--provider", "aws"],
        ],
    )
    def test_the_subcommand_flag_still_wins_when_given(self, parser, argv):
        assert parser.parse_args(argv).provider == "aws"

    @pytest.mark.parametrize(
        "argv",
        [["apply", "c.fluid.yaml", "--yes"], ["plan", "c.fluid.yaml"]],
    )
    def test_absent_everywhere_still_means_detect_from_the_contract(self, parser, argv):
        assert getattr(parser.parse_args(argv), "provider", None) is None

    def test_provider_config_is_not_swallowed_by_abbreviation(self, parser):
        """The reason apply re-registers --provider at all (cli/apply.py)."""
        args = parser.parse_args(["apply", "c.fluid.yaml", "--provider", "aws", "--yes"])
        assert getattr(args, "provider_config", None) is None
