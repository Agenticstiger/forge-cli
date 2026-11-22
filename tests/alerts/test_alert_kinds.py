# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Forge's four kinds: the notifier each builds, and the payload each service reads.

The keyword names are the notifiers' own (apache-airflow-providers-smtp, -slack, -http and
-pagerduty, read from their sources: SmtpNotifier ``from_email``/``to``/``subject``/
``html_content``/``smtp_conn_id``; SlackWebhookNotifier ``slack_webhook_conn_id``/``text``;
HttpNotifier ``http_conn_id``/``endpoint``/``method``/``json``; PagerdutyNotifier
``summary``/``severity``/``source``/``action``/``dedup_key``/``custom_details``/
``pagerduty_events_conn_id``). The live test runs them in a real Airflow 2 and 3.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.alerts import AlertRoute
from fluid_build.alerts.builtin import BUILTIN_KINDS, duration_words, make_alert

pytestmark = pytest.mark.unit

DAG = "sales.orders__daily"


def _kwargs(kind: str, event: str, **route: Any) -> Dict[str, Any]:
    r = AlertRoute(name="r", kind=kind, events=(event,), **route)
    alert = make_alert(
        event,
        dag_id=DAG,
        product_id="sales.orders",
        severity=r.severity_for(event),
        sla_seconds=3600,
    )
    return dict(BUILTIN_KINDS[kind].kwargs(r, alert))


def test_alert_text_names_the_dag_and_reads_the_task_from_airflow() -> None:
    failure = make_alert("failure", dag_id=DAG, product_id="p", severity="critical")
    assert failure.title == f"fluid: {DAG}: task {{{{ ti.task_id }}}} failed"
    assert "{{ ti.log_url }}" in failure.text and failure.dedup_key == f"fluid/{DAG}"
    retry = make_alert("retry", dag_id=DAG, product_id="p", severity="warning")
    assert retry.title.endswith("failed and will retry")
    success = make_alert("success", dag_id=DAG, product_id="p", severity="info")
    assert success.text == "Run {{ run_id }}."
    sla = make_alert("sla_miss", dag_id=DAG, product_id="p", severity="warning", sla_seconds=5400)
    assert sla.title.endswith("missed its SLA of 1h30m") and sla.sla_seconds == 5400
    # Airflow 2's sla_miss_callback passes task_list; Airflow 3's deadline alert the run
    assert "task_list is defined" in sla.text and "deadline.deadline_time" in sla.text
    with pytest.raises(ValueError, match="unknown alert event"):
        make_alert("start", dag_id=DAG, product_id="p", severity="info")


@pytest.mark.parametrize(
    "seconds, words", [(14400, "4h"), (5400, "1h30m"), (90061, "1d1h1m1s"), (0, "0s")]
)
def test_duration_words(seconds: int, words: str) -> None:
    assert duration_words(seconds) == words


def test_email() -> None:
    kw = _kwargs("email", "failure", to=("a@corp.example",), sender="fluid@corp.example")
    assert kw["to"] == ["a@corp.example"] and kw["from_email"] == "fluid@corp.example"
    assert kw["smtp_conn_id"] == "smtp_default" and "{{ ti.task_id }}" in kw["subject"]
    assert _kwargs("email", "failure", to=("a@b.co",), connection="smtp_ops")["smtp_conn_id"] == (
        "smtp_ops"
    )


def test_slack() -> None:
    kw = _kwargs("slack", "retry")
    assert kw == {
        "slack_webhook_conn_id": "slack_default",
        "text": f"fluid: {DAG}: task {{{{ ti.task_id }}}} failed and will retry\n"
        "Run {{ ti.run_id }}, try {{ ti.try_number }}. Log: {{ ti.log_url }}",
    }


class TestWebhook:
    def test_generic(self) -> None:
        kw = _kwargs("webhook", "failure", connection="hook")
        assert kw["http_conn_id"] == "hook" and kw["method"] == "POST" and "endpoint" not in kw
        body = kw["json"]
        assert body["source"] == "fluid" and body["event"] == "failure"
        assert body["severity"] == "critical" and body["dag_id"] == DAG
        assert body["task_id"] == "{{ ti.task_id }}" and body["dedup_key"] == f"fluid/{DAG}"

    def test_zenduty_triggers_and_resolves_one_entity(self) -> None:
        failure = _kwargs("webhook", "failure", connection="zd", format="zenduty")["json"]
        success = _kwargs("webhook", "success", connection="zd", format="zenduty")["json"]
        assert failure["alert_type"] == "critical" and success["alert_type"] == "resolved"
        assert failure["entity_id"] == success["entity_id"] == f"fluid/{DAG}"
        assert set(failure) == {"alert_type", "message", "summary", "entity_id"}

    def test_zenduty_alert_type_follows_severity(self) -> None:
        kw = _kwargs("webhook", "failure", connection="zd", format="zenduty", severity="error")
        assert kw["json"]["alert_type"] == "error"

    def test_opsgenie_creates_then_closes_by_alias(self) -> None:
        create = _kwargs("webhook", "failure", connection="og", format="opsgenie")
        close = _kwargs("webhook", "success", connection="og", format="opsgenie")
        assert "endpoint" not in create and create["json"]["priority"] == "P1"
        assert create["json"]["alias"] == f"fluid-{DAG}"
        assert close["endpoint"] == f"/fluid-{DAG}/close?identifierType=alias"
        assert close["json"]["source"] == "fluid"

    @pytest.mark.parametrize(
        "severity, priority",
        [("critical", "P1"), ("error", "P2"), ("warning", "P3"), ("info", "P5")],
    )
    def test_opsgenie_priority(self, severity: str, priority: str) -> None:
        kw = _kwargs("webhook", "failure", connection="og", format="opsgenie", severity=severity)
        assert kw["json"]["priority"] == priority


def test_pagerduty_triggers_and_resolves_one_dedup_key() -> None:
    trigger = _kwargs("pagerduty", "failure")
    resolve = _kwargs("pagerduty", "success", connection="pd_events")
    assert trigger["action"] == "trigger" and resolve["action"] == "resolve"
    assert trigger["dedup_key"] == resolve["dedup_key"] == f"fluid/{DAG}"
    assert trigger["severity"] == "critical" and resolve["severity"] == "info"
    assert trigger["pagerduty_events_conn_id"] == "pagerduty_events_default"
    assert resolve["pagerduty_events_conn_id"] == "pd_events"
    # the routing key is never an argument: PagerdutyNotifier reads it from the connection
    assert "integration_key" not in trigger and "routing_key" not in str(trigger)


def test_success_is_always_info_whatever_the_route_says() -> None:
    route = AlertRoute(name="r", kind="pagerduty", events=("success",), severity="critical")
    assert route.severity_for("success") == "info" and route.severity_for("retry") == "critical"


def test_each_builtin_declares_its_notifier_and_requirements() -> None:
    for name, kind in BUILTIN_KINDS.items():
        module, _, cls = kind.notifier.partition(":")
        assert module.startswith("airflow.providers.") and cls, name
        assert kind.requirements and kind.async_requirements and kind.supports_async, name
    assert BUILTIN_KINDS["webhook"].requires_connection is True
    assert {k for k, v in BUILTIN_KINDS.items() if not v.requires_connection} == {
        "email",
        "slack",
        "pagerduty",
    }
