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

"""Apply enrichment artifacts back into the contract dict.

The post-synthesis enrichment hook
(:mod:`fluid_build.copilot.enrichment`) produces three artifact types:

* **dbt tests** — a ``schema.yml``-shaped dict (``version: 2`` +
  ``models``) per exposed dataset.
* **freshness** — a single ``{warn_after, error_after, filter}`` block.
* **physical_layout** — one
  ``{clustering_keys, partition_by, partition_grain,
  materialization_hint, provider_specific}`` per exposed dataset.

Those land under ``.fluid/agents/<run-id>/enrichment/`` and inform the
JudgeAgent but are not woven into the contract itself. This module
provides a *conservative* apply pass that fills missing slots in the
contract dict — never overwriting a user-set field. Gated behind the
``--apply-enrichment`` CLI flag.

Design borrowed from:

* Python stdlib ``difflib.unified_diff`` (full credit) for the diff
  preview; pyyaml ``safe_dump`` for canonical serialisation.
* The DomWeldon recursive-merge recipe
  (https://gist.github.com/angstwad/bf22d1822c38a92ec0a9) — adapted to
  the "fill missing, never overwrite" variant.
* Helm 3-way strategic merge — include only the fields you want to
  change; preserve all others.

No new pip deps: stdlib + the existing ``pyyaml`` dependency are
enough. The contract dict is mutated in-place on a deepcopy and
returned alongside a human-readable changelog so the caller can render
a confirmation panel.
"""

from __future__ import annotations

import copy
import difflib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

LOG = logging.getLogger("fluid.copilot.enrichment_apply")

__all__ = [
    "ENRICHMENT_MARKER_SOURCE",
    "apply_enrichment_to_contract",
    "has_enrichment_marker",
    "render_enrichment_diff",
]


ENRICHMENT_MARKER_SOURCE = "enrichment-v1"
"""Stamp value written under ``metadata.enrichmentApplied.source``.

Bumped only when the apply semantics change in a way that demands a
re-run on previously-enriched contracts.
"""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def apply_enrichment_to_contract(
    contract: Mapping[str, Any],
    artifacts: Optional[Mapping[str, Any]],
    *,
    run_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Apply enrichment artifacts to *contract* (conservative merge).

    Returns ``(patched_contract, change_log)``. The input contract is
    not mutated; a deepcopy is returned. The change-log is a list of
    plain-English strings — one entry per fill — suitable for the
    confirmation panel.

    Conservative invariant: **never overwrite a user-set field**. A
    slot is considered "user-set" when the key is present and carries
    a truthy / explicitly-empty value (an empty list/dict still counts
    as user-set so the apply pass cannot stomp an intentional clear).

    Idempotency: a re-apply with the same artifacts produces zero
    changes. The ``metadata.enrichmentApplied`` marker is checked
    upfront — if the source + artifacts_run_id already matches, we
    short-circuit and return ``(contract_copy, [])``.

    Parameters
    ----------
    contract
        The synthesised contract dict (FLUID schema 0.7.x shape).
    artifacts
        The dict returned by
        :func:`fluid_build.copilot.enrichment.enrich_contract`. ``None``
        or empty short-circuits — nothing is applied.
    run_id
        The forge run id (``YYYYMMDD-HHMMSS-<6hex>``). Stamped on the
        marker so re-applies are detectable.
    now
        Injectable clock for deterministic tests.
    """
    patched: Dict[str, Any] = copy.deepcopy(dict(contract or {}))
    changes: List[str] = []

    if not artifacts:
        return patched, changes

    # Idempotency short-circuit — same artifacts run_id already
    # applied? Skip without raising. The caller (--apply-enrichment)
    # should still render the (empty) diff so users see the no-op.
    existing_marker = _get_marker(patched)
    if (
        existing_marker
        and existing_marker.get("source") == ENRICHMENT_MARKER_SOURCE
        and run_id is not None
        and existing_marker.get("artifacts_run_id") == run_id
    ):
        return patched, changes

    # --- 1. dbt tests --------------------------------------------------
    # Two slots:
    #   * ``metadata.dbtTestSuggestions`` — full schema.yml-shaped dicts so
    #     downstream tools can apply directly.
    #   * ``qualityChecks`` (top-level) — compact
    #     ``{model: {column: [tests]}}`` shape for at-a-glance review.
    dbt_tests = artifacts.get("dbt_tests") or []
    if dbt_tests:
        _apply_dbt_tests(patched, dbt_tests, changes)

    # --- 2. freshness --------------------------------------------------
    freshness = artifacts.get("freshness") or {}
    if freshness:
        _apply_freshness(patched, freshness, changes)

    # --- 3. physical layout -------------------------------------------
    physical_layout = artifacts.get("physical_layout") or []
    if physical_layout:
        _apply_physical_layout(patched, physical_layout, changes)

    # --- Stamp marker (only if we actually changed something) ---------
    if changes:
        _stamp_marker(
            patched,
            run_id=run_id,
            now=now or datetime.now(timezone.utc),
        )
        changes.append("stamped metadata.enrichmentApplied marker")

    return patched, changes


def render_enrichment_diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    fromfile: str = "contract.before.yaml",
    tofile: str = "contract.after.yaml",
    context_lines: int = 3,
) -> str:
    """Return a unified-diff string between two contract dicts.

    Serialises both sides with ``yaml.safe_dump(sort_keys=False)`` to
    keep the diff aligned with how the contract is actually written
    to disk. 3 lines of context by default — matches ``diff -u`` /
    ``git diff`` defaults.

    Empty string when the dicts serialise identically.
    """
    before_yaml = yaml.safe_dump(dict(before or {}), sort_keys=False, default_flow_style=False)
    after_yaml = yaml.safe_dump(dict(after or {}), sort_keys=False, default_flow_style=False)
    if before_yaml == after_yaml:
        return ""
    diff = difflib.unified_diff(
        before_yaml.splitlines(keepends=True),
        after_yaml.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
        n=context_lines,
    )
    return "".join(diff)


def has_enrichment_marker(contract: Mapping[str, Any]) -> bool:
    """Return ``True`` when the contract already carries the marker."""
    return _get_marker(contract) is not None


# ---------------------------------------------------------------------------
# Per-artifact handlers (private)
# ---------------------------------------------------------------------------


def _apply_dbt_tests(
    contract: Dict[str, Any],
    dbt_tests: List[Mapping[str, Any]],
    changes: List[str],
) -> None:
    """Wire dbt test suggestions into ``metadata.dbtTestSuggestions``
    (full schema.yml shape) and ``qualityChecks`` (compact view).

    Both slots are NEW namespaces under FLUID — the schema doesn't
    forbid them and downstream tools will recognise the dbt-canonical
    shape. We never touch existing user-set values.
    """
    metadata = _ensure_dict(contract, "metadata")
    suggestions_existing = metadata.get("dbtTestSuggestions")
    quality_existing = contract.get("qualityChecks")

    # ``metadata.dbtTestSuggestions`` — store the raw schema.yml list
    # only if missing. Re-running ``enrichment_apply`` against the same
    # artifacts must not double up the list.
    if suggestions_existing is None:
        metadata["dbtTestSuggestions"] = [dict(t) for t in dbt_tests]
        changes.append(f"added metadata.dbtTestSuggestions ({len(dbt_tests)} schema.yml block(s))")

    # ``qualityChecks`` — compact ``{model: {column: [tests]}}`` rollup.
    # If the user already authored qualityChecks (even partial), we
    # merge per-model only into models the user did NOT declare. This
    # is the strictest non-stomping interpretation.
    compact_new = _compact_quality_checks_from_dbt(dbt_tests)
    if compact_new:
        if not isinstance(quality_existing, dict) or not quality_existing:
            # Slot is empty / missing — fill it entirely.
            contract["qualityChecks"] = compact_new
            for model in compact_new:
                changes.append(f"added qualityChecks[{model}] from dbt suggestions")
        else:
            # Per-model fill — only models the user didn't declare.
            for model, cols in compact_new.items():
                if model not in quality_existing:
                    quality_existing[model] = cols
                    changes.append(f"added qualityChecks[{model}] from dbt suggestions")


def _compact_quality_checks_from_dbt(
    dbt_tests: List[Mapping[str, Any]],
) -> Dict[str, Dict[str, List[str]]]:
    """Build the compact ``{model: {column: [test names]}}`` rollup.

    Test names are normalised to strings — dbt allows test entries to
    be either bare strings (``not_null``) or dicts
    (``{accepted_values: {values: [...]}}``). The dict-shaped tests
    are stringified as their single top-level key for the compact
    view; downstream tools should consume the full
    ``metadata.dbtTestSuggestions`` for the dict bodies.
    """
    compact: Dict[str, Dict[str, List[str]]] = {}
    for block in dbt_tests:
        if not isinstance(block, dict):
            continue
        for model in block.get("models") or []:
            if not isinstance(model, dict):
                continue
            mname = str(model.get("name") or "")
            if not mname:
                continue
            col_map: Dict[str, List[str]] = {}
            for col in model.get("columns") or []:
                if not isinstance(col, dict):
                    continue
                cname = str(col.get("name") or "")
                if not cname:
                    continue
                tests_raw = col.get("tests") or []
                tests_norm: List[str] = []
                for t in tests_raw:
                    if isinstance(t, str):
                        tests_norm.append(t)
                    elif isinstance(t, dict) and t:
                        # The single top-level key is the test name.
                        tests_norm.append(next(iter(t.keys())))
                if tests_norm:
                    col_map[cname] = tests_norm
            if col_map:
                compact[mname] = col_map
    return compact


def _apply_freshness(
    contract: Dict[str, Any],
    freshness: Mapping[str, Any],
    changes: List[str],
) -> None:
    """Fill ``exposes[0].contract.freshness`` when missing.

    FLUID has at most one refresh cadence per product (multi-source
    CDC is handled via source_type), so a single expose carries the
    canonical block. We pick the first expose with a ``contract`` sub-
    object — the slot the JSON schema actually defines.
    """
    exposes = contract.get("exposes") or []
    if not isinstance(exposes, list) or not exposes:
        return
    target = None
    for ex in exposes:
        if isinstance(ex, dict):
            target = ex
            break
    if target is None:
        return
    inner = _ensure_dict(target, "contract")
    if "freshness" in inner and inner["freshness"]:
        # User-set freshness — never overwrite.
        return
    inner["freshness"] = dict(freshness)
    expose_id = target.get("exposeId") or target.get("name") or "exposes[0]"
    changes.append(f"added freshness to exposes[{expose_id}]")


def _apply_physical_layout(
    contract: Dict[str, Any],
    physical_layout: List[Mapping[str, Any]],
    changes: List[str],
) -> None:
    """Fill ``exposes[i].binding.physical`` for each suggested expose.

    Matching by exposeId. The ``binding.physical`` sub-block is a NEW
    namespace under the existing ``binding`` map; the FLUID schema
    permits additional binding properties via the ``additionalProperties``
    relaxation on the binding object, and downstream IaC emitters
    already know how to walk ``binding.*``.
    """
    exposes = contract.get("exposes") or []
    if not isinstance(exposes, list) or not exposes:
        return

    # When there's exactly one expose and one layout suggestion, pair
    # them positionally so a model_name mismatch (e.g. snake_case vs
    # camelCase from the LLM) doesn't drop the fill.
    if len(exposes) == 1 and len(physical_layout) == 1:
        _fill_physical_on_expose(exposes[0], physical_layout[0], changes)
        return

    # Multi-expose: zip by model_name → exposeId. The enrichment tool
    # reads model_name from the expose dict, so this is the natural
    # join key. Fall back to positional pairing for unmatched layouts.
    by_id: Dict[str, Dict[str, Any]] = {}
    for ex in exposes:
        if not isinstance(ex, dict):
            continue
        ex_id = str(ex.get("exposeId") or ex.get("name") or ex.get("id") or "")
        if ex_id:
            by_id[ex_id] = ex

    for layout in physical_layout:
        if not isinstance(layout, Mapping):
            continue
        # The layout dict from suggest_physical_layout doesn't directly
        # carry model_name back, but extract_schemas_from_contract uses
        # exposeId as model_name and the enrichment hook calls the tool
        # once per schema — so the n-th layout aligns with the n-th
        # exposes entry. Try id-keyed first (cheaper to reason about),
        # fall back to positional.
        target = layout.get("model_name") and by_id.get(layout["model_name"])
        if target is None:
            idx = physical_layout.index(layout)
            if idx < len(exposes) and isinstance(exposes[idx], dict):
                target = exposes[idx]
        if target is None:
            continue
        _fill_physical_on_expose(target, layout, changes)


def _fill_physical_on_expose(
    expose: Dict[str, Any],
    layout: Mapping[str, Any],
    changes: List[str],
) -> None:
    """Fill ``binding.physical`` on a single expose, conservatively."""
    if not isinstance(expose, dict):
        return
    binding = _ensure_dict(expose, "binding")
    if "physical" in binding and binding["physical"]:
        return  # user-set — never overwrite

    # Only copy keys that are non-empty in the layout suggestion so we
    # don't sprinkle ``None``s into the contract.
    payload: Dict[str, Any] = {}
    for key in (
        "clustering_keys",
        "partition_by",
        "partition_grain",
        "materialization_hint",
        "provider_specific",
    ):
        value = layout.get(key)
        if value is None:
            continue
        # Empty list ⇒ skip (cosmetic clutter).
        if isinstance(value, list) and not value:
            continue
        # Empty dict / str ⇒ skip (provider_specific often empty).
        if isinstance(value, (dict, str)) and not value:
            continue
        payload[key] = copy.deepcopy(value)

    if not payload:
        return

    binding["physical"] = payload
    expose_id = expose.get("exposeId") or expose.get("name") or "exposes[?]"
    summary = []
    if payload.get("clustering_keys"):
        summary.append(f"clustering={','.join(payload['clustering_keys'])}")
    if payload.get("partition_by"):
        grain = payload.get("partition_grain", "")
        summary.append(f"partition={payload['partition_by']}{f'/{grain}' if grain else ''}")
    if payload.get("materialization_hint"):
        summary.append(f"materialization={payload['materialization_hint']}")
    detail = "; ".join(summary) if summary else "physical layout"
    changes.append(f"added binding.physical to exposes[{expose_id}] ({detail})")


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


def _stamp_marker(
    contract: Dict[str, Any],
    *,
    run_id: Optional[str],
    now: datetime,
) -> None:
    """Stamp ``metadata.enrichmentApplied`` so re-runs are idempotent."""
    metadata = _ensure_dict(contract, "metadata")
    metadata["enrichmentApplied"] = {
        "timestamp_utc": now.astimezone(timezone.utc).isoformat(),
        "source": ENRICHMENT_MARKER_SOURCE,
        "artifacts_run_id": run_id or "",
    }


def _get_marker(contract: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    metadata = contract.get("metadata") if isinstance(contract, Mapping) else None
    if not isinstance(metadata, Mapping):
        return None
    marker = metadata.get("enrichmentApplied")
    return dict(marker) if isinstance(marker, Mapping) else None


# ---------------------------------------------------------------------------
# Small dict helpers
# ---------------------------------------------------------------------------


def _ensure_dict(container: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return ``container[key]`` as a dict, creating it if missing.

    Replaces a non-dict value (e.g. ``None`` from a sparse contract)
    with a fresh dict. The caller wants a mutable nested map; any
    non-mapping there is treated as "missing" for our purposes.
    """
    existing = container.get(key)
    if not isinstance(existing, dict):
        existing = {}
        container[key] = existing
    return existing
