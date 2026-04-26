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

"""DataHub metadata-source catalog adapter.

Reads datasets, schemas, lineage, ownership, tags, domains, and
business attributes from DataHub via the ``acryl-datahub`` Python SDK.
DataHub is the open-source / SaaS catalog from LinkedIn's data team,
widely deployed at large engineering orgs.

Required DataHub privileges:

* ``Manage Metadata`` (default for authenticated users) — list +
  read datasets.
* ``View Metadata`` (minimum) — read tags, ownership, glossary.

Auth methods (recommended → legacy):

* ``oauth`` — preferred for production. Configure an OAuth client in
  your IDP (Okta / Azure AD / Auth0) and pair with DataHub's
  acl-based RBAC.
* ``pat`` — personal access token. Acceptable when issued with
  short expiry and rotated regularly.
* ``none`` — no-auth dev path. The adapter logs a warning so
  operators know they're in dev-mode.

Lazy SDK import: ``acryl-datahub`` lives inside :meth:`_emitter` so
the adapter module loads without the ``[datahub]`` extra.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fluid_build.copilot.catalog._patterns import (
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
    DataHubCredentials,
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


class DataHubCatalogAdapter(CatalogAdapter):
    """Read metadata from DataHub via the acryl-datahub SDK."""

    name = "datahub"

    def __init__(self, credentials: DataHubCredentials) -> None:
        self._credentials = credentials
        self._cached_graph: Optional[Any] = None
        if credentials.auth_method == "none":
            _log.warning(
                "fluid.copilot.catalog.datahub.no_auth: DataHub adapter is in "
                "no-auth mode (auth_method='none'). Use only for dev / local "
                "instances; never in production."
            )

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Dict[str, Any]] = None,
    ) -> "DataHubCatalogAdapter":
        creds = resolver.resolve(
            catalog_name="datahub",
            credential_type=DataHubCredentials,
            credential_id=credential_id,
            inline_credentials=inline_credentials,
        )
        return cls(credentials=creds)

    def _emitter(self) -> Any:
        """Pattern 4 — lazy SDK import. Returns a DataHub
        ``DataHubGraph`` client which exposes the read-side API
        (search, lineage, glossary)."""
        if self._cached_graph is not None:
            return self._cached_graph
        try:
            from datahub.ingestion.graph.client import (  # type: ignore
                DataHubGraph,
                DataHubGraphConfig,
            )
        except ImportError as exc:
            raise CatalogConfigError(
                message=(
                    "acryl-datahub is not installed. The DataHub catalog "
                    "adapter requires the optional [datahub] extra."
                ),
                suggestions=[
                    'Install via: pip install "data-product-forge[datahub]"',
                    'Or install all catalogs: pip install "data-product-forge[catalogs]"',
                ],
            ) from exc

        kwargs = self._credentials.to_connection_kwargs()
        try:
            graph_config = DataHubGraphConfig(
                server=kwargs["server"],
                token=kwargs.get("token"),
            )
            graph = DataHubGraph(graph_config)
        except Exception as exc:
            raise CatalogConnectionError(
                message=f"DataHub graph client construction failed: {exc}",
                suggestions=[
                    "Verify DATAHUB_GMS_HOST / DATAHUB_GMS_TOKEN env vars.",
                    "Test connectivity: curl -H 'Authorization: Bearer <token>' <server>/config",
                ],
                original_error=exc,
            ) from exc
        self._cached_graph = graph
        return graph

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """Search DataHub for datasets matching the scope.

        Uses DataHub's search API filtered by platform + container
        (database + schema) to narrow results."""
        graph = self._emitter()
        # DataHub's search uses URN filters. Build a query that
        # narrows by platform / container if the scope provides
        # them; otherwise broad search.
        query_parts: List[str] = []
        if scope.database:
            query_parts.append(f"container:{scope.database}")
        if scope.schema_name:
            query_parts.append(f"container:{scope.schema_name}")
        query_parts.append("entity_type:dataset")
        query = " AND ".join(query_parts) or "*"

        try:
            results = list(
                graph.search(
                    query=query,
                    entity_types=["dataset"],
                    skip=0,
                    count=200,
                )
            )
        except Exception as exc:
            raise self._translate_query_error(exc, scope=scope) from exc

        out: List[CatalogTable] = []
        for entity in results:
            # DataHub SDK returns entities as SDK objects (with
            # ``.urn`` attribute) on most paths and dicts on others;
            # support both shapes. Parenthesise the conditional to
            # avoid Python's operator-precedence trap where
            # ``a or b if cond else None`` parses as
            # ``(a or b) if cond else None`` and discards the attr
            # path entirely when ``entity`` isn't a dict.
            if isinstance(entity, dict):
                urn = entity.get("urn")
            else:
                urn = getattr(entity, "urn", None)
            if not urn:
                continue
            name = _extract_dataset_name(urn)
            if scope.tables and name not in scope.tables:
                continue
            out.append(
                CatalogTable(
                    fqn=urn,
                    name=name,
                    catalog_specific={"datahub_urn": urn},
                )
            )
        return out

    def get_table(self, fqn: str) -> CatalogTable:
        """Pull full dataset detail from DataHub.

        DataHub uses URNs as FQNs; we accept either the full URN or
        a 3-part dotted name and translate.
        """
        graph = self._emitter()
        urn = self._normalise_urn(fqn)
        try:
            from datahub.metadata.schema_classes import (  # type: ignore
                DatasetPropertiesClass,
                GlobalTagsClass,
                OwnershipClass,
                SchemaMetadataClass,
            )

            props = graph.get_aspect(entity_urn=urn, aspect=DatasetPropertiesClass)
            schema = graph.get_aspect(entity_urn=urn, aspect=SchemaMetadataClass)
            ownership = graph.get_aspect(entity_urn=urn, aspect=OwnershipClass)
            tags = graph.get_aspect(entity_urn=urn, aspect=GlobalTagsClass)
        except Exception as exc:
            raise self._translate_query_error(exc, fqn=fqn) from exc

        columns: List[CatalogColumn] = []
        if schema and schema.fields:
            for field in schema.fields:
                columns.append(
                    CatalogColumn(
                        name=field.fieldPath,
                        data_type=str(field.nativeDataType or "STRING"),
                        nullable=bool(getattr(field, "nullable", True)),
                        description=getattr(field, "description", None),
                    )
                )

        owner_str = None
        if ownership and ownership.owners:
            owner_str = ",".join(str(o.owner) for o in ownership.owners)

        tag_dict: Dict[str, str] = {}
        if tags and tags.tags:
            for tag_assoc in tags.tags:
                tag_dict[str(tag_assoc.tag).split(":")[-1]] = ""

        return CatalogTable(
            fqn=urn,
            name=urn.rsplit(",", 1)[-1].split(")")[0].split(":")[-1],
            description=getattr(props, "description", None) if props else None,
            owner=owner_str,
            tags=tag_dict,
            columns=columns,
            catalog_specific={"datahub_urn": urn},
        )

    def get_lineage(self, fqn: str) -> CatalogLineage:
        """Pull DataHub upstream + downstream lineage for ``fqn``."""
        graph = self._emitter()
        urn = self._normalise_urn(fqn)

        upstream: List[LineageRef] = []
        downstream: List[LineageRef] = []
        try:
            # DataHub's get_aspect with UpstreamLineageClass.
            from datahub.metadata.schema_classes import UpstreamLineageClass  # type: ignore

            up = graph.get_aspect(entity_urn=urn, aspect=UpstreamLineageClass)
            if up and up.upstreams:
                for u in up.upstreams:
                    upstream.append(LineageRef(fqn=str(u.dataset), kind="upstream"))
        except Exception as exc:
            _log.debug(
                "fluid.copilot.catalog.datahub.lineage.skipped: %s — %s",
                urn,
                exc,
            )
            return CatalogLineage()

        # Downstream — DataHub doesn't store downstream as an aspect;
        # we'd need a search query. Pattern 1 — soft-fail and leave
        # downstream empty. Sprint D may add an explicit
        # ``search_downstream`` step.
        return CatalogLineage(upstream=upstream, downstream=downstream)

    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """List DataHub glossary terms.

        DataHub has a first-class business glossary; this maps each
        :class:`GlossaryTerm` directly."""
        graph = self._emitter()
        try:
            results = list(
                graph.search(
                    query="*",
                    entity_types=["glossaryTerm"],
                    skip=0,
                    count=500,
                )
            )
        except Exception as exc:
            _log.debug(
                "fluid.copilot.catalog.datahub.glossary.skipped: %s",
                exc,
            )
            return []

        out: List[GlossaryTerm] = []
        for entity in results:
            urn = getattr(entity, "urn", None) or (
                entity.get("urn") if isinstance(entity, dict) else None
            )
            if not urn:
                continue
            name = urn.rsplit(",", 1)[-1].split(":")[-1]
            out.append(
                GlossaryTerm(
                    term=name,
                    definition=getattr(entity, "description", "") or "",
                    catalog_specific={"datahub_urn": urn},
                )
            )
        return out

    # -----------------------------------------------------------------
    # Audit context
    # -----------------------------------------------------------------

    def audit_context(self) -> Dict[str, Any]:
        ctx = super().audit_context()
        ctx["server"] = self._credentials.server
        ctx["auth_method"] = self._credentials.auth_method
        return ctx

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _extract_table_name(self, urn: str) -> str:
        """Extract a human-readable table name from a DataHub URN.

        Wraps the module-level helper so adapter callers don't
        need to import it directly. Handles both the standard
        URN form and a degenerate dotted FQN that's been passed
        through unchanged.
        """
        return _extract_dataset_name(urn)

    @staticmethod
    def _normalise_urn(fqn: str) -> str:
        """Accept either a full DataHub URN or a 3-part dotted FQN
        and return the URN form. Allows users to type
        ``my-platform.dataset.table`` instead of the verbose URN."""
        if fqn.startswith("urn:li:"):
            return fqn
        parts = fqn.split(".", 2)
        if len(parts) == 3:
            platform, container, name = parts
            return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{container}.{name},PROD)"
        # Otherwise pass through; DataHub's search may still match.
        return fqn

    def _translate_query_error(
        self,
        exc: Exception,
        *,
        fqn: Optional[str] = None,
        scope: Optional[CatalogScope] = None,
    ) -> CatalogError:
        target = fqn or (f"{scope.database}.{scope.schema_name}" if scope else "<unknown>")
        return translate_permission_or_connection_error(
            exc,
            target=target,
            privilege_label="View Metadata (or Manage Metadata) DataHub policy",
            permission_markers=("Forbidden", "401", "403", "Unauthorized"),
            not_found_markers=("Not Found", "404", "does not exist"),
            connection_suggestions=[
                "Verify DATAHUB_GMS_HOST is reachable from this host.",
                "Test the token: curl -H 'Authorization: Bearer <token>' <server>/config",
            ],
        )


def _extract_dataset_name(urn: str) -> str:
    """Pull the dataset name out of a DataHub URN.

    The URN format is::

        urn:li:dataset:(urn:li:dataPlatform:<platform>,<dataset>,<env>)

    The dataset segment is the **middle** comma-separated field
    inside the parentheses — NOT the last (that's the env) and
    NOT the first (that's the platform URN). Splitting by ``rsplit("," , 1)[-1]``
    produces the env, which was the V1.5 first-cut bug.

    The adapter then takes the last dotted component as the
    "table name" so a UI / log can show ``orders`` rather than
    ``db.schema.orders``. The full FQN is preserved in
    :attr:`CatalogTable.fqn` (which uses the URN verbatim) for
    downstream consumers that need the canonical identity.

    Falls back gracefully when the input isn't a URN — returns
    the last dotted component, matching how the rest of the
    adapter's dispatch works on bare FQN inputs.
    """
    if not urn:
        return ""
    if urn.startswith("urn:li:dataset:") and "(" in urn and ")" in urn:
        # Strip ``urn:li:dataset:(`` prefix + ``)`` suffix, then
        # split on commas — element 1 is the dataset segment.
        body = urn.split("(", 1)[1].rsplit(")", 1)[0]
        parts = body.split(",")
        if len(parts) >= 2:
            dataset_segment = parts[1].strip()
            return dataset_segment.rsplit(".", 1)[-1] or dataset_segment
    # Fall back: dotted FQN passed through unchanged.
    return urn.rsplit(".", 1)[-1] or urn


__all__ = ["DataHubCatalogAdapter"]
