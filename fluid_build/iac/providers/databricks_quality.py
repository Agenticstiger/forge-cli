# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Databricks data quality monitoring on the schemas a product owns.

Databricks data quality monitoring (formerly anomaly detection) is enabled per Unity
Catalog schema. On serverless compute it scans the schema's tables for freshness (a
commit unusually late for the table) and completeness (fewer rows in the last 24 hours
than the table's history predicts), writes one row per table and scan to
``system.data_quality_monitoring.table_results`` and shows the result as the Quality
indicator on each table's Overview in Catalog Explorer (the docs' "health indicator",
https://docs.databricks.com/aws/en/data-quality-monitoring/anomaly-detection/).

An expose asks for it with fields it already has, no new one: a freshness or completeness
SLO (``qos.freshnessSLO``, ``qos.completenessTarget``, an ``observability.defaultSLIs``
``freshness`` or ``completeness`` block) or a ``contract.dq.rules`` entry of type
``freshness``, ``completeness`` or ``anomaly_detection``. ``contract.dq.monitoring.enabled:
false`` turns it off for that expose. A schema gets one ``databricks_data_quality_monitor``
(``object_type = "schema"``; the pinned provider 1.140.0 has the resource) when at least one
of the module's tables in it asks, and lists the module's other tables there in
``anomaly_detection_config.excluded_table_full_names``. A table the contract does not
declare is scanned too: exclusions are by name.

The monitor learns each table's bounds from that table's own history. It takes no
threshold: on provider 1.140.0 ``excluded_table_full_names`` is its only setting, and the
job parameters that took threshold overrides are closed to new customers since 2025-07-21.
So the contract's values (``PT15M``, ``0.98``) decide which tables are watched, and are
not the bounds the monitor checks. Learned and stated thresholds are complements, not
substitutes (Elementary's ``freshness_anomalies`` beside its ``data_freshness_sla``).

The monitor's ``object_id`` is the owned schema's ``schema_id``, so OpenTofu deletes it
before the schema whenever both go. Removing it (an apply whose contract no longer asks,
or the schema's removal) deletes its hidden scan job and what it has learned, never table
data, so the data-loss gate does not count it (``RUN_DEFINITION_RESOURCE_TYPES``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Protocol, Sequence, Tuple

from ..naming import TofuExpr
from .databricks_retention import DAY, iso_duration_seconds

#: The provider resource: on a schema, anomaly detection.
RESOURCE_TYPE = "databricks_data_quality_monitor"

#: The ``contract.dq.rules`` types that ask for it: the two checks it runs, and the
#: schema's own name for the feature (``dqRule.type``).
_RULE_TYPES = ("freshness", "completeness", "anomaly_detection")


class MonitoredTable(Protocol):
    """What this module reads of a resolved table (``databricks.py::_Table``)."""

    catalog: str
    schema: str
    exposure: Mapping[str, Any]

    @property
    def full_name(self) -> str: ...

    @property
    def where(self) -> str: ...


def _dq(exposure: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = exposure.get("contract")
    dq = contract.get("dq") if isinstance(contract, Mapping) else None
    return dq if isinstance(dq, Mapping) else {}


def _monitoring(exposure: Mapping[str, Any]) -> Mapping[str, Any]:
    monitoring = _dq(exposure).get("monitoring")
    return monitoring if isinstance(monitoring, Mapping) else {}


def monitoring_request(exposure: Mapping[str, Any]) -> Tuple[str, ...]:
    """The fields of ``exposure`` that ask for data quality monitoring, in contract order;
    ``()`` when none does, or when ``contract.dq.monitoring.enabled`` is ``false``."""
    if _monitoring(exposure).get("enabled") is False:
        return ()
    asks: List[str] = []
    qos = exposure.get("qos")
    if isinstance(qos, Mapping):
        if qos.get("freshnessSLO"):
            asks.append("qos.freshnessSLO")
        if qos.get("completenessTarget") is not None:
            asks.append("qos.completenessTarget")
    observability = exposure.get("observability")
    slis = observability.get("defaultSLIs") if isinstance(observability, Mapping) else None
    if isinstance(slis, Mapping) and slis.get("enabled") is not False:
        for name in ("freshness", "completeness"):
            sli = slis.get(name)
            if isinstance(sli, Mapping) and sli.get("enabled") is not False:
                asks.append(f"observability.defaultSLIs.{name}")
    rules = _dq(exposure).get("rules")
    for index, rule in enumerate(rules if isinstance(rules, list) else []):
        if isinstance(rule, Mapping) and rule.get("type") in _RULE_TYPES:
            asks.append(f"contract.dq.rules[{index}] ({rule['type']})")
    return tuple(asks)


@dataclass(frozen=True)
class SchemaMonitor:
    """One schema's monitor: the module's tables in it that asked, and the ones it skips."""

    catalog: str
    schema: str
    #: Full names, sorted.
    monitored: Tuple[str, ...]
    excluded: Tuple[str, ...]

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema}"

    def body(self, schema_id: TofuExpr) -> Dict[str, Any]:
        """The resource body; with nothing excluded the config is ``{}``, no empty list."""
        config: Dict[str, Any] = {}
        if self.excluded:
            config["excluded_table_full_names"] = list(self.excluded)
        return {
            "object_type": "schema",
            "object_id": schema_id,
            "anomaly_detection_config": config,
        }


def plan_monitors(tables: Sequence[MonitoredTable]) -> List[SchemaMonitor]:
    """One monitor per schema in which at least one table asks (:func:`monitoring_request`),
    in the order the schemas first appear."""
    by_schema: Dict[Tuple[str, str], Tuple[List[str], List[str]]] = {}
    for table in tables:
        asked, skipped = by_schema.setdefault((table.catalog, table.schema), ([], []))
        (asked if monitoring_request(table.exposure) else skipped).append(table.full_name)
    return [
        SchemaMonitor(catalog, schema, tuple(sorted(set(asked))), tuple(sorted(set(skipped))))
        for (catalog, schema), (asked, skipped) in by_schema.items()
        if asked
    ]


def window_warnings(tables: Sequence[MonitoredTable]) -> List[str]:
    """A ``fluid validate`` warning for each monitored table whose ``contract.dq.monitoring
    .window`` is not the one day the monitor counts rows over: the window is not applied."""
    warnings: List[str] = []
    for table in tables:
        window = _monitoring(table.exposure).get("window")
        if window is None or not monitoring_request(table.exposure):
            continue
        try:
            if iso_duration_seconds(window) == DAY:
                continue
        except ValueError:
            continue  # not a duration: the schema reports it
        warnings.append(
            f"{table.where}: contract.dq.monitoring.window {window} is not applied. Databricks "
            "data quality monitoring counts a table's rows over the last 24 hours and judges "
            "its freshness against the table's own commit history. Set it to P1D, or remove it."
        )
    return warnings


def monitor_notices(monitors: Sequence[SchemaMonitor]) -> List[str]:
    """What the apply says about each monitor: where the results are, who can read them,
    and that the bounds and the cost are Databricks'."""
    notices: List[str] = []
    for monitor in monitors:
        skipped = f"; excluded: {', '.join(monitor.excluded)}" if monitor.excluded else ""
        notices.append(
            f"data quality monitoring is on for schema {monitor.full_name} (watched: "
            f"{', '.join(monitor.monitored)}{skipped}). Databricks learns each table's freshness "
            "and completeness bounds from its history, not from the contract's SLO values; it "
            "scans on serverless compute billed as DATA_QUALITY_MONITORING and shows the result "
            "as the table's Quality indicator in Catalog Explorer. "
            "system.data_quality_monitoring.table_results holds every scan (account admins "
            "only, unless they grant SELECT). contract.dq.monitoring.enabled: false turns it "
            "off for an expose."
        )
    return notices


def failure_hints(address: str) -> List[str]:
    """The way on when ``tofu apply`` failed creating or changing the monitor at
    ``address``; ``[]`` for any other resource. The monitor is created after the schema's
    tables (``depends_on``), so they exist by then."""
    if not address.startswith(f"{RESOURCE_TYPE}."):
        return []
    return [
        "Databricks did not turn on data quality monitoring for this schema (its tables were "
        "created first). It needs a Unity Catalog workspace with serverless compute, and "
        "MANAGE on the schema for this identity (the schema's owner has it).",
        "Or turn it off for the expose: set contract.dq.monitoring.enabled: false, then "
        "re-run fluid apply.",
    ]


__all__ = [
    "RESOURCE_TYPE",
    "MonitoredTable",
    "SchemaMonitor",
    "failure_hints",
    "monitor_notices",
    "monitoring_request",
    "plan_monitors",
    "window_warnings",
]
