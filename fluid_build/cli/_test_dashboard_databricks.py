# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid test --results-dashboard``: an AI/BI (Lakeview) dashboard over the results table.

The dashboard is found by its workspace path, ``<folder>/<name>.lvdash.json``
(``GET /api/2.0/workspace/get-status``, whose ``resource_id`` is the dashboard
id), so every run, on any machine, finds the same one and no state file is
kept. The lookup is the one databricks-solutions/ai-dev-kit uses; Databricks
Labs DQX records the id in its install state instead, which an ephemeral CI
agent would lose.

- Missing: the folder is made (``workspace/mkdirs``), the dashboard created
  (``POST /api/2.0/lakeview/dashboards``) and published.
- Present with this definition: left alone (published if it never was).
- Present with another definition (another table, or an older forge): replaced
  with ``PATCH``, then published again.

It is published without embedded credentials: each viewer reads the results
table as themself, so they need ``SELECT`` on it and access to the warehouse.

The widget shapes (counter v2, bar and line v3, table v1, text box) follow the
``.lvdash.json`` files Databricks Labs DQX ships; nothing is copied from them.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from ._common import CLIError

DASHBOARDS_PATH = "/api/2.0/lakeview/dashboards"
GET_STATUS_PATH = "/api/2.0/workspace/get-status"
MKDIRS_PATH = "/api/2.0/workspace/mkdirs"
_SUFFIX = ".lvdash.json"

#: Rows that are fluid test's own structural checks, not data-quality rules.
_RULES_ONLY = "COALESCE(rule_type, '') <> 'structure'"


def _status(message: str) -> None:
    sys.stderr.write(message + "\n")


def parse_dashboard_path(value: Any) -> Tuple[str, str]:
    """``(folder, name)`` of ``--results-dashboard /Folder/Name``."""
    text = str(value or "").strip()
    if text.endswith(_SUFFIX):
        text = text[: -len(_SUFFIX)]
    segments = text.split("/")
    problems = (
        not text.startswith("/")
        or len(segments) < 3
        or any(not s.strip() or s in (".", "..") for s in segments[1:])
        or any(ord(ch) < 32 or ch in "\\\x7f" for ch in text)
    )
    if problems:
        raise CLIError(
            2,
            "test_results_dashboard_invalid",
            {
                "results_dashboard": str(value),
                "expected": "a workspace path: /Workspace/Shared/fluid/Data quality "
                "(the last part is the dashboard's name)",
            },
        )
    return "/".join(segments[:-1]), segments[-1]


def _sql_lines(sql: str) -> List[str]:
    lines = sql.strip().splitlines()
    return [line + "\n" for line in lines[:-1]] + lines[-1:]


def _table_column(name: str, kind: str, title: str, order: int) -> Dict[str, Any]:
    """One column of a table widget (spec version 1), with every key the editor
    writes: Lakeview refuses a column without them ("Invalid widget definition")."""
    column: Dict[str, Any] = {
        "fieldName": name,
        "booleanValues": ["false", "true"],
        "imageUrlTemplate": "{{ @ }}",
        "imageTitleTemplate": "{{ @ }}",
        "imageWidth": "",
        "imageHeight": "",
        "linkUrlTemplate": "{{ @ }}",
        "linkTextTemplate": "{{ @ }}",
        "linkTitleTemplate": "{{ @ }}",
        "linkOpenInNewTab": True,
        "type": kind,
        "displayAs": kind,
        "visible": True,
        "order": order,
        "title": title,
        "allowSearch": kind == "string",
        "alignContent": "left" if kind == "string" else "right",
        "allowHTML": False,
        "highlightLinks": False,
        "useMonospaceFont": False,
        "preserveWhitespace": False,
    }
    if kind == "datetime":
        column["dateTimeFormat"] = "YYYY-MM-DD HH:mm:ss"
    return column


def dashboard_definition(table: Tuple[str, str, str]) -> Dict[str, Any]:
    """The ``serialized_dashboard`` for a results table (validated name parts)."""
    ref = ".".join(f"`{p}`" for p in table)
    # Each product's latest run is the latest one that tested rules (a run that
    # tested none, say a product that declares none, counts only when no other
    # did), so the rule widgets always show a product's last real result.
    latest = (
        "WITH last_run AS (\n"
        "  SELECT product_id, COALESCE(\n"
        f"    MAX(CASE WHEN {_RULES_ONLY} THEN run_time END), MAX(run_time)\n"
        "  ) AS run_time\n"
        f"  FROM {ref} GROUP BY product_id\n"
        "),\n"
        "latest AS (\n"
        f"  SELECT t.* FROM {ref} AS t JOIN last_run AS l\n"
        "    ON t.product_id = l.product_id AND t.run_time = l.run_time\n"
        ")\n"
    )
    pass_pct = "ROUND(100 * AVG(CASE WHEN passed THEN 1.0 ELSE 0.0 END), 1)"
    datasets = [
        {
            "name": "fluid_dq_overall",
            "displayName": "Latest run, all products",
            "queryLines": _sql_lines(
                f"{latest}SELECT {pass_pct} AS pass_rate_pct,\n"
                "  COUNT(DISTINCT product_id) AS products,\n"
                "  SUM(CASE WHEN passed THEN 0 ELSE 1 END) AS failing_rules\n"
                f"FROM latest WHERE {_RULES_ONLY}"
            ),
        },
        {
            "name": "fluid_dq_products",
            "displayName": "Latest run per product",
            "queryLines": _sql_lines(
                f"{latest}SELECT product_id, COALESCE(layer, 'unset') AS layer,\n"
                "  COUNT(*) AS rules,\n"
                "  SUM(CASE WHEN passed THEN 0 ELSE 1 END) AS failing_rules,\n"
                f"  {pass_pct} AS pass_rate_pct,\n"
                "  MAX(run_time) AS last_run\n"
                f"FROM latest WHERE {_RULES_ONLY}\n"
                "GROUP BY product_id, COALESCE(layer, 'unset')"
            ),
        },
        {
            "name": "fluid_dq_trend",
            "displayName": "Daily pass rate per product",
            "queryLines": _sql_lines(
                "SELECT DATE(run_time) AS run_date, product_id,\n"
                f"  {pass_pct} AS pass_rate_pct\n"
                f"FROM {ref} WHERE {_RULES_ONLY}\n"
                "GROUP BY DATE(run_time), product_id"
            ),
        },
        {
            "name": "fluid_dq_failing",
            "displayName": "Failing checks, latest run",
            "queryLines": _sql_lines(
                f"{latest}SELECT product_id, layer, rule_id, rule_type, severity,\n"
                "  observed_value, threshold, message, run_time\n"
                "FROM latest WHERE NOT passed\n"
                "ORDER BY product_id, rule_id"
            ),
        },
    ]

    def counter(name: str, title: str, field: str, x: int) -> Dict[str, Any]:
        agg = f"sum({field})"
        return {
            "widget": {
                "name": name,
                "queries": [
                    {
                        "name": "main_query",
                        "query": {
                            "datasetName": "fluid_dq_overall",
                            "fields": [{"name": agg, "expression": f"SUM(`{field}`)"}],
                            "disaggregated": False,
                        },
                    }
                ],
                "spec": {
                    "version": 2,
                    "widgetType": "counter",
                    "encodings": {"value": {"fieldName": agg, "displayName": title}},
                    "frame": {"title": title, "showTitle": True},
                },
            },
            "position": {"x": x, "y": 1, "width": 2, "height": 2},
        }

    def fields(*names: str) -> List[Dict[str, str]]:
        return [{"name": n, "expression": f"`{n}`"} for n in names]

    columns = (
        ("product_id", "string", "Product"),
        ("layer", "string", "Layer"),
        ("rule_id", "string", "Rule"),
        ("rule_type", "string", "Type"),
        ("severity", "string", "Severity"),
        ("observed_value", "string", "Observed"),
        ("threshold", "string", "Threshold"),
        ("message", "string", "Message"),
        ("run_time", "datetime", "Run time"),
    )
    layout = [
        {
            "widget": {
                "name": "fluid_dq_title",
                "multilineTextboxSpec": {
                    "lines": [
                        "## Data quality by product\n",
                        f"Every `fluid test --results-table {'.'.join(table)}` run: each "
                        "product's data-quality rules, passed or failed, for Bronze products "
                        "and dbt-built ones alike.",
                    ]
                },
            },
            "position": {"x": 0, "y": 0, "width": 6, "height": 1},
        },
        counter("fluid_dq_pass_rate", "Rules passed, latest runs (%)", "pass_rate_pct", 0),
        counter("fluid_dq_product_count", "Products tested", "products", 2),
        counter("fluid_dq_failing_count", "Failing rules, latest runs", "failing_rules", 4),
        {
            "widget": {
                "name": "fluid_dq_pass_rate_by_product",
                "queries": [
                    {
                        "name": "main_query",
                        "query": {
                            "datasetName": "fluid_dq_products",
                            "fields": fields("product_id", "layer", "pass_rate_pct"),
                            "disaggregated": True,
                        },
                    }
                ],
                "spec": {
                    "version": 3,
                    "widgetType": "bar",
                    "encodings": {
                        "x": {
                            "fieldName": "product_id",
                            "displayName": "Product",
                            "scale": {"type": "categorical"},
                        },
                        "y": {
                            "fieldName": "pass_rate_pct",
                            "displayName": "Rules passed (%)",
                            "scale": {"type": "quantitative"},
                        },
                        "color": {
                            "fieldName": "layer",
                            "displayName": "Layer",
                            "scale": {"type": "categorical"},
                        },
                        "label": {"show": True},
                    },
                    "frame": {"title": "Pass rate per product, latest run", "showTitle": True},
                },
            },
            "position": {"x": 0, "y": 3, "width": 6, "height": 6},
        },
        {
            "widget": {
                "name": "fluid_dq_trend",
                "queries": [
                    {
                        "name": "main_query",
                        "query": {
                            "datasetName": "fluid_dq_trend",
                            "fields": fields("run_date", "product_id", "pass_rate_pct"),
                            "disaggregated": True,
                        },
                    }
                ],
                "spec": {
                    "version": 3,
                    "widgetType": "line",
                    "encodings": {
                        "x": {
                            "fieldName": "run_date",
                            "displayName": "Day",
                            "scale": {"type": "temporal"},
                        },
                        "y": {
                            "fieldName": "pass_rate_pct",
                            "displayName": "Rules passed (%)",
                            "scale": {"type": "quantitative"},
                        },
                        "color": {
                            "fieldName": "product_id",
                            "displayName": "Product",
                            "scale": {"type": "categorical"},
                        },
                    },
                    "frame": {"title": "Pass rate over time", "showTitle": True},
                },
            },
            "position": {"x": 0, "y": 9, "width": 6, "height": 6},
        },
        {
            "widget": {
                "name": "fluid_dq_failing",
                "queries": [
                    {
                        "name": "main_query",
                        "query": {
                            "datasetName": "fluid_dq_failing",
                            "fields": fields(*(c[0] for c in columns)),
                            "disaggregated": True,
                        },
                    }
                ],
                "spec": {
                    "version": 1,
                    "widgetType": "table",
                    "encodings": {
                        "columns": [
                            _table_column(name, kind, title, 100000 + i)
                            for i, (name, kind, title) in enumerate(columns)
                        ]
                    },
                    "invisibleColumns": [],
                    "allowHTMLByDefault": False,
                    "itemsPerPage": 25,
                    "paginationSize": "default",
                    "condensed": True,
                    "withRowNumber": False,
                    "frame": {"title": "Failing checks, latest run", "showTitle": True},
                },
            },
            "position": {"x": 0, "y": 15, "width": 6, "height": 8},
        },
    ]
    return {
        "datasets": datasets,
        "pages": [
            {
                "name": "fluid_dq",
                "displayName": "Data quality",
                "pageType": "PAGE_TYPE_CANVAS",
                "layout": layout,
            }
        ],
    }


def _status_code_is(exc: BaseException, *codes: str) -> bool:
    text = f"{getattr(exc, 'error_code', '')} {exc}".upper()
    return any(code in text for code in codes)


def _is_not_found(exc: BaseException) -> bool:
    for attr in ("status_code", "status", "http_status"):
        if getattr(exc, attr, None) == 404:
            return True
    return type(exc).__name__ in {"NotFound", "ResourceDoesNotExist"} or _status_code_is(
        exc, "RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"
    )


def _later(first: Any, second: Any) -> bool:
    """Whether timestamp ``first`` is after ``second`` (``False`` when either is unreadable)."""
    import re
    from datetime import datetime

    def parse(value: Any) -> datetime:
        text = str(value).replace("Z", "+00:00")
        # Python 3.10's fromisoformat takes only 3 or 6 fraction digits; the API
        # may send 1 to 9. Pad or cut the fraction to 6.
        text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
        return datetime.fromisoformat(text)

    try:
        a, b = parse(first), parse(second)
    except (TypeError, ValueError):
        return False
    try:
        return a > b
    except TypeError:  # one naive, one aware
        return False


def _same(theirs: Any, ours: Mapping[str, Any]) -> bool:
    """Whether a stored ``serialized_dashboard`` holds our datasets and pages."""
    try:
        stored = json.loads(theirs) if isinstance(theirs, str) else theirs
    except ValueError:
        return False
    if not isinstance(stored, dict):
        return False
    return all(
        json.dumps(stored.get(key), sort_keys=True) == json.dumps(ours.get(key), sort_keys=True)
        for key in ("datasets", "pages")
    )


class _Lakeview:
    """The few REST calls this needs, through the SDK's ``api_client.do``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def do(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        out = self._client.api_client.do(
            method, path, query=dict(query) if query else None, body=dict(body) if body else None
        )
        return out if isinstance(out, dict) else {}

    def find(self, folder: str, name: str) -> Optional[str]:
        path = f"{folder}/{name}{_SUFFIX}"
        try:
            found = self.do("GET", GET_STATUS_PATH, query={"path": path})
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise
        if str(found.get("object_type") or "").upper() != "DASHBOARD":
            raise CLIError(
                1,
                "test_results_dashboard_path_taken",
                {"results_dashboard": f"{folder}/{name}", "found": found.get("object_type")},
            )
        return str(found.get("resource_id") or "") or None


def ensure_dashboard(
    dashboard_path: str,
    table: Tuple[str, str, str],
    *,
    client_factory: Optional[Callable[[], Any]] = None,
    warehouse_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create, update or leave the dashboard; return what happened.

    Raises :class:`CLIError` with what to do when the dashboard could not be
    made or published.
    """
    from fluid_build.providers.databricks.statements import (
        default_client,
        scrub,
        warehouse_id_from_env,
    )

    folder, name = parse_dashboard_path(dashboard_path)
    definition = dashboard_definition(table)
    serialized = json.dumps(definition, separators=(",", ":"))
    try:
        client = (client_factory or default_client)()
        warehouse = warehouse_id or warehouse_id_from_env()
        api = _Lakeview(client)
        dashboard_id = api.find(folder, name)
        action = "unchanged"
        if dashboard_id is None:
            api.do("POST", MKDIRS_PATH, body={"path": folder})
            body = {
                "display_name": name,
                "parent_path": folder,
                "serialized_dashboard": serialized,
                "warehouse_id": warehouse,
            }
            try:
                dashboard_id = str(api.do("POST", DASHBOARDS_PATH, body=body)["dashboard_id"])
                action = "created"
            except Exception as exc:  # noqa: BLE001
                # Another run created it between the lookup and here.
                if not _status_code_is(exc, "ALREADY_EXISTS", "ALREADY EXISTS"):
                    raise
                dashboard_id = api.find(folder, name)
                if dashboard_id is None:
                    raise
        if action != "created":
            current = api.do("GET", f"{DASHBOARDS_PATH}/{quote(dashboard_id, safe='')}")
            if not _same(current.get("serialized_dashboard"), definition) or (
                current.get("warehouse_id") != warehouse
            ):
                patch = {"serialized_dashboard": serialized, "warehouse_id": warehouse}
                if current.get("etag"):
                    patch["etag"] = current["etag"]
                api.do("PATCH", f"{DASHBOARDS_PATH}/{quote(dashboard_id, safe='')}", body=patch)
                action = "updated"
        published_path = f"{DASHBOARDS_PATH}/{quote(dashboard_id, safe='')}/published"
        publish = action != "unchanged"
        if not publish:
            # Unchanged here, but a run whose PATCH landed and whose publish failed
            # left the published revision behind the draft: publish it then.
            try:
                published = api.do("GET", published_path)
            except Exception as exc:  # noqa: BLE001
                if not _is_not_found(exc):
                    raise
                publish = True
            else:
                publish = _later(current.get("update_time"), published.get("revision_create_time"))
        if publish:
            api.do(
                "POST", published_path, body={"embed_credentials": False, "warehouse_id": warehouse}
            )
    except CLIError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes one safe message
        context = {
            "results_dashboard": f"{folder}/{name}",
            "error": f"{type(exc).__name__}: {scrub(exc)}",
        }
        if isinstance(exc, ImportError):
            from fluid_build.providers.databricks.statements import DATABRICKS_EXTRA_INSTALL

            context["fix"] = f"Install the Databricks extra: {DATABRICKS_EXTRA_INSTALL}"
        raise CLIError(1, "test_results_dashboard_failed", context) from None
    host = str(getattr(getattr(client, "config", None), "host", "") or "").rstrip("/")
    url = f"{host}/dashboardsv3/{dashboard_id}/published" if host else None
    _status(f"Dashboard {action}: {folder}/{name}" + (f" ({url})" if url else ""))
    return {"action": action, "dashboard_id": dashboard_id, "url": url}


__all__ = ["dashboard_definition", "ensure_dashboard", "parse_dashboard_path"]
