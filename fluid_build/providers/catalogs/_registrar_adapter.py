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

"""Adapter — expose any sync ``api.catalog.CatalogRegistrar`` behind
the async ``BaseCatalogProvider`` interface that ``cli/publish.py``
consumes.

A single :class:`RegistrarBackedCatalogProvider` does the sync→async
bridging and the asset→(product_id, expose_id, contract) translation;
:func:`build_registrar_backed_provider` mints a per-target subclass
straight from a :class:`~fluid_build.api.catalog_backend.CatalogBackendSpec`
so adding a new backend never touches this module — the spec lives
next to the registrar.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, Type

import yaml

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult

from .base import BaseCatalogProvider, CatalogAsset, PublishResult

if TYPE_CHECKING:
    from fluid_build.api.catalog_backend import CatalogBackendSpec

LOG = logging.getLogger(__name__)


class RegistrarBackedCatalogProvider(BaseCatalogProvider):
    """Generic async wrapper around any sync ``CatalogRegistrar``.

    Subclasses (minted by :func:`build_registrar_backed_provider`)
    set the ``_spec`` class attribute to the backend's
    :class:`CatalogBackendSpec`; this base class uses
    ``_spec.registrar_factory(config)`` to construct the bound
    registrar and handles:

    - ``publish`` / ``update`` → ``registrar.register(...)`` in a worker
      thread (sync→async via ``asyncio.to_thread``)
    - ``verify`` / ``health_check`` → optimistic True (registrars
      don't expose existence/health probes; the actual publish
      surfaces real failures with full error messages)
    - asset → (product_id, expose_id, contract dict) reconstruction
      from ``asset.contract_yaml`` (publish.py sets this on every call)
    """

    name: str = "registrar-backed"
    _spec: "CatalogBackendSpec" = None  # type: ignore[assignment]

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        if self._spec is None:
            raise TypeError(
                f"{type(self).__name__} has no _spec attribute — subclasses must be "
                "minted via build_registrar_backed_provider(spec)."
            )
        self._registrar: CatalogRegistrar = self._spec.registrar_factory(config)

    @staticmethod
    def _expose_from_contract(contract: Dict[str, Any]) -> str:
        """Return the first expose's identifier in v0.7.3 + legacy shapes.

        Field-name precedence: ``exposeId`` (v0.7.3 canonical) → ``name``
        (pre-0.7.3 + some emitters) → ``id`` (legacy). Without the
        ``exposeId`` lookup, v0.7.3 contracts fall back to a synthetic
        expose_id equal to the contract id — which the URN builder
        then concatenates as ``<contract.id>.<contract.id>``, writing
        the dataset under a URN no caller would predict.
        """
        exposes = contract.get("exposes") or []
        if exposes:
            first = exposes[0] or {}
            return str(first.get("exposeId") or first.get("name") or first.get("id") or "")
        return ""

    @staticmethod
    def _contract_from_asset(asset: CatalogAsset) -> Dict[str, Any]:
        """Reconstruct the original contract dict from
        ``asset.contract_yaml`` (publish.py serialises the env-resolved
        contract there before calling us). Falls back to a minimal
        dict synthesised from the asset's fields if the YAML round-
        trip is unavailable — that path keeps unit tests that hand-
        build a ``CatalogAsset`` working."""
        if asset.contract_yaml:
            try:
                parsed = yaml.safe_load(asset.contract_yaml)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:  # noqa: BLE001
                LOG.debug(
                    "Falling back to synthetic contract for asset %s",
                    asset.id,
                    exc_info=True,
                )
        return {
            "id": asset.id,
            "name": asset.name,
            "description": asset.description,
            "metadata": {
                "owner": {"team": asset.owner, "email": asset.owner_email},
                "layer": asset.layer,
                "tags": asset.tags,
            },
            "exposes": [
                {
                    "name": asset.id,
                    "binding": {"platform": asset.platform, "location": asset.location},
                    "contract": {"schema": asset.schema or []},
                }
            ],
        }

    async def publish(self, asset: CatalogAsset) -> PublishResult:
        is_valid, err = self.validate_asset(asset)
        if not is_valid:
            return PublishResult(
                success=False,
                catalog_id=self.name,
                asset_id=asset.id,
                error=f"Validation failed: {err}",
            )
        contract = self._contract_from_asset(asset)
        # Build the canonical payload once on the async side, then hand
        # it to the sync registrar via a worker thread. That centralises
        # ODPS/ODCS rendering on the publish.py path — every backend
        # (DataHub, OpenMetadata, …) sees the same pre-rendered specs
        # whether it was reached via ``fluid publish --target`` or
        # ``properties.catalog.register:``.
        from fluid_build.api.catalog_publication import CatalogPublicationPayload

        payload = CatalogPublicationPayload.from_contract(contract, {})
        # Detect a *real* canonical method (vs. the Protocol's
        # ``...`` stub inherited by legacy registrars that subclass
        # :class:`CatalogRegistrar`). See ``_catalog._has_canonical_register``.
        from fluid_build.build_runners._catalog import _has_canonical_register

        try:
            if _has_canonical_register(self._registrar):
                result: RegistrationResult = await asyncio.to_thread(
                    self._registrar.register_payload, payload
                )
            else:
                # Third-party registrar that hasn't migrated — fall back
                # to the legacy per-expose path so they keep working.
                product_id = payload.product.product_id or asset.id
                expose_id = payload.assets[0].asset_id if payload.assets else asset.id
                result = await asyncio.to_thread(
                    self._registrar.register, product_id, expose_id, contract, {}
                )
        except Exception as exc:  # noqa: BLE001
            return PublishResult(
                success=False,
                catalog_id=self.name,
                asset_id=asset.id,
                error=str(exc),
            )
        return PublishResult(
            success=result.succeeded,
            catalog_id=self.name,
            asset_id=asset.id,
            catalog_url=result.urn or None,
            error=result.error,
            details={"urn": result.urn, "target": result.target, **result.metadata},
        )

    async def update(self, asset: CatalogAsset) -> PublishResult:
        return await self.publish(asset)

    async def verify(self, asset_id: str) -> bool:
        return True

    async def delete(self, asset_id: str) -> bool:
        LOG.warning(
            "delete not implemented for %s — registrar.unregister needs (product_id, expose_id)",
            self.name,
        )
        return False

    async def health_check(self) -> bool:
        return True


_PROVIDER_CACHE: Dict[str, Type[RegistrarBackedCatalogProvider]] = {}


def build_registrar_backed_provider(
    spec: "CatalogBackendSpec",
) -> Type[RegistrarBackedCatalogProvider]:
    """Mint (or fetch from cache) a :class:`BaseCatalogProvider`
    subclass that wraps *spec*'s registrar factory.

    Caching by ``spec.name`` keeps ``isinstance`` checks well-behaved
    across repeated calls and gives logs a stable class name.
    """
    if spec.name in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[spec.name]

    class _Provider(RegistrarBackedCatalogProvider):
        name = spec.name
        _spec = spec  # type: ignore[assignment]

    _Provider.__name__ = (
        spec.name.replace("-", "_").replace("_", " ").title().replace(" ", "") + "CatalogProvider"
    )
    _Provider.__qualname__ = _Provider.__name__
    _PROVIDER_CACHE[spec.name] = _Provider
    return _Provider


__all__ = [
    "RegistrarBackedCatalogProvider",
    "build_registrar_backed_provider",
]
