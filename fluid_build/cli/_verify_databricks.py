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

"""``fluid verify`` for a Databricks binding (Unity Catalog table plus its dbt jobs).

``fluid apply`` provisions a ``platform: databricks`` expose as a Unity Catalog
table, grants to its readers, and one job per dbt build that writes it. Stage 9
used to answer "unsupported" for it, which the summary counts as *not checked*.

What is checked is what the emitter's own pass says it provisions
(``databricks.managed_objects``), so verify never re-derives it:

1. **The table.** ``GET /api/2.1/unity-catalog/tables/{full_name}``. A 404 is
   CRITICAL ("run ``fluid apply``"). Columns are compared with the contract the
   way every verifier does: missing and a changed type are CRITICAL, an extra
   column is INFO, nullability is the constraint class. Live types go through
   the provider's own ``normalize_column_type``.
2. **Catalog access.** Each reader principal of the table needs USE CATALOG (or
   ALL PRIVILEGES) on the catalog, or it can be granted SELECT and still not
   reach the table. Read from the catalog's effective permissions. forge-cli
   never grants it (the catalog is not the product's), so the finding carries
   the statement an admin runs.
3. **Governance tags and comments.** The contract's governance labels are Unity Catalog
   tags (``databricks_entity_tag_assignment``) on the table and its schema, the product's
   identity and owner team are tags on the table, and each classified column carries its
   ``fluid_sensitivity`` column tag. Each expected tag must be there with its value; a tag
   someone else set is ignored. Read from ``GET .../entity-tag-assignments/{type}/{name}/tags``
   (``columns`` named ``catalog.schema.table.column``), which pages. The table's ``comment``
   and each described column's, from the GetTable already read, must be the contract's; on a
   table a dbt build writes the module sets none, so none is checked. A dbt
   ``CREATE OR REPLACE`` may drop a table's tags (Databricks does not say), so this is the
   gate: the fix is ``fluid apply``.
4. **Row filter and column masks.** The contract's ``columnRestrictions`` and ``rowFilters``
   are Unity Catalog SQL functions bound on the table (``databricks_access.py``), named by a
   hash of their definition. The table's ``row_filter.function_name`` / ``input_column_names``
   and each column's ``mask.function_name``, from the GetTable already read, must be the
   expected ones: a missing binding, or one to another function, is CRITICAL (the table is
   readable without what the contract promises), and the fix is ``fluid apply``, which binds
   them before it grants. Each bound function is read back too
   (``GET /api/2.1/unity-catalog/functions/{name}``): its owner must be this verify's identity
   or the table's owner, and its body, parameters and return type the expected ones, since a
   content-hashed name is computable from the contract and could have been taken by someone
   else's function. ``skip`` when the contract restricts nothing.
5. **Retention.** An expose with ``lifecycle.expire`` is accepted because its table is rebuilt in
   full and the contract's retention covers its Delta history (``databricks_retention.py``).
   The table's live ``delta.logRetentionDuration`` and ``delta.deletedFileRetentionDuration``
   (``properties`` and ``delta_runtime_properties_kvpairs`` of the GetTable already read;
   unset means Delta's 30 and 7 days) must not make the history longer than the contract's
   retention: CRITICAL if they do, with the ``ALTER TABLE`` that sets them. Predictive
   optimization, which runs ``VACUUM`` on managed tables, off for the table
   (``effective_predictive_optimization_flag``) is a warning: nothing removes the old files
   until a ``VACUUM`` runs. ``skip`` when the contract sets no expire.
6. **The jobs.** Each dbt build that writes the expose has exactly one job of
   its name. None means ``fluid apply`` did not run (or the job was deleted);
   several mean an apply without its state created a duplicate. A job that is paused while
   its table's masks or row filter are not in place fails too, with the apply that binds them
   and starts the job as the fix.

The client is ``databricks.sdk.WorkspaceClient`` built from ``sdk_config`` (the
credential policy the provider and the doctor share), used only through its
low-level ``api_client.do``, which is stable across SDK versions. The SDK is
imported lazily: it is the ``[databricks]`` extra, and ``fluid --help`` must not
load it.

Every failure is a result, never a traceback, and no message carries a token.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

#: The optional-dependency extra that brings databricks-sdk.
DATABRICKS_EXTRA_INSTALL = "pip install 'data-product-forge[databricks]'"

#: Privileges on a catalog that let a principal reach what is inside it.
_CATALOG_ACCESS = frozenset({"USE_CATALOG", "ALL_PRIVILEGES"})

#: A guard against a server that never stops paging.
_MAX_PAGES = 100
_MAX_MESSAGE = 240

ClientFactory = Callable[[], Any]


class _Unreadable(Exception):
    """A check could not run; the message is safe to show."""


def _default_client() -> Any:
    """``WorkspaceClient(**sdk_config(os.environ))``; the SDK imported only here.

    ``sdk_config`` first: it needs no SDK, and a credential problem should be
    reported as that, not as a missing extra.
    """
    from fluid_build.iac.providers.databricks_auth import sdk_config

    config = sdk_config(os.environ)
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(**config)


def _scrub(text: str) -> str:
    """One short line with every credential value in the environment removed."""
    from fluid_build.iac.providers.databricks_auth import credential_values
    from fluid_build.observability.secret_redactor import redact_secret_text

    # The environment's credentials and the named profile's (an OAuth secret has
    # no pattern a redactor could match).
    secrets = credential_values(os.environ)
    line = " ".join(redact_secret_text(str(text), extra_secrets=secrets).split())
    return line if len(line) <= _MAX_MESSAGE else line[: _MAX_MESSAGE - 3] + "..."


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_scrub(str(exc))}" if str(exc) else type(exc).__name__


def _is_not_found(exc: BaseException) -> bool:
    """A 404 from the SDK (``NotFound``) or from anything carrying a status."""
    for attr in ("status_code", "status", "http_status"):
        if getattr(exc, attr, None) == 404:
            return True
    if getattr(getattr(exc, "response", None), "status_code", None) == 404:
        return True
    if type(exc).__name__ in {"NotFound", "ResourceDoesNotExist"}:
        return True
    code = str(getattr(exc, "error_code", "") or "")
    return code in {"TABLE_DOES_NOT_EXIST", "RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"}


def _error(
    message: str,
    *,
    target: Optional[str] = None,
    exists: Optional[bool] = None,
    dimensions: Optional[Dict[str, Any]] = None,
    remediation: Sequence[str] = (),
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "error", "error": message, "exists": exists}
    if remediation:
        result["error"] = f"{message} {' '.join(remediation)}"
    if target:
        result["target"] = target
    if dimensions:
        result["dimensions"] = dimensions
    return result


# ── REST reads ──────────────────────────────────────────────────────────


def _get(client: Any, path: str, query: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    out = client.api_client.do("GET", path, query=dict(query) if query else None)
    return out if isinstance(out, dict) else {}


def _live_columns(table: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    from fluid_build.iac.providers.databricks import normalize_column_type

    live: Dict[str, Dict[str, Any]] = {}
    for col in table.get("columns") or []:
        if not isinstance(col, Mapping) or not col.get("name"):
            continue
        name = str(col["name"])
        raw = col.get("type_text") or col.get("type_name") or ""
        live[name.lower()] = {
            "name": name,
            "type": normalize_column_type(raw),
            "mode": "nullable" if col.get("nullable", True) is not False else "required",
        }
    return live


def _compare(
    live: Mapping[str, Dict[str, Any]], declared: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """The structure, type and nullability differences between the live table and the contract."""
    from fluid_build.iac.providers.databricks import normalize_column_type

    expected: Dict[str, Dict[str, Any]] = {}
    for col in declared:
        name = str(col["name"])
        expected[name.lower()] = {
            "name": name,
            "type": normalize_column_type(col.get("type")),
            "mode": "nullable" if col.get("nullable", True) else "required",
        }
    matching: List[str] = []
    missing: List[Any] = []
    type_mismatches: List[Any] = []
    mode_mismatches: List[Any] = []
    for key, want in expected.items():
        have = live.get(key)
        if have is None:
            missing.append(
                {"field": want["name"], "expected": {"type": want["type"], "mode": want["mode"]}}
            )
            continue
        matching.append(want["name"])
        if have["type"] != want["type"]:
            type_mismatches.append(
                {"field": want["name"], "expected": want["type"], "actual": have["type"]}
            )
        if have["mode"] != want["mode"]:
            mode_mismatches.append(
                {"field": want["name"], "expected": want["mode"], "actual": have["mode"]}
            )
    extra = [
        {"field": have["name"], "actual": {"type": have["type"], "mode": have["mode"]}}
        for key, have in live.items()
        if key not in expected
    ]
    return {
        "matching": matching,
        "missing": missing,
        "extra": extra,
        "type_mismatches": type_mismatches,
        "mode_mismatches": mode_mismatches,
        "total_expected": len(expected),
        "total_actual": len(live),
    }


def _quote_name(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def grant_statement(catalog: str, principal: str) -> str:
    """The statement that gives ``principal`` USE CATALOG, names backtick-quoted."""
    return f"GRANT USE CATALOG ON CATALOG {_quote_name(catalog)} TO {_quote_name(principal)}"


def _catalog_privileges(client: Any, catalog: str, principal: str) -> set:
    """The privileges ``principal`` effectively holds on ``catalog`` (following pages).

    The call is scoped to ``principal``, so every assignment it returns is one the
    principal holds. An assignment names the principal that CONVEYS the privilege,
    not the one asked about: asked about ``account users``, a live workspace
    (2026-11-01) answered with ``_workspace_users_workspace_<id>``'s USE_CATALOG. A
    filter on the asked-for name read that as no USE CATALOG at all.
    """
    path = f"/api/2.1/unity-catalog/effective-permissions/catalog/{quote(catalog, safe='')}"
    held: set = set()
    token: Optional[str] = None
    seen: set = set()
    for _ in range(_MAX_PAGES):
        query: Dict[str, Any] = {"principal": principal}
        if token:
            query["page_token"] = token
        page = _get(client, path, query)
        for assignment in page.get("privilege_assignments") or []:
            if not isinstance(assignment, Mapping):
                continue
            for priv in assignment.get("privileges") or []:
                name = priv.get("privilege") if isinstance(priv, Mapping) else priv
                if name:
                    held.add(str(name).upper())
        token = page.get("next_page_token") or None
        if not token or token in seen:
            break
        seen.add(token)
    return held


def _live_tags(client: Any, entity_type: str, full_name: str) -> Dict[str, str]:
    """The tags on a table or schema, ``{key: value}``, following every page.

    The list is paginated: keep reading while ``next_page_token`` is present, an
    empty page included, since the server may answer one with a token. A token seen
    twice, or more than ``_MAX_PAGES`` pages, is a server that never stops, and an
    error rather than a quiet stop (a stop would read as missing tags).
    """
    path = (
        f"/api/2.1/unity-catalog/entity-tag-assignments/{entity_type}/"
        f"{quote(full_name, safe='.')}/tags"
    )
    live: Dict[str, str] = {}
    token: Optional[str] = None
    seen: set = set()
    for _ in range(_MAX_PAGES):
        page = _get(client, path, {"page_token": token} if token else None)
        for entry in page.get("tag_assignments") or []:
            if isinstance(entry, Mapping) and entry.get("tag_key") is not None:
                live[str(entry["tag_key"])] = str(entry.get("tag_value") or "")
        token = page.get("next_page_token") or None
        if not token:
            return live
        if token in seen:
            break
        seen.add(token)
    raise _Unreadable(f"the tag list of {full_name} never ended")


def _jobs_named(client: Any, name: str) -> List[Dict[str, Any]]:
    """Every job whose name is exactly ``name`` (the ``name`` filter is a hint, not trusted)."""
    found: List[Dict[str, Any]] = []
    token: Optional[str] = None
    seen: set = set()
    for _ in range(_MAX_PAGES):
        query: Dict[str, Any] = {"name": name, "limit": 100}
        if token:
            query["page_token"] = token
        page = _get(client, "/api/2.2/jobs/list", query)
        for job in page.get("jobs") or []:
            settings = job.get("settings") if isinstance(job, Mapping) else None
            if isinstance(settings, Mapping) and settings.get("name") == name:
                found.append(dict(job))
        token = page.get("next_page_token") or None
        if not page.get("has_more") or not token or token in seen:
            break
        seen.add(token)
    return found


# ── Dimensions ──────────────────────────────────────────────────────────


def _access_dimension(client: Any, table: Any) -> Dict[str, Any]:
    catalog = table.full_name.split(".")[0]
    findings: List[Dict[str, Any]] = []
    for principal in table.readers:
        held = _catalog_privileges(client, catalog, principal)
        if not held & _CATALOG_ACCESS:
            fix = grant_statement(catalog, principal)
            findings.append(
                {
                    "principal": principal,
                    "catalog": catalog,
                    "message": (
                        f"{principal} has SELECT on {table.full_name} but no USE CATALOG on "
                        f"catalog {catalog}, so it cannot reach the table"
                    ),
                    "fix": fix,
                }
            )
    if not table.readers:
        message = "the table has no reader principals to check"
    elif findings:
        message = f"{len(findings)} reader(s) lack USE CATALOG on catalog {catalog}"
    else:
        message = f"all {len(table.readers)} reader(s) hold USE CATALOG on catalog {catalog}"
    return {
        "status": "fail" if findings else "pass",
        "message": message,
        "readers": list(table.readers),
        "findings": findings,
    }


def _residency_dimension(client: Any, table: Any) -> Dict[str, Any]:
    """The workspace's metastore region against the one the contract declares.

    ``skip`` when the contract declares no region: nothing was promised, so
    nothing can be broken (the sovereignty check is what asks for one).
    """
    declared = str(getattr(table, "region", "") or "")
    if not declared:
        return {
            "status": "skip",
            "message": "the contract declares no binding.location.region to check",
            "declared": None,
            "actual": None,
        }
    summary = _get(client, "/api/2.1/unity-catalog/metastore_summary")
    actual = str(summary.get("region") or "")
    if actual.lower() == declared.lower():
        return {
            "status": "pass",
            "message": f"the workspace's metastore is in {actual}, as declared",
            "declared": declared,
            "actual": actual,
        }
    shown = actual or "no region"
    return {
        "status": "fail",
        "message": (
            f"the contract declares region {declared} but the workspace's metastore is in {shown}"
        ),
        "declared": declared,
        "actual": actual or None,
        "fix": (
            f"Point DATABRICKS_HOST at a workspace in {declared}, or correct "
            "binding.location.region in the contract"
        ),
    }


def _live_properties(live_table: Mapping[str, Any]) -> Dict[str, Any]:
    """The table's properties as GetTable serves them: ``properties``, with the Delta runtime
    ones (``delta_runtime_properties_kvpairs``, the table's current state) over them."""
    props: Dict[str, Any] = {}
    declared = live_table.get("properties")
    if isinstance(declared, Mapping):
        props.update(declared)
    runtime = live_table.get("delta_runtime_properties_kvpairs")
    runtime = runtime.get("delta_runtime_properties") if isinstance(runtime, Mapping) else None
    if isinstance(runtime, Mapping):
        props.update(runtime)
    return props


def _predictive_optimization(live_table: Mapping[str, Any]) -> Optional[str]:
    """``ENABLE`` or ``DISABLE`` for the table, as Unity Catalog reports it (the effective flag,
    else the table's own unless it inherits); ``None`` when it does not say."""
    effective = live_table.get("effective_predictive_optimization_flag")
    value = effective.get("value") if isinstance(effective, Mapping) else None
    if not value:
        value = live_table.get("enable_predictive_optimization")
    value = str(value or "").upper()
    return value if value in {"ENABLE", "DISABLE"} else None


def _retention_dimension(table: Any, live_table: Mapping[str, Any]) -> Dict[str, Any]:
    """The table's live Delta history retention against the contract's ``lifecycle.retention``.

    ``skip`` when the contract sets no expire: nothing was promised. ``fail`` when the longer
    of ``delta.logRetentionDuration`` and ``delta.deletedFileRetentionDuration`` (each at
    Delta's default when unset) exceeds the contract's retention, or one is not an interval.
    ``warn`` (not a failure) when predictive optimization is off for the table.
    """
    from fluid_build.iac.providers.databricks_retention import (
        DELETED_FILE_RETENTION,
        LOG_RETENTION,
        days_text,
        history_seconds,
    )

    promised = getattr(table, "retention_seconds", None)
    if promised is None:
        return {
            "status": "skip",
            "message": "the contract sets no lifecycle.expire to check the Delta history against",
            "findings": [],
            "warnings": [],
        }
    period = getattr(table, "retention_period", None)
    optimization = _predictive_optimization(live_table)
    quoted = ".".join(_quote_name(part) for part in table.full_name.split("."))
    schema = ".".join(_quote_name(part) for part in table.full_name.split(".")[:2])
    warnings: List[str] = []
    if optimization == "DISABLE":
        warnings.append(
            f"predictive optimization is off for {table.full_name}, so nothing runs VACUUM and "
            "the old data files stay past the retention. Enable it "
            f"(ALTER SCHEMA {schema} ENABLE PREDICTIVE OPTIMIZATION), or schedule "
            f"VACUUM {quoted}"
        )
    result: Dict[str, Any] = {
        "retention": period,
        "predictive_optimization": optimization,
        "findings": [],
        "warnings": warnings,
    }
    try:
        effective = history_seconds(_live_properties(live_table))
    except ValueError as exc:
        return {
            **result,
            "status": "fail",
            "message": f"a Delta retention property of {table.full_name} cannot be read: {exc}",
            "findings": [{"message": str(exc)}],
            "fix": (
                f"Set {LOG_RETENTION} and {DELETED_FILE_RETENTION} on the table to "
                "'interval N days'"
            ),
        }
    result["history_seconds"] = effective
    if "properties" not in live_table and "delta_runtime_properties_kvpairs" not in live_table:
        warnings.append(
            f"Unity Catalog returned no table properties for {table.full_name}, so Delta's "
            f"defaults were assumed. Check them with SHOW TBLPROPERTIES {quoted}"
        )
    if effective > promised:
        days = max(promised // 86400, 1)
        fix = (
            f"ALTER TABLE {quoted} SET TBLPROPERTIES ('{LOG_RETENTION}' = 'interval {days} "
            f"days', '{DELETED_FILE_RETENTION}' = 'interval {days} days'), or raise "
            "lifecycle.retention"
        )
        message = (
            f"the Delta history of {table.full_name} is {days_text(effective)}, longer than "
            f"the contract's retention {period} ({days_text(promised)})"
        )
        return {
            **result,
            "status": "fail",
            "message": message,
            "findings": [{"message": message}],
            "fix": fix,
        }
    message = (
        f"the Delta history of {table.full_name} is {days_text(effective)}, within the "
        f"contract's retention {period} ({days_text(promised)})"
    )
    if warnings:
        return {**result, "status": "warn", "message": message, "fix": warnings[0]}
    return {**result, "status": "pass", "message": message}


def _comment_findings(table: Any, live_table: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The table and column comments the module sets, against the live ones."""
    findings: List[Dict[str, Any]] = []
    label = f"table {table.full_name}"
    expected = getattr(table, "comment", None)
    if expected is not None and (live_table.get("comment") or None) != expected:
        findings.append(
            {
                "entity": label,
                "comment": "table",
                "expected": expected,
                "actual": live_table.get("comment"),
                "message": f"{label} does not have the contract's description as its comment",
            }
        )
    live_columns = {
        str(col.get("name") or "").lower(): col
        for col in live_table.get("columns") or []
        if isinstance(col, Mapping)
    }
    for column, comment in getattr(table, "column_comments", ()):
        live = live_columns.get(column)
        if live is None:
            continue  # a missing column is the structure dimension's finding
        if (live.get("comment") or None) != comment:
            findings.append(
                {
                    "entity": f"column {table.full_name}.{column}",
                    "comment": column,
                    "expected": comment,
                    "actual": live.get("comment"),
                    "message": (
                        f"column {table.full_name}.{column} does not have the contract's "
                        "description as its comment"
                    ),
                }
            )
    return findings


def _tags_dimension(client: Any, table: Any, live_table: Mapping[str, Any]) -> Dict[str, Any]:
    """The tags and comments the module sets on the table, its schema and its columns,
    against the live ones.

    ``skip`` when the module sets none: nothing was promised. A tag set by someone
    else is ignored; a missing one, or one with another value, fails, and so does a
    comment that is not the contract's.
    """
    schema = table.full_name.rsplit(".", 1)[0]
    by_column: Dict[str, List[Any]] = {}
    for column, key, value in getattr(table, "column_tags", ()):
        by_column.setdefault(column, []).append((key, value))
    wanted = (
        ("schemas", schema, f"schema {schema}", tuple(getattr(table, "schema_tags", ()))),
        ("tables", table.full_name, f"table {table.full_name}", tuple(getattr(table, "tags", ()))),
    ) + tuple(
        (
            "columns",
            f"{table.full_name}.{column}",
            f"column {table.full_name}.{column}",
            tuple(tags),
        )
        for column, tags in sorted(by_column.items())
    )
    comments = (1 if getattr(table, "comment", None) is not None else 0) + len(
        getattr(table, "column_comments", ())
    )
    total = sum(len(expected) for *_rest, expected in wanted)
    if not total and not comments:
        return {
            "status": "skip",
            "message": "the contract sets no tags or comments to check",
            "findings": [],
        }
    findings: List[Dict[str, Any]] = []
    for entity_type, name, label, expected in wanted:
        if not expected:
            continue
        live = _live_tags(client, entity_type, name)
        for key, value in expected:
            if key not in live:
                findings.append(
                    {
                        "entity": label,
                        "tag": key,
                        "expected": value,
                        "actual": None,
                        "message": f"{label} has no tag {key} (expected {value!r})",
                    }
                )
            elif live[key] != value:
                findings.append(
                    {
                        "entity": label,
                        "tag": key,
                        "expected": value,
                        "actual": live[key],
                        "message": (
                            f"{label} has tag {key}={live[key]!r}, the contract says {value!r}"
                        ),
                    }
                )
    findings.extend(_comment_findings(table, live_table))
    checked = f"{total} tag(s) and {comments} comment(s)"
    if not findings:
        return {
            "status": "pass",
            "message": f"all {checked} are on the table, its schema and its columns",
            "findings": [],
        }
    return {
        "status": "fail",
        "message": f"{len(findings)} of {checked} missing or changed",
        "findings": findings,
        "fix": (
            "Run fluid apply to set them again; a dbt CREATE OR REPLACE may have dropped "
            "them (whether Databricks keeps tags across it is undocumented)"
        ),
    }


def _enforcement_dimension(
    table: Any, live_table: Mapping[str, Any], client: Any = None
) -> Dict[str, Any]:
    """The row filter and column masks the contract asks for, against the table's live ones.

    Expected function names carry a hash of their definition, so a changed predicate or
    rule is a different name and shows here as "different function". ``skip`` when the
    contract declares no restriction: nothing was promised. A binding the contract does
    not ask for is not checked.

    A bound function is read back too (``client`` given): its owner must be the identity
    this verify runs as or the table's owner, and its body, parameters and return type the
    expected ones. The hashed name is computable from the contract, so a function someone else
    created under it first would pass a comparison of names.
    """
    from fluid_build.iac.providers.databricks_access import same_function

    row_filter = getattr(table, "row_filter", None)
    masks = tuple(getattr(table, "masks", ()))
    if row_filter is None and not masks:
        return {
            "status": "skip",
            "message": "the contract declares no column restriction or row filter to check",
            "findings": [],
        }
    findings: List[Dict[str, Any]] = []
    if row_filter is not None:
        function, columns = row_filter
        live = live_table.get("row_filter") or {}
        actual = str(live.get("function_name") or "") if isinstance(live, Mapping) else ""
        inputs = [str(c).lower() for c in (live.get("input_column_names") or [])]
        if not actual:
            findings.append(
                {
                    "kind": "row_filter",
                    "expected": function,
                    "actual": None,
                    "message": f"{table.full_name} has no row filter, expected {function}",
                }
            )
        elif not same_function(actual, function):
            findings.append(
                {
                    "kind": "row_filter",
                    "expected": function,
                    "actual": actual,
                    "message": (
                        f"{table.full_name} has row filter {actual}, the contract's is {function}"
                    ),
                }
            )
        elif inputs != [c.lower() for c in columns]:
            findings.append(
                {
                    "kind": "row_filter",
                    "expected": function,
                    "actual": actual,
                    "message": (
                        f"row filter {function} on {table.full_name} reads columns "
                        f"{', '.join(inputs) or 'none'}, expected {', '.join(columns)}"
                    ),
                }
            )
    live_columns = {
        str(c.get("name") or "").lower(): c
        for c in live_table.get("columns") or []
        if isinstance(c, Mapping)
    }
    for column, function in masks:
        mask = (live_columns.get(column.lower()) or {}).get("mask") or {}
        actual = str(mask.get("function_name") or "") if isinstance(mask, Mapping) else ""
        if not actual:
            findings.append(
                {
                    "kind": "mask",
                    "column": column,
                    "expected": function,
                    "actual": None,
                    "message": f"{table.full_name}.{column} has no mask, expected {function}",
                }
            )
        elif not same_function(actual, function):
            findings.append(
                {
                    "kind": "mask",
                    "column": column,
                    "expected": function,
                    "actual": actual,
                    "message": (
                        f"{table.full_name}.{column} has mask {actual}, the contract's is "
                        f"{function}"
                    ),
                }
            )
    if client is not None and getattr(table, "enforcement", None) is not None:
        findings += _function_findings(table, live_table, client, findings)
    total = len(masks) + (1 if row_filter is not None else 0)
    if not findings:
        return {
            "status": "pass",
            "message": f"all {total} row filter and column mask binding(s) are in place",
            "findings": [],
        }
    return {
        "status": "fail",
        "message": (
            f"{len(findings)} of {total} row filter and column mask binding(s) missing or "
            "different: the table is readable without what the contract promises"
        ),
        "findings": findings,
        "fix": (
            "Run fluid apply: it binds the row filter and masks (before any grant it adds) "
            "and revokes SELECT from the restricted principals of a table it cannot bind"
        ),
    }


def _function_findings(
    table: Any, live_table: Mapping[str, Any], client: Any, named: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """A finding for each expected, correctly named binding whose function is not the expected one."""
    from fluid_build.iac.providers.databricks_enforce import (
        deploy_identity,
        function_problem,
        read_function,
    )

    enforcement = table.enforcement
    wrong = {f.get("expected") for f in named}
    expected = []
    if enforcement.row_filter is not None:
        expected.append(("row filter", enforcement.row_filter.function))
    expected += [(f"mask on {m.column}", m.function) for m in enforcement.masks]
    expected = [(label, fn) for label, fn in expected if fn.full_name not in wrong]
    if not expected:
        return []
    owners = {str(live_table.get("owner") or "")}
    try:
        owners.add(deploy_identity(client))
    except Exception:  # noqa: BLE001 — the table's owner alone is then the reference
        pass
    owners.discard("")
    out: List[Dict[str, Any]] = []
    for label, fn in expected:
        try:
            info = read_function(client, fn.full_name)
        except Exception as exc:  # noqa: BLE001 — a function that cannot be read is not verified
            raise _Unreadable(f"could not read function {fn.full_name}: {_failure(exc)}") from exc
        problem = function_problem(info, fn, owners or {"unknown"})
        if problem is not None:
            out.append(
                {
                    "kind": "function",
                    "expected": fn.full_name,
                    "actual": fn.full_name,
                    "message": f"the {label} of {table.full_name}: {problem}",
                }
            )
    return out


def _paused(job: Mapping[str, Any]) -> bool:
    """Whether the job's schedule or continuous block is ``PAUSED``."""
    settings = job.get("settings")
    if not isinstance(settings, Mapping):
        return False
    return any(
        isinstance(settings.get(block), Mapping) and settings[block].get("pause_status") == "PAUSED"
        for block in ("schedule", "continuous")
    )


def _jobs_dimension(
    client: Any,
    expose_id: str,
    contract: Mapping[str, Any],
    jobs: Any,
    enforcement: Optional[Mapping[str, Any]] = None,
    table_name: str = "",
) -> Any:
    """The dimension for the dbt jobs that write ``expose_id``; ``None`` when there are none.

    A job that is paused while the table's masks or row filter are not in place is the one
    ``fluid apply`` creates paused for that reason (``DatabricksIacPlugin.emit``): it is a finding
    whose fix is the apply that binds them and starts it.
    """
    from fluid_build.util.contract import builds_writing_expose

    writers = {
        str(b.get("id")) for b in builds_writing_expose(expose_id, contract.get("builds") or [])
    }
    wanted = [job for job in jobs if job.build_id in writers]
    if not wanted:
        return None
    findings: List[Dict[str, Any]] = []
    entries: List[Dict[str, Any]] = []
    for job in wanted:
        hits = _jobs_named(client, job.name)
        ids = [str(h.get("job_id")) for h in hits if h.get("job_id") is not None]
        entries.append({"build": job.build_id, "name": job.name, "found": len(hits), "ids": ids})
        if not hits:
            findings.append(
                {
                    "build": job.build_id,
                    "message": f"no job named {job.name!r} for build {job.build_id}: "
                    "run fluid apply",
                }
            )
        elif len(hits) == 1 and _paused(hits[0]) and (enforcement or {}).get("status") == "fail":
            findings.append(
                {
                    "build": job.build_id,
                    "message": (
                        f"job {job.name!r} is paused because {table_name or 'its table'} is not "
                        "protected (the row_filter_masks dimension fails): run fluid apply, which "
                        "binds the masks and row filter, then starts the job"
                    ),
                }
            )
        elif len(hits) > 1:
            findings.append(
                {
                    "build": job.build_id,
                    "message": (
                        f"{len(hits)} jobs are named {job.name!r} (ids {', '.join(ids)}): an "
                        "apply without its state created a duplicate; keep the one in state "
                        "and delete the others"
                    ),
                }
            )
    return {
        "status": "fail" if findings else "pass",
        "message": (
            f"{len(findings)} of {len(wanted)} job(s) wrong"
            if findings
            else f"{len(wanted)} job(s) present, one each"
        ),
        "jobs": entries,
        "findings": findings,
    }


def _severity(
    diff: Mapping[str, Any],
    access: Optional[Mapping[str, Any]],
    jobs: Optional[Mapping[str, Any]],
    missing_table: str = "",
    residency: Optional[Mapping[str, Any]] = None,
    tags: Optional[Mapping[str, Any]] = None,
    enforcement: Optional[Mapping[str, Any]] = None,
    retention: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The shared drift grading, raised to CRITICAL by a missing table, reader, job,
    a workspace outside the declared region, a governance tag that is not there or a row
    filter or column mask that is not bound."""
    from fluid_build.cli.verify import assess_drift_severity

    severity = assess_drift_severity(
        missing_fields=diff["missing"],
        extra_fields=diff["extra"],
        type_mismatches=diff["type_mismatches"],
        mode_mismatches=diff["mode_mismatches"],
        region_match=True,
    )
    reasons: List[str] = []
    actions: List[str] = []
    if missing_table:
        reasons.append(f"table {missing_table} does not exist")
        actions.append("Run fluid apply to create it")
    for finding in (access or {}).get("findings") or []:
        reasons.append(finding["message"])
        actions.append(f"An admin of catalog {finding['catalog']} runs: {finding['fix']}")
    for finding in (jobs or {}).get("findings") or []:
        reasons.append(finding["message"])
        actions.append(finding["message"].split(": ", 1)[-1])
    if (residency or {}).get("status") == "fail":
        reasons.append(residency["message"])
        actions.append(residency["fix"])
    if (tags or {}).get("status") == "fail":
        reasons.append(tags["message"] + ": " + "; ".join(f["message"] for f in tags["findings"]))
        actions.append(tags["fix"])
    if (enforcement or {}).get("status") == "fail":
        reasons.append(
            enforcement["message"] + ": " + "; ".join(f["message"] for f in enforcement["findings"])
        )
        actions.append(enforcement["fix"])
    if (retention or {}).get("status") == "fail":
        reasons.append(retention["message"])
        actions.append(retention["fix"])
    if not reasons:
        return severity
    already = severity["level"] == "CRITICAL"
    return {
        "level": "CRITICAL",
        "impact": "HIGH",
        "symbol": "🔴",
        "remediation": "MANUAL_INTERVENTION_REQUIRED",
        "reason": "; ".join(([severity["reason"]] if already else []) + reasons),
        "actions": (list(severity["actions"]) if already else []) + actions,
    }


def _shape_dimensions(diff: Mapping[str, Any], full_name: str) -> Dict[str, Any]:
    from fluid_build.cli._verify_dimensions import schema_dimensions

    return {
        **schema_dimensions(diff),
        "constraints": {
            "status": "pass" if not diff["mode_mismatches"] else "fail",
            "mismatches": diff["mode_mismatches"],
        },
        "location": {"status": "pass", "expected": full_name, "actual": full_name, "message": None},
    }


def verify_databricks_expose(
    expose_id: str,
    expose: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    reference_only: bool = False,
    client_factory: Optional[ClientFactory] = None,
) -> Dict[str, Any]:
    """Verify one Databricks-bound expose. Returns the shape the other verifiers return.

    ``status`` is ``match``, ``mismatch`` (with a ``severity``), ``error`` or
    ``unsupported`` (the emitter manages no table for this expose). Never raises
    for a configuration, SDK or HTTP failure: the reason is the result.
    ``reference_only`` downgrades a missing table to the INFO every verifier
    gives one (an external pipeline owns its creation).
    ``client_factory()`` defaults to ``WorkspaceClient(**sdk_config(os.environ))``.
    """
    from fluid_build.cli._common import resolve_env_templates_in_contract
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.providers.databricks import managed_objects

    try:
        # What fluid apply provisions, from the contract as apply reads it: with its
        # ``{{ env.X }}`` placeholders substituted.
        resolved = resolve_env_templates_in_contract(copy.deepcopy(dict(contract or {})))
        tables, jobs = managed_objects(resolved, os.environ)
    except UnsupportedBindingError as exc:
        return _error(f"{exc} ({exc.kind})", remediation=tuple(exc.remediation))
    except Exception as exc:  # noqa: BLE001 — the emitter's refusal is the result
        return _error(f"Could not work out what fluid apply provisions: {_failure(exc)}")
    table = next((t for t in tables if t.expose_id == expose_id), None)
    if table is None:
        return {
            "status": "unsupported",
            "error": "The Databricks provider manages no Unity Catalog table for this expose",
        }
    target = table.full_name

    try:
        client = (client_factory or _default_client)()
    except UnsupportedBindingError as exc:
        return _error(f"{exc} ({exc.kind})", target=target, remediation=tuple(exc.remediation))
    except ImportError:
        return _error(
            "databricks-sdk is not installed: install the [databricks] extra: "
            f"{DATABRICKS_EXTRA_INSTALL}",
            target=target,
        )
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        return _error(f"Could not create the Databricks client: {_failure(exc)}", target=target)

    declared = [dict(c) for c in table.columns]
    try:
        live_table = _get(client, f"/api/2.1/unity-catalog/tables/{quote(target, safe='.')}")
    except Exception as exc:  # noqa: BLE001 — every SDK/HTTP failure is reported, not raised
        if not _is_not_found(exc):
            return _error(f"Unity Catalog GetTable {target} failed: {_failure(exc)}", target=target)
        if reference_only:
            return _error(
                f"Table not found: {target} (run fluid apply)", target=target, exists=False
            )
        diff = _compare({}, declared)
        severity = _severity(diff, None, None, missing_table=target)
        return {
            "status": "mismatch",
            "exists": False,
            "table_id": target,
            "target": target,
            "severity": severity,
            "dimensions": _shape_dimensions(diff, target),
            "metadata": {"num_rows": None, "created": None, "modified": None},
        }

    diff = _compare(_live_columns(live_table), declared)
    dimensions = _shape_dimensions(diff, target)
    try:
        access = _access_dimension(client, table)
        residency = _residency_dimension(client, table)
        tags = _tags_dimension(client, table, live_table)
        enforcement = _enforcement_dimension(table, live_table, client)
        retention = _retention_dimension(table, live_table)
        job_dim = _jobs_dimension(client, expose_id, contract, jobs, enforcement, target)
    except Exception as exc:  # noqa: BLE001 — a check that could not run is an error
        return _error(
            f"Databricks check for {target} could not run: {_failure(exc)}",
            target=target,
            exists=True,
            dimensions=dimensions,
        )
    dimensions["catalog_access"] = access
    dimensions["residency"] = residency
    dimensions["governance_tags"] = tags
    dimensions["row_filter_masks"] = enforcement
    dimensions["retention"] = retention
    if job_dim is not None:
        dimensions["jobs"] = job_dim

    has_issues = any(
        dimensions[name]["status"] == "fail"
        for name in (
            "structure",
            "types",
            "constraints",
            "catalog_access",
            "residency",
            "governance_tags",
            "row_filter_masks",
            "retention",
            "jobs",
        )
        if name in dimensions
    )
    return {
        "status": "mismatch" if has_issues else "match",
        "exists": True,
        "table_id": target,
        "target": target,
        "severity": _severity(
            diff,
            access,
            job_dim,
            residency=residency,
            tags=tags,
            enforcement=enforcement,
            retention=retention,
        ),
        "dimensions": dimensions,
        "metadata": {
            "num_rows": None,
            "created": _epoch_iso(live_table.get("created_at")),
            "modified": _epoch_iso(live_table.get("updated_at")),
        },
    }


def _epoch_iso(millis: Any) -> Optional[str]:
    """Unity Catalog timestamps are epoch milliseconds."""
    from datetime import datetime, timezone

    if not isinstance(millis, (int, float)) or isinstance(millis, bool):
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


__all__ = [
    "DATABRICKS_EXTRA_INSTALL",
    "grant_statement",
    "verify_databricks_expose",
]
