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

Every write target is a slot the FLUID JSON schema actually declares —
the bundled schemas (0.7.1 → 0.7.5) set ``additionalProperties: false``
on the contract root, ``metadata``, ``exposeContract`` and ``binding``
objects, so the pass may only use declared properties or the two
explicitly-open extension points (root ``extensions`` and
``binding.properties``). Slot map:

* dbt tests → ``extensions.enrichment.dbtTestSuggestions`` (full
  schema.yml shape) + ``extensions.enrichment.qualityChecks`` (compact
  rollup).
* freshness → ``exposes[0].qos.freshnessSLO`` (ISO-8601 duration from
  ``warn_after``) + a ``type: freshness`` dq rule under
  ``exposes[0].contract.dq.rules`` (``window`` from ``error_after``).
* physical_layout → ``exposes[i].binding.properties.physical``.
* apply marker → ``extensions.enrichment.applied``.

Contracts enriched by pre-v2 versions of this module carry these
payloads in schema-invalid slots (``metadata.enrichmentApplied``,
``metadata.dbtTestSuggestions``, top-level ``qualityChecks``,
``exposes[].contract.freshness``, ``exposes[].binding.physical``); a
re-apply migrates them into the slots above.

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


ENRICHMENT_MARKER_SOURCE = "enrichment-v2"
"""Stamp value written under ``extensions.enrichment.applied.source``.

Bumped only when the apply semantics change in a way that demands a
re-run on previously-enriched contracts. v2 moved every write target
to a schema-valid slot (the v1 targets were rejected by the schemas'
``additionalProperties: false``), so v1 markers intentionally never
short-circuit — a re-apply performs the legacy-slot migration.
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
    changes. The ``extensions.enrichment.applied`` marker is checked
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

    # --- 0. legacy-slot migration --------------------------------------
    # Contracts enriched by pre-v2 versions of this module carry data in
    # schema-invalid slots; relocate them first so the conservative
    # "user-set" checks below see the migrated values.
    _migrate_legacy_slots(patched, changes)

    # --- 1. dbt tests --------------------------------------------------
    # Two slots, both under the ``extensions.enrichment`` namespace:
    #   * ``dbtTestSuggestions`` — full schema.yml-shaped dicts so
    #     downstream tools can apply directly.
    #   * ``qualityChecks`` — compact ``{model: {column: [tests]}}``
    #     shape for at-a-glance review.
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
        changes.append("stamped extensions.enrichment.applied marker")

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
    """Wire dbt test suggestions into ``extensions.enrichment``.

    Two slots — ``dbtTestSuggestions`` (full schema.yml shape) and
    ``qualityChecks`` (compact view) — both under the enrichment plugin
    namespace: the root ``extensions`` object is the schema's designated
    free-form extension point, whereas ``metadata`` and the contract
    root are ``additionalProperties: false`` (the pre-v2 targets there
    never passed validation). We never touch existing user-set values.
    """
    ext = _enrichment_ext(contract)
    suggestions_existing = ext.get("dbtTestSuggestions")
    quality_existing = ext.get("qualityChecks")

    # ``dbtTestSuggestions`` — store the raw schema.yml list only if
    # missing. Re-running ``enrichment_apply`` against the same
    # artifacts must not double up the list.
    if suggestions_existing is None:
        ext["dbtTestSuggestions"] = [dict(t) for t in dbt_tests]
        changes.append(
            f"added extensions.enrichment.dbtTestSuggestions "
            f"({len(dbt_tests)} schema.yml block(s))"
        )

    # ``qualityChecks`` — compact ``{model: {column: [tests]}}`` rollup.
    # If the user already authored qualityChecks (even partial), we
    # merge per-model only into models the user did NOT declare. This
    # is the strictest non-stomping interpretation.
    compact_new = _compact_quality_checks_from_dbt(dbt_tests)
    if compact_new:
        if not isinstance(quality_existing, dict) or not quality_existing:
            # Slot is empty / missing — fill it entirely.
            ext["qualityChecks"] = compact_new
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
    ``extensions.enrichment.dbtTestSuggestions`` for the dict bodies.
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
    """Fill the schema-declared freshness slots on the first expose.

    FLUID has at most one refresh cadence per product (multi-source
    CDC is handled via source_type), so a single expose carries the
    canonical freshness declaration. The dbt-shaped artifact
    (``{warn_after, error_after, filter}``) maps onto the two slots the
    JSON schema actually defines — ``exposeContract`` is
    ``additionalProperties: false``, so the pre-v2
    ``contract.freshness`` target never passed validation:

    * ``qos.freshnessSLO`` (ISO-8601 duration) ← ``warn_after`` — the
      producer's freshness promise.
    * a ``type: freshness`` rule under ``contract.dq.rules`` with
      ``window`` ← ``error_after`` at ``severity: error`` — the
      enforceable breach threshold, consumed by the quality engine and
      the dbt-tests exporter. This is also the pairing the policy
      engine expects (``freshnessSLO`` without a freshness dq rule
      draws a policy warning).
    """
    exposes = contract.get("exposes") or []
    if not isinstance(exposes, list):
        return
    target = next((ex for ex in exposes if isinstance(ex, dict)), None)
    if target is not None:
        _fill_freshness_on_expose(target, freshness, changes)


def _fill_freshness_on_expose(
    expose: Dict[str, Any],
    freshness: Mapping[str, Any],
    changes: List[str],
) -> None:
    """Fill ``qos.freshnessSLO`` + a freshness dq rule, conservatively.

    Each half is filled independently and only when missing: a
    ``freshnessSLO`` key already present or an existing ``type:
    freshness`` dq rule is user-set and never overwritten.

    The dq rule is only emitted when the expose schema has a timestamp
    column to check (``selector``): the quality engine fails any rule
    that lacks a selector, so a selector-less machine rule would inject
    a spurious failure into live quality runs. Without a detectable
    column only the SLO half is written (the policy engine's
    "freshnessSLO without a freshness rule" warning then nudges the
    user to author one against the right column).
    """
    expose_id = expose.get("exposeId") or expose.get("name") or "exposes[0]"

    warn_iso = _freshness_unit_to_iso(freshness.get("warn_after"))
    error_iso = _freshness_unit_to_iso(freshness.get("error_after"))

    if warn_iso:
        qos = _ensure_dict(expose, "qos")
        if "freshnessSLO" not in qos:
            qos["freshnessSLO"] = warn_iso
            changes.append(f"added qos.freshnessSLO {warn_iso} to exposes[{expose_id}]")

    selector = _freshness_selector_column(expose) if error_iso else None
    if error_iso and selector:
        inner = _ensure_dict(expose, "contract")
        dq = _ensure_dict(inner, "dq")
        rules = dq.get("rules")
        if rules is None:
            rules = []
            dq["rules"] = rules
        if isinstance(rules, list) and not any(
            isinstance(r, Mapping) and r.get("type") == "freshness" for r in rules
        ):
            rule: Dict[str, Any] = {
                "id": "freshness-slo",
                "type": "freshness",
                "selector": selector,
                "severity": "error",
                "window": error_iso,
                "description": f"Data older than {error_iso} breaches the freshness SLO.",
            }
            filter_expr = freshness.get("filter")
            if filter_expr:
                rule["description"] += f" Row filter: {filter_expr}."
            rules.append(rule)
            changes.append(f"added freshness dq rule (window {error_iso}) to exposes[{expose_id}]")


# Ordered by how strongly the name signals "row last touched" — a
# load/update stamp beats a creation stamp for freshness checks.
_FRESHNESS_COLUMN_HINTS = (
    "updated_at",
    "last_updated_at",
    "last_updated",
    "loaded_at",
    "_loaded_at",
    "ingested_at",
    "_ingested_at",
    "modified_at",
    "event_ts",
    "event_time",
    "created_at",
)

_TEMPORAL_TYPE_TOKENS = ("timestamp", "datetime", "date")


def _freshness_selector_column(expose: Mapping[str, Any]) -> Optional[str]:
    """Pick the timestamp column a freshness dq rule should check.

    Only temporally-typed columns qualify (a varchar ``updated_at``
    would break the generated SQL check); among those, well-known
    load/update stamp names win, else the first temporal column.
    Returns ``None`` when the expose schema has no usable column.
    """
    inner = expose.get("contract")
    schema = inner.get("schema") if isinstance(inner, Mapping) else None
    if not isinstance(schema, list):
        return None
    temporal: List[str] = []
    for col in schema:
        if not isinstance(col, Mapping):
            continue
        name = str(col.get("name") or "")
        col_type = str(col.get("type") or "").lower()
        if name and any(token in col_type for token in _TEMPORAL_TYPE_TOKENS):
            temporal.append(name)
    by_lower = {name.lower(): name for name in temporal}
    for hint in _FRESHNESS_COLUMN_HINTS:
        if hint in by_lower:
            return by_lower[hint]
    return temporal[0] if temporal else None


_ISO_PARTS_BY_PERIOD = {"minute": ("PT", "M"), "hour": ("PT", "H"), "day": ("P", "D")}


def _freshness_unit_to_iso(unit: Any) -> Optional[str]:
    """Convert a dbt ``{count, period}`` unit into an ISO-8601 duration.

    ``{"count": 2, "period": "hour"}`` → ``"PT2H"``. Unknown periods or
    non-positive counts return ``None`` and the caller skips that half.
    """
    if not isinstance(unit, Mapping):
        return None
    count = unit.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return None
    parts = _ISO_PARTS_BY_PERIOD.get(str(unit.get("period") or "").lower())
    if parts is None:
        return None
    prefix, suffix = parts
    return f"{prefix}{count}{suffix}"


def _apply_physical_layout(
    contract: Dict[str, Any],
    physical_layout: List[Mapping[str, Any]],
    changes: List[str],
) -> None:
    """Fill ``exposes[i].binding.properties.physical`` for each suggested expose.

    Matching by exposeId. The payload lives under ``binding.properties``
    — the schema's provider-specific bag (``additionalProperties: true``).
    The ``binding`` object itself is ``additionalProperties: false``, so
    the pre-v2 ``binding.physical`` target never passed validation.
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
    """Fill ``binding.properties.physical`` on a single expose, conservatively."""
    if not isinstance(expose, dict):
        return
    existing_binding = expose.get("binding")
    if isinstance(existing_binding, dict):
        existing_props = existing_binding.get("properties")
        if isinstance(existing_props, dict) and "physical" in existing_props:
            return  # user-set (even an intentional clear) — never overwrite

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

    binding = _ensure_dict(expose, "binding")
    properties = _ensure_dict(binding, "properties")
    properties["physical"] = payload
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
    changes.append(f"added binding.properties.physical to exposes[{expose_id}] ({detail})")


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


def _stamp_marker(
    contract: Dict[str, Any],
    *,
    run_id: Optional[str],
    now: datetime,
) -> None:
    """Stamp ``extensions.enrichment.applied`` so re-runs are idempotent."""
    ext = _enrichment_ext(contract)
    ext["applied"] = {
        "timestamp_utc": now.astimezone(timezone.utc).isoformat(),
        "source": ENRICHMENT_MARKER_SOURCE,
        "artifacts_run_id": run_id or "",
    }


def _get_marker(contract: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(contract, Mapping):
        return None
    extensions = contract.get("extensions")
    if isinstance(extensions, Mapping):
        enrichment = extensions.get("enrichment")
        if isinstance(enrichment, Mapping):
            marker = enrichment.get("applied")
            if isinstance(marker, Mapping):
                return dict(marker)
    # Legacy (pre-v2) location — kept readable so
    # ``has_enrichment_marker`` still recognises old contracts.
    metadata = contract.get("metadata")
    if isinstance(metadata, Mapping):
        marker = metadata.get("enrichmentApplied")
        if isinstance(marker, Mapping):
            return dict(marker)
    return None


# ---------------------------------------------------------------------------
# Legacy-slot migration (pre-v2 → schema-valid slots)
# ---------------------------------------------------------------------------


def _migrate_legacy_slots(contract: Dict[str, Any], changes: List[str]) -> None:
    """Relocate machine-written slots from their pre-v2 (schema-invalid) homes.

    Versions of this module up to ``enrichment-v1`` wrote five slots that
    every bundled schema rejects via ``additionalProperties: false``:
    ``metadata.enrichmentApplied``, ``metadata.dbtTestSuggestions``,
    top-level ``qualityChecks``, ``exposes[].contract.freshness`` and
    ``exposes[].binding.physical``. All five were written exclusively by
    this module, so moving them is safe — a re-run of
    ``--apply-enrichment`` on a previously-enriched contract heals it
    into a schema-valid shape. Non-mapping values in these slots are
    left untouched (whatever put them there, it wasn't us).
    """
    metadata = contract.get("metadata")
    if isinstance(metadata, dict):
        legacy_marker = metadata.get("enrichmentApplied")
        if isinstance(legacy_marker, dict):
            _enrichment_ext(contract).setdefault("applied", dict(legacy_marker))
            del metadata["enrichmentApplied"]
            changes.append(
                "relocated legacy metadata.enrichmentApplied → extensions.enrichment.applied"
            )
        legacy_suggestions = metadata.get("dbtTestSuggestions")
        if isinstance(legacy_suggestions, list):
            _enrichment_ext(contract).setdefault("dbtTestSuggestions", legacy_suggestions)
            del metadata["dbtTestSuggestions"]
            changes.append(
                "relocated legacy metadata.dbtTestSuggestions → "
                "extensions.enrichment.dbtTestSuggestions"
            )

    legacy_quality = contract.get("qualityChecks")
    if isinstance(legacy_quality, dict):
        _enrichment_ext(contract).setdefault("qualityChecks", legacy_quality)
        del contract["qualityChecks"]
        changes.append("relocated legacy qualityChecks → extensions.enrichment.qualityChecks")

    exposes = contract.get("exposes")
    if not isinstance(exposes, list):
        return
    for ex in exposes:
        if not isinstance(ex, dict):
            continue
        expose_id = ex.get("exposeId") or ex.get("name") or "exposes[?]"
        inner = ex.get("contract")
        if isinstance(inner, dict) and isinstance(inner.get("freshness"), Mapping):
            legacy_freshness = inner["freshness"]
            # Convert rather than copy: the dbt-shaped block has no
            # schema-valid home, so it becomes qos.freshnessSLO + a dq
            # rule (each half fills only if not already set). Migrate
            # only blocks the new slots can represent losslessly —
            # hand-edited variants (unknown keys, week/month periods, a
            # row filter) stay put for the user to resolve.
            if _is_migratable_freshness(legacy_freshness):
                del inner["freshness"]
                _fill_freshness_on_expose(ex, legacy_freshness, changes)
                changes.append(f"removed legacy contract.freshness from exposes[{expose_id}]")
        binding = ex.get("binding")
        if isinstance(binding, dict) and isinstance(binding.get("physical"), dict):
            legacy_physical = binding.pop("physical")
            _ensure_dict(binding, "properties").setdefault("physical", legacy_physical)
            changes.append(
                f"relocated legacy binding.physical → binding.properties.physical "
                f"on exposes[{expose_id}]"
            )


# ---------------------------------------------------------------------------
# Small dict helpers
# ---------------------------------------------------------------------------


def _is_migratable_freshness(block: Mapping[str, Any]) -> bool:
    """True when a legacy freshness block matches the machine-written shape.

    The v1 emitter (:mod:`fluid_build.copilot.tools.freshness_emitter`)
    only ever produced ``{warn_after, error_after, filter}`` with
    minute/hour/day units and ``filter: None``. Anything else was
    hand-edited after the fact and cannot be represented losslessly in
    the new slots, so migration leaves it alone.
    """
    if any(key not in ("warn_after", "error_after", "filter") for key in block):
        return False
    if block.get("filter"):
        return False
    thresholds = [block.get(key) for key in ("warn_after", "error_after")]
    if not any(isinstance(t, Mapping) for t in thresholds):
        return False
    return all(
        t is None or (isinstance(t, Mapping) and _freshness_unit_to_iso(t) is not None)
        for t in thresholds
    )


def _enrichment_ext(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ``extensions.enrichment`` namespace, creating it if missing.

    The root ``extensions`` object is the schema's designated free-form
    extension point ("each plugin claims a single sub-key"); this module
    claims ``enrichment``.
    """
    return _ensure_dict(_ensure_dict(contract, "extensions"), "enrichment")


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
