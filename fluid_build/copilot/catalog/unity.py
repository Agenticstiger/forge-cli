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

"""Databricks Unity Catalog adapter.

Reads metadata from Unity Catalog via the official ``databricks-sdk``:

* tables, columns, comments
* column-level tags (Unity ``TagAssignment`` API)
* column-level masks (Unity ``ColumnMask`` API)
* business-glossary entries
* lineage chains via the Unity lineage system tables

Required Databricks privileges (the adapter raises
:class:`CatalogPermissionError` with the matching guidance when one is
missing):

* ``USE CATALOG`` on the catalog being inspected.
* ``USE SCHEMA`` on the schema being inspected.
* ``BROWSE`` (or ``SELECT``) on the tables being inspected.
* For lineage: ``SELECT`` on
  ``system.access.table_lineage`` /
  ``system.access.column_lineage``.

Configuration is consumed via the ``DATABRICKS_HOST`` /
``DATABRICKS_TOKEN`` env vars (or a Databricks CLI profile via
``DATABRICKS_CONFIG_PROFILE``) — the SDK's standard auth chain. The
adapter passes ``connection_kwargs`` straight through to
``WorkspaceClient(**kwargs)``.

Lazy SDK import: ``databricks.sdk`` lives inside :meth:`_client` so a
forge-cli install without the ``[databricks]`` extra still loads the
module (raising :class:`CatalogConfigError` only when a method is
invoked).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fluid_build.copilot.catalog.base import (
    CatalogAdapter,
    CatalogConfigError,
    CatalogConnectionError,
    CatalogError,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    UnityCredentials,
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


class UnityCatalogAdapter(CatalogAdapter):
    """Read metadata from Databricks Unity Catalog.

    Construction takes a typed :class:`UnityCredentials` (preferred)
    so the auth-method choice (PAT / OAuth M2M / Azure AD / Google ID)
    is explicit and ``SecretStr`` protects every secret field.

    Use :meth:`from_resolver` for the canonical MCP / CLI dispatch
    path: the resolver merges keyring + ``sources.yaml`` into a
    typed credential.
    """

    name = "unity"

    def __init__(self, credentials: UnityCredentials) -> None:
        self._credentials = credentials
        self._cached_client: Optional[Any] = None

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Dict[str, Any]] = None,
    ) -> "UnityCatalogAdapter":
        """Build an adapter using the credential-resolver chain.

        Mirrors :meth:`SnowflakeCatalogAdapter.from_resolver` for
        consistent dispatch across catalogs.
        """
        creds = resolver.resolve(
            catalog_name="unity",
            credential_type=UnityCredentials,
            credential_id=credential_id,
            inline_credentials=inline_credentials,
        )
        return cls(credentials=creds)

    # -----------------------------------------------------------------
    # Lazy SDK import + per-call client
    # -----------------------------------------------------------------

    def _client(self) -> Any:
        """Return a ``WorkspaceClient`` for one operation.

        The Databricks SDK client is lightweight and reuses an
        underlying ``httpx`` connection pool, so caching one per
        adapter instance is fine — but we still scope the cache to
        the adapter, so an MCP server that constructs a fresh
        adapter per tool call gets fresh credentials too. The SDK
        itself is responsible for token refresh.
        """
        if self._cached_client is not None:
            return self._cached_client
        try:
            from databricks.sdk import WorkspaceClient  # type: ignore
        except ImportError as exc:
            raise CatalogConfigError(
                message=(
                    "databricks-sdk is not installed. The Unity Catalog "
                    "adapter requires the optional extra."
                ),
                suggestions=[
                    'Install via: pip install "data-product-forge[databricks]"',
                    "Or install the umbrella catalog extra: "
                    'pip install "data-product-forge[catalogs]"',
                ],
            ) from exc

        try:
            client = WorkspaceClient(**self._credentials.to_connection_kwargs())
        except Exception as exc:  # noqa: BLE001
            raise CatalogConnectionError(
                message=f"Databricks Unity workspace client construction failed: {exc}",
                suggestions=[
                    "Verify DATABRICKS_HOST and DATABRICKS_TOKEN env vars are set.",
                    "If using a config profile, set DATABRICKS_CONFIG_PROFILE.",
                    "Tokens can be issued from the Databricks UI under "
                    "User Settings → Developer → Access tokens.",
                ],
                original_error=exc,
            ) from exc
        self._cached_client = client
        return client

    # -----------------------------------------------------------------
    # CatalogAdapter ABC
    # -----------------------------------------------------------------

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """Enumerate tables under (catalog, schema) via the SDK's
        ``tables.list_summaries`` (lightweight; no per-column
        round trips)."""
        if not scope.catalog or not scope.schema_name:
            raise CatalogConfigError(
                message="Unity CatalogScope requires both 'catalog' and 'schema_name'.",
                suggestions=[
                    "Pass scope.catalog='main' and scope.schema_name='analytics'.",
                    "Unity is catalog-scoped (NOT database-scoped) by design.",
                ],
            )

        client = self._client()
        try:
            summaries = list(
                client.tables.list_summaries(
                    catalog_name=scope.catalog,
                    schema_name_pattern=scope.schema_name,
                )
            )
        except Exception as exc:
            raise self._translate_query_error(exc, scope=scope) from exc

        results: List[CatalogTable] = []
        for summary in summaries:
            full_name = getattr(summary, "full_name", None)
            if full_name is None:
                continue
            if scope.tables and full_name.split(".")[-1] not in scope.tables:
                continue
            results.append(
                CatalogTable(
                    fqn=full_name,
                    catalog_specific={"unity_table_type": getattr(summary, "table_type", None)},
                    database=scope.catalog,
                    schema_name=scope.schema_name,
                    name=full_name.split(".")[-1],
                )
            )
        return results

    def get_table(self, fqn: str) -> CatalogTable:
        """Return full metadata for one Unity FQN.

        Issues two SDK calls: ``tables.get`` (header + columns +
        comment + owner) and ``tags.list`` (Unity tags as a
        catalog-tag dict). Lineage stays in :meth:`get_lineage` so
        callers can opt in.
        """
        client = self._client()
        try:
            table = client.tables.get(full_name=fqn)
        except Exception as exc:
            raise self._translate_query_error(exc, fqn=fqn) from exc

        columns: List[CatalogColumn] = []
        for col in getattr(table, "columns", []) or []:
            columns.append(
                CatalogColumn(
                    name=col.name,
                    data_type=str(getattr(col, "type_text", "") or ""),
                    nullable=bool(getattr(col, "nullable", True)),
                    description=getattr(col, "comment", None),
                    primary_key=False,  # Unity exposes via separate constraint API in v1.6+
                    mask_expression=self._extract_column_mask(col),
                    classifications=self._extract_column_classifications(col),
                )
            )

        return CatalogTable(
            fqn=fqn,
            database=getattr(table, "catalog_name", None),
            schema_name=getattr(table, "schema_name", None),
            name=getattr(table, "name", None) or fqn.split(".")[-1],
            description=getattr(table, "comment", None),
            owner=getattr(table, "owner", None),
            tags=self._extract_table_tags(table),
            columns=columns,
            certification_level=getattr(table, "certification_status", None),
            catalog_specific={
                "unity_table_id": getattr(table, "table_id", None),
                "unity_table_type": str(getattr(table, "table_type", "")),
            },
        )

    def get_lineage(self, fqn: str) -> CatalogLineage:
        """Return upstream + downstream lineage from Unity's lineage
        system tables.

        Uses the SDK's ``lineage_tracking`` API when available and
        falls back to an empty :class:`CatalogLineage` when the
        workspace doesn't have lineage enabled (Unity feature flag).
        """
        client = self._client()
        upstream: List[LineageRef] = []
        downstream: List[LineageRef] = []
        try:
            lineage = client.lineage_tracking.get_table_lineage(table_name=fqn)  # type: ignore[attr-defined]
        except AttributeError:
            # Older SDK versions don't expose lineage_tracking; the
            # workspace may still have lineage enabled, but the
            # caller-side method isn't there. Gracefully degrade
            # rather than fail.
            return CatalogLineage()
        except Exception as exc:
            raise self._translate_query_error(exc, fqn=fqn) from exc

        for ref in getattr(lineage, "upstream_tables", []) or []:
            upstream.append(
                LineageRef(
                    fqn=getattr(ref, "name", "<unknown>"),
                    kind="upstream",
                    transformation_type=getattr(ref, "type", None),
                )
            )
        for ref in getattr(lineage, "downstream_tables", []) or []:
            downstream.append(
                LineageRef(
                    fqn=getattr(ref, "name", "<unknown>"),
                    kind="downstream",
                    transformation_type=getattr(ref, "type", None),
                )
            )
        return CatalogLineage(upstream=upstream, downstream=downstream)

    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """Unity exposes business attributes as managed-tag values
        rather than a first-class glossary API.

        Returns an empty list for v1.5 Sprint A; Sprint B / D will
        fold managed-tag taxonomy into glossary terms via the
        ``catalog.tags`` SDK if Databricks ships a true glossary
        endpoint.
        """
        return []

    # -----------------------------------------------------------------
    # Audit context — non-sensitive
    # -----------------------------------------------------------------

    def audit_context(self) -> Dict[str, Any]:
        """Override that adds the Databricks workspace host (no token).

        ``host`` and ``auth_method`` are non-sensitive and let the
        forensic trail distinguish events from different
        workspaces / different auth flows. Token / client-secret
        fields are intentionally excluded — the audit trail's
        redaction layer also scrubs them as a defense in depth.
        """
        ctx = super().audit_context()
        ctx["host"] = self._credentials.host
        ctx["auth_method"] = self._credentials.auth_method
        return ctx

    # -----------------------------------------------------------------
    # Private helpers — Unity-specific column shape extraction
    # -----------------------------------------------------------------

    @staticmethod
    def _extract_column_mask(col: Any) -> Optional[str]:
        """Pull the column-mask expression (if any) off a Unity
        ``ColumnInfo``. Unity stores masks in the ``mask`` attribute
        on the column object; older SDK versions surfaced it as
        ``column_mask``."""
        for attr in ("mask", "column_mask"):
            value = getattr(col, attr, None)
            if value is None:
                continue
            # ``value`` is typically a SDK object; ``__str__`` is the
            # default mask name. We don't try to extract the mask SQL
            # body — that's a separate workspace API call we don't
            # need for V1.5 Sprint A.
            return str(value)
        return None

    @staticmethod
    def _extract_column_classifications(col: Any) -> List[str]:
        """Pull catalog-level classifications (PII / PHI markers) off
        a Unity ``ColumnInfo``. Unity exposes these via the
        ``classifications`` attribute introduced in mid-2025; older
        SDK versions return ``None`` and we fall through gracefully.
        """
        classifications = getattr(col, "classifications", None)
        if not classifications:
            return []
        return [str(item) for item in classifications]

    @staticmethod
    def _extract_table_tags(table: Any) -> Dict[str, str]:
        """Convert Unity table-level tags to ``{name: value}``."""
        tags = getattr(table, "tags", None) or []
        result: Dict[str, str] = {}
        for tag in tags:
            name = getattr(tag, "name", None) or getattr(tag, "key", None)
            value = getattr(tag, "value", "") or ""
            if name:
                result[str(name)] = str(value)
        return result

    # -----------------------------------------------------------------
    # Error translation
    # -----------------------------------------------------------------

    def _translate_query_error(
        self, exc: Exception, *, fqn: Optional[str] = None, scope: Optional[CatalogScope] = None
    ) -> CatalogError:
        """Map an SDK exception to the typed catalog hierarchy.

        Databricks returns HTTP-shape errors from the SDK; we
        string-match the message to surface the most actionable
        suggestion, falling through to ``CatalogConnectionError``
        for unrecognised failures.
        """
        msg = str(exc)
        target = fqn or (f"{scope.catalog}.{scope.schema_name}" if scope else "<unknown>")
        if "PERMISSION_DENIED" in msg or "does not have" in msg.lower():
            return self._permission_error(
                f"Unity denied a metadata read on {target}: {msg}",
                privilege="USE CATALOG + USE SCHEMA + BROWSE on the table",
                grant_sql=(
                    f"GRANT USE CATALOG ON CATALOG `{target.split('.')[0]}` TO `<principal>`; "
                    f"GRANT USE SCHEMA ON SCHEMA `{target.split('.', 2)[0]}.{target.split('.', 2)[1]}` "
                    f"TO `<principal>`; "
                    f"GRANT BROWSE ON TABLE `{target}` TO `<principal>`;"
                ),
            )
        if "RESOURCE_DOES_NOT_EXIST" in msg or "Table not found" in msg:
            return CatalogConnectionError(
                message=f"Unity table not found: {target}",
                suggestions=[
                    f"Confirm the table exists: DESCRIBE TABLE EXTENDED `{target}`;",
                    "Verify the catalog + schema names are spelled correctly.",
                ],
                original_error=exc,
            )
        return CatalogConnectionError(
            message=f"Unity metadata read failed for {target}: {msg}",
            suggestions=[
                "Re-run with --verbose to see the full SDK trace.",
                "Confirm DATABRICKS_HOST is reachable from this network.",
            ],
            original_error=exc,
        )


__all__ = ["UnityCatalogAdapter"]
