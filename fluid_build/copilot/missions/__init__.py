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

The autonomous ``MissionRunner`` (LLM outer loop) is PR 2; nothing in this
package calls an LLM. Import cost is irrelevant to ``fluid --help`` — the
CLI (``cli/mission.py``) defers importing this package into its handlers.
"""

from fluid_build.copilot.missions.checks import (
    MISSION_CHECKS,
    CheckResult,
    MissionCheckError,
    MissionScorecard,
    register_mission_check,
    run_mission_checks,
)
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
    "MISSION_CHECKS",
    "MissionBudgets",
    "MissionCheckError",
    "MissionGates",
    "MissionScorecard",
    "MissionSpec",
    "MissionSpecError",
    "MissionTrustError",
    "discover_all_mission_specs",
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
