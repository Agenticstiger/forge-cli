# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.maintenance.statements``: a plugin action's SQL, held to one write on its table."""

from __future__ import annotations

import pytest

from fluid_build.maintenance.statements import checked_statements, statement_problem
from tests.maintenance.conftest import TABLE


class TestAccepted:
    @pytest.mark.parametrize(
        "sql",
        [
            f"DELETE FROM {TABLE} WHERE customer_id = 'c1'",
            f"delete from {TABLE} where customer_id in (select id from fluid.silver.requests)",
            f"DELETE FROM {TABLE.upper()} WHERE 1 = 1",
            f"DELETE FROM {TABLE} AS t WHERE t.customer_id = 'c1'",
            f"UPDATE {TABLE} SET email_hash = NULL WHERE customer_id = 'c1'",
            f"UPDATE {TABLE} AS t SET t.email_hash = NULL WHERE t.customer_id = 'c1'",
            f"UPDATE {TABLE} t SET email_hash = NULL",
            (
                f"MERGE INTO {TABLE} t USING fluid.silver.requests r ON t.customer_id = r.id "
                "WHEN MATCHED THEN DELETE"
            ),
            (
                f"MERGE INTO {TABLE} AS t USING (SELECT id FROM fluid.silver.requests) AS r "
                "ON t.customer_id = r.id WHEN MATCHED THEN UPDATE SET t.email_hash = NULL"
            ),
            f"DELETE FROM {TABLE} WHERE customer_id = 'c1';",
            f"DELETE FROM {TABLE}\nWHERE\tcustomer_id = 'c1'\r\n",
        ],
    )
    def test_own_table_as_target(self, sql):
        assert statement_problem(sql, TABLE) == ""

    def test_the_table_name_is_compared_without_case(self):
        assert statement_problem(f"DELETE FROM {TABLE}", TABLE.upper()) == ""

    def test_the_statement_may_read_other_tables(self):
        sql = f"DELETE FROM {TABLE} WHERE customer_id IN (SELECT id FROM other.db.requests)"
        assert statement_problem(sql, TABLE) == ""


class TestRefused:
    def test_another_table_as_target(self):
        for sql in (
            "DELETE FROM fluid.commerce.other WHERE 1 = 1",
            "UPDATE fluid.commerce.other SET a = 1",
            "MERGE INTO fluid.commerce.other t USING fluid.commerce.x s ON t.a = s.a "
            "WHEN MATCHED THEN DELETE",
            f"DELETE FROM {TABLE}_2",
            f"DELETE FROM other.{TABLE}",
        ):
            assert "must write the action's own table" in statement_problem(sql, TABLE)

    def test_the_problem_names_the_table_not_the_sql(self):
        problem = statement_problem("DELETE FROM fluid.commerce.other WHERE secretcol = 1", TABLE)
        assert TABLE in problem
        assert "secretcol" not in problem

    @pytest.mark.parametrize("sql", ["DELETE FROM order_lines", "DELETE FROM commerce.order_lines"])
    def test_unqualified_or_catalog_less_name(self, sql):
        # Spark reads an unqualified name in its current catalog, which may be another
        assert "must write the action's own table" in statement_problem(sql, TABLE)

    @pytest.mark.parametrize(
        "sql",
        [
            f"DROP TABLE {TABLE}",
            f"INSERT INTO {TABLE} VALUES (1)",
            f"INSERT OVERWRITE {TABLE} SELECT * FROM fluid.commerce.x",
            f"SELECT * FROM {TABLE}",
            f"CREATE TABLE {TABLE} (a int)",
            f"ALTER TABLE {TABLE} ADD COLUMN b int",
            f"TRUNCATE TABLE {TABLE}",
            f"CALL fluid.system.expire_snapshots('{TABLE}')",
        ],
    )
    def test_anything_but_delete_update_merge(self, sql):
        assert statement_problem(sql, TABLE) in (
            "must be one of DELETE, UPDATE, MERGE",
            "is not SQL Spark reads",
        )

    def test_drop_and_select_and_create_are_named_refusals(self):
        for sql in (
            f"DROP TABLE {TABLE}",
            f"SELECT 1 FROM {TABLE}",
            f"CREATE TABLE {TABLE} (a int)",
        ):
            assert statement_problem(sql, TABLE) == "must be one of DELETE, UPDATE, MERGE"

    def test_two_statements(self):
        sql = f"DELETE FROM {TABLE} WHERE a = 1; DELETE FROM {TABLE} WHERE a = 2"
        assert statement_problem(sql, TABLE) == "must be exactly one statement"
        sql = f"DELETE FROM {TABLE} WHERE a = 1; DROP TABLE {TABLE}"
        assert statement_problem(sql, TABLE) == "must be exactly one statement"

    def test_variable_syntax(self):
        sql = f"DELETE FROM {TABLE} WHERE a = '${{x}}'"
        assert statement_problem(sql, TABLE) == "holds a ${...} variable"
        assert statement_problem(f"DELETE FROM {TABLE} WHERE a = ${{x}}", TABLE) == (
            "holds a ${...} variable"
        )

    @pytest.mark.parametrize("char", ["\x00", "\x07", "\x1b", "\x7f", "\x0b", "\x0c"])
    def test_control_characters(self, char):
        sql = f"DELETE FROM {TABLE} WHERE a = 'x{char}y'"
        assert statement_problem(sql, TABLE) == "holds a control character"

    def test_length_limit(self):
        head = f"DELETE FROM {TABLE} WHERE a = '"
        ok = head + "x" * (20_000 - len(head) - 1) + "'"
        assert len(ok) == 20_000
        assert statement_problem(ok, TABLE) == ""
        assert statement_problem(ok + " ", TABLE) == "is longer than 20,000 characters"

    @pytest.mark.parametrize("value", [None, 5, "", "   \n", b"DELETE", ["DELETE"], {"a": 1}])
    def test_not_a_statement(self, value):
        assert statement_problem(value, TABLE) == "is not a SQL statement"

    def test_text_spark_does_not_read(self):
        assert statement_problem("DELETE FROM FROM WHERE (((", TABLE) == "is not SQL Spark reads"


class TestCheckedStatements:
    def test_returns_each_stripped_of_its_trailing_semicolon(self):
        out = checked_statements(
            [f"  DELETE FROM {TABLE} WHERE a = 1;  ", f"UPDATE {TABLE} SET a = 2;;"], TABLE, "w"
        )
        assert out == [f"DELETE FROM {TABLE} WHERE a = 1", f"UPDATE {TABLE} SET a = 2"]

    def test_accepts_a_tuple_and_refuses_an_empty_list(self):
        assert checked_statements((f"DELETE FROM {TABLE}",), TABLE, "w") == [f"DELETE FROM {TABLE}"]
        # An action that runs nothing would compile to a job config the runner refuses.
        with pytest.raises(ValueError, match="w: the action returned no statement"):
            checked_statements([], TABLE, "w")

    @pytest.mark.parametrize("value", [None, 5, {"a": 1}, {f"DELETE FROM {TABLE}"}])
    def test_refuses_a_non_list(self, value):
        with pytest.raises(ValueError, match="w: the action's statements are not a list"):
            checked_statements(value, TABLE, "w")

    def test_refuses_a_bare_string(self):
        with pytest.raises(ValueError, match="not a list of SQL statements"):
            checked_statements(f"DELETE FROM {TABLE}", TABLE, "w")

    def test_names_the_first_problem_by_position(self):
        with pytest.raises(ValueError) as info:
            checked_statements(
                [f"DELETE FROM {TABLE}", "DROP TABLE fluid.a.b", "SELECT 1"], TABLE, "actions[x]"
            )
        assert str(info.value) == "actions[x]: statement 1 must be one of DELETE, UPDATE, MERGE"

    def test_never_quotes_the_sql(self):
        marker = "marker_column_xyz"
        cases = [
            f"DELETE FROM fluid.commerce.other WHERE {marker} = 1",
            f"DROP TABLE {marker}",
            f"DELETE FROM {TABLE} WHERE {marker} = '${{v}}'",
            f"DELETE FROM {TABLE} WHERE {marker} = 1; DELETE FROM {TABLE}",
            f"DELETE FROM {TABLE} WHERE {marker} = '\x00'",
            f"DELETE FROM FROM {marker} (((",
            "SELECT " + marker + " FROM " + TABLE,
        ]
        for sql in cases:
            with pytest.raises(ValueError) as info:
                checked_statements([sql], TABLE, "w")
            assert marker not in str(info.value)
