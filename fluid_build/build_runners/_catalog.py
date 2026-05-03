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

LOG = logging.getLogger("fluid.acquire.catalog")

# Registry of available registrars. Populated lazily so that missing optional
# extras (e.g., DataHub SDK) don't fail import.
_REGISTRY: Dict[str, CatalogRegistrar] = {}


def register_registrar(target: str, registrar: CatalogRegistrar) -> None:
    _REGISTRY[target] = registrar


def get_registrar(target: str) -> Optional[CatalogRegistrar]:
    return _REGISTRY.get(target)


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
) -> RegistrationOutcome:
    classifications = classifications or {}
    outcome = RegistrationOutcome()
    for target in plan.targets:
        registrar = get_registrar(target)
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
