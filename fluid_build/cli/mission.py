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

# ruff: noqa: T201 — this CLI command owns user-facing print() output by design
# (same convention as ``cli/stats.py``).
"""``fluid mission`` — mission specs: trust pinning + zero-LLM scorecards.

Deep-agents PR 1 (RFC-deep-agents.md). Three subcommands:

- ``fluid mission check <spec> [contract]`` — load the spec
  (trust-gated), run its deterministic success criteria against the
  re-read on-disk contract, render a scorecard, exit 0/1. Zero LLM
  calls — usable as a standalone CI gate.
- ``fluid mission trust <spec>`` — one-time direnv-style approval:
  pins the spec's content hash so it may configure autonomous behavior.
- ``fluid mission list`` — built-in + user mission specs with their
  trust status.

The autonomous ``fluid mission run`` arrives in PR 2. Everything heavy
(yaml, the checks registry, ``jsonschema`` via ``schema_manager``) is
imported inside the handlers — ``register`` needs argparse only, so the
``fluid --help`` cold path stays light (tests/perf/test_startup_budget.py).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

LOG = logging.getLogger("fluid.cli.mission")

COMMAND = "mission"

#: Exit codes: 0 = scorecard green, 1 = scorecard red,
#: 2 = harness error (bad spec, untrusted spec, unreadable contract).
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

_DEFAULT_CONTRACT = "contract.fluid.yaml"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Wire ``fluid mission`` into the CLI. Argparse only — stays light."""
    mission = subparsers.add_parser(
        COMMAND,
        help="Mission specs: trust pinning + zero-LLM success-criteria scorecards",
        description=(
            "Missions are declarative YAML goals with deterministically "
            "verifiable success criteria (RFC-deep-agents.md). "
            "'check' runs the criteria against a contract with zero LLM calls; "
            "'trust' approves a workspace spec (direnv-style content-hash pin); "
            "'list' shows available missions and their trust status."
        ),
    )
    sub = mission.add_subparsers(dest="subcommand")

    check = sub.add_parser(
        "check",
        help="Run a mission's success criteria against a contract (zero-LLM, CI-usable)",
        description=(
            "Loads the mission spec (trust-gated), re-reads the on-disk "
            "contract, runs every success criterion, and renders a scorecard. "
            "Exit 0 when all non-advisory checks pass, 1 when any fails, "
            "2 on spec/trust/contract errors."
        ),
    )
    check.add_argument("spec", help="Mission name (e.g. quality-coverage) or spec YAML path")
    check.add_argument(
        "contract",
        nargs="?",
        default=_DEFAULT_CONTRACT,
        help=f"Path to the contract to verify (default: {_DEFAULT_CONTRACT})",
    )
    check.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit the scorecard as JSON (machine-readable, for CI)",
    )
    check.set_defaults(func=run, subcommand="check")

    trust = sub.add_parser(
        "trust",
        help="Approve a mission spec (records its content hash, direnv-style)",
        description=(
            "Records the sha256 of the spec file in the user-global trust "
            "database. A changed file requires re-approval. Built-ins and "
            "user-global specs (~/.fluid/missions/) are trusted implicitly."
        ),
    )
    trust.add_argument("spec", help="Mission name or spec YAML path to trust")
    trust.set_defaults(func=run, subcommand="trust")

    lst = sub.add_parser(
        "list",
        help="List available mission specs and their trust status",
    )
    lst.set_defaults(func=run, subcommand="list")

    mission.set_defaults(func=run, subcommand=None)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Dispatch ``fluid mission <subcommand>``."""
    subcommand = getattr(args, "subcommand", None)
    if subcommand == "check":
        return _run_check(args)
    if subcommand == "trust":
        return _run_trust(args)
    if subcommand == "list":
        return _run_list(args)
    print("Usage: fluid mission {check,trust,list} — see `fluid mission --help`.")
    return EXIT_ERROR


def _load_spec_or_fail(ref: str):
    """Resolve + load a mission spec; returns (spec, None) or (None, exit_code)."""
    from fluid_build.copilot.missions import MissionSpecError, resolve_mission_spec

    try:
        return resolve_mission_spec(ref), None
    except MissionSpecError as exc:
        print(f"Mission spec error: {exc}")
        return None, EXIT_ERROR


def _run_check(args: argparse.Namespace) -> int:
    import json as _json

    from fluid_build.copilot.missions import (
        MissionCheckError,
        MissionTrustError,
        require_trusted,
        run_mission_checks,
    )

    spec, err = _load_spec_or_fail(args.spec)
    if spec is None:
        return err

    try:
        require_trusted(spec)
    except MissionTrustError as exc:
        print(f"Refusing to run untrusted mission spec ({exc.status}).")
        print(str(exc))
        return EXIT_ERROR

    try:
        scorecard = run_mission_checks(spec, Path(args.contract))
    except MissionCheckError as exc:
        print(f"Cannot run checks: {exc}")
        return EXIT_ERROR

    if args.json:
        print(_json.dumps(scorecard.to_dict(), indent=2, sort_keys=True))
    else:
        _render_scorecard(spec, scorecard)
    return EXIT_PASS if scorecard.passed else EXIT_FAIL


def _render_scorecard(spec, scorecard) -> None:
    goal = " ".join(str(spec.goal).split())
    print(f"Mission: {scorecard.mission} — {spec.description}")
    print(f"Goal: {goal}")
    print(f"Contract: {scorecard.contract_path}")
    print(f"Contract sha256: {scorecard.contract_sha256[:16]}…")
    print()
    for index, result in enumerate(scorecard.results):
        verdict = "PASS" if result.passed else "FAIL"
        advisory = " (advisory)" if result.advisory else ""
        if index < len(spec.success_criteria):
            label = spec.success_criteria[index].describe()
        else:  # pragma: no cover — results always mirror criteria
            label = result.name
        summary = result.detail or label
        print(f"  {verdict}  {result.name:<10} {summary}{advisory}")
        for line in result.diagnostics:
            print(f"        - {line}")
    print()
    verdict = "PASS" if scorecard.passed else "FAIL"
    print(
        f"Scorecard: {scorecard.gating_passed}/{scorecard.gating_total} "
        f"non-advisory checks passing — {verdict}"
    )


def _run_trust(args: argparse.Namespace) -> int:
    from fluid_build.copilot.missions import trust_file_path, trust_spec

    spec, err = _load_spec_or_fail(args.spec)
    if spec is None:
        return err

    record = trust_spec(spec)
    if record["status"] in ("builtin", "user_global"):
        kind = "built-in" if record["status"] == "builtin" else "user-global"
        print(f"Mission '{spec.name}' is {kind} — implicitly trusted, nothing to pin.")
        return EXIT_PASS
    print(f"Trusted mission '{spec.name}'.")
    print(f"  Path:   {record['path']}")
    print(f"  sha256: {record['sha256']}")
    print(f"  Pinned in {trust_file_path()} — editing the file requires re-approval.")
    return EXIT_PASS


def _run_list(args: argparse.Namespace) -> int:
    from fluid_build.copilot.missions import discover_all_mission_specs, spec_trust_status

    specs = discover_all_mission_specs()
    if not specs:
        print("No mission specs found.")
        return EXIT_PASS

    print(f"{'NAME':<20} {'TRUST':<12} DESCRIPTION")
    for name in sorted(specs):
        spec = specs[name]
        status = spec_trust_status(spec)
        print(f"{name:<20} {status:<12} {spec.description}")
    print()
    print("Run criteria with: fluid mission check <name> [contract]")
    print("Approve a workspace spec with: fluid mission trust <path>")
    return EXIT_PASS


__all__ = ["COMMAND", "register", "run"]
