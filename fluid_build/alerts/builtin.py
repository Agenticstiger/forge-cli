# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Forge's four route kinds, each one of Airflow's own notifiers, and the text of an alert.

==============  ==========================================================  ==========================
kind            notifier (Airflow provider)                                 the secret, in the
                                                                            Airflow connection
==============  ==========================================================  ==========================
``email``       ``SmtpNotifier`` (apache-airflow-providers-smtp)            SMTP login (``smtp_default``)
``slack``       ``SlackWebhookNotifier`` (apache-airflow-providers-slack)   webhook token (``slack_default``)
``webhook``     ``HttpNotifier`` (apache-airflow-providers-http)            URL with its key; headers
                                                                            in the extra (required)
``pagerduty``   ``PagerdutyNotifier`` (apache-airflow-providers-pagerduty)  routing key
                                                                            (``pagerduty_events_default``)
==============  ==========================================================  ==========================

``webhook`` posts JSON in one of three ``format``\\ s:

* ``generic`` (the default): forge's own document (``source``, ``event``, ``severity``, the
  product, the DAG, the task and the run, ``title``, ``text``, ``dedup_key``);
* ``zenduty``: a Zenduty Generic Integration event (``alert_type``, ``message``, ``summary``,
  ``entity_id``); the integration URL, key and all, is the connection's host. A success sends
  ``resolved`` for the same ``entity_id``, which resolves the incident;
* ``opsgenie``: an Opsgenie Alert API create request (``message``, ``alias``, ``description``,
  ``priority``, ``source``, ``tags``); the connection's host is the alerts URL
  (``https://api.opsgenie.com/v2/alerts``, or the EU one) and its extra holds
  ``{"Authorization": "GenieKey <key>"}``, which ``HttpHook`` sends as a header. A success
  closes the alert by its alias.

PagerDuty is its own kind because its Events API v2 takes the routing key in the request
body: a generic webhook would have to write the key into the DAG. ``PagerdutyNotifier`` reads
it from the connection.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Mapping, Sequence, Tuple

from fluid_build.alerts.model import EVENTS, Alert, AlertNotifier, AlertRoute

#: ``format`` values a ``webhook`` route may set; the first is the default.
WEBHOOK_FORMATS: Tuple[str, ...] = ("generic", "zenduty", "opsgenie")

#: The Opsgenie priority for each severity.
_OPSGENIE_PRIORITY: Mapping[str, str] = {
    "critical": "P1",
    "error": "P2",
    "warning": "P3",
    "info": "P5",
}

#: How a task-level alert names the task, the run and the log (Airflow 2's ``TaskInstance``
#: and Airflow 3's ``RuntimeTaskInstance`` both have these).
_TASK_TEXT = "Run {{ ti.run_id }}, try {{ ti.try_number }}. Log: {{ ti.log_url }}"

#: The sla_miss text in both of Airflow's contexts: Airflow 2's ``sla_miss_callback`` passes
#: ``task_list``; Airflow 3's deadline alert passes the run (``dag_run``, as the REST API
#: shapes it) and the ``deadline``.
_SLA_TEXT = (
    "{% if task_list is defined %}Not done in time: {{ task_list }}"
    "{% else %}Run {{ dag_run.dag_run_id }} was not done by {{ deadline.deadline_time }}"
    "{% endif %}"
)


def duration_words(seconds: int) -> str:
    """``14400`` -> ``4h``, ``5400`` -> ``1h30m``, ``90061`` -> ``1d1h1m1s``."""
    parts = []
    remaining = int(seconds)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


def make_alert(
    event: str,
    *,
    dag_id: str,
    product_id: str,
    severity: str,
    sla_seconds: Any = None,
) -> Alert:
    """Forge's alert for ``event`` of the DAG ``dag_id``: its title, text and dedup key.

    ``dag_id`` and ``product_id`` must already be validated identifiers: they go into Jinja
    templates as they are.
    """
    if event == "failure":
        title = f"fluid: {dag_id}: task {{{{ ti.task_id }}}} failed"
        text = _TASK_TEXT
    elif event == "retry":
        title = f"fluid: {dag_id}: task {{{{ ti.task_id }}}} failed and will retry"
        text = _TASK_TEXT
    elif event == "success":
        title = f"fluid: {dag_id} succeeded"
        text = "Run {{ run_id }}."
    elif event == "sla_miss":
        title = f"fluid: {dag_id} missed its SLA of {duration_words(int(sla_seconds or 0))}"
        text = _SLA_TEXT
    else:
        raise ValueError(f"unknown alert event {event!r}; expected one of {', '.join(EVENTS)}")
    return Alert(
        event=event,
        dag_id=dag_id,
        product_id=product_id,
        severity=severity,
        title=title,
        text=text,
        dedup_key=f"fluid/{dag_id}",
        sla_seconds=int(sla_seconds) if sla_seconds else None,
    )


def _task_fields(event: str) -> Dict[str, str]:
    """The task and run of ``event``, as Jinja for Airflow's context (task events only)."""
    if event in ("failure", "retry"):
        return {
            "task_id": "{{ ti.task_id }}",
            "run_id": "{{ ti.run_id }}",
            "try_number": "{{ ti.try_number }}",
            "log_url": "{{ ti.log_url }}",
        }
    if event == "success":
        return {"run_id": "{{ run_id }}"}
    return {}


class EmailNotifier(AlertNotifier):
    """``kind: email``: ``SmtpNotifier`` to the route's ``to``, through ``smtp_default`` unless
    the route names its SMTP connection. ``from`` is the sender; without it Airflow uses the
    connection's ``from_email`` (or, on Airflow 2, ``[smtp] smtp_mail_from``)."""

    notifier = "airflow.providers.smtp.notifications.smtp:SmtpNotifier"
    requirements = ("apache-airflow-providers-smtp>=1.2.0",)
    async_requirements = ("apache-airflow-providers-smtp>=2.3.0",)
    supports_async = True
    requires_connection = False
    default_connection = "smtp_default"

    def check(self, route: AlertRoute) -> Sequence[str]:
        return () if route.to else ("an email route needs `to`: the addresses to send to",)

    def kwargs(self, route: AlertRoute, alert: Alert) -> Mapping[str, Any]:
        return {
            "from_email": route.sender,
            "to": list(route.to),
            "subject": alert.title,
            "html_content": (
                f"<p>{alert.text}</p>"
                f"<p>Product {alert.product_id}, DAG {alert.dag_id}, event {alert.event}.</p>"
            ),
            "smtp_conn_id": route.connection or self.default_connection,
        }


class SlackNotifier(AlertNotifier):
    """``kind: slack``: ``SlackWebhookNotifier`` through a Slack incoming-webhook connection
    (``slack_default`` unless the route names one), which also fixes the channel."""

    notifier = "airflow.providers.slack.notifications.slack_webhook:SlackWebhookNotifier"
    requirements = ("apache-airflow-providers-slack>=8.1.0",)
    async_requirements = ("apache-airflow-providers-slack>=9.2.0",)
    supports_async = True
    requires_connection = False
    default_connection = "slack_default"

    def kwargs(self, route: AlertRoute, alert: Alert) -> Mapping[str, Any]:
        return {
            "slack_webhook_conn_id": route.connection or self.default_connection,
            "text": f"{alert.title}\n{alert.text}",
        }


class WebhookNotifier(AlertNotifier):
    """``kind: webhook``: ``HttpNotifier`` posting JSON through the route's HTTP connection.

    The connection's host is the whole URL (``HttpHook`` uses a host holding ``://`` as the
    base URL and adds nothing when the endpoint is empty), so a key in the URL's path stays in
    the connection. Headers that carry a key go in its extra, which ``HttpHook`` sends as
    request headers. ``HttpHook`` logs the URL at DEBUG only.
    """

    notifier = "airflow.providers.http.notifications.http:HttpNotifier"
    requirements = ("apache-airflow-providers-http>=5.5.0",)
    async_requirements = ("apache-airflow-providers-http>=5.5.0",)
    supports_async = True
    requires_connection = True

    def check(self, route: AlertRoute) -> Sequence[str]:
        if route.format and route.format not in WEBHOOK_FORMATS:
            return (f"format {route.format!r} is not one of {', '.join(WEBHOOK_FORMATS)}",)
        return ()

    def kwargs(self, route: AlertRoute, alert: Alert) -> Mapping[str, Any]:
        fmt = route.format or WEBHOOK_FORMATS[0]
        call: Dict[str, Any] = {"http_conn_id": route.connection, "method": "POST"}
        if fmt == "zenduty":
            call["json"] = {
                "alert_type": "resolved" if alert.event == "success" else alert.severity,
                "message": alert.title,
                "summary": alert.text,
                "entity_id": alert.dedup_key,
            }
        elif fmt == "opsgenie":
            alias = alert.dedup_key.replace("/", "-")
            if alert.event == "success":
                call["endpoint"] = f"/{alias}/close?identifierType=alias"
                call["json"] = {"source": "fluid", "note": f"{alert.title}. {alert.text}"}
            else:
                call["json"] = {
                    "message": alert.title,
                    "alias": alias,
                    "description": alert.text,
                    "priority": _OPSGENIE_PRIORITY.get(alert.severity, "P3"),
                    "source": "fluid",
                    "tags": ["fluid", alert.product_id],
                }
        else:
            call["json"] = {
                "source": "fluid",
                "event": alert.event,
                "severity": alert.severity,
                "product_id": alert.product_id,
                "dag_id": alert.dag_id,
                "dedup_key": alert.dedup_key,
                **_task_fields(alert.event),
                "title": alert.title,
                "text": alert.text,
            }
        return call


class PagerDutyNotifier(AlertNotifier):
    """``kind: pagerduty``: ``PagerdutyNotifier`` (Events API v2) through a PagerDuty Events
    connection (``pagerduty_events_default`` unless the route names one) holding the routing
    key. A success resolves the incident the DAG's failures opened (one dedup key per DAG)."""

    notifier = "airflow.providers.pagerduty.notifications.pagerduty:PagerdutyNotifier"
    requirements = ("apache-airflow-providers-pagerduty>=3.3.0",)
    async_requirements = ("apache-airflow-providers-pagerduty>=5.1.0",)
    supports_async = True
    requires_connection = False
    default_connection = "pagerduty_events_default"

    def kwargs(self, route: AlertRoute, alert: Alert) -> Mapping[str, Any]:
        return {
            "summary": alert.title,
            "severity": alert.severity,
            "source": "fluid",
            "action": "resolve" if alert.event == "success" else "trigger",
            "dedup_key": alert.dedup_key,
            "custom_details": {
                "product_id": alert.product_id,
                "dag_id": alert.dag_id,
                "event": alert.event,
                **_task_fields(alert.event),
                "text": alert.text,
            },
            "pagerduty_events_conn_id": route.connection or self.default_connection,
        }


#: The built-in kinds by name. A plugin may not claim one of these names.
BUILTIN_KINDS: Mapping[str, AlertNotifier] = {
    "email": EmailNotifier(),
    "slack": SlackNotifier(),
    "webhook": WebhookNotifier(),
    "pagerduty": PagerDutyNotifier(),
}

#: Route keys only a built-in kind reads, so a route of another kind may not set them.
KIND_KEYS: Mapping[str, FrozenSet[str]] = {
    "to": frozenset({"email"}),
    "from": frozenset({"email"}),
    "format": frozenset({"webhook"}),
}


__all__ = [
    "BUILTIN_KINDS",
    "EmailNotifier",
    "KIND_KEYS",
    "PagerDutyNotifier",
    "SlackNotifier",
    "WEBHOOK_FORMATS",
    "WebhookNotifier",
    "duration_words",
    "make_alert",
]
