# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Catalog auto-registration orchestrator.

Reads ``properties.catalog.register`` from the contract and dispatches each
target to its registrar. Registrars conform to ``api.catalog.CatalogRegistrar``;
the built-in adapters wrap the existing providers under
``fluid_build/providers/catalogs/`` where possible.

If a target's registrar is missing or fails, the orchestrator records the
failure but does not abort the run — catalog registration is observability,
not correctness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_publication import CatalogPublicationPayload

LOG = logging.getLogger("fluid.acquire.catalog")

# Registry of available registrars. Populated lazily so that missing optional
# extras (e.g., DataHub SDK) don't fail import.
_REGISTRY: Dict[str, CatalogRegistrar] = {}


def register_registrar(target: str, registrar: CatalogRegistrar) -> None:
    _REGISTRY[target] = registrar


def get_registrar(target: str) -> Optional[CatalogRegistrar]:
    return _REGISTRY.get(target)


def build_registrar(
    target: str, config: Optional[Dict[str, Any]] = None
) -> Optional[CatalogRegistrar]:
    """Build a registrar for *target* on demand.

    Resolution order (Surface B — contract ``register: [...]``):

    1. **Plug-in backend spec** — consult
       :func:`fluid_build.api.catalog_backend.get_catalog_backend`.
       A registered spec means a canonical-payload-driven registrar
       (DataHub, OpenMetadata, Unity, Glue, Snowflake Horizon,
       Data Mesh Manager) — that's what acquisition contracts should
       always go through.
    2. **CATALOG_PROVIDERS fallback** — for native async providers
       (fluid-command-center) wrap them behind
       :class:`ProviderBackedRegistrar`. This keeps Surface B working
       for backends that only have a rich Surface A implementation.
    3. **None** — target isn't wired anywhere; ``register_all`` emits
       its existing ``No registrar configured`` error.

    Note: a backend can appear in both the plug-in registry *and*
    ``CATALOG_PROVIDERS`` (Data Mesh Manager does — async-native for
    Surface A's deep features, canonical-sync for Surface B). Checking
    the plug-in registry first ensures the canonical layer wins for
    Surface B even when a native async provider shares the name.
    """
    config = config or {}
    # 1. Plug-in spec (preferred).
    try:
        # Importing the registrar package triggers each module's
        # ``register_catalog_backend(...)`` side effect — required for
        # callers that haven't already touched ``providers.catalogs``.
        import fluid_build.build_runners.catalog_registrars  # noqa: F401
        from fluid_build.api.catalog_backend import get_catalog_backend
    except Exception:  # pragma: no cover — defensive
        spec = None
    else:
        spec = get_catalog_backend(target)
    if spec is not None:
        try:
            return spec.registrar_factory(config)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Failed to build catalog backend %s from spec: %s", target, exc)
            # Fall through to the CATALOG_PROVIDERS path — there might
            # be a usable native provider even if the plug-in factory
            # blew up (rare, but the failure mode shouldn't be silent).

    # 2. CATALOG_PROVIDERS fallback for native async providers.
    try:
        from fluid_build.providers.catalogs import CATALOG_PROVIDERS
        from fluid_build.providers.catalogs._registrar_adapter import (
            RegistrarBackedCatalogProvider,
        )
    except Exception:  # pragma: no cover
        return None
    cls = CATALOG_PROVIDERS.get(target)
    if cls is None:
        return None
    try:
        instance = cls(config)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Failed to build catalog backend %s: %s", target, exc)
        return None
    if isinstance(instance, RegistrarBackedCatalogProvider):
        return instance._registrar
    # Async-only provider — wrap behind the sync Protocol.
    from fluid_build.build_runners.catalog_registrars._provider_adapter import (
        ProviderBackedRegistrar,
    )

    adapter = ProviderBackedRegistrar(target=target)
    adapter._provider = instance  # type: ignore[assignment]
    return adapter


@dataclass
class CatalogPlan:
    targets: List[str]
    documentation_mode: str = "auto"

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "CatalogPlan":
        d = d or {}
        return cls(
            targets=list(d.get("register", [])),
            documentation_mode=d.get("documentation", "auto"),
        )


@dataclass
class RegistrationOutcome:
    results: List[RegistrationResult] = field(default_factory=list)

    @property
    def succeeded(self) -> List[RegistrationResult]:
        return [r for r in self.results if r.succeeded]

    @property
    def failed(self) -> List[RegistrationResult]:
        return [r for r in self.results if not r.succeeded]


def register_all(
    plan: CatalogPlan,
    product_id: str,
    expose_id: str,
    contract: Dict[str, Any],
    classifications: Optional[Dict[str, List[str]]] = None,
    target_configs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> RegistrationOutcome:
    """Dispatch *plan* to each registered target — legacy per-expose entry.

    Resolution order per target:

    1. Pre-registered instance in ``_REGISTRY`` (tests + extension
       modules use ``register_registrar`` to seed this).
    2. On-demand build via :func:`build_registrar` using
       ``target_configs[target]`` (or ``{}``) so the contract-driven
       path shares the same backends as ``fluid publish --target``
       without an explicit ``register_registrar`` call.
    3. Failure result with ``No registrar configured``.

    Backends are called through their **legacy** ``register`` method
    (per-expose) to preserve historical semantics for test fixtures
    that pre-register custom registrars and assert on per-expose URNs.
    Callers wanting the canonical once-per-contract path should use
    :func:`register_all_payload` instead.
    """
    classifications = classifications or {}
    target_configs = target_configs or {}
    outcome = RegistrationOutcome()
    for target in plan.targets:
        registrar = get_registrar(target)
        if registrar is None:
            registrar = build_registrar(target, target_configs.get(target))
        if registrar is None:
            outcome.results.append(
                RegistrationResult(
                    target=target,
                    urn=f"forge://{product_id}/{expose_id}",
                    succeeded=False,
                    error=f"No registrar configured for target '{target}'",
                )
            )
            continue
        try:
            outcome.results.append(
                registrar.register(product_id, expose_id, contract, classifications)
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Catalog target %s failed: %s", target, exc)
            outcome.results.append(
                RegistrationResult(
                    target=target,
                    urn=f"forge://{product_id}/{expose_id}",
                    succeeded=False,
                    error=str(exc),
                )
            )
    return outcome


def _has_canonical_register(registrar: CatalogRegistrar) -> bool:
    """Detect whether *registrar* actually implements
    :meth:`register_payload` or just inherits the Protocol's no-op
    ``...`` stub.

    The Protocol is ``runtime_checkable`` so concrete classes can use
    ``isinstance`` against it, but that comes with the cost that every
    subclass inherits the stub methods. A bare ``hasattr`` would route
    DMM-style legacy registrars (which only implement ``register``)
    into a code path that returns ``None``, breaking the dispatcher.

    The fix is to inspect where the method was *defined*: concrete
    implementations live on the registrar's own class; the Protocol
    stub lives on ``CatalogRegistrar``. Comparing qualnames keeps the
    check fast and dependency-free.
    """
    method = getattr(registrar, "register_payload", None)
    if method is None:
        return False
    qualname = getattr(method, "__qualname__", "")
    return not qualname.startswith("CatalogRegistrar.")


def register_all_payload(
    plan: CatalogPlan,
    payload: CatalogPublicationPayload,
    *,
    target_configs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> RegistrationOutcome:
    """Canonical dispatcher — call ``register_payload`` once per target.

    This is the dispatcher new code should use. Each backend gets the
    pre-built :class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
    (with rendered ODPS/ODCS specs, derived lineage, normalised
    metadata) instead of the raw FLUID dict — so renderers run once
    across the entire publish, regardless of how many targets are in
    the plan.

    Resolution order mirrors :func:`register_all` so test-injected
    registrars (via :func:`register_registrar`) still take priority
    over the auto-built ones.
    """
    target_configs = target_configs or {}
    outcome = RegistrationOutcome()
    product_urn_fallback = f"forge://{payload.product.product_id}"
    for target in plan.targets:
        registrar = get_registrar(target)
        if registrar is None:
            registrar = build_registrar(target, target_configs.get(target))
        if registrar is None:
            outcome.results.append(
                RegistrationResult(
                    target=target,
                    urn=product_urn_fallback,
                    succeeded=False,
                    error=f"No registrar configured for target '{target}'",
                )
            )
            continue
        # Prefer the canonical method when the backend implements it
        # natively. Fall back to legacy per-expose ``register`` for
        # third-party / test registrars that pre-date the canonical
        # layer — same fan-out as the old ``register_all`` did. Note
        # the ``__qualname__`` sniff: the Protocol's ``...`` stub gets
        # inherited as a no-op method on classes that subclass
        # :class:`CatalogRegistrar` and don't override it, so a bare
        # ``hasattr`` check would incorrectly route to a method that
        # returns ``None``. The qualname starts with the *defining*
        # class — concrete implementations land outside ``CatalogRegistrar``.
        if _has_canonical_register(registrar):
            try:
                outcome.results.append(registrar.register_payload(payload))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Catalog target %s failed (payload): %s", target, exc)
                outcome.results.append(
                    RegistrationResult(
                        target=target,
                        urn=product_urn_fallback,
                        succeeded=False,
                        error=str(exc),
                    )
                )
            continue
        # Legacy fan-out for backends without ``register_payload``.
        # Iterate every asset so the publish surface is consistent
        # whether the registrar is canonical or legacy.
        for asset in payload.assets:
            try:
                outcome.results.append(
                    registrar.register(
                        payload.product.product_id,
                        asset.asset_id,
                        dict(payload.contract),
                        {k: list(v) for k, v in payload.classifications.items()},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Catalog target %s failed (legacy): %s", target, exc)
                outcome.results.append(
                    RegistrationResult(
                        target=target,
                        urn=f"forge://{payload.product.product_id}/{asset.asset_id}",
                        succeeded=False,
                        error=str(exc),
                    )
                )
    return outcome
