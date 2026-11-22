# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract's ``alerts`` block: what ``fluid validate`` and the DAG renderers accept.

A route carries no secret: ``connection`` names the Airflow connection, and a URL, an address
with a password or a key-shaped value there is refused without being shown. Every string a
route gives is held to no ``{`` / ``}`` (Airflow's Jinja reads a notifier's template fields),
no URL and nothing the redactor would mask.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.alerts import (
    AlertRouteError,
    contract_alert_findings,
    plugin_alert_findings,
    read_alert_plan,
    reset_alert_plugin_cache,
)
from fluid_build.alerts.routes import MAX_ROUTES, NOT_SHOWN, parse_duration
from fluid_build.schemas import __file__ as _schemas_init

pytestmark = pytest.mark.unit

#: A Zenduty Generic Integration URL: the key is in the path.
ZENDUTY_URL = (
    "https://events.zenduty.com/integration/" + "ab12cd34" + "/generic/"
    "0f8fad5b-d9cb-469f-a165-" + "70867728950e/"
)


@pytest.fixture(autouse=True)
def _no_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """No installed package registers a kind or default routes."""
    from fluid_build import plugin_manager

    monkeypatch.setattr(plugin_manager, "_entry_points", lambda group: [])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    reset_alert_plugin_cache()


def _contract(routes: List[Dict[str, Any]], **block: Any) -> Dict[str, Any]:
    return {"id": "sales.orders", "alerts": {"routes": routes, **block}}


def _errors(contract: Dict[str, Any]) -> List[str]:
    return contract_alert_findings(contract)[0]


class TestRoutesThatRender:
    def test_each_builtin_kind_reads_back_with_its_defaults(self) -> None:
        plan = read_alert_plan(
            _contract(
                [
                    {"name": "oncall", "kind": "webhook", "connection": "zenduty_oncall"},
                    {"name": "team", "kind": "email", "to": ["data@corp.example"]},
                    {"name": "pager", "kind": "pagerduty"},
                    {"name": "chat", "kind": "slack", "connection": "slack_alerts"},
                ]
            )
        )
        assert [r.name for r in plan.routes] == ["oncall", "team", "pager", "chat"]
        assert all(r.events == ("failure",) for r in plan.routes)
        assert plan.sla_seconds is None
        assert not plan.listens("sla_miss") and plan.listens("failure")

    def test_events_read_in_wiring_order(self) -> None:
        plan = read_alert_plan(
            _contract(
                [
                    {
                        "name": "x",
                        "kind": "slack",
                        "events": ["sla_miss", "success", "failure", "retry"],
                    }
                ],
                sla="PT4H",
            )
        )
        assert plan.routes[0].events == ("failure", "retry", "success", "sla_miss")
        assert plan.sla_seconds == 4 * 3600 and plan.listens("sla_miss")

    def test_a_contract_without_alerts_has_none(self) -> None:
        assert read_alert_plan({"id": "p"}).routes == ()
        assert contract_alert_findings({"id": "p"}) == ([], [])

    @pytest.mark.parametrize(
        "text, seconds",
        [
            ("PT4H", 14400),
            ("PT90M", 5400),
            ("P1DT6H", 108000),
            ("P1W", 604800),
            ("PT1S", 1),
            ("PT1H30M15S", 5415),
        ],
    )
    def test_sla_durations(self, text: str, seconds: int) -> None:
        assert parse_duration(text) == seconds

    @pytest.mark.parametrize("text", ["P1M", "P1Y", "PT", "P", "4h", "PT4h", "P1DT", "", "-PT1H"])
    def test_calendar_or_malformed_durations_are_not_read(self, text: str) -> None:
        assert parse_duration(text) is None


class TestRefusals:
    @pytest.mark.parametrize(
        "connection",
        [
            ZENDUTY_URL,
            "https://hooks.example.com/services/T0/B0/X",
            "ops:" + "pa55" + "@smtp.example.com",
            "0123456789abcdef0123456789abcdef",  # a PagerDuty routing key
            "0f8fad5b-d9cb-469f-a165-70867728950e",  # an Opsgenie API key
            "with space",
            "{{ conn.x.password }}",
            "",
        ],
    )
    def test_connection_is_a_connection_id_never_its_secret(self, connection: str) -> None:
        errors = _errors(_contract([{"name": "x", "kind": "webhook", "connection": connection}]))
        assert errors and all(NOT_SHOWN in e for e in errors if ".connection" in e)
        rendered = " ".join(errors)
        if connection:
            assert connection not in rendered
        assert "ab12cd34" not in rendered and "70867728950e" not in rendered
        with pytest.raises(AlertRouteError) as info:
            read_alert_plan(_contract([{"name": "x", "kind": "webhook", "connection": connection}]))
        if connection:
            assert connection not in str(info.value)

    def test_a_webhook_needs_its_connection(self) -> None:
        errors = _errors(_contract([{"name": "x", "kind": "webhook"}]))
        assert any(
            "alerts.routes[0].connection is required for a webhook route" in e for e in errors
        )

    def test_an_email_route_needs_to_and_only_email_reads_it(self) -> None:
        assert any("needs `to`" in e for e in _errors(_contract([{"name": "x", "kind": "email"}])))
        errors = _errors(_contract([{"name": "x", "kind": "slack", "to": ["a@corp.example"]}]))
        assert any("alerts.routes[0].to is read by a email route only" in e for e in errors)
        errors = _errors(_contract([{"name": "x", "kind": "email", "to": ["not an address"]}]))
        assert any("not a plain email address" in e for e in errors)

    def test_format_is_a_webhook_choice(self) -> None:
        errors = _errors(
            _contract([{"name": "x", "kind": "webhook", "connection": "c", "format": "teams"}])
        )
        assert any("format must be one of generic, zenduty, opsgenie" in e for e in errors)
        assert "teams" not in " ".join(errors)
        errors = _errors(_contract([{"name": "x", "kind": "slack", "format": "zenduty"}]))
        assert any("format is read by a webhook route only" in e for e in errors)

    def test_options_are_a_plugin_kinds(self) -> None:
        errors = _errors(_contract([{"name": "x", "kind": "slack", "options": {"a": 1}}]))
        assert any("options is read by a plugin's kind only" in e for e in errors)

    def test_sla_miss_needs_an_sla(self) -> None:
        errors = _errors(_contract([{"name": "x", "kind": "slack", "events": ["sla_miss"]}]))
        assert any("alerts.sla is not set" in e for e in errors)

    def test_an_sla_no_route_watches_warns_when_defaults_are_off(self) -> None:
        errors, warnings = contract_alert_findings(
            _contract([{"name": "x", "kind": "slack"}], sla="PT1H", defaults=False)
        )
        assert errors == []
        assert any("no route alerts on sla_miss" in w for w in warnings)

    @pytest.mark.parametrize("sla", ["P31D", "PT0S", "P1M", 3600, "soon"])
    def test_an_sla_out_of_range_or_not_a_duration(self, sla: Any) -> None:
        errors = _errors(_contract([], sla=sla))
        assert any("alerts.sla must be an ISO-8601 duration" in e for e in errors)

    def test_unknown_keys_events_and_severity(self) -> None:
        errors = _errors(
            _contract(
                [
                    {
                        "name": "x",
                        "kind": "slack",
                        "url": "https://x",
                        "events": ["failure", "failure"],
                        "severity": "fatal",
                    }
                ],
                retries=3,
            )
        )
        joined = " | ".join(errors)
        assert "alerts has keys it does not read: retries" in joined
        assert "has keys a route does not read: url" in joined
        assert "events must be a list of distinct events" in joined
        assert "severity must be one of critical, error, warning, info" in joined

    def test_names_are_plain_and_unique(self) -> None:
        errors = _errors(
            _contract(
                [
                    {"name": "x", "kind": "slack"},
                    {"name": "x", "kind": "slack"},
                    {"name": "bad name", "kind": "slack"},
                ]
            )
        )
        assert any("alerts.routes[1].name 'x' is also alerts.routes[0]" in e for e in errors)
        assert any("alerts.routes[2].name must be a plain name" in e for e in errors)

    @pytest.mark.parametrize("kind", ["Slack", "slack!", "", None, 3])
    def test_kind_is_lower_case_name(self, kind: Any) -> None:
        errors = _errors(_contract([{"name": "x", "kind": kind}]))
        assert any("alerts.routes[0].kind must name a kind" in e for e in errors)

    def test_a_kind_nothing_serves_is_refused_with_where_to_get_one(self) -> None:
        contract = _contract([{"name": "x", "kind": "teams", "connection": "c"}])
        assert _errors(contract) == []  # the contract phase runs no plugin
        errors = plugin_alert_findings(contract)[0]
        assert any(
            "no alert kind 'teams' can be used" in e and "fluid_build.alert_notifiers" in e
            for e in errors
        )

    def test_at_most_max_routes(self) -> None:
        routes = [{"name": f"r{i}", "kind": "slack"} for i in range(MAX_ROUTES + 1)]
        with pytest.raises(AlertRouteError, match=f"more than {MAX_ROUTES} routes"):
            read_alert_plan(_contract(routes))

    def test_a_block_that_is_not_a_mapping(self) -> None:
        assert _errors({"alerts": ["x"]}) == ["alerts must be a mapping"]
        assert _errors({"alerts": {"routes": {"x": 1}}}) == ["alerts.routes must be a list"]
        assert any("is not a mapping" in e for e in _errors({"alerts": {"routes": ["x"]}}))


class TestPluginOptions:
    """``options`` reach a plugin's notifier, whose fields Airflow renders as templates."""

    @pytest.fixture(autouse=True)
    def _teams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.alerts.test_alert_plugins import FakeEP, TeamsKind, scanner

        monkeypatch.setattr(
            "fluid_build.plugin_manager._entry_points",
            scanner({"fluid_build.alert_notifiers": [FakeEP("teams", TeamsKind)]}),
        )
        reset_alert_plugin_cache()

    def _options_errors(self, options: Any) -> List[str]:
        route = {"name": "x", "kind": "teams", "connection": "teams_hook", "options": options}
        return _errors(_contract([route]))

    def test_plain_options_pass(self) -> None:
        options = {"mention": "@data-oncall", "priority": 2, "nested": {"tags": ["a", "b"]}}
        assert self._options_errors(options) == []

    @pytest.mark.parametrize(
        "value, why",
        [
            ("{{ 7 * 7 }}", "holds '{' or '}'"),
            ("{% raw %}", "holds '{' or '}'"),
            ("line\nbreak", "control character"),
            ("see https://hooks.example.com/x", "holds a URL"),
            ("password=" + "hunter2", "looks like a secret"),
            ("x" * 1001, "longer than 1000"),
        ],
    )
    def test_a_string_that_may_not_reach_a_template_field(self, value: str, why: str) -> None:
        errors = self._options_errors({"text": value})
        assert any(why in e and NOT_SHOWN in e for e in errors), errors
        assert value not in " ".join(errors)

    def test_values_that_are_not_json(self) -> None:
        assert any("not a finite number" in e for e in self._options_errors({"x": float("nan")}))
        assert any("not a JSON value" in e for e in self._options_errors({"x": object()}))
        assert any("not a plain name" in e for e in self._options_errors({"bad key": 1}))
        deep: Any = 1
        for _ in range(6):
            deep = {"k": deep}
        assert any("nested deeper" in e for e in self._options_errors(deep))


def test_the_schema_matches_the_route_grammar() -> None:
    """The 0.7.7 schema's patterns are the ones this module checks (one grammar)."""
    import json
    from pathlib import Path

    from fluid_build.alerts import routes

    schema = json.loads(
        (Path(_schemas_init).parent / "fluid-schema-0.7.7.json").read_text(encoding="utf-8")
    )
    route = schema["$defs"]["alertRoute"]["properties"]
    assert route["connection"]["pattern"] == "^" + routes._CONNECTION.pattern[:-2] + "$"
    assert route["name"]["pattern"] == "^" + routes._ROUTE_NAME.pattern[:-2] + "$"
    assert route["kind"]["pattern"] == "^" + routes._KIND.pattern[:-2] + "$"
    assert set(route["events"]["items"]["enum"]) == set(routes.EVENTS)
    assert schema["properties"]["alerts"]["$ref"] == "#/$defs/alerts"


class TestWhatTheScanFound:
    """Regressions for what a review of this change found."""

    def test_an_sla_of_huge_digits_is_a_finding_not_a_crash(self) -> None:
        contract = _contract([], sla="PT" + "9" * 5000 + "S")
        assert any("alerts.sla must be an ISO-8601 duration" in e for e in _errors(contract))
        assert parse_duration("PT" + "9" * 8 + "S") is None

    @pytest.mark.parametrize(
        "fmt",
        ["ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789", "0f8fad5b-d9cb-469f-a165-70867728950e"],
    )
    def test_a_format_is_never_echoed(self, fmt: str) -> None:
        route = {"name": "w", "kind": "webhook", "connection": "x", "format": fmt}
        errors = _errors(_contract([route]))
        assert errors and fmt not in " ".join(errors)
        with pytest.raises(AlertRouteError) as info:
            read_alert_plan(_contract([route]))
        assert fmt not in str(info.value)

    @pytest.mark.parametrize("field", ["name", "kind"])
    def test_a_name_or_kind_shaped_like_a_secret(self, field: str) -> None:
        token = "xox" + "b-1111111111-abcdefghij"
        route = {"name": "w", "kind": "slack", field: token}
        errors = _errors(_contract([route]))
        assert any(f".{field} must" in e for e in errors) and token not in " ".join(errors)

    def test_more_routes_than_the_cap_in_the_contract_alone(self) -> None:
        routes = [{"name": f"r{i}", "kind": "slack"} for i in range(MAX_ROUTES + 1)]
        assert any(f"more than {MAX_ROUTES} routes" in e for e in _errors(_contract(routes)))
