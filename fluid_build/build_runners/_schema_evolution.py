# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Schema-evolution decision engine.

Resolves a per-event action from ``schemaPolicy`` + per-change overrides
(``onAddedColumn``, ``onRemovedColumn``, ``onTypeChange``). Stricter wins.

Decision matrix (events × policies):

| Event              | strict | discover_and_freeze | evolve_safe        | evolve_all       |
|--------------------|--------|---------------------|--------------------|------------------|
| no change          | OK     | OK                  | OK                 | OK               |
| added column       | FAIL   | OK if first run else FAIL | INCLUDE          | INCLUDE          |
| removed column     | FAIL   | FAIL                | WARN+DROP          | DROP             |
| type widened       | FAIL   | FAIL                | OK                 | OK               |
| type narrowed      | FAIL   | FAIL                | FAIL               | CAST (failures→DLQ) |
| reordered          | OK     | OK                  | OK                 | OK               |
| renamed            | FAIL   | FAIL                | WARN+DROP+ADD      | INCLUDE          |
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from fluid_build.api.schema import (
    EvolutionAction,
    SchemaColumn,
    SchemaEvolutionDecision,
    SchemaPolicy,
)

# Type widening: numeric promotions and integer→string only when explicit.
_NUMERIC_ORDER = ("tinyint", "smallint", "int", "integer", "bigint", "float", "double", "decimal")


def _is_widening(old: str, new: str) -> bool:
    o, n = old.lower(), new.lower()
    if o == n:
        return False
    try:
        return _NUMERIC_ORDER.index(o) < _NUMERIC_ORDER.index(n)
    except ValueError:
        return False


def _is_narrowing(old: str, new: str) -> bool:
    o, n = old.lower(), new.lower()
    if o == n:
        return False
    try:
        return _NUMERIC_ORDER.index(o) > _NUMERIC_ORDER.index(n)
    except ValueError:
        return False


@dataclass
class EvolutionPlan:
    decisions: List[SchemaEvolutionDecision]
    has_failure: bool

    @property
    def must_fail(self) -> bool:
        return self.has_failure


def diff_columns(
    baseline: Sequence[SchemaColumn], current: Sequence[SchemaColumn]
) -> List[Tuple[str, str, Optional[SchemaColumn], Optional[SchemaColumn]]]:
    """Return tuples (event, column-name, baseline-col, current-col) describing
    the column-level differences.
    """
    by_baseline = {c.name: c for c in baseline}
    by_current = {c.name: c for c in current}
    events: List[Tuple[str, str, Optional[SchemaColumn], Optional[SchemaColumn]]] = []
    for name, cur in by_current.items():
        if name not in by_baseline:
            events.append(("added", name, None, cur))
            continue
        base = by_baseline[name]
        if base.type.lower() == cur.type.lower():
            continue
        if _is_widening(base.type, cur.type):
            events.append(("type_widened", name, base, cur))
        elif _is_narrowing(base.type, cur.type):
            events.append(("type_narrowed", name, base, cur))
        else:
            events.append(("type_changed", name, base, cur))
    for name, base in by_baseline.items():
        if name not in by_current:
            events.append(("removed", name, base, None))
    return events


# Override applies on top of a policy. None = use policy default.
def _override_for_event(event: str, overrides: Dict[str, str]) -> Optional[str]:
    if event == "added":
        return overrides.get("onAddedColumn")
    if event == "removed":
        return overrides.get("onRemovedColumn")
    if event in ("type_widened", "type_narrowed", "type_changed"):
        return overrides.get("onTypeChange")
    return None


# Strictness rank used to enforce "stricter wins".
_STRICTNESS = {
    "include": 0,
    "drop": 0,
    "cast": 0,
    "warn": 1,
    "fail": 2,
}


def _stricter(a: EvolutionAction, b: EvolutionAction) -> EvolutionAction:
    return a if _STRICTNESS.get(a.value, 0) >= _STRICTNESS.get(b.value, 0) else b


def _policy_default(policy: SchemaPolicy, event: str, is_first_run: bool) -> EvolutionAction:
    if event == "added":
        if policy is SchemaPolicy.STRICT:
            return EvolutionAction.FAIL
        if policy is SchemaPolicy.DISCOVER_AND_FREEZE:
            return EvolutionAction.INCLUDE if is_first_run else EvolutionAction.FAIL
        return EvolutionAction.INCLUDE  # evolve_safe / evolve_all
    if event == "removed":
        if policy in (SchemaPolicy.STRICT, SchemaPolicy.DISCOVER_AND_FREEZE):
            return EvolutionAction.FAIL
        if policy is SchemaPolicy.EVOLVE_SAFE:
            return EvolutionAction.WARN
        return EvolutionAction.DROP
    if event == "type_widened":
        if policy in (SchemaPolicy.STRICT, SchemaPolicy.DISCOVER_AND_FREEZE):
            return EvolutionAction.FAIL
        return EvolutionAction.OK
    if event == "type_narrowed":
        if policy is SchemaPolicy.EVOLVE_ALL:
            return EvolutionAction.CAST
        return EvolutionAction.FAIL
    if event == "type_changed":
        # Fallback: treat unknown type changes as narrowing.
        if policy is SchemaPolicy.EVOLVE_ALL:
            return EvolutionAction.CAST
        return EvolutionAction.FAIL
    return EvolutionAction.OK


def resolve(
    baseline: Sequence[SchemaColumn],
    current: Sequence[SchemaColumn],
    policy: SchemaPolicy,
    overrides: Optional[Dict[str, str]] = None,
    is_first_run: bool = False,
) -> EvolutionPlan:
    """Return per-event decisions according to policy and overrides."""
    overrides = overrides or {}
    decisions: List[SchemaEvolutionDecision] = []
    for event, name, _base, _cur in diff_columns(baseline, current):
        default = _policy_default(policy, event, is_first_run)
        override_str = _override_for_event(event, overrides)
        if override_str:
            try:
                override = EvolutionAction(override_str)
                action = _stricter(default, override)
            except ValueError:
                action = default
        else:
            action = default
        reason = f"policy={policy.value}, event={event}, override={override_str or 'none'}"
        decisions.append(
            SchemaEvolutionDecision(column=name, event=event, action=action, reason=reason)
        )
    has_failure = any(d.action is EvolutionAction.FAIL for d in decisions)
    return EvolutionPlan(decisions=decisions, has_failure=has_failure)


# ── Strict-policy enforcement helper ────────────────────────────────────


def raise_if_strict_drift(
    plan: "EvolutionPlan",
    *,
    baseline_digest: str,
    current_digest: str,
) -> None:
    """Raise ``SchemaDriftError`` when the plan contains FAIL decisions.

    Callers that operate under ``schemaPolicy=strict`` (or hit a per-event
    override that returns FAIL) invoke this to convert the structured
    decision list into the typed catalog error so the user sees the rich
    five-field Panel rather than a flat run-record string.
    """
    if not plan.has_failure:
        return
    summary = "; ".join(
        f"{d.column}/{d.event}→{d.action.value}"
        for d in plan.decisions
        if d.action is EvolutionAction.FAIL
    )
    from fluid_build._errors import SchemaDriftError

    raise SchemaDriftError.for_diff(
        baseline_digest=baseline_digest,
        current_digest=current_digest,
        summary=summary or "schema policy rejects pending changes",
    )
