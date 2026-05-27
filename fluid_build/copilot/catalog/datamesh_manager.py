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

"""Data Mesh Manager (DMM) metadata-source catalog adapter.

forge-cli already PUBLISHES contracts to DMM via
``providers/datamesh_manager/``. This adapter is the dual: it READS
data product registrations + their contracts back out of DMM so a
new forge run can be informed by the existing data products in the
mesh — e.g., "I'm forging an analytics product that consumes
``customer-orders``; please respect that product's published
contract version 1.4."

The adapter wraps DMM's REST API directly (no separate SDK needed —
DMM exposes a clean OpenAPI surface). ``httpx`` is already a core
forge-cli dependency, so this adapter has zero new optional deps.

DMM uses a single Bearer-token auth model. Tokens rotate per
environment (dev / staging / prod); operators register them via
``fluid ai setup --source dmm-prod``.

This adapter applies all 9 patterns from
:mod:`fluid_build.copilot.catalog._patterns`. The "lazy SDK import"
pattern is satisfied trivially because httpx is already loaded by
the rest of forge-cli.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from fluid_build.copilot.catalog._patterns import (
    safe_metadata_call,
    translate_permission_or_connection_error,
)
from fluid_build.copilot.catalog.base import (
    CatalogAdapter,
    CatalogError,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    DataMeshManagerCredentials,
)
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
    LineageRef,
)

_log = logging.getLogger(__name__)


class DataMeshManagerCatalogAdapter(CatalogAdapter):
    """Read data-product registrations from Data Mesh Manager."""

    name = "datamesh_manager"

    def __init__(self, credentials: DataMeshManagerCredentials) -> None:
        self._credentials = credentials

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Dict[str, Any]] = None,
    ) -> "DataMeshManagerCatalogAdapter":
        creds = resolver.resolve(
            catalog_name="datamesh_manager",
            credential_type=DataMeshManagerCredentials,
            credential_id=credential_id,
            inline_credentials=inline_credentials,
        )
        return cls(credentials=creds)

    # -----------------------------------------------------------------
    # HTTP helper — per-call client lifecycle (pattern 3)
    # -----------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue one DMM REST call.

        Pattern 3 — per-call client lifecycle. We use a fresh
        ``httpx.Client`` per call (cheap) so an MCP server can't
        accumulate ambient connections.
        """
        url = f"{self._credentials.server.rstrip('/')}{path}"
        kw = self._credentials.to_connection_kwargs()
        api_key = kw["api_key"]
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("Authorization", f"Bearer {api_key}")
        headers.setdefault("Accept", "application/json")
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.request(method, url, headers=headers, **kwargs)
                resp.raise_for_status()
                if resp.headers.get("content-type", "").startswith("application/json"):
                    return resp.json()
                return resp.text
        except Exception as exc:
            raise self._translate_query_error(exc, fqn=path) from exc

    # -----------------------------------------------------------------
    # CatalogAdapter ABC
    # -----------------------------------------------------------------

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """List data products registered in DMM.

        DMM's "data product" is the analog of a table for forge
        purposes — each registered product has an FQN, owner,
        domain, and (optionally) a contract reference. We surface
        them as :class:`CatalogTable` instances so the MCP / staged
        pipeline treat them uniformly.
        """
        params: Dict[str, Any] = {}
        if scope.database:
            params["domain"] = scope.database
        try:
            # DMM REST endpoint is /api/dataproducts (no hyphen). The
            # publisher side at providers/datamesh_manager uses the
            # same path; matching ensures both directions stay in
            # lock-step with the vendor API surface.
            data = self._request("GET", "/api/dataproducts", params=params)
        except CatalogError:
            raise
        out: List[CatalogTable] = []
        items = data if isinstance(data, list) else data.get("dataProducts") or []
        for entry in items:
            name = entry.get("name") or entry.get("id") or "<unknown>"
            if scope.tables and name not in scope.tables:
                continue
            out.append(
                CatalogTable(
                    fqn=entry.get("id") or name,
                    name=name,
                    description=entry.get("description"),
                    owner=entry.get("owner"),
                    domain=entry.get("domain"),
                    catalog_specific={
                        "dmm_id": entry.get("id"),
                        "dmm_status": entry.get("status"),
                    },
                )
            )
        return out

    def get_table(self, fqn: str) -> CatalogTable:
        """Fetch one data product's full record + its associated
        contract (if registered)."""
        product = self._request("GET", f"/api/dataproducts/{fqn}")
        # Data contracts live at the separate ``/api/datacontracts/{id}``
        # endpoint in DMM. The data product itself only carries a
        # pointer; the contract body is fetched via its own path.
        contract = safe_metadata_call(
            lambda: self._request("GET", f"/api/datacontracts/{fqn}"),
            fallback=None,
            description="dmm contract fetch",
            log_target=fqn,
        )
        # The data-product schema lists output ports — each with its
        # own field list. Surface the FIRST output port's fields as
        # the table's columns; downstream stages can drill into
        # additional ports via catalog_specific.
        columns: List[CatalogColumn] = []
        output_ports = product.get("outputPorts") or []
        if output_ports:
            for field in output_ports[0].get("schema") or []:
                columns.append(
                    CatalogColumn(
                        name=field.get("name") or "<unknown>",
                        data_type=field.get("type") or "STRING",
                        nullable=bool(field.get("nullable", True)),
                        description=field.get("description"),
                    )
                )
        return CatalogTable(
            fqn=fqn,
            name=product.get("name") or fqn,
            description=product.get("description"),
            owner=product.get("owner"),
            domain=product.get("domain"),
            tags=dict(product.get("tags") or {}),
            columns=columns,
            certification_level=product.get("status"),
            catalog_specific={
                "dmm_contract": contract,
                "dmm_output_ports_count": len(output_ports),
            },
        )

    def get_lineage(self, fqn: str) -> CatalogLineage:
        """DMM tracks lineage between data products via the
        ``lineage`` endpoint."""
        try:
            data = self._request("GET", f"/api/dataproducts/{fqn}/lineage")
        except CatalogError as exc:
            _log.debug(
                "fluid.copilot.catalog.dmm.lineage.skipped: %s — %s",
                fqn,
                exc,
            )
            return CatalogLineage()
        upstream = [
            LineageRef(fqn=entry.get("id") or entry.get("name", "<unknown>"), kind="upstream")
            for entry in (data.get("upstream") or [])
        ]
        downstream = [
            LineageRef(fqn=entry.get("id") or entry.get("name", "<unknown>"), kind="downstream")
            for entry in (data.get("downstream") or [])
        ]
        return CatalogLineage(upstream=upstream, downstream=downstream)

    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """DMM has no first-class glossary endpoint.

        DMM models business vocabulary inside data-product /
        data-contract definitions rather than via a standalone
        ``/api/glossary`` collection. We return an empty list to
        keep the cross-adapter shape consistent (mirrors Glue,
        which also has no first-class glossary). Downstream stages
        of the forge pipeline already treat ``[]`` as the
        no-glossary-available signal.
        """
        return []

    # -----------------------------------------------------------------
    # Audit context
    # -----------------------------------------------------------------

    def audit_context(self) -> Dict[str, Any]:
        """Pattern 6 — non-sensitive only. Server URL is fine; the
        api_key is intentionally excluded."""
        ctx = super().audit_context()
        ctx["server"] = self._credentials.server
        return ctx

    # -----------------------------------------------------------------
    # Error translation
    # -----------------------------------------------------------------

    def _translate_query_error(self, exc: Exception, *, fqn: Optional[str] = None) -> CatalogError:
        target = fqn or "<dmm-root>"
        return translate_permission_or_connection_error(
            exc,
            target=target,
            permission_markers=("401", "403", "Unauthorized", "Forbidden"),
            not_found_markers=("404", "Not Found", "does not exist"),
            connection_suggestions=[
                "Verify DMM_API_URL is correct and reachable from this host.",
                (
                    "Test the token: curl -H 'Authorization: Bearer <token>' "
                    "<server>/api/dataproducts"
                ),
            ],
        )


__all__ = ["DataMeshManagerCatalogAdapter"]
