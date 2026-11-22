# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Failure alerts in the DAGs forge generates, declared in the contract's ``alerts`` block.

Where it fits in the pipeline:

* the contract declares the routes (``alerts.routes``) and the SLA (``alerts.sla``); an
  organisation's pack adds default routes through ``fluid_build.alert_defaults``;
* ``fluid validate`` checks them (:func:`contract_alert_findings`, and in its plugin phase
  :func:`plugin_alert_findings`);
* ``fluid generate schedule`` (and ``fluid generate artifacts``) writes them into each DAG as
  Airflow's own notifiers on the DAG's callbacks: the DAG is the artifact ``fluid
  schedule-sync`` deploys, never edited by hand;
* Airflow runs them: a failed or retried task, a successful run, a missed SLA (Airflow 2) or
  deadline (Airflow 3.1+).

The secrets stay in Airflow's connections, which a route names. The base class of a route
kind, for a package that adds one through ``fluid_build.alert_notifiers``, is
:class:`AlertNotifier`.
"""

from fluid_build.alerts.model import (
    DEFAULT_EVENTS,
    EVENT_SEVERITY,
    EVENTS,
    SEVERITIES,
    Alert,
    AlertNotifier,
    AlertRoute,
    AlertRouteError,
    NotifierCall,
)
from fluid_build.alerts.plugins import DEFAULTS_GROUP, NOTIFIER_GROUP, reset_alert_plugin_cache
from fluid_build.alerts.routes import (
    AlertPlan,
    contract_alert_findings,
    notifier_calls,
    plugin_alert_findings,
    read_alert_plan,
)

__all__ = [
    "Alert",
    "AlertNotifier",
    "AlertPlan",
    "AlertRoute",
    "AlertRouteError",
    "DEFAULTS_GROUP",
    "DEFAULT_EVENTS",
    "EVENTS",
    "EVENT_SEVERITY",
    "NOTIFIER_GROUP",
    "NotifierCall",
    "SEVERITIES",
    "contract_alert_findings",
    "notifier_calls",
    "plugin_alert_findings",
    "read_alert_plan",
    "reset_alert_plugin_cache",
]
