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

"""StageCoordinator top-level helpers — physical extraction.

Lifted from ``copilot/agents/coordinator.py`` (host file was 1522
LOC). Pure functions that don't depend on coordinator state:

* :func:`parallel_physical_enabled` — env-var gate for parallel
  physical-stage execution.
* :func:`new_run_id` — short hex id for the staged-invocation span.
* :func:`diagnose_failing_stage` — map a :class:`ValidationReport`
  back to the stage responsible for the error.
* :class:`CoordinatorResult` — output dataclass.
* Constants: ``MAX_REPAIR_ATTEMPTS``, ``PHYSICAL_REPAIR_STAGES``,
  ``LOGICAL_REPAIR_STAGES``.

``coordinator.py`` re-imports each at module top so existing call
sites and test patches keep resolving.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Optional

from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    PhysicalDraft,
    ValidationReport,
)

# Env-var escape hatch: set to ``0`` / ``false`` / ``no`` to force
# the physical stages to run sequentially. Default is parallel.
_PARALLEL_ENV_VAR = "FLUID_COPILOT_PARALLEL_PHYSICAL"
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def parallel_physical_enabled() -> bool:
    raw = os.environ.get(_PARALLEL_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLE_TOKENS


# Hard cap: one repair attempt per invocation. The re-run uses
# ``session.no_cache=True`` so the LLM isn't served the same bad
# output from the cache.
MAX_REPAIR_ATTEMPTS = 1

# Physical-scope stages we can re-run locally from
# ``_run_physical_stages``.
PHYSICAL_REPAIR_STAGES = frozenset({"builder", "transformation"})

# Phase 3.7 — logical-scope repair. When the LogicalAgent's draft
# fails OSI / DV2 / Dimensional conformance, the LogicalAgent gets
# one extra repair turn before the run is failed.
LOGICAL_REPAIR_STAGES = frozenset({"logical"})


def diagnose_failing_stage(report: ValidationReport) -> Optional[str]:
    """Map a failed validator report back to the stage responsible.

    Returns one of ``"logical"`` / ``"builder"`` / ``"transformation"``
    / ``"readme"`` when the error's ``field`` (or message) clearly
    implicates that stage; returns ``None`` when the signal is too
    noisy to route (we prefer "don't repair" over "repair the wrong
    stage"). Pure function — no session, no I/O, trivially testable.
    """
    if report.passes_schema:
        return None

    # First pass: structured ``field`` hints win, because the
    # validator module chooses these deliberately.
    for finding in report.issues:
        if finding.severity != "error":
            continue
        field = (finding.field or "").strip()
        if not field:
            continue
        if field == "osi" or field.startswith("osi."):
            return "logical"
        if field == "dv2" or field.startswith("dv2."):
            return "logical"
        if field == "dimensional" or field.startswith("dimensional."):
            return "logical"
        if field == "exposes" or field.startswith("exposes"):
            return "builder"
        if field.startswith("transform_plan") or field.startswith("builds"):
            return "transformation"
        if field.startswith("readme"):
            return "readme"

    # Second pass: fall back to message scanning for validators that
    # didn't populate ``field``.
    for finding in report.issues:
        if finding.severity != "error":
            continue
        msg = (finding.message or "").lower()
        if "transform" in msg or "build sql" in msg or "builds[" in msg:
            return "transformation"
        if "exposes" in msg or "contract" in msg:
            return "builder"
        if "osi" in msg or "semantic model" in msg:
            return "logical"

    return None


@dataclass
class CoordinatorResult:
    logical: LogicalDraft
    contract: dict
    physical: Optional[PhysicalDraft] = None


def new_run_id() -> str:
    """Generate a short run-id for the staged-invocation parent span.

    Short enough to read in a CLI log line; uniqueness is per-process
    so collisions across hosts are negligible.
    """
    return uuid.uuid4().hex[:12]


__all__ = [
    "CoordinatorResult",
    "LOGICAL_REPAIR_STAGES",
    "MAX_REPAIR_ATTEMPTS",
    "PHYSICAL_REPAIR_STAGES",
    "diagnose_failing_stage",
    "new_run_id",
    "parallel_physical_enabled",
]
