# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Packaging-mode resolution — the single chokepoint for container ownership.

Implements the resolver half of ``RFC-packaging-modes.md`` (repo root): a
contract's ``packaging`` block declares, per infrastructure *container*
(bucket / database / dataset / schema / warehouse / cluster), whether this
product **owns** the container (``isolated`` → an OpenTofu resource) or
writes into a pre-existing, platform-owned pool (``shared`` → an OpenTofu
data source + leaf-only owned resources). The ownership vocabulary borrows
Terraform's resource-vs-data-source split and Unity Catalog's
``ISOLATED``/``OPEN`` isolation-mode enum.

Design rules (all load-bearing, see the RFC):

* **Pure function.** :func:`resolve_packaging` reads the contract mapping and
  nothing else — deterministic, digest-stable, no I/O, no heavy imports
  (cold-path safe).
* **LEGACY is a distinct sentinel, never conflated with OWNED.** A contract
  with *no* ``packaging`` block anywhere resolves to the module-level
  :data:`LEGACY` singleton (identity-checkable: ``resolution is LEGACY``),
  so the no-block path is a provable no-op — emitters keep today's exact
  output, pinned byte-for-byte by
  ``tests/iac/test_iac_packaging_default_pin.py``.
* **Single chokepoint.** Mirrors ``forge/product_types.py::
  normalize_metadata_in_place`` — every consumer (the provider plugins, from
  PR2 of the RFC) calls this; none reimplements the two-level precedence
  (``binding.packaging`` > top-level ``packaging`` > absent-LEGACY).
* **Typed errors.** Invalid combinations raise :class:`PackagingError` whose
  ``kind`` is a stable, greppable tag (the ``PlanBindingError.kind``
  discipline).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "CONTAINER_KINDS",
    "ContainerDecision",
    "ExposurePackaging",
    "LEGACY",
    "PackagingError",
    "PackagingResolution",
    "resolve_packaging",
]

# The six container kinds the schema's ``packaging.containers`` map accepts.
# Normative platform mapping (RFC §Contract-spec surface): bucket → S3/GCS;
# database → Snowflake database AND AWS Glue database; dataset → BigQuery;
# schema / warehouse → Snowflake; cluster → Confluent environment/cluster.
CONTAINER_KINDS: Tuple[str, ...] = (
    "bucket",
    "database",
    "dataset",
    "schema",
    "warehouse",
    "cluster",
)

_MODES: Tuple[str, ...] = ("isolated", "shared")
_BLOCK_KEYS = frozenset({"mode", "pool", "poolManifest", "containers"})


class PackagingError(ValueError):
    """Raised when a ``packaging`` block declares an invalid combination.

    ``kind`` carries one of these stable tags (each a distinct, greppable
    event for CI log parsers — same discipline as ``PlanBindingError``):

    - ``"invalid-block"`` — the block is not a mapping, or carries an
      unknown key.
    - ``"invalid-mode"`` — ``mode`` is not ``isolated`` / ``shared``.
    - ``"invalid-pool"`` — ``pool`` / ``poolManifest`` is not a non-empty
      string.
    - ``"invalid-containers"`` — ``containers`` is not a mapping.
    - ``"invalid-container-kind"`` — a ``containers`` key is not one of
      :data:`CONTAINER_KINDS`.
    - ``"invalid-container-mode"`` — a ``containers`` value is not
      ``isolated`` / ``shared``.
    - ``"pool-required"`` — some container resolved ``shared`` but no
      ``pool`` id is in scope (a pool must be addressable — RFC open
      question 1, resolved as *require pool*).
    - ``"cluster-isolated-unsupported"`` — ``containers.cluster: isolated``
      was declared; dedicated-cluster provisioning is v2 (RFC file 6).
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class ContainerDecision(Enum):
    """Per-container ownership decision.

    ``LEGACY`` is deliberately distinct from ``OWNED``: it means "no
    ``packaging`` block was declared — emit exactly today's output,
    including ``force_destroy`` and the fallback bucket", and is never a
    synonym for "implicit isolated with improvements".
    """

    LEGACY = "legacy"
    OWNED = "owned"
    REFERENCED = "referenced"


@dataclass(frozen=True)
class ExposurePackaging:
    """The resolved packaging for one ``exposes[]`` entry.

    ``declared`` is False only when neither the contract top level nor this
    exposure's binding carries a ``packaging`` block (every decision is then
    ``LEGACY``).
    """

    expose_id: Optional[str]
    declared: bool
    pool: Optional[str]
    pool_manifest: Optional[str]
    decisions: Mapping[str, ContainerDecision]


@dataclass(frozen=True)
class PackagingResolution:
    """The full packaging resolution for a contract.

    ``decisions`` is the contract-level default (top-level ``packaging``
    block, or all-``LEGACY`` when absent); ``exposures`` carries the
    per-exposure result after the ``binding.packaging`` override, in
    contract order. Every decisions mapping covers all six
    :data:`CONTAINER_KINDS`.
    """

    is_legacy: bool
    pool: Optional[str]
    pool_manifest: Optional[str]
    decisions: Mapping[str, ContainerDecision]
    exposures: Tuple[ExposurePackaging, ...]

    def exposure_for(self, expose_id: str) -> Optional[ExposurePackaging]:
        """The resolved exposure with this id, or ``None``."""
        for exposure in self.exposures:
            if exposure.expose_id == expose_id:
                return exposure
        return None

    def decision_for(self, kind: str, expose_id: Optional[str] = None) -> ContainerDecision:
        """The effective decision for a container kind.

        With ``expose_id`` the per-exposure result wins (two-level
        precedence); otherwise — or when the id is unknown — the
        contract-level default answers.
        """
        if kind not in CONTAINER_KINDS:
            raise PackagingError(
                "invalid-container-kind",
                f"unknown container kind {kind!r} — expected one of {', '.join(CONTAINER_KINDS)}",
            )
        if expose_id is not None:
            exposure = self.exposure_for(expose_id)
            if exposure is not None:
                return exposure.decisions[kind]
        return self.decisions[kind]


_LEGACY_DECISIONS: Mapping[str, ContainerDecision] = {
    kind: ContainerDecision.LEGACY for kind in CONTAINER_KINDS
}

#: The distinct LEGACY sentinel — returned (by identity) for every contract
#: that declares no ``packaging`` block anywhere. Emitters test
#: ``resolution is LEGACY`` and take today's exact emit path, byte-for-byte.
LEGACY = PackagingResolution(
    is_legacy=True,
    pool=None,
    pool_manifest=None,
    decisions=_LEGACY_DECISIONS,
    exposures=(),
)


@dataclass(frozen=True)
class _Block:
    """A validated (but not yet inherited/merged) ``packaging`` block."""

    mode: Optional[str]
    pool: Optional[str]
    pool_manifest: Optional[str]
    containers: Mapping[str, str]


def _parse_block(raw: Any, *, where: str) -> _Block:
    """Validate one raw ``packaging`` mapping → :class:`_Block` or raise."""
    if not isinstance(raw, Mapping):
        raise PackagingError(
            "invalid-block",
            f"{where} must be a mapping, got {type(raw).__name__}",
        )
    unknown = sorted(str(k) for k in raw.keys() if k not in _BLOCK_KEYS)
    if unknown:
        raise PackagingError(
            "invalid-block",
            f"{where} has unknown key(s) {', '.join(unknown)} — "
            f"expected only {', '.join(sorted(_BLOCK_KEYS))}",
        )

    mode = raw.get("mode")
    if mode is not None and mode not in _MODES:
        raise PackagingError(
            "invalid-mode",
            f"{where}.mode must be 'isolated' or 'shared', got {mode!r}",
        )

    pool = raw.get("pool")
    if pool is not None and (not isinstance(pool, str) or not pool.strip()):
        raise PackagingError(
            "invalid-pool",
            f"{where}.pool must be a non-empty string, got {pool!r}",
        )
    manifest = raw.get("poolManifest")
    if manifest is not None and (not isinstance(manifest, str) or not manifest.strip()):
        raise PackagingError(
            "invalid-pool",
            f"{where}.poolManifest must be a non-empty string, got {manifest!r}",
        )

    containers_raw = raw.get("containers")
    containers: Dict[str, str] = {}
    if containers_raw is not None:
        if not isinstance(containers_raw, Mapping):
            raise PackagingError(
                "invalid-containers",
                f"{where}.containers must be a mapping, got {type(containers_raw).__name__}",
            )
        for kind, value in containers_raw.items():
            if kind not in CONTAINER_KINDS:
                raise PackagingError(
                    "invalid-container-kind",
                    f"{where}.containers has unknown container kind {kind!r} — "
                    f"expected one of {', '.join(CONTAINER_KINDS)}",
                )
            if value not in _MODES:
                raise PackagingError(
                    "invalid-container-mode",
                    f"{where}.containers.{kind} must be 'isolated' or 'shared', got {value!r}",
                )
            containers[str(kind)] = str(value)

    return _Block(mode=mode, pool=pool, pool_manifest=manifest, containers=containers)


def _effective(
    block: _Block, *, base: Optional[_Block], where: str
) -> Tuple[Dict[str, ContainerDecision], Optional[str], Optional[str]]:
    """Fold one scope: ``block`` over ``base`` → (decisions, pool, manifest).

    Precedence is key-wise (``binding.packaging`` over top-level
    ``packaging``); the per-kind ``containers`` overrides win over the
    blanket ``mode``, whose default — when a block is present — is
    ``isolated`` (RFC: "default when block present: isolated").
    """
    mode = block.mode or (base.mode if base else None) or "isolated"
    pool = block.pool or (base.pool if base else None)
    manifest = block.pool_manifest or (base.pool_manifest if base else None)
    containers: Dict[str, str] = dict(base.containers) if base else {}
    containers.update(block.containers)

    # v1 accepts only `cluster: shared` — a dedicated Confluent cluster is
    # not provisionable yet, so an explicit isolated declaration fails fast
    # here rather than silently no-op'ing at emit time (RFC file 6).
    if containers.get("cluster") == "isolated":
        raise PackagingError(
            "cluster-isolated-unsupported",
            f"{where}: containers.cluster: isolated is not supported in v1 — "
            "dedicated-cluster provisioning is not yet available (use 'shared')",
        )

    decisions = {
        kind: (
            ContainerDecision.OWNED
            if containers.get(kind, mode) == "isolated"
            else ContainerDecision.REFERENCED
        )
        for kind in CONTAINER_KINDS
    }

    # A pool must be addressable whenever anything is shared (RFC open
    # question 1, resolved: require `pool`; `poolManifest` stays optional).
    if pool is None and any(d is ContainerDecision.REFERENCED for d in decisions.values()):
        raise PackagingError(
            "pool-required",
            f"{where}: at least one container resolved 'shared' but no `pool` id "
            "is declared — a shared container must name the platform-owned pool "
            "it writes into (add `pool: <id>`)",
        )

    return decisions, pool, manifest


def resolve_packaging(contract: Mapping[str, Any]) -> PackagingResolution:
    """Resolve a contract's packaging declaration — the single chokepoint.

    Returns the module-level :data:`LEGACY` sentinel (by identity) when no
    ``packaging`` block exists anywhere — neither at the contract top level
    nor on any ``exposes[].binding``. Otherwise returns a fully-folded
    :class:`PackagingResolution` (two-level precedence: ``binding.packaging``
    > top-level ``packaging``; ``containers`` overrides > ``mode``; mode
    defaults to ``isolated`` when a block is present).

    Raises :class:`PackagingError` (typed via ``.kind``) on any invalid
    shape or combination. Pure: no I/O, no mutation of ``contract``.
    """
    top_raw = contract.get("packaging") if isinstance(contract, Mapping) else None

    exposes = contract.get("exposes") if isinstance(contract, Mapping) else None
    if not isinstance(exposes, (list, tuple)):
        exposes = []
    per_expose_raw = []
    for expose in exposes:
        binding = expose.get("binding") if isinstance(expose, Mapping) else None
        raw = binding.get("packaging") if isinstance(binding, Mapping) else None
        per_expose_raw.append((expose, raw))

    if top_raw is None and all(raw is None for _, raw in per_expose_raw):
        return LEGACY

    top = _parse_block(top_raw, where="packaging") if top_raw is not None else None
    if top is not None:
        contract_decisions, pool, manifest = _effective(top, base=None, where="packaging")
    else:
        contract_decisions, pool, manifest = dict(_LEGACY_DECISIONS), None, None

    exposures = []
    for index, (expose, raw) in enumerate(per_expose_raw):
        expose_id = None
        if isinstance(expose, Mapping):
            candidate = expose.get("exposeId") or expose.get("id")
            if isinstance(candidate, str) and candidate:
                expose_id = candidate
        if raw is None:
            if top is not None:
                exposures.append(
                    ExposurePackaging(
                        expose_id=expose_id,
                        declared=True,
                        pool=pool,
                        pool_manifest=manifest,
                        decisions=contract_decisions,
                    )
                )
            else:
                exposures.append(
                    ExposurePackaging(
                        expose_id=expose_id,
                        declared=False,
                        pool=None,
                        pool_manifest=None,
                        decisions=_LEGACY_DECISIONS,
                    )
                )
            continue
        where = f"exposes[{index}].binding.packaging"
        block = _parse_block(raw, where=where)
        decisions, epool, emanifest = _effective(block, base=top, where=where)
        exposures.append(
            ExposurePackaging(
                expose_id=expose_id,
                declared=True,
                pool=epool,
                pool_manifest=emanifest,
                decisions=decisions,
            )
        )

    return PackagingResolution(
        is_legacy=False,
        pool=pool,
        pool_manifest=manifest,
        decisions=contract_decisions,
        exposures=tuple(exposures),
    )
