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

"""Shared semantics-builder pins + the double-aggregation regression.

The headline regression this file pins: OSI metric expressions are whole
aggregate calls (``SUM(amount)``) and the old translation copied them
verbatim into ``measures[].expr`` next to an inferred ``agg`` — so every
consumer aggregated twice (``SUM(SUM(amount))``, invalid SQL on every
engine, from every deterministic forge run). The end-to-end test drives
contract emission → MCP compilation → real execution on duckdb.
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.schemas.osi import (
    OSIDataset,
    OSIDimension,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.semantics_builder import (
    default_agg_time_dimension,
    first_aggregate_call,
    infer_measure_agg,
    measure_from_aggregate_expression,
    normalized_time_type_params,
    parse_aggregate_expression,
    simple_metric,
    validate_semantics_block,
)


class TestParseAggregateExpression:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("SUM(amount)", ("sum", "amount")),
            ("sum( amount )", ("sum", "amount")),
            ("AVG(latency_ms)", ("avg", "latency_ms")),
            ("MIN(created_at)", ("min", "created_at")),
            ("MAX(created_at)", ("max", "created_at")),
            ("MEDIAN(amount)", ("median", "amount")),
            ("COUNT(order_id)", ("count", "order_id")),
            ("COUNT(*)", ("count", "1")),
            ("COUNT(DISTINCT customer_id)", ("count_distinct", "customer_id")),
            ("count(distinct customer_id)", ("count_distinct", "customer_id")),
            # Aggregates over computed row expressions keep the inner expr.
            ("SUM(price * quantity)", ("sum", "price * quantity")),
        ],
    )
    def test_single_aggregates_split(self, expr, expected) -> None:
        assert parse_aggregate_expression(expr) == expected

    @pytest.mark.parametrize(
        "expr",
        [
            "SUM(a) / COUNT(b)",  # ratio — not a single aggregate
            "SUM(a) + SUM(b)",  # derived — not a single aggregate
            "amount",  # bare column
            "1 + 2",
            "",
        ],
    )
    def test_non_single_aggregates_return_none(self, expr) -> None:
        assert parse_aggregate_expression(expr) is None


class TestMeasureBuilding:
    def test_single_aggregate_is_split_never_double_wrapped(self) -> None:
        measure = measure_from_aggregate_expression("total_revenue", "SUM(amount)")
        assert measure == {"name": "total_revenue", "agg": "sum", "expr": "amount"}

    def test_count_distinct_classified_correctly(self) -> None:
        """Previously COUNT(DISTINCT x) misfiled as plain count."""
        assert infer_measure_agg("COUNT(DISTINCT customer_id)") == "count_distinct"
        measure = measure_from_aggregate_expression("customers", "COUNT(DISTINCT customer_id)")
        assert measure["agg"] == "count_distinct"
        assert measure["expr"] == "customer_id"

    def test_complex_expression_keeps_legacy_verbatim_shape(self) -> None:
        measure = measure_from_aggregate_expression("aov", "SUM(a) / COUNT(b)")
        assert measure == {"name": "aov", "agg": "sum", "expr": "SUM(a) / COUNT(b)"}

    def test_description_is_optional(self) -> None:
        assert "description" not in measure_from_aggregate_expression("m", "SUM(x)")
        assert (
            measure_from_aggregate_expression("m", "SUM(x)", description="d")["description"] == "d"
        )

    def test_simple_metric_shape(self) -> None:
        assert simple_metric("m_metric", "m", description="d") == {
            "name": "m_metric",
            "type": "simple",
            "measure": "m",
            "description": "d",
        }


class TestTimeHelpers:
    def test_normalized_time_type_params(self) -> None:
        assert normalized_time_type_params("daily") == {"timeGranularity": "day"}
        assert normalized_time_type_params("per month") == {"timeGranularity": "month"}
        assert normalized_time_type_params("fortnight") is None
        assert normalized_time_type_params(None) is None

    def test_default_agg_time_dimension_picks_first_time_dimension(self) -> None:
        dimensions = [
            {"name": "region", "type": "categorical"},
            {"name": "order_date", "type": "time"},
            {"name": "shipped_at", "type": "time"},
        ]
        assert default_agg_time_dimension(dimensions) == "order_date"
        assert default_agg_time_dimension([{"name": "region", "type": "categorical"}]) is None


def _make_logical() -> LogicalDraft:
    osi = OSISemanticModel(
        name="orders",
        description="Order analytics",
        datasets=[
            OSIDataset(
                name="orders",
                source="raw.orders",
                primary_key=["order_id"],
                fields=[
                    OSIField(name="order_id"),
                    OSIField(name="region"),
                    OSIField(
                        name="order_date",
                        expression=OSIExpression(
                            dialects=[
                                OSIExpressionDialect(dialect="ANSI_SQL", expression="order_date")
                            ]
                        ),
                        dimension=OSIDimension(is_time=True, grain="day"),
                    ),
                    OSIField(name="amount"),
                ],
            )
        ],
        metrics=[
            OSIMetric(
                name="total_revenue",
                description="Total revenue",
                expression=OSIExpression(
                    dialects=[OSIExpressionDialect(dialect="ANSI_SQL", expression="SUM(amount)")]
                ),
            ),
            OSIMetric(
                name="unique_regions",
                expression=OSIExpression(
                    dialects=[
                        OSIExpressionDialect(
                            dialect="ANSI_SQL", expression="COUNT(DISTINCT region)"
                        )
                    ]
                ),
            ),
        ],
    )
    return LogicalDraft(
        name="orders",
        technique="flat",
        osi=osi,
        conceptual=ConceptualDraft(name="orders"),
    )


class TestEmittedContractRegression:
    def test_measures_are_not_double_aggregated(self) -> None:
        contract = build_contract_from_logical(_make_logical())
        semantics = contract["exposes"][0]["semantics"]
        measures = {m["name"]: m for m in semantics["measures"]}
        assert measures["total_revenue"]["agg"] == "sum"
        assert measures["total_revenue"]["expr"] == "amount"  # NOT "SUM(amount)"
        assert measures["unique_regions"]["agg"] == "count_distinct"
        assert measures["unique_regions"]["expr"] == "region"

    def test_default_agg_time_dimension_is_populated(self) -> None:
        """The schema field was write-never; the shared builder populates
        it from the first time dimension so consumers stop guessing."""
        contract = build_contract_from_logical(_make_logical())
        semantics = contract["exposes"][0]["semantics"]
        assert semantics["defaultAggTimeDimension"] == "order_date"

    def test_forge_contract_queries_execute_on_a_real_engine(self) -> None:
        """End-to-end: forge-emitted semantics → MCP compiler → duckdb.
        Under the old translation this rendered SUM(SUM(amount)) and the
        engine rejected every query."""
        duckdb = pytest.importorskip("duckdb")
        from fluid_build.output_ports.mcp.query_compiler import compile_semantic_query

        contract = build_contract_from_logical(_make_logical())
        expose = contract["exposes"][0]
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE orders AS SELECT * FROM (VALUES "
            "('emea', 100, DATE '2026-01-01'), ('emea', 50, DATE '2026-01-02'), "
            "('apac', 70, DATE '2026-01-03')) t(region, amount, order_date)"
        )
        compiled = compile_semantic_query(
            expose=expose,
            metric="total_revenue",
            limit=10,
            table_reference="orders",
            dialect="duckdb",
        )
        assert "SUM(SUM(" not in compiled.sql
        (total,) = con.execute(compiled.render_sql_for_dialect("duckdb")).fetchone()
        assert total == 220
        compiled_distinct = compile_semantic_query(
            expose=expose,
            metric="unique_regions",
            limit=10,
            table_reference="orders",
            dialect="duckdb",
        )
        (regions,) = con.execute(compiled_distinct.render_sql_for_dialect("duckdb")).fetchone()
        assert regions == 2


class TestInterviewProducerAlignment:
    def test_interview_grain_normalizes_and_default_time_dimension_set(self) -> None:
        from fluid_build.cli.forge_copilot_contract_helpers import (
            _build_semantics_from_interview_summary,
        )

        semantics = _build_semantics_from_interview_summary(
            columns=[{"name": "order_id"}],
            interview_summary={
                "semantic_intent": {
                    "primary_entity": "order",
                    "primary_measures": ["revenue"],
                    "primary_dimensions": ["region"],
                    "time_dimension": "order_date",
                    "time_granularity": "daily",
                }
            },
            expose_name="orders",
            description="Orders",
        )
        time_dims = [d for d in semantics["dimensions"] if d.get("type") == "time"]
        assert time_dims[0]["typeParams"] == {"timeGranularity": "day"}
        assert semantics["defaultAggTimeDimension"] == "order_date"

    def test_interview_unknown_grain_is_omitted_not_emitted_invalid(self) -> None:
        from fluid_build.cli.forge_copilot_contract_helpers import (
            _build_semantics_from_interview_summary,
        )

        semantics = _build_semantics_from_interview_summary(
            columns=[{"name": "order_id"}],
            interview_summary={
                "semantic_intent": {
                    "time_dimension": "order_date",
                    "time_granularity": "fortnightly",
                }
            },
            expose_name="orders",
            description="Orders",
        )
        time_dims = [d for d in semantics["dimensions"] if d.get("type") == "time"]
        assert "typeParams" not in time_dims[0]


# ---------------------------------------------------------------------
# The validate-time guard for the SAME double-aggregation shape.
#
# #440 fixed the two EMITTERS; nothing detected the shape when a human or
# a third-party tool hand-authored it. ``fluid validate`` reported
# "✅ Valid" with zero warnings and ``fluid policy-check --strict`` scored
# 100/100 on a contract whose every governed query dies at the engine.
# ---------------------------------------------------------------------


def _contract_with_semantics(**semantics):
    return {
        "fluidVersion": "0.7.6",
        "exposes": [{"exposeId": "orders", "semantics": semantics}],
    }


@pytest.mark.parametrize(
    "expr,agg",
    [
        ("SUM(TOTAL_PRICE)", "sum"),
        ("COUNT(DISTINCT ORDER_ID)", "count"),
        ("count(*)", "count"),
        ("  Avg( amount )  ", "avg"),
        # Compound aggregate expression — parse_aggregate_expression
        # deliberately returns None for these (they belong in a derived
        # metric), but they double-wrap just the same.
        ("SUM(a) / COUNT(b)", "sum"),
        ("COALESCE(SUM(amount), 0)", "sum"),
    ],
)
def test_validate_semantics_block_rejects_double_aggregation(expr, agg):
    errors, warnings = validate_semantics_block(
        _contract_with_semantics(measures=[{"name": "m", "agg": agg, "expr": expr}])
    )
    assert len(errors) == 1
    assert "invalid SQL on every engine" in errors[0]
    assert warnings == []


@pytest.mark.parametrize(
    "expr",
    [
        "TOTAL_PRICE",
        "1",
        "amount * quantity",
        "COALESCE(amount, 0)",
        "YEAR(order_date)",
        # A column whose NAME merely contains an aggregate word must not
        # trip the scan — only a call (``name(``) counts.
        "count_of_items",
        "max_seen_at",
    ],
)
def test_validate_semantics_block_accepts_a_pre_aggregation_expr(expr):
    errors, warnings = validate_semantics_block(
        _contract_with_semantics(measures=[{"name": "m", "agg": "sum", "expr": expr}])
    )
    assert errors == []
    assert warnings == []


def test_validate_semantics_block_rejects_an_aggregate_dimension():
    errors, _ = validate_semantics_block(
        _contract_with_semantics(
            dimensions=[{"name": "d", "type": "categorical", "expr": "MAX(status)"}]
        )
    )
    assert len(errors) == 1
    assert "GROUP BY" in errors[0]


def test_validate_semantics_block_accepts_a_scalar_dimension_expr():
    errors, _ = validate_semantics_block(
        _contract_with_semantics(
            dimensions=[{"name": "order_year", "type": "time", "expr": "YEAR(ORDER_DATE)"}]
        )
    )
    assert errors == []


def test_validate_semantics_block_ignores_a_measure_with_no_agg():
    """Without a declared ``agg`` there is nothing to double-wrap with;
    the missing ``agg`` is the query compiler's error to report."""
    errors, _ = validate_semantics_block(
        _contract_with_semantics(measures=[{"name": "m", "expr": "SUM(amount)"}])
    )
    assert errors == []


def test_validate_semantics_block_is_inert_without_semantics():
    assert validate_semantics_block({"exposes": [{"exposeId": "x"}]}) == ([], [])
    assert validate_semantics_block({}) == ([], [])
    assert validate_semantics_block({"exposes": "not-a-list"}) == ([], [])


def test_emitter_output_passes_the_validate_time_guard():
    """The two sides of #440 agree: what the emitter produces is exactly
    what the validator accepts."""
    measure = measure_from_aggregate_expression("revenue", "SUM(amount)")
    errors, _ = validate_semantics_block(_contract_with_semantics(measures=[measure]))
    assert errors == []


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("SUM(amount)", "sum"),
        ("count( * )", "count"),
        ("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY x)", "percentile_cont"),
        ("LISTAGG(tag, ',')", "listagg"),
        ("amount * 2", None),
        ("count_of_items", None),
        ("", None),
        (None, None),
    ],
)
def test_first_aggregate_call_matches_calls_not_column_names(expr, expected):
    assert first_aggregate_call(expr) == expected


# ---------------------------------------------------------------------
# Name collisions across the ONE semantic namespace.
#
# dbt MetricFlow registers entities / dimensions / measures / metrics in
# a single global namespace and Cube requires ``name`` to be unique among
# all dimensions, measures and segments. We can't hard-error (contracts
# carrying the collision answer queries correctly — the governed query
# path renames the aggregate column), so it is a warning that names the
# ambiguity before someone meets it at query time.
# ---------------------------------------------------------------------


def _colliding(metric_measure="revenue"):
    return _contract_with_semantics(
        measures=[
            {"name": "revenue", "agg": "sum", "expr": "TOTAL_PRICE"},
            {"name": "order_status", "agg": "sum", "expr": "TOTAL_PRICE"},
        ],
        dimensions=[{"name": "order_status", "expr": "ORDER_STATUS"}],
        metrics=[{"name": "order_status", "type": "simple", "measure": metric_measure}],
    )


def test_metric_named_like_a_dimension_warns_and_names_the_fallback():
    errors, warnings = validate_semantics_block(_colliding())
    assert errors == []
    metric_warning = [w for w in warnings if "metric 'order_status'" in w]
    assert len(metric_warning) == 1
    assert "falls back to the measure name ('revenue')" in metric_warning[0]


def test_metric_whose_measure_also_collides_warns_that_the_query_is_rejected():
    errors, warnings = validate_semantics_block(_colliding(metric_measure="order_status"))
    assert errors == []
    metric_warning = [w for w in warnings if "metric 'order_status'" in w]
    assert len(metric_warning) == 1
    assert "REJECTS that request" in metric_warning[0]


def test_measure_named_like_a_dimension_warns():
    _, warnings = validate_semantics_block(_colliding())
    measure_warning = [w for w in warnings if "measure 'order_status'" in w]
    assert len(measure_warning) == 1
    assert "silently dropped" in measure_warning[0]


def test_collision_check_is_case_insensitive():
    _, warnings = validate_semantics_block(
        _contract_with_semantics(
            measures=[{"name": "revenue", "agg": "sum", "expr": "TOTAL_PRICE"}],
            dimensions=[{"name": "Order_Status", "expr": "ORDER_STATUS"}],
            metrics=[{"name": "order_status", "type": "simple", "measure": "revenue"}],
        )
    )
    assert len(warnings) == 1
    assert "Order_Status" in warnings[0]


def test_distinct_names_produce_no_collision_warning():
    """The ordinary contract shape must stay warning-free — this guard
    runs on every ``fluid validate`` and ``--strict`` turns warnings into
    a non-zero exit code."""
    errors, warnings = validate_semantics_block(
        _contract_with_semantics(
            measures=[{"name": "revenue", "agg": "sum", "expr": "TOTAL_PRICE"}],
            dimensions=[{"name": "order_status", "expr": "ORDER_STATUS"}],
            metrics=[{"name": "total_revenue", "type": "simple", "measure": "revenue"}],
        )
    )
    assert errors == []
    assert warnings == []
