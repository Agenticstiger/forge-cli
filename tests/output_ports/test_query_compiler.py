# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
    QueryValidationError,
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
    # Aliased to the METRIC name (``active_customers``), not the measure
    # it wraps, so a transcript / cached result set is self-describing.
    assert "COUNT(DISTINCT customer_id) AS active_customers" in compiled.sql
    assert "GROUP BY signup_date" in compiled.sql
    # A grouped result gets a deterministic top-N order; without it the
    # LIMIT clipped an arbitrary, non-reproducible slice of the groups.
    assert "ORDER BY active_customers DESC, signup_date ASC" in compiled.sql
    assert "LIMIT 10" in compiled.sql
    assert compiled.params == []
    assert compiled.columns == ["signup_date", "active_customers"]


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


# ---------------------------------------------------------------------
# Metric filters: contract-declared predicates must be applied, and
# unsafe ones must fail closed — never silently dropped (which returned
# semantically wrong, unfiltered numbers while the dbt MetricFlow export
# honoured the same filter).
# ---------------------------------------------------------------------


def _expose_with_filtered_metric(filter_sql: str):
    return make_expose(
        semantics={
            "name": "orders",
            "measures": [
                {"name": "revenue", "agg": "sum", "expr": "amount"},
            ],
            "dimensions": [
                {"name": "region", "type": "categorical"},
            ],
            "metrics": [
                {
                    "name": "completed_revenue",
                    "type": "simple",
                    "measure": "revenue",
                    "filter": filter_sql,
                },
            ],
        },
    )


def test_metric_filter_is_applied_as_where_predicate():
    compiled = compile_semantic_query(
        expose=_expose_with_filtered_metric("status = 'completed'"),
        metric="completed_revenue",
        limit=10,
        table_reference="orders",
    )
    assert "WHERE (status = 'completed')" in compiled.sql
    assert "SUM(amount) AS completed_revenue" in compiled.sql


def test_metric_filter_composes_with_dimension_filters_and_binding():
    """The metric predicate is inline (contract-declared, allowlisted);
    the caller's dimension filters stay bound parameters, and the
    parameter indexes are unaffected by the inline predicate."""
    compiled = compile_semantic_query(
        expose=_expose_with_filtered_metric("status = 'completed'"),
        metric="completed_revenue",
        dimensions=["region"],
        filters={"region": "emea"},
        limit=10,
        table_reference="orders",
    )
    assert "(status = 'completed')" in compiled.sql
    assert "region = :p_0" in compiled.sql
    assert compiled.params == ["emea"]


def test_direct_measure_query_is_unaffected_by_metric_filters():
    """Querying the bare measure bypasses the metric and therefore its
    filter — metric semantics attach to the metric name only."""
    compiled = compile_semantic_query(
        expose=_expose_with_filtered_metric("status = 'completed'"),
        measure="revenue",
        limit=10,
        table_reference="orders",
    )
    assert "status" not in compiled.sql


@pytest.mark.parametrize(
    "hostile",
    [
        "status = 'completed'; DROP TABLE orders",  # statement injection markers
        "status = 'completed' -- comment",  # comment marker
        "{{ Dimension('orders__status') }} = 'completed'",  # MetricFlow Jinja
        "1 = 1 UNION SELECT password FROM users",  # blocked keyword
    ],
)
def test_unsafe_metric_filter_fails_closed(hostile):
    """A filter that fails the safe-expression allowlist must raise —
    not degrade to unfiltered (wrong) results, and not echo the raw
    filter text back to the calling agent."""
    with pytest.raises(QueryValidationError) as excinfo:
        compile_semantic_query(
            expose=_expose_with_filtered_metric(hostile),
            metric="completed_revenue",
            limit=10,
            table_reference="orders",
        )
    assert "completed_revenue" in str(excinfo.value)
    assert "DROP" not in str(excinfo.value)
    assert "{{" not in str(excinfo.value)


# ---------------------------------------------------------------------
# Percentile measures: aggParams wiring + fail-closed dialects.
# ---------------------------------------------------------------------


def _expose_with_percentile(agg_params=None):
    measure = {"name": "p95_latency", "agg": "percentile", "expr": "latency_ms"}
    if agg_params is not None:
        measure["aggParams"] = agg_params
    return make_expose(
        semantics={
            "name": "requests",
            "measures": [measure],
            "dimensions": [],
            "metrics": [],
        },
    )


def test_percentile_defaults_to_median_continuous():
    compiled = compile_semantic_query(
        expose=_expose_with_percentile(),
        measure="p95_latency",
        limit=10,
        table_reference="requests",
    )
    assert "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency" in compiled.sql


def test_percentile_agg_params_value_is_rendered():
    compiled = compile_semantic_query(
        expose=_expose_with_percentile({"percentile": 0.95}),
        measure="p95_latency",
        limit=10,
        table_reference="requests",
    )
    assert "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)" in compiled.sql


def test_percentile_discrete_flag_selects_percentile_disc():
    compiled = compile_semantic_query(
        expose=_expose_with_percentile({"percentile": 0.9, "useDiscretePercentile": True}),
        measure="p95_latency",
        limit=10,
        table_reference="requests",
    )
    assert "PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY latency_ms)" in compiled.sql


@pytest.mark.parametrize("bad", [-0.1, 1.5, "high", True])
def test_percentile_out_of_range_rejected(bad):
    with pytest.raises(QueryValidationError, match="aggParams.percentile"):
        compile_semantic_query(
            expose=_expose_with_percentile({"percentile": bad}),
            measure="p95_latency",
            limit=10,
            table_reference="requests",
        )


@pytest.mark.parametrize("dialect", ["bigquery", "athena"])
def test_percentile_fails_closed_on_unsupported_dialects(dialect):
    """BigQuery / Athena have no grouped ordered-set percentile — the
    compiler must refuse rather than emit SQL the engine rejects."""
    with pytest.raises(QueryValidationError, match="percentile"):
        compile_semantic_query(
            expose=_expose_with_percentile(),
            measure="p95_latency",
            limit=10,
            table_reference="requests",
            dialect=dialect,
        )


def test_default_percentile_pinned_across_consumers():
    """The MCP compiler and the dbt MetricFlow bridge must agree on the
    default percentile — otherwise the same contract answers different
    numbers depending on the consumer."""
    from fluid_build.engines.dbt import semantic_models as dbt_bridge
    from fluid_build.output_ports.mcp import query_compiler

    assert query_compiler.DEFAULT_PERCENTILE == dbt_bridge.DEFAULT_PERCENTILE == 0.5


@pytest.mark.parametrize(
    "unbalanced",
    [
        "1 = 1) OR (1 = 1",  # paren break-out that would neutralize ANDed RLS
        "(status = 'completed'",  # unclosed
        "status = 'completed')",  # early close
    ],
)
def test_unbalanced_paren_metric_filter_fails_closed(unbalanced):
    """Defence-in-depth: an unbalanced filter passes the char allowlist
    but would escape its wrapping parens and, by AND/OR precedence,
    neutralize an ANDed policy rowFilter. Reject the whole class."""
    with pytest.raises(QueryValidationError, match="unbalanced"):
        compile_semantic_query(
            expose=_expose_with_filtered_metric(unbalanced),
            metric="completed_revenue",
            limit=10,
            table_reference="orders",
        )


def test_balanced_or_filter_stays_contained_next_to_row_filters():
    """A legitimate OR filter must stay inside its parens so the ANDed
    RLS clause still constrains every branch."""
    expose = _expose_with_filtered_metric("status = 'completed' OR status = 'shipped'")
    expose["policy"] = {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]}
    compiled = compile_semantic_query(
        expose=expose,
        metric="completed_revenue",
        limit=10,
        table_reference="orders",
        caller_attributes={"tenant_id": "t-1"},
    )
    assert "(status = 'completed' OR status = 'shipped') AND \"tenant_id\" = :p_0" in compiled.sql
    assert compiled.params == ["t-1"]


# ---------------------------------------------------------------------
# dimensions[].typeParams.timeGranularity (regression)
#
# The dbt export reads timeGranularity and emits
# `type_params: {time_granularity: day}` into the MetricFlow project, but the
# governed query path interpolated `expr` verbatim into SELECT and GROUP BY.
# Live on Snowflake over a TIMESTAMP_NTZ column with 20,000 rows / 2,405
# distinct days, "revenue by day" returned 20,000 groups of one order each:
#
#   before: SELECT ORDER_TS AS order_ts_day ... GROUP BY ORDER_TS
#           → 1993-11-10 20:25:19 | 4635.38      (a single order)
#   after:  SELECT DATE_TRUNC('day', ORDER_TS) ... GROUP BY DATE_TRUNC(...)
#           → 1996-06-13 00:00:00 | 1423603.62   (matches hand SQL exactly)
# ---------------------------------------------------------------------


def _expose_with_grain(granularity="day", dim_type="time", column_type="TIMESTAMP"):
    dimension = {"name": "order_ts_day", "type": dim_type, "expr": "order_ts"}
    if granularity is not None:
        dimension["typeParams"] = {"timeGranularity": granularity}
    return make_expose(
        columns=[
            {"name": "order_id", "type": "STRING"},
            {"name": "amount", "type": "FLOAT64"},
            {"name": "order_ts", "type": column_type},
        ],
        semantics={
            "name": "orders",
            "measures": [{"name": "revenue", "agg": "sum", "expr": "amount"}],
            "dimensions": [dimension],
            "metrics": [{"name": "total_revenue", "type": "simple", "measure": "revenue"}],
        },
    )


# Column-level policy on the GOVERNED semantic path
#
# ``project()`` drops restricted columns / redacts PII by matching the
# OUTPUT column name — which the semantic layer aliases away. These pin
# the compile-time enforcement that closes the alias bypass.
# ---------------------------------------------------------------------


def _expose_with_governed_columns():
    """An expose whose semantics reach a DENIED column (account_balance)
    and a PII column (email) through names that don't look like either."""
    return make_expose(
        columns=[
            {"name": "customer_id", "type": "STRING"},
            {"name": "email", "type": "STRING", "sensitivity": "pii"},
            {"name": "account_balance", "type": "FLOAT64"},
            {"name": "signup_date", "type": "DATE"},
        ],
        column_restrictions=[
            {"principal": "*", "columns": ["account_balance"], "access": "deny"},
        ],
        semantics={
            "name": "customer_profiles",
            "measures": [
                {"name": "customer_count", "agg": "count_distinct", "expr": "customer_id"},
                {"name": "avg_balance", "agg": "avg", "expr": "account_balance"},
                {"name": "max_email", "agg": "max", "expr": "email"},
                {"name": "email_count", "agg": "count_distinct", "expr": "email"},
            ],
            "dimensions": [
                {"name": "signup_date", "type": "time"},
                {"name": "contact", "type": "categorical", "expr": "email"},
                {"name": "balance_band", "type": "categorical", "expr": "account_balance"},
            ],
            "metrics": [
                {"name": "active_customers", "type": "simple", "measure": "customer_count"},
                {"name": "mean_balance", "type": "simple", "measure": "avg_balance"},
                {
                    "name": "rich_customers",
                    "type": "simple",
                    "measure": "customer_count",
                    "filter": "account_balance > 1000",
                },
            ],
        },
    )


def _grain_sql(**kwargs):
    dialect = kwargs.pop("dialect", "snowflake")
    return compile_semantic_query(
        expose=_expose_with_grain(**kwargs),
        metric="total_revenue",
        dimensions=["order_ts_day"],
        limit=5,
        table_reference="orders",
        dialect=dialect,
    ).sql


def test_declared_grain_truncates_select_and_group_by():
    sql = _grain_sql()
    assert "DATE_TRUNC('day', order_ts) AS order_ts_day" in sql
    assert "GROUP BY DATE_TRUNC('day', order_ts)" in sql


@pytest.mark.parametrize(
    "declared,expected",
    [("day", "day"), ("MONTH", "month"), ("daily", "day"), ("hr", "hour")],
)
def test_grain_aliases_resolve_through_the_shared_vocabulary(declared, expected):
    """Same normalisation the dbt export uses, so both surfaces agree."""
    assert f"DATE_TRUNC('{expected}', order_ts)" in _grain_sql(granularity=declared)


def test_unrecognised_grain_is_ignored_rather_than_interpolated():
    sql = _grain_sql(granularity="fortnight")
    assert "DATE_TRUNC" not in sql
    assert "order_ts AS order_ts_day" in sql


def test_no_grain_keeps_the_raw_expression():
    sql = _grain_sql(granularity=None)
    assert "DATE_TRUNC" not in sql


def test_categorical_dimension_is_never_truncated():
    sql = _grain_sql(dim_type="categorical")
    assert "DATE_TRUNC" not in sql


def test_filters_use_the_same_truncated_expression_as_the_projection():
    compiled = compile_semantic_query(
        expose=_expose_with_grain(),
        metric="total_revenue",
        dimensions=["order_ts_day"],
        filters={"order_ts_day": "1992-01-02"},
        limit=5,
        table_reference="orders",
        dialect="snowflake",
    )
    assert "WHERE DATE_TRUNC('day', order_ts) = :p_0" in compiled.sql


def test_bigquery_picks_the_typed_truncation_function():
    assert "TIMESTAMP_TRUNC(order_ts, DAY)" in _grain_sql(
        dialect="bigquery", column_type="TIMESTAMP"
    )
    assert "DATE_TRUNC(order_ts, DAY)" in _grain_sql(dialect="bigquery", column_type="DATE")


def test_bigquery_fails_closed_when_the_column_type_is_unknowable():
    """BigQuery's truncation function is typed; guessing it emits SQL the
    engine rejects. Same fail-closed posture as percentile on BigQuery."""
    with pytest.raises(QueryValidationError, match="BigQuery's truncation function"):
        _grain_sql(dialect="bigquery", column_type="STRING")


_RESTRICTED = ("account_balance",)
_REDACTED = ("email",)


def test_measure_over_restricted_column_is_rejected():
    """A measure whose ``expr`` reads a denied column served that
    column's statistics under an alias the name-matching drop never saw."""
    with pytest.raises(QueryValidationError, match="account_balance"):
        compile_semantic_query(
            expose=_expose_with_governed_columns(),
            measure="avg_balance",
            limit=10,
            table_reference="customer_profiles",
            restricted_columns=_RESTRICTED,
        )


def test_metric_over_restricted_measure_is_rejected():
    with pytest.raises(QueryValidationError, match="account_balance"):
        compile_semantic_query(
            expose=_expose_with_governed_columns(),
            metric="mean_balance",
            limit=10,
            table_reference="customer_profiles",
            restricted_columns=_RESTRICTED,
        )


def test_dimension_over_restricted_column_is_rejected():
    with pytest.raises(QueryValidationError, match="account_balance"):
        compile_semantic_query(
            expose=_expose_with_governed_columns(),
            metric="active_customers",
            dimensions=["balance_band"],
            limit=10,
            table_reference="customer_profiles",
            restricted_columns=_RESTRICTED,
        )


def test_filter_on_restricted_column_is_rejected():
    """An equality filter on a denied column is an inference oracle even
    though the column never appears in the projection."""
    with pytest.raises(QueryValidationError, match="account_balance"):
        compile_semantic_query(
            expose=_expose_with_governed_columns(),
            metric="active_customers",
            filters={"account_balance": 9999.99},
            limit=10,
            table_reference="customer_profiles",
            restricted_columns=_RESTRICTED,
        )


def test_metric_filter_over_restricted_column_is_rejected():
    """The contract's own ``metrics[].filter`` lands in the WHERE too."""
    with pytest.raises(QueryValidationError, match="account_balance"):
        compile_semantic_query(
            expose=_expose_with_governed_columns(),
            metric="rich_customers",
            limit=10,
            table_reference="customer_profiles",
            restricted_columns=_RESTRICTED,
        )


def test_unrestricted_query_is_unaffected_by_the_deny_scan():
    compiled = compile_semantic_query(
        expose=_expose_with_governed_columns(),
        metric="active_customers",
        dimensions=["signup_date"],
        limit=10,
        table_reference="customer_profiles",
        restricted_columns=_RESTRICTED,
        redacted_columns=_REDACTED,
    )
    assert compiled.columns == ["signup_date", "active_customers"]
    assert compiled.redacted_columns == ()


def test_pii_dimension_is_redacted_under_any_alias():
    """Redaction keys off the source EXPRESSION: a dimension named
    differently from its PII column used to return raw values."""
    compiled = compile_semantic_query(
        expose=_expose_with_governed_columns(),
        metric="active_customers",
        dimensions=["contact"],
        limit=10,
        table_reference="customer_profiles",
        restricted_columns=_RESTRICTED,
        redacted_columns=_REDACTED,
    )
    assert "email AS contact" in compiled.sql
    assert compiled.redacted_columns == ("contact",)


def test_value_revealing_aggregate_over_pii_is_redacted():
    """MAX(email) returns an actual address; COUNT(DISTINCT email) does
    not. Only the former is redacted — aggregate analysis over a PII
    column stays legitimate, which is the whole point of the
    visible-but-redacted layer."""
    revealing = compile_semantic_query(
        expose=_expose_with_governed_columns(),
        measure="max_email",
        limit=10,
        table_reference="customer_profiles",
        redacted_columns=_REDACTED,
    )
    assert revealing.redacted_columns == ("max_email",)
    summarising = compile_semantic_query(
        expose=_expose_with_governed_columns(),
        measure="email_count",
        limit=10,
        table_reference="customer_profiles",
        redacted_columns=_REDACTED,
    )
    assert summarising.redacted_columns == ()


def test_deny_scan_ignores_string_literals():
    """``WHERE label = 'account_balance'`` must not trip the identifier
    scan — same rule the free-form path already applies."""
    expose = _expose_with_governed_columns()
    expose["semantics"]["metrics"].append(
        {
            "name": "labelled",
            "type": "simple",
            "measure": "customer_count",
            "filter": "signup_date = 'account_balance'",
        }
    )
    compiled = compile_semantic_query(
        expose=expose,
        metric="labelled",
        limit=10,
        table_reference="customer_profiles",
        restricted_columns=_RESTRICTED,
    )
    assert "signup_date = 'account_balance'" in compiled.sql


# ---------------------------------------------------------------------
# Honest truncation + deterministic ordering on a grouped query
# ---------------------------------------------------------------------


def test_grouped_query_carries_a_row_cap_and_deterministic_order():
    compiled = compile_semantic_query(
        expose=_expose_with_semantics(),
        metric="active_customers",
        dimensions=["signup_date"],
        limit=50,
        table_reference="customer_profiles",
    )
    assert compiled.row_cap == 50
    assert "ORDER BY active_customers DESC, signup_date ASC" in compiled.sql


def test_ungrouped_aggregate_has_no_row_cap():
    """A bare aggregate is exactly one row whatever the LIMIT says, so it
    must never be reported as truncatable."""
    compiled = compile_semantic_query(
        expose=_expose_with_semantics(),
        measure="total_ltv_usd",
        limit=1,
        table_reference="customer_profiles",
    )
    assert compiled.row_cap is None
    assert "ORDER BY" not in compiled.sql


def test_free_form_sql_row_cap_tracks_the_effective_limit():
    appended = compile_free_form_sql(
        sql="SELECT customer_id FROM customer_profiles",
        table_reference="customer_profiles",
        limit=25,
    )
    assert appended.row_cap == 25
    caller_owned = compile_free_form_sql(
        sql="SELECT customer_id FROM customer_profiles LIMIT 7",
        table_reference="customer_profiles",
        limit=25,
    )
    assert caller_owned.row_cap == 7


def test_two_metrics_over_one_measure_project_distinct_columns():
    expose = _expose_with_filtered_metric("status = 'completed'")
    expose["semantics"]["metrics"].append(
        {"name": "total_revenue", "type": "simple", "measure": "revenue"}
    )
    completed = compile_semantic_query(
        expose=expose, metric="completed_revenue", limit=10, table_reference="orders"
    )
    total = compile_semantic_query(
        expose=expose, metric="total_revenue", limit=10, table_reference="orders"
    )
    assert completed.columns == ["completed_revenue"]
    assert total.columns == ["total_revenue"]


# ---------------------------------------------------------------------
# Projection-alias uniqueness
#
# Aliasing the aggregate to the METRIC name made two metrics over one
# measure distinguishable, but it can collide with a dimension name:
# metric / measure / dimension share ONE namespace in every mainstream
# semantic layer (dbt MetricFlow, Cube), and a duplicate output name
# both breaks ``ORDER BY <alias>`` and collapses in the drivers'
# ``dict(zip(columns, values))`` row keying.
# ---------------------------------------------------------------------


def _expose_with_colliding_names():
    """``order_status`` is BOTH a dimension and a metric; ``priority`` is
    both a dimension and a measure."""
    return make_expose(
        columns=[
            {"name": "order_id", "type": "STRING"},
            {"name": "order_status", "type": "STRING"},
            {"name": "order_priority", "type": "STRING"},
            {"name": "amount", "type": "FLOAT64"},
        ],
        semantics={
            "name": "orders",
            "measures": [
                {"name": "revenue", "agg": "sum", "expr": "amount"},
                {"name": "priority", "agg": "sum", "expr": "amount"},
            ],
            "dimensions": [
                {"name": "order_status", "type": "categorical"},
                {"name": "priority", "type": "categorical", "expr": "order_priority"},
                # Folds onto ``order_status`` but projects a DIFFERENT column.
                {"name": "Order_Status", "type": "categorical", "expr": "order_priority"},
            ],
            "metrics": [
                {"name": "order_status", "type": "simple", "measure": "revenue"},
                {"name": "total_revenue", "type": "simple", "measure": "revenue"},
                {"name": "Order_Status", "type": "simple", "measure": "revenue"},
                # Name AND measure name both collide with dimension ``priority``.
                {"name": "priority", "type": "simple", "measure": "priority"},
            ],
        },
    )


def test_metric_named_like_a_dimension_falls_back_to_the_measure_name():
    """The pre-aliasing behaviour for exactly this shape: the aggregate
    column is named after the MEASURE, and the query still runs."""
    compiled = compile_semantic_query(
        expose=_expose_with_colliding_names(),
        metric="order_status",
        dimensions=["order_status"],
        limit=10,
        table_reference="orders",
    )
    assert compiled.columns == ["order_status", "revenue"]
    assert "SUM(amount) AS revenue" in compiled.sql
    assert "ORDER BY revenue DESC, order_status ASC" in compiled.sql


def test_metric_alias_collision_is_case_insensitive():
    """Engines fold unquoted identifiers, so ``Order_Status`` and
    ``order_status`` are ONE output column name — the fallback has to
    trigger on the folded comparison or the SQL is still ambiguous."""
    compiled = compile_semantic_query(
        expose=_expose_with_colliding_names(),
        metric="Order_Status",
        dimensions=["order_status"],
        limit=10,
        table_reference="orders",
    )
    assert compiled.columns == ["order_status", "revenue"]


def test_metric_and_measure_both_colliding_is_rejected_loudly():
    """No distinct name left. Rejecting names the problem; the released
    behaviour silently dropped one of the two values from every row."""
    with pytest.raises(QueryValidationError, match="collides too"):
        compile_semantic_query(
            expose=_expose_with_colliding_names(),
            metric="priority",
            dimensions=["priority"],
            limit=10,
            table_reference="orders",
        )


def test_bare_measure_named_like_a_requested_dimension_is_rejected():
    with pytest.raises(QueryValidationError, match="collides"):
        compile_semantic_query(
            expose=_expose_with_colliding_names(),
            measure="priority",
            dimensions=["priority"],
            limit=10,
            table_reference="orders",
        )


def test_metric_name_survives_when_no_dimension_collides():
    """The collision guard must not disturb the ordinary case — the
    metric name is still what the aggregate column is called."""
    compiled = compile_semantic_query(
        expose=_expose_with_colliding_names(),
        metric="total_revenue",
        dimensions=["order_status"],
        limit=10,
        table_reference="orders",
    )
    assert compiled.columns == ["order_status", "total_revenue"]


def test_repeated_dimension_projects_once():
    """Asking for the same dimension twice is idempotent, not two
    identically-named columns (which collapse in the row dict and make
    ``ORDER BY <alias>`` ambiguous)."""
    compiled = compile_semantic_query(
        expose=_expose_with_colliding_names(),
        metric="total_revenue",
        dimensions=["order_status", "order_status"],
        limit=10,
        table_reference="orders",
    )
    assert compiled.columns == ["order_status", "total_revenue"]
    assert compiled.sql.count("AS order_status") == 1
    assert "GROUP BY order_status\n" in compiled.sql


def test_two_dimensions_with_one_output_name_are_rejected():
    """``Order_Status`` (expr ``order_status``) and ``order_status`` fold
    to one column name but carry different expressions, so one value
    would be silently dropped from every row."""
    with pytest.raises(QueryValidationError, match="silently dropped"):
        compile_semantic_query(
            expose=_expose_with_colliding_names(),
            metric="total_revenue",
            dimensions=["order_status", "Order_Status"],
            limit=10,
            table_reference="orders",
        )
