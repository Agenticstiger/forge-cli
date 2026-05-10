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

"""Google Cloud Dataplex metadata-source catalog adapter.

Reads aspect-types, business glossary, and column-level lineage from
Dataplex — Google's governance/discovery service that sits over
BigQuery + GCS + Pub/Sub. Where the BigQuery adapter handles raw
schema reads, Dataplex provides the *governance* layer: data quality
scores, freshness SLAs, sensitivity classifications, lineage chains,
business glossary terms.

This adapter is the dual to :class:`BigQueryCatalogAdapter` —
typically operators install both, point them at the same project,
and let the V1.5 staged pipeline pick up structure (BQ) +
governance (Dataplex) in one forge.

Required Dataplex roles:

* ``roles/dataplex.metadataReader`` for entry-group + entry reads.
* ``roles/dataplex.dataLineageViewer`` for lineage chains.
* ``roles/dataplex.glossaryViewer`` for business-glossary entries.

Lazy SDK import: ``google.cloud.dataplex_v1`` lives inside
:meth:`_clients` so the adapter module loads without the
``[gcp]`` extra installed.

This adapter applies all 9 patterns documented in
:mod:`fluid_build.copilot.catalog._patterns`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fluid_build.copilot.catalog._patterns import (
    safe_metadata_call,
    translate_permission_or_connection_error,
)
from fluid_build.copilot.catalog.base import (
    CatalogAdapter,
    CatalogConfigError,
    CatalogConnectionError,
    CatalogError,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    DataplexCredentials,
)
from fluid_build.copilot.catalog.models import (
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
    LineageRef,
)

_log = logging.getLogger(__name__)


class DataplexCatalogAdapter(CatalogAdapter):
    """Read governance metadata from Google Cloud Dataplex.

    Construction takes a typed :class:`DataplexCredentials`; use
    :meth:`from_resolver` for the canonical MCP / CLI dispatch path.
    """

    name = "dataplex"

    def __init__(self, credentials: DataplexCredentials) -> None:
        self._credentials = credentials
        self._cached_clients: Optional[Dict[str, Any]] = None

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Dict[str, Any]] = None,
    ) -> "DataplexCatalogAdapter":
        creds = resolver.resolve(
            catalog_name="dataplex",
            credential_type=DataplexCredentials,
            credential_id=credential_id,
            inline_credentials=inline_credentials,
        )
        return cls(credentials=creds)

    # -----------------------------------------------------------------
    # Lazy SDK import + per-call client lifecycle
    # -----------------------------------------------------------------

    def _clients(self) -> Dict[str, Any]:
        """Construct the three Dataplex SDK clients we need.

        Dataplex splits its API surface across three clients:
        ``CatalogServiceClient`` (entries / aspects),
        ``LineageServiceClient`` (lineage), and
        ``GlossaryServiceClient`` (business glossary). The adapter
        materialises all three on first use; subsequent calls reuse
        the cache.
        """
        if self._cached_clients is not None:
            return self._cached_clients
        try:
            from google.cloud import dataplex_v1  # type: ignore
        except ImportError as exc:
            raise CatalogConfigError(
                message=(
                    "google-cloud-dataplex is not installed. The Dataplex "
                    "catalog adapter requires the optional [gcp] extra."
                ),
                suggestions=[
                    'Install via: pip install "data-product-forge[gcp]"',
                    'Or install all catalogs: pip install "data-product-forge[catalogs]"',
                ],
            ) from exc

        kwargs = self._credentials.to_connection_kwargs()
        kwargs.pop("project", None)
        kwargs.pop("location", None)
        sa_path = kwargs.pop("credentials_path", None)
        try:
            if sa_path:
                from google.oauth2 import service_account  # type: ignore

                creds = service_account.Credentials.from_service_account_file(sa_path)
                catalog_client = dataplex_v1.CatalogServiceClient(credentials=creds)
                lineage_client = dataplex_v1.LineageServiceClient(credentials=creds)
                glossary_client = dataplex_v1.GlossaryServiceClient(credentials=creds)
            else:
                catalog_client = dataplex_v1.CatalogServiceClient()
                lineage_client = dataplex_v1.LineageServiceClient()
                glossary_client = dataplex_v1.GlossaryServiceClient()
        except Exception as exc:
            raise CatalogConnectionError(
                message=f"Dataplex client construction failed: {exc}",
                suggestions=[
                    "Verify GOOGLE_APPLICATION_CREDENTIALS env var or run "
                    "`gcloud auth application-default login`.",
                    "Confirm Dataplex API is enabled: "
                    "`gcloud services enable dataplex.googleapis.com`.",
                ],
                original_error=exc,
            ) from exc

        self._cached_clients = {
            "catalog": catalog_client,
            "lineage": lineage_client,
            "glossary": glossary_client,
        }
        return self._cached_clients

    # -----------------------------------------------------------------
    # CatalogAdapter ABC
    # -----------------------------------------------------------------

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """List Dataplex entries (the closest analog of "tables")
        under the configured project + location.

        Dataplex entries are richer than tables — they cover BQ
        tables, GCS files, Spanner tables, etc. This adapter
        filters to entries whose entry-type indicates a tabular
        resource so the modeler-agent prompt sees a familiar shape.
        """
        clients = self._clients()
        catalog = clients["catalog"]
        parent = (
            f"projects/{self._credentials.project}/"
            f"locations/{self._credentials.location}/"
            f"entryGroups/{scope.catalog or '@bigquery'}"
        )
        try:
            entries = list(catalog.list_entries(parent=parent))
        except Exception as exc:
            raise self._translate_query_error(exc, scope=scope) from exc

        results: List[CatalogTable] = []
        for entry in entries:
            fqn = entry.name  # full Dataplex resource name
            display_name = getattr(entry, "fully_qualified_name", None) or fqn
            results.append(
                CatalogTable(
                    fqn=fqn,
                    name=display_name.split(".")[-1],
                    description=getattr(entry, "description", None),
                    catalog_specific={
                        "entry_type": getattr(entry, "entry_type", None),
                        "fully_qualified_name": display_name,
                    },
                )
            )
        return results

    def get_table(self, fqn: str) -> CatalogTable:
        """Fetch full entry metadata + every aspect attached.

        Aspects carry data-quality scores, freshness SLAs,
        sensitivity classifications — exactly the governance signal
        the BuilderAgent and TransformationAgent want for the
        Fluid contract's ``dataQuality`` / ``agentPolicy`` blocks
        (Sprint D wires this through).
        """
        clients = self._clients()
        catalog = clients["catalog"]
        try:
            entry = catalog.get_entry(name=fqn, view="FULL")
        except Exception as exc:
            raise self._translate_query_error(exc, fqn=fqn) from exc

        # Extract aspect metadata into our typed shape. Aspects with
        # well-known names get hoisted to top-level fields; the rest
        # land in catalog_specific for the modeler to inspect.
        aspects = dict(getattr(entry, "aspects", {}) or {})
        quality_score = safe_metadata_call(
            lambda: (
                float(
                    aspects.get("dataplex.googleapis.com/data-quality/score", {})
                    .get("data", {})
                    .get("score", 0.0)
                )
                or None
            ),
            fallback=None,
            description="dataplex quality-score extraction",
            log_target=fqn,
        )
        freshness_sla = safe_metadata_call(
            lambda: aspects.get("dataplex.googleapis.com/freshness", {}).get("data", {}).get("sla"),
            fallback=None,
            description="dataplex freshness-sla extraction",
            log_target=fqn,
        )

        return CatalogTable(
            fqn=fqn,
            name=fqn.split("/")[-1],
            description=getattr(entry, "description", None),
            data_quality_score=quality_score,
            freshness_sla=freshness_sla,
            catalog_specific={"aspects_count": len(aspects)},
        )

    def get_lineage(self, fqn: str) -> CatalogLineage:
        """Fetch upstream + downstream lineage for ``fqn`` from
        Dataplex's data-lineage API.

        Returns an empty :class:`CatalogLineage` (not None) when the
        resource has no lineage data — keeps consumers from
        defensively checking.
        """
        clients = self._clients()
        lineage_client = clients["lineage"]

        upstream: List[LineageRef] = []
        downstream: List[LineageRef] = []
        try:
            parent = f"projects/{self._credentials.project}/locations/{self._credentials.location}"
            up = lineage_client.search_links(
                parent=parent,
                target={"fully_qualified_name": fqn},
            )
            for link in up:
                src_fqn = getattr(link, "source", {}).fully_qualified_name
                if src_fqn:
                    upstream.append(LineageRef(fqn=src_fqn, kind="upstream"))
            down = lineage_client.search_links(
                parent=parent,
                source={"fully_qualified_name": fqn},
            )
            for link in down:
                tgt_fqn = getattr(link, "target", {}).fully_qualified_name
                if tgt_fqn:
                    downstream.append(LineageRef(fqn=tgt_fqn, kind="downstream"))
        except Exception as exc:
            # Lineage is optional governance metadata; missing
            # roles on the lineage API shouldn't block ``get_table``
            # from returning useful data. Pattern 1 — soft-fail.
            _log.debug(
                "fluid.copilot.catalog.dataplex.lineage.skipped: %s — %s",
                fqn,
                exc,
            )
            return CatalogLineage()
        return CatalogLineage(upstream=upstream, downstream=downstream)

    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """List business-glossary entries for the configured
        project/location."""
        clients = self._clients()
        glossary = clients["glossary"]
        parent = f"projects/{self._credentials.project}/locations/{self._credentials.location}"
        try:
            terms_list = list(glossary.list_glossary_terms(parent=parent))
        except Exception as exc:
            # Soft-fail — glossary may simply not be set up.
            _log.debug(
                "fluid.copilot.catalog.dataplex.glossary.skipped: %s",
                exc,
            )
            return []

        results: List[GlossaryTerm] = []
        for term in terms_list:
            results.append(
                GlossaryTerm(
                    term=getattr(term, "display_name", None) or term.name.split("/")[-1],
                    definition=getattr(term, "description", "") or "",
                    synonyms=list(getattr(term, "synonyms", []) or []),
                    domain=scope.catalog,
                )
            )
        return results

    # -----------------------------------------------------------------
    # Audit context
    # -----------------------------------------------------------------

    def audit_context(self) -> Dict[str, Any]:
        ctx = super().audit_context()
        ctx["project"] = self._credentials.project
        ctx["location"] = self._credentials.location
        ctx["auth_method"] = self._credentials.auth_method
        return ctx

    # -----------------------------------------------------------------
    # Error translation
    # -----------------------------------------------------------------

    def _translate_query_error(
        self,
        exc: Exception,
        *,
        fqn: Optional[str] = None,
        scope: Optional[CatalogScope] = None,
    ) -> CatalogError:
        target = fqn or (
            f"projects/{self._credentials.project}/locations/{self._credentials.location}"
        )
        return translate_permission_or_connection_error(
            exc,
            target=target,
            permission_grant_hint=(
                f"gcloud projects add-iam-policy-binding {self._credentials.project} "
                '--member="user:<email>" --role="roles/dataplex.metadataReader"'
            ),
            privilege_label="roles/dataplex.metadataReader (and dataLineageViewer / glossaryViewer)",
            connection_suggestions=[
                "Verify GOOGLE_APPLICATION_CREDENTIALS env var.",
                "Confirm Dataplex API is enabled: gcloud services enable dataplex.googleapis.com.",
            ],
        )


__all__ = ["DataplexCatalogAdapter"]
