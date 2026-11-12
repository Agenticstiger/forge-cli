# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unity Catalog metric views from ``exposes[].semantics`` (``iac/providers/databricks_metrics.py``).

Pinned offline: the YAML a site-360-shaped semantics block compiles to (an entity, categorical
and time dimensions, non-additive measures, filtered simple metrics, a ratio), the
``source.<column>`` qualification the live warehouse needs, the time grains, and every refusal.
The hooks run against the in-process stand-in (``_fake_databricks.py``): a first apply creates
the view and grants it to the table's readers, a second runs nothing, a changed definition is an
``ALTER VIEW`` that keeps the grants, ``SELECT`` is exactly the readers, a view the contract no
longer declares is dropped, a view this module did not create is refused before the apply, and
the views over a restricted table are closed when its masks or row filter cannot be bound.

Not proven here: what real Databricks accepts and answers (the Free Edition run recorded in the
PR, HONESTLY_TESTED.md).
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Callable, Dict, Iterator, List

import pytest
import yaml

from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_metrics as mv

from . import _databricks_governed as governed
from ._databricks_cli import make_contract
from ._fake_databricks import DEPLOY_USER, FakeDatabricks, _execute

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

PLUGIN = IAC_PLUGINS["databricks"]
ENV = {"DATABRICKS_WAREHOUSE_ID": "wh-1"}
TABLE = "main.sales.site_360"
VIEW = "main.sales.site_360_semantic_model"
WINDOW = {"order": "snapshot_date", "range": "current", "semiadditive": "last"}

COLUMNS = [
    ("site_id", "string", True),
    ("country_code", "string", False),
    ("site_status", "string", False),
    ("colocation_ready_flag", "boolean", False),
    ("tenancy_count", "int", False),
    ("availability_90d_pct", "double", False),
    ("snapshot_date", "date", False),
    ("captured_at", "timestamp", False),
]

SEMANTICS: Dict[str, Any] = {
    "name": "site_360_semantic_model",
    "description": "Business semantics of Site 360.",
    "defaultAggTimeDimension": "snapshot_date",
    "entities": [
        {"name": "site_id", "type": "primary", "expr": "site_id", "description": "Primary key."}
    ],
    "dimensions": [
        {
            "name": "country_code",
            "type": "categorical",
            "expr": "country_code",
            "description": "ISO market",
        },
        {"name": "site_status", "type": "categorical", "expr": "site_status"},
        {"name": "colocation_ready_flag", "type": "categorical", "expr": "colocation_ready_flag"},
        {
            "name": "snapshot_date",
            "type": "time",
            "expr": "snapshot_date",
            "typeParams": {"timeGranularity": "day"},
        },
    ],
    "measures": [
        {
            "name": "site_count",
            "description": "Count of distinct sites.",
            "agg": "count_distinct",
            "expr": "site_id",
            "createMetric": False,
            "aggTimeDimension": "snapshot_date",
            "nonAdditiveDimension": {"name": "snapshot_date", "windowChoice": "max"},
        },
        {
            "name": "tenancy_count",
            "agg": "avg",
            "expr": "tenancy_count",
            "nonAdditiveDimension": {"name": "snapshot_date", "windowChoice": "max"},
        },
    ],
    "metrics": [
        {
            "name": "avg_tenancy_ratio",
            "type": "simple",
            "measure": "tenancy_count",
            "filter": "site_status = 'ON_AIR'",
            "description": "Average tenancies per on-air site.",
            "owner": "tm-data-platform",
        },
        {
            "name": "colocation_ready_site_count",
            "type": "simple",
            "measure": "site_count",
            "filter": "colocation_ready_flag = true AND site_status = 'ON_AIR'",
        },
        {
            "name": "ready_share",
            "type": "ratio",
            "numerator": "colocation_ready_site_count",
            "denominator": "site_count",
        },
    ],
    "tags": ["semantic-model"],
    "labels": {
        "ai-synonyms": "Site 360; Tower Passport",
        "ai-instructions": "One row per site.",
        "ai-example-questions": "Which sites are ready?",
    },
}

EXPECTED: Dict[str, Any] = {
    "version": 1.1,
    "comment": (
        "Business semantics of Site 360.\n\nSynonyms: Site 360; Tower Passport.\n\n"
        "Instructions: One row per site."
    ),
    "source": TABLE,
    "dimensions": [
        {
            "name": "site_id",
            "expr": "source.site_id",
            "comment": "Primary key.",
            "display_name": "Site ID",
            "synonyms": ["site code", "tower id"],
        },
        {
            "name": "country_code",
            "expr": "source.country_code",
            "comment": "ISO market",
            "display_name": "Country Code",
            "synonyms": ["market"],
        },
        {"name": "site_status", "expr": "source.site_status"},
        {"name": "colocation_ready_flag", "expr": "source.colocation_ready_flag"},
        {"name": "snapshot_date", "expr": "source.snapshot_date"},
    ],
    "measures": [
        {
            "name": "site_count",
            "expr": "COUNT(DISTINCT source.site_id)",
            "comment": "Count of distinct sites.",
            "window": [WINDOW],
        },
        {"name": "tenancy_count", "expr": "AVG(source.tenancy_count)", "window": [WINDOW]},
        {
            "name": "avg_tenancy_ratio",
            "expr": "AVG(source.tenancy_count) FILTER (WHERE source.site_status = 'ON_AIR')",
            "comment": "Average tenancies per on-air site.",
            "window": [WINDOW],
        },
        {
            "name": "colocation_ready_site_count",
            "expr": (
                "COUNT(DISTINCT source.site_id) FILTER (WHERE source.colocation_ready_flag = true "
                "AND source.site_status = 'ON_AIR')"
            ),
            "window": [WINDOW],
        },
        {
            "name": "ready_share",
            "expr": "MEASURE(colocation_ready_site_count) / NULLIF(MEASURE(site_count), 0)",
        },
    ],
}


def site_contract(semantics: Any = None, **kw: Any) -> Dict[str, Any]:
    contract = make_contract(columns=COLUMNS, readers=kw.pop("readers", ("group:analysts",)), **kw)
    expose = contract["exposes"][0]
    expose["binding"]["location"]["table"] = "site_360"
    for column in expose["contract"]["schema"]:
        if column["name"] == "site_id":
            column.update(
                description="Tower site the row consolidates",
                businessName="Site ID",
                labels={"synonyms": "site code; tower id; Site Code"},
            )
        elif column["name"] == "country_code":
            column.update(businessName="Country Code", labels={"synonyms": "market"})
    expose["semantics"] = copy.deepcopy(SEMANTICS) if semantics is None else semantics
    return contract


def compile_views(contract: Dict[str, Any]) -> dbx._Compiled:
    return dbx._compile(contract, ENV)


def refusals_of(contract: Dict[str, Any]) -> List[UnsupportedBindingError]:
    return [r for r in compile_views(contract).refusals if r.kind.startswith("databricks-metric")]


def only_view(contract: Dict[str, Any]) -> mv.MetricView:
    compiled = compile_views(contract)
    assert compiled.refusals == []
    (view,) = compiled.metric_views.views
    return view


# ── compile ─────────────────────────────────────────────────────────────


def test_site_360_semantics_compile_to_the_metric_view_yaml():
    view = only_view(site_contract())
    assert view.full_name == VIEW
    assert view.source == TABLE
    assert view.readers == ("analysts",)
    assert view.restricted is False
    assert yaml.safe_load(view.definition) == EXPECTED
    # Plain YAML for a reader: no anchors for the windows the metrics share with their measure.
    assert "&id" not in view.definition and "*id" not in view.definition


def test_statements_create_with_properties_and_alter_keeps_the_view():
    view = only_view(site_contract())
    name = "`main`.`sales`.`site_360_semantic_model`"
    assert view.create.startswith(
        f"CREATE VIEW {name} WITH METRICS LANGUAGE YAML TBLPROPERTIES ('fluid.product_id' = "
        f"'dbx.fake.orders', 'fluid.expose_id' = 'orders', 'fluid.definition_sha256' = "
        f"'{view.digest}') AS $$version: 1.1\n"
    )
    assert view.create.endswith("\n$$")
    assert "OR REPLACE" not in view.create
    alter, mark = view.update
    assert alter == f"ALTER VIEW {name} AS $${view.definition}$$"
    assert mark.startswith(f"ALTER VIEW {name} SET TBLPROPERTIES ('fluid.product_id' = ")


def test_the_view_is_named_after_the_table_without_a_model_name():
    semantics = copy.deepcopy(SEMANTICS)
    del semantics["name"]
    assert only_view(site_contract(semantics)).full_name == "main.sales.site_360_semantic_model"
    semantics["name"] = "Site_Metrics"
    assert only_view(site_contract(semantics)).full_name == "main.sales.site_metrics"


def test_the_plan_and_validate_see_the_view(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    actions = dbx.planned_objects(site_contract())
    (action,) = [a for a in actions if a["resource_type"] == "databricks_metric_view"]
    assert action["resource_id"] == VIEW
    assert action["source"] == TABLE
    assert action["readers"] == ["analysts"]
    semantics = copy.deepcopy(SEMANTICS)
    semantics["metrics"].append({"name": "net", "type": "derived", "expr": "a - b"})
    errors, _warnings = dbx.validate_databricks_binding(site_contract(semantics), ENV)
    assert any("derived metric" in e for e in errors)


def test_qualify_writes_columns_as_source_and_leaves_the_rest():
    columns = {"a": {}, "b": {}, "d": {}}
    text, bare = mv._qualify("a = 'a' AND b.c > 1 AND upper(a) <> `d` OR e IS NULL", columns)
    assert (
        text == "source.a = 'a' AND source.b.c > 1 AND upper(source.a) <> source.`d` OR e IS NULL"
    )
    assert bare == ["AND", "AND", "OR", "e", "IS", "NULL"]
    assert mv._qualify("COUNT(*)", columns) == ("COUNT(*)", [])
    assert mv._qualify("x1e5 + 1e5", {"e5": {}})[0] == "x1e5 + 1e5"
    typed = "CAST(a AS date) > DATE'2026-01-01' AND date IS NOT NULL"
    assert mv._qualify(typed, {"a": {}, "date": {}})[0] == (
        "CAST(source.a AS date) > DATE'2026-01-01' AND source.date IS NOT NULL"
    )


@pytest.mark.parametrize(
    ("column_type", "grain", "expected"),
    [
        ("date", "day", "source.snapshot_date"),
        ("date", "month", "TRUNC(source.snapshot_date, 'MONTH')"),
        ("date", "weekly", "TRUNC(source.snapshot_date, 'WEEK')"),
        ("timestamp", "day", "DATE_TRUNC('DAY', source.snapshot_date)"),
        ("timestamp", "hour", "DATE_TRUNC('HOUR', source.snapshot_date)"),
        ("date", None, "source.snapshot_date"),
    ],
)
def test_time_dimension_is_at_its_grain(column_type, grain, expected):
    contract = site_contract()
    for column in contract["exposes"][0]["contract"]["schema"]:
        if column["name"] == "snapshot_date":
            column["type"] = column_type
    dimension = contract["exposes"][0]["semantics"]["dimensions"][3]
    if grain is None:
        del dimension["typeParams"]
    else:
        dimension["typeParams"]["timeGranularity"] = grain
    fields = yaml.safe_load(only_view(contract).definition)["dimensions"]
    assert fields[-1] == {"name": "snapshot_date", "expr": expected}


def test_percentile_and_median_measures_take_a_filter():
    semantics = copy.deepcopy(SEMANTICS)
    semantics["measures"].append(
        {
            "name": "p90_availability",
            "agg": "percentile",
            "expr": "availability_90d_pct",
            "aggParams": {"percentile": 0.9},
        }
    )
    semantics["measures"].append(
        {"name": "median_availability", "agg": "median", "expr": "availability_90d_pct"}
    )
    semantics["metrics"].append(
        {
            "name": "p90_on_air",
            "type": "simple",
            "measure": "p90_availability",
            "filter": "site_status = 'ON_AIR'",
        }
    )
    measures = {
        m["name"]: m
        for m in yaml.safe_load(only_view(site_contract(semantics)).definition)["measures"]
    }
    assert measures["p90_availability"]["expr"] == (
        "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY source.availability_90d_pct)"
    )
    assert measures["median_availability"]["expr"] == "MEDIAN(source.availability_90d_pct)"
    assert measures["p90_on_air"]["expr"] == (
        "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY source.availability_90d_pct) "
        "FILTER (WHERE source.site_status = 'ON_AIR')"
    )


def test_a_metric_that_is_its_measure_is_one_measure_and_ratios_follow_their_inputs():
    semantics = copy.deepcopy(SEMANTICS)
    semantics["metrics"] = [
        {"name": "share_of_share", "type": "ratio", "inputMetrics": ["ready_share", "site_count"]},
        {"name": "site_count", "type": "simple", "measure": "site_count"},
        {
            "name": "ready_share",
            "type": "ratio",
            "numerator": "tenancy_count",
            "denominator": "site_count",
        },
    ]
    names = [
        m["name"]
        for m in yaml.safe_load(only_view(site_contract(semantics)).definition)["measures"]
    ]
    assert names == ["site_count", "tenancy_count", "ready_share", "share_of_share"]


def test_comment_falls_back_to_the_expose_description_and_may_be_absent():
    semantics = copy.deepcopy(SEMANTICS)
    del semantics["description"], semantics["labels"]
    contract = site_contract(semantics)
    contract["exposes"][0]["description"] = "One row per site."
    assert yaml.safe_load(only_view(contract).definition)["comment"] == "One row per site."
    del contract["exposes"][0]["description"]
    assert "comment" not in yaml.safe_load(only_view(contract).definition)


def test_readers_are_who_may_select_the_table_and_writers_are_not():
    contract = site_contract(readers=("group:analysts",))
    contract["exposes"][0]["policy"]["authz"]["writers"] = ["group:loaders"]
    contract["accessPolicy"] = {
        "grants": [
            {"principal": "group:admins", "permissions": ["admin"]},
            {"principal": "group:bi", "permissions": ["read"]},
            {"principal": "group:etl", "permissions": ["write"]},
        ]
    }
    assert only_view(contract).readers == ("admins", "analysts", "bi")


def test_semantics_with_nothing_to_compile_warns_and_makes_no_view():
    compiled = compile_views(site_contract({"name": "empty", "description": "Nothing yet."}))
    assert compiled.refusals == [] and compiled.metric_views.views == []
    assert any("no metric view is created" in w for w in compiled.warnings)


def _with(path: str, value: Any) -> Callable[[Dict[str, Any]], None]:
    """Set (or, with ``...``, delete) one key of the semantics block: ``measures.0.agg``."""

    def change(semantics: Dict[str, Any]) -> None:
        *parents, last = path.split(".")
        node: Any = semantics
        for part in parents:
            node = node[int(part)] if part.isdigit() else node[part]
        key: Any = int(last) if last.isdigit() else last
        if value is ...:
            del node[key]
        else:
            node[key] = value

    return change


def _append(kind: str, entry: Dict[str, Any]) -> Callable[[Dict[str, Any]], None]:
    return lambda semantics: semantics[kind].append(entry)


REFUSALS = [
    (_append("metrics", {"name": "net", "type": "derived", "expr": "a - b"}), "derived metric"),
    (_with("metrics.2.filter", "site_status = 'X'"), "ratio metric with a filter"),
    (_with("metrics.2.denominator", "nope"), "'nope' is no measure or metric"),
    (_with("metrics.0.measure", "missing"), "over measure 'missing'"),
    (_with("metrics.0.type", "cumulative"), "has type 'cumulative'"),
    (_with("measures.0.nonAdditiveDimension.name", "site_status"), "not a time dimension"),
    (_with("measures.0.nonAdditiveDimension.windowChoice", "last"), "windowChoice is 'last'"),
    (_with("dimensions.3.typeParams.timeGranularity", "hour"), "which a date does not have"),
    (_with("dimensions.3.typeParams.timeGranularity", "fortnight"), "no grain"),
    (_with("measures.1.name", "Country_Code"), "share one namespace"),
    (_with("measures.1.name", "tenancy count"), "not a plain identifier"),
    (_with("dimensions.0.name", "source"), "the name a metric view expression gives"),
    (_with("measures.0.expr", "site_id; DROP TABLE x"), "not an expression"),
    (_with("measures.0.expr", "(site_id"), "do not balance"),
    (_with("measures.0.expr", "SUM(site_id)"), "calls SUM(...)"),
    (_with("dimensions.0.expr", "MAX(country_code)"), "calls MAX(...)"),
    (_with("measures.0.expr", "MEASURE(site_count)"), "calls MEASURE()"),
    (_with("metrics.0.filter", "tenancy_count > (SELECT 1)"), "not an expression"),
    (_with("measures.1.expr", "avg_tenancy_ratio + 1"), "not a column of"),
    (_with("measures.1.expr", "length(secret('scope', 'key'))"), "calls secret()"),
    (_with("dimensions.0.expr", "ai_query('endpoint', country_code)"), "calls ai_query()"),
    (
        _with("metrics.0.filter", "is_account_group_member('admins')"),
        "calls is_account_group_member()",
    ),
    (_with("measures.1.expr", "length(`secret`('s', 'k'))"), "a quoted or qualified function"),
    (_with("measures.1.expr", "length(system.builtin.secret('s', 'k'))"), "quoted or qualified"),
    (_with("dimensions.0.expr", "builtin . ai_query('ep', country_code)"), "quoted or qualified"),
    (_with("dimensions.0.expr", "main.tools.lookup(country_code)"), "quoted or qualified"),
    (_with("metrics.0.filter", "IDENTIFIER('secret')('a', 'b') = 'x'"), "calls IDENTIFIER()"),
    (_with("metrics.0.filter", "upper(site_status) = 'AI_QUERY('"), None),  # a literal: fine
    # A quote inside a backtick-quoted name opens no literal: the call between them is seen.
    (
        _with(
            "metrics.0.filter",
            'exists(array(1), `a\'` -> true) AND length(secret("s", "k")) > 0 '
            "AND exists(array(1), `'b` -> true)",
        ),
        "calls secret()",
    ),
    (
        _with(
            "metrics.0.filter",
            "exists(array(1), `a'` -> true)) + 0 * (exists(array(1), `'b` -> true)",
        ),
        "parentheses do not balance",
    ),
    (_with("metrics.0.filter", "site_status = 'ON_AIR"), "does not close"),
    (_with("metrics.0.filter", "`site_status = 'x'"), "does not close"),
    (_with("metrics.0.filter", "`we(ird` = 'x' OR site_status = 'y'"), None),
    (_append("measures", {"name": "ghost", "agg": "sum"}), "has no expr"),
    (_with("measures.0.agg", "stddev"), "unsupported agg 'stddev'"),
    (_with("measures.0.agg", "percentile"), None),  # default 0.5: compiles
    (
        _append(
            "measures",
            {
                "name": "p",
                "agg": "percentile",
                "aggParams": {"percentile": 2},
                "expr": "tenancy_count",
            },
        ),
        "invalid aggParams.percentile",
    ),
    (_with("description", "Price in $$"), "holds $$"),
    (_with("labels", {"ai-instructions": "Use ${env:SECRET}"}), "holds ${"),
    (_with("name", "site-360"), "it names the metric view"),
    (_with("name", 42), "it names the metric view"),
    (_with("name", "site_360"), "a name exposes[0] (orders) already takes"),
]


@pytest.mark.parametrize(("change", "fragment"), REFUSALS)
def test_what_the_yaml_cannot_hold_is_refused(change, fragment):
    semantics = copy.deepcopy(SEMANTICS)
    change(semantics)
    found = refusals_of(site_contract(semantics))
    if fragment is None:
        assert found == []
        return
    assert found, f"expected a refusal mentioning {fragment!r}"
    assert any(fragment in str(r) for r in found), [str(r) for r in found]
    assert all(r.remediation for r in found)


def test_column_text_limits_are_refused_not_truncated():
    contract = site_contract()
    site_id = contract["exposes"][0]["contract"]["schema"][0]
    site_id["labels"] = {"synonyms": "; ".join(f"s{i}" for i in range(11))}
    assert any("at most 10" in str(r) for r in refusals_of(contract))
    site_id["labels"] = {}
    site_id["businessName"] = "x" * 256
    assert any("at most 255" in str(r) for r in refusals_of(contract))


def test_a_property_value_spark_would_substitute_is_refused():
    contract = site_contract()
    contract["id"] = "dbx.${env:X}"
    assert any("records it as a property" in str(r) for r in refusals_of(contract))


# ── the hooks, against the stand-in ─────────────────────────────────────


@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, server.warehouse_id)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret-token-value")  # pragma: allowlist secret
    server.create_table(TABLE, COLUMNS)
    try:
        yield server
    finally:
        server.stop()


class Run:
    """The plugin's hooks against ``fake``, with what they print kept."""

    def __init__(self, fake: FakeDatabricks):
        self.fake = fake
        self.said: List[str] = []

    def pre(self, doc: Dict[str, Any], **kw: Any) -> None:
        PLUGIN.pre_apply(doc, say=self.said.append, client_factory=self.fake.client, **kw)

    def post(self, doc: Dict[str, Any], **kw: Any) -> None:
        PLUGIN.post_apply(doc, say=self.said.append, client_factory=self.fake.client, **kw)

    def statements(self, since: int = 0) -> List[str]:
        return [
            s for s in self.fake.sql_log[since:] if "VIEW" in s or "GRANT" in s or "REVOKE" in s
        ]

    @property
    def text(self) -> str:
        return "\n".join(self.said)


def test_first_apply_creates_and_grants_and_a_second_runs_nothing(fake):
    run, doc = Run(fake), site_contract()
    run.pre(doc)
    assert run.statements() == []  # nothing before the apply
    run.post(doc)
    created = fake.tables[VIEW]
    assert created["table_type"] == "METRIC_VIEW"
    assert created["properties"]["fluid.product_id"] == "dbx.fake.orders"
    assert created["properties"]["fluid.expose_id"] == "orders"
    assert created["properties"]["fluid.definition_sha256"] == only_view(doc).digest
    assert created["comment"].startswith("Business semantics of Site 360.")
    assert fake.grants[("table", VIEW)] == {"analysts": ["SELECT"]}
    assert [s.split(" WITH ")[0].split(" TO ")[0] for s in run.statements()] == [
        "CREATE VIEW `main`.`sales`.`site_360_semantic_model`",
        "GRANT SELECT ON TABLE `main`.`sales`.`site_360_semantic_model`",
    ]
    assert "done: create metric view main.sales.site_360_semantic_model" in run.text
    before = len(fake.sql_log)
    run.pre(doc)
    run.post(doc)
    assert run.statements(before) == []


def test_a_changed_definition_is_altered_and_keeps_its_grants(fake):
    run, doc = Run(fake), site_contract()
    run.post(doc)
    fake.grant("table", VIEW, "analysts", "MANAGE")
    doc["exposes"][0]["semantics"]["measures"][0]["description"] = "Distinct sites."
    before = len(fake.sql_log)
    run.post(doc)
    assert [s[:12] for s in run.statements(before)] == ["ALTER VIEW `", "ALTER VIEW `"]
    assert fake.grants[("table", VIEW)] == {"analysts": ["MANAGE", "SELECT"]}
    assert fake.tables[VIEW]["properties"]["fluid.definition_sha256"] == only_view(doc).digest
    # Changed by hand in Databricks: the stored structure no longer matches, so it is restored.
    fake.tables[VIEW]["view_definition"] = "version: 1.1\nsource: main.sales.site_360\n"
    before = len(fake.sql_log)
    run.post(doc)
    assert [s[:12] for s in run.statements(before)] == ["ALTER VIEW `", "ALTER VIEW `"]


def test_select_on_the_view_is_exactly_the_tables_readers(fake):
    run, doc = Run(fake), site_contract(readers=("group:analysts", "group:bi"))
    run.post(doc)
    fake.grant("table", VIEW, "intruder", "SELECT")
    fake.grant("table", VIEW, DEPLOY_USER, "SELECT")
    before = len(fake.sql_log)
    run.post(site_contract(readers=("group:bi",)))
    assert sorted(run.statements(before)) == [
        "REVOKE SELECT ON TABLE `main`.`sales`.`site_360_semantic_model` FROM `analysts`",
        "REVOKE SELECT ON TABLE `main`.`sales`.`site_360_semantic_model` FROM `intruder`",
    ]
    assert fake.grants[("table", VIEW)]["bi"] == ["SELECT"]
    assert fake.grants[("table", VIEW)][DEPLOY_USER] == ["SELECT"]  # this run's own: left


def test_a_view_the_contract_no_longer_declares_is_dropped(fake):
    run, doc = Run(fake), site_contract()
    run.post(doc)
    other = "main.sales.someone_elses"
    ok, err = _execute(
        fake,
        f"CREATE VIEW {other} WITH METRICS LANGUAGE YAML TBLPROPERTIES ('fluid.product_id' = "
        f"'other.product') AS $$version: 1.1\nsource: {TABLE}\nmeasures:\n- name: n\n  expr: COUNT(1)\n$$",
    )
    assert ok, err
    renamed = site_contract()
    renamed["exposes"][0]["semantics"]["name"] = "site_metrics"
    before = len(fake.sql_log)
    run.post(renamed)
    assert "DROP VIEW IF EXISTS `main`.`sales`.`site_360_semantic_model`" in run.statements(before)
    assert VIEW not in fake.tables and "main.sales.site_metrics" in fake.tables
    assert other in fake.tables  # another product's view is left alone
    del renamed["exposes"][0]["semantics"]
    run.post(renamed)
    assert "main.sales.site_metrics" not in fake.tables
    assert "dropped metric view main.sales.site_metrics" in run.text


def test_dry_run_prints_the_statements_and_runs_none(fake):
    run, doc = Run(fake), site_contract()
    run.pre(doc, dry_run=True)
    assert run.statements() == []
    assert "after apply: create the metric view over main.sales.site_360" in run.text
    assert "CREATE VIEW `main`.`sales`.`site_360_semantic_model` WITH METRICS" in run.text
    assert "after apply: grant SELECT to analysts" in run.text
    assert "dry-run: none of these statements ran." in run.text


@pytest.mark.parametrize(
    ("seed", "why"),
    [
        (
            lambda fake: fake.create_table(VIEW, [("x", "string", True)]),
            "it is a managed table, not a metric view",
        ),
        (
            lambda fake: _execute(
                fake,
                f"CREATE VIEW {VIEW} WITH METRICS LANGUAGE YAML AS $$version: 1.1\nsource: "
                f"{TABLE}\nmeasures:\n- name: n\n  expr: COUNT(1)\n$$",
            ),
            "did not create (no fluid.product_id)",
        ),
        (
            lambda fake: _execute(
                fake,
                f"CREATE VIEW {VIEW} WITH METRICS LANGUAGE YAML TBLPROPERTIES ('fluid.product_id' "
                f"= 'other.product') AS $$version: 1.1\nsource: {TABLE}\nmeasures:\n- name: n\n  "
                "expr: COUNT(1)\n$$",
            ),
            "the metric view of product 'other.product'",
        ),
    ],
)
def test_a_name_taken_by_an_object_this_module_did_not_create_is_refused_before_the_apply(
    fake, seed, why
):
    seed(fake)
    before = len(fake.sql_log)
    with pytest.raises(UnsupportedBindingError, match="the metric view name is taken") as caught:
        Run(fake).pre(site_contract())
    assert why in str(caught.value)
    assert caught.value.kind == "databricks-metric-view-failed"
    assert [s for s in fake.sql_log[before:] if "VIEW" in s] == []


def test_a_view_another_identity_owns_is_refused(fake):
    Run(fake).post(site_contract())
    fake.tables[VIEW]["owner"] = "someone@example.test"
    with pytest.raises(UnsupportedBindingError, match="owned by someone@example.test"):
        Run(fake).pre(site_contract())


def test_a_failed_statement_is_a_typed_error_naming_the_view(fake):
    fake.fail_on("CREATE VIEW", "[PERMISSION_DENIED] no CREATE TABLE on schema")
    run = Run(fake)
    with pytest.raises(UnsupportedBindingError, match="create metric view .* failed") as caught:
        run.post(site_contract())
    assert caught.value.kind == "databricks-metric-view-failed"
    assert "PERMISSION_DENIED" in str(caught.value)
    assert ("table", VIEW) not in fake.grants


def test_no_sdk_is_refused_only_when_a_view_is_declared(fake):
    def no_sdk() -> Any:
        raise ImportError("no databricks-sdk")

    with pytest.raises(UnsupportedBindingError, match="databricks-sdk is not installed"):
        PLUGIN.pre_apply(site_contract(), say=lambda _l: None, client_factory=no_sdk)
    plain = site_contract()
    del plain["exposes"][0]["semantics"]
    PLUGIN.post_apply(plain, say=lambda _l: None, client_factory=no_sdk)  # nothing to do: silent


def test_views_over_a_restricted_table_are_closed_when_its_binding_fails(fake):
    profile = "main.cp360.profile"
    view = "main.cp360.profile_semantic_model"
    fake.create_table(profile, [(n, t, True) for n, t in governed.COLUMNS])
    doc = governed.contract()
    doc["exposes"][0]["semantics"] = {
        "dimensions": [{"name": "region", "type": "categorical"}],
        "measures": [{"name": "profiles", "agg": "count", "expr": "1"}],
    }
    run = Run(fake)
    run.pre(doc)
    run.post(doc)
    assert set(fake.grants[("table", view)]) == {governed.ANALYSTS_GROUP, governed.STEWARDS_GROUP}
    # A rebuild dropped the row filter, and binding it again fails: the table is locked, and
    # so is its metric view (it reads the table with its owner's rights).
    fake.tables[profile].pop("row_filter", None)
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] injected")
    with pytest.raises(UnsupportedBindingError):
        run.post(doc)
    assert all("SELECT" not in privs for privs in fake.grants[("table", view)].values())
    assert (
        "fail-safe: revoked: REVOKE SELECT ON TABLE `main`.`cp360`.`profile_semantic_model`"
        in run.text
    )


def test_lock_restricted_leaves_the_views_of_unrestricted_tables(fake):
    run, doc = Run(fake), site_contract()
    run.post(doc)
    plan = dbx._compile(doc, {"DATABRICKS_WAREHOUSE_ID": fake.warehouse_id}).metric_views
    mv.lock_restricted(plan, run.said.append, fake.client)
    assert fake.grants[("table", VIEW)] == {"analysts": ["SELECT"]}
    restricted = mv.MetricViews(plan.product_id, [replace(plan.views[0], restricted=True)])
    mv.lock_restricted(restricted, run.said.append, fake.client)
    assert fake.grants[("table", VIEW)] == {"analysts": []}
