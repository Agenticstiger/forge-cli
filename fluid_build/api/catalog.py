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

"""Catalog auto-registration types.

The :class:`CatalogRegistrar` Protocol carries TWO publish methods:

* :meth:`register_payload` — the canonical path. Backends consume the
  pre-built :class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
  with rendered specs, derived lineage, and normalised metadata.
  All new backends should implement this.

* :meth:`register` — the legacy per-expose path. Kept for backward
  compatibility with backends that haven't migrated yet and with
  callers (tests, third-party extensions) that pass the raw FLUID
  contract dict. New code should not lean on it.

The two methods agree on one thing: each ``register*`` call publishes
the *whole* product (every expose, every spec). Callers wanting
strict per-expose behaviour have to scope the contract themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .catalog_publication import CatalogPublicationPayload


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of one catalog registration."""

    target: str  # "datahub" | "openmetadata" | "unity" | "glue" | "snowflake_horizon"
    urn: str
    succeeded: bool
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CatalogRegistrar(Protocol):
    """Catalog registrar Protocol. One implementation per target.

    Implementations must define:

    * ``target`` attribute — the canonical backend name.
    * ``register_payload(payload)`` — canonical publish entry point.
    * ``unregister(product_id, expose_id)`` — soft-delete the product.

    ``register(product_id, expose_id, contract, classifications)`` is
    the legacy per-expose entry point and is **not** abstract here;
    the framework calls ``register_payload`` directly when possible.
    Backends that still need the legacy method (e.g. for direct test
    invocation) can implement it by building a payload internally.
    """

    target: str

    def register_payload(self, payload: "CatalogPublicationPayload") -> RegistrationResult: ...

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult: ...

    # ``register`` is kept on the Protocol for ``isinstance`` /
    # ``runtime_checkable`` symmetry with the legacy interface; backends
    # that have migrated to ``register_payload`` may keep a thin
    # wrapper that builds a payload from the args and delegates.
    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult: ...
