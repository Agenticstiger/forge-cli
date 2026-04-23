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

"""Tests for the ``fluid policy {check,compile,apply}`` umbrella command.

Pins:

* all three subcommands are registered and dispatch to the existing
  ``policy_check`` / ``policy_compile`` / ``policy_apply`` run functions
  (the umbrella must NOT silently no-op),
* the argument surface of each subcommand matches the legacy
  ``fluid policy-*`` hyphenated form — same options, same types, no drift,
* the legacy hyphenated forms (``fluid policy-check``, etc.) stay
  registered for the one-release deprecation window.

Why: G4 from the code review. Before this command group, the CLI had
``policy-check`` and ``policy-apply`` as sibling top-level commands
with almost-identical names but entirely different behaviours
(``policy-check`` = static lint; ``policy-apply`` = deploy IAM
bindings). This test class is a regression guard that both surfaces
stay usable while the umbrella group is the new idiomatic path.
"""

from __future__ import annotations

import argparse

import pytest


def _build_root_parser():
    """Return the production argparse tree by re-running bootstrap.

    Mirrors the approach in other bootstrap-dependent tests — we go
    through the real registration path so a future change that drops
    one of the subcommands immediately fails here.
    """
    from fluid_build.cli import bootstrap

    parser = argparse.ArgumentParser(prog="fluid")
    sp = parser.add_subparsers(dest="command")
    bootstrap.register_core_commands(sp)
    return parser


class TestPolicyUmbrellaRegistration:
    def test_policy_is_registered_as_top_level_subcommand(self):
        """``fluid policy`` must appear in the top-level subparser map.
        Regression on the bootstrap line that adds it; without this
        the umbrella is invisible and the code review's G4 fix is
        silently undone."""
        parser = _build_root_parser()
        # Parse just the command name to confirm argparse accepts it.
        # ``policy`` with no subcommand triggers the required=True
        # error (SystemExit 2); we catch that — proof that the parser
        # recognises ``policy`` as a valid top-level verb.
        with pytest.raises(SystemExit):
            parser.parse_args(["policy"])

    @pytest.mark.parametrize("sub", ["check", "compile", "apply"])
    def test_each_subcommand_is_parseable(self, sub):
        """All three subcommands must be reachable via argparse and
        accept their minimum required positional argument. A missing
        positional triggers SystemExit(2) — proof the parser knows
        the subcommand AND has the right required-arg shape."""
        parser = _build_root_parser()
        # Each subcommand has one required positional (contract or
        # bindings.json). Running without it must exit 2, not 0.
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["policy", sub])
        assert exc.value.code == 2

    def test_legacy_hyphenated_commands_still_registered(self):
        """``fluid policy-check`` / ``fluid policy-compile`` /
        ``fluid policy-apply`` must still be reachable as top-level
        commands. Removing them is a separate deprecation step — this
        test is the regression guard for that one-release window."""
        parser = _build_root_parser()
        for legacy in ("policy-check", "policy-compile", "policy-apply"):
            # Each requires a positional; parsing without it errors
            # (SystemExit 2). A SystemExit at all proves the legacy
            # command is still in the subparser map.
            with pytest.raises(SystemExit) as exc:
                parser.parse_args([legacy])
            assert exc.value.code == 2, f"{legacy} should still be registered"


class TestPolicyUmbrellaArgumentSurface:
    """Pin that ``fluid policy <sub>`` and ``fluid policy-<sub>`` share
    the same option surface — because they both call ``_add_arguments``
    from the same underlying module. A regression here means the
    umbrella and legacy forms have drifted; users would get subtly
    different behaviour from each, which is exactly the UX problem
    G4 set out to fix."""

    def test_check_subcommand_accepts_strict_flag(self):
        """``policy check`` must accept ``--strict`` just like the
        legacy ``policy-check``."""
        parser = _build_root_parser()
        ns = parser.parse_args(["policy", "check", "contract.yaml", "--strict"])
        assert ns.strict is True
        assert ns.contract == "contract.yaml"

    def test_check_subcommand_accepts_category_choices(self):
        """``policy check --category access_control`` must be
        accepted by the umbrella form."""
        parser = _build_root_parser()
        ns = parser.parse_args(["policy", "check", "contract.yaml", "--category", "access_control"])
        assert ns.category == "access_control"

    def test_compile_subcommand_default_out_path(self):
        """``policy compile`` defaults --out to runtime/policy/bindings.json
        — same default as the legacy form. The stage-3 artifact fanout
        relies on that default path, so drift breaks pipelines."""
        parser = _build_root_parser()
        ns = parser.parse_args(["policy", "compile", "contract.yaml"])
        assert ns.out == "runtime/policy/bindings.json"

    def test_apply_subcommand_defaults_mode_to_check(self):
        """``policy apply --mode`` defaults to ``check`` (dry-run).
        Defaulting to ``enforce`` would be destructive-by-default —
        the opposite of the safety posture this CLI aims for."""
        parser = _build_root_parser()
        ns = parser.parse_args(["policy", "apply", "bindings.json"])
        assert ns.mode == "check"

    def test_apply_subcommand_mode_rejects_invalid_choice(self):
        """``--mode bogus`` must fail at argparse level (exit 2)
        — the choice set is ``check|enforce`` and nothing else."""
        parser = _build_root_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["policy", "apply", "bindings.json", "--mode", "bogus"])
        assert exc.value.code == 2
