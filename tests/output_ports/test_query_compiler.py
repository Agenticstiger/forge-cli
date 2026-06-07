# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Pin the query compiler's safety guarantees.

The compiler is the only path from a tool-call payload to executed
SQL, so these tests exercise every documented guardrail:

* Identifiers must be allowlist-validated.
* Filter values are bound as parameters (never inline literals).
* Free-form SQL is rejected unless it starts with SELECT.
* The reserved-word allowlist blocks DROP / DELETE / etc.
"""

from __future__ import annotations

import pytest

from fluid_build.output_ports.mcp.query_compiler import (
    CompiledQuery,
    compile_free_form_sql,
    compile_row_filter_clauses,
    compile_semantic_query,
)

from ._fixtures import make_expose


def _expose_with_semantics():
    return make_expose(
        semantics={
            "name": "customer_profiles",
            "measures": [
                {"name": "customer_count", "agg": "count_distinct", "expr": "customer_id"},
                {"name": "total_ltv_usd", "agg": "sum", "expr": "lifetime_value_usd"},
            ],
            "dimensions": [
                {"name": "signup_date", "type": "time"},
            ],
            "metrics": [
                {"name": "active_customers", "type": "simple", "measure": "customer_count"},
            ],
        },
    )


# ---------------------------------------------------------------------
# Happy path: semantic query
# ---------------------------------------------------------------------


def test_semantic_query_with_metric_and_dimension():
    compiled = compile_semantic_query(
        expose=_expose_with_semantics(),
        metric="active_customers",
        dimensions=["signup_date"],
        limit=10,
        table_reference="customer_profiles",
    )
    assert "COUNT(DISTINCT customer_id) AS customer_count" in compiled.sql
    assert "GROUP BY signup_date" in compiled.sql
    assert "LIMIT 10" in compiled.sql
    assert compiled.params == []
    assert compiled.columns == ["signup_date", "customer_count"]


def test_semantic_query_with_filter_uses_parameter_binding():
    compiled = compile_semantic_query(
        expose=_expose_with_semantics(),
        measure="total_ltv_usd",
        dimensions=[],
        filters={"signup_date": "2024-02-10"},
        limit=5,
        table_reference="customer_profiles",
    )
    assert "WHERE signup_date = :p_0" in compiled.sql
    assert compiled.params == ["2024-02-10"]


def test_semantic_query_dialect_rendering_bigquery():
    compiled = compile_semantic_query(
        expose=_expose_with_semantics(),
        measure="customer_count",
        dimensions=[],
        filters={"signup_date": "2024-02-10"},
        limit=1,
        table_reference="customer_profiles",
    )
    rendered = compiled.render_sql_for_dialect("bigquery")
    assert "@p_0" in rendered
    assert ":p_0" not in rendered


def test_semantic_query_dialect_rendering_snowflake():
    compiled = compile_semantic_query(
        expose=_expose_with_semantics(),
        measure="customer_count",
        dimensions=[],
        filters={"signup_date": "2024-02-10"},
        limit=1,
        table_reference="customer_profiles",
    )
    rendered = compiled.render_sql_for_dialect("snowflake")
    assert "%(p_0)s" in rendered


# ---------------------------------------------------------------------
# Placeholder rewrite: substring-collision (:p_1 vs :p_10) at >= 11 params
# ---------------------------------------------------------------------


def _twelve_param_query() -> CompiledQuery:
    """A CompiledQuery whose SQL references :p_0 … :p_11 (12 params) so the
    rewrite must distinguish :p_1 from :p_10 / :p_11."""
    placeholders = ", ".join(f":p_{i}" for i in range(12))
    sql = f"SELECT * FROM t WHERE x IN ({placeholders})"
    return CompiledQuery(sql=sql, params=list(range(12)), columns=[])


def test_render_snowflake_no_substring_collision_at_twelve_params():
    # A naive per-index str.replace loop turns :p_10 into %(p_1)s0 because
    # :p_1 is a prefix of :p_10. The word-boundary regex must keep them
    # distinct.
    rendered = _twelve_param_query().render_sql_for_dialect("snowflake")
    assert "%(p_10)s" in rendered
    assert "%(p_11)s" in rendered
    assert "%(p_1)s0" not in rendered  # the corruption the bug produced
    # Every one of the 12 placeholders is rewritten exactly once.
    for i in range(12):
        assert f"%(p_{i})s" in rendered
    assert ":p_" not in rendered


def test_render_duckdb_no_substring_collision_at_twelve_params():
    rendered = _twelve_param_query().render_sql_for_dialect("duckdb")
    assert "$p_10" in rendered
    assert "$p_11" in rendered
    assert "$p_1 0" not in rendered  # i.e. :p_10 was not mangled
    for i in range(12):
        assert f"$p_{i}" in rendered
    assert ":p_" not in rendered


def test_render_bigquery_no_substring_collision_at_twelve_params():
    # BigQuery keeps the bare-prefix replace (1:1 :p_ -> @p_), which is
    # collision-safe; pin it so the behaviour is covered alongside the others.
    rendered = _twelve_param_query().render_sql_for_dialect("bigquery")
    assert "@p_10" in rendered
    assert "@p_11" in rendered
    for i in range(12):
        assert f"@p_{i}" in rendered
    assert ":p_" not in rendered


# ---------------------------------------------------------------------
# Row-filter identifier quoting is dialect-aware (BigQuery needs backticks)
# ---------------------------------------------------------------------

_RF_EXPOSE = {
    "policy": {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]},
}


def test_row_filter_quoting_bigquery_uses_backticks():
    # BigQuery reads ANSI "tenant_id" as a STRING LITERAL → predicate always
    # false → zero rows. It MUST be backtick-quoted instead.
    clauses, params = compile_row_filter_clauses(
        _RF_EXPOSE, {"tenant_id": "acme"}, dialect="bigquery"
    )
    assert clauses == ["`tenant_id` = :p_0"]
    assert params == ["acme"]


@pytest.mark.parametrize("dialect", [None, "duckdb", "snowflake", "postgres", "athena"])
def test_row_filter_quoting_non_bigquery_uses_ansi_double_quotes(dialect):
    clauses, params = compile_row_filter_clauses(_RF_EXPOSE, {"tenant_id": "acme"}, dialect=dialect)
    assert clauses == ['"tenant_id" = :p_0']
    assert params == ["acme"]


def test_semantic_query_threads_dialect_into_rowfilter_quoting():
    # End-to-end: compile_semantic_query passes dialect through to the merged
    # rowFilter so a BigQuery query gets backticked tenant_id.
    expose = make_expose(
        semantics={"name": "p", "measures": [{"name": "n", "agg": "count", "expr": "id"}]},
    )
    expose["policy"] = {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]}
    compiled = compile_semantic_query(
        expose=expose,
        measure="n",
        limit=10,
        caller_attributes={"tenant_id": "acme"},
        table_reference="`proj.ds.t`",
        dialect="bigquery",
    )
    assert "`tenant_id` = :p_0" in compiled.sql
    assert compiled.params == ["acme"]


# ---------------------------------------------------------------------
# Validation failures: identifier safety
# ---------------------------------------------------------------------


def test_unknown_metric_rejected():
    with pytest.raises(ValueError, match="Unknown metric"):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="not_a_metric",
            dimensions=[],
            limit=1,
            table_reference="customer_profiles",
        )


def test_unknown_measure_rejected():
    with pytest.raises(ValueError, match="Unknown measure"):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            measure="bogus",
            dimensions=[],
            limit=1,
            table_reference="customer_profiles",
        )


def test_metric_and_measure_both_rejected():
    with pytest.raises(ValueError, match="Exactly one of"):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="active_customers",
            measure="customer_count",
            dimensions=[],
            limit=1,
            table_reference="customer_profiles",
        )


def test_unknown_dimension_rejected():
    with pytest.raises(ValueError, match="Unknown dimension"):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="active_customers",
            dimensions=["unknown_axis"],
            limit=1,
            table_reference="customer_profiles",
        )


def test_unknown_filter_key_rejected():
    with pytest.raises(ValueError, match="not a known dimension"):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="active_customers",
            dimensions=[],
            filters={"unknown_key": 1},
            limit=1,
            table_reference="customer_profiles",
        )


def test_invalid_table_identifier_rejected():
    with pytest.raises(ValueError):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="active_customers",
            dimensions=[],
            limit=1,
            # Semicolon is in the blocked-token set
            table_reference="customer_profiles; DROP TABLE x",
        )


def test_limit_outside_range_rejected():
    with pytest.raises(ValueError):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="active_customers",
            dimensions=[],
            limit=0,
            table_reference="customer_profiles",
        )
    with pytest.raises(ValueError):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="active_customers",
            dimensions=[],
            limit=10_000_000,
            table_reference="customer_profiles",
        )


def test_filter_value_must_be_scalar():
    with pytest.raises(ValueError, match="must be a scalar"):
        compile_semantic_query(
            expose=_expose_with_semantics(),
            metric="active_customers",
            dimensions=[],
            filters={"signup_date": ["one", "two"]},
            limit=1,
            table_reference="customer_profiles",
        )


# ---------------------------------------------------------------------
# Free-form SQL gate
# ---------------------------------------------------------------------


def test_free_form_sql_appends_limit():
    compiled = compile_free_form_sql(
        sql="SELECT customer_id FROM customer_profiles WHERE customer_id = 'C0001'",
        table_reference="customer_profiles",
        limit=20,
    )
    assert compiled.sql.endswith("LIMIT 20")


def test_free_form_sql_blocks_drop():
    with pytest.raises(ValueError):
        compile_free_form_sql(
            sql="SELECT * FROM x; DROP TABLE x",
            table_reference="customer_profiles",
            limit=20,
        )


def test_free_form_sql_blocks_non_select():
    with pytest.raises(ValueError, match="Only SELECT"):
        compile_free_form_sql(
            sql="DELETE FROM customer_profiles",
            table_reference="customer_profiles",
            limit=20,
        )


def test_free_form_sql_blocks_double_dash_comment():
    with pytest.raises(ValueError):
        compile_free_form_sql(
            sql="SELECT * FROM x -- DROP TABLE x",
            table_reference="customer_profiles",
            limit=20,
        )


def test_free_form_sql_blocks_block_comment():
    with pytest.raises(ValueError):
        compile_free_form_sql(
            sql="SELECT /* DROP TABLE */ * FROM x",
            table_reference="customer_profiles",
            limit=20,
        )


def test_free_form_sql_blocks_update_token():
    with pytest.raises(ValueError):
        compile_free_form_sql(
            sql="SELECT * FROM x WHERE 1=1; UPDATE x SET y = 1",
            table_reference="customer_profiles",
            limit=20,
        )


def test_free_form_sql_blocks_alter_token():
    with pytest.raises(ValueError):
        compile_free_form_sql(
            sql="SELECT * FROM x; ALTER TABLE x ADD COLUMN y INT",
            table_reference="customer_profiles",
            limit=20,
        )


# ---------------------------------------------------------------------
# Security regressions — the bypasses identified by the security
# review. Each test pins exactly one bypass so a future refactor that
# weakens the check fails loud.
# ---------------------------------------------------------------------


def test_free_form_sql_blocks_tab_separated_union_select():
    """Vuln-1 regression: the previous implementation only checked
    for ASCII space when extracting the SELECT body, which let
    tab-separated tokens skip the reserved-word allowlist."""
    with pytest.raises(ValueError):
        compile_free_form_sql(
            sql="SELECT\tcustomer_id\tFROM\tcustomer_profiles\tUNION\tALL\tSELECT\tsecret\tFROM\tprivate.credentials",
            table_reference="customer_profiles",
            limit=10,
        )


def test_free_form_sql_blocks_newline_separated_union_select():
    """Vuln-1 regression: same bypass class — newline rather than
    space — must also be rejected."""
    with pytest.raises(ValueError):
        compile_free_form_sql(
            sql="SELECT\ncustomer_id\nFROM\ncustomer_profiles\nUNION\nSELECT\nsecret\nFROM\nx",
            table_reference="customer_profiles",
            limit=10,
        )


def test_free_form_sql_rejects_select_with_no_body():
    """A bare SELECT keyword must be rejected (the previous code
    silently treated it as 'no validation needed' which is what
    enabled the whitespace bypass)."""
    with pytest.raises(ValueError, match="SELECT body"):
        compile_free_form_sql(
            sql="SELECT",
            table_reference="customer_profiles",
            limit=10,
        )


def test_free_form_sql_blocks_aliased_restricted_column():
    """Vuln-2 regression: ``SELECT email AS not_email`` would
    previously pass column masking because the result-set column
    name (``not_email``) doesn't match the restricted set
    (``{email}``). The compiler now rejects any reference to a
    restricted column regardless of alias."""
    with pytest.raises(ValueError, match="restricted"):
        compile_free_form_sql(
            sql="SELECT email AS not_email, customer_id FROM customer_profiles",
            table_reference="customer_profiles",
            limit=10,
            restricted_columns=("email",),
        )


def test_free_form_sql_blocks_restricted_column_in_where_clause():
    """Even if the projection doesn't expose the restricted column
    by alias, a ``WHERE email = …`` clause leaks information by
    side-channel (existence-based timing or cardinality)."""
    with pytest.raises(ValueError, match="restricted"):
        compile_free_form_sql(
            sql="SELECT customer_id FROM customer_profiles WHERE email IS NOT NULL",
            table_reference="customer_profiles",
            limit=10,
            restricted_columns=("email",),
        )


def test_free_form_sql_allows_string_literal_matching_restricted_name():
    """The restricted-column scan strips quoted string literals
    before identifier matching so ``WHERE label = 'email'`` is not
    a false positive when ``email`` is a restricted column name."""
    compiled = compile_free_form_sql(
        sql="SELECT customer_id FROM customer_profiles WHERE label = 'email'",
        table_reference="customer_profiles",
        limit=10,
        restricted_columns=("email",),
    )
    assert "LIMIT 10" in compiled.sql


def test_free_form_sql_restricted_column_check_is_case_insensitive():
    """Identifier matching must be case-insensitive so
    ``EMAIL``-cased references are still rejected when the
    contract restricted ``email``."""
    with pytest.raises(ValueError, match="restricted"):
        compile_free_form_sql(
            sql="SELECT EMAIL AS x FROM customer_profiles",
            table_reference="customer_profiles",
            limit=10,
            restricted_columns=("email",),
        )


def test_free_form_sql_with_no_restricted_columns_is_unaffected():
    """When the contract has no column restrictions, the new check
    must not constrain queries — preserves the existing
    --allow-sql semantics for unrestricted exposes."""
    compiled = compile_free_form_sql(
        sql="SELECT customer_id, email FROM customer_profiles",
        table_reference="customer_profiles",
        limit=5,
        restricted_columns=(),
    )
    assert "LIMIT 5" in compiled.sql
