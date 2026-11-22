# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.alert_notifiers`` (a route kind) and ``fluid_build.alert_defaults`` (an
organisation's default routes): loaded by name, under the operator's allow/block policy,
never quietly dropped."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping

import pytest

from fluid_build import plugin_manager
from fluid_build.alerts import (
    Alert,
    AlertNotifier,
    AlertRoute,
    AlertRouteError,
    contract_alert_findings,
    default_alert_findings,
    notifier_calls,
    read_alert_plan,
    reset_alert_plugin_cache,
)
from fluid_build.schedulers.airflow._alerts import render_alerts

pytestmark = pytest.mark.unit


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name
        self.version = "1.0"
        self.metadata = {"Name": name}


class FakeEP:
    """An entry point: a name, the object ``load()`` returns (or raises), its distribution."""

    def __init__(self, name: str, obj: Any, dist: str = "acme-alerts") -> None:
        self.name = name
        self._obj = obj
        self.dist = _Dist(dist)
        self.loads = 0

    def load(self) -> Any:
        self.loads += 1
        if isinstance(self._obj, BaseException):
            raise self._obj
        return self._obj


def scanner(groups: Mapping[str, List[FakeEP]]) -> Callable[[str], List[FakeEP]]:
    return lambda group: list(groups.get(group, []))


class TeamsKind(AlertNotifier):
    """A plugin kind, as a package would write it: subclassing the SDK base class."""

    notifier = "acme_alerts.notifiers:TeamsNotifier"
    requirements = ("acme-alerts>=1.2",)

    def check(self, route: AlertRoute) -> List[str]:
        return [] if route.connection else ["a teams route needs its webhook connection"]

    def kwargs(self, route: AlertRoute, alert: Alert) -> Dict[str, Any]:
        return {
            "teams_conn_id": route.connection,
            "title": alert.title,
            "text": alert.text,
            "mention": route.options.get("mention"),
        }


class DuckKind:
    """A kind that does not import forge at all: the same attributes are enough."""

    notifier = "duck_alerts:DuckNotifier"
    supports_async = True

    def kwargs(self, route: Any, alert: Any) -> Dict[str, Any]:
        return {"conn_id": route.connection, "text": alert.title}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.setattr(plugin_manager, "_entry_points", lambda group: [])
    reset_alert_plugin_cache()
    yield
    reset_alert_plugin_cache()


def _install(monkeypatch: pytest.MonkeyPatch, **groups: List[FakeEP]) -> None:
    mapping = {
        {"kinds": "fluid_build.alert_notifiers", "defaults": "fluid_build.alert_defaults"}[k]: v
        for k, v in groups.items()
    }
    monkeypatch.setattr(plugin_manager, "_entry_points", scanner(mapping))
    reset_alert_plugin_cache()


def _route(kind: str, **extra: Any) -> Dict[str, Any]:
    return {"name": "r", "kind": kind, "connection": "teams_hook", **extra}


def _contract(*routes: Dict[str, Any], **block: Any) -> Dict[str, Any]:
    return {"id": "sales.orders", "alerts": {"routes": list(routes), **block}}


class TestAKind:
    def test_a_named_kind_loads_and_renders_into_the_dag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ep = FakeEP("teams", TeamsKind)
        other = FakeEP("pager-plus", TeamsKind)
        _install(monkeypatch, kinds=[ep, other])
        plan = read_alert_plan(_contract(_route("teams", options={"mention": "@oncall"})))
        (call,) = notifier_calls(plan, "failure", dag_id="sales.orders__b", product_id="p")
        assert (call.module, call.name) == ("acme_alerts.notifiers", "TeamsNotifier")
        assert call.kwargs["mention"] == "@oncall" and "{{ ti.task_id }}" in call.kwargs["title"]
        assert ep.loads == 1 and other.loads == 0  # only the kind a route names is imported
        rendered = render_alerts(plan, dag_id="sales.orders__b", product_id="p")
        assert "from acme_alerts.notifiers import TeamsNotifier\n" in rendered.imports
        assert "acme-alerts>=1.2" in " ".join(rendered.notes)

    @pytest.mark.parametrize(
        "obj", [TeamsKind, TeamsKind(), lambda: TeamsKind(), DuckKind, DuckKind()]
    )
    def test_a_class_an_instance_or_a_factory(
        self, monkeypatch: pytest.MonkeyPatch, obj: Any
    ) -> None:
        _install(monkeypatch, kinds=[FakeEP("teams", obj)])
        assert contract_alert_findings(_contract(_route("teams"))) == ([], [])

    def test_the_kind_checks_its_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, kinds=[FakeEP("teams", TeamsKind)])
        route = {"name": "r", "kind": "teams"}
        errors, _ = contract_alert_findings(_contract(route))
        assert any("a teams route needs its webhook connection" in e for e in errors)

    def test_a_kind_without_async_warns_for_sla_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, kinds=[FakeEP("teams", TeamsKind)])
        errors, warnings = contract_alert_findings(
            _contract(_route("teams", events=["sla_miss"]), sla="PT1H")
        )
        assert errors == [] and any("Airflow 2 only" in w for w in warnings)

    @pytest.mark.parametrize(
        "env, value",
        [("FLUID_PLUGINS_BLOCKLIST", "Teams"), ("FLUID_PLUGINS_ALLOWLIST", "something_else")],
    )
    def test_the_policy_refuses_before_import(
        self, monkeypatch: pytest.MonkeyPatch, env: str, value: str
    ) -> None:
        ep = FakeEP("teams", TeamsKind)
        _install(monkeypatch, kinds=[ep])
        monkeypatch.setenv(env, value)
        errors, _ = contract_alert_findings(_contract(_route("teams")))
        assert any("FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST" in e for e in errors)
        assert ep.loads == 0

    def test_a_builtin_name_is_reserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ep = FakeEP("webhook", DuckKind)
        _install(monkeypatch, kinds=[ep])
        plan = read_alert_plan(_contract(_route("webhook")))
        (call,) = notifier_calls(plan, "failure", dag_id="d", product_id="p")
        assert call.name == "HttpNotifier" and ep.loads == 0

    def test_two_distributions_one_name_are_both_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a, b = FakeEP("teams", TeamsKind, "acme-a"), FakeEP("teams", TeamsKind, "acme-b")
        _install(monkeypatch, kinds=[a, b])
        errors, _ = contract_alert_findings(_contract(_route("teams")))
        assert any("2 distributions ship it" in e for e in errors)
        assert a.loads == b.loads == 0

    def test_a_failing_import_names_the_type_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, kinds=[FakeEP("teams", RuntimeError("token=s3cr3t-value"))])
        errors, _ = contract_alert_findings(_contract(_route("teams")))
        assert any("importing it raised RuntimeError" in e for e in errors)
        assert "s3cr3t" not in " ".join(errors)

    @pytest.mark.parametrize(
        "bad, why",
        [
            (type("NoPath", (), {"kwargs": lambda self, r, a: {}}), "is not a '<module>:<class>'"),
            (type("Code", (), {"notifier": "x; import os:Y", "kwargs": lambda s, r, a: {}}), ""),
            (type("NoKwargs", (), {"notifier": "a:B"}), "no `kwargs(route, alert)`"),
            (
                type(
                    "BadReq",
                    (),
                    {"notifier": "a:B", "kwargs": lambda s, r, a: {}, "requirements": ('x"""',)},
                ),
                "plain pip requirements",
            ),
            (
                type(
                    "BadEvents",
                    (),
                    {"notifier": "a:B", "kwargs": lambda s, r, a: {}, "events": {"boom"}},
                ),
                "unknown events",
            ),
        ],
    )
    def test_what_is_not_a_kind(self, monkeypatch: pytest.MonkeyPatch, bad: Any, why: str) -> None:
        _install(monkeypatch, kinds=[FakeEP("teams", bad)])
        errors, _ = contract_alert_findings(_contract(_route("teams")))
        assert any("it is not an alert kind" in e and why in e for e in errors), errors

    @pytest.mark.parametrize(
        "kwargs, why",
        [
            ({"bad name": 1}, "not a Python identifier"),
            ({"class": 1}, "not a Python identifier"),
            ({"context": {}}, "which Airflow passes itself"),
            ({"x": object()}, "is not a literal"),
            ({"x": {1: "a"}}, "key that is not a string"),
            ({"x": float("inf")}, "not a finite number"),
        ],
    )
    def test_kwargs_that_cannot_be_written_into_a_dag(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: Dict[str, Any], why: str
    ) -> None:
        kind = type("K", (), {"notifier": "a.b:C", "kwargs": lambda s, r, a: kwargs})
        _install(monkeypatch, kinds=[FakeEP("teams", kind)])
        plan = read_alert_plan(_contract(_route("teams")))
        with pytest.raises(AlertRouteError, match=why):
            notifier_calls(plan, "failure", dag_id="d", product_id="p")

    def test_kwargs_that_raise_refuse_the_dag_by_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(self: Any, route: Any, alert: Any) -> Dict[str, Any]:
            raise ValueError("secret=abc")

        _install(
            monkeypatch, kinds=[FakeEP("teams", type("K", (), {"notifier": "a:B", "kwargs": boom}))]
        )
        plan = read_alert_plan(_contract(_route("teams")))
        with pytest.raises(AlertRouteError, match="raised ValueError") as info:
            notifier_calls(plan, "failure", dag_id="d", product_id="p")
        assert "abc" not in str(info.value)

    def test_a_notifier_class_named_like_a_dag_name_is_aliased(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kind = type("K", (), {"notifier": "evil.mod:DAG", "kwargs": lambda s, r, a: {}})
        _install(monkeypatch, kinds=[FakeEP("teams", kind)])
        rendered = render_alerts(
            read_alert_plan(_contract(_route("teams"))), dag_id="d", product_id="p"
        )
        assert "from evil.mod import DAG as DAG_2\n" in rendered.imports
        assert "alert(\n            DAG_2," in rendered.definitions

    def test_listing_plugins_never_imports_an_alert_plugin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kind, defaults = FakeEP("teams", TeamsKind), FakeEP("acme", lambda: [])
        _install(monkeypatch, kinds=[kind], defaults=[defaults])
        listed = plugin_manager.detailed_plugins()
        assert [e["name"] for e in listed["alert_notifier"]] == ["teams"]
        assert [e["name"] for e in listed["alert_defaults"]] == ["acme"]
        assert kind.loads == defaults.loads == 0


class TestOrgDefaults:
    ONCALL = {"name": "oncall", "kind": "webhook", "format": "zenduty", "connection": "zd"}

    def test_default_routes_come_after_the_contracts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, defaults=[FakeEP("acme", lambda: [self.ONCALL])])
        plan = read_alert_plan(_contract({"name": "team", "kind": "email", "to": ["a@b.co"]}))
        assert [(r.name, r.origin) for r in plan.routes] == [
            ("team", "contract"),
            ("oncall", "org default acme"),
        ]
        rendered = render_alerts(plan, dag_id="d", product_id="p")
        assert "the default routes of acme" in rendered.notes[0]

    def test_a_contract_without_alerts_takes_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, defaults=[FakeEP("acme", [self.ONCALL])])
        assert [r.name for r in read_alert_plan({"id": "p"}).routes] == ["oncall"]

    def test_a_route_of_the_same_name_replaces_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, defaults=[FakeEP("acme", [self.ONCALL])])
        own = {"name": "oncall", "kind": "slack", "connection": "slack_x"}
        (route,) = read_alert_plan(_contract(own)).routes
        assert (route.kind, route.origin) == ("slack", "contract")

    def test_defaults_false_leaves_them_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ep = FakeEP("acme", [self.ONCALL])
        _install(monkeypatch, defaults=[ep])
        assert read_alert_plan(_contract(defaults=False)).routes == ()
        assert default_alert_findings(_contract(defaults=False)) == ([], [])
        assert ep.loads == 0

    def test_a_default_sla_miss_route_waits_for_the_contracts_sla(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        route = {**self.ONCALL, "events": ["failure", "sla_miss"]}
        _install(monkeypatch, defaults=[FakeEP("acme", [route])])
        assert contract_alert_findings({"id": "p"}) == ([], [])
        assert default_alert_findings({"id": "p"}) == ([], [])
        assert not read_alert_plan({"id": "p"}).listens("sla_miss")
        assert read_alert_plan(_contract(sla="PT1H")).listens("sla_miss")

    @pytest.mark.parametrize(
        "obj, why",
        [
            (RuntimeError("key=abc123"), "loading them raised RuntimeError"),
            (lambda: {"name": "x"}, "are not a list of routes"),
            ([{"name": "x", "kind": "webhook", "connection": "https://e.x/k"}], "connection"),
        ],
    )
    def test_defaults_that_cannot_be_read_refuse_the_dag(
        self, monkeypatch: pytest.MonkeyPatch, obj: Any, why: str
    ) -> None:
        _install(monkeypatch, defaults=[FakeEP("acme", obj)])
        errors, _ = default_alert_findings({"id": "p"})
        assert any(why in e for e in errors), errors
        with pytest.raises(AlertRouteError, match="the alert routes cannot be rendered") as info:
            read_alert_plan({"id": "p"})
        assert "abc123" not in str(info.value) and "https://e.x/k" not in str(info.value)

    def test_two_packs_one_route_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            defaults=[FakeEP("acme", [self.ONCALL]), FakeEP("beta", [self.ONCALL], "beta-pack")],
        )
        errors, _ = default_alert_findings({"id": "p"})
        assert any("is also one of the default alert routes 'acme'" in e for e in errors)

    def test_blocked_defaults_are_left_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ep = FakeEP("acme", [self.ONCALL])
        _install(monkeypatch, defaults=[ep])
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "acme")
        assert read_alert_plan({"id": "p"}).routes == () and ep.loads == 0


def test_a_check_that_returns_one_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    kind = type(
        "K",
        (),
        {"notifier": "a:B", "kwargs": lambda s, r, a: {}, "check": lambda s, r: "needs a team"},
    )
    _install(monkeypatch, kinds=[FakeEP("teams", kind)])
    errors, _ = contract_alert_findings(_contract(_route("teams")))
    assert errors == ["alerts.routes[0]: needs a team"]


def test_a_route_hashes() -> None:
    assert hash(AlertRoute(name="r", kind="slack", options={"a": 1}))
