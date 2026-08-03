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

"""Planner truthfulness for packaging modes (RFC-packaging-modes.md file 8).

``plan.json`` is the digest-bound artifact a human reviews and approves.
Under ``packaging.mode: shared`` the emit path correctly refuses to own
the pool container — but the *plan* still listed a create action for it,
so a reviewed, cryptographically-bound plan could claim it would create a
pool it must never own. The tofu gate kept that safe; the review contract
did not survive it.

Two changes, both applied at a single chokepoint in ``cli/plan.py`` so
they cover every planner path rather than three provider modules:

1. **Drop container-*creation* actions for REFERENCED containers.** The
   RFC's open question 2 resolved in favour of dropping over
   annotate-in-place: "a plan that lists creations that won't happen is
   worse than a changed count". The dropped actions are not silently
   vanished — they are itemised under ``packaging.droppedActions`` so a CI
   parser keying on action counts can see exactly what left and why.

2. **Stamp a ``packaging`` summary block.** Effective ownership per
   container kind, contract-wide and per exposure, so an approver reads
   ownership off the plan instead of recomputing the two-level precedence
   from the contract.

**Scope, honestly.** Only the *granular* native planner ops name a single
container (``s3.ensure_bucket``, ``bq.ensure_dataset``, …) and are
therefore droppable. The v0.7.x abstract ``provisionDataset`` action —
what ``ProviderActionParser`` emits, and what every modern contract
actually plans — is per-*exposure*: it provisions the container and its
tables together, so dropping it would gut the plan rather than make it
truthful. For that path the summary block is the truthfulness mechanism,
which is exactly the role open question 2 assigns it.

Actions keep BOTH ``op`` and ``action_type``: this module only ever
removes whole actions, never rewrites one, so the invariant that the
apply dispatcher reads ``op`` while display/viz reads ``action_type``
(CLAUDE.md) is preserved by construction.

Pure — no I/O, no ``cli`` imports, deterministic, so the plan stays
digest-stable.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .packaging import LEGACY, ContainerDecision, PackagingError, resolve_packaging

__all__ = [
    "CONTAINER_CREATION_OPS",
    "apply_packaging_to_plan",
    "build_packaging_summary",
    "filter_referenced_container_actions",
]

#: Native-planner op → RFC container kind, for the ops that create exactly
#: one container. Anything absent here is either a leaf resource (tables,
#: grants, tasks) or a composite action, and is never dropped.
#:
#: Kept deliberately narrow: a false entry here would silently remove a
#: real create action from an approved plan.
CONTAINER_CREATION_OPS: Mapping[str, str] = {
    # AWS — providers/aws/plan/planner.py
    "s3.ensure_bucket": "bucket",
    "glue.ensure_database": "database",
    # GCP — providers/gcp/plan/planner.py
    "gcs.ensure_bucket": "bucket",
    "bq.ensure_dataset": "dataset",
    # Snowflake — providers/snowflake/plan/planner.py
    "sf.database.ensure": "database",
    "sf.schema.ensure": "schema",
    "sf.warehouse.ensure": "warehouse",
}


def _action_id(action: Mapping[str, Any]) -> Optional[str]:
    """The action's identifier under either planner path's key."""
    for key in ("action_id", "id"):
        value = action.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def build_packaging_summary(contract: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The reviewer-facing ownership summary, or ``None`` for LEGACY.

    ``None`` (not an empty dict) for a contract with no ``packaging``
    block, so ``plan.json`` gains no new key at all and existing plans
    keep their exact shape — the same compatibility discipline as the
    emit path's LEGACY branch.
    """
    resolution = resolve_packaging(contract)
    if resolution is LEGACY:
        return None

    summary: Dict[str, Any] = {
        "pool": resolution.pool,
        "containers": {
            kind: decision.value for kind, decision in sorted(resolution.decisions.items())
        },
        # Which of those decisions are observable at all. ``containers`` is
        # total by design — providers index it by their own kinds — so on a
        # Snowflake-only contract it carries `bucket: owned`, `dataset: owned`
        # and `cluster: owned` for containers Snowflake has no notion of.
        # Reporting that verbatim announced ownership of infrastructure the
        # plan will never touch; every reporter must intersect with this list
        # first. Additive: ``containers`` keeps its exact shape and values.
        #
        # Contract-wide (the union over every bound platform), and it governs
        # the per-exposure ``containers`` blocks below too. A union can only
        # ever be a superset of what one exposure maps, so applying it to an
        # exposure fails in the safe direction — it never hides a container
        # that exposure really owns.
        "applicableContainers": sorted(resolution.applicable_kinds),
    }
    if resolution.pool_manifest:
        summary["poolManifest"] = resolution.pool_manifest

    exposures: List[Dict[str, Any]] = []
    for exposure in resolution.exposures:
        if not exposure.declared:
            continue
        exposures.append(
            {
                "exposeId": exposure.expose_id,
                "pool": exposure.pool,
                "containers": {
                    kind: decision.value for kind, decision in sorted(exposure.decisions.items())
                },
            }
        )
    if exposures:
        summary["exposures"] = exposures
    return summary


def filter_referenced_container_actions(
    contract: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]
) -> Tuple[List[Mapping[str, Any]], List[Dict[str, Any]]]:
    """Split ``actions`` into (kept, dropped-records).

    An action is dropped when its ``op`` names a single container kind
    (:data:`CONTAINER_CREATION_OPS`) that resolves to
    :data:`~fluid_build.iac.packaging.ContainerDecision.REFERENCED`.

    The decision is read at contract scope. A native planner action does
    not carry the exposure that produced it, so a per-exposure override
    cannot be attributed to one action; the contract-level decision is the
    only sound reading, and a hybrid contract keeps the action (the emit
    path, which *does* know the exposure, is authoritative either way).
    """
    try:
        resolution = resolve_packaging(contract)
    except PackagingError:
        # The plan gate resolves the same block moments later and reports
        # it with a message naming the real culprit — never drop actions
        # on the strength of a block we could not parse.
        return list(actions), []
    if resolution is LEGACY:
        return list(actions), []

    kept: List[Mapping[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for action in actions or ():
        if not isinstance(action, Mapping):
            kept.append(action)
            continue
        kind = CONTAINER_CREATION_OPS.get(str(action.get("op") or ""))
        if kind is not None and resolution.decisions[kind] is ContainerDecision.REFERENCED:
            record = {
                "op": action.get("op"),
                "container": kind,
                "reason": "referenced",
            }
            action_id = _action_id(action)
            if action_id:
                record["actionId"] = action_id
            dropped.append(record)
            continue
        kept.append(action)
    return kept, dropped


def _renumber(actions: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Close the holes a drop leaves in a ``step``-numbered action list.

    Only touches actions that already carry ``step`` (the abstract-op
    path); the native planner's actions have no step and are returned
    untouched.
    """
    out: List[Mapping[str, Any]] = []
    step = 0
    for action in actions:
        if isinstance(action, Mapping) and "step" in action:
            step += 1
            renumbered = dict(action)
            renumbered["step"] = step
            out.append(renumbered)
        else:
            out.append(action)
    return out


def _prune_dependencies(
    actions: Sequence[Mapping[str, Any]], removed_ids: Sequence[str]
) -> List[Mapping[str, Any]]:
    """Drop references to removed actions from surviving ``depends_on`` lists."""
    if not removed_ids:
        return list(actions)
    gone = set(removed_ids)
    out: List[Mapping[str, Any]] = []
    for action in actions:
        depends = action.get("depends_on") if isinstance(action, Mapping) else None
        if isinstance(depends, (list, tuple)) and any(d in gone for d in depends):
            pruned = dict(action)
            pruned["depends_on"] = [d for d in depends if d not in gone]
            out.append(pruned)
        else:
            out.append(action)
    return out


def apply_packaging_to_plan(plan: Dict[str, Any], contract: Mapping[str, Any]) -> Dict[str, Any]:
    """Make ``plan`` tell the truth about container ownership.

    Returns ``plan`` unmodified for a contract with no ``packaging``
    block — same object, no new keys — so every existing plan's shape and
    digest are untouched.

    Otherwise the plan gains a ``packaging`` summary and loses any
    container-creation action for a REFERENCED container. Must run BEFORE
    ``inject_digests`` so ``planDigest`` covers both.
    """
    summary = build_packaging_summary(contract)
    if summary is None:
        return plan

    actions = plan.get("actions") or []
    kept, dropped = filter_referenced_container_actions(contract, actions)
    if dropped:
        summary["droppedActions"] = dropped
        removed_ids = [record["actionId"] for record in dropped if "actionId" in record]
        kept = _renumber(_prune_dependencies(kept, removed_ids))
        plan["actions"] = kept
        plan["total_actions"] = len(kept)
        if "has_dependencies" in plan:
            plan["has_dependencies"] = any(
                a.get("depends_on") for a in kept if isinstance(a, Mapping)
            )
        if "dependency_graph" in plan:
            ids = [_action_id(a) for a in kept if isinstance(a, Mapping)]
            ids = [i for i in ids if i]
            plan["dependency_graph"] = {
                "nodes": ids,
                "edges": [
                    (_action_id(a), dep)
                    for a in kept
                    if isinstance(a, Mapping)
                    for dep in (a.get("depends_on") or [])
                ],
            }

    plan["packaging"] = summary
    return plan
