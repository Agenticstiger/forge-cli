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
3. **The jobs.** Each dbt build that writes the expose has exactly one job of
   its name. None means ``fluid apply`` did not run (or the job was deleted);
   several mean an apply without its state created a duplicate.

The client is ``databricks.sdk.WorkspaceClient`` built from ``sdk_config`` (the
credential policy the provider and the doctor share), used only through its
low-level ``api_client.do``, which is stable across SDK versions. The SDK is
imported lazily: it is the ``[databricks]`` extra, and ``fluid --help`` must not
load it.

Every failure is a result, never a traceback, and no message carries a token.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

LOG = logging.getLogger("fluid.cli.verify.databricks")

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
    """The privileges ``principal`` effectively holds on ``catalog`` (following pages)."""
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
            if str(assignment.get("principal") or "").lower() != principal.lower():
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


def _jobs_dimension(client: Any, expose_id: str, contract: Mapping[str, Any], jobs: Any) -> Any:
    """The dimension for the dbt jobs that write ``expose_id``; ``None`` when there are none."""
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
) -> Dict[str, Any]:
    """The shared drift grading, raised to CRITICAL by a missing table, reader or job."""
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
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.providers.databricks import managed_objects

    try:
        tables, jobs = managed_objects(contract, os.environ)
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
        job_dim = _jobs_dimension(client, expose_id, contract, jobs)
    except Exception as exc:  # noqa: BLE001 — a check that could not run is an error
        return _error(
            f"Databricks check for {target} could not run: {_failure(exc)}",
            target=target,
            exists=True,
            dimensions=dimensions,
        )
    dimensions["catalog_access"] = access
    if job_dim is not None:
        dimensions["jobs"] = job_dim

    has_issues = any(
        dimensions[name]["status"] == "fail"
        for name in ("structure", "types", "constraints", "catalog_access", "jobs")
        if name in dimensions
    )
    return {
        "status": "mismatch" if has_issues else "match",
        "exists": True,
        "table_id": target,
        "target": target,
        "severity": _severity(diff, access, job_dim),
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
