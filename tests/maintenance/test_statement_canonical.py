# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A plugin's statement ships as sqlglot rendered the statement it checked, never as the text it
was given: Spark and sqlglot read some text differently, and the bytes the runner executes must be
the ones whose target forge checked. The runner refuses a statement that does not start with its
verb and its own table, as forge renders it."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from fluid_build.maintenance import sql_literal
from fluid_build.maintenance.statements import canonical_statement, checked_statements

TABLE = "fluid.commerce.order_lines"
RUNNER_PATH = Path(__file__).resolve().parents[2] / "fluid_build/spark/runner/forge_spark_runner.py"

#: Spark continues a ``--`` comment over a backslash-newline; sqlglot ends it at the newline. Read
#: as given, Spark deletes from fluid.gold.secret while sqlglot sees the action's own table.
SPLIT_COMMENT = (
    "DELETE FROM -- \\\nfluid.commerce.order_lines /*\nfluid.gold.secret -- */\nWHERE true"
)
#: The same differential in the WHERE clause: Spark reads ``id = 1``, sqlglot ``id = 1 OR 1 = 1``,
#: which would have shipped and deleted every row (proven with Spark 3.5's own parser).
WIDENED_WHERE = f"DELETE FROM {TABLE} WHERE id = 1 -- x \\\nOR 1=1"
#: Escapes Spark and sqlglot read to different values: unicode and octal.
ESCAPES = [
    f"DELETE FROM {TABLE} WHERE sku = 'a\\u0041'",
    f"DELETE FROM {TABLE} WHERE sku = 'a\\U00000041'",
    f"DELETE FROM {TABLE} WHERE sku = '\\101'",
]


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("forge_spark_runner_canonical", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "sql",
    [
        SPLIT_COMMENT,
        WIDENED_WHERE,
        f"DELETE FROM {TABLE} WHERE qty = 1 -- why",
        f"DELETE FROM {TABLE} /* x",
        f"DELETE FROM {TABLE} WHERE note = '-- not a comment'",  # not even inside a string
    ],
)
def test_a_statement_with_a_comment_marker_is_refused(sql: str) -> None:
    """Spark and sqlglot end some comments in different places, so no comment ships at all."""
    assert canonical_statement(sql, TABLE)[1].startswith("holds '--' or '/*'")


@pytest.mark.parametrize("sql", ESCAPES + [f"DELETE FROM {TABLE} WHERE sku = 'a\\'b'"])
def test_a_statement_with_a_backslash_is_refused(sql: str) -> None:
    assert canonical_statement(sql, TABLE)[1].startswith("holds a backslash")


def test_what_ships_is_sqlglots_rendering() -> None:
    (shipped,) = checked_statements([f"delete   from {TABLE}\nwhere  qty=1"], TABLE, "w")
    assert shipped == f"DELETE FROM {TABLE} WHERE qty = 1"


def test_sql_literal_writes_a_plain_value_and_refuses_the_rest() -> None:
    assert sql_literal("gone") == "'gone'"
    (shipped,) = checked_statements(
        [f"DELETE FROM {TABLE} WHERE sku = {sql_literal('gone')}"], TABLE, "w"
    )
    assert shipped == f"DELETE FROM {TABLE} WHERE sku = 'gone'"
    for value in ["o'brien", "a\\b", "x -- y", "/* z", "${x}", "line\nbreak", "tab\there", 7]:
        with pytest.raises(ValueError) as caught:
            sql_literal(value)  # type: ignore[arg-type]
        assert str(value) not in str(caught.value) or value == 7


@pytest.mark.parametrize(
    "sql",
    [
        f"DELETE FROM {TABLE} WHERE customer_id IN (SELECT id FROM fluid.gdpr.requests)",
        f"UPDATE {TABLE} AS t SET t.email_hash = NULL WHERE t.customer_id = 'c1'",
        f"MERGE INTO {TABLE} AS t USING fluid.gdpr.requests AS r ON t.customer_id = r.id "
        + "WHEN MATCHED THEN DELETE",
    ],
)
def test_a_plain_statement_reads_back_as_itself(sql: str) -> None:
    shipped, problem = canonical_statement(sql, TABLE)
    assert problem == "" and shipped == canonical_statement(shipped, TABLE)[0]
    assert shipped.split(" ")[1:3] in (["FROM", TABLE], [TABLE, "AS"], ["INTO", TABLE])


@pytest.mark.parametrize(
    "sql",
    [
        # A dotted quoted part spells the three names but is one identifier to Spark.
        "DELETE FROM `fluid.commerce`.order_lines WHERE TRUE",
        "DELETE FROM `fluid.commerce.order_lines` WHERE TRUE",
        "DELETE FROM fluid.`commerce.order_lines` WHERE TRUE",
    ],
)
def test_a_dotted_quoted_name_is_not_the_tables(sql: str) -> None:
    assert canonical_statement(sql, TABLE)[1] == f"must write the action's own table, named {TABLE}"


@pytest.mark.parametrize(
    "sql",
    [
        f"DELETE FROM {TABLE} WHERE sku = 'a'b'",  # an option with a quote, pasted in
        f"DELETE FROM {TABLE} WHERE sku = 'x",
        f'DELETE FROM {TABLE} WHERE sku = "x',
        f"DELETE FROM {TABLE} WHERE sku = `x",
        f"DELETE FROM {TABLE} WHERE " + "(" * 5000 + "1" + ")" * 5000,
    ],
)
def test_text_sqlglot_cannot_tokenize_is_refused_not_raised(sql: str) -> None:
    with pytest.raises(ValueError) as caught:
        checked_statements([sql], TABLE, "w")
    assert "statement 0 is not SQL Spark reads" in str(caught.value)
    assert "sku" not in str(caught.value)  # the SQL is never quoted


def test_trailing_separators_never_reach_the_runner(runner) -> None:
    (shipped,) = checked_statements([f"DELETE FROM {TABLE} WHERE qty = 1; ;"], TABLE, "w")
    assert ";" not in shipped
    runner._check_statement({"name": "erase", "sql": shipped}, TABLE)


def _config(sql: str) -> str:
    return json.dumps(
        {
            "configVersion": 1,
            "product": "commerce.orders-stream",
            "build": "ingest-orders",
            "mode": "maintenance",
            "sink": {"catalog": "fluid", "type": "glue", "table": "commerce.order_lines"},
            "maintenance": {
                "expose": "order_lines",
                "steps": [{"step": "statement", "name": "erase", "sql": sql}],
            },
        }
    )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM fluid.gold.secret WHERE TRUE",
        "UPDATE fluid.gold.secret SET x = 1",
        "MERGE INTO fluid.gold.secret AS t USING fluid.commerce.order_lines AS s ON t.a = s.a "
        + "WHEN MATCHED THEN DELETE",
        f"DELETE  FROM {TABLE} WHERE TRUE",  # not as forge renders it
        # A quoted part that holds a dot, or a whole quoted dotted name, is another table.
        "DELETE FROM `fluid.commerce`.order_lines WHERE TRUE",
        "DELETE FROM `fluid.commerce.order_lines` WHERE TRUE",
        "DELETE FROM fluid.commerce.`order``_lines` WHERE TRUE",
    ],
)
def test_the_runner_refuses_a_statement_on_another_table(runner, sql: str) -> None:
    with pytest.raises(runner.JobConfigError, match="a statement writes the job's own table"):
        runner.parse_config(_config(sql))


@pytest.mark.parametrize("sql", [SPLIT_COMMENT, WIDENED_WHERE, *ESCAPES])
def test_the_runner_refuses_a_comment_or_a_backslash(runner, sql: str) -> None:
    with pytest.raises(runner.JobConfigError, match="no comment and no backslash"):
        runner.parse_config(_config(sql))


def test_the_runner_takes_what_forge_ships(runner) -> None:
    (shipped,) = checked_statements([f"DELETE FROM {TABLE} WHERE sku = 'gone'"], TABLE, "w")
    config = runner.parse_config(_config(shipped))
    assert config["maintenance"]["steps"][0]["sql"] == shipped
    for quoted in (
        "DELETE FROM fluid.commerce.`order_lines` WHERE TRUE",
        "DELETE FROM `fluid`.`commerce`.`order_lines` WHERE TRUE",
    ):
        runner.parse_config(_config(quoted))  # a name Spark needs quoted is still the table
