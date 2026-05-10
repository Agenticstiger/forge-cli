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

"""Shared catalog-adapter patterns — applied uniformly to every adapter.

Validated against a live Snowflake telco demo schema (23 tables →
23 hubs / 33 links / 23 satellites / 23 OSI datasets, end-to-end).
Every pattern below is a direct lesson from that exercise.  Sprint A's
Snowflake + Unity adapters apply them; Sprint B's BigQuery / Dataplex /
Glue / DataHub / DMM adapters must apply them too.

The shared helpers exposed here are kept private to ``copilot.catalog``
(prefixed with ``_``) — they're internal scaffolding for adapter
implementers, not part of the public surface community contributors
import.

The nine patterns:

1. **Soft-fail on optional metadata views.** PK / FK / lineage / tags
   are frequently missing or unauthorised in real-world catalogs.
   Adapters log-and-skip rather than blocking the whole ``get_table``.
   Use :func:`safe_metadata_call` to wrap optional reads.

2. **Identifier validation + quoting.** Never inline raw user input
   into SQL / API paths. Use :func:`validate_and_quote_identifier`
   with the catalog's allowed-character class.

3. **Per-call connection lifecycle.** No persistent state in the
   adapter. ``_connect`` (Snowflake) / ``_client`` (Unity) opens
   fresh per call. The MCP server can never accumulate ambient
   catalog access this way.

4. **Lazy SDK import.** Inside the connection helper, ``import
   snowflake.connector`` / ``import databricks.sdk`` runs at
   first-call time. Module load is import-free so ``fluid --help``
   stays sub-second even with all extras installed.

5. **``from_resolver`` classmethod.** Construction-by-credential is
   the canonical dispatch path used by the MCP layer. Inline
   credential construction (``__init__(credentials=...)``) is for
   tests + direct CLI callers.

6. **Audit context excludes secrets.** Only non-sensitive identifiers
   (account, host, region, project, user, role, auth_method) appear
   in :meth:`CatalogAdapter.audit_context`. Token / password /
   client_secret / private-key fields are NEVER serialised.

7. **Vendor-error → typed-exception translation.** Each adapter has
   a ``_translate_query_error`` private method that maps the SDK's
   native exception class to one of the typed catalog errors
   (:class:`CatalogPermissionError`, :class:`CatalogConnectionError`,
   :class:`CatalogConfigError`). The translated error carries
   ``suggestions`` with the next-action operators need (the GRANT
   SQL, the env-var fix, etc.).

8. **Two-pass fetching.** ``list_tables`` returns lightweight
   :class:`CatalogTable` summaries (FQN + description + owner +
   last-modified). ``get_table`` fetches full detail (columns,
   PK/FK, tags, classifications). Callers that just need to
   enumerate the schema don't pay the per-table detail cost.

9. **Never fetch data values.** Only INFORMATION_SCHEMA-equivalent
   metadata reads. ``SELECT * FROM <table>`` is forbidden by
   contract — adapters that violate this fail the V1.5 security
   review.

For new adapter authors: copy ``snowflake.py`` as a template,
rename, then walk through this list checking that your adapter
honours each pattern. The Sprint A + B test suite will fail your
adapter at any point you skip — every pattern has a test.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional, TypeVar

from fluid_build.copilot.catalog.base import (
    CatalogConfigError,
    CatalogConnectionError,
    CatalogError,
    CatalogPermissionError,
)

_log = logging.getLogger(__name__)


# Conservative default identifier shape — covers Snowflake / BigQuery /
# Unity / Glue without quoting weirdness. Catalogs with looser rules
# (e.g., DataHub URN strings) override the regex per-adapter.
_DEFAULT_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_and_quote_identifier(
    value: str,
    *,
    kind: str,
    pattern: Optional[re.Pattern[str]] = None,
    quote_char: str = '"',
) -> str:
    """Whitelist-validate an identifier then return it double-quoted.

    Pattern 2 — identifier validation + quoting. Adapters call this
    whenever a user-controlled identifier (database / schema / table
    name) is interpolated into a query string. Defends against:

    * SQL injection: a hostile database name like ``X"; DROP TABLE Y; --``
      can't make it past the regex.
    * Syntax errors: a database name with a dot like ``MY.DB`` would
      otherwise produce ``"MY.DB".INFORMATION_SCHEMA.TABLES`` which is
      malformed Snowflake SQL — the caller learns immediately rather
      than getting an opaque cursor error.
    * Reserved-word collisions: double-quoting forces Snowflake /
      BigQuery to treat the identifier as a literal even when it
      shadows a keyword.

    Parameters
    ----------
    value:
        The raw identifier (typically from a CatalogScope or FQN).
    kind:
        Human-readable label (``"database"`` / ``"schema"`` /
        ``"table"``) — appears in the error message so operators
        know which input was rejected.
    pattern:
        Override the default ``[A-Za-z_][A-Za-z0-9_]*`` regex. Some
        catalogs (DataHub URN, Glue Lake Formation tags) accept
        looser shapes.
    quote_char:
        Almost always ``"``. Override only for catalogs with
        non-SQL-shaped identifiers.
    """
    rule = pattern or _DEFAULT_IDENTIFIER
    if not rule.match(value or ""):
        raise CatalogConfigError(
            message=f"Invalid {kind} identifier: {value!r}",
            suggestions=[
                f"{kind} names must match {rule.pattern} (no dots, spaces, or special characters).",
            ],
        )
    return f"{quote_char}{value}{quote_char}"


_T = TypeVar("_T")


def safe_metadata_call(
    func: Callable[[], _T],
    *,
    fallback: _T,
    description: str,
    log_target: Optional[Any] = None,
) -> _T:
    """Run ``func``; on any exception log + return ``fallback``.

    Pattern 1 — soft-fail on optional metadata views. Snowflake
    KEY_COLUMN_USAGE, Unity column-mask API, BigQuery
    PARTITIONS-by-name, DataHub relationship aspects — every catalog
    has metadata views that may simply not exist for a given table
    or may be unauthorised under the user's role. Wrapping each
    optional read with this helper turns "the whole ``get_table``
    call exploded" into "the whole ``get_table`` call returned
    less metadata" — exactly the V1.5 contract.

    Note: this helper is for OPTIONAL reads. Required reads
    (the table header, the column list) MUST raise on failure so
    the caller sees a clear error rather than an empty table —
    use ``_translate_query_error`` for those.

    Parameters
    ----------
    func:
        Zero-argument callable that performs the metadata read.
    fallback:
        Value to return when ``func`` raises. Typically an empty
        list / dict / ``None``.
    description:
        Human-readable description of what was being fetched. Goes
        into the debug log so operators can grep
        ``fluid.copilot.catalog.metadata.skipped`` and find the
        soft-fails.
    log_target:
        Optional structured log context (e.g., the FQN being
        inspected). Logged alongside the description so failures
        from different tables don't all look the same.
    """
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 — soft-fail by design
        _log.debug(
            "fluid.copilot.catalog.metadata.skipped: %s — %s [%s]",
            description,
            exc,
            log_target if log_target is not None else "(no context)",
        )
        return fallback


def translate_permission_or_connection_error(
    exc: Exception,
    *,
    target: str,
    permission_markers: tuple[str, ...] = (
        "Insufficient privileges",
        "PERMISSION_DENIED",
        "does not have",
        "Not authorized",
    ),
    not_found_markers: tuple[str, ...] = (
        "does not exist",
        "RESOURCE_DOES_NOT_EXIST",
        "Table not found",
    ),
    permission_grant_hint: Optional[str] = None,
    privilege_label: Optional[str] = None,
    connection_suggestions: Optional[list[str]] = None,
) -> CatalogError:
    """Map a vendor SDK exception to a typed catalog exception.

    Pattern 7 — vendor-error → typed-exception translation. Every
    catalog SDK uses its own exception hierarchy (snowflake.connector
    / databricks.sdk.errors / google.api_core.exceptions); this helper
    string-matches the message text to pick the right typed catalog
    error and attaches actionable ``suggestions``.

    String-matching is acceptable here because:

    * The patterns are well-established across each SDK's history.
    * Each adapter wraps its OWN call sites — the helper isn't
      trying to be a general-purpose vendor-error translator.
    * False positives (a "does not exist" message that wasn't a
      catalog-level not-found) gracefully degrade to a generic
      :class:`CatalogConnectionError` which is still informative.

    The helper covers the two dominant error classes
    (permission-denied + not-found); adapters with unique error
    shapes can call this for the common cases and add their own
    catch above for the edge cases.
    """
    msg = str(exc)
    if any(marker in msg for marker in permission_markers):
        suggestions: list[str] = []
        if privilege_label:
            suggestions.append(f"Confirm your role has the {privilege_label} privilege.")
        if permission_grant_hint:
            suggestions.append(
                f"Fix: run `{permission_grant_hint}` as a sufficiently-privileged user."
            )
        return CatalogPermissionError(
            message=f"Catalog denied a metadata read on {target}: {msg}",
            suggestions=suggestions or None,
            original_error=exc,
        )
    if any(marker in msg for marker in not_found_markers):
        return CatalogConnectionError(
            message=f"Catalog object not found: {target} ({msg})",
            suggestions=[
                f"Confirm the object exists at {target}.",
                "Verify any database/schema/catalog scope arguments are correct.",
            ],
            original_error=exc,
        )
    return CatalogConnectionError(
        message=f"Catalog metadata read failed for {target}: {msg}",
        suggestions=connection_suggestions
        or [
            "Re-run with --verbose to see the full SDK trace.",
            "Confirm network reachability to the catalog endpoint.",
        ],
        original_error=exc,
    )


__all__ = [
    "safe_metadata_call",
    "translate_permission_or_connection_error",
    "validate_and_quote_identifier",
]
