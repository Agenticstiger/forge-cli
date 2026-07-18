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
"""``fluid mission`` — mission specs: trust pinning, scorecards, runner.

RFC-deep-agents.md. Four subcommands:

- ``fluid mission check <spec> [contract]`` — load the spec
  (trust-gated), run its deterministic success criteria against the
  re-read on-disk contract, render a scorecard, exit 0/1. Zero LLM
  calls — usable as a standalone CI gate.
- ``fluid mission run <spec> [contract]`` — the autonomous loop
  (PR 2): VERIFY → PLAN → EXECUTE → GATE → PROGRESS, terminating only
  when the code-owned checks pass, or when a budget / iteration /
  stall ceiling fires. ``--resume`` re-enters an existing run at
  VERIFY.
- ``fluid mission trust <spec>`` — one-time direnv-style approval:
  pins the spec's content hash so it may configure autonomous behavior.
- ``fluid mission list`` — built-in + user mission specs with their
  trust status.

Everything heavy (yaml, the checks registry, ``jsonschema`` via
``schema_manager``, the LLM runtime) is imported inside the handlers —
``register`` needs argparse only, so the ``fluid --help`` cold path
stays light (tests/perf/test_startup_budget.py).
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

    run_p = sub.add_parser(
        "run",
        help="Run a mission autonomously until its code-owned checks pass",
        description=(
            "Runs the mission loop: VERIFY (re-read + re-hash the contract, "
            "re-run every success criterion) -> PLAN -> EXECUTE -> GATE -> "
            "PROGRESS. Only the deterministic checks can declare success; the "
            "LLM plans and edits but can never terminate the mission. Budgets "
            "(max_usd / max_iterations / max_wall_seconds) come from the spec. "
            "Destructive edits are gated and fail closed on a non-TTY — --yes "
            "never approves a destructive diff."
        ),
    )
    run_p.add_argument("spec", help="Mission name (e.g. quality-coverage) or spec YAML path")
    run_p.add_argument(
        "contract",
        nargs="?",
        default=_DEFAULT_CONTRACT,
        help=f"Path to the contract to work on (default: {_DEFAULT_CONTRACT})",
    )
    run_p.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "Re-enter the newest unfinished run for this mission. VERIFY is "
            "idempotent, so resuming just re-verifies the contract on disk — "
            "there is no replay."
        ),
    )
    run_p.add_argument("--run-id", default=None, help="Target a specific mission run id")
    run_p.add_argument(
        "--llm-provider", default=None, help="LLM provider (default: your configured provider)"
    )
    run_p.add_argument("--llm-model", default=None, help="LLM model id")
    run_p.add_argument(
        "--workspace",
        default=None,
        help="Workspace root for receipts and tool confinement (default: auto-detected)",
    )
    run_p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit the outcome as JSON (machine-readable, for CI)",
    )
    run_p.set_defaults(func=run, subcommand="run")

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
    if subcommand == "run":
        return _run_mission(args)
    if subcommand == "trust":
        return _run_trust(args)
    if subcommand == "list":
        return _run_list(args)
    print("Usage: fluid mission {check,run,trust,list} — see `fluid mission --help`.")
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


def _resolve_workspace_root(args: argparse.Namespace) -> Path:
    """Workspace root for receipts + tool confinement.

    Same resolution order the agent-loop entry point uses: an explicit
    ``--workspace`` wins, else the detected project root, else cwd. The
    human operator's intent decides the confinement boundary, never the
    model.
    """
    from fluid_build.cli.workspace_config import find_workspace_root

    explicit = getattr(args, "workspace", None)
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (find_workspace_root(Path.cwd()) or Path.cwd()).resolve()


def _resolve_mission_llm_config(args: argparse.Namespace):
    """Resolve the LLM config, or ``(None, message)`` when unavailable."""
    from fluid_build.llm.providers import CopilotGenerationError, resolve_llm_config

    try:
        return resolve_llm_config(args, environ=None), None
    except CopilotGenerationError as exc:
        return None, str(exc)


def _agent_loop_executor(
    step,
    contract,
    *,
    spec,
    llm_config,
    workspace_root,
    console=None,
):
    """The real EXECUTE step — one repair through the inner agent loop.

    This lives in ``cli`` because it needs two ``cli`` collaborators
    (``run_copilot_agent_loop`` and ``build_agent_loop_seed_context``)
    and ``fluid_build.copilot`` must not import ``fluid_build.cli``. The
    runner declares the shape it needs as
    ``copilot.missions.executor.MissionStepExecutor``; this satisfies it.

    The two mission-specific parameters are the honest surgery the RFC
    called for: ``tool_allowlist`` (``spec.tools.allow``, intersected
    with the live registry inside the loop) and ``goal_scope`` (this
    step's framing). The seed — the contract as it exists on disk right
    now — rides the existing ``seed_contract_override`` seam.

    Returns the proposed contract; it never writes. The runner owns the
    write so every proposal passes the destructive gate first.
    """
    from fluid_build.cli.forge_copilot_agent_loop import run_copilot_agent_loop
    from fluid_build.cli.forge_copilot_runtime import build_agent_loop_seed_context
    from fluid_build.copilot.missions.runner import (
        INNER_LOOP_ITERATIONS,
        extract_proposed_contract,
    )

    context = build_agent_loop_seed_context(
        {"project_goal": spec.goal},
        seed_contract=contract,
    )
    payload = run_copilot_agent_loop(
        context=context,
        llm_config=llm_config,
        workspace_root=workspace_root,
        console=console,
        max_iterations=INNER_LOOP_ITERATIONS,
        tool_allowlist=list(spec.tools_allow) or None,
        goal_scope=step.goal,
    )
    return extract_proposed_contract(payload)


def build_mission_runtime():
    """Wire the cli-side collaborators the mission runner depends on.

    The single place where the ``cli`` tier is bound to the ``copilot``
    tier's Protocols. Keeping it one function means the dependency
    inversion has exactly one production implementation to audit.
    """
    from fluid_build.cli.forge_contract_factory import write_contract
    from fluid_build.cli.forge_copilot_runtime import extract_json_object
    from fluid_build.copilot.missions.executor import MissionRuntime

    return MissionRuntime(
        execute=_agent_loop_executor,
        write_contract=write_contract,
        parse_json=extract_json_object,
    )


def _run_mission(args: argparse.Namespace) -> int:
    """``fluid mission run`` — the autonomous VERIFY-anchored loop."""
    import json as _json

    from fluid_build.copilot.missions import MissionTrustError, require_trusted
    from fluid_build.copilot.missions.runner import run_mission

    spec, err = _load_spec_or_fail(args.spec)
    if spec is None:
        return err

    # The trust gate runs BEFORE anything the spec configures takes
    # effect — tool allowlist, gate mode, budgets, and the goal text
    # that reaches the planner are all attacker-controlled in a cloned
    # repo otherwise.
    try:
        require_trusted(spec)
    except MissionTrustError as exc:
        print(f"Refusing to run untrusted mission spec ({exc.status}).")
        print(str(exc))
        return EXIT_ERROR

    llm_config, llm_error = _resolve_mission_llm_config(args)
    if llm_config is None:
        print(f"Cannot run mission: {llm_error}")
        print("Missions need an LLM to plan and edit. `fluid mission check` runs zero-LLM.")
        return EXIT_ERROR

    workspace_root = _resolve_workspace_root(args)
    console = _build_console()

    print(f"Mission: {spec.name} — {spec.description}")
    print(f"Goal: {' '.join(str(spec.goal).split())}")
    print(f"Contract: {Path(args.contract).resolve()}")
    print(
        f"Budgets: max_usd={spec.budgets.max_usd} "
        f"max_iterations={spec.budgets.max_iterations} "
        f"max_wall_seconds={spec.budgets.max_wall_seconds}"
    )
    print()

    try:
        outcome = run_mission(
            spec,
            Path(args.contract),
            llm_config=llm_config,
            workspace_root=workspace_root,
            resume=bool(getattr(args, "resume", False)),
            run_id=getattr(args, "run_id", None),
            console=console,
            runtime=build_mission_runtime(),
        )
    except Exception as exc:  # noqa: BLE001 — surface a typed name, never a traceback
        LOG.warning("mission_run_failed", extra={"error": type(exc).__name__}, exc_info=True)
        print(f"Mission run failed ({type(exc).__name__}) — see server logs.")
        return EXIT_ERROR

    if getattr(args, "json", False):
        print(_json.dumps(outcome.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        _render_outcome(spec, outcome)
    return EXIT_PASS if outcome.passed else EXIT_FAIL


def _build_console():
    """Rich console when available; ``None`` degrades to plain output."""
    try:
        from rich.console import Console

        return Console()
    except Exception:  # noqa: BLE001 — rich is optional at this layer
        return None


def _render_outcome(spec, outcome) -> None:
    print()
    if outcome.scorecard is not None:
        _render_scorecard(spec, outcome.scorecard)
        print()
    verdict = {
        "complete": "MISSION COMPLETE",
        "paused": "MISSION PAUSED",
        "failed": "MISSION FAILED",
    }.get(outcome.status, outcome.status.upper())
    reason = f" ({outcome.pause_reason})" if outcome.pause_reason else ""
    print(f"{verdict}{reason} after {outcome.cycles} cycle(s) — {outcome.detail}")
    print(f"Spend: ${outcome.spend_usd:.4f}")
    print(f"Receipts: {outcome.run_dir}")
    if outcome.status == "paused":
        print(f"Resume with: fluid mission run {spec.name} --resume")


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
