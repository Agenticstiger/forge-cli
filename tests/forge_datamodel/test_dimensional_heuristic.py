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

"""Tests for the dimensional-heuristic classifier — UX audit H2.

Borrow-before-build receipts:

- Kimball Design Tip #95 — order line-items are the fact, not orders
  https://www.kimballgroup.com/2007/10/design-tip-95-patterns-to-avoid-when-modeling-headerline-item-transactions/
- dbt-dimensional-modelling tutorial (Kimball + dbt rules):
  fact tables have FKs + measures; dim tables have descriptive
  attrs + single PK; IDs are never measures.
  https://github.com/Data-Engineer-Camp/dbt-dimensional-modelling/blob/main/docs/part03-identify-fact-dimension.md
- Kimball calendar-date dimension — fact-table date columns should
  reference dim_date
  https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/calendar-date-dimension/

The pre-fix heuristic:
- picked the wider table as the fact (so ``orders`` → fact even though
  ``order_items`` is the Kimball line-item fact)
- listed ``id`` and ``customer_id`` as SUM measures
- never extracted ``dim_date`` from ``placed_at`` / ``created_at``
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.agents._modeler_helpers import (
    _build_dim_date,
    _build_referenced_by_counts,
    _classify_fact_or_dim,
    _extract_date_columns,
    _extract_measure_columns,
    _infer_foreign_keys,
    _is_date_type,
    _is_id_column,
    _is_numeric_type,
)
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.forge_datamodel.from_ddl.parser import (
    ColumnDefinition,
    TableDefinition,
)

# ── unit: column predicates ────────────────────────────────────────────


class TestColumnPredicates:
    @pytest.mark.parametrize(
        "name",
        [
            "id",
            "ID",
            "customer_id",
            "order_id",
            "product_sk",
            "vendor_fk",
            "uuid",
            "key",
        ],
    )
    def test_id_columns_detected(self, name):
        assert _is_id_column(name), f"{name!r} should be classified as an ID"

    @pytest.mark.parametrize(
        "name",
        [
            "revenue",
            "amount",
            "quantity",
            "unit_price",
            "name",
            "description",
        ],
    )
    def test_non_id_columns_not_misclassified(self, name):
        assert not _is_id_column(name), f"{name!r} should NOT be an ID"

    @pytest.mark.parametrize(
        "logical_type",
        ["INT", "INTEGER", "DECIMAL(18,2)", "NUMERIC", "FLOAT", "DOUBLE PRECISION"],
    )
    def test_numeric_types_detected(self, logical_type):
        assert _is_numeric_type(logical_type)

    @pytest.mark.parametrize(
        "logical_type",
        ["DATE", "TIMESTAMP", "TIMESTAMP_TZ", "DATETIME", "TIME"],
    )
    def test_date_types_detected(self, logical_type):
        assert _is_date_type(logical_type)


# ── unit: measure extraction (no SUM(id)) ──────────────────────────────


class TestMeasureExtraction:
    def test_id_columns_not_listed_as_measures(self):
        """The bug we're fixing: IDs were listed as measures."""
        table = TableDefinition(
            name="orders",
            columns=[
                ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                ColumnDefinition(name="customer_id", logical_type="INT"),
                ColumnDefinition(name="total_amount", logical_type="DECIMAL(18,2)"),
                ColumnDefinition(name="quantity", logical_type="INT"),
            ],
            primary_keys=["id"],
        )
        measures = _extract_measure_columns(table)
        measure_names = {col.name for col in measures}
        # Real measures present.
        assert "total_amount" in measure_names
        assert "quantity" in measure_names
        # IDs ABSENT (the bug).
        assert "id" not in measure_names, "id is a PK, never a measure"
        assert "customer_id" not in measure_names, "customer_id is an FK, never a measure"

    def test_descriptive_columns_not_measures(self):
        table = TableDefinition(
            name="products",
            columns=[
                ColumnDefinition(name="id", logical_type="INT"),
                ColumnDefinition(name="name", logical_type="STRING"),
                ColumnDefinition(name="price", logical_type="DECIMAL(10,2)"),
            ],
            primary_keys=["id"],
        )
        measures = _extract_measure_columns(table)
        names = {col.name for col in measures}
        assert "price" in names
        assert "name" not in names  # text → not numeric → not a measure
        assert "id" not in names


# ── unit: date extraction ──────────────────────────────────────────────


class TestDateExtraction:
    def test_timestamp_columns_extracted(self):
        table = TableDefinition(
            name="orders",
            columns=[
                ColumnDefinition(name="id", logical_type="INT"),
                ColumnDefinition(name="placed_at", logical_type="TIMESTAMP"),
                ColumnDefinition(name="created_at", logical_type="DATETIME"),
                ColumnDefinition(name="customer_id", logical_type="INT"),
            ],
        )
        date_cols = _extract_date_columns(table)
        names = {col.name for col in date_cols}
        assert names == {"placed_at", "created_at"}

    def test_no_date_columns_returns_empty(self):
        table = TableDefinition(
            name="products",
            columns=[
                ColumnDefinition(name="id", logical_type="INT"),
                ColumnDefinition(name="name", logical_type="STRING"),
            ],
        )
        assert _extract_date_columns(table) == []


# ── unit: FK inference ─────────────────────────────────────────────────


class TestForeignKeyInference:
    def test_id_columns_pointing_at_sibling_tables_are_fks(self):
        order_items = TableDefinition(
            name="order_items",
            columns=[
                ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                ColumnDefinition(name="order_id", logical_type="INT"),
                ColumnDefinition(name="product_id", logical_type="INT"),
                ColumnDefinition(name="qty", logical_type="INT"),
            ],
            primary_keys=["id"],
        )
        fks = _infer_foreign_keys(
            order_items, all_table_names=["orders", "products", "order_items"]
        )
        assert "order_id" in fks
        assert "product_id" in fks
        # PK is not an FK.
        assert "id" not in fks
        # Non-id column is not an FK.
        assert "qty" not in fks

    def test_pk_alone_is_not_a_fk(self):
        customers = TableDefinition(
            name="customers",
            columns=[
                ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                ColumnDefinition(name="name", logical_type="STRING"),
            ],
            primary_keys=["id"],
        )
        fks = _infer_foreign_keys(customers, all_table_names=["customers"])
        assert fks == []


# ── unit: referenced-by counts ─────────────────────────────────────────


class TestReferencedByCounts:
    def test_pointed_at_table_is_counted(self):
        tables = [
            TableDefinition(
                name="customers",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="orders",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="customer_id", logical_type="INT"),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="addresses",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="customer_id", logical_type="INT"),
                ],
                primary_keys=["id"],
            ),
        ]
        counts = _build_referenced_by_counts(tables)
        assert counts["customers"] == 2  # both orders + addresses point at customers
        assert counts["orders"] == 0
        assert counts["addresses"] == 0


# ── unit: fact/dim classifier ──────────────────────────────────────────


class TestFactDimClassifier:
    def _orders_world(self):
        # The canonical Kimball example: orders + order_items + customers.
        return [
            TableDefinition(
                name="customers",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="name", logical_type="STRING"),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="products",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="name", logical_type="STRING"),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="orders",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="customer_id", logical_type="INT"),
                    ColumnDefinition(name="placed_at", logical_type="TIMESTAMP"),
                    ColumnDefinition(name="status", logical_type="STRING"),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="order_items",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="order_id", logical_type="INT"),
                    ColumnDefinition(name="product_id", logical_type="INT"),
                    ColumnDefinition(name="quantity", logical_type="INT"),
                    ColumnDefinition(name="unit_price", logical_type="DECIMAL(10,2)"),
                ],
                primary_keys=["id"],
            ),
        ]

    def test_order_items_is_fact_not_dim(self):
        """The headline H2 bug: order_items was classified as dim."""
        tables = self._orders_world()
        all_names = [t.name for t in tables]
        ref_counts = _build_referenced_by_counts(tables)

        # order_items: has FKs (order_id, product_id), nothing references it.
        result = _classify_fact_or_dim(
            next(t for t in tables if t.name == "order_items"),
            all_names,
            ref_counts,
        )
        assert result == "fact", "order_items must be a fact (Kimball line item)"

    def test_customers_is_dim(self):
        tables = self._orders_world()
        all_names = [t.name for t in tables]
        ref_counts = _build_referenced_by_counts(tables)

        result = _classify_fact_or_dim(
            next(t for t in tables if t.name == "customers"),
            all_names,
            ref_counts,
        )
        assert result == "dim"

    def test_products_is_dim(self):
        tables = self._orders_world()
        all_names = [t.name for t in tables]
        ref_counts = _build_referenced_by_counts(tables)

        result = _classify_fact_or_dim(
            next(t for t in tables if t.name == "products"),
            all_names,
            ref_counts,
        )
        assert result == "dim"


# ── E2E: _dimensional_from_tables happy path ───────────────────────────


class TestDimensionalFromTablesE2E:
    """Pin the full output shape for the canonical orders example."""

    def _orders_world(self):
        return [
            TableDefinition(
                name="customers",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="name", logical_type="STRING"),
                    ColumnDefinition(name="email", logical_type="STRING"),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="products",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="name", logical_type="STRING"),
                    ColumnDefinition(name="category", logical_type="STRING"),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="orders",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="customer_id", logical_type="INT"),
                    ColumnDefinition(name="placed_at", logical_type="TIMESTAMP"),
                    ColumnDefinition(name="status", logical_type="STRING"),
                ],
                primary_keys=["id"],
            ),
            TableDefinition(
                name="order_items",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="order_id", logical_type="INT"),
                    ColumnDefinition(name="product_id", logical_type="INT"),
                    ColumnDefinition(name="quantity", logical_type="INT"),
                    ColumnDefinition(name="unit_price", logical_type="DECIMAL(10,2)"),
                ],
                primary_keys=["id"],
            ),
        ]

    def _build(self):
        agent = ModelerAgent()
        return agent._dimensional_from_tables(name="ecommerce", tables=self._orders_world())

    def test_order_items_emerges_as_fact(self):
        model = self._build()
        fact_names = {fact.name for fact in model.facts}
        # The canonical line-item fact must appear.
        assert "fact_order_items" in fact_names

    def test_no_dim_for_order_items(self):
        model = self._build()
        dim_names = {dim.name for dim in model.dimensions}
        # H2 BUG: order_items was being emitted as dim_order_items.
        assert (
            "dim_order_items" not in dim_names
        ), "order_items is a fact (line items), not a dimension"

    def test_dim_customers_and_dim_products_extracted(self):
        model = self._build()
        dim_names = {dim.name for dim in model.dimensions}
        assert "dim_customers" in dim_names
        assert "dim_products" in dim_names

    def test_dim_date_emitted_when_fact_has_timestamp(self):
        """Kimball calendar-date dimension extraction."""
        model = self._build()
        dim_names = {dim.name for dim in model.dimensions}
        assert "dim_date" in dim_names, (
            "fact tables with date/timestamp columns must reference a dim_date "
            "(Kimball calendar dimension)"
        )

    def test_no_id_columns_listed_as_measures(self):
        """Bug being fixed: ``SUM(id)`` / ``SUM(customer_id)`` is nonsense."""
        model = self._build()
        for fact in model.facts:
            measure_names = {m.name for m in fact.measures}
            # Common ID column names that previously leaked through.
            forbidden = {"id", "order_id", "customer_id", "product_id"}
            leaked = measure_names & forbidden
            assert leaked == set(), (
                f"ID columns leaked into measures on fact {fact.name}: {leaked}. "
                "IDs are never measures (Kimball + dbt-utils)."
            )

    def test_real_measures_appear_on_line_item_fact(self):
        model = self._build()
        order_items_fact = next((f for f in model.facts if f.name == "fact_order_items"), None)
        assert order_items_fact is not None
        measure_names = {m.name for m in order_items_fact.measures}
        assert "quantity" in measure_names
        assert "unit_price" in measure_names

    def test_fk_columns_listed_in_foreign_keys_not_measures(self):
        model = self._build()
        order_items_fact = next((f for f in model.facts if f.name == "fact_order_items"), None)
        assert order_items_fact is not None
        # FKs come from inferred FK detection.
        assert "order_id" in order_items_fact.foreign_keys
        assert "product_id" in order_items_fact.foreign_keys


# ── lone-table fallback (no FK relationships visible) ──────────────────


class TestLoneTableFallback:
    def test_single_table_becomes_fact(self):
        agent = ModelerAgent()
        tables = [
            TableDefinition(
                name="events",
                columns=[
                    ColumnDefinition(name="id", logical_type="INT", primary_key=True),
                    ColumnDefinition(name="amount", logical_type="DECIMAL(10,2)"),
                    ColumnDefinition(name="created_at", logical_type="TIMESTAMP"),
                ],
                primary_keys=["id"],
            ),
        ]
        model = agent._dimensional_from_tables(name="solo", tables=tables)
        assert len(model.facts) >= 1
        # No dimensions (no siblings to be dims) but dim_date is still
        # emitted because the fact carries a timestamp column.
        dim_names = {d.name for d in model.dimensions}
        assert dim_names == {"dim_date"} or dim_names == set()


# ── dim_date factory ───────────────────────────────────────────────────


class TestDimDateFactory:
    def test_dim_date_has_kimball_calendar_attributes(self):
        dim = _build_dim_date()
        attr_names = {a.name for a in dim.attributes}
        # Kimball's canonical date-dim columns.
        assert "year" in attr_names
        assert "quarter" in attr_names
        assert "month" in attr_names
        assert "day" in attr_names
        assert "day_of_week" in attr_names
        assert "is_weekend" in attr_names
        assert dim.name == "dim_date"
        # Surrogate key shape per Kimball calendar-date conventions.
        assert dim.surrogate_key in ("date_sk", "date_key")
