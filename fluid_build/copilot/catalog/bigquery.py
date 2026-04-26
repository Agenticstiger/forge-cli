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

"""BigQuery metadata-source catalog adapter.

Reads table / column / partition / FK metadata from BigQuery's
INFORMATION_SCHEMA. Read-only by design — no ``SELECT * FROM <table>``
ever runs; only the metadata views (TABLES, COLUMNS, TABLE_OPTIONS,
TABLE_CONSTRAINTS, KEY_COLUMN_USAGE, PARTITIONS).

Required BigQuery roles (``roles/bigquery.metadataViewer`` is the
minimum that satisfies the metadata reads here; the adapter's typed
permission errors point operators at the exact role needed):

* ``bigquery.tables.list`` (in ``roles/bigquery.metadataViewer``)
* ``bigquery.tables.get``
* ``bigquery.routines.get`` (for stored-procedure metadata; optional)
* ``resourcemanager.projects.get`` (project-level metadata)

Configuration honours the standard Google auth chain:

* ``GOOGLE_APPLICATION_CREDENTIALS`` → service account JSON.
* ``gcloud auth application-default login`` → ADC (developer
  laptops).
* GCE / GKE / Cloud Run metadata server → workload identity (only
  used when ``--allow-metadata-service`` / ``FLUID_ALLOW_METADATA_SERVICE=1``
  is set per the V1.5 plan's Choice 3 = (B)).

Lazy SDK import: ``google.cloud.bigquery`` lives inside :meth:`_client`
so a forge-cli install without the ``[gcp]`` extra still loads the
adapter module — only invocation requires the SDK.

This adapter applies all 9 patterns documented in
:mod:`fluid_build.copilot.catalog._patterns` (read the module
docstring for the full template).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fluid_build.copilot.catalog._patterns import (
    safe_metadata_call,
    translate_permission_or_connection_error,
    validate_and_quote_identifier,
)
from fluid_build.copilot.catalog.base import (
    CatalogAdapter,
    CatalogConfigError,
    CatalogConnectionError,
    CatalogError,
)
from fluid_build.copilot.catalog.credentials import (
    BigQueryCredentials,
    CredentialResolver,
)
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
)

# BigQuery has DIFFERENT identifier rules per object kind:
#
# * Project IDs allow lowercase letters, digits, and hyphens
#   (must start with a lowercase letter; 6-30 chars). The hyphen
#   is the gotcha — every other warehouse forbids it but it's
#   how Google Cloud projects are commonly named (``my-proj-123``).
# * Dataset / table names are stricter: letters, digits, underscores
#   only. No hyphens allowed.
#
# The pattern table here keeps both rules visible — adding a new
# identifier kind (e.g., a connection name) just adds one entry.
_BQ_IDENT_PATTERNS = {
    # Allow hyphens for project IDs.
    "project": re.compile(r"^[A-Za-z][A-Za-z0-9-]*$"),
    # Stricter for datasets / tables.
    "dataset": re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$"),
    "table": re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$"),
}


def _quote_ident(value: str, *, kind: str) -> str:
    """BigQuery identifiers are backtick-quoted, not double-quoted —
    the only deviation from Snowflake's pattern.

    Picks the right validation pattern based on ``kind`` so project
    IDs (which allow hyphens) and dataset / table names (which
    don't) get the right rule. An unknown ``kind`` falls back to
    the default identifier pattern from the shared helper.
    """
    pattern = _BQ_IDENT_PATTERNS.get(kind)
    return validate_and_quote_identifier(
        value, kind=f"BigQuery {kind}", pattern=pattern, quote_char="`"
    )


def _validate_ident(value: str, *, kind: str) -> str:
    """Return a BigQuery identifier after strict kind-specific validation."""
    pattern = _BQ_IDENT_PATTERNS.get(kind)
    if pattern is None or not pattern.match(value or ""):
        raise CatalogConfigError(
            message=f"Invalid BigQuery {kind} identifier: {value!r}",
            suggestions=[
                "Use only letters, numbers, and underscores for datasets/tables; "
                "project IDs may also include hyphens."
            ],
        )
    return value


class BigQueryCatalogAdapter(CatalogAdapter):
    """Read metadata from a BigQuery project/dataset.

    Construction takes a typed :class:`BigQueryCredentials`; use
    :meth:`from_resolver` for the canonical MCP / CLI dispatch path.
    """

    name = "bigquery"

    def __init__(self, credentials: BigQueryCredentials) -> None:
        self._credentials = credentials
        self._cached_client: Optional[Any] = None

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Dict[str, Any]] = None,
    ) -> "BigQueryCatalogAdapter":
        creds = resolver.resolve(
            catalog_name="bigquery",
            credential_type=BigQueryCredentials,
            credential_id=credential_id,
            inline_credentials=inline_credentials,
        )
        return cls(credentials=creds)

    # -----------------------------------------------------------------
    # Lazy SDK import + per-call client
    # -----------------------------------------------------------------

    def _client(self) -> Any:
        """Pattern 4 — lazy SDK import.

        ``google.cloud.bigquery`` is heavy (~3MB of dependencies);
        deferring the import until first call keeps ``fluid --help``
        sub-second.

        BigQuery's ``Client`` reuses an HTTP/2 connection pool, so
        caching one per adapter instance is fine. Per-call
        construction (the MCP server's contract) makes a fresh
        adapter each request, which gets a fresh client — same
        end result as Snowflake's per-call connect.
        """
        if self._cached_client is not None:
            return self._cached_client
        try:
            from google.cloud import bigquery  # type: ignore
        except ImportError as exc:
            raise CatalogConfigError(
                message=(
                    "google-cloud-bigquery is not installed. The BigQuery "
                    "catalog adapter requires the optional [gcp] extra."
                ),
                suggestions=[
                    'Install via: pip install "data-product-forge[gcp]"',
                    'Or install all catalogs: pip install "data-product-forge[catalogs]"',
                ],
            ) from exc

        kwargs = self._credentials.to_connection_kwargs()
        sa_path = kwargs.pop("credentials_path", None)
        try:
            if sa_path:
                from google.oauth2 import service_account  # type: ignore

                creds = service_account.Credentials.from_service_account_file(sa_path)
                client = bigquery.Client(credentials=creds, **kwargs)
            else:
                # ADC path — google-auth's default chain runs:
                # GOOGLE_APPLICATION_CREDENTIALS env var, then gcloud
                # ADC, then metadata server (only fires if the host
                # is on GCE / GKE / Cloud Run; the V1.5
                # ``allow_metadata_service`` gate is enforced at
                # the resolver layer, NOT here).
                client = bigquery.Client(**kwargs)
        except Exception as exc:
            raise CatalogConnectionError(
                message=f"BigQuery client construction failed: {exc}",
                suggestions=[
                    (
                        "Verify GOOGLE_APPLICATION_CREDENTIALS env var or run "
                        "`gcloud auth application-default login`."
                    ),
                    "Confirm the service account has roles/bigquery.metadataViewer.",
                ],
                original_error=exc,
            ) from exc
        self._cached_client = client
        return client

    # -----------------------------------------------------------------
    # CatalogAdapter ABC
    # -----------------------------------------------------------------

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """Pattern 8 — two-pass fetching: lightweight summaries here,
        full per-table detail in :meth:`get_table`.

        Uses ``INFORMATION_SCHEMA.TABLES`` so the listing is one
        query regardless of how many tables exist.
        """
        if not scope.schema_name:
            raise CatalogConfigError(
                message="BigQuery CatalogScope requires 'schema_name' (the dataset).",
                suggestions=[
                    "Pass scope.schema_name='analytics' (the BigQuery dataset).",
                    "Override the project with scope.database='myproj' if different from credentials.",
                ],
            )
        project = scope.database or self._credentials.project
        project_quoted = _quote_ident(project, kind="project")
        dataset_quoted = _quote_ident(scope.schema_name, kind="dataset")

        client = self._client()
        sql_template = """
            SELECT table_catalog, table_schema, table_name,
                   creation_time, ddl
              FROM {project}.{dataset}.INFORMATION_SCHEMA.TABLES
             WHERE table_type = 'BASE TABLE'
        """
        sql = sql_template.format(project=project_quoted, dataset=dataset_quoted)
        if scope.tables:
            validated_tables = [_validate_ident(t, kind="table") for t in scope.tables]
            placeholders = ", ".join(f"'{t}'" for t in validated_tables)
            if placeholders:
                sql += f" AND table_name IN ({placeholders})"
        try:
            results = list(client.query(sql).result())
        except Exception as exc:
            raise self._translate_query_error(exc, scope=scope) from exc

        tables: List[CatalogTable] = []
        for row in results:
            fqn = f"{row['table_catalog']}.{row['table_schema']}.{row['table_name']}"
            tables.append(
                CatalogTable(
                    fqn=fqn,
                    database=row["table_catalog"],
                    schema_name=row["table_schema"],
                    name=row["table_name"],
                    last_modified=row.get("creation_time"),
                )
            )
        return tables

    def get_table(self, fqn: str) -> CatalogTable:
        """Full per-table detail.

        Header from ``INFORMATION_SCHEMA.TABLE_OPTIONS`` (description
        + labels), columns from ``COLUMNS``, partition keys from
        ``PARTITIONS``, primary/foreign keys from ``TABLE_CONSTRAINTS``
        + ``KEY_COLUMN_USAGE``. PK/FK + partition reads use the
        soft-fail wrapper (pattern 1) since GCP datasets often
        decline to populate constraint metadata.
        """
        project, dataset, table = self._parse_fqn(fqn)
        project_quoted = _quote_ident(project, kind="project")
        dataset_quoted = _quote_ident(dataset, kind="dataset")

        client = self._client()
        # Use the SDK's structured ``get_table`` for the table header
        # rather than INFORMATION_SCHEMA — gives us description +
        # labels + schema + clustering_fields without three separate
        # queries.
        try:
            table_ref = client.get_table(fqn)
        except Exception as exc:
            raise self._translate_query_error(exc, fqn=fqn) from exc

        columns = [
            CatalogColumn(
                name=field.name,
                data_type=str(field.field_type),
                nullable=(field.mode != "REQUIRED"),
                description=field.description,
                primary_key=False,  # BQ PK is informational; populated below
            )
            for field in table_ref.schema
        ]

        # Partition / clustering keys.
        partition_keys: List[str] = []
        if table_ref.time_partitioning and table_ref.time_partitioning.field:
            partition_keys.append(table_ref.time_partitioning.field)
        if table_ref.range_partitioning and table_ref.range_partitioning.field:
            partition_keys.append(table_ref.range_partitioning.field)
        clustering_keys = list(table_ref.clustering_fields or [])

        # Primary keys + foreign keys via INFORMATION_SCHEMA. Soft-fail
        # — these are informational in BigQuery and frequently absent.
        pk_columns = safe_metadata_call(
            lambda: self._fetch_primary_key(client, project_quoted, dataset_quoted, table),
            fallback=[],
            description="bigquery primary-key fetch",
            log_target=fqn,
        )
        foreign_keys = safe_metadata_call(
            lambda: self._fetch_foreign_keys(client, project_quoted, dataset_quoted, table),
            fallback=[],
            description="bigquery foreign-key fetch",
            log_target=fqn,
        )
        # Mark PK columns on the column list.
        if pk_columns:
            columns = [
                CatalogColumn(**{**col.model_dump(), "primary_key": col.name in pk_columns})
                for col in columns
            ]

        return CatalogTable(
            fqn=fqn,
            database=project,
            schema_name=dataset,
            name=table,
            description=table_ref.description,
            owner=getattr(table_ref, "labels", {}).get("owner"),
            tags=dict(getattr(table_ref, "labels", {}) or {}),
            primary_key_columns=pk_columns,
            foreign_keys=foreign_keys,
            partition_keys=partition_keys,
            clustering_keys=clustering_keys,
            columns=columns,
            last_modified=table_ref.modified,
        )

    def get_lineage(self, fqn: str) -> CatalogLineage:
        """BigQuery's first-class lineage lives under Dataplex Lineage
        (separate adapter), not BigQuery itself. The BigQuery adapter
        returns an empty :class:`CatalogLineage` so the caller gets
        a consistent shape; richer lineage is the
        :class:`DataplexCatalogAdapter`'s job.
        """
        return CatalogLineage()

    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """BigQuery has no first-class glossary API. Glossary signal
        flows through Dataplex's business-glossary aspect — handled
        by the dataplex adapter."""
        return []

    # -----------------------------------------------------------------
    # Audit context
    # -----------------------------------------------------------------

    def audit_context(self) -> Dict[str, Any]:
        """Pattern 6 — non-sensitive identifiers only."""
        ctx = super().audit_context()
        ctx["project"] = self._credentials.project
        ctx["auth_method"] = self._credentials.auth_method
        if self._credentials.location:
            ctx["location"] = self._credentials.location
        return ctx

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _parse_fqn(fqn: str) -> tuple[str, str, str]:
        parts = fqn.split(".")
        if len(parts) != 3:
            raise CatalogConfigError(
                message=f"BigQuery FQN must be PROJECT.DATASET.TABLE; got {fqn!r}",
                suggestions=["Provide all three parts: my-proj.analytics.events."],
            )
        return (
            _validate_ident(parts[0], kind="project"),
            _validate_ident(parts[1], kind="dataset"),
            _validate_ident(parts[2], kind="table"),
        )

    def _fetch_primary_key(
        self, client: Any, project_quoted: str, dataset_quoted: str, table: str
    ) -> List[str]:
        """Read PRIMARY KEY constraint from INFORMATION_SCHEMA.

        BigQuery PRIMARY KEY constraints were added in 2023; older
        datasets won't have entries in TABLE_CONSTRAINTS. The
        soft-fail wrapper (pattern 1) handles that gracefully.
        """
        sql_template = """
            SELECT kcu.column_name
              FROM {project}.{dataset}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS  tc
              JOIN {project}.{dataset}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE   kcu
                ON tc.constraint_name = kcu.constraint_name
             WHERE tc.constraint_type = 'PRIMARY KEY'
               AND tc.table_name      = '{table_name}'
             ORDER BY kcu.ordinal_position
        """
        sql = sql_template.format(
            project=project_quoted,
            dataset=dataset_quoted,
            table_name=table,
        )
        return [row["column_name"] for row in client.query(sql).result()]

    def _fetch_foreign_keys(
        self, client: Any, project_quoted: str, dataset_quoted: str, table: str
    ) -> List[CatalogForeignKey]:
        sql_template = """
            SELECT tc.constraint_name,
                   kcu.column_name,
                   ccu.table_catalog,
                   ccu.table_schema,
                   ccu.table_name,
                   ccu.column_name AS to_column
              FROM {project}.{dataset}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS         tc
              JOIN {project}.{dataset}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE          kcu
                ON tc.constraint_name = kcu.constraint_name
              JOIN {project}.{dataset}.INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE   ccu
                ON tc.constraint_name = ccu.constraint_name
             WHERE tc.constraint_type = 'FOREIGN KEY'
               AND tc.table_name      = '{table_name}'
             ORDER BY tc.constraint_name, kcu.ordinal_position
        """
        sql = sql_template.format(
            project=project_quoted,
            dataset=dataset_quoted,
            table_name=table,
        )
        rows = list(client.query(sql).result())
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            entry = grouped.setdefault(
                row["constraint_name"],
                {
                    "to_table": f"{row['table_catalog']}.{row['table_schema']}.{row['table_name']}",
                    "from_columns": [],
                    "to_columns": [],
                },
            )
            entry["from_columns"].append(row["column_name"])
            entry["to_columns"].append(row["to_column"])
        return [
            CatalogForeignKey(
                constraint_name=name,
                from_columns=entry["from_columns"],
                to_table=entry["to_table"],
                to_columns=entry["to_columns"],
            )
            for name, entry in grouped.items()
        ]

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
            f"{scope.database or self._credentials.project}.{scope.schema_name}"
            if scope
            else "<unknown>"
        )
        return translate_permission_or_connection_error(
            exc,
            target=target,
            permission_grant_hint=(
                f"gcloud projects add-iam-policy-binding {self._credentials.project} "
                '--member="user:<email>" --role="roles/bigquery.metadataViewer"'
            ),
            privilege_label="roles/bigquery.metadataViewer",
            connection_suggestions=[
                "Verify GOOGLE_APPLICATION_CREDENTIALS env var or run `gcloud auth application-default login`.",
                "If the dataset is in a non-default region, set scope.location.",
            ],
        )


__all__ = ["BigQueryCatalogAdapter"]
