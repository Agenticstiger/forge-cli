# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Contract DQ rules as dbt tests in a Databricks dbt job (``databricks_dbt_tests.py``).

Stage 1 unit: no ``tofu``, no network. Each generated file is rendered with Jinja in a stand-in
for dbt's context (``config``, ``ref``, ``source``, ``adapter``), so what dbt would run is what
is asserted. The live proof (Databricks Free Edition) is recorded in HONESTLY_TESTED.md.
"""

from __future__ import annotations

import base64
import copy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import jinja2
import pytest

from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_dbt_tests as dq
from fluid_build.iac.providers.databricks_access import string_literal
from fluid_build.iac.source import contract_source
from fluid_build.util.dq_checks import DqCheck, Reference

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

WAREHOUSE = "wh-0123456789abcdef"
GIT = "https://github.com/o/r?tag=v1.2.0"

RULES: List[Dict[str, Any]] = [
    {"id": "orders_id_unique", "type": "uniqueness", "selector": "id", "severity": "critical"},
    {
        "id": "orders_complete",
        "type": "completeness",
        "selector": "id,status",
        "threshold": 1.0,
        "operator": ">=",
        "severity": "error",
    },
    {
        "id": "orders_status_valid",
        "type": "valid_values",
        "selector": "status",
        "severity": "error",
    },
    {
        "id": "orders_country_valid",
        "type": "valid_values",
        "selector": "country",
        "severity": "warn",
    },
    {
        "id": "orders_amount_positive",
        "type": "valid_values",
        "selector": "amount",
        "severity": "error",
    },
    {
        "id": "orders_customer_ref",
        "type": "valid_values",
        "selector": "customer_id",
        "severity": "warn",
    },
    {
        "id": "orders_fresh",
        "type": "freshness",
        "selector": "loaded_at",
        "window": "PT2H",
        "operator": "<=",
        "severity": "warn",
    },
    {"id": "orders_schema", "type": "schema", "selector": "*", "severity": "error"},
    {
        "id": "orders_status_mostly",
        "type": "completeness",
        "selector": "status",
        "threshold": 0.95,
        "operator": ">=",
        "severity": "error",
    },
]
SCHEMA: List[Dict[str, Any]] = [
    {"name": "id", "type": "bigint"},
    {
        "name": "status",
        "type": "string",
        "validationRules": [{"type": "enum", "constraint": "NEW | SHIPPED"}],
    },
    {
        "name": "country",
        "type": "string",
        "validationRules": [{"type": "regex", "constraint": "^[A-Z]{2}$"}],
    },
    {
        "name": "amount",
        "type": "decimal(12,2)",
        "validationRules": [{"type": "range", "constraint": ">= 0"}],
    },
    {"name": "customer_id", "type": "string", "labels": {"references": "crm.customers_v1.id"}},
    {"name": "loaded_at", "type": "timestamp"},
]
SOURCES = (
    "version: 2\nsources:\n  - name: crm\n    tables:\n      - name: customers\n"
    "  - name: other\n    tables:\n      - name: payments\n"
)
USER_YAML = "# the user's own model YAML, comments and all\nversion: 2\nmodels:\n  - name: orders\n"


def _contract(repository: str = "./dbt", rules: Optional[List[Dict[str, Any]]] = None):
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "consumes": [{"productId": "crm.customers_v1", "exposeId": "customers"}],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "sales", "table": "orders"},
                },
                "contract": {
                    "schema": copy.deepcopy(SCHEMA),
                    "dq": {"rules": copy.deepcopy(RULES if rules is None else rules)},
                },
            }
        ],
        "builds": [
            {
                "id": "transform",
                "engine": "dbt",
                "repository": repository,
                "outputs": ["orders"],
                "properties": {"model": "orders"},
                "execution": {"runtime": {"platform": "databricks"}},
            }
        ],
    }


def _project(tmp_path: Path, files: Optional[Dict[str, str]] = None, config: str = "") -> Path:
    root = tmp_path / "dbt"
    root.mkdir(exist_ok=True)
    (root / "dbt_project.yml").write_text(f"name: p\n{config}", encoding="utf-8")
    default = {
        "models/orders.sql": "select 1 as id\n",
        "models/orders.yml": USER_YAML,
        "models/sources.yml": SOURCES,
    }
    for rel, text in (default if files is None else files).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _compile(contract, tmp_path: Optional[Path], *, validating: bool = False):
    source = tmp_path / "contract.fluid.yaml" if tmp_path is not None else None
    with contract_source(source):
        return dbx._compile(contract, {dbx.WAREHOUSE_ENV: WAREHOUSE}, validating=validating)


def _kinds(compiled) -> List[str]:
    return [refusal.kind for refusal in compiled.refusals]


def _uploaded(compiled) -> Dict[str, str]:
    """``{path below the upload folder: text}`` of every workspace file."""
    out = {}
    for body in compiled.resources.get("databricks_workspace_file", {}).values():
        rel = str(body["path"]).split("/files/", 1)[1]
        out[rel] = base64.b64decode(body["content_base64"]).decode("utf-8")
    return out


# ── a dbt-shaped Jinja context ───────────────────────────────────────────────


class _Column:
    def __init__(self, name: str, dtype: str) -> None:
        self.name, self.dtype = name, dtype


class _Adapter:
    def __init__(self, columns: List[_Column]) -> None:
        self.columns = columns
        self.calls: List[tuple] = []

    def dispatch(self, name: str, namespace: str):
        def call(*args: Any) -> str:
            self.calls.append((name, namespace, args))
            return f"<{name}>"

        return call

    def get_columns_in_relation(self, relation: Any) -> List[_Column]:
        return self.columns


def _render(text: str, columns: Optional[List[_Column]] = None, *, execute: bool = True):
    """Render a generated file as dbt would: ``(sql, config kwargs, adapter calls)``.
    ``execute=False`` is dbt's parse, which records the ``ref`` calls as the test's parents."""
    configs: Dict[str, Any] = {}
    adapter = _Adapter(columns or [])
    env = jinja2.Environment(extensions=["jinja2.ext.do"], undefined=jinja2.StrictUndefined)
    refs: List[str] = []

    def ref(name: str) -> str:
        refs.append(name)
        return f"`main`.`sales`.`{name}`"

    sql = env.from_string(text).render(
        config=lambda **kwargs: configs.update(kwargs) or "",
        ref=ref,
        source=lambda source, table: f"`main`.`{source}`.`{table}`",
        adapter=adapter,
        execute=execute,
    )
    _render.refs = refs  # type: ignore[attr-defined]
    return sql, configs, adapter.calls


def _check(kind: str, **fields: Any) -> DqCheck:
    base = {"name": "r", "rule_id": "r", "rule_type": "valid_values", "expose_id": "orders"}
    return DqCheck(**{**base, "kind": kind, "severity": "error", "column": "c", **fields})


# ── the job ──────────────────────────────────────────────────────────────────


class TestTheJobUploadsATestPerCheck:
    def test_each_check_is_a_test_file_beside_the_users_files(self, tmp_path):
        _project(tmp_path)
        compiled = _compile(_contract(), tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        uploaded = _uploaded(compiled)
        tests = sorted(p for p in uploaded if p.startswith("tests/fluid_dq/orders/"))
        assert tests == sorted(
            f"tests/fluid_dq/orders/{name}.sql"
            for name in [
                "orders_id_unique",
                "orders_complete__id",
                "orders_complete__status",
                "orders_status_valid",
                "orders_country_valid",
                "orders_amount_positive",
                "orders_customer_ref",
                "orders_fresh",
                "orders_schema",
                "orders_status_mostly",
            ]
        )
        # The user's own files upload byte for byte: no entry is merged into them.
        assert uploaded["models/orders.yml"] == USER_YAML
        (job,) = compiled.jobs
        assert len(job.tests) == 10
        (task,) = compiled.resources["databricks_job"][
            next(iter(compiled.resources["databricks_job"]))
        ]["task"]
        assert task["dbt_task"]["commands"] == ["dbt deps", "dbt build --select +orders+"]

    def test_the_plan_lists_the_tests_of_the_job(self, tmp_path, monkeypatch):
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        _project(tmp_path)
        with contract_source(tmp_path / "contract.fluid.yaml"):
            actions = dbx.planned_objects(_contract())
        (job,) = [a for a in actions if a["op"] == "databricks.job.ensure"]
        assert "orders_id_unique" in job["dq_tests"]

    def test_a_contract_without_rules_uploads_no_test(self, tmp_path):
        _project(tmp_path)
        compiled = _compile(_contract(rules=[]), tmp_path)
        assert not compiled.refusals
        assert not [p for p in _uploaded(compiled) if "fluid_dq" in p]
        assert compiled.jobs[0].tests == ()

    def test_the_first_test_path_holds_the_tests(self, tmp_path):
        _project(tmp_path, config="test-paths: ['checks', 'more']\n")
        compiled = _compile(_contract(), tmp_path)
        assert any(p.startswith("checks/fluid_dq/orders/") for p in _uploaded(compiled))


class TestRefusals:
    def _refused(self, tmp_path, contract, kind: str) -> str:
        compiled = _compile(contract, tmp_path)
        assert f"databricks-job-dq-{kind}" in _kinds(compiled), _kinds(compiled)
        assert "databricks_job" not in compiled.resources  # never half a job
        return " ".join(str(r) + " " + " ".join(r.remediation) for r in compiled.refusals)

    def test_a_git_source_with_rules_is_refused(self, tmp_path):
        text = self._refused(tmp_path, _contract(GIT), "git-source")
        assert "local dbt project" in text

    def test_a_git_source_without_rules_still_runs(self, tmp_path):
        compiled = _compile(_contract(GIT, rules=[]), tmp_path)
        assert not compiled.refusals
        assert "databricks_job" in compiled.resources

    def test_a_rule_no_test_can_run_is_refused(self, tmp_path):
        _project(tmp_path)
        rules = [{"id": "drift", "type": "drift_detection", "selector": "*", "severity": "warn"}]
        assert "the table's history" in self._refused(tmp_path, _contract(rules=rules), "rule")

    def test_a_model_the_project_does_not_hold_is_refused(self, tmp_path):
        _project(tmp_path, files={"models/other.sql": "select 1\n", "models/sources.yml": SOURCES})
        assert "'orders'" in self._refused(tmp_path, _contract(), "model")

    @pytest.mark.parametrize(
        "sources",
        [
            "version: 2\nsources: []\n",
            SOURCES + "  - name: crm_copy\n    tables:\n      - name: customers\n",
        ],
    )
    def test_a_reference_needs_exactly_one_source_table_for_the_consumed_expose(
        self, tmp_path, sources
    ):
        _project(tmp_path, files={"models/orders.sql": "select 1\n", "models/sources.yml": sources})
        text = self._refused(tmp_path, _contract(), "reference")
        assert "named 'customers'" in text

    def test_a_reference_to_a_product_not_consumed_is_refused(self, tmp_path):
        _project(tmp_path)
        contract = _contract()
        contract["consumes"] = []
        text = self._refused(tmp_path, contract, "reference")
        assert "consumes[] does not list 'crm.customers_v1'" in text

    def test_a_user_file_where_a_test_goes_is_refused(self, tmp_path):
        _project(tmp_path)
        clash = tmp_path / "dbt/tests/fluid_dq/orders/orders_schema.sql"
        clash.parent.mkdir(parents=True)
        clash.write_text("select 1\n", encoding="utf-8")
        self._refused(tmp_path, _contract(), "path")

    def test_a_test_name_the_projects_own_tests_use_is_refused(self, tmp_path):
        # dbt names singular tests project-wide: a clash fails the whole job's parse
        # ("dbt found two data_tests with the name"), so the plan refuses it first.
        _project(
            tmp_path,
            files={
                "models/orders.sql": "select 1\n",
                "models/sources.yml": SOURCES,
                "tests/mine/orders_schema.sql": "select 1 where false\n",
            },
        )
        assert "tests/mine/orders_schema.sql" in self._refused(tmp_path, _contract(), "name")

    def test_a_selection_that_leaves_the_model_out_is_refused(self, tmp_path):
        _project(tmp_path)
        contract = _contract()
        contract["builds"][0]["properties"] = {"select": ["other_model"]}
        assert "leaves the model out" in self._refused(tmp_path, contract, "not-selected")

    def test_a_selection_it_cannot_evaluate_is_left_to_the_runs_report(self, tmp_path):
        _project(tmp_path)
        contract = _contract()
        contract["builds"][0]["properties"] = {"select": ["tag:nightly"]}
        compiled = _compile(contract, tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        assert compiled.jobs[0].tests

    def test_a_sibling_output_without_rules_needs_no_model(self, tmp_path):
        # Only the exposes with rules are tested, so only their models are looked for.
        _project(tmp_path)
        contract = _contract()
        sibling = copy.deepcopy(contract["exposes"][0])
        sibling.update(exposeId="extra")
        sibling["binding"]["location"]["table"] = "extra"
        sibling["contract"]["dq"] = {"rules": []}
        contract["exposes"].append(sibling)
        contract["builds"][0]["outputs"] = ["orders", "extra"]
        compiled = _compile(contract, tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        assert all(
            p.startswith("tests/fluid_dq/orders/") for p in _uploaded(compiled) if "fluid_dq" in p
        )

    def test_each_table_of_a_build_without_outputs_is_tested_through_its_own_model(self, tmp_path):
        files = {
            "models/orders.sql": "select 1\n",
            "models/extra.sql": "select 1\n",
            "models/sources.yml": SOURCES,
        }
        _project(tmp_path, files=files)
        contract = _contract(rules=[])
        extra = copy.deepcopy(contract["exposes"][0])
        extra.update(exposeId="extra")
        extra["binding"]["location"]["table"] = "extra"
        extra["contract"]["dq"] = {
            "rules": [
                {"id": "extra_id", "type": "uniqueness", "selector": "id", "severity": "error"}
            ]
        }
        contract["exposes"].append(extra)
        del contract["builds"][0]["outputs"]  # writes every expose
        compiled = _compile(contract, tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        assert "tests/fluid_dq/extra/extra_id.sql" in _uploaded(compiled)

    def test_a_user_test_of_that_name_under_fluid_dq_is_refused_too(self, tmp_path):
        _project(
            tmp_path,
            files={
                "models/orders.sql": "select 1\n",
                "models/sources.yml": SOURCES,
                "tests/fluid_dq/legacy/orders_schema.sql": "select 1 where false\n",
            },
        )
        self._refused(tmp_path, _contract(), "name")

    def test_a_project_yaml_too_deep_to_read_is_passed_over(self, tmp_path):
        files = {
            "models/orders.sql": "select 1\n",
            "models/sources.yml": SOURCES,
            "models/deep.yml": "x: " + "[" * 5000 + "]" * 5000 + "\n",
        }
        _project(tmp_path, files=files)
        compiled = _compile(_contract(), tmp_path)
        assert not compiled.refusals, _kinds(compiled)

    def test_validate_defers_a_model_named_by_an_env_placeholder(self, tmp_path):
        # fluid validate reads the contract before {{ env.* }} is substituted.
        _project(tmp_path)
        contract = _contract()
        contract["exposes"][0]["binding"]["location"]["table"] = "{{ env.ORDERS_TABLE }}"
        del contract["builds"][0]["properties"]
        compiled = _compile(contract, tmp_path, validating=True)
        assert "databricks-job-dq-model" not in _kinds(compiled), _kinds(compiled)

    def test_an_empty_test_paths_is_refused(self, tmp_path):
        _project(tmp_path, config="test-paths: []\n")
        self._refused(tmp_path, _contract(), "test-path")

    def test_a_referenced_column_that_is_no_plain_identifier_is_refused(self, tmp_path):
        # The table's own columns are identifiers already (the table emitter refuses others);
        # a reference names a column of another product's table.
        _project(tmp_path)
        contract = _contract()
        column = contract["exposes"][0]["contract"]["schema"][4]
        column["labels"]["references"] = "crm.customers_v1.odd name"
        self._refused(tmp_path, contract, "column")

    def test_fluid_validate_without_a_contract_file_still_reads_the_rules(self):
        rules = [{"id": "a", "type": "anomaly_detection", "selector": "*", "severity": "warn"}]
        compiled = _compile(_contract(rules=rules), None, validating=True)
        assert "databricks-job-dq-rule" in _kinds(compiled)


# ── what dbt runs ────────────────────────────────────────────────────────────


PARSED = [
    ("not_null", {}),
    ("unique", {}),
    ("accepted_values", {"values": ("A",)}),
    ("regex", {"pattern": "^A$"}),
    ("bounds", {"bounds": ((">=", 0.0),)}),
    ("freshness", {"max_age_seconds": 60}),
    ("schema", {"column": None, "columns": (("id", "bigint"),)}),
    ("not_null", {"share": (">=", 0.5)}),
]


class TestRendering:
    @pytest.mark.parametrize("kind, fields", PARSED)
    def test_every_test_refs_its_model_while_dbt_parses(self, kind, fields):
        # dbt selects a test by the parents its parse recorded (``execute`` false):
        # a ``ref`` only behind ``if execute`` left the schema test unselected, live.
        _render(dq.render(_check(kind, **fields), "orders"), execute=False)
        assert _render.refs == ["orders"]  # type: ignore[attr-defined]

    def test_the_config_sets_severity_enabled_tag_and_the_rule(self):
        _, configs, _ = _render(dq.render(_check("not_null", severity="warn"), "orders"))
        assert configs == {
            "severity": "warn",
            "enabled": True,
            "tags": ["fluid_dq"],
            "meta": {"fluid_rule_id": "r", "fluid_expose_id": "orders"},
        }

    @pytest.mark.parametrize(
        "kind, test", [("not_null", "test_not_null"), ("unique", "test_unique")]
    )
    def test_not_null_and_unique_run_dbts_own_tests(self, kind, test):
        _, _, calls = _render(dq.render(_check(kind), "orders"))
        assert calls == [(test, "dbt", ("`main`.`sales`.`orders`", "`c`"))]

    def test_accepted_values_passes_databricks_literals_unquoted_by_dbt(self):
        hostile = "O'Re}}il{%ly\\ #}"
        _, _, calls = _render(dq.render(_check("accepted_values", values=("A", hostile)), "orders"))
        ((name, _, args),) = calls
        assert name == "test_accepted_values"
        assert args[2] == [string_literal("A"), string_literal(hostile)]
        assert args[3] is False  # dbt adds no quotes of its own

    def test_a_reference_is_dbts_relationships_against_the_source_table(self):
        check = _check("reference", reference=Reference("crm.customers_v1", "customers", "id"))
        _, _, calls = _render(dq.render(check, "orders", ("crm", "customers")))
        assert calls == [
            (
                "test_relationships",
                "dbt",
                ("`main`.`sales`.`orders`", "`c`", "`main`.`crm`.`customers`", "`id`"),
            )
        ]

    def test_a_regex_selects_the_non_null_values_that_do_not_match(self):
        sql, _, _ = _render(dq.render(_check("regex", pattern="^\\d{2}'}}$"), "orders"))
        literal = string_literal("^\\d{2}'}}$")
        assert f"where `c` is not null and not (cast(`c` as string) rlike {literal})" in sql, sql

    def test_bounds_select_the_values_out_of_range(self):
        check = _check("bounds", bounds=((">=", 0.0), ("<", 1.5)))
        sql, _, _ = _render(dq.render(check, "orders"))
        assert "where `c` is not null and not (`c` >= 0 and `c` < 1.5)" in sql

    @pytest.mark.parametrize("strict, older", [(False, "<"), (True, "<=")])
    def test_freshness_fails_on_no_row_or_an_old_newest_value(self, strict, older):
        check = _check("freshness", max_age_seconds=7200, strict=strict)
        sql, _, _ = _render(dq.render(check, "orders"))
        assert "max(`c`) as newest_value from `main`.`sales`.`orders`" in sql
        assert "where newest_value is null" in sql
        assert f"newest_value {older} timestampadd(SECOND, -7200, current_timestamp())" in sql

    def test_a_share_fails_with_the_measured_share_when_it_misses(self):
        check = _check("not_null", share=(">=", 0.95))
        sql, _, _ = _render(dq.render(check, "orders"))
        assert "try_divide(count(`c`), count(*)) as passing_share" in sql
        assert "where passing_share is not null and not (passing_share >= 0.95)" in sql

    def test_a_reference_share_counts_the_values_found_in_the_parent(self):
        check = _check(
            "reference",
            share=(">", 0.5),
            reference=Reference("crm.customers_v1", "customers", "id"),
        )
        sql, _, _ = _render(dq.render(check, "orders", ("crm", "customers")))
        assert "try_divide(count(parent.reference_value), count(child.`c`))" in sql
        assert "from `main`.`crm`.`customers`) as parent" in sql
        assert "not (passing_share > 0.5)" in sql

    def test_schema_reports_each_missing_or_retyped_column(self):
        check = _check(
            "schema",
            column=None,
            columns=(
                ("id", "bigint"),
                ("amount", "decimal(12,2)"),
                ("gone", "string"),
                ("n", "number"),
            ),
        )
        text = dq.render(check, "orders")
        columns = [
            _Column("ID", "BIGINT"),
            _Column("amount", "decimal(10, 2)"),
            _Column("n", "decimal(10,0)"),
        ]
        sql, _, _ = _render(text, columns)
        rows = [line for line in sql.splitlines() if line.startswith("select ")]
        assert rows == [
            "select 'amount' as column_name, 'type' as problem, 'decimal(12,2)' as expected_type,"
            " 'decimal(10,2)' as actual_type",
            "select 'gone' as column_name, 'missing' as problem, 'string' as expected_type,"
            " '' as actual_type",
        ]
        assert "union all" in sql

    def test_a_conforming_schema_selects_no_row(self):
        check = _check("schema", column=None, columns=(("id", "bigint"),))
        sql, _, _ = _render(dq.render(check, "orders"), [_Column("id", "bigint")])
        assert "where 1 = 0" in sql

    def test_a_decimal_without_scale_expects_the_scale_databricks_reports(self):
        check = _check("schema", column=None, columns=(("amount", "decimal(12)"),))
        sql, _, _ = _render(dq.render(check, "orders"), [_Column("amount", "decimal(12,0)")])
        assert "where 1 = 0" in sql, sql

    def test_a_character_outside_the_bmp_reaches_dbt_whole(self):
        # ensure_ascii wrote it as a surrogate pair, which dbt could not encode.
        text = dq.render(_check("accepted_values", rule_id="r😀", values=("NEW😀",)), "orders")
        _, configs, calls = _render(text)
        assert configs["meta"]["fluid_rule_id"] == "r😀"
        assert calls[0][2][2] == ["'NEW😀'"]
        text.encode("utf-8")

    def test_a_type_the_warehouse_reports_cannot_break_out_of_its_literal(self):
        check = _check("schema", column=None, columns=(("id", "bigint"),))
        sql, _, _ = _render(dq.render(check, "orders"), [_Column("id", "x' or '1'='1\\")])
        # Spaces are folded away first (``decimal(10, 2)`` is ``decimal(10,2)``).
        assert "'x\\'or\\'1\\'=\\'1\\\\' as actual_type" in sql


# ── the run's results ───────────────────────────────────────────────────────

LOG = """\
12:00:01  1 of 4 START test orders_id_unique .................................. [RUN]
12:00:02  1 of 4 PASS orders_id_unique ........................................ [PASS in 1.02s]
12:00:02  2 of 4 FAIL 3 orders_complete__id ................................... [FAIL 3 in 0.88s]
12:00:03  3 of 4 WARN 1 orders_fresh .......................................... [WARN 1 in 0.51s]
12:00:03  4 of 4 SKIP test orders_schema ...................................... [SKIP]
12:00:04  Done. PASS=1 WARN=1 ERROR=1 SKIP=1 NO-OP=0 TOTAL=4
"""


class TestReport:
    def test_results_read_dbts_line_for_each_test(self):
        assert dq.results(LOG) == {
            "orders_id_unique": ("PASS", None),
            "orders_complete__id": ("FAIL", 3),
            "orders_fresh": ("WARN", 1),
            "orders_schema": ("SKIP", None),
        }

    def test_the_report_lists_every_rule_and_what_dbt_said(self):
        checks = [
            _check("unique", name="orders_id_unique", rule_type="uniqueness", column="id"),
            _check("not_null", name="orders_complete__id", rule_type="completeness", column="id"),
            _check("freshness", name="orders_fresh", rule_type="freshness", severity="warn"),
            _check("schema", name="orders_schema", rule_type="schema", column=None),
            _check("not_null", name="orders_gone", rule_type="completeness"),
        ]
        lines = dq.report(LOG, checks, truncated=True)
        assert lines[0] == (
            "   contract rules as dbt tests: 1 pass, 1 warn, 1 fail, 1 skip, 1 not reported"
        )
        assert "✗ orders_complete__id: FAIL 3 rows - completeness id, severity error" in lines[2]
        assert lines[-1].endswith(
            "orders_gone: not in the task's output (Databricks kept only its end) - "
            "completeness c, severity error"
        )

    def test_no_rules_no_report(self):
        assert dq.report(LOG, []) == []

    def test_a_run_that_reached_no_test_says_so_once(self):
        checks = [_check("not_null", name="a"), _check("unique", name="b")]
        (line,) = dq.report("12:00:01  Encountered an error:\nCompilation Error", checks)
        assert "none of the 2 reported" in line

    def test_expected_tests_come_from_the_contract_alone(self):
        contract = _contract()
        names = [c.name for c in dq.expected_tests(contract, contract["builds"][0])]
        assert names[0] == "orders_id_unique" and len(names) == 10


# ── real dbt: the generated tests parse, and the build's selection picks each one ───────


def _find_dbt() -> Optional[str]:
    candidate = Path(sys.executable).parent / "dbt"
    return str(candidate) if candidate.exists() else shutil.which("dbt")


@pytest.mark.integration
def test_dbt_parses_the_upload_and_selects_every_rules_test(tmp_path):
    """dbt (any adapter) parses the uploaded project and ``dbt ls --select +orders+`` lists
    each generated test: a test whose ``ref`` dbt cannot see at parse time would be missing,
    as the schema test was on a live run before it was fixed."""
    dbt = _find_dbt()
    if dbt is None:
        pytest.skip("dbt CLI not available")
    pytest.importorskip("dbt.adapters.duckdb")
    _project(tmp_path, config="profile: p\nconfig-version: 2\nversion: '1.0'\n")
    compiled = _compile(_contract(), tmp_path)
    assert not compiled.refusals, _kinds(compiled)
    upload = tmp_path / "upload"
    for rel, text in _uploaded(compiled).items():
        target = upload / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (upload / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: ':memory:'\n",
        encoding="utf-8",
    )
    listed = subprocess.run(
        [dbt, "ls", "--resource-type", "test", "--select", "+orders+", "--output", "name"]
        + ["--project-dir", str(upload), "--profiles-dir", str(upload), "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        cwd=upload,
        timeout=300,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert set(listed.stdout.split()) == set(compiled.jobs[0].tests)


def test_the_keys_task_runs_once_dbt_has_ended_whatever_its_outcome():
    # A failing error-severity rule fails dbt after CREATE OR REPLACE dropped the table's
    # keys; the fluid_keys task (main's #860) must still declare them again.
    from fluid_build.iac.providers import databricks_jobs as jobs

    item = jobs._Build({"id": "transform"}, "transform", "builds[0]", "sales.orders", WAREHOUSE)
    task = jobs._keys_task(item)
    assert task["depends_on"] == [{"task_key": jobs.DBT_TASK_KEY}]
    assert task["run_if"] == "ALL_DONE"
