# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The check of a consumer query: what it refuses, what it accepts, and what it writes back."""

from __future__ import annotations

from typing import Mapping

import pytest

pytest.importorskip("sqlglot")

from fluid_build.profiler.consumer_queries import (  # noqa: E402
    Checked,
    Refused,
    Table,
    check_text,
)

TABLES: Mapping[str, Table] = {
    "readings": Table(
        columns=(
            ("id", "long"),
            ("station", "string"),
            ("read_on", "date"),
            ("level", "decimal(10,2)"),
            ("valid", "boolean"),
        ),
        unique=(("id",),),
    ),
    "stations": Table(
        columns=(("station", "string"), ("region", "string")),
        unique=(("station",),),
    ),
}


def refused(text: str, code: str, tables: Mapping[str, Table] = TABLES) -> Refused:
    result = check_text(text, kind="query", tables=tables)
    assert isinstance(result, Refused), f"expected a refusal of {text!r}"
    assert result.code in code.split("|"), (result.code, result.reason)
    assert result.kind == "query"
    return result


def accepted(text: str, kind: str = "query") -> Checked:
    result = check_text(text, kind=kind, tables=TABLES)
    assert isinstance(result, Checked), getattr(result, "reason", "")
    return result


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("SELECT 1 FROM readings; SELECT 2 FROM readings", "several_statements"),
        ("", "several_statements"),
        ("DROP TABLE readings", "not_a_query"),
        ("CREATE TABLE x AS SELECT * FROM readings", "not_a_query"),
        ("INSERT INTO readings VALUES (1, 'a', NULL, NULL, NULL)", "not_a_query"),
        ("UPDATE readings SET level = 1", "not_a_query"),
        ("DELETE FROM readings", "not_a_query"),
        ("COPY readings TO 'out.csv'", "not_a_query"),
        ("ATTACH 'other.db'", "not_a_query"),
        ("PRAGMA database_list", "not_a_query"),
        ("SET threads = 4", "not_a_query"),
        ("INSTALL httpfs", "not_a_query"),
        ("LOAD httpfs", "not_a_query"),
        ("EXPORT DATABASE 'dir'", "not_a_query|unparsable"),
        ("CALL pragma_version()", "not_a_query"),
        ("SELECT * FROM read_csv('/etc/hosts')", "table"),
        ("SELECT * FROM glob('/*')", "table"),
        ("SELECT * FROM range(10)", "table"),
        ("SELECT * FROM 'data.parquet'", "table"),
        ("SELECT * FROM 'https://example.com/x.csv'", "table"),
        ('SELECT * FROM "data.csv"', "table"),
        ("SELECT * FROM unknown_table", "table"),
        ("SELECT * FROM main.readings", "table"),
        ("SELECT * FROM other.main.readings", "table"),
        (
            "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT n FROM r",
            "construct",
        ),
        ("SELECT random() FROM readings", "function"),
        ("SELECT current_setting('threads') FROM readings", "function"),
        ("SELECT getenv('HOME') FROM readings", "function"),
        ("SELECT nextval('s') FROM readings", "function"),
        ("SELECT now() FROM readings", "function"),
        ("SELECT current_date FROM readings", "function"),
        ("SELECT read_text('/etc/hosts') FROM readings", "function"),
        ("SELECT * FROM readings WHERE id = $1", "placeholder"),
        ("SELECT * FROM readings WHERE id = ?", "placeholder"),
        ("SELECT nope FROM readings", "column"),
        ("SELECT * FROM readings r JOIN stations s ON station = station", "column"),
        ("SELECT id AS a, a + 1 AS b FROM readings", "alias"),
        ("SELECT id FROM readings LIMIT 3", "order"),
        ("SELECT id FROM readings ORDER BY level LIMIT 3", "order"),
        ("SELECT id FROM readings ORDER BY station OFFSET 3", "order"),
        ("SELECT row_number() OVER (ORDER BY level) FROM readings", "order"),
        ("SELECT row_number() OVER () FROM readings", "order"),
        ("SELECT * FROM readings QUALIFY row_number() OVER (ORDER BY id) = 1", "construct"),
        ("SELECT * FROM readings USING SAMPLE 10", "construct"),
        ("SELECT * FROM readings WHERE station LIKE '" + "a" * 600 + "'", "pattern"),
    ],
)
def test_refuses(text: str, code: str) -> None:
    refused(text, code)


def test_refuses_a_with_name_that_shadows_a_listed_table() -> None:
    result = refused("WITH readings AS (SELECT 1 AS x) SELECT x FROM readings", "ambiguous_table")
    assert "shadows" in result.reason


def test_refuses_a_table_that_differs_from_another_only_in_case() -> None:
    tables = {
        "Readings": TABLES["readings"],
        "readings": TABLES["readings"],
    }
    refused("SELECT id FROM readings", "ambiguous_table", tables)


def test_refuses_two_columns_that_duckdb_reads_as_one() -> None:
    tables = {"t": Table(columns=(("a", "long"), ("A", "long")))}
    refused("SELECT a FROM t", "schema", tables)


def test_refuses_a_table_without_columns() -> None:
    refused("SELECT 1 FROM t", "schema", {"t": Table(columns=())})


def test_refuses_over_the_token_limit() -> None:
    text = "SELECT " + ", ".join(["1"] * 10_001) + " FROM readings"
    assert "20000 tokens" in refused(text, "bounds").reason


def test_refuses_brackets_nested_past_the_limit() -> None:
    text = "SELECT " + "(" * 65 + "1" + ")" * 65 + " FROM readings"
    assert "brackets" in refused(text, "bounds").reason


def test_refuses_a_chain_that_nests_past_the_depth_limit() -> None:
    text = "SELECT " + " + ".join(["id"] * 200) + " FROM readings"
    assert "levels" in refused(text, "bounds").reason


def test_refuses_more_nodes_than_the_limit() -> None:
    text = "SELECT " + ", ".join(["id + 1"] * 3_400) + " FROM readings"
    assert "10000 expression nodes" in refused(text, "bounds").reason


def test_refuses_stars_that_write_out_past_the_limit() -> None:
    wide = {"wide": Table(columns=tuple((f"c{i}", "long") for i in range(400)))}
    text = "SELECT " + ", ".join(["*"] * 100) + " FROM wide"
    result = refused(text, "bounds", wide)
    assert "stars" in result.reason


def test_a_recursion_error_is_a_refusal_and_never_raised() -> None:
    result = check_text("SELECT " + "NOT " * 5_000 + "TRUE", kind="query", tables=TABLES)
    assert isinstance(result, Refused)
    assert result.code in ("recursion", "bounds", "unparsable")


def test_a_recursion_error_inside_sqlglot_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.profiler.consumer_queries import check

    def boom(*args: object, **kwargs: object) -> object:
        raise RecursionError

    monkeypatch.setattr(check, "_parse", boom)
    result = check_text("SELECT 1", kind="query", tables=TABLES)
    assert isinstance(result, Refused) and result.code == "recursion"


def test_an_error_nobody_named_is_a_refusal_with_its_class_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.profiler.consumer_queries import check

    def boom(*args: object, **kwargs: object) -> object:
        raise KeyError("a-secret-value")

    monkeypatch.setattr(check, "_parse", boom)
    result = check_text("SELECT 1", kind="query", tables=TABLES)
    assert isinstance(result, Refused) and result.code == "internal"
    assert "KeyError" in result.reason and "a-secret-value" not in result.reason


def test_a_reason_never_echoes_a_literal_of_the_text() -> None:
    for text in (
        "SELECT mystery_fn('sentinel-literal') FROM readings",
        "SELECT * FROM readings WHERE station = 'sentinel-literal' AND nope = 1",
        "SELECT * FROM 'sentinel-literal.csv'",
        "COPY readings TO 'sentinel-literal'",
        "SELECT 'sentinel-literal' FROM readings; SELECT 1",
        "SELECT 'sentinel-literal' +",
    ):
        result = check_text(text, kind="query", tables=TABLES)
        assert isinstance(result, Refused)
        assert "sentinel" not in result.reason


def test_a_name_in_a_reason_is_bounded_and_printable() -> None:
    long_name = "x" * 500
    result = refused(f"SELECT * FROM {long_name}", "table")
    assert long_name not in result.reason and len(result.reason) < 400


def test_kind_must_be_a_query_or_a_view() -> None:
    with pytest.raises(ValueError):
        check_text("SELECT 1", kind="other", tables=TABLES)


def test_the_kind_words_the_reason() -> None:
    result = check_text("DROP TABLE readings", kind="view", tables=TABLES)
    assert isinstance(result, Refused) and result.kind == "view"
    assert result.reason.startswith("the view")


# -- accepted -------------------------------------------------------------------------------


def test_accepts_a_filter_query() -> None:
    checked = accepted("select id from readings where level > 3.5 and valid")
    assert checked.kind == "query"
    assert checked.tables == ("readings",)
    assert checked.columns == (("readings", "id"), ("readings", "level"), ("readings", "valid"))
    assert checked.output_columns == ("id",)
    assert checked.text.startswith('SELECT "readings"."id" AS "id" FROM "readings"')
    assert checked.digest.startswith("sha256:")


def test_accepts_a_with_view() -> None:
    checked = accepted(
        "WITH recent AS (SELECT id, level FROM readings WHERE read_on >= DATE '2024-01-01') "
        "SELECT id FROM recent WHERE level > 1",
        kind="view",
    )
    assert checked.kind == "view"
    assert checked.tables == ("readings",)
    assert ("readings", "read_on") in checked.columns


def test_accepts_an_aggregate_view_with_group_by() -> None:
    checked = accepted(
        "SELECT station, count(*) AS n, avg(level) AS mean_level FROM readings "
        "GROUP BY station HAVING count(*) > 2",
        kind="view",
    )
    assert checked.output_columns == ("station", "n", "mean_level")
    assert checked.columns == (("readings", "level"), ("readings", "station"))


def test_accepts_a_join_and_reads_columns_of_both_tables() -> None:
    checked = accepted(
        "SELECT r.id FROM readings r JOIN stations s ON r.station = s.station "
        "WHERE s.region = 'north' AND r.valid"
    )
    assert checked.tables == ("readings", "stations")
    assert ("stations", "region") in checked.columns and ("readings", "station") in checked.columns


def test_accepts_the_functions_on_the_list() -> None:
    accepted(
        "SELECT lower(station), round(level, 1), extract(year FROM read_on), "
        "CAST(id AS VARCHAR), coalesce(level, 0), CASE WHEN valid THEN 1 ELSE 0 END, "
        "date_trunc('month', read_on) FROM readings"
    )


def test_names_are_read_without_regard_to_case_and_written_as_the_contract_has_them() -> None:
    checked = accepted("SELECT ID FROM READINGS WHERE STATION = 'a'")
    assert checked.tables == ("readings",)
    assert checked.columns == (("readings", "id"), ("readings", "station"))
    assert '"readings"' in checked.text and '"station"' in checked.text


def test_a_limit_over_a_declared_unique_key_is_a_total_order() -> None:
    accepted("SELECT id FROM readings ORDER BY id LIMIT 5")
    accepted("SELECT row_number() OVER (ORDER BY id) FROM readings")


def test_a_limit_over_every_column_is_a_total_order() -> None:
    accepted(
        "SELECT * FROM stations ORDER BY station, region LIMIT 3",
    )
    accepted("SELECT region FROM stations ORDER BY region LIMIT 3")  # the whole output row


def test_without_declared_keys_only_a_full_order_passes() -> None:
    tables = {"readings": Table(columns=TABLES["readings"].columns)}
    refused("SELECT id, station FROM readings ORDER BY id LIMIT 5", "order", tables)
    # the whole output row is in the order, so no tie can change which rows are kept
    ok = check_text("SELECT id FROM readings ORDER BY id LIMIT 5", kind="query", tables=tables)
    assert isinstance(ok, Checked)
    with_keys = check_text(
        "SELECT id, station FROM readings ORDER BY id LIMIT 5", kind="query", tables=TABLES
    )
    assert isinstance(with_keys, Checked)


def test_a_count_star_stays_a_star() -> None:
    checked = accepted("SELECT count(*) FROM readings")
    assert "COUNT(*)" in checked.text
    assert checked.columns == ()


@pytest.mark.parametrize(
    "text",
    [
        "SELECT id FROM readings WHERE level > 3.5 AND valid",
        (
            "WITH w AS (SELECT station, count(*) AS n FROM readings GROUP BY station) "
            "SELECT station FROM w WHERE n > 1"
        ),
        "SELECT r.id FROM readings r JOIN stations s ON r.station = s.station",
        "SELECT region, count(*) FROM stations GROUP BY region ORDER BY region",
        "SELECT id FROM readings UNION SELECT id FROM readings",
        "SELECT * FROM stations",
    ],
)
def test_the_canonical_text_round_trips(text: str) -> None:
    first = accepted(text)
    second = accepted(first.text)
    assert second.text == first.text
    assert second.digest == first.digest
    assert second.tables == first.tables and second.columns == first.columns


def test_the_returned_tree_is_the_tree_of_the_canonical_text() -> None:
    import sqlglot

    checked = accepted("SELECT id, station FROM readings WHERE valid")
    again = sqlglot.parse_one(checked.text, read="duckdb")
    assert checked.tree == again
    assert checked.tree.sql(dialect="duckdb", comments=False) == checked.text


def test_a_comment_never_carries_a_second_statement() -> None:
    checked = accepted("SELECT id /* ; DROP TABLE readings */ FROM readings -- ; SELECT 2")
    assert ";" not in checked.text and "DROP" not in checked.text


def test_unknown_contract_types_read_as_unknown_not_as_a_failure() -> None:
    tables = {"t": Table(columns=(("a", "geometry"), ("b", "long")))}
    result = check_text("SELECT b FROM t WHERE a IS NOT NULL", kind="query", tables=tables)
    assert isinstance(result, Checked)
