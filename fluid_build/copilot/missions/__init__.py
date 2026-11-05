# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Mission-based deep agents — declarative goal specs (RFC-deep-agents.md).

A *mission* is a declarative YAML spec stating a goal, deterministically
verifiable success criteria, budgets, and gates. This package ships the
zero-LLM foundation (deep-agents PR 1):

- :mod:`spec` — the ``MissionSpec`` format, loader, and typed validation
  errors (``fluid_build/copilot/missions/builtin/`` carries the shipped
  missions as package data, mirroring ``cli/agent_specs/``).
- :mod:`trust` — direnv-style first-run content-hash pinning for
  workspace-local specs (``fluid mission trust``). Fail closed.
- :mod:`checks` — the code-owned success-criteria check registry
  (``validate`` / ``ai_ready`` / the frozen ``predicate`` DSL) that runs
  against the re-read, re-hashed on-disk contract and renders a scorecard.

Deep-agents PR 2 adds the autonomous half:

- :mod:`runner` — ``MissionRunner``: the VERIFY → PLAN → EXECUTE → GATE
  → PROGRESS loop. Only :mod:`checks` can terminate it.
- :mod:`planner` — the one cheap LLM call per cycle that turns failing
  diagnostics into an ordered repair plan (and degrades to a
  deterministic fallback plan when it can't).
- :mod:`destructive` — the fail-closed old→new contract-diff classifier
  behind the gate.
- :mod:`gate` — ``confirm_fail_closed``: the destructive gate's confirm
  primitive, which rejects on a non-TTY (the inverse of
  ``cli/_preview_panel.confirm``'s fail-open posture).
- :mod:`store` — the ``.fluid/missions/<run-id>/`` run directory:
  manifest, digest-bound scorecards, per-cycle cost receipts.

Only :mod:`planner` and :mod:`runner` reach an LLM, and both are
imported lazily. Import cost is irrelevant to ``fluid --help`` — the CLI
(``cli/mission.py``) defers importing this package into its handlers.
"""

from fluid_build.copilot.missions.checks import (
    MISSION_CHECKS,
    CheckResult,
    MissionCheckError,
    MissionScorecard,
    register_mission_check,
    run_mission_checks,
)
from fluid_build.copilot.missions.destructive import (
    DiffVerdict,
    classify_contract_diff,
    is_destructive,
)
from fluid_build.copilot.missions.gate import confirm_fail_closed, reject_destructive
from fluid_build.copilot.missions.spec import (
    BUILTIN_MISSIONS_DIR,
    CriterionSpec,
    MissionBudgets,
    MissionGates,
    MissionSpec,
    MissionSpecError,
    discover_all_mission_specs,
    load_builtin_mission_spec,
    load_mission_spec_from_path,
    resolve_mission_spec,
)
from fluid_build.copilot.missions.store import MissionRunStore, find_resumable_run
from fluid_build.copilot.missions.trust import (
    MissionTrustError,
    require_trusted,
    spec_trust_status,
    trust_file_path,
    trust_spec,
)

__all__ = [
    "BUILTIN_MISSIONS_DIR",
    "CheckResult",
    "CriterionSpec",
    "DiffVerdict",
    "MISSION_CHECKS",
    "MissionBudgets",
    "MissionCheckError",
    "MissionGates",
    "MissionRunStore",
    "MissionScorecard",
    "MissionSpec",
    "MissionSpecError",
    "MissionTrustError",
    "classify_contract_diff",
    "confirm_fail_closed",
    "discover_all_mission_specs",
    "find_resumable_run",
    "is_destructive",
    "reject_destructive",
    "load_builtin_mission_spec",
    "load_mission_spec_from_path",
    "register_mission_check",
    "require_trusted",
    "resolve_mission_spec",
    "run_mission_checks",
    "spec_trust_status",
    "trust_file_path",
    "trust_spec",
]
