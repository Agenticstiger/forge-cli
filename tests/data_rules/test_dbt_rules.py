# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Declared invariants as dbt ``fluid_rule`` tests (``fluid_build.data_rules._dbt``).

Unit tests pin the Jinja escaping, the exact band predicate, the test names, the
placeholder ``where`` and what the exporter and ``fluid generate dbt-tests`` write. The
end-to-end tests run real dbt-duckdb over a generated project: every rule's outcome is the
compiler's count against its band, a hostile name or literal stays data, a placeholder row
is exempt from ``accepted_values``, and an adapter with no SQL fails loudly.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import jinja2
import pytest
import yaml

from fluid_build import fluid_sql as fs
from fluid_build.data_rules import _dbt
from fluid_build.data_rules._compile import Band
from fluid_build.exporters.dbt_tests import render_dbt_tests, render_project_tests

pytestmark = pytest.mark.unit

duckdb = pytest.importorskip("duckdb")


def _rule(entry: Dict[str, Any]) -> str:
    """A generated test's rule id, read back from its Jinja literal."""
    return jinja2.Environment().from_string(entry["fluid_rule"]["rule"]).render()


def _billing() -> Dict[str, Any]:
    sys.path.insert(0, str(Path(__file__).parent))
    from test_compile import _billing as billing

    return billing()


# -- Jinja ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        'it\'s \\ {{ y }} {% endraw %} {%- raw -%} {# c #} "q"',
        "ü 日本 😀 tab\there newline\nend",
        "{{ exceptions.raise_compiler_error('x') }}",
    ],
)
def test_jinja_text_renders_back_exactly(text: str) -> None:
    rendered = jinja2.Environment().from_string(_dbt.jinja_text(text)).render()
    assert rendered == text


def test_a_rule_sql_never_carries_raw_jinja() -> None:
    tests, _ = _dbt.rule_tests(_billing(), lambda e: e)
    for entries in tests.values():
        for entry in entries:
            body = entry["fluid_rule"]
            for text in [*body["sql"].values(), *body["holds"].values()]:
                # every piece is a {{ "..." }} literal or a ref("...") call
                env = jinja2.Environment()
                env.globals["ref"] = lambda name: f"<{name}>"
                assert "{{" in text and env.from_string(text).render()


# -- the band ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "band, covered, violating, holds",
    [
        (Band("mustBe", 0), 4, 0, True),
        (Band("mustBe", 0), 4, 1, False),
        (Band("mustBeLessOrEqualTo", 33.333333333333, "percent"), 3, 1, False),
        (Band("mustBeLessOrEqualTo", 33.333333333334, "percent"), 3, 1, True),
        (Band("mustBeLessThan", 1, "percent"), 0, 0, True),  # no covered row: 0 %
        (Band("mustBeGreaterThan", 0, "percent"), 0, 0, False),
        (Band("mustBeBetween", [1.5, 2.5], "percent"), 100, 2, True),
        (Band("mustBeBetween", [1.5, 2.5], "percent"), 100, 3, False),
        (Band("mustBeGreaterOrEqualTo", 2), 9, 2, True),
        (Band("mustBeBetween", [1, 3]), 9, 4, False),
    ],
)
def test_the_band_is_compared_exactly(
    band: Band, covered: int, violating: int, holds: bool
) -> None:
    predicate = _dbt.holds_sql("duckdb", band)
    db = duckdb.connect()
    got = db.execute(
        f"SELECT {predicate} FROM (SELECT {covered}::BIGINT AS covered, "
        f"{violating}::BIGINT AS violating)"
    ).fetchone()[0]
    assert got is holds


def test_every_dialect_writes_a_band_sqlglot_parses() -> None:
    sqlglot = pytest.importorskip("sqlglot")
    for dialect in fs.DIALECTS:
        for band in (Band("mustBe", 0), Band("mustBeBetween", [0.5, 2.5], "percent")):
            sql = f"SELECT 1 FROM t WHERE {_dbt.holds_sql(dialect, band)}"
            sqlglot.parse_one(sql, read={"athena": "trino"}.get(dialect, dialect))
    assert "BIGNUMERIC '0.5'" in _dbt.holds_sql("bigquery", Band("mustBe", 0.5))


def test_test_names_are_unique_and_safe() -> None:
    taken: set = set()
    assert _dbt.test_name("a.b", taken) == "fluid_a_b"
    assert _dbt.test_name("a_b", taken) == "fluid_a_b_2"
    assert _dbt.test_name("A_B", taken) == "fluid_A_B_3"
    assert _dbt.test_name("...", taken) == "fluid_rule"


# -- what the exporter writes --------------------------------------------------------------


def test_each_rule_is_one_model_level_test_with_sql_per_adapter() -> None:
    tests, skipped = _dbt.rule_tests(_billing(), lambda e: e)
    assert skipped == []
    names = [_rule(t) for t in tests["invoices"]]
    assert names == [
        "gross_from_net",
        "due_after_issue",
        "billed",
        "fee_is_monthly",
        "dep",
        "contract.status",
    ]
    body = tests["invoices"][1]["fluid_rule"]
    assert set(body["sql"]) == set(_dbt.ADAPTERS)
    assert body["config"] == {"severity": "warn"}
    assert body["name"] == "fluid_due_after_issue"
    assert 'ref("contracts")' in tests["invoices"][3]["fluid_rule"]["sql"]["duckdb"]


def test_a_rule_no_engine_renders_is_skipped_with_its_reason() -> None:
    contract = _billing()
    contract["exposes"][0]["contract"]["invariants"].append({"id": "bad", "check": "net LIKE 'x'"})
    tests, skipped = _dbt.rule_tests(contract, lambda e: e)
    assert [s.id for s in skipped] == ["bad"]
    assert "validator refuses it" in skipped[0].reason
    assert "bad" not in [_rule(t) for t in tests["invoices"]]


def test_a_contract_below_the_preview_renders_as_before() -> None:
    contract = _billing()
    contract["fluidVersion"] = "0.7.6"
    result = render_project_tests(contract)
    assert not result.uses_fluid_rule and "fluid_rule" not in result.yaml
    assert result.yaml == render_dbt_tests(contract)


def test_a_placeholder_is_exempt_from_accepted_values_and_range() -> None:
    contract = _billing()
    status = next(c for c in contract["exposes"][0]["contract"]["schema"] if c["name"] == "status")
    status["validationRules"] = [{"type": "enum", "constraint": "A, B"}]
    status["placeholders"] = {"values": ["?"], "prefixes": ["TBD_"]}
    doc = yaml.safe_load(render_dbt_tests(contract, tests_key="data_tests"))
    column = next(c for c in doc["models"][0]["columns"] if c["name"] == "status")
    where = column["data_tests"][0]["accepted_values"]["config"]["where"]
    assert where.startswith("{% if target.type in [") and where.endswith(
        "{% else %}TRUE{% endif %}"
    )
    env = jinja2.Environment()
    rendered = env.from_string(where).render(target=SimpleNamespace(type="duckdb"))
    assert (
        rendered == "NOT COALESCE(\"status\" IN ('?') OR SUBSTR(\"status\", 1, 4) = 'TBD_', FALSE)"
    )
    assert env.from_string(where).render(target=SimpleNamespace(type="redshift")) == "TRUE"


# -- fluid generate dbt-tests --------------------------------------------------------------


def _run_cli(tmp_path: Path, out: Path, contract: Dict[str, Any], **extra: Any) -> int:
    from fluid_build.cli.generate_dbt_tests import run

    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    args = SimpleNamespace(
        contract=str(path), out=str(out), env=None, dbt_tests_key="data_tests", **extra
    )
    import logging

    return run(args, logging.getLogger("test"))


def test_the_macro_goes_to_the_projects_first_macro_path(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text("name: p\nmacro-paths: [m2, m3]\n")
    assert _run_cli(tmp_path, tmp_path / "models" / "schema.yml", _billing()) == 0
    macro = tmp_path / "m2" / "fluid" / "fluid_rule.sql"
    assert macro.read_text().startswith(_dbt.MACRO_SENTINEL)
    # A re-run overwrites its own file.
    assert _run_cli(tmp_path, tmp_path / "models" / "schema.yml", _billing()) == 0


def test_macros_dir_wins_and_a_user_file_is_never_overwritten(tmp_path: Path) -> None:
    from fluid_build.cli._common import CLIError

    target = tmp_path / "mine"
    assert _run_cli(tmp_path, tmp_path / "s.yml", _billing(), macros_dir=str(target)) == 0
    macro = target / "fluid" / "fluid_rule.sql"
    assert macro.exists()
    macro.write_text("{% test fluid_rule(model) %}select 1{% endtest %}\n")
    with pytest.raises(CLIError) as refused:
        _run_cli(tmp_path, tmp_path / "s.yml", _billing(), macros_dir=str(target))
    assert "refusing to overwrite" in str(refused.value.context)


def test_without_a_project_or_flag_the_cli_says_what_to_pass(tmp_path: Path) -> None:
    from fluid_build.cli._common import CLIError

    with pytest.raises(CLIError) as refused:
        _run_cli(tmp_path, tmp_path / "s.yml", _billing())
    assert "--macros-dir" in refused.value.context["detail"]
    assert not (tmp_path / "s.yml").exists()  # nothing written before the refusal
    contract = _billing()
    contract["fluidVersion"] = "0.7.6"
    assert _run_cli(tmp_path, tmp_path / "s.yml", contract) == 0  # no invariants: no macro


def test_a_project_macro_path_outside_it_is_refused(tmp_path: Path) -> None:
    from fluid_build.cli._common import CLIError

    (tmp_path / "dbt_project.yml").write_text("name: p\nmacro-paths: ['../elsewhere']\n")
    with pytest.raises(CLIError) as refused:
        _run_cli(tmp_path, tmp_path / "models" / "schema.yml", _billing())
    assert "--macros-dir" in refused.value.context["detail"]
    assert not (tmp_path / "models" / "schema.yml").exists()
    (tmp_path / "dbt_project.yml").write_text("name: p\n")  # no macro-paths: dbt's macros/
    assert _run_cli(tmp_path, tmp_path / "models" / "schema.yml", _billing()) == 0
    assert (tmp_path / "macros" / "fluid" / "fluid_rule.sql").exists()


# -- real dbt ------------------------------------------------------------------------------

_REQUIRE_DBT = os.environ.get("FLUID_REQUIRE_DBT") == "1"


def _dbt_binary() -> str | None:
    beside = Path(sys.executable).parent / "dbt"
    if beside.exists():
        return str(beside)
    return shutil.which("dbt")


def _ensure_dbt() -> str:
    binary = _dbt_binary()
    if binary is None:
        if _REQUIRE_DBT:
            pytest.fail("FLUID_REQUIRE_DBT=1 and no dbt binary was found")
        pytest.skip("dbt not installed")
    pytest.importorskip("dbt.adapters.duckdb")
    return binary


ODD = 'odd "col" {{ x }}'


def _project_contract() -> Dict[str, Any]:
    contract = _billing()
    invoices = contract["exposes"][0]
    status = next(c for c in invoices["contract"]["schema"] if c["name"] == "status")
    status["validationRules"] = [
        {
            "type": "enum",
            "constraint": "'A', 'B', '{{ exceptions.raise_compiler_error(1337 * 1000 + 42) }}'",
        }
    ]
    status["placeholders"] = {"values": ["?"]}
    status["description"] = "{{ exceptions.raise_compiler_error(1337 * 1000 + 42) }}"
    invoices["contract"]["schema"].append({"name": ODD, "type": "string"})
    quoted = '"' + ODD.replace('"', '""') + '"'
    invoices["contract"]["invariants"] += [
        {"id": "hostile", "check": f"{quoted} <> 'it''s {{{{ y }}}} {{% endraw %}}'"},
        {
            "id": "late_share",
            "check": "issue_date <= due_date",
            "mustBeLessOrEqualTo": 25,
            "unit": "percent",
        },
        # Ids are contract text; dbt renders test arguments, even a bare env_var(...).
        {"id": "{{ exceptions.raise_compiler_error(1337 * 1000 + 42) }}", "check": "net >= 0"},
        {"id": "env_var('HOME')", "check": "net >= 0"},
    ]
    return contract


_MODELS = {
    "invoices": (
        "select * from (values "
        "('i1','c1',100.00::decimal(12,2),0.2000::decimal(5,4),120.00::decimal(12,2),"
        "10.00::decimal(12,2),date '2026-01-01',date '2026-02-01','A','fine'),"
        "('i2','c2',100.00,0.2000,121.00,0.00,date '2026-03-01',date '2026-02-01','?',"
        "'it''s {{ \"{{\" }} y }} {% raw %}{% endraw %}{{ \"{%\" }} endraw %}'),"
        "('i3',null,1.00,0.0000,1.00,null,null,date '2026-01-01','A','fine'),"
        "('i4','c9',1.00,0.0000,1.00,0.00,date '2026-01-01',date '2026-01-02','A','fine')) "
        "as t(invoice_id, contract_id, net, tax_rate, gross, billed_base_fee, issue_date, "
        'due_date, status, "odd ""col"" {{ \'{{\' }} x }}")'
    ),
    "invoice_lines": (
        "select * from (values ('l1','i1','base_fee',10.00::decimal(12,2)),"
        "('l2','i1','other',5.00),('l3','i2','other',3.00)) as t(line_id, invoice_id, kind, amount)"
    ),
    "contracts": (
        "select * from (values ('c1',10.00::decimal(12,2),'A'),('c2',5.00,'B'),('c2',5.00,'B')) "
        "as t(contract_id, monthly_fee, status)"
    ),
}


@pytest.fixture(scope="module")
def dbt_results(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    binary = _ensure_dbt()
    root = tmp_path_factory.mktemp("dbt_project")
    (root / "models").mkdir()
    (root / "dbt_project.yml").write_text(
        'name: rules\nversion: "1.0"\nprofile: rules\nconfig-version: 2\n'
    )
    (root / "profiles.yml").write_text(
        "rules:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {root / 'r.duckdb'}\n"
    )
    for name, sql in _MODELS.items():
        (root / "models" / f"{name}.sql").write_text(sql)
    assert _run_cli(root, root / "models" / "schema.yml", _project_contract()) == 0
    # One test whose adapter has no SQL, to pin the loud failure.
    schema = yaml.safe_load((root / "models" / "schema.yml").read_text())
    tests = schema["models"][0]["data_tests"]
    orphan = copy.deepcopy(next(t for t in tests if _rule(t) == "dep"))
    orphan["fluid_rule"].update(name="fluid_no_duckdb", rule=_dbt.jinja_text("no_duckdb"))
    orphan["fluid_rule"]["sql"].pop("duckdb")
    tests.append(orphan)
    (root / "models" / "schema.yml").write_text(yaml.safe_dump(schema, sort_keys=False))
    env = {
        **os.environ,
        "DBT_PROFILES_DIR": str(root),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    done = subprocess.run(
        [binary, "build", "--no-use-colors", "--project-dir", str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    results = json.loads((root / "target" / "run_results.json").read_text())
    by_name = {r["unique_id"].split(".")[2]: r for r in results["results"]}
    return {"by_name": by_name, "log": done.stdout + done.stderr}


@pytest.mark.parametrize(
    "test, status, failures",
    [
        ("fluid_gross_from_net", "fail", 1),  # i2 is off by a cent
        ("fluid_due_after_issue", "pass", 0),  # 2 of 4 rows, at most 50 %
        ("fluid_late_share", "fail", 1),  # 2 of 4 rows, above 25 %
        ("fluid_billed", "fail", 1),  # i3's NULL fee vs the summary of no children
        ("fluid_fee_is_monthly", "fail", 1),  # ambiguous, NULL and orphan links
        ("fluid_dep", "pass", 0),
        ("fluid_contract_status", "pass", 0),  # only i1's link reaches one row
        ("fluid_hostile", "fail", 1),  # i2 holds the literal; nothing ran as Jinja
    ],
)
def test_real_dbt_counts_what_the_compiler_counts(
    dbt_results: Dict[str, Any], test: str, status: str, failures: int
) -> None:
    result = dbt_results["by_name"][test]
    assert (result["status"], result["failures"]) == (status, failures), dbt_results["log"]


def test_real_dbt_exempts_a_placeholder_from_accepted_values(dbt_results: Dict[str, Any]) -> None:
    accepted = [
        n for n in dbt_results["by_name"] if n.startswith("accepted_values_invoices_status")
    ]
    assert accepted and dbt_results["by_name"][accepted[0]]["status"] == "pass"


def test_real_dbt_fails_loudly_for_an_adapter_with_no_sql(dbt_results: Dict[str, Any]) -> None:
    result = dbt_results["by_name"]["fluid_no_duckdb"]
    assert result["status"] == "error"
    assert "no_duckdb" in result["message"] and "duckdb adapter" in result["message"]


def test_one_column_rule_id_on_two_exposes_gives_two_tests() -> None:
    from test_compile import _keys_chain

    tests, skipped = _dbt.rule_tests(_keys_chain(), lambda e: e)
    assert skipped == []
    bodies = {model: entries[0]["fluid_rule"] for model, entries in tests.items()}
    assert set(bodies) == {"a1", "b2", "c3"}
    assert len({b["name"] for b in bodies.values()}) == 3
    for model, body in bodies.items():
        assert f'ref("{model}")' in body["sql"]["duckdb"]


def test_real_dbt_runs_no_contract_text_as_jinja(dbt_results: Dict[str, Any]) -> None:
    # Run as Jinja, the text would print 1337042; as data it stays 1337 * 1000 + 42.
    assert "1337042" not in dbt_results["log"]
    names = dbt_results["by_name"]
    assert names["fluid_exceptions_raise_compiler_error_1337_1000_42"]["status"] == "pass"
    assert names["fluid_env_var_HOME"]["status"] == "pass"


def test_a_dq_value_rule_on_a_placeholder_column_is_exempt_too() -> None:
    contract = _billing()
    invoices = contract["exposes"][0]
    status = next(c for c in invoices["contract"]["schema"] if c["name"] == "status")
    status["placeholders"] = {"values": ["?"]}
    invoices["contract"]["dq"] = {
        "rules": [{"id": "st", "type": "valid_values", "selector": "status", "values": ["A", "B"]}]
    }
    doc = yaml.safe_load(render_dbt_tests(contract))
    column = next(c for c in doc["models"][0]["columns"] if c["name"] == "status")
    accepted = [t for t in column["tests"] if isinstance(t, dict) and "accepted_values" in t]
    assert accepted and all("where" in t["accepted_values"]["config"] for t in accepted)


def test_the_engine_path_exempts_placeholders_as_the_exporter_does() -> None:
    from fluid_build.engines.dbt.schema_yml import _tests_for_column

    column = {
        "name": "status",
        "type": "string",
        "validationRules": [{"type": "enum", "constraint": "A, B"}],
        "placeholders": {"prefixes": ["TBD_"]},
    }
    tests = _tests_for_column(column, [])
    accepted = next(t for t in tests if isinstance(t, dict) and "accepted_values" in t)
    assert "SUBSTR" in accepted["accepted_values"]["config"]["where"]
    column.pop("placeholders")
    assert (
        "config"
        not in next(
            t
            for t in _tests_for_column(column, [])
            if isinstance(t, dict) and "accepted_values" in t
        )["accepted_values"]
    )


def test_a_placeholder_prefix_covers_text_with_a_line_break() -> None:
    from test_compile import _contract, _expose

    contract = _contract(
        _expose(
            "p",
            [
                {
                    "name": "code",
                    "type": "string",
                    "identifierFormat": [{"mask": "99"}],
                    "placeholders": {"prefixes": ["TBD"]},
                }
            ],
        )
    )
    from fluid_build.data_rules._compile import compile_rules

    rule = next(r for r in compile_rules(contract) if r.kind == "identifierFormat")
    db = duckdb.connect()
    db.execute("CREATE TABLE p (code VARCHAR)")
    db.execute("INSERT INTO p VALUES ('12'), ('TBD a' || chr(10) || 'b'), ('x')")
    assert db.execute(rule.counts(lambda e: f'"{e}"')).fetchone() == (3, 1)
