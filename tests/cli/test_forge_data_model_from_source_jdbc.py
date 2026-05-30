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

"""Pin the JDBC ``--source <jdbc>`` path of ``fluid forge data-model
from-source``.

Phase 2.6 of the world-class plan adds postgres / mysql / sqlite as
sources alongside the existing 7 catalogs. The implementation reuses
the duckdb extension scanner, so this test exercises the SQLite
extension path (no external infra) to pin:

* The introspection helper round-trips: a real SQLite file with two
  tables produces an :class:`IntrospectedDatabase` with the right
  tables + columns + types.
* The CLI dispatcher writes a v0.7.3 contract under ``--output`` with
  one ``exposes[]`` entry per table.
* The JDBC type → logical type mapping is reasonable.

Constraint extraction (H5 in the UX-audit findings) is exercised at
two layers:

* ``TestConstraintExtractors`` mocks the duckdb ``con.execute(...)``
  pass-through and verifies ``_extract_primary_keys`` /
  ``_extract_foreign_keys`` / ``_extract_check_constraints`` group
  composite keys correctly and filter NOT-NULL auto-CHECKs.
* ``TestConstraintsRoundTripIntoContract`` patches
  ``introspect_jdbc`` to return a constraint-laden database and
  inspects the emitted contract for PK markers + FK labels + CHECK
  validation rules + ``extensions.jdbcIntrospection`` block.

A live-Postgres marker (``live_postgres``) covers the real
``localhost:55432/tpch/retail`` sandbox per the audit's H5 follow-up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Path:
    """Create a real SQLite database with two tables to introspect."""
    db_path = tmp_path / "fixtures.sqlite"
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            created_at DATETIME
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount REAL,
            ordered_at DATETIME
        )
        """
    )
    con.commit()
    con.close()
    return db_path


class TestIntrospectJdbc:
    def test_sqlite_round_trip(self, sqlite_db: Path):
        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        db = introspect_jdbc(
            source="sqlite",
            uri=f"sqlite:///{sqlite_db}",
        )
        # Two tables enumerated.
        names = sorted(t.name for t in db.tables)
        assert names == ["customers", "orders"]

        customers = next(t for t in db.tables if t.name == "customers")
        cols = sorted(c.name for c in customers.columns)
        assert cols == ["created_at", "email", "id"]

    def test_table_filter_narrows_results(self, sqlite_db: Path):
        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        db = introspect_jdbc(
            source="sqlite",
            uri=f"sqlite:///{sqlite_db}",
            table_filter=["orders"],
        )
        assert [t.name for t in db.tables] == ["orders"]

    def test_unsupported_source_raises(self):
        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        with pytest.raises(ValueError, match="Unsupported"):
            introspect_jdbc(source="oracle", uri="x")

    def test_postgresql_aliases_to_postgres(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _normalize_kind,
        )

        assert _normalize_kind("postgresql") == "postgres"
        assert _normalize_kind("postgres") == "postgres"


class TestRunFromJdbcSource:
    def test_emits_v073_contract_from_sqlite(self, sqlite_db: Path, tmp_path: Path):
        import logging

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "out.fluid.yaml"
        args = SimpleNamespace(
            source="sqlite",
            uri=f"sqlite:///{sqlite_db}",
            schema_name=None,
            tables=None,
            name="forged_sqlite",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0
        assert out.exists()

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        assert contract["fluidVersion"] == "0.7.3"
        # Top-level kind is now required by v0.7.3.
        assert contract["kind"] == "DataProduct"
        assert contract["id"] == "forged_sqlite"
        assert contract["metadata"]["productType"] == "SDP"
        # owner is required by v0.7.3 metadata.
        assert "owner" in contract["metadata"]
        # exposes[] uses exposeId (not id), plus required kind + binding.
        exposes = {e["exposeId"]: e for e in contract["exposes"]}
        assert set(exposes) == {"customers", "orders"}
        for expose in exposes.values():
            assert expose["kind"] == "table"
            assert "binding" in expose
            assert expose["binding"]["platform"] == "local"

        customers_schema = exposes["customers"]["contract"]["schema"]
        col_names = [c["name"] for c in customers_schema]
        assert set(col_names) == {"id", "email", "created_at"}
        # Columns must use "type" (not logicalType / physicalType / nullable).
        for col in customers_schema:
            assert "type" in col
            assert "logicalType" not in col
            assert "physicalType" not in col
            assert "nullable" not in col

    def test_missing_uri_returns_error_code(self, tmp_path: Path):
        import logging

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        args = SimpleNamespace(
            source="postgres",
            uri=None,
            schema_name=None,
            tables=None,
            name=None,
            output=str(tmp_path / "x.yaml"),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 2

    def test_sqlite_without_name_derives_valid_id_from_file_stem(
        self, sqlite_db: Path, tmp_path: Path
    ):
        """Regression: with no ``--name``, the sqlite FILE PATH must not leak
        into the contract id. db.database is a path for sqlite (ATTACH takes a
        path), so the raw value ``/.../fixtures.sqlite`` previously became the
        id and the whole contract was rejected by the validator. It must now
        become the sanitized file stem and the contract must validate."""
        import logging
        import re as _re

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "out.fluid.yaml"
        args = SimpleNamespace(
            source="sqlite",
            uri=f"sqlite:///{sqlite_db}",
            schema_name=None,
            tables=None,
            name=None,  # the bug only triggers WITHOUT an explicit --name
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0, "emitted contract must pass in-process id validation"

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        # The fixture file is ``fixtures.sqlite`` → id is its stem, path-free,
        # and matches the FLUID identifier pattern.
        assert contract["id"] == "fixtures"
        assert "/" not in contract["id"]
        assert str(sqlite_db) not in contract["id"]
        assert _re.match(r"^[a-z0-9_][a-z0-9_.-]*[a-z0-9_]$|^[a-z0-9_]$", contract["id"])


class TestJdbcTypeMapping:
    def test_integer_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        for t in ("integer", "int", "smallint", "serial", "int8"):
            assert _map_jdbc_type_to_logical(t) == "integer"

    def test_decimal_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        for t in ("numeric", "decimal", "real", "double precision", "float"):
            assert _map_jdbc_type_to_logical(t) == "decimal"

    def test_string_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        for t in ("varchar", "text", "char", "uuid", "json"):
            assert _map_jdbc_type_to_logical(t) == "string"

    def test_temporal_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        assert _map_jdbc_type_to_logical("timestamp") == "timestamp"
        assert _map_jdbc_type_to_logical("datetime") == "timestamp"
        assert _map_jdbc_type_to_logical("date") == "date"
        assert _map_jdbc_type_to_logical("time") == "time"


class TestPrecisionScalePassThrough:
    """H18: ``numeric(15,2)`` / ``varchar(80)`` / ``char(1)`` must
    round-trip from the source database all the way into the contract's
    column ``type`` string instead of collapsing to bucket-only
    ``decimal`` / ``string``.
    """

    def test_decimal_with_precision_and_scale(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        assert (
            _map_jdbc_type_to_logical("numeric", numeric_precision=15, numeric_scale=2)
            == "decimal(15,2)"
        )

    def test_decimal_with_only_precision(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        assert _map_jdbc_type_to_logical("numeric", numeric_precision=18) == "decimal(18)"

    def test_decimal_bucket_when_no_precision(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        assert _map_jdbc_type_to_logical("numeric") == "decimal"

    def test_varchar_with_max_length(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        assert _map_jdbc_type_to_logical("varchar", character_max_length=80) == "varchar(80)"
        assert (
            _map_jdbc_type_to_logical("character varying", character_max_length=25) == "varchar(25)"
        )

    def test_char_with_max_length(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        # When the source type contains "char" but NOT "varchar" /
        # "varying" we emit char(N), not varchar(N).
        assert _map_jdbc_type_to_logical("char", character_max_length=1) == "char(1)"
        assert _map_jdbc_type_to_logical("character", character_max_length=10) == "char(10)"

    def test_string_bucket_for_uuid_and_json_ignores_length(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        # uuid / json don't take a length — emit bare string even if
        # character_max_length comes back populated.
        assert _map_jdbc_type_to_logical("uuid", character_max_length=36) == "string"
        assert _map_jdbc_type_to_logical("json") == "string"


class TestConstraintExtractors:
    """Pin the per-helper extraction logic (PK / FK / CHECK) against
    a mocked duckdb connection. We feed canned rows and check the
    grouping, composite-key handling, and NOT-NULL auto-CHECK filtering.
    """

    def test_primary_keys_simple_and_composite(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _extract_primary_keys,
        )

        con = MagicMock()
        # rows: (constraint_name, schema, table_name, column_name, ordinal_position)
        # lineitem has a composite PK over (l_orderkey, l_linenumber).
        con.execute.return_value.fetchall.return_value = [
            ("customer_pkey", "retail", "customer", "c_custkey", 1),
            ("lineitem_pkey", "retail", "lineitem", "l_orderkey", 1),
            ("lineitem_pkey", "retail", "lineitem", "l_linenumber", 2),
            ("orders_pkey", "retail", "orders", "o_orderkey", 1),
            ("region_pkey", "retail", "region", "r_regionkey", 1),
            ("nation_pkey", "retail", "nation", "n_nationkey", 1),
        ]
        result = _extract_primary_keys(con, "src", "postgres", "retail")

        assert result["customer"] == ["c_custkey"]
        # Composite ordering follows ordinal_position.
        assert result["lineitem"] == ["l_orderkey", "l_linenumber"]
        assert result["orders"] == ["o_orderkey"]
        assert result["region"] == ["r_regionkey"]
        assert result["nation"] == ["n_nationkey"]

    def test_primary_keys_pass_through_query_called(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _extract_primary_keys,
        )

        con = MagicMock()
        con.execute.return_value.fetchall.return_value = []
        _extract_primary_keys(con, "source_db", "postgres", "retail")
        # The pass-through has to go via postgres_query() so the FK
        # rows aren't filtered by duckdb's union view.
        called = con.execute.call_args[0][0]
        assert "postgres_query('source_db'" in called
        assert "PRIMARY KEY" in called
        assert "retail" in called

    def test_foreign_keys_simple_single_column(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _extract_foreign_keys,
        )

        con = MagicMock()
        # Real shape from postgres_query() on the sandbox:
        # (cname, src_schema, src_table, src_col, src_pos,
        #  tgt_schema, tgt_table, tgt_col, update, delete, match)
        con.execute.return_value.fetchall.return_value = [
            (
                "customer_c_nationkey_fkey",
                "retail",
                "customer",
                "c_nationkey",
                1,
                "retail",
                "nation",
                "n_nationkey",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
            (
                "lineitem_l_orderkey_fkey",
                "retail",
                "lineitem",
                "l_orderkey",
                1,
                "retail",
                "orders",
                "o_orderkey",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
            (
                "nation_n_regionkey_fkey",
                "retail",
                "nation",
                "n_regionkey",
                1,
                "retail",
                "region",
                "r_regionkey",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
            (
                "orders_o_custkey_fkey",
                "retail",
                "orders",
                "o_custkey",
                1,
                "retail",
                "customer",
                "c_custkey",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
        ]
        result = _extract_foreign_keys(con, "src", "postgres", "retail")

        # All 4 FK edges captured.
        assert {tbl for tbl in result.keys()} == {
            "customer",
            "lineitem",
            "nation",
            "orders",
        }
        cust_fk = result["customer"][0]
        assert cust_fk.constraint_name == "customer_c_nationkey_fkey"
        assert cust_fk.from_columns == ["c_nationkey"]
        assert cust_fk.to_table == "nation"
        assert cust_fk.to_columns == ["n_nationkey"]
        assert cust_fk.to_schema == "retail"
        assert cust_fk.update_rule == "NO ACTION"
        assert cust_fk.delete_rule == "NO ACTION"

    def test_foreign_keys_composite_columns_grouped(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _extract_foreign_keys,
        )

        # Composite FK over (a, b) → (x, y) — two rows, one FK.
        con = MagicMock()
        con.execute.return_value.fetchall.return_value = [
            (
                "child_fk",
                "s",
                "child",
                "a",
                1,
                "s",
                "parent",
                "x",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
            (
                "child_fk",
                "s",
                "child",
                "b",
                2,
                "s",
                "parent",
                "y",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
        ]
        result = _extract_foreign_keys(con, "src", "postgres", "s")
        assert len(result["child"]) == 1
        fk = result["child"][0]
        # Composite columns are zipped position-aligned.
        assert fk.from_columns == ["a", "b"]
        assert fk.to_columns == ["x", "y"]

    def test_foreign_keys_sqlite_returns_empty(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _extract_foreign_keys,
        )

        con = MagicMock()
        result = _extract_foreign_keys(con, "src", "sqlite", None)
        # SQLite path is a degraded no-op — duckdb has no
        # sqlite_query() and the union view drops FK rows.
        assert result == {}

    def test_check_constraints_filters_not_null(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _extract_check_constraints,
        )

        con = MagicMock()
        # (cname, check_clause, schema, table_name, column_name)
        con.execute.return_value.fetchall.return_value = [
            # Auto-emitted NOT NULL CHECK — filtered out.
            (
                "customer_c_custkey_not_null",
                "c_custkey IS NOT NULL",
                "retail",
                "customer",
                "c_custkey",
            ),
            # Parenthesised NOT NULL — also filtered.
            (
                "customer_c_name_not_null",
                "(c_name IS NOT NULL)",
                "retail",
                "customer",
                "c_name",
            ),
            # Real application CHECK — kept.
            (
                "orders_o_orderstatus_check",
                "((o_orderstatus = ANY (ARRAY['O'::bpchar, 'F'::bpchar, 'P'::bpchar])))",
                "retail",
                "orders",
                "o_orderstatus",
            ),
            (
                "orders_o_totalprice_check",
                "((o_totalprice >= (0)::numeric))",
                "retail",
                "orders",
                "o_totalprice",
            ),
            (
                "lineitem_l_quantity_check",
                "((l_quantity > (0)::numeric))",
                "retail",
                "lineitem",
                "l_quantity",
            ),
        ]
        result = _extract_check_constraints(con, "src", "postgres", "retail")

        # NOT-NULL auto-checks for customer dropped.
        assert "customer" not in result
        # Application CHECKs preserved verbatim.
        assert len(result["orders"]) == 2
        names = sorted(c.constraint_name for c in result["orders"])
        assert names == [
            "orders_o_orderstatus_check",
            "orders_o_totalprice_check",
        ]
        # Lineitem has one application check.
        assert result["lineitem"][0].constraint_name == "lineitem_l_quantity_check"
        assert result["lineitem"][0].columns == ["l_quantity"]

    def test_check_constraints_sqlite_returns_empty(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _extract_check_constraints,
        )

        con = MagicMock()
        result = _extract_check_constraints(con, "src", "sqlite", None)
        assert result == {}


class TestConstraintsRoundTripIntoContract:
    """Pin that the constraints flow from
    :class:`IntrospectedDatabase` all the way into the emitted v0.7.3
    contract. We patch ``introspect_jdbc`` so we don't need a live DB.
    """

    def _make_db(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            IntrospectedCheckConstraint,
            IntrospectedColumn,
            IntrospectedDatabase,
            IntrospectedForeignKey,
            IntrospectedTable,
        )

        customer = IntrospectedTable(
            schema="retail",
            name="customer",
            columns=[
                IntrospectedColumn(
                    name="c_custkey",
                    type_name="bigint",
                    nullable=False,
                ),
                IntrospectedColumn(
                    name="c_email",
                    type_name="varchar",
                    nullable=True,
                    character_max_length=80,
                ),
                IntrospectedColumn(
                    name="c_acctbal",
                    type_name="numeric",
                    nullable=True,
                    numeric_precision=15,
                    numeric_scale=2,
                ),
                IntrospectedColumn(
                    name="c_phone",
                    type_name="char",
                    nullable=True,
                    character_max_length=15,
                ),
                IntrospectedColumn(
                    name="c_nationkey",
                    type_name="integer",
                    nullable=True,
                ),
            ],
            primary_key_columns=["c_custkey"],
            foreign_keys=[
                IntrospectedForeignKey(
                    constraint_name="customer_c_nationkey_fkey",
                    from_columns=["c_nationkey"],
                    to_schema="retail",
                    to_table="nation",
                    to_columns=["n_nationkey"],
                )
            ],
            checks=[],
        )
        orders = IntrospectedTable(
            schema="retail",
            name="orders",
            columns=[
                IntrospectedColumn(name="o_orderkey", type_name="bigint", nullable=False),
                IntrospectedColumn(
                    name="o_orderstatus",
                    type_name="character",
                    nullable=True,
                    character_max_length=1,
                ),
                IntrospectedColumn(
                    name="o_totalprice",
                    type_name="numeric",
                    nullable=True,
                    numeric_precision=15,
                    numeric_scale=2,
                ),
                IntrospectedColumn(
                    name="o_custkey",
                    type_name="bigint",
                    nullable=False,
                ),
            ],
            primary_key_columns=["o_orderkey"],
            foreign_keys=[
                IntrospectedForeignKey(
                    constraint_name="orders_o_custkey_fkey",
                    from_columns=["o_custkey"],
                    to_schema="retail",
                    to_table="customer",
                    to_columns=["c_custkey"],
                )
            ],
            checks=[
                IntrospectedCheckConstraint(
                    constraint_name="orders_o_orderstatus_check",
                    check_clause=(
                        "((o_orderstatus = ANY " "(ARRAY['O'::bpchar, 'F'::bpchar, 'P'::bpchar])))"
                    ),
                    columns=["o_orderstatus"],
                ),
                IntrospectedCheckConstraint(
                    constraint_name="orders_o_totalprice_check",
                    check_clause="((o_totalprice >= (0)::numeric))",
                    columns=["o_totalprice"],
                ),
            ],
        )
        return IntrospectedDatabase(
            source_kind="postgres", database="tpch", tables=[customer, orders]
        )

    def test_pk_marked_required_with_primary_key_tag(self, tmp_path, monkeypatch):
        import logging

        # ``_run_from_jdbc_source`` imports introspect_jdbc lazily from
        # the discover submodule, so patch the source attribute the
        # production code resolves.
        from fluid_build.cli.discover import _jdbc_introspect as _intro

        monkeypatch.setattr(_intro, "introspect_jdbc", lambda **kwargs: self._make_db())

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "retail.fluid.yaml"
        args = SimpleNamespace(
            source="postgres",
            uri="postgresql://u:p@h:5432/tpch",
            schema_name="retail",
            tables=None,
            name="retail",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0, "should succeed against the mocked DB"

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        exposes = {e["exposeId"]: e for e in contract["exposes"]}
        customer_schema = {c["name"]: c for c in exposes["customer"]["contract"]["schema"]}
        assert customer_schema["c_custkey"]["required"] is True
        assert "primary-key" in customer_schema["c_custkey"]["tags"]

    def test_fk_marked_with_foreign_key_tag_and_target_label(self, tmp_path, monkeypatch):
        import logging

        from fluid_build.cli.discover import _jdbc_introspect as _intro

        monkeypatch.setattr(_intro, "introspect_jdbc", lambda **kwargs: self._make_db())

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "retail.fluid.yaml"
        args = SimpleNamespace(
            source="postgres",
            uri="postgresql://u:p@h:5432/tpch",
            schema_name="retail",
            tables=None,
            name="retail",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        exposes = {e["exposeId"]: e for e in contract["exposes"]}
        c_nationkey = next(
            c for c in exposes["customer"]["contract"]["schema"] if c["name"] == "c_nationkey"
        )
        assert "foreign-key" in c_nationkey["tags"]
        assert c_nationkey["labels"]["jdbc.fk.target"] == "retail.nation.n_nationkey"

    def test_check_constraint_lands_as_validation_rule(self, tmp_path, monkeypatch):
        import logging

        from fluid_build.cli.discover import _jdbc_introspect as _intro

        monkeypatch.setattr(_intro, "introspect_jdbc", lambda **kwargs: self._make_db())

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "retail.fluid.yaml"
        args = SimpleNamespace(
            source="postgres",
            uri="postgresql://u:p@h:5432/tpch",
            schema_name="retail",
            tables=None,
            name="retail",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        exposes = {e["exposeId"]: e for e in contract["exposes"]}
        orders_schema = {c["name"]: c for c in exposes["orders"]["contract"]["schema"]}
        o_status = orders_schema["o_orderstatus"]
        assert "validationRules" in o_status
        # Casts stripped, parens normalised — expression reads cleanly.
        rule = o_status["validationRules"][0]
        assert rule["type"] == "custom"
        assert "o_orderstatus" in rule["constraint"]
        assert "ARRAY['O', 'F', 'P']" in rule["constraint"] or "'O'" in rule["constraint"]

        o_totalprice = orders_schema["o_totalprice"]
        rule2 = o_totalprice["validationRules"][0]
        assert rule2["constraint"].startswith("o_totalprice")
        assert ">= " in rule2["constraint"] or ">=" in rule2["constraint"]

    def test_precision_scale_in_column_type(self, tmp_path, monkeypatch):
        """``numeric(15,2)`` and ``varchar(80)`` and ``char(1)`` flow
        through as parameterised type strings, not bucket-collapsed
        ``decimal`` / ``string``.
        """
        import logging

        from fluid_build.cli.discover import _jdbc_introspect as _intro

        monkeypatch.setattr(_intro, "introspect_jdbc", lambda **kwargs: self._make_db())

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "retail.fluid.yaml"
        args = SimpleNamespace(
            source="postgres",
            uri="postgresql://u:p@h:5432/tpch",
            schema_name="retail",
            tables=None,
            name="retail",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        exposes = {e["exposeId"]: e for e in contract["exposes"]}

        customer_schema = {c["name"]: c for c in exposes["customer"]["contract"]["schema"]}
        assert customer_schema["c_acctbal"]["type"] == "decimal(15,2)"
        assert customer_schema["c_email"]["type"] == "varchar(80)"
        assert customer_schema["c_phone"]["type"] == "char(15)"

        orders_schema = {c["name"]: c for c in exposes["orders"]["contract"]["schema"]}
        assert orders_schema["o_totalprice"]["type"] == "decimal(15,2)"
        # NOTE: "character" type with character_max_length=1 emits
        # char(1) (not varchar(1)) — preserves source-side intent.
        assert orders_schema["o_orderstatus"]["type"] == "char(1)"

    def test_extensions_jdbc_introspection_block_attached(self, tmp_path, monkeypatch):
        """The top-level ``extensions.jdbcIntrospection`` block carries
        the complete PK/FK/CHECK surface for downstream consumers that
        can't read each column's tags.
        """
        import logging

        from fluid_build.cli.discover import _jdbc_introspect as _intro

        monkeypatch.setattr(_intro, "introspect_jdbc", lambda **kwargs: self._make_db())

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "retail.fluid.yaml"
        args = SimpleNamespace(
            source="postgres",
            uri="postgresql://u:p@h:5432/tpch",
            schema_name="retail",
            tables=None,
            name="retail",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        ext = contract["extensions"]["jdbcIntrospection"]
        assert ext["source_kind"] == "postgres"
        assert ext["database"] == "tpch"
        assert ext["schemas"] == ["retail"]

        # Both FKs present, each carrying source/target table+columns.
        fk_pairs = {(fk["from_table"], fk["to_table"]) for fk in ext["foreignKeys"]}
        assert ("customer", "nation") in fk_pairs
        assert ("orders", "customer") in fk_pairs

        # PKs at the catalog level.
        pks = {p["table"]: p["columns"] for p in ext["primaryKeys"]}
        assert pks["customer"] == ["c_custkey"]
        assert pks["orders"] == ["o_orderkey"]

        # Application CHECKs preserved verbatim (with casts).
        check_names = {c["constraint_name"] for c in ext["checkConstraints"]}
        assert "orders_o_orderstatus_check" in check_names
        assert "orders_o_totalprice_check" in check_names


# ---------------------------------------------------------------------------
# Live-Postgres integration coverage. Skipped when the docker sandbox
# isn't reachable. Matches the audit's H5 requirement to verify a real
# introspect carries every FK + PK + CHECK.
# ---------------------------------------------------------------------------


def _postgres_sandbox_reachable() -> bool:
    """Cheap reachability probe — duckdb postgres extension connect
    against localhost:55432/tpch. Returns True iff the ATTACH succeeds.
    """
    try:
        import duckdb as _duckdb  # noqa: F401
    except ImportError:
        return False
    try:
        con = duckdb.connect(":memory:")
        try:
            con.execute("INSTALL postgres; LOAD postgres;")
            con.execute(
                "ATTACH 'dbname=tpch host=localhost port=55432 "
                "user=postgres password=postgres' AS probe (TYPE postgres)"
            )
            con.execute("SELECT 1 FROM information_schema.tables LIMIT 1").fetchall()
            return True
        finally:
            con.close()
    except Exception:
        return False


@pytest.mark.live_postgres
@pytest.mark.skipif(
    not _postgres_sandbox_reachable(),
    reason="postgres sandbox at localhost:55432/tpch not reachable",
)
class TestLivePostgresRoundTrip:
    """Real-DB sanity check against the audit's
    ``/tmp/forge-ux-sandboxes/from-source-postgres/`` sandbox.

    Confirms (a) all 6 FK edges are captured (4 in retail + others
    expected per the docker compose fixture), (b) PKs stamp the
    ``primary-key`` tag on every PK column, (c) the application CHECK
    constraints lineitem_l_quantity_check / orders_o_orderstatus_check /
    orders_o_totalprice_check make it into validationRules.
    """

    URI = "postgresql://postgres:postgres@localhost:55432/tpch"

    def test_introspect_carries_pk_fk_check(self):
        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        db = introspect_jdbc(source="postgres", uri=self.URI, schema_filter="retail")
        by_name = {t.name: t for t in db.tables}

        # 5 tables expected: customer, orders, lineitem, nation, region.
        assert {"customer", "orders", "lineitem", "nation", "region"} <= set(by_name)

        # PKs.
        assert by_name["customer"].primary_key_columns == ["c_custkey"]
        assert by_name["lineitem"].primary_key_columns == [
            "l_orderkey",
            "l_linenumber",
        ]
        assert by_name["orders"].primary_key_columns == ["o_orderkey"]

        # 4 FK edges in the retail schema (per pg_constraint contype='f').
        all_fks = [fk for t in db.tables for fk in t.foreign_keys]
        assert len(all_fks) == 4
        edges = {(fk.to_table, tuple(fk.to_columns)) for fk in all_fks}
        assert ("nation", ("n_nationkey",)) in edges
        assert ("orders", ("o_orderkey",)) in edges
        assert ("region", ("r_regionkey",)) in edges
        assert ("customer", ("c_custkey",)) in edges

        # Application CHECK constraints (NOT-NULL auto-checks excluded).
        all_checks = [chk for t in db.tables for chk in t.checks]
        check_names = {c.constraint_name for c in all_checks}
        assert "orders_o_orderstatus_check" in check_names
        assert "orders_o_totalprice_check" in check_names
        assert "lineitem_l_quantity_check" in check_names

    def test_contract_emit_validates_and_carries_constraints(self, tmp_path):
        import logging

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "retail.fluid.yaml"
        args = SimpleNamespace(
            source="postgres",
            uri=self.URI,
            schema_name="retail",
            tables=None,
            name="retail_live",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        # Validates against v0.7.3.
        from fluid_build.schema_manager import validate_contract_file

        vr = validate_contract_file(str(out), offline_only=True, logger=logging.getLogger("test"))
        assert vr.is_valid, [str(e) for e in vr.errors]

        # Precision / scale survive.
        exposes = {e["exposeId"]: e for e in contract["exposes"]}
        cust_cols = {c["name"]: c for c in exposes["customer"]["contract"]["schema"]}
        assert cust_cols["c_acctbal"]["type"] == "decimal(15,2)"
        assert cust_cols["c_email"]["type"] == "varchar(80)"

        # PK tag on c_custkey.
        assert "primary-key" in cust_cols["c_custkey"]["tags"]

        # FK tag on c_nationkey pointing at nation.n_nationkey.
        assert "foreign-key" in cust_cols["c_nationkey"]["tags"]
        assert "nation" in cust_cols["c_nationkey"]["labels"]["jdbc.fk.target"]

        # extensions.jdbcIntrospection carries all 4 FKs + 3 CHECKs.
        ext = contract["extensions"]["jdbcIntrospection"]
        assert len(ext["foreignKeys"]) == 4
        check_names = {c["constraint_name"] for c in ext["checkConstraints"]}
        assert {
            "orders_o_orderstatus_check",
            "orders_o_totalprice_check",
            "lineitem_l_quantity_check",
        } <= check_names
