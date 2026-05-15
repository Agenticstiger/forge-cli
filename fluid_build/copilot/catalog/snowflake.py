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

"""Snowflake Horizon catalog adapter.

Reads metadata from Snowflake's INFORMATION_SCHEMA and Horizon
governance objects (OBJECT_TAGS, OBJECT_DEPENDENCIES, classifications).
**Read-only by design** — no ``SELECT * FROM <table>`` ever runs; we
only query the metadata views.

Required Snowflake privileges (the adapter raises
:class:`CatalogPermissionError` with the exact GRANT SQL when one is
missing):

* ``USAGE`` on the database + schema being inspected.
* ``REFERENCES`` (or higher) on the tables being inspected — required
  to read INFORMATION_SCHEMA.TABLE_CONSTRAINTS for primary / foreign
  keys.
* ``IMPORTED PRIVILEGES`` on the ``SNOWFLAKE`` shared database for
  ACCOUNT_USAGE views (object_dependencies / tag_references).

Configuration is consumed via the standard Snowflake env vars
(``SNOWFLAKE_ACCOUNT``, ``SNOWFLAKE_USER``, ``SNOWFLAKE_PASSWORD`` /
``SNOWFLAKE_PRIVATE_KEY``, ``SNOWFLAKE_ROLE``, ``SNOWFLAKE_WAREHOUSE``)
or passed inline through the constructor's ``connection_kwargs``.
The adapter never persists credentials beyond a single call — every
``list_tables`` / ``get_table`` opens a fresh connection and closes it
on exit, so an MCP-mediated agent can't replay an old session.

Lazy SDK import: the ``snowflake.connector`` import lives inside
:meth:`_connect` so a forge-cli install without the ``[snowflake]``
extra still loads the adapter module (raising
:class:`CatalogConfigError` only if the user actually invokes a
catalog method).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from fluid_build.copilot.catalog._patterns import (
    safe_metadata_call,
    translate_permission_or_connection_error,
    validate_and_quote_identifier,
)


def _quote_ident(value: str, *, kind: str) -> str:
    """Snowflake-specific wrapper over the shared identifier helper.

    Pattern 2 (identifier validation + quoting) — defers to the
    shared helper so every adapter validates with the same rules.
    Snowflake double-quotes are the SQL standard form and match the
    helper's default ``quote_char``.
    """
    return validate_and_quote_identifier(value, kind=f"Snowflake {kind}")


from fluid_build.copilot.catalog.base import (
    CatalogAdapter,
    CatalogConfigError,
    CatalogConnectionError,
    CatalogError,
    CatalogPermissionError,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    SnowflakeCredentials,
)
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
    LineageRef,
)

_log = logging.getLogger(__name__)


class SnowflakeCatalogAdapter(CatalogAdapter):
    """Read metadata from Snowflake Horizon.

    Construction takes a typed :class:`SnowflakeCredentials` (preferred)
    so the auth-method choice is explicit and ``SecretStr`` prevents
    accidental credential leakage. The adapter does NOT open a
    connection at construction time — connections open per-call,
    ensuring the MCP server can't accumulate persistent catalog
    access.

    Use :meth:`from_resolver` to construct from a
    :class:`CredentialResolver` with either a saved ``credential_id``
    or inline credentials — that's the canonical entry point from
    MCP tool dispatch.
    """

    name = "snowflake"

    def __init__(self, credentials: SnowflakeCredentials) -> None:
        self._credentials = credentials

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Dict[str, Any]] = None,
    ) -> "SnowflakeCatalogAdapter":
        """Build an adapter using the credential-resolver chain.

        This is the path the MCP tool dispatch uses: caller passes
        a ``credential_id`` (saved-source name) and the resolver
        merges keyring + ``sources.yaml`` into a typed
        :class:`SnowflakeCredentials`.
        """
        creds = resolver.resolve(
            catalog_name="snowflake",
            credential_type=SnowflakeCredentials,
            credential_id=credential_id,
            inline_credentials=inline_credentials,
        )
        return cls(credentials=creds)

    # -----------------------------------------------------------------
    # Connection management — lazy SDK import + per-call lifecycle
    # -----------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        """Open a fresh Snowflake connection for one operation.

        Lazy import ensures the optional ``[snowflake]`` extra isn't
        required to load the adapter module — only to call its
        methods. ``snowflake.connector`` imports a substantial
        amount of code; deferring keeps ``fluid --help`` cold-start
        below the plan's sub-second target.
        """
        try:
            import snowflake.connector  # type: ignore
        except ImportError as exc:
            raise CatalogConfigError(
                message=(
                    "snowflake-connector-python is not installed. "
                    "The Snowflake catalog adapter requires the optional extra."
                ),
                suggestions=[
                    'Install via: pip install "data-product-forge[snowflake]"',
                    "Or install the umbrella catalog extra: "
                    'pip install "data-product-forge[catalogs]"',
                ],
            ) from exc

        # Silence the connector's chatty stdout/stderr. The connector
        # prints "Snowflake Connector for Python Version: ..." +
        # "Connecting to GLOBAL Snowflake domain" on every connect
        # AND streams an OCSP / telemetry log to its own loggers. Under
        # ``--quiet`` (or in CI), that pollutes stdout and breaks
        # downstream JSON pipelines. We unconditionally raise the
        # connector's loggers to WARNING — we still want the
        # operator's own forge logger to show what the adapter is
        # doing; we just don't want the SDK's noise.
        import logging as _logging

        for _name in (
            "snowflake.connector",
            "snowflake.connector.network",
            "snowflake.connector.cursor",
            "snowflake.connector.connection",
            "snowflake.connector.telemetry",
            "botocore",
            "boto3",
        ):
            _logging.getLogger(_name).setLevel(_logging.WARNING)

        try:
            conn = snowflake.connector.connect(**self._credentials.to_connection_kwargs())
        except Exception as exc:  # noqa: BLE001 — translate to typed error
            # ``snowflake.connector`` raises specific subclasses; we
            # collapse to ``CatalogConnectionError`` so the typed
            # hierarchy stays clean. The underlying message is
            # surfaced verbatim via ``original_error``.
            raise CatalogConnectionError(
                message=f"Snowflake connection failed: {exc}",
                suggestions=[
                    "Verify SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / "
                    "SNOWFLAKE_PASSWORD env vars are set.",
                    "If using SSO, ensure a fresh ID-token via `fluid verify snowflake`.",
                    "If the account is behind a corporate proxy, set "
                    "HTTPS_PROXY before invoking the catalog tool.",
                ],
                original_error=exc,
            ) from exc
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover — defensive cleanup
                pass

    # -----------------------------------------------------------------
    # CatalogAdapter ABC — list_tables / get_table / get_lineage /
    #                       list_glossary_terms
    # -----------------------------------------------------------------

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """Enumerate tables under ``scope`` via INFORMATION_SCHEMA.TABLES.

        Returns lightweight :class:`CatalogTable` instances populated
        with identity + description + owner + tag fields; the
        per-column / per-FK / per-lineage detail is fetched on demand
        through :meth:`get_table` to keep large-schema enumeration
        fast.
        """
        if not scope.database:
            raise CatalogConfigError(
                message="Snowflake CatalogScope requires 'database'.",
                suggestions=[
                    "Pass scope.database='YOUR_DB'.",
                    "Snowflake catalog enumeration is database-scoped.",
                ],
            )

        # Snowflake's INFORMATION_SCHEMA is database-scoped — qualify
        # the view directly via quoted-identifier inlining. We whitelist
        # the database name shape in :func:`_quote_ident` so this can't
        # turn into SQL injection even if the operator passes a hostile
        # database name.
        db_quoted = _quote_ident(scope.database, kind="database")
        schema_name = scope.schema_name or "PUBLIC"

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                # Filter on TABLE_TYPE so we exclude views / materialised
                # views from base table listings — adapters expose those
                # via separate methods in v1.6+ if needed.
                base_sql = """
                    SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME,
                           COMMENT, TABLE_OWNER, LAST_ALTERED
                      FROM {database}.INFORMATION_SCHEMA.TABLES
                     WHERE TABLE_SCHEMA  = %s
                       AND TABLE_TYPE    = 'BASE TABLE'
                """.replace("{database}", db_quoted)  # nosec B608
                if scope.tables:
                    placeholders = ", ".join("%s" for _ in scope.tables)
                    cur.execute(
                        base_sql + f" AND TABLE_NAME IN ({placeholders})",
                        [schema_name, *scope.tables],
                    )
                else:
                    cur.execute(base_sql, [schema_name])
                rows = cur.fetchall()
            except Exception as exc:
                raise self._translate_query_error(exc, scope=scope) from exc
            finally:
                cur.close()

        return [
            CatalogTable(
                fqn=f"{db}.{sch}.{name}",
                database=db,
                schema_name=sch,
                name=name,
                description=comment,
                owner=owner,
                last_modified=last_altered,
            )
            for (db, sch, name, comment, owner, last_altered) in rows
        ]

    def get_table(self, fqn: str) -> CatalogTable:
        """Return full metadata for one Snowflake FQN.

        Issues four metadata reads in sequence: TABLES (header),
        COLUMNS (per-column shape), TABLE_CONSTRAINTS +
        REFERENTIAL_CONSTRAINTS (PK / FK), TAG_REFERENCES (Horizon
        tags). Lineage is fetched separately via :meth:`get_lineage`
        to keep table-detail callers from paying the lineage cost
        unless they want it.
        """
        db, sch, name = self._parse_fqn(fqn)
        db_quoted = _quote_ident(db, kind="database")

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                # Header
                header_sql = """
                    SELECT COMMENT, TABLE_OWNER, LAST_ALTERED
                      FROM {database}.INFORMATION_SCHEMA.TABLES
                     WHERE TABLE_SCHEMA  = %s
                       AND TABLE_NAME    = %s
                    """.replace("{database}", db_quoted)  # nosec B608
                cur.execute(
                    header_sql,
                    [sch, name],
                )
                header = cur.fetchone()
                if header is None:
                    raise CatalogConnectionError(
                        message=f"Snowflake table not found: {fqn}",
                        suggestions=[
                            "Confirm the table exists and your role can inspect it.",
                            "Confirm your role has USAGE on the database and schema.",
                        ],
                    )
                comment, owner, last_altered = header

                # Columns
                columns_sql = """
                    SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                           COMMENT, ORDINAL_POSITION
                      FROM {database}.INFORMATION_SCHEMA.COLUMNS
                     WHERE TABLE_SCHEMA  = %s
                       AND TABLE_NAME    = %s
                     ORDER BY ORDINAL_POSITION
                    """.replace("{database}", db_quoted)  # nosec B608
                cur.execute(
                    columns_sql,
                    [sch, name],
                )
                column_rows = cur.fetchall()

                # Primary keys, FKs, tags — all OPTIONAL metadata
                # (Snowflake constraints are informational; tags
                # require IMPORTED PRIVILEGES on SNOWFLAKE shared
                # database). Wrap each in safe_metadata_call so a
                # missing view / privilege gap doesn't block the
                # whole get_table call. Pattern 1.
                pk_columns = safe_metadata_call(
                    lambda: self._fetch_primary_key(cur, db, sch, name),
                    fallback=[],
                    description="snowflake primary-key fetch",
                    log_target=fqn,
                )
                foreign_keys = safe_metadata_call(
                    lambda: self._fetch_foreign_keys(cur, db, sch, name),
                    fallback=[],
                    description="snowflake foreign-key fetch",
                    log_target=fqn,
                )
                tags = safe_metadata_call(
                    lambda: self._fetch_tags(cur, db, sch, name),
                    fallback={},
                    description="snowflake tag fetch",
                    log_target=fqn,
                )
            except (CatalogConnectionError, CatalogPermissionError):
                raise
            except Exception as exc:
                raise self._translate_query_error(exc, fqn=fqn) from exc
            finally:
                cur.close()

        columns = [
            CatalogColumn(
                name=col_name,
                data_type=data_type,
                nullable=(is_nullable == "YES"),
                description=col_comment,
                primary_key=col_name in pk_columns,
                # Sensitivity tags propagate up from the Horizon
                # classification on the column. We don't query
                # SYSTEM$CLASSIFY here — it's a separate per-column
                # round-trip that's expensive on large schemas; the
                # umbrella tag list is sufficient for v1.5 Sprint A.
            )
            for (col_name, data_type, is_nullable, col_comment, _ord) in column_rows
        ]

        return CatalogTable(
            fqn=fqn,
            database=db,
            schema_name=sch,
            name=name,
            description=comment,
            owner=owner,
            tags=tags,
            primary_key_columns=list(pk_columns),
            foreign_keys=foreign_keys,
            columns=columns,
            last_modified=last_altered,
        )

    def get_lineage(self, fqn: str) -> CatalogLineage:
        """Return upstream + downstream lineage from
        ACCOUNT_USAGE.OBJECT_DEPENDENCIES.

        Requires ``IMPORTED PRIVILEGES`` on the ``SNOWFLAKE`` shared
        database. When the privilege is missing, raises
        :class:`CatalogPermissionError` with the exact GRANT SQL —
        the adapter does not silently return empty lineage.
        """
        db, sch, name = self._parse_fqn(fqn)

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT REFERENCED_DATABASE, REFERENCED_SCHEMA,
                           REFERENCED_OBJECT_NAME, REFERENCED_OBJECT_DOMAIN
                      FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES
                     WHERE REFERENCING_DATABASE       = %s
                       AND REFERENCING_SCHEMA         = %s
                       AND REFERENCING_OBJECT_NAME    = %s
                    """,
                    [db, sch, name],
                )
                upstream_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT REFERENCING_DATABASE, REFERENCING_SCHEMA,
                           REFERENCING_OBJECT_NAME, REFERENCING_OBJECT_DOMAIN
                      FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES
                     WHERE REFERENCED_DATABASE       = %s
                       AND REFERENCED_SCHEMA         = %s
                       AND REFERENCED_OBJECT_NAME    = %s
                    """,
                    [db, sch, name],
                )
                downstream_rows = cur.fetchall()
            except Exception as exc:
                # Most lineage failures are insufficient privilege on
                # SNOWFLAKE.ACCOUNT_USAGE; surface the exact grant.
                raise self._translate_lineage_error(exc) from exc
            finally:
                cur.close()

        return CatalogLineage(
            upstream=[
                LineageRef(
                    fqn=f"{u_db}.{u_sch}.{u_name}",
                    kind="upstream",
                    transformation_type=u_domain,
                )
                for (u_db, u_sch, u_name, u_domain) in upstream_rows
            ],
            downstream=[
                LineageRef(
                    fqn=f"{d_db}.{d_sch}.{d_name}",
                    kind="downstream",
                    transformation_type=d_domain,
                )
                for (d_db, d_sch, d_name, d_domain) in downstream_rows
            ],
        )

    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """Snowflake Horizon does not (as of 2026-04) expose a public
        business-glossary API distinct from object tags / comments.

        Returns an empty list. Glossary signal flows through
        :attr:`CatalogTable.tags` and :attr:`CatalogColumn.description`
        instead. When Snowflake ships the rumoured Glossary API in a
        future release, this method becomes a thin wrapper over it.
        """
        return []

    # -----------------------------------------------------------------
    # FQN parsing + audit context
    # -----------------------------------------------------------------

    @staticmethod
    def _parse_fqn(fqn: str) -> tuple[str, str, str]:
        """Split a fully-qualified Snowflake table name.

        Three-part dotted form: ``DATABASE.SCHEMA.TABLE``. Surfaces a
        :class:`CatalogConfigError` with the expected shape on
        malformed input — defends against the common mistake of
        passing a two-part ``SCHEMA.TABLE`` in.
        """
        parts = fqn.split(".")
        if len(parts) != 3:
            raise CatalogConfigError(
                message=f"Snowflake FQN must be DATABASE.SCHEMA.TABLE; got {fqn!r}",
                suggestions=[
                    "Provide all three parts: DEMO_DB.SEEDED.ORDERS.",
                ],
            )
        return parts[0], parts[1], parts[2]

    def audit_context(self) -> Dict[str, Any]:
        """Override that adds the Snowflake account locator (no creds).

        The account locator is non-sensitive (it's an account
        identifier, not authentication material) and lets the
        forensic trail distinguish events from different Snowflake
        accounts. ``user`` and ``role`` are also non-sensitive and
        useful for audit-trail attribution. ``auth_method`` lets
        operators see which auth path was used (helpful for
        rotation triage).
        """
        ctx = super().audit_context()
        ctx["account"] = self._credentials.account
        ctx["user"] = self._credentials.user
        if self._credentials.role:
            ctx["role"] = self._credentials.role
        ctx["auth_method"] = self._credentials.auth_method
        return ctx

    # -----------------------------------------------------------------
    # Helpers — primary keys / foreign keys / tag reads
    # -----------------------------------------------------------------

    def _fetch_primary_key(self, cur: Any, db: str, sch: str, name: str) -> List[str]:
        """Return PK column names by joining
        TABLE_CONSTRAINTS + KEY_COLUMN_USAGE.

        Snowflake constraints are *informational* (not enforced),
        and many databases never populate ``KEY_COLUMN_USAGE`` —
        the view exists but returns zero rows, OR the view itself
        isn't materialised in the customer's account. Soft-fail
        with an empty list rather than blocking ``get_table``;
        the modeler agent already handles "no PK declared" via
        column-name heuristics.
        """
        db_quoted = _quote_ident(db, kind="database")
        try:
            pk_sql = """
                SELECT KCU.COLUMN_NAME
                  FROM {database}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS  TC
                  JOIN {database}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE   KCU
                    ON TC.CONSTRAINT_NAME = KCU.CONSTRAINT_NAME
                 WHERE TC.CONSTRAINT_TYPE = 'PRIMARY KEY'
                   AND TC.TABLE_SCHEMA    = %s
                   AND TC.TABLE_NAME      = %s
                 ORDER BY KCU.ORDINAL_POSITION
                """.replace("{database}", db_quoted)  # nosec B608
            cur.execute(
                pk_sql,
                [sch, name],
            )
            return [row[0] for row in cur.fetchall()]
        except Exception as exc:  # pragma: no cover — schema-driven
            _log.debug(
                "fluid.copilot.catalog.snowflake.pk.skipped: %s.%s.%s — %s",
                db,
                sch,
                name,
                exc,
            )
            return []

    def _fetch_foreign_keys(
        self, cur: Any, db: str, sch: str, name: str
    ) -> List[CatalogForeignKey]:
        """Group FK columns by constraint name.

        Same caveat as :meth:`_fetch_primary_key`: Snowflake FK
        declarations are informational and frequently absent.
        Soft-fail with an empty list when the metadata views are
        missing or the user lacks ``REFERENCES`` privilege — the
        modeler agent has heuristic FK inference as a fallback.
        """
        db_quoted = _quote_ident(db, kind="database")
        try:
            fk_sql = """
                SELECT TC.CONSTRAINT_NAME,
                       KCU.COLUMN_NAME,
                       RC.UNIQUE_CONSTRAINT_CATALOG,
                       RC.UNIQUE_CONSTRAINT_SCHEMA,
                       KCU2.TABLE_NAME,
                       KCU2.COLUMN_NAME
                  FROM {database}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS        TC
                  JOIN {database}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE         KCU
                    ON TC.CONSTRAINT_NAME = KCU.CONSTRAINT_NAME
                  JOIN {database}.INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS  RC
                    ON TC.CONSTRAINT_NAME = RC.CONSTRAINT_NAME
                  JOIN {database}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE         KCU2
                    ON RC.UNIQUE_CONSTRAINT_NAME = KCU2.CONSTRAINT_NAME
                   AND KCU.ORDINAL_POSITION       = KCU2.ORDINAL_POSITION
                 WHERE TC.CONSTRAINT_TYPE = 'FOREIGN KEY'
                   AND TC.TABLE_SCHEMA    = %s
                   AND TC.TABLE_NAME      = %s
                 ORDER BY TC.CONSTRAINT_NAME, KCU.ORDINAL_POSITION
                """.replace("{database}", db_quoted)  # nosec B608
            cur.execute(
                fk_sql,
                [sch, name],
            )
            rows = cur.fetchall()
        except Exception as exc:  # pragma: no cover — schema-driven
            _log.debug(
                "fluid.copilot.catalog.snowflake.fk.skipped: %s.%s.%s — %s",
                db,
                sch,
                name,
                exc,
            )
            return []
        grouped: Dict[str, Dict[str, Any]] = {}
        for constraint_name, from_col, ref_db, ref_sch, ref_table, to_col in rows:
            entry = grouped.setdefault(
                constraint_name,
                {
                    "to_table": f"{ref_db}.{ref_sch}.{ref_table}",
                    "from_columns": [],
                    "to_columns": [],
                },
            )
            entry["from_columns"].append(from_col)
            entry["to_columns"].append(to_col)
        return [
            CatalogForeignKey(
                constraint_name=name,
                from_columns=entry["from_columns"],
                to_table=entry["to_table"],
                to_columns=entry["to_columns"],
            )
            for name, entry in grouped.items()
        ]

    def _fetch_tags(self, cur: Any, db: str, sch: str, name: str) -> Dict[str, str]:
        """Read object-level Horizon tags from
        ACCOUNT_USAGE.TAG_REFERENCES.

        Tags become ``{tag_name: tag_value}``; downstream code maps
        these into industry-pack matching (``domain: party`` → telco
        TMF SID skeleton) and Fluid contract metadata.
        """
        try:
            cur.execute(
                """
                SELECT TAG_NAME, TAG_VALUE
                  FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES
                 WHERE OBJECT_DATABASE = %s
                   AND OBJECT_SCHEMA   = %s
                   AND OBJECT_NAME     = %s
                """,
                [db, sch, name],
            )
            return {tag_name: tag_value or "" for (tag_name, tag_value) in cur.fetchall()}
        except Exception as exc:  # pragma: no cover — privilege-driven
            # Tag reads are best-effort: missing IMPORTED PRIVILEGES
            # on SNOWFLAKE shouldn't fail the whole get_table call;
            # we log and return empty so the modeler still sees the
            # rest of the table's metadata.
            _log.debug(
                "fluid.copilot.catalog.snowflake.tags.skipped: %s.%s.%s — %s",
                db,
                sch,
                name,
                exc,
            )
            return {}

    # -----------------------------------------------------------------
    # Error translation — turn opaque SDK errors into typed ones
    # -----------------------------------------------------------------

    def _translate_query_error(
        self, exc: Exception, *, fqn: Optional[str] = None, scope: Optional[CatalogScope] = None
    ) -> CatalogError:
        """Pattern 7 — vendor-error → typed-exception translation.

        Defers to the shared :func:`translate_permission_or_connection_error`
        with Snowflake-specific GRANT-SQL hints. Keeping the
        Snowflake-specific suggestions inline here means the error
        message tells operators the EXACT GRANT to run, not a
        generic "check your privileges."
        """
        target = fqn or (f"{scope.database}.{scope.schema_name}" if scope else "<unknown>")
        db = target.split(".")[0]
        return translate_permission_or_connection_error(
            exc,
            target=target,
            permission_grant_hint=(
                f"GRANT USAGE ON DATABASE {db} TO ROLE <role>; "
                f"GRANT USAGE ON ALL SCHEMAS IN DATABASE {db} TO ROLE <role>; "
                f"GRANT REFERENCES ON ALL TABLES IN SCHEMA {target} TO ROLE <role>;"
            ),
            privilege_label="USAGE on the database/schema and REFERENCES on the table",
            connection_suggestions=[
                "Re-run with --verbose to see the full SQL.",
                "Verify the warehouse is running (XS is sufficient).",
            ],
        )

    def _translate_lineage_error(self, exc: Exception) -> CatalogError:
        """Lineage queries hit SNOWFLAKE.ACCOUNT_USAGE; the
        dominant failure is missing IMPORTED PRIVILEGES. Same
        translation pattern as :meth:`_translate_query_error` with
        the lineage-specific grant hint."""
        return translate_permission_or_connection_error(
            exc,
            target="SNOWFLAKE.ACCOUNT_USAGE",
            permission_grant_hint=(
                "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE <role>;"
            ),
            privilege_label="IMPORTED PRIVILEGES on the SNOWFLAKE shared database",
            connection_suggestions=[
                "ACCOUNT_USAGE has up to 90-min latency for new objects.",
                "If the table is fresh, retry after the latency window.",
            ],
        )


__all__ = ["SnowflakeCatalogAdapter"]
