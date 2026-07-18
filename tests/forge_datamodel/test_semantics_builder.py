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
    infer_measure_agg,
    measure_from_aggregate_expression,
    normalized_time_type_params,
    parse_aggregate_expression,
    simple_metric,
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
