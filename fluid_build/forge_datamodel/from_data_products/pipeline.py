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

"""Phase 3 — author ADP / CDP products from existing upstream contracts.

This is the fourth pipeline alongside ``from_intent`` / ``from_catalog`` /
``from_ddl``. It answers the data-mesh authoring question that the other
three can't: *"compose this new product from these existing products"*.

The pipeline:

1. **Resolve** — turn a mix of product IDs, contract paths, and
   workspace globs into a concrete set of upstream contract files.
2. **Load** — parse each contract, extract the canonical productType,
   the exposed schema, and reachable expose IDs.
3. **Validate** — run :func:`fluid_build.forge.product_types.validate_composition`
   against the target type so an ADP can't take a CDP upstream.
4. **Project** — return a :class:`CompositionContext` with everything
   the LLM needs (schemas, exposeIds, names) so the seed prompt can
   build correct ``consumes[]`` references and join-key suggestions.

The pipeline is pure — the runtime feeds the ``CompositionContext``
to whichever authoring path the user chose. Every path funnels
through ``shape_contract`` with the same ``ProductTypeAnswer``
(I2 byte-equivalence invariant).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamProduct:
    """One resolved upstream product, ready to feed the LLM context."""

    id: str
    name: str
    product_type: str  # canonical SDP / ADP / CDP
    layer: str  # canonical Bronze / Silver / Gold
    domain: str
    contract_path: str  # absolute path on disk
    exposes: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    """Each entry: {exposeId, kind, schema: [{name, type, required}, ...]}."""


@dataclass(frozen=True)
class CompositionContext:
    """Validated composition context the runtime hands to the seed builder.

    ``violations`` is non-empty only when ``validate_composition`` rejected
    one of the upstream products. The runtime should refuse to proceed
    and surface the violations to the user instead of silently dropping
    them.
    """

    target_type: str  # SDP / ADP / CDP
    upstream_products: Tuple[UpstreamProduct, ...] = field(default_factory=tuple)
    violations: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        return not self.violations

    def to_consumes_block(self) -> List[Dict[str, str]]:
        """Project upstream products into the schema-correct consumes[] shape.

        Each upstream contributes one ``{productId, exposeId}`` row per
        expose it carries. The LLM can edit / dedupe later — the goal
        here is "valid YAML that compiles" so the seed contract can
        round-trip through validate_contract without manual fix-up.
        """
        rows: List[Dict[str, str]] = []
        for product in self.upstream_products:
            for ex in product.exposes:
                rows.append({"productId": product.id, "exposeId": ex.get("exposeId", "")})
        return rows

    def to_prompt_summary(self) -> Dict[str, Any]:
        """Compact, prompt-friendly projection for the LLM.

        Trimmed: schemas capped at 12 columns each, exposes capped at 5
        per product, total products capped at 10. Keeps token cost
        bounded for large workspaces.
        """
        out_products: List[Dict[str, Any]] = []
        for product in self.upstream_products[:10]:
            exposes = []
            for ex in product.exposes[:5]:
                cols = (ex.get("schema") or [])[:12]
                exposes.append(
                    {
                        "exposeId": ex.get("exposeId"),
                        "kind": ex.get("kind"),
                        "schema": cols,
                    }
                )
            out_products.append(
                {
                    "id": product.id,
                    "name": product.name,
                    "productType": product.product_type,
                    "layer": product.layer,
                    "domain": product.domain,
                    "exposes": exposes,
                }
            )
        return {
            "target_type": self.target_type,
            "upstream_products": out_products,
            "total": len(self.upstream_products),
        }


# ---------------------------------------------------------------------------
# Resolution: refs / paths / workspace globs → contract files
# ---------------------------------------------------------------------------


def _looks_like_path(ref: str) -> bool:
    """Heuristic: paths contain ``/`` or end with ``.yaml`` / ``.yml``."""
    return ("/" in ref) or ref.endswith((".yaml", ".yml"))


def resolve_upstream_paths(
    refs: Sequence[str],
    *,
    workspace_root: Optional[Path] = None,
    extra_search_paths: Sequence[Path] = (),
) -> List[Path]:
    """Resolve a mix of product IDs and paths into concrete contract files.

    Each *ref*:

    * If it ends with ``.yaml`` / ``.yml`` or contains ``/``, treated as a
      path: relative paths resolve under ``workspace_root``; absolute
      paths are taken as-is.
    * Otherwise treated as a product *id* and matched against
      ``contract.fluid.yaml`` files under ``workspace_root`` and any
      ``extra_search_paths`` (per ``--from-workspace`` flag).

    Missing refs are silently dropped — the caller surfaces them via
    ``CompositionContext.violations``.
    """
    import yaml as _yaml

    base = (workspace_root or Path.cwd()).resolve()
    search_roots = [base, *(Path(p).resolve() for p in extra_search_paths)]

    resolved: List[Path] = []
    seen: set = set()

    # Pre-build an index of (id -> path) by walking each search root once.
    id_index: Dict[str, Path] = {}
    for root in search_roots:
        if not root.is_dir():
            continue
        for candidate in root.rglob("contract.fluid.yaml"):
            try:
                doc = _yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                continue
            cid = doc.get("id")
            if cid and cid not in id_index:
                id_index[str(cid)] = candidate

    for ref in refs:
        ref = (ref or "").strip()
        if not ref:
            continue
        if _looks_like_path(ref):
            path = Path(ref)
            if not path.is_absolute():
                path = base / path
            path = path.resolve()
            if path.exists() and path not in seen:
                resolved.append(path)
                seen.add(path)
            continue
        # Treat as product id
        match = id_index.get(ref)
        if match and match not in seen:
            resolved.append(match)
            seen.add(match)

    return resolved


# ---------------------------------------------------------------------------
# Load: contract path → UpstreamProduct
# ---------------------------------------------------------------------------


def _project_exposes(exposes: Any) -> Tuple[Dict[str, Any], ...]:
    """Project the raw ``exposes`` list into a compact, prompt-friendly shape.

    Preserves the column's PII / sensitivity tag — read from
    ``sensitivity`` (the v0.7.3 schema field) with ``classification``
    accepted as an alias for back-compat — so downstream composition
    can propagate the tag onto matching column names in the new
    ADP/CDP.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(exposes, list):
        return ()
    for ex in exposes:
        if not isinstance(ex, dict):
            continue
        expose_id = ex.get("exposeId") or ex.get("id") or ""
        kind = ex.get("kind") or ex.get("type") or ""
        schema_columns = []
        contract_block = ex.get("contract") or {}
        for col in contract_block.get("schema") or []:
            if not isinstance(col, dict):
                continue
            entry: Dict[str, Any] = {
                "name": col.get("name", ""),
                "type": col.get("type", ""),
                "required": bool(col.get("required", False)),
            }
            # Accept ``sensitivity`` (schema-canonical) OR ``classification``
            # (catalog-style alias used by some authoring paths).
            tag = col.get("sensitivity") or col.get("classification")
            if tag:
                entry["sensitivity"] = tag
            schema_columns.append(entry)
        out.append(
            {
                "exposeId": expose_id,
                "kind": kind,
                "schema": schema_columns,
            }
        )
    return tuple(out)


def propagate_pii_classifications(
    new_contract: Dict[str, Any],
    upstream_products: Sequence["UpstreamProduct"],
) -> List[str]:
    """Stamp upstream PII / sensitivity tags onto matching downstream
    columns by name. Returns a list of human-readable propagation log
    messages (used for receipts / agent feedback).

    Reads from / writes to the schema-canonical ``sensitivity`` field.
    Also accepts upstream ``classification`` as an alias when the
    upstream contract used the catalog-style key.

    Match semantics: column name equality (case-sensitive). Composition
    pipelines that rename columns lose the tag — the operator must
    re-tag at the rename site. The rule stays predictable: if a
    downstream column is named ``email`` and an upstream column is
    also ``email`` tagged ``pii``, the downstream is tagged ``pii``.

    No-op when:

    * Downstream column already has an explicit ``sensitivity`` /
      ``classification`` (operator override wins; we don't downgrade).
    * No upstream tag exists for that name.
    * Either side's schema is missing.

    The downstream contract can still strip a propagated tag; it must
    do so deliberately by setting the column's ``sensitivity``.
    """
    log: List[str] = []
    upstream_tags: Dict[str, str] = {}
    for prod in upstream_products:
        for expose in prod.exposes:
            for col in expose.get("schema", []):
                name = col.get("name")
                cls = col.get("sensitivity") or col.get("classification")
                if name and cls and name not in upstream_tags:
                    upstream_tags[name] = cls

    if not upstream_tags:
        return log

    exposes = new_contract.get("exposes") or []
    if not isinstance(exposes, list):
        return log

    for ex_idx, expose in enumerate(exposes):
        if not isinstance(expose, dict):
            continue
        contract_block = expose.get("contract") or {}
        schema = contract_block.get("schema") or []
        if not isinstance(schema, list):
            continue
        for col in schema:
            if not isinstance(col, dict):
                continue
            name = col.get("name")
            if not name:
                continue
            if col.get("sensitivity") or col.get("classification"):
                continue  # operator override — don't overwrite
            tag = upstream_tags.get(name)
            if tag:
                col["sensitivity"] = tag
                log.append(
                    f"propagated sensitivity={tag!r} onto "
                    f"exposes[{ex_idx}].schema['{name}'] from upstream"
                )
    return log


def load_upstream_products(
    paths: Sequence[Path],
) -> Tuple[List[UpstreamProduct], List[str]]:
    """Parse each contract path; return (products, problems).

    *problems* carries human-readable strings the runtime can surface
    when a contract fails to parse or is missing required metadata.
    """
    import yaml as _yaml

    from fluid_build.forge.product_types import (
        LAYER_TO_PRODUCT_TYPE,
        PRODUCT_TYPE_TO_LAYER,
        get_product_type,
    )

    products: List[UpstreamProduct] = []
    problems: List[str] = []
    for path in paths:
        try:
            doc = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Failed to parse {path}: {exc}")
            continue
        meta = doc.get("metadata") or {}
        layer = meta.get("layer") or ""
        product_type = meta.get("productType") or ""
        if not product_type and layer:
            product_type = LAYER_TO_PRODUCT_TYPE.get(layer, "")
        if not layer and product_type:
            layer = PRODUCT_TYPE_TO_LAYER.get(product_type, "")
        normalized = get_product_type(product_type) if product_type else None
        canonical_pt = normalized.code if normalized else product_type
        canonical_layer = normalized.layer if normalized else layer

        products.append(
            UpstreamProduct(
                id=str(doc.get("id") or ""),
                name=str(doc.get("name") or ""),
                product_type=canonical_pt,
                layer=canonical_layer,
                domain=str(doc.get("domain") or ""),
                contract_path=str(path),
                exposes=_project_exposes(doc.get("exposes")),
            )
        )
    return products, problems


# ---------------------------------------------------------------------------
# End-to-end: refs → CompositionContext
# ---------------------------------------------------------------------------


def run_from_data_products(
    *,
    target_type: str,
    upstream_refs: Sequence[str],
    workspace_root: Optional[Path] = None,
    extra_search_paths: Sequence[Path] = (),
) -> CompositionContext:
    """Resolve + load + validate composition. Pure function.

    *target_type*: SDP / ADP / CDP (or layer / alias — resolved through
    the registry).

    Always returns a :class:`CompositionContext`; ``violations`` is
    populated when composition rules reject an upstream or any ref
    couldn't be resolved.
    """
    from fluid_build.forge.product_types import (
        get_product_type,
        validate_composition,
    )

    target = get_product_type(target_type)
    target_code = target.code if target else target_type

    paths = resolve_upstream_paths(
        upstream_refs,
        workspace_root=workspace_root,
        extra_search_paths=extra_search_paths,
    )
    # Compute which refs failed to resolve by re-running the same path
    # normalization the resolver used. A naive ``set(refs) - {str(p)}``
    # is wrong on macOS where ``/tmp`` resolves to ``/private/tmp`` —
    # the resolved path string never matches the input ref string.
    base = (workspace_root or Path.cwd()).resolve()
    resolved_path_set = {p.resolve() for p in paths}
    products, problems = load_upstream_products(paths)
    resolved_id_set = {p.id for p in products}

    def _ref_resolved(ref: str) -> bool:
        ref = (ref or "").strip()
        if not ref:
            return True  # blank refs are no-ops, not missing
        if _looks_like_path(ref):
            candidate = Path(ref)
            if not candidate.is_absolute():
                candidate = base / candidate
            try:
                return candidate.resolve() in resolved_path_set
            except OSError:
                return False
        # ID-style ref: check if any loaded product has that id.
        return ref in resolved_id_set

    missing = {ref for ref in upstream_refs if not _ref_resolved(ref)}

    upstream_types: Dict[str, Optional[str]] = {p.id: p.product_type or None for p in products}
    composition_violations = validate_composition(
        target_type=target_code, upstream_types=upstream_types
    )

    violations: List[str] = list(problems)
    for v in composition_violations:
        violations.append(f"{v.upstream_id} ({v.upstream_type or 'unknown'}): {v.reason}")
    if missing:
        for ref in sorted(missing):
            # Don't double-flag refs that already resolved to a path.
            if not any(p.id == ref for p in products):
                violations.append(f"Upstream ref {ref!r} could not be resolved to a contract.")

    return CompositionContext(
        target_type=target_code,
        upstream_products=tuple(products),
        violations=tuple(violations),
    )


__all__ = [
    "CompositionContext",
    "UpstreamProduct",
    "load_upstream_products",
    "resolve_upstream_paths",
    "run_from_data_products",
    "propagate_pii_classifications",
]
