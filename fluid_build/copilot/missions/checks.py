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

"""Mission success-criteria checks — the termination authority.

The load-bearing design decision (RFC-deep-agents.md): mission
completion is decided **only** by this registry of code-owned checks,
re-run against the re-read, re-hashed on-disk contract. An LLM plans
and proposes; it can never declare done. Checks therefore:

- **wrap existing CLI internals, never parallel implementations** —
  ``validate`` calls the same ``FluidSchemaManager.validate_contract``
  the ``fluid validate`` stage runs; ``ai_ready`` reuses
  ``copilot/agents/ai_ready_agent.enforce_ai_ready``;
- run against the **on-disk artifact**, not any in-memory claim
  (:func:`run_mission_checks` re-reads and re-hashes the contract);
- return structured pass/fail + diagnostics, with **every**
  ``detail``/``diagnostics`` string passed through the secret redactor
  before persistence or LLM exposure (the PRs #28–#33 invariant applies
  to the whole registry, built-in or registered later);
- **fail closed**: a crashing check is a failing check, and its
  exception text never round-trips into the result (typed name only —
  the traceback goes to server logs).

The registry mirrors the ``IAC_PLUGINS`` shape
(``iac/registry.py::register_iac_plugin``).
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from fluid_build.copilot.missions.spec import CriterionSpec, MissionSpec

LOG = logging.getLogger("fluid.copilot.missions.checks")

#: Cap per-check diagnostics so a pathological contract can't flood the
#: scorecard (or, in PR 2, the repair prompt).
MAX_DIAGNOSTICS = 25


class MissionCheckError(RuntimeError):
    """Raised when the check harness itself cannot run (unreadable contract)."""


@dataclass
class CheckResult:
    """Structured outcome of one criterion. ``detail`` is the one-line
    summary; ``diagnostics`` carry per-finding lines (both redacted by
    the harness before anything persists or reaches an LLM)."""

    name: str
    passed: bool
    advisory: bool = False
    detail: str = ""
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "advisory": self.advisory,
            "detail": self.detail,
            "diagnostics": list(self.diagnostics),
        }


@dataclass
class MissionScorecard:
    """The scorecard: termination authority (and, in PR 2, resume pointer).

    Digest-bound: ``contract_sha256`` is the canonical hash of the exact
    contract dict that was verified (same recipe as the checkpoint
    stale-detector), so a green scorecard can be marked STALE the moment
    the on-disk contract diverges.
    """

    mission: str
    goal: str
    contract_path: str
    contract_sha256: str
    results: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """ALL non-advisory checks pass — advisory results never gate."""
        return all(r.passed for r in self.results if not r.advisory)

    @property
    def gating_total(self) -> int:
        return sum(1 for r in self.results if not r.advisory)

    @property
    def gating_passed(self) -> int:
        return sum(1 for r in self.results if not r.advisory and r.passed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission": self.mission,
            "goal": self.goal,
            "contract_path": self.contract_path,
            "contract_sha256": self.contract_sha256,
            "passed": self.passed,
            "gating_passed": self.gating_passed,
            "gating_total": self.gating_total,
            "results": [r.to_dict() for r in self.results],
        }


#: Check implementations: ``fn(criterion, contract, contract_path=...) -> CheckResult``.
#: Each call receives its own deep copy of the contract dict — a check can
#: never leak mutations into a sibling check's view of the artifact.
CheckFn = Callable[..., CheckResult]

MISSION_CHECKS: Dict[str, CheckFn] = {}


def register_mission_check(name: str, fn: CheckFn) -> None:
    """Register a mission check under *name* (``IAC_PLUGINS`` registry shape).

    v1 has no entry-points discovery on purpose — third-party checks
    raise the stakes from suggestion to attestation and need their own
    provenance design (RFC "v1 scope"). This seam exists for tests and
    for PR 2's in-process wiring.
    """
    MISSION_CHECKS[name] = fn


def get_mission_check(name: str) -> Optional[CheckFn]:
    """Return the registered check for *name*, or ``None`` if unknown."""
    return MISSION_CHECKS.get(name)


# ---------------------------------------------------------------------------
# Built-in check: validate
# ---------------------------------------------------------------------------


def _check_validate(
    criterion: CriterionSpec, contract: Dict[str, Any], *, contract_path: Path
) -> CheckResult:
    """``fluid validate`` green — in-process, exit-0 semantics.

    Calls the exact CLI internal (``FluidSchemaManager.validate_contract``
    with ``offline_only=True`` and the contract's own ``fluidVersion``
    auto-detected) that ``cli/validate.run_on_contract_dict`` wraps —
    never a parallel implementation. Non-strict semantics: schema errors
    fail, warnings alone do not (matching ``fluid validate``'s exit 0).
    """
    from fluid_build.schema_manager import FluidSchemaManager

    result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    version = f" (schema v{result.schema_version})" if result.schema_version else ""
    if result.is_valid:
        detail = f"contract validates{version}"
        if result.warnings:
            detail += f"; {len(result.warnings)} warning(s)"
        return CheckResult(name="validate", passed=True, detail=detail)
    return CheckResult(
        name="validate",
        passed=False,
        detail=f"{len(result.errors)} schema error(s){version}",
        diagnostics=[str(err) for err in result.errors[:MAX_DIAGNOSTICS]],
    )


# ---------------------------------------------------------------------------
# Built-in check: ai_ready
# ---------------------------------------------------------------------------


def _expose_id(expose: Dict[str, Any]) -> str:
    """Same id-resolution order as ``enforce_ai_ready``'s per-port loop."""
    return str(expose.get("exposeId") or expose.get("id") or expose.get("name") or "port")


def _sensitive_exposes_missing_policy(
    contract: Dict[str, Any], sensitive_ids: List[str]
) -> List[str]:
    """Sensitive expose ids whose *on-disk* port lacks a non-empty agentPolicy."""
    annotated: Dict[str, bool] = {}
    exposes = contract.get("exposes")
    for expose in exposes if isinstance(exposes, list) else []:
        if not isinstance(expose, dict):
            continue
        policy = expose.get("policy")
        agent_policy = policy.get("agentPolicy") if isinstance(policy, dict) else None
        annotated[_expose_id(expose)] = isinstance(agent_policy, dict) and bool(agent_policy)
    return [eid for eid in sensitive_ids if not annotated.get(eid, False)]


def _check_ai_ready(
    criterion: CriterionSpec, contract: Dict[str, Any], *, contract_path: Path
) -> CheckResult:
    """Reuse the ``copilot/agents/ai_ready`` enforcement as a verifier.

    Runs :func:`enforce_ai_ready` on a scratch copy (the pass mutates its
    input; a VERIFY check must never write) and evaluates the criterion's
    ``require`` block against the report — the on-disk contract is only
    ever read. With no ``require``, the report's own ``is_ai_ready``
    verdict gates. The ``FLUID_AI_READY=0`` kill-switch fails the check:
    a disabled enforcement cannot attest anything (fail closed).
    """
    from fluid_build.copilot.agents.ai_ready_agent import enforce_ai_ready

    scratch = copy.deepcopy(contract)
    report = enforce_ai_ready(scratch)
    if not report.enabled:
        return CheckResult(
            name="ai_ready",
            passed=False,
            advisory=criterion.advisory,
            detail="FLUID_AI_READY=0 — enforcement disabled, cannot attest readiness",
        )

    diagnostics: List[str] = []
    require = criterion.require

    if not require:
        passed = report.is_ai_ready
        if not report.exposes_annotated:
            diagnostics.append("contract has no output ports to annotate")
        diagnostics.extend(
            f"missing description: {ref}" for ref in report.missing_descriptions[:MAX_DIAGNOSTICS]
        )
        detail = (
            "contract is AI-ready"
            if passed
            else f"not AI-ready ({len(report.missing_descriptions)} missing description(s))"
        )
        return CheckResult(
            name="ai_ready",
            passed=passed,
            advisory=criterion.advisory,
            detail=detail,
            diagnostics=diagnostics[:MAX_DIAGNOSTICS],
        )

    passed = True
    if "missing_descriptions" in require:
        allowed = int(require["missing_descriptions"])
        found = len(report.missing_descriptions)
        if found > allowed:
            passed = False
            diagnostics.extend(
                f"missing description: {ref}"
                for ref in report.missing_descriptions[:MAX_DIAGNOSTICS]
            )
    if require.get("sensitive_exposes_annotated"):
        missing = _sensitive_exposes_missing_policy(contract, report.sensitive_exposes)
        if missing:
            passed = False
            diagnostics.extend(f"sensitive port without agentPolicy: {eid}" for eid in missing)

    detail = (
        "ai_ready requirements met"
        if passed
        else f"ai_ready requirements not met ({len(diagnostics)} finding(s))"
    )
    return CheckResult(
        name="ai_ready",
        passed=passed,
        advisory=criterion.advisory,
        detail=detail,
        diagnostics=diagnostics[:MAX_DIAGNOSTICS],
    )


# ---------------------------------------------------------------------------
# Built-in check: predicate (frozen DSL)
# ---------------------------------------------------------------------------


def _resolve_predicate_path(
    contract: Dict[str, Any], path: str
) -> Tuple[List[Tuple[str, Any]], List[str]]:
    """Resolve a frozen-DSL path to ``(leaves, missing)``.

    ``leaves`` are ``(concrete_path, value)`` pairs with fan-out indexes
    substituted (``exposes[1].contract.dq.rules``); ``missing`` are the
    concrete paths where traversal stopped. Empty arrays under ``[*]``
    count as missing — "every element of nothing" must not vacuously
    satisfy a success criterion (fail closed).
    """
    nodes: List[Tuple[str, Any]] = [("", contract)]
    missing: List[str] = []
    for segment in path.split("."):
        fan_out = segment.endswith("[*]")
        key = segment[:-3] if fan_out else segment
        next_nodes: List[Tuple[str, Any]] = []
        for prefix, node in nodes:
            concrete = f"{prefix}.{key}" if prefix else key
            if not isinstance(node, dict) or key not in node:
                missing.append(concrete)
                continue
            value = node[key]
            if not fan_out:
                next_nodes.append((concrete, value))
                continue
            if not isinstance(value, list):
                missing.append(f"{concrete}[*] (not an array)")
            elif not value:
                missing.append(f"{concrete}[*] (empty array)")
            else:
                next_nodes.extend(
                    (f"{concrete}[{index}]", item) for index, item in enumerate(value)
                )
        nodes = next_nodes
    return nodes, missing


def _value_exists(value: Any) -> bool:
    """``exists`` semantics: non-null, and non-empty for str/list/dict."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    return True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(op: str, leaf: Any, expected: Any) -> Tuple[bool, str]:
    """Evaluate one leaf. Returns ``(ok, reason_if_not)``. Type mismatches
    fail with a reason rather than raising — fail closed, never crash."""
    if op == "eq":
        return (leaf == expected, f"expected == {expected!r}, got {leaf!r}")
    if op == "ne":
        return (leaf != expected, f"expected != {expected!r}, got {leaf!r}")
    if op == "contains":
        if isinstance(leaf, str):
            ok = isinstance(expected, str) and expected in leaf
        elif isinstance(leaf, list):
            ok = expected in leaf
        else:
            return (False, f"contains needs a string or array, got {type(leaf).__name__}")
        return (ok, f"expected to contain {expected!r}, got {leaf!r}")

    # Ordered comparisons: both numeric, or both strings — never mixed.
    if _is_number(leaf) and _is_number(expected):
        pass
    elif isinstance(leaf, str) and isinstance(expected, str):
        pass
    else:
        return (
            False,
            f"cannot order-compare {type(leaf).__name__} with {type(expected).__name__}",
        )
    ok = {
        "lt": leaf < expected,
        "lte": leaf <= expected,
        "gt": leaf > expected,
        "gte": leaf >= expected,
    }[op]
    return (ok, f"expected {op} {expected!r}, got {leaf!r}")


def _check_predicate(
    criterion: CriterionSpec, contract: Dict[str, Any], *, contract_path: Path
) -> CheckResult:
    """Evaluate the frozen predicate DSL against the contract dict.

    ALL fanned-out leaves must satisfy the op. A path that resolves to
    nothing (missing keys, empty arrays) fails every op except
    ``exists: false`` — no evidence is never success (fail closed).
    """
    leaves, missing = _resolve_predicate_path(contract, criterion.path)
    label = criterion.describe()
    diagnostics: List[str] = []

    if criterion.op == "exists":
        want = criterion.value if criterion.value_provided else True
        absent = [f"{p}: path not found" for p in missing]
        absent += [f"{p}: empty/null value" for p, v in leaves if not _value_exists(v)]
        present = [p for p, v in leaves if _value_exists(v)]
        if want:
            passed = not absent and bool(present)
            diagnostics = absent or ([] if present else [f"{criterion.path}: path not found"])
        else:
            passed = not present
            diagnostics = [f"{p}: unexpectedly present" for p in present]
        detail = (
            f"{label} — {len(present)} present, {len(absent)} absent"
            if (present or absent)
            else f"{label} — path not found"
        )
        return CheckResult(
            name="predicate",
            passed=passed,
            advisory=criterion.advisory,
            detail=detail,
            diagnostics=diagnostics[:MAX_DIAGNOSTICS],
        )

    diagnostics.extend(f"{p}: path not found" for p in missing)
    failing = 0
    for concrete, leaf in leaves:
        ok, reason = _compare(criterion.op, leaf, criterion.value)
        if not ok:
            failing += 1
            diagnostics.append(f"{concrete}: {reason}")
    if not leaves:
        diagnostics.append(f"{criterion.path}: no values to compare")

    passed = bool(leaves) and not missing and failing == 0
    detail = f"{label} — {len(leaves) - failing}/{len(leaves)} leaf value(s) satisfy"
    if missing:
        detail += f", {len(missing)} path(s) missing"
    return CheckResult(
        name="predicate",
        passed=passed,
        advisory=criterion.advisory,
        detail=detail,
        diagnostics=diagnostics[:MAX_DIAGNOSTICS],
    )


register_mission_check("validate", _check_validate)
register_mission_check("ai_ready", _check_ai_ready)
register_mission_check("predicate", _check_predicate)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _redact_result(result: CheckResult) -> CheckResult:
    """Single redaction chokepoint for the whole registry."""
    from fluid_build.observability.secret_redactor import redact_secret_text

    result.detail = redact_secret_text(result.detail)
    result.diagnostics = [redact_secret_text(line) for line in result.diagnostics]
    return result


def load_contract_for_checks(contract_path: Path) -> Tuple[Dict[str, Any], str]:
    """Re-read the contract from disk and re-hash it.

    Returns ``(contract_dict, canonical_sha256)``. The hash reuses the
    checkpoint stale-detector's canonical recipe so scorecards and
    checkpoints agree on what "the same contract" means. Raises
    :class:`MissionCheckError` on unreadable/unparseable input.
    """
    # Same canonical recipe as StageCoordinator._hash_contract — one hash
    # definition across checkpoints, stale-detection, and scorecards.
    from fluid_build.copilot.checkpoint_stale import _canonical_hash

    contract_path = contract_path.resolve()
    if not contract_path.is_file():
        raise MissionCheckError(f"Contract not found: {contract_path}")
    try:
        raw = contract_path.read_text(encoding="utf-8")
        contract = yaml.safe_load(raw)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise MissionCheckError(
            f"Contract at {contract_path} is not readable YAML ({type(exc).__name__})."
        ) from exc
    if not isinstance(contract, dict):
        raise MissionCheckError(f"Contract at {contract_path} is not a mapping.")
    return contract, _canonical_hash(contract)


def run_mission_checks(spec: MissionSpec, contract_path: Path) -> MissionScorecard:
    """VERIFY: run every criterion against the re-read on-disk contract.

    Idempotent by construction — this is both the termination authority
    and (in PR 2) the resume pointer. Trust-gating the *spec* is the
    caller's job (``cli/mission.py`` gates before calling); this
    function assumes an approved spec and only reads the contract.
    """
    contract, contract_sha256 = load_contract_for_checks(contract_path)
    scorecard = MissionScorecard(
        mission=spec.name,
        goal=spec.goal,
        contract_path=str(contract_path.resolve()),
        contract_sha256=contract_sha256,
    )
    for criterion in spec.success_criteria:
        fn = get_mission_check(criterion.check)
        if fn is None:
            # Spec validation pins check names, but a registry mutation
            # (test seam, future plugin unload) must still fail closed.
            scorecard.results.append(
                CheckResult(
                    name=criterion.check,
                    passed=False,
                    advisory=criterion.advisory,
                    detail=f"unknown check '{criterion.check}' — failing closed",
                )
            )
            continue
        try:
            result = fn(criterion, copy.deepcopy(contract), contract_path=contract_path)
        except Exception as exc:  # noqa: BLE001 — a crashing check is a failing check
            LOG.warning(
                "mission_check_crashed",
                extra={"check": criterion.check, "error": type(exc).__name__},
                exc_info=True,
            )
            # Typed name only — exception text never round-trips into the
            # scorecard (and, in PR 2, the LLM context). See PRs #28–#33.
            result = CheckResult(
                name=criterion.check,
                passed=False,
                advisory=criterion.advisory,
                detail=f"check raised {type(exc).__name__} — see server logs",
            )
        result.advisory = criterion.advisory
        scorecard.results.append(_redact_result(result))
    return scorecard


__all__ = [
    "CheckFn",
    "CheckResult",
    "MAX_DIAGNOSTICS",
    "MISSION_CHECKS",
    "MissionCheckError",
    "MissionScorecard",
    "get_mission_check",
    "load_contract_for_checks",
    "register_mission_check",
    "run_mission_checks",
]
