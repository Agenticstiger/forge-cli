# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A data product's Databricks Genie space: compiled from the contract, converged against the
stand-in.

No ``tofu``: :func:`databricks_genie.compile_space` on a Site-360-shaped contract, and the
plugin's ``converge_output_ports`` hook against ``_fake_databricks.FakeDatabricks``, which keeps
a space as the live service does (every ``false`` flag left out, a stale ``etag`` answered 409,
the creator's access inherited from the folder). Pinned: what the space holds (and that prompt
matching is never turned on), the refusals, a first apply that creates and shares and a second
that changes nothing, an update that sends the ``etag``, a removal that trashes, a dry run that
writes nothing, the metric view used once it exists, and the parity of the measure SQL with the
governed MCP query path.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, Iterator, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_genie as genie

from ._fake_databricks import FakeDatabricks

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

PLUGIN = IAC_PLUGINS["databricks"]
TABLE = "main.asset.site_360"
PRODUCT = "gold.asset.site_360_v1"
MARKER = f"[fluid-genie:{PRODUCT}]"
ANALYSTS = "group:analysts@towers.example"
NOC = "group:noc@towers.example"
STEWARD_SP = "8d5f1c3e-2b6a-4f0e-9c1d-7a3b5e9f0c21"
QUESTIONS = (
    "Give me everything about site TASC-JO-00042. | Which on-air sites are colocation-ready? "
    "| Which sites are at risk?"
)


def site_contract(**block: Any) -> Dict[str, Any]:
    """A Site-360-shaped product with ``extensions.databricksGenie`` (``block``)."""
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Site 360",
        "description": "One governed row per tower.",
        "labels": {"grain": "One row per site."},
        "exposes": [
            {
                "exposeId": "site_360",
                "kind": "table",
                "description": "Every agentic use case starts by resolving a site.",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "asset", "table": "site_360"},
                    "principals": {
                        ANALYSTS: "tm-analysts",
                        NOC: "noc@towers.io",
                        "group:stewards@towers.example": STEWARD_SP,
                    },
                },
                "policy": {
                    "classification": "Confidential",
                    "authz": {
                        "readers": [ANALYSTS, NOC, "group:stewards@towers.example"],
                    },
                    "agentPolicy": {
                        "allowedUseCases": ["analysis", "qa", "rag"],
                        "deniedUseCases": ["training", "fine_tuning"],
                        "canStore": False,
                        "purposeLimitation": "Asset domain only; no model training.",
                    },
                },
                "contract": {
                    "schema": [
                        {
                            "name": "site_id",
                            "type": "STRING",
                            "required": True,
                            "description": "Tower site the row consolidates",
                            "sensitivity": "internal",
                            "businessName": "Site ID",
                            "labels": {"example": "TASC-JO-00042", "synonyms": "site code"},
                        },
                        {
                            "name": "site_status",
                            "type": "STRING",
                            "description": "Lifecycle state from the Site Master",
                            "businessName": "Site Status",
                            "validationRules": [
                                {"type": "enum", "constraint": "PLANNED | ON_AIR | SUSPENDED"}
                            ],
                            "labels": {"example": "ON_AIR"},
                        },
                        {
                            "name": "tenancy_count",
                            "type": "INTEGER",
                            "description": "Active tenancies on the site",
                            "validationRules": [{"type": "range", "constraint": "0..12"}],
                        },
                        {
                            "name": "colocation_ready_flag",
                            "type": "BOOLEAN",
                            "description": "True when the site can take another tenant",
                            "labels": {"synonyms": "colo ready; colocation ready"},
                        },
                        {
                            "name": "monthly_gross_margin",
                            "type": "DECIMAL(14,2)",
                            "description": "Latest monthly gross margin",
                            "sensitivity": "confidential",
                            "labels": {"example": "1234.50"},
                        },
                        {"name": "snapshot_date", "type": "DATE"},
                    ]
                },
                "semantics": {
                    "name": "site_360_semantic_model",
                    "defaultAggTimeDimension": "snapshot_date",
                    "entities": [{"name": "site_id", "type": "primary", "expr": "site_id"}],
                    "measures": [
                        {
                            "name": "site_count",
                            "agg": "count_distinct",
                            "expr": "site_id",
                            "nonAdditiveDimension": {
                                "name": "snapshot_date",
                                "windowChoice": "max",
                            },
                        },
                        {"name": "tenancy_count", "agg": "avg", "expr": "tenancy_count"},
                        {
                            "name": "total_margin",
                            "agg": "sum",
                            "expr": "monthly_gross_margin",
                            "description": "Total gross margin.",
                            "createMetric": True,
                        },
                    ],
                    "metrics": [
                        {
                            "name": "avg_tenancy_ratio",
                            "description": "Average active tenancies per on-air site",
                            "type": "simple",
                            "measure": "tenancy_count",
                            "filter": "site_status = 'ON_AIR'",
                        },
                        {
                            "name": "colocation_ready_site_count",
                            "type": "simple",
                            "measure": "site_count",
                            "filter": "colocation_ready_flag = true AND site_status = 'ON_AIR'",
                        },
                    ],
                    "labels": {
                        "ai-instructions": "Resolve every site question here first.",
                        "ai-example-questions": QUESTIONS,
                        "ai-synonyms": "Site 360; Tower Passport",
                    },
                },
            }
        ],
        "consumes": [
            {
                "productId": "bronze.asset.site_master_v1",
                "exposeId": "site_master",
                "purpose": "Site identity and lifecycle status.",
            }
        ],
        "extensions": {genie.EXTENSION_KEY: dict(block)},
    }


def without_block(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(doc)
    out.pop("extensions")
    return out


def compiled(doc: Dict[str, Any]) -> genie.Compiled:
    return dbx._genie(
        doc,
        dbx._compile(doc, {"DATABRICKS_WAREHOUSE_ID": "wh-1"}),
        {"DATABRICKS_WAREHOUSE_ID": "wh-1"},
    )


def space_of(doc: Dict[str, Any]) -> genie.Space:
    result = compiled(doc)
    assert result.refusals == [], result.refusals
    assert result.space is not None
    return result.space


def text_of(space: genie.Space) -> str:
    return "".join(space.serialized["instructions"]["text_instructions"][0]["content"])


# ---------------------------------------------------------------------- what the space holds


def test_the_space_holds_the_products_questions_table_and_instructions():
    space = space_of(site_contract())
    serialized = space.serialized

    assert space.title == "Site 360"
    assert space.description.endswith(MARKER)
    assert space.description.startswith("One governed row per tower.")
    assert space.tables == (TABLE,)
    # Everyone with SELECT on the table, as Unity Catalog names them.
    assert space.readers == ("8d5f1c3e-2b6a-4f0e-9c1d-7a3b5e9f0c21", "noc@towers.io", "tm-analysts")

    questions = serialized["config"]["sample_questions"]
    assert [q["question"] for q in sorted(questions, key=lambda q: q["question"][0])] == [
        ["Give me everything about site TASC-JO-00042."],
        ["Which on-air sites are colocation-ready?"],
        ["Which sites are at risk?"],
    ]
    assert [q["id"] for q in questions] == sorted(q["id"] for q in questions)
    assert all(len(q["id"]) == 32 and int(q["id"], 16) >= 0 for q in questions)

    (table,) = serialized["data_sources"]["tables"]
    assert table["identifier"] == TABLE
    assert table["description"] == ["Every agentic use case starts by resolving a site."]
    columns = {c["column_name"]: c for c in table["column_configs"]}
    assert list(columns) == sorted(columns)
    assert columns["site_id"]["description"] == [
        "Tower site the row consolidates. Example: TASC-JO-00042."
    ]
    assert columns["site_id"]["synonyms"] == ["site code"]  # "Site ID" is the name itself
    assert columns["site_status"]["description"] == [
        "Lifecycle state from the Site Master. Allowed values: PLANNED, ON_AIR, SUSPENDED. "
        "Example: ON_AIR."
    ]
    assert columns["tenancy_count"]["description"] == [
        "Active tenancies on the site. Range: 0..12."
    ]
    assert columns["colocation_ready_flag"]["synonyms"] == ["colo ready", "colocation ready"]
    assert "snapshot_date" not in columns  # nothing to say about it

    text = text_of(space)
    for line in (
        "Resolve every site question here first.",
        "Also called: Site 360; Tower Passport.",
        "Grain: One row per site.",
        "Primary key: site_id.",
        "Default time dimension: snapshot_date.",
        "site_count hold a state as of snapshot_date: aggregate them at the latest "
        "snapshot_date (max) unless the question asks about history.",
        "- avg_tenancy_ratio: Average active tenancies per on-air site. SQL: "
        "AVG(tenancy_count) FILTER (WHERE site_status = 'ON_AIR')",
        "- colocation_ready_site_count SQL: COUNT(DISTINCT site_id) FILTER (WHERE "
        "colocation_ready_flag = true AND site_status = 'ON_AIR')",
        "- total_margin: Total gross margin. SQL: SUM(monthly_gross_margin)",
        "Allowed uses of this data: analysis, qa, rag.",
        "Not allowed: training, fine_tuning. Decline a request for a use that is not allowed.",
        "Purpose limitation: Asset domain only; no model training.",
        "AI systems may not cache or store this data (agentPolicy canStore: false).",
        "Classification: Confidential.",
        "- bronze.asset.site_master_v1 (site_master): Site identity and lifecycle status.",
        "How access works: Genie runs each query as the person asking, under that person's "
        "own Unity Catalog grants, so column masks and row filters apply. This space grants "
        "no access to data",
    ):
        assert line in text, line
    assert all(
        line.endswith("\n")
        for line in serialized["instructions"]["text_instructions"][0]["content"]
    )

    measures = {m["alias"]: m for m in serialized["instructions"]["sql_snippets"]["measures"]}
    assert measures["avg_tenancy_ratio"]["sql"] == [
        "AVG(tenancy_count) FILTER (WHERE site_status = 'ON_AIR')"
    ]
    assert set(measures) == {"avg_tenancy_ratio", "colocation_ready_site_count", "total_margin"}


def test_prompt_matching_is_never_turned_on():
    """Its sampled values are read with the author's permissions and shown to everyone in the
    space; the contract supplies the values instead."""
    raw = json.dumps(space_of(site_contract()).serialized)
    assert "enable_format_assistance" not in raw and "enable_entity_matching" not in raw


def test_a_sensitive_columns_example_stays_out_of_the_space():
    columns = {
        c["column_name"]: c
        for c in space_of(site_contract()).serialized["data_sources"]["tables"][0]["column_configs"]
    }
    assert columns["monthly_gross_margin"]["description"] == [
        "Latest monthly gross margin. Sensitivity: confidential."
    ]
    assert "1234.50" not in json.dumps(columns)


def test_the_title_comes_from_the_block_and_an_empty_block_is_an_opt_in():
    assert space_of(site_contract(title="Tower Passport")).title == "Tower Passport"
    doc = site_contract()
    doc["extensions"][genie.EXTENSION_KEY] = None  # ``databricksGenie:`` with nothing under it
    assert space_of(doc).title == "Site 360"


def test_a_contract_without_the_block_compiles_no_space():
    result = compiled(without_block(site_contract()))
    assert result.space is None and result.refusals == []


def test_two_tables_get_a_section_each_and_no_sql_snippets():
    doc = site_contract()
    second = copy.deepcopy(doc["exposes"][0])
    second["exposeId"] = "site_history"
    second["binding"]["location"]["table"] = "site_history"
    second["semantics"]["labels"]["ai-example-questions"] = "How did tenancy change last year?"
    doc["exposes"].append(second)
    space = space_of(doc)
    assert space.tables == ("main.asset.site_360", "main.asset.site_history")
    assert "sql_snippets" not in space.serialized["instructions"]
    text = text_of(space)
    assert "About main.asset.site_360 (site_360):" in text
    assert "About main.asset.site_history (site_history):" in text
    assert len(space.serialized["config"]["sample_questions"]) == 4
    assert text.count("How access works") == 1


def test_a_metric_whose_filter_is_not_plain_sql_gets_its_words_only_and_a_warning():
    doc = site_contract()
    doc["exposes"][0]["semantics"]["metrics"][0]["filter"] = "{{ Dimension('site__status') }} = 1"
    space = space_of(doc)
    assert "- avg_tenancy_ratio: Average active tenancies per on-air site.\n" in text_of(space)
    aliases = [m["alias"] for m in space.serialized["instructions"]["sql_snippets"]["measures"]]
    assert "avg_tenancy_ratio" not in aliases
    _errors, warnings = dbx.validate_databricks_binding(doc, {"DATABRICKS_WAREHOUSE_ID": "wh-1"})
    assert any("avg_tenancy_ratio" in w and "without SQL" in w for w in warnings)


def test_no_example_questions_is_a_warning_not_a_refusal():
    doc = site_contract()
    del doc["exposes"][0]["semantics"]["labels"]["ai-example-questions"]
    space = space_of(doc)
    assert "config" not in space.serialized
    errors, warnings = dbx.validate_databricks_binding(doc, {"DATABRICKS_WAREHOUSE_ID": "wh-1"})
    assert errors == []
    assert any("no sample questions" in w for w in warnings)


@pytest.mark.parametrize(
    "agg, extra",
    [
        ("sum", {}),
        ("avg", {}),
        ("count", {}),
        ("count_distinct", {}),
        ("min", {}),
        ("max", {}),
        ("median", {}),
        ("percentile", {"aggParams": {"percentile": 0.9}}),
        ("percentile", {"aggParams": {"percentile": 0.25, "useDiscretePercentile": True}}),
        ("percentile", {}),
    ],
)
def test_the_measure_sql_is_what_the_governed_mcp_query_path_renders(agg, extra):
    """One contract, one number: Genie's SQL and the MCP ``query`` tool's agree."""
    from fluid_build.output_ports.mcp.query_compiler import _render_measure_expression

    measure = {"name": "m", "agg": agg, "expr": "amount", **extra}
    mcp = _render_measure_expression("m", measure, dialect="databricks")
    assert mcp == f"{genie.aggregate_sql('m', measure)} AS m"


# ---------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "change, kind, words",
    [
        (lambda d: d["extensions"].update({genie.EXTENSION_KEY: True}), "invalid", "is True"),
        (
            lambda d: d["extensions"][genie.EXTENSION_KEY].update({"warehouse": "x"}),
            "invalid",
            "'warehouse'",
        ),
        (
            lambda d: d["extensions"][genie.EXTENSION_KEY].update({"titel": "x"}),
            "invalid",
            "did you mean 'title'?",
        ),
        (
            lambda d: d["extensions"][genie.EXTENSION_KEY].update({"title": " "}),
            "invalid",
            "needs a title",
        ),
        (lambda d: d.pop("id"), "invalid", "needs the contract's id"),
        (
            lambda d: d["exposes"][0]["policy"]["agentPolicy"].update({"deniedUseCases": ["qa"]}),
            "policy",
            "denies 'qa'",
        ),
        (
            lambda d: d["exposes"][0]["policy"]["agentPolicy"].update(
                {"allowedUseCases": ["training"]}
            ),
            "policy",
            "allows neither 'qa' nor 'analysis'",
        ),
        (
            lambda d: d["exposes"][0]["policy"]["agentPolicy"].update({"allowedModels": []}),
            "policy",
            "no AI access",
        ),
        (
            lambda d: d["exposes"][0]["semantics"]["labels"].update(
                {"ai-instructions": "x" * 25_001}
            ),
            "limit",
            "25,000",
        ),
        (
            lambda d: d["exposes"][0]["binding"].update({"platform": "snowflake"}),
            "no-table",
            "no Databricks table expose",
        ),
    ],
)
def test_what_the_space_cannot_be_is_refused_by_validate_and_before_tofu(
    change, kind, words, monkeypatch
):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    doc = site_contract()
    change(doc)
    if kind == "no-table":
        # A contract with no Databricks expose never reaches this plugin; compile it directly.
        result = genie.compile_space(doc, [], [], {})
    else:
        result = compiled(doc)
    assert [r.kind for r in result.refusals] == [f"databricks-genie-{kind}"], result.refusals
    assert words in str(result.refusals[0])
    assert result.refusals[0].remediation
    if kind != "no-table":
        errors, _warnings = dbx.validate_databricks_binding(
            doc, {"DATABRICKS_WAREHOUSE_ID": "wh-1"}
        )
        assert any(words in e for e in errors), errors
        with pytest.raises(UnsupportedBindingError) as exc:
            PLUGIN.emit(doc)
        assert exc.value.kind == f"databricks-genie-{kind}"


def test_a_table_refusal_is_not_repeated_as_a_genie_one():
    doc = site_contract()
    doc["exposes"][0]["binding"]["location"]["table"] = "bad name!"
    errors, _warnings = dbx.validate_databricks_binding(doc, {"DATABRICKS_WAREHOUSE_ID": "wh-1"})
    assert errors and not any("Genie" in e for e in errors)


# ---------------------------------------------------------------------- against the stand-in


@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeDatabricks]:
    server = FakeDatabricks(catalogs=("main",)).start()
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, server.warehouse_id)
    monkeypatch.setenv(
        "DATABRICKS_TOKEN", "dapi-genie-test-token-value"
    )  # pragma: allowlist secret
    server.create_table(
        TABLE,
        [
            ("site_id", "string", False),
            ("site_status", "string", True),
            ("tenancy_count", "int", True),
            ("colocation_ready_flag", "boolean", True),
            ("monthly_gross_margin", "decimal(14,2)", True),
            ("snapshot_date", "date", True),
        ],
    )
    try:
        yield server
    finally:
        server.stop()


class Run:
    def __init__(self, fake: FakeDatabricks):
        self.fake = fake
        self.said: List[str] = []
        self.start = len(fake.requests)

    def __call__(self, doc: Dict[str, Any], **kw: Any) -> "Run":
        PLUGIN.converge_output_ports(
            doc, say=self.said.append, client_factory=self.fake.client, **kw
        )
        return self

    @property
    def text(self) -> str:
        return "\n".join(self.said)

    def writes(self) -> List[Any]:
        return [
            r
            for r in self.fake.requests[self.start :]
            if r[0] in ("POST", "PATCH", "PUT", "DELETE")
        ]


def only_space(fake: FakeDatabricks) -> Dict[str, Any]:
    (space,) = fake.genie_spaces.values()
    return space


def test_a_first_apply_creates_and_shares_and_a_second_changes_nothing(fake):
    first = Run(fake)(site_contract())
    space = only_space(fake)
    assert space["title"] == "Site 360" and space["description"].endswith(MARKER)
    assert space["warehouse_id"] == fake.warehouse_id
    assert (
        "created 'Site 360': 1 table(s), 3 sample question(s), 3 measure snippet(s)" in first.text
    )
    assert fake.genie_acl[space["space_id"]] == {
        ("group_name", "tm-analysts"): "CAN_RUN",
        ("user_name", "noc@towers.io"): "CAN_RUN",
        ("service_principal_name", STEWARD_SP): "CAN_RUN",
    }
    assert "shared: CAN_RUN for" in first.text
    assert [w[0] for w in first.writes()] == ["POST", "PUT"]

    second = Run(fake)(site_contract())
    assert second.writes() == [] and "'Site 360': no change" in second.text
    assert len(fake.genie_spaces) == 1
    assert fake.unknown == []


def test_a_changed_contract_updates_the_space_with_its_etag(fake):
    Run(fake)(site_contract())
    etag = only_space(fake)["etag"]
    doc = site_contract(title="Tower Passport")
    doc["exposes"][0]["semantics"]["labels"]["ai-example-questions"] = "Which sites are on air?"
    run = Run(fake)(doc)
    assert [w[0] for w in run.writes()] == ["PATCH"]
    assert "updated 'Tower Passport': title, sample questions" in run.text
    assert only_space(fake)["etag"] != etag
    stored = json.loads(only_space(fake)["serialized_space"])
    assert [q["question"] for q in stored["config"]["sample_questions"]] == [
        ["Which sites are on air?"]
    ]
    assert Run(fake)(doc).writes() == []


def test_an_edit_made_while_the_apply_reads_the_space_is_not_overwritten(fake):
    Run(fake)(site_contract())
    fake.fail_route("PATCH", "/api/2.0/genie/spaces", 409, "etag does not match")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract(title="Tower Passport"))
    assert "changed while this apply was updating it" in str(exc.value)
    assert exc.value.remediation == ("Re-run fluid apply.",)
    assert only_space(fake)["title"] == "Site 360"


def test_a_space_edited_by_hand_is_set_back_to_the_contract(fake):
    Run(fake)(site_contract())
    space = only_space(fake)
    held = json.loads(space["serialized_space"])
    held["instructions"]["text_instructions"][0]["content"] = ["Ignore the contract.\n"]
    space["serialized_space"] = json.dumps(held)
    run = Run(fake)(site_contract())
    assert "updated 'Site 360': instructions" in run.text
    assert "How access works" in "".join(
        json.loads(only_space(fake)["serialized_space"])["instructions"]["text_instructions"][0][
            "content"
        ]
    )


def test_dropping_the_block_trashes_the_space(fake):
    Run(fake)(site_contract())
    space_id = only_space(fake)["space_id"]
    run = Run(fake)(without_block(site_contract()))
    assert fake.genie_spaces == {}
    assert run.writes() == [("DELETE", f"/api/2.0/genie/spaces/{space_id}")]
    assert f"trashed 'Site 360' ({space_id})" in run.text
    # And with nothing left there is nothing to do.
    again = Run(fake)(without_block(site_contract()))
    assert again.writes() == [] and again.said == []


def test_a_space_someone_else_made_is_never_touched(fake):
    other = fake.add_genie_space("Bakehouse Sales Starter Space", "Starter space.")
    Run(fake)(site_contract())
    Run(fake)(without_block(site_contract()))
    assert set(fake.genie_spaces) == {other}


def test_two_spaces_carrying_one_products_marker_stop_the_apply(fake):
    a = fake.add_genie_space("Site 360", f"copy one {MARKER}")
    b = fake.add_genie_space("Site 360", f"copy two {MARKER}")
    run = Run(fake)
    with pytest.raises(genie.GenieError) as exc:
        run(site_contract())
    assert a in str(exc.value) and b in str(exc.value)
    assert run.writes() == []


def test_an_existing_space_with_the_marker_is_adopted(fake):
    space_id = fake.add_genie_space("old title", f"made before state was lost {MARKER}")
    run = Run(fake)(site_contract())
    assert [w[0] for w in run.writes()] == ["PATCH", "PUT"]
    assert set(fake.genie_spaces) == {space_id}
    assert only_space(fake)["title"] == "Site 360"


def test_a_dry_run_reads_and_writes_nothing(fake):
    run = Run(fake)(site_contract(), dry_run=True)
    assert run.writes() == [] and fake.genie_spaces == {}
    assert "would create 'Site 360'" in run.text
    assert "would share it (CAN_RUN) with" in run.text
    assert "dry-run: no Genie space was created, changed, moved, shared or trashed." in run.text

    Run(fake)(site_contract())
    changed = Run(fake)(site_contract(title="Tower Passport"), dry_run=True)
    assert changed.writes() == [] and "would update 'Tower Passport': title" in changed.text
    retire = Run(fake)(without_block(site_contract()), dry_run=True)
    assert retire.writes() == [] and "would trash 'Site 360'" in retire.text
    assert len(fake.genie_spaces) == 1


def test_a_metric_view_over_the_table_becomes_a_data_source_once_it_exists(fake):
    Run(fake)(site_contract())
    fake.add_metric_view("main.asset.site_360_metrics", TABLE, comment="Site 360 metrics")
    fake.add_metric_view("main.asset.other_metrics", "main.asset.other")
    run = Run(fake)(site_contract())
    assert "updated 'Site 360': metric views (" in run.text  # the table itself is unchanged
    stored = json.loads(only_space(fake)["serialized_space"])
    # Among the tables, where the service keeps a metric view (it moves one sent under
    # data_sources.metric_views there), so a second apply compares equal.
    assert "metric_views" not in stored["data_sources"]
    assert [t["identifier"] for t in stored["data_sources"]["tables"]] == [
        TABLE,
        "main.asset.site_360_metrics",
    ]
    assert stored["data_sources"]["tables"][1] == {
        "identifier": "main.asset.site_360_metrics",
        "description": ["Site 360 metrics."],
    }
    assert Run(fake)(site_contract()).writes() == []
    # Dropped in Unity Catalog, it leaves the space on the next apply.
    fake.metric_views.clear()
    assert "updated 'Site 360': metric views (" in Run(fake)(site_contract()).text


def test_a_metric_view_sent_where_the_service_does_not_keep_it_would_never_converge(fake):
    """The stand-in keeps a space as the live service does: a metric view sent under
    ``data_sources.metric_views`` comes back among the tables (measured 2026-11-12)."""
    fake.add_metric_view("main.asset.site_360_metrics", TABLE)
    body = {
        "warehouse_id": fake.warehouse_id,
        "serialized_space": json.dumps(
            {
                "version": 2,
                "data_sources": {
                    "tables": [{"identifier": TABLE}],
                    "metric_views": [{"identifier": "main.asset.site_360_metrics"}],
                },
            }
        ),
    }
    created = fake.client().api_client.do("POST", genie.SPACES_PATH, body=body)
    stored = json.loads(created["serialized_space"])
    assert stored["data_sources"] == {
        "tables": [{"identifier": TABLE}, {"identifier": "main.asset.site_360_metrics"}]
    }


def test_a_metric_view_definition_missing_from_the_list_is_read_from_the_view():
    class Client:
        class api_client:  # noqa: N801 — the SDK's attribute name
            @staticmethod
            def do(method, path, query=None, body=None):
                if path == genie.TABLES_PATH:
                    return {"tables": [{"full_name": "c.s.mv", "table_type": "METRIC_VIEW"}]}
                assert path == f"{genie.TABLES_PATH}/c.s.mv"
                return {"view_definition": "version: 1.1\nsource: '`c`.`S`.`t`'\n"}

    assert genie.metric_views(Client(), ["c.s.t"]) == [{"identifier": "c.s.mv"}]


def test_sharing_keeps_editors_and_takes_access_from_former_readers(fake):
    Run(fake)(site_contract())
    space_id = only_space(fake)["space_id"]
    fake.genie_acl[space_id][("user_name", "editor@towers.io")] = "CAN_EDIT"
    fake.genie_acl[space_id][("group_name", "former-readers")] = "CAN_RUN"
    run = Run(fake)(site_contract())
    acl = fake.genie_acl[space_id]
    assert acl[("user_name", "editor@towers.io")] == "CAN_EDIT"
    assert ("group_name", "former-readers") not in acl
    assert acl[("group_name", "tm-analysts")] == "CAN_RUN"
    assert "removed from former-readers" in run.text


@pytest.mark.parametrize(
    "principal, key",
    [
        ("ana@towers.io", "user_name"),
        (STEWARD_SP, "service_principal_name"),
        ("tm-analysts", "group_name"),
        ("account users", "group_name"),
    ],
)
def test_a_principal_is_shared_under_the_key_its_form_names(principal, key):
    assert genie.acl_key(principal) == (key, principal)


def test_a_workspace_without_genie_fails_with_what_to_do(fake):
    fake.fail_route("GET", "/api/2.0/genie/spaces", 404, "ENDPOINT_NOT_FOUND")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    assert "could not list the Genie spaces" in str(exc.value)
    assert "Genie (AI/BI) is enabled" in exc.value.remediation[0]
    # A contract that asks for no space says nothing there: none can have been made.
    quiet = Run(fake)(without_block(site_contract()))
    assert quiet.said == []


def test_a_failure_message_never_carries_the_credential(fake):
    fake.fail_route("POST", "/api/2.0/genie/spaces", 500, "boom dapi-genie-test-token-value")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    assert "dapi-genie-test-token-value" not in str(exc.value)


def test_without_the_sdk_the_block_says_what_to_install_and_no_block_says_nothing(fake):
    def missing():
        raise ImportError("No module named 'databricks'")

    said: List[str] = []
    with pytest.raises(genie.GenieError) as exc:
        PLUGIN.converge_output_ports(site_contract(), say=said.append, client_factory=missing)
    assert "databricks-sdk is not installed" in str(exc.value)
    assert genie.DATABRICKS_EXTRA_INSTALL in exc.value.remediation[0]
    PLUGIN.converge_output_ports(
        without_block(site_contract()), say=said.append, client_factory=missing
    )
    assert said == []


def test_the_service_keeps_the_space_as_sent(fake):
    """The no-change check compares what was sent with what the service holds: the stand-in
    keeps it as the live service does, so this pins that the module sends nothing it drops."""
    Run(fake)(site_contract())
    sent = space_of(site_contract()).serialized
    assert json.loads(only_space(fake)["serialized_space"]) == sent


# ---------------------------------------------------------------------- the engine


class _Recorder:
    def __init__(self, error: Exception = None):
        self.calls: List[Any] = []
        self.error = error

    def converge_output_ports(self, contract, say=None, dry_run=False):
        self.calls.append(dry_run)
        if self.error:
            raise self.error


def test_the_engine_runs_the_hook_a_plugin_has_and_skips_one_without():
    rec = _Recorder()
    engine._converge_output_ports(rec, {})
    engine._converge_output_ports(rec, {}, dry_run=True)
    assert rec.calls == [False, True]
    engine._converge_output_ports(object(), {})  # a plugin with no hook: nothing happens


def test_a_genie_failure_is_a_typed_error_with_its_fix():
    with pytest.raises(CLIError) as exc:
        engine._converge_output_ports(_Recorder(genie.GenieError("it broke", ("fix it",))), {})
    assert exc.value.exit_code == 1 and exc.value.event == "opentofu_output_port_failed"
    assert exc.value.context["kind"] == "databricks-genie-failed"
    assert exc.value.context["remediation"] == ["fix it"]


def test_the_engine_converges_the_space_after_access_enforcement_and_in_a_dry_run():
    source = __import__("inspect").getsource(engine._apply_via_opentofu)
    post = source.index('_enforce_access(plugin, "post_apply", contract)')
    ports = source.index("_converge_output_ports(plugin, contract)")
    dry = source.index('_enforce_access(plugin, "pre_apply", contract, dry_run=True)')
    dry_ports = source.index("_converge_output_ports(plugin, contract, dry_run=True)")
    assert post < ports
    assert dry < dry_ports < source.index('cprint("\\ndry-run: plan only')


# ---------------------------------------------------------------------- the real SDK's errors
# databricks-sdk raises a class per HTTP status and carries no status attribute
# (``databricks/sdk/errors/platform.py``); the stand-in's errors carry ``status_code``. These are
# shaped like the SDK's, by name, so a 403, 404 or 409 from a real workspace is read right.


class DatabricksError(IOError):
    pass


class NotFound(DatabricksError):
    pass


class ResourceDoesNotExist(NotFound):
    pass


class PermissionDenied(DatabricksError):
    pass


class ResourceConflict(DatabricksError):
    pass


class Aborted(ResourceConflict):
    pass


@pytest.mark.parametrize(
    "error, status",
    [
        (NotFound("Schema 'c.s' does not exist."), 404),
        (ResourceDoesNotExist("no space"), 404),
        (PermissionDenied("Genie is disabled"), 403),
        (Aborted("etag mismatch"), 409),
        (DatabricksError("boom"), None),
    ],
)
def test_an_sdk_error_is_read_as_the_status_its_class_stands_for(error, status):
    assert genie._status(error) == status


class _SdkClient:
    """``api_client.do`` that raises what the real SDK raises for ``(method, path)``."""

    def __init__(self, errors: Dict[Any, Exception], answers: Dict[Any, Any] = None):
        self.errors, self.answers = errors, answers or {}

        class _Api:
            @staticmethod
            def do(method, path, query=None, body=None):
                if (method, path) in self.errors:
                    raise self.errors[(method, path)]
                return self.answers.get((method, path), {})

        self.api_client = _Api()


def test_a_schema_not_created_yet_holds_no_metric_view():
    """A first apply's dry run: the table's schema does not exist, and the list says 404."""
    client = _SdkClient({("GET", genie.TABLES_PATH): NotFound("Schema 'c.s' does not exist.")})
    assert genie.metric_views(client, ["c.s.t"]) == []


def test_an_sdk_conflict_on_update_is_said_as_an_edit_in_between():
    client = _SdkClient({("PATCH", f"{genie.SPACES_PATH}/s1"): Aborted("etag mismatch")})
    space = space_of(site_contract())
    with pytest.raises(genie.GenieError) as exc:
        genie._update(client, "s1", space, space.serialized, "etag-1")
    assert "changed while this apply was updating it" in str(exc.value)


def test_an_sdk_permission_error_on_the_list_says_to_check_genie_and_retiring_is_quiet():
    client = _SdkClient({("GET", genie.SPACES_PATH): PermissionDenied("Genie is disabled")})
    with pytest.raises(genie.GenieError) as exc:
        genie.converge(
            compiled(site_contract()),
            print,
            client_factory=lambda: client,
        )
    assert "Genie (AI/BI) is enabled" in exc.value.remediation[0]
    said: List[str] = []
    genie.converge(
        genie.Compiled(product_id=PRODUCT),
        said.append,
        client_factory=lambda: client,
    )
    assert said == []


@pytest.mark.parametrize(
    "space, words",
    [
        (
            {"version": 2, "config": {"sample_questions": [{"id": "b" * 32}, {"id": "a" * 32}]}},
            "must be sorted by id",
        ),
        ({"version": 2, "config": {"sample_questions": [{"id": "q1"}]}}, "32 lowercase hex"),
        (
            {"version": 2, "config": {"sample_questions": [{"id": "a" * 32, "question": "x"}]}},
            "Expected an array for question",
        ),
        ({"version": 1}, "unsupported serialized_space version"),
    ],
)
def test_the_stand_in_refuses_what_the_service_refuses(fake, space, words):
    """The stand-in is only an oracle if it refuses what the Genie API refuses."""
    body = {"warehouse_id": fake.warehouse_id, "serialized_space": json.dumps(space)}
    with pytest.raises(Exception) as exc:
        fake.client().api_client.do("POST", genie.SPACES_PATH, body=body)
    assert exc.value.status_code == 400 and words in str(exc.value)


# ---------------------------------------------------------------------- whose space it is


def test_a_space_planted_in_someone_elses_folder_is_never_adopted_shared_or_trashed(fake):
    """Adopting it would share it to the readers while its maker keeps CAN_MANAGE (inherited
    from their folder), which reads the readers' conversations."""
    decoy = fake.add_genie_space(
        "Site 360", f"looks like it {MARKER}", parent_path="/Users/mallory@towers.io"
    )
    before = copy.deepcopy(fake.genie_spaces[decoy])
    run = Run(fake)(site_contract())
    assert fake.genie_spaces[decoy] == before and fake.genie_acl[decoy] == {}
    (ours,) = set(fake.genie_spaces) - {decoy}
    assert fake.genie_spaces[ours]["parent_path"] == "/Users/fake-user@example.test"
    assert f"left alone: space {decoy}" in run.text and "/Users/mallory@towers.io" in run.text
    assert "created 'Site 360'" in run.text
    # Asked again: the product's own space, not the decoy, and still no refusal for two.
    assert "'Site 360': no change" in Run(fake)(site_contract()).text
    retire = Run(fake)(without_block(site_contract()))
    assert set(fake.genie_spaces) == {decoy}
    assert (
        f"trashed 'Site 360' ({ours})" in retire.text
        and f"left alone: space {decoy}" in retire.text
    )


def test_a_marker_quoted_inside_a_description_is_not_a_match(fake):
    quoted = fake.add_genie_space("Notes", f"see {MARKER} for the site space; ours is elsewhere")
    Run(fake)(site_contract())
    assert len(fake.genie_spaces) == 2 and fake.genie_spaces[quoted]["title"] == "Notes"


def test_the_folder_match_ignores_case(fake):
    """Unity Catalog names a user by email, which Databricks compares without case."""
    space_id = fake.add_genie_space(
        "old", f"made earlier {MARKER}", parent_path="/Users/Fake-User@Example.test"
    )
    Run(fake)(site_contract())
    assert set(fake.genie_spaces) == {space_id} and only_space(fake)["title"] == "Site 360"


def test_direct_editors_and_managers_that_are_not_readers_are_named(fake):
    Run(fake)(site_contract())
    space_id = only_space(fake)["space_id"]
    fake.genie_acl[space_id][("user_name", "editor@towers.io")] = "CAN_MANAGE"
    run = Run(fake)(site_contract())
    assert "note: editor@towers.io also hold CAN_EDIT or CAN_MANAGE" in run.text


# ---------------------------------------------------------------------- what the live run taught


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(genie, "_POLL_SECONDS", 0.0)


def test_an_apply_straight_after_a_create_finds_the_space_not_a_second_one(fake, no_wait):
    """The list shows a new space only some seconds later; the create waits for it."""
    fake.genie_list_lag = 3
    Run(fake)(site_contract())
    assert "'Site 360': no change" in Run(fake)(site_contract()).text
    assert len(fake.genie_spaces) == 1


def test_a_list_that_never_shows_the_new_space_is_said_not_waited_on_forever(
    fake, no_wait, monkeypatch
):
    monkeypatch.setattr(genie, "LIST_WAIT_SECONDS", 0.0)
    fake.genie_list_lag = 1000
    run = Run(fake)(site_contract())
    assert "created 'Site 360'" in run.text and "does not show the new space yet" in run.text


def test_a_title_another_space_has_is_kept_as_the_service_suffixed_it_and_said(fake):
    fake.add_genie_space("Site 360", "the other team's Site 360")
    first = Run(fake)(site_contract())
    ours = [s for s in fake.genie_spaces.values() if s["description"].endswith(MARKER)]
    assert ours[0]["title"] == "Site 360 2026-11-12 20:35:27"
    assert "another space in this identity's folder is already called 'Site 360'" in first.text
    second = Run(fake)(site_contract())
    assert second.writes() == [] and "'Site 360': no change" in second.text
    assert "already called 'Site 360'" in second.text  # said on every apply until it is fixed


def test_renaming_the_space_to_a_title_another_space_has_says_so(fake):
    Run(fake)(site_contract())
    fake.add_genie_space("Tower Passport", "someone else's")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract(title="Tower Passport"))
    assert "already called 'Tower Passport'" in str(exc.value)
    assert "title of its own" in exc.value.remediation[0]
    assert "changed while this apply" not in str(exc.value)


def test_a_policy_that_allows_analysis_but_names_no_qa_still_gets_its_space():
    doc = site_contract()
    doc["exposes"][0]["policy"]["agentPolicy"]["allowedUseCases"] = ["analysis"]
    assert compiled(doc).refusals == []


def test_a_transient_failure_to_read_a_spaces_folder_fails_not_duplicates(fake):
    """Taken for "someone else's", a 503 would make a second space for the product."""
    Run(fake)(site_contract())
    fake.fail_route("GET", r"/api/2\.0/genie/spaces/", 503, "TEMPORARILY_UNAVAILABLE")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    assert "could not check whose Genie spaces those are" in str(exc.value)
    assert len(fake.genie_spaces) == 1
    # Removing the block under the same failure trashes nothing and says it could not check.
    retire = Run(fake)(without_block(site_contract()))
    assert len(fake.genie_spaces) == 1 and "not checked for a Genie space" in retire.text


def test_a_new_space_the_next_apply_could_not_find_is_trashed_and_said(fake):
    """If the identity's folder were not /Users/<userName> (a service principal's, say), every
    apply would make another space; the create checks it and stops instead."""
    fake.me_user_name = "8d5f1c3e-2b6a-4f0e-9c1d-7a3b5e9f0c21"  # the folder stays the user's
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    assert "where the next apply looks for it, so it was trashed" in str(exc.value)
    assert fake.genie_spaces == {}


def test_a_policy_that_denies_analysis_refuses_the_space():
    """A Genie space answers by analysing the data, so denying analysis rules it out too."""
    doc = site_contract()
    doc["exposes"][0]["policy"]["agentPolicy"]["deniedUseCases"].append("analysis")
    doc["exposes"][0]["policy"]["agentPolicy"]["allowedUseCases"] = ["qa"]
    (refusal,) = compiled(doc).refusals
    assert refusal.kind == "databricks-genie-policy" and "denies 'analysis'" in str(refusal)


# ---------------------------------------------------------------------- the defect scan's finds


def test_the_engine_prints_genie_lines_literally(capsys):
    """Rich reads ``[...]`` as markup: the marker vanished and a ``[/x]`` title raised."""

    class Plugin:
        def converge_output_ports(self, contract, say=None, dry_run=False):
            say("note: carries [fluid-genie:a.b] in '[/x] odd title'")

    engine._converge_output_ports(Plugin(), {})
    out = capsys.readouterr().out
    assert "[fluid-genie:a.b]" in out and "'[/x] odd title'" in out


def test_a_principal_that_manages_the_table_reads_it_and_gets_can_run():
    doc = site_contract()
    doc["exposes"][0]["policy"]["authz"]["readers"] = [ANALYSTS, NOC]
    doc["accessPolicy"] = {
        "grants": [{"principal": "group:stewards@towers.example", "permissions": ["manage"]}]
    }
    assert STEWARD_SP in space_of(doc).readers


def test_metric_views_count_toward_the_cap(fake, monkeypatch):
    monkeypatch.setattr(genie, "MAX_TABLES", 1)
    fake.add_metric_view("main.asset.site_360_metrics", TABLE)
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    assert "1 tables and 1 metric views" in str(exc.value) and fake.genie_spaces == {}


@pytest.mark.parametrize(
    "status, words",
    [(400, "Fix what the message names"), (503, "busy or unavailable"), (401, "credential")],
)
def test_a_failed_call_says_what_to_do_for_its_status(fake, status, words):
    fake.fail_route("POST", "/api/2.0/genie/spaces", status, "nope")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    assert words in exc.value.remediation[0]
    assert "remove extensions" not in exc.value.remediation[0]


def test_a_misplaced_new_space_that_cannot_be_trashed_is_named(fake):
    fake.me_user_name = "8d5f1c3e-2b6a-4f0e-9c1d-7a3b5e9f0c21"
    fake.fail_route("DELETE", r"/api/2\.0/genie/spaces/", 403, "denied")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    (space_id,) = fake.genie_spaces
    assert space_id in str(exc.value) and "/Users/fake-user@example.test" in str(exc.value)
    assert exc.value.remediation == (
        f"Trash space {space_id} by hand before the next apply, which would make another.",
    )


def test_a_missing_sdk_stops_a_genie_apply_before_tofu(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")

    def missing():
        raise ImportError("No module named 'databricks'")

    with pytest.raises(genie.GenieError) as exc:
        PLUGIN.pre_apply(site_contract(), say=print, client_factory=missing)
    assert "databricks-sdk is not installed" in str(exc.value)
    PLUGIN.pre_apply(without_block(site_contract()), say=print, client_factory=missing)

    class Plugin:
        def pre_apply(self, contract, say=None, dry_run=False):
            raise genie.GenieError("it broke", ("fix it",))

    with pytest.raises(CLIError) as err:
        engine._enforce_access(Plugin(), "pre_apply", {})
    assert err.value.event == "opentofu_output_port_failed"


def test_a_foreign_metric_view_whose_yaml_does_not_parse_is_not_a_match():
    assert genie._metric_view_source("source: a.b.c\nx: 2021-13-45\n") == ""


def test_two_metrics_with_one_name_give_one_snippet():
    doc = site_contract()
    metrics = doc["exposes"][0]["semantics"]["metrics"]
    metrics.append(dict(metrics[0], filter="site_status = 'SUSPENDED'"))
    measures = space_of(doc).serialized["instructions"]["sql_snippets"]["measures"]
    assert [m["alias"] for m in measures].count("avg_tenancy_ratio") == 1


def test_the_native_plan_refuses_what_emit_refuses(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    with pytest.raises(UnsupportedBindingError) as exc:
        dbx.planned_objects(site_contract(title=" "))
    assert exc.value.kind == "databricks-genie-invalid"


def test_a_principal_the_service_keeps_in_another_case_is_not_shared_again(fake):
    fake.genie_acl_folds_case = True
    doc = site_contract()
    doc["exposes"][0]["binding"]["principals"][NOC] = "Noc@Towers.io"
    Run(fake)(doc)
    assert ("user_name", "noc@towers.io") in fake.genie_acl[only_space(fake)["space_id"]]
    assert Run(fake)(doc).writes() == []


def test_a_refused_metric_view_read_is_not_blamed_on_genie(fake):
    fake.fail_route("GET", r"/api/2\.1/unity-catalog/tables", 403, "no USE SCHEMA")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract())
    assert exc.value.remediation == (
        "Check that this identity may read the metric views in Unity Catalog, then re-run "
        "fluid apply.",
    )


# ---------------------------------------------------------------------- parentPath: one shared folder

SALES = "/genie/sales"
CI_SP = "3f2a9c1e-7b4d-4e8a-9f0c-2d6b8e1a5c37"  # a CI service principal's application id


def in_sales(fake: FakeDatabricks) -> None:
    """The folder an admin made for the product, outside /Shared (only admins write the root)."""
    fake.workspace_dirs.update({"/genie", SALES})


@pytest.mark.parametrize(
    "raw, folder",
    [
        (SALES, SALES),
        ("/Workspace/genie/sales/", SALES),
        (" /genie/sales ", SALES),
        ("/WorkspaceX/sales", "/WorkspaceX/sales"),  # the prefix counts as a whole segment only
        ("/Users/fake-user@example.test/genie", "/Users/fake-user@example.test/genie"),
    ],
)
def test_parent_path_is_written_as_the_api_reads_it_back(raw, folder):
    assert space_of(site_contract(parentPath=raw)).parent_path == folder
    assert space_of(site_contract()).parent_path == ""


@pytest.mark.parametrize(
    "raw, words",
    [
        ("genie/sales", "not absolute"),
        ("/", "the workspace root"),
        ("/Workspace", "the workspace root"),
        ("/genie/../sales", "'..' part"),
        ("/genie//sales", "empty"),
        ("/genie/./sales", "'.'"),
        ("/genie/sales\n", "control character"),
        ("", "not a folder path"),
        (None, "not a folder path"),
        (42, "not a folder path"),
    ],
)
def test_a_parent_path_that_names_no_folder_is_refused_by_validate_and_before_tofu(raw, words):
    doc = site_contract(parentPath=raw)
    result = compiled(doc)
    assert [r.kind for r in result.refusals] == ["databricks-genie-invalid"], result.refusals
    assert (
        words in str(result.refusals[0]) and "/genie/<domain>" in result.refusals[0].remediation[0]
    )
    errors, _warnings = dbx.validate_databricks_binding(doc, {"DATABRICKS_WAREHOUSE_ID": "wh-1"})
    assert any("parentPath" in e for e in errors), errors
    with pytest.raises(UnsupportedBindingError):
        PLUGIN.emit(doc)


@pytest.mark.parametrize("raw", ["/Shared", "/Shared/genie", "/Workspace/Shared/genie/sales/"])
def test_a_parent_path_in_shared_is_refused_offline(raw):
    """/Shared grants every workspace user CAN_MANAGE, inherited by everything in it
    (measured): they could read every reader's conversations."""
    result = compiled(site_contract(parentPath=raw))
    assert [r.kind for r in result.refusals] == ["databricks-genie-folder"]
    message = str(result.refusals[0])
    assert "every workspace user holds CAN_MANAGE" in message
    assert "read every reader's conversations" in message
    assert "outside /Shared" in result.refusals[0].remediation[0]
    assert compiled(site_contract(parentPath="/SharedData/genie")).refusals == []


def test_a_misspelt_parent_path_key_is_named():
    result = compiled(site_contract(parent_path=SALES))
    assert "did you mean 'parentPath'?" in str(result.refusals[0])


def test_a_space_is_made_in_the_parent_path_and_found_there_again(fake):
    in_sales(fake)
    first = Run(fake)(site_contract(parentPath="/Workspace/genie/sales/"))
    space = only_space(fake)
    assert space["parent_path"] == SALES
    assert "created 'Site 360' in /genie/sales: 1 table(s)" in first.text
    assert Run(fake)(site_contract(parentPath=SALES)).writes() == []


def test_another_identity_finds_the_same_space_in_the_parent_path(fake):
    """The point of parentPath: a laptop and CI keep one space, not one each."""
    in_sales(fake)
    Run(fake)(site_contract(parentPath=SALES))
    fake.me_user_name = CI_SP
    second = Run(fake)(site_contract(parentPath=SALES))
    assert len(fake.genie_spaces) == 1
    assert second.writes() == [] and "'Site 360': no change" in second.text


def test_without_parent_path_each_identity_keeps_its_own_space(fake):
    """Why parentPath exists: CI does not adopt the laptop's space in the laptop's folder."""
    Run(fake)(site_contract())
    laptop = only_space(fake)["space_id"]
    fake.me_user_name = CI_SP
    fake.genie_default_parent = f"/Users/{CI_SP}"
    run = Run(fake)(site_contract())
    assert f"left alone: space {laptop}" in run.text
    assert len(fake.genie_spaces) == 2


def test_a_space_in_this_identitys_own_folder_is_moved_into_the_parent_path(fake):
    in_sales(fake)
    Run(fake)(site_contract())
    space_id = only_space(fake)["space_id"]
    dry = Run(fake)(site_contract(parentPath=SALES), dry_run=True)
    assert dry.writes() == [] and f"would move 'Site 360' ({space_id}) from" in dry.text
    run = Run(fake)(site_contract(parentPath=SALES))
    assert set(fake.genie_spaces) == {space_id} and only_space(fake)["parent_path"] == SALES
    assert run.writes()[0] == ("PATCH", f"/api/2.0/genie/spaces/{space_id}")
    assert (
        f"moved 'Site 360' ({space_id}) from this identity's own folder into /genie/sales"
        in run.text
    )
    assert Run(fake)(site_contract(parentPath=SALES)).writes() == []


def test_a_copy_in_this_identitys_own_folder_is_trashed_once_the_space_is_in_the_folder(fake):
    in_sales(fake)
    Run(fake)(site_contract(parentPath=SALES))
    shared = only_space(fake)["space_id"]
    fake.me_user_name = CI_SP
    old = fake.add_genie_space(
        "Site 360", f"CI's own, from before {MARKER}", parent_path=f"/Users/{CI_SP}"
    )
    dry = Run(fake)(site_contract(parentPath=SALES), dry_run=True)
    assert dry.writes() == [] and f"would trash 'Site 360' ({old})" in dry.text
    run = Run(fake)(site_contract(parentPath=SALES))
    assert set(fake.genie_spaces) == {shared}
    assert ("DELETE", f"/api/2.0/genie/spaces/{old}") in run.writes()
    assert "a copy in this identity's own folder: the product's space is the one in" in run.text


def test_two_copies_in_this_identitys_own_folder_are_not_guessed_between(fake):
    in_sales(fake)
    a = fake.add_genie_space("Site 360", f"one {MARKER}")
    b = fake.add_genie_space("Site 360 2026-11-12 20:35:27", f"two {MARKER}")
    run = Run(fake)
    with pytest.raises(genie.GenieError) as exc:
        run(site_contract(parentPath=SALES))
    assert a in str(exc.value) and b in str(exc.value) and run.writes() == []


@pytest.mark.parametrize(
    "where",
    [
        "/Users/mallory@towers.io",
        "/genie",  # the folder above
        "/genie/sales/drafts",  # a folder inside: its permissions can be wider
        "/Genie/sales",  # workspace paths are case-sensitive
    ],
)
def test_a_marker_space_beside_the_parent_path_is_left_alone(fake, where):
    in_sales(fake)
    fake.workspace_dirs.update({"/genie/sales/drafts", "/Genie", "/Genie/sales"})
    decoy = fake.add_genie_space("Site 360", f"planted {MARKER}", parent_path=where)
    before = copy.deepcopy(fake.genie_spaces[decoy])
    run = Run(fake)(site_contract(parentPath=SALES))
    assert fake.genie_spaces[decoy] == before and fake.genie_acl[decoy] == {}
    assert f"left alone: space {decoy}" in run.text
    assert f"not in {SALES} or this identity's own folder" in run.text
    (ours,) = set(fake.genie_spaces) - {decoy}
    assert fake.genie_spaces[ours]["parent_path"] == SALES


def test_a_missing_parent_folder_stops_the_apply_before_tofu(fake):
    doc = site_contract(parentPath=SALES)
    with pytest.raises(genie.GenieError) as exc:
        PLUGIN.pre_apply(doc, say=lambda _line: None, client_factory=fake.client)
    assert "the workspace has no such folder" in str(exc.value)
    assert "A workspace admin creates /genie/sales" in exc.value.remediation[0]
    for dry_run in (True, False):
        run = Run(fake)
        with pytest.raises(genie.GenieError):
            run(doc, dry_run=dry_run)
        assert run.writes() == []
    assert fake.genie_spaces == {}


@pytest.mark.parametrize(
    "folder, level, harm",
    [
        ("/genie", "CAN_MANAGE", "read every reader's conversations"),  # inherited
        (SALES, "CAN_EDIT", "edit the space's instructions"),
    ],
)
def test_a_parent_folder_every_user_can_edit_is_refused(fake, folder, level, harm):
    in_sales(fake)
    fake.folder_acl[folder] = {("group_name", "users"): level}
    doc = site_contract(parentPath=SALES)
    with pytest.raises(genie.GenieError) as before_tofu:
        PLUGIN.pre_apply(doc, say=lambda _line: None, client_factory=fake.client)
    run = Run(fake)
    with pytest.raises(genie.GenieError) as exc:
        run(doc)
    for error in (before_tofu.value, exc.value):
        assert f"every workspace user holds {level} on the folder {SALES}" in str(error)
        assert harm in str(error)
        assert "away from users" in error.remediation[0]
    assert run.writes() == [] and fake.genie_spaces == {}


def test_a_parent_folder_every_user_can_open_is_said_not_refused(fake):
    in_sales(fake)
    fake.folder_acl[SALES] = {("group_name", "users"): "CAN_RUN"}
    run = Run(fake)(site_contract(parentPath=SALES))
    assert "note: every workspace user holds CAN_RUN on /genie/sales" in run.text
    assert only_space(fake)["parent_path"] == SALES


def test_the_account_users_group_counts_as_every_user(fake):
    """Under identity federation the account's own all-users group can be granted on a folder."""
    in_sales(fake)
    fake.folder_acl["/genie"] = {("group_name", "account users"): "CAN_MANAGE"}
    run = Run(fake)
    with pytest.raises(genie.GenieError) as exc:
        run(site_contract(parentPath=SALES))
    assert "holds CAN_MANAGE on the folder /genie/sales (the account users group)" in str(exc.value)
    assert "away from account users" in exc.value.remediation[0]
    assert run.writes() == []


class _FolderClient:
    """Answers the two folder reads with canned documents, as an odd service might."""

    def __init__(self, status: Any, acl: Any):
        self.api_client = self
        self._status, self._acl = status, acl
        self.paths: List[str] = []

    def do(self, method: str, path: str, query: Any = None, body: Any = None) -> Any:
        self.paths.append(path)
        return self._status if path == genie.WORKSPACE_STATUS_PATH else self._acl


_DIR = {"object_type": "DIRECTORY", "object_id": 5}
_ADMINS = {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]}


@pytest.mark.parametrize(
    "acl",
    [
        None,
        [],
        {},
        {"access_control_list": []},
        {"access_control_list": [_ADMINS, {"group_name": "users"}]},
        {"access_control_list": [_ADMINS, {"group_name": "users", "all_permissions": None}]},
        {"access_control_list": [_ADMINS, {"group_name": "users", "all_permissions": ["x"]}]},
        {
            "access_control_list": [
                _ADMINS,
                {"group_name": "users", "all_permissions": [{"permission_level": "CAN_OWN"}]},
            ]
        },
    ],
)
def test_folder_permissions_of_another_shape_stop_the_apply(acl):
    """Every folder lists admins at least: an answer that says less could hide a grant."""
    with pytest.raises(genie.GenieError):
        genie._check_folder(_FolderClient(_DIR, acl), SALES, lambda _line: None)


def test_a_level_in_another_case_counts_and_a_folder_without_its_id_stops():
    acl = {
        "access_control_list": [
            _ADMINS,
            {"group_name": "users", "all_permissions": [{"permission_level": "can_manage"}]},
        ]
    }
    with pytest.raises(genie.GenieError) as exc:
        genie._check_folder(_FolderClient(_DIR, acl), SALES, lambda _line: None)
    assert "holds CAN_MANAGE" in str(exc.value)
    no_id = _FolderClient({"object_type": "DIRECTORY"}, {"access_control_list": [_ADMINS]})
    with pytest.raises(genie.GenieError) as exc:
        genie._check_folder(no_id, SALES, lambda _line: None)
    assert "without its object id" in str(exc.value)
    assert not any("None" in path for path in no_id.paths)


def test_a_move_onto_a_title_the_folder_holds_says_so(fake):
    in_sales(fake)
    Run(fake)(site_contract())
    fake.add_genie_space("Site 360", "another product's", parent_path=SALES)
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract(parentPath=SALES))
    assert "another space there is already called 'Site 360'" in str(exc.value)
    assert "title of its own" in exc.value.remediation[0]


def test_folder_permissions_this_identity_cannot_read_stop_the_apply(fake):
    in_sales(fake)
    fake.fail_route("GET", "/api/2.0/permissions/directories/", 403, "no CAN_MANAGE")
    run = Run(fake)
    with pytest.raises(genie.GenieError) as exc:
        run(site_contract(parentPath=SALES))
    assert "could not read the permissions on the folder /genie/sales" in str(exc.value)
    assert run.writes() == []


def test_a_parent_path_that_is_a_file_is_refused(fake):
    fake._put_file("/genie/sales", b"not a folder")
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract(parentPath=SALES))
    assert "which is a file, and a Genie space goes in a folder" in str(exc.value)


def test_a_new_space_that_does_not_land_in_the_parent_path_is_trashed(fake):
    in_sales(fake)
    fake.genie_ignores_parent_path = True
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract(parentPath=SALES))
    assert "not in /genie/sales, where the next apply looks for it" in str(exc.value)
    assert fake.genie_spaces == {}


def test_a_move_the_service_does_not_make_stops_the_apply(fake):
    in_sales(fake)
    Run(fake)(site_contract())
    fake.genie_ignores_parent_path = True
    with pytest.raises(genie.GenieError) as exc:
        Run(fake)(site_contract(parentPath=SALES))
    assert "accepted the move" in str(exc.value) and "by hand" in exc.value.remediation[0]


def test_dropping_the_block_leaves_a_space_in_the_parent_path_with_a_note(fake):
    """Without the block the apply no longer knows which folder it trusts, so it does not trash
    a space there; the note names it for the person removing the block."""
    in_sales(fake)
    Run(fake)(site_contract(parentPath=SALES))
    space_id = only_space(fake)["space_id"]
    run = Run(fake)(without_block(site_contract()))
    assert set(fake.genie_spaces) == {space_id} and run.writes() == []
    assert f"left alone: space {space_id}" in run.text and SALES in run.text
    assert "left in a folder the contract no longer names as" in run.text


# ---------------------------------------------------------------------- plan, diff, generate iac


def test_the_plan_lists_the_space_as_an_action_outside_the_module(monkeypatch):
    from fluid_build.iac.shadow import native_logical_resources

    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    doc = site_contract(parentPath=SALES)
    (action,) = [
        a for a in dbx.planned_objects(doc) if a["resource_type"] == "databricks_genie_space"
    ]
    assert action == {
        "action_id": f"genie_space_{PRODUCT}",
        "op": "databricks.genie_space.converge",
        "action_type": "databricks.genie_space.converge",
        "provider": "databricks",
        "resource_type": "databricks_genie_space",
        "resource_id": PRODUCT,
        "description": "Genie space 'Site 360' in /genie/sales: fluid apply creates or updates "
        "it through the Genie API, after tofu apply",
        "title": "Site 360",
        "parent_path": SALES,
        "tables": [TABLE],
        "readers": list(space_of(doc).readers),
    }
    # No logical resource for the engines' parity check, and the module is the same with it.
    assert native_logical_resources([action]) == set()
    assert PLUGIN.emit(doc, [action]) == PLUGIN.emit(doc)
    assert not [
        a
        for a in dbx.planned_objects(without_block(doc))
        if a["resource_type"] == "databricks_genie_space"
    ]
    # fluid plan's hook lists the same action, and none for a contract the emitter refuses.
    assert PLUGIN.planned_output_ports(doc) == [action]
    assert PLUGIN.planned_output_ports(without_block(doc)) == []
    with pytest.raises(UnsupportedBindingError):  # fluid plan says it as a warning
        PLUGIN.planned_output_ports(site_contract(parentPath="/Shared/x"))
    (own,) = [
        a
        for a in dbx.planned_objects(site_contract())
        if a["resource_type"] == "databricks_genie_space"
    ]
    assert own["parent_path"] is None and " in " not in own["description"].split(":")[0]


def _plan_args(**kw: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**{"provider": None, "contract": None, **kw})


def _planned_spaces(doc: Dict[str, Any], **kw: Any) -> List[Dict[str, Any]]:
    from fluid_build.cli import plan as plan_cli

    plan: Dict[str, Any] = {"actions": []}
    plan_cli._append_output_port_actions(plan, doc, _plan_args(**kw), logging.getLogger("t"))
    return [a for a in plan["actions"] if a.get("resource_type") == "databricks_genie_space"]


def test_the_plan_reads_the_contract_as_the_apply_does(monkeypatch):
    """Its {{ env.* }} resolved: the readers are who the apply shares the space with."""
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    monkeypatch.setenv("FLUID_TEST_NOC", "noc@towers.io")
    doc = site_contract()
    doc["exposes"][0]["binding"]["principals"][NOC] = "{{ env.FLUID_TEST_NOC }}"
    (listed,) = _planned_spaces(doc)
    assert "noc@towers.io" in listed["readers"]
    assert not any("{{" in reader for reader in listed["readers"])


def test_the_plan_lists_the_space_only_for_the_provider_the_apply_runs(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    doc = site_contract()
    doc["exposes"].append(
        {
            "exposeId": "events",
            "kind": "table",
            "binding": {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {"project": "p-1", "dataset": "d", "table": "events"},
            },
            "contract": {"schema": [{"name": "id", "type": "STRING"}]},
        }
    )
    assert len(_planned_spaces(doc, provider="databricks")) == 1
    assert _planned_spaces(doc, provider="gcp") == []
    assert _planned_spaces(doc) == []  # two clouds: the apply needs --provider too


def test_a_refused_contract_is_said_by_the_plan_not_dropped(monkeypatch, caplog):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    with caplog.at_level(logging.WARNING):
        assert _planned_spaces(site_contract(parentPath="/Shared/x")) == []
    assert "plan_output_ports_skipped" in caplog.text and "databricks-genie-folder" in caplog.text


def test_a_plan_that_already_holds_the_space_does_not_list_it_twice(monkeypatch):
    """The native planner (``provider.plan``, the path a plan without the action parser takes)
    lists the space through ``planned_objects`` already."""
    from fluid_build.cli import plan as plan_cli

    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-1")
    doc = site_contract(parentPath=SALES)
    plan = {"actions": dbx.planned_objects(doc)}
    before = len(plan["actions"])
    plan_cli._append_output_port_actions(plan, doc, _plan_args(), logging.getLogger("t"))
    assert len(plan["actions"]) == before
    assert [a for a in plan["actions"] if a["resource_type"] == "databricks_genie_space"]


def test_the_plan_lists_the_space_of_a_product_with_a_local_dbt_build(monkeypatch, tmp_path, capfd):
    """A local dbt project is read from the contract's directory, which the plan must know."""
    from ._databricks_cli import isolate_workspace, make_contract, run_fluid, write_contract

    isolate_workspace(
        monkeypatch,
        tmp_path,
        host="https://adb-1.azuredatabricks.net",
        warehouse_id="wh-0123456789abcdef",
        plugin_cache=str(tmp_path),
    )
    project = tmp_path / "dbt"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
    (project / "models" / "orders.sql").write_text("select 1 as id", encoding="utf-8")
    doc = make_contract()
    doc["builds"] = [
        {
            "id": "dbt_daily",
            "engine": "dbt",
            "pattern": "hybrid-reference",
            "repository": "./dbt",
            "properties": {"model": "orders", "select": "orders+"},
            "outputs": ["orders"],
            "execution": {
                "trigger": {"type": "schedule", "cron": "0 2 * * *"},
                "runtime": {"platform": "databricks"},
            },
        }
    ]
    doc["extensions"] = {genie.EXTENSION_KEY: {}}
    path = write_contract(tmp_path, doc)
    rc, out = run_fluid(capfd, "plan", path, "--out", str(tmp_path / "plan.json"))
    assert rc == 0, out
    actions = json.loads((tmp_path / "plan.json").read_text())["actions"]
    assert [
        a["resource_id"] for a in actions if a.get("resource_type") == "databricks_genie_space"
    ] == ["dbx.fake.orders"]


def test_generate_iac_notes_the_space_its_module_does_not_hold():
    (notice,) = PLUGIN.standalone_module_notices(site_contract())
    assert "extensions.databricksGenie asks for a Genie space" in notice
    assert "a plain tofu apply of this module makes no space" in notice
    assert PLUGIN.standalone_module_notices(without_block(site_contract())) == []


def test_the_cli_plans_lists_diffs_and_notes_the_space(monkeypatch, tmp_path, capfd):
    from ._databricks_cli import isolate_workspace, make_contract, run_fluid, write_contract

    isolate_workspace(
        monkeypatch,
        tmp_path,
        host="https://adb-1.azuredatabricks.net",
        warehouse_id="wh-0123456789abcdef",
        plugin_cache=str(tmp_path),
    )
    doc = make_contract()
    doc["extensions"] = {genie.EXTENSION_KEY: {"parentPath": SALES}}
    path = write_contract(tmp_path, doc)

    rc, out = run_fluid(capfd, "generate", "iac", path, "--out", str(tmp_path / "iac"))
    assert rc == 0, out
    assert "Note: extensions.databricksGenie asks for a Genie space, which this module" in out
    assert "databricks_genie" not in (tmp_path / "iac" / "main.tf.json").read_text()

    rc, out = run_fluid(capfd, "plan", path, "--out", str(tmp_path / "plan.json"))
    assert rc == 0, out
    plan = json.loads((tmp_path / "plan.json").read_text())
    (listed,) = [a for a in plan["actions"] if a.get("resource_type") == "databricks_genie_space"]
    assert listed["resource_id"] == "dbx.fake.orders" and listed["parent_path"] == SALES
    assert listed["step"] == plan["total_actions"] == len(plan["actions"])
    assert listed["action_id"] in plan["dependency_graph"]["nodes"]
    assert f"genie_space_dbx.fake.orders ({genie.PLAN_OP})" in out

    # Listed among what the contract asks for, and no drift: nothing compares it with an apply.
    rc, out = run_fluid(
        capfd, "diff", path, "--exit-on-drift", "--out", str(tmp_path / "drift.json")
    )
    assert rc == 0, out
    report = json.loads((tmp_path / "drift.json").read_text())
    assert "databricks_genie_space:dbx.fake.orders" in report["changes"]["added"]
    assert report["summary"]["has_drift"] is False
