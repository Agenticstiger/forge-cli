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

"""Adapter — expose an async ``BaseCatalogProvider`` as a sync
``CatalogRegistrar`` so the two top-level surface targets
(``fluid-command-center`` and ``datamesh-manager``) are reachable from
the acquisition-pipeline ``properties.catalog.register: [...]`` path.

The symmetric counterpart of ``providers/catalogs/_registrar_adapter.py``.
Together they make every catalog backend reachable from both surfaces.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult

LOG = logging.getLogger("fluid.acquire.catalog.provider_adapter")


def _run_coro_blocking(coro):
    """Run *coro* to completion and return the result, regardless of
    whether the current thread already has a running event loop.

    The acquisition stage's ``publish_acquisition`` is sync but the
    typical CLI caller (``publish.py::run_async``) invokes it from
    inside ``asyncio.run(...)``. In that case ``asyncio.run`` from
    within the loop raises ``RuntimeError: asyncio.run() cannot be
    called from a running event loop``. The thread-pool short-hop
    sidesteps that by spinning the coroutine on a brand-new loop in a
    worker thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


@dataclass
class ProviderBackedRegistrar(CatalogRegistrar):
    """Wrap an async ``BaseCatalogProvider`` instance behind the sync
    ``CatalogRegistrar`` Protocol.

    The provider is constructed lazily so registrar instantiation at
    module import does not pull in optional HTTP dependencies for
    targets the user never publishes to.
    """

    target: str = "provider-backed"
    provider_name: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    _provider: Optional[Any] = None  # BaseCatalogProvider (lazy)

    def _resolved_provider(self):
        if self._provider is None:
            from fluid_build.providers.catalogs import get_catalog_provider

            self._provider = get_catalog_provider(self.provider_name or self.target, self.config)
        return self._provider

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        urn = f"forge://{product_id}/{expose_id}"
        try:
            provider = self._resolved_provider()
            asset = provider.map_contract_to_asset(contract)
            try:
                import yaml as _yaml

                asset.contract_yaml = _yaml.safe_dump(contract, sort_keys=False)
            except Exception:  # noqa: BLE001 — best-effort serialisation
                LOG.debug("contract_yaml round-trip failed for %s", product_id, exc_info=True)
            publish_result = _run_coro_blocking(provider.publish(asset))
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target=self.target, urn=urn, succeeded=False, error=str(exc))
        return RegistrationResult(
            target=self.target,
            urn=publish_result.catalog_url or urn,
            succeeded=publish_result.success,
            error=publish_result.error,
            metadata=publish_result.details or {},
        )

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        urn = f"forge://{product_id}/{expose_id}"
        try:
            provider = self._resolved_provider()
            ok = _run_coro_blocking(provider.delete(product_id))
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target=self.target, urn=urn, succeeded=False, error=str(exc))
        return RegistrationResult(target=self.target, urn=urn, succeeded=bool(ok))


__all__ = ["ProviderBackedRegistrar"]
