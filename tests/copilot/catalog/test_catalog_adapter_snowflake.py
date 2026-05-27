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

"""Snowflake catalog adapter coverage — RETEST-6 precision/scale pin.

H3 fixed the STRING-everywhere DDL bug. RETEST-6 found that NUMBER
columns still emitted as bare ``NUMBER`` because the Snowflake catalog
adapter discarded NUMERIC_PRECISION / NUMERIC_SCALE at the SELECT
layer (the warehouse had the right answer in
INFORMATION_SCHEMA.COLUMNS; the adapter just wasn't reading those
columns).

These tests pin:

* The COLUMNS SELECT now includes NUMERIC_PRECISION / NUMERIC_SCALE.
* The adapter composes them into the returned
  :attr:`CatalogColumn.data_type` (e.g. ``NUMBER(15,2)``).
* Non-NUMBER columns (VARCHAR / TEXT / TIMESTAMP_TZ / BOOLEAN) stay
  untouched — the composer must not append spurious ``(p,s)`` to
  string / temporal types.
* Bare NUMBER (precision null) stays bare so the downstream emitter
  can fall back to Snowflake's default ``NUMBER(38,0)`` semantics
  without us inventing a different precision.

The Snowflake connector SDK is stubbed via ``sys.modules`` injection
(matches the pattern in ``test_catalog_adapter_bigquery.py``).
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    SnowflakeCredentials,
)

# ----------------------------------------------------------------------
# SDK stub
# ----------------------------------------------------------------------


def _stub_snowflake_connector_module() -> ModuleType:
    """Build a minimal ``snowflake.connector`` stub.

    Only the entry points the adapter actually uses are populated:
    ``connect`` (returns a connection whose ``cursor()`` returns a
    MagicMock cursor whose ``fetchall`` / ``fetchone`` the test
    pre-programs).
    """
    snowflake_module = ModuleType("snowflake")
    connector_module = ModuleType("snowflake.connector")
    connector_module.connect = MagicMock(name="snowflake.connector.connect")
    snowflake_module.connector = connector_module
    return snowflake_module


@pytest.fixture
def snowflake_sdk_stub(monkeypatch):
    """Install the Snowflake SDK stub for the duration of one test."""
    snowflake_module = _stub_snowflake_connector_module()
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_module)
    monkeypatch.setitem(sys.modules, "snowflake.connector", snowflake_module.connector)
    yield snowflake_module.connector.connect


def _make_adapter() -> Any:
    from fluid_build.copilot.catalog.snowflake import SnowflakeCatalogAdapter

    return SnowflakeCatalogAdapter(
        credentials=SnowflakeCredentials(
            account="acct-123",
            user="bob",
            auth_method="password",
            password="hunter2",
            role="ENGINEER",
            warehouse="XS",
        )
    )


# ----------------------------------------------------------------------
# from_resolver — canonical construction path (matches every adapter)
# ----------------------------------------------------------------------


class TestFromResolver:
    def test_inline_credentials_construct_adapter(self):
        from fluid_build.copilot.catalog.snowflake import SnowflakeCatalogAdapter

        resolver = CredentialResolver(sources_config_path="/tmp/none.yaml")
        adapter = SnowflakeCatalogAdapter.from_resolver(
            resolver,
            inline_credentials={
                "account": "acct-123",
                "user": "bob",
                "auth_method": "password",
                "password": "hunter2",
            },
        )
        assert adapter._credentials.account == "acct-123"
        assert adapter._credentials.user == "bob"


# ----------------------------------------------------------------------
# RETEST-6 — composer helper unit coverage
# ----------------------------------------------------------------------


class TestComposeDataTypeHelper:
    def test_number_with_precision_and_scale(self):
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", 15, 2) == "NUMBER(15,2)"

    def test_number_with_zero_scale(self):
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", 10, 0) == "NUMBER(10,0)"

    def test_decimal_dec_numeric_aliases_all_parameterised(self):
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("DECIMAL", 18, 4) == "DECIMAL(18,4)"
        assert _compose_data_type_with_precision_scale("DEC", 6, 2) == "DEC(6,2)"
        assert _compose_data_type_with_precision_scale("NUMERIC", 12, 6) == "NUMERIC(12,6)"

    def test_text_varchar_timestamp_untouched(self):
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("TEXT", None, None) == "TEXT"
        assert _compose_data_type_with_precision_scale("TIMESTAMP_TZ", None, None) == "TIMESTAMP_TZ"
        assert _compose_data_type_with_precision_scale("VARCHAR", None, None) == "VARCHAR"

    def test_non_numeric_with_spurious_precision_still_untouched(self):
        """Defensive guard: even if a future schema returns
        NUMERIC_PRECISION on a TIMESTAMP_TZ row, we do NOT compose
        ``TIMESTAMP_TZ(6)`` — only NUMBER-family types get
        parameterised."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("TIMESTAMP_TZ", 9, None) == "TIMESTAMP_TZ"
        assert _compose_data_type_with_precision_scale("BOOLEAN", 1, 0) == "BOOLEAN"

    def test_number_with_precision_only(self):
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", 18, None) == "NUMBER(18)"

    def test_bare_number_preserved_when_no_precision(self):
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", None, None) == "NUMBER"

    def test_empty_data_type_falls_back_to_string(self):
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale(None, 15, 2) == "STRING"
        assert _compose_data_type_with_precision_scale("", 15, 2) == "STRING"


# ----------------------------------------------------------------------
# RETEST-6 — get_table SELECT must include NUMERIC_PRECISION / SCALE,
# and the returned CatalogColumn must carry the composed type.
# ----------------------------------------------------------------------


def _wire_get_table_cursor(
    connect_mock: MagicMock,
    *,
    column_rows: list[tuple],
    header_row: tuple = ("invoice table comment", "DATA_ENG", None),
    pk_rows: list[tuple] | None = None,
    fk_rows: list[tuple] | None = None,
    tag_rows: list[tuple] | None = None,
) -> MagicMock:
    """Pre-program the cursor's ``fetchall`` / ``fetchone`` for a
    ``get_table`` call. Order matches the adapter's call sequence
    (header → columns → PK → FK → tags).
    """
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    connect_mock.return_value = conn

    pk_rows = pk_rows or []
    fk_rows = fk_rows or []
    tag_rows = tag_rows or []

    # Cursor calls in adapter: header (fetchone), columns (fetchall),
    # PK (fetchall — via _fetch_primary_key), FK (fetchall — via
    # _fetch_foreign_keys), tags (fetchall — via _fetch_tags).
    cur.fetchone.return_value = header_row
    cur.fetchall.side_effect = [column_rows, pk_rows, fk_rows, tag_rows]
    return cur


class TestGetTableComposesPrecisionScale:
    def test_columns_select_includes_numeric_precision_and_scale(self, snowflake_sdk_stub):
        """The COLUMNS SELECT must fetch NUMERIC_PRECISION and
        NUMERIC_SCALE — without these, the composer has nothing to
        work with and we regress to bare ``NUMBER``."""
        _wire_get_table_cursor(
            snowflake_sdk_stub,
            column_rows=[
                ("INVOICE_ID", "TEXT", "NO", "Invoice id", 1, None, None),
            ],
        )
        adapter = _make_adapter()
        adapter.get_table("TELCO_LAB.TELCO_STAGE_LOAD.INVOICES")

        # Inspect every cur.execute call's SQL string. The columns
        # query is the second call (after the header query).
        cur = snowflake_sdk_stub.return_value.cursor.return_value
        executed_sqls = [call.args[0] for call in cur.execute.call_args_list if call.args]
        columns_sql = next(sql for sql in executed_sqls if "INFORMATION_SCHEMA.COLUMNS" in sql)
        assert "NUMERIC_PRECISION" in columns_sql
        assert "NUMERIC_SCALE" in columns_sql

    def test_number_column_emits_parameterised_data_type(self, snowflake_sdk_stub):
        """RETEST-6 core: NUMBER + precision + scale → ``NUMBER(15,2)``."""
        _wire_get_table_cursor(
            snowflake_sdk_stub,
            column_rows=[
                # (COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COMMENT,
                #  ORDINAL_POSITION, NUMERIC_PRECISION, NUMERIC_SCALE)
                ("INVOICE_ID", "TEXT", "NO", "id", 1, None, None),
                ("AMOUNT_CHF", "NUMBER", "YES", "amount", 2, 15, 2),
            ],
        )
        adapter = _make_adapter()
        table = adapter.get_table("TELCO_LAB.TELCO_STAGE_LOAD.INVOICES")

        by_name = {col.name: col for col in table.columns}
        assert by_name["AMOUNT_CHF"].data_type == "NUMBER(15,2)"
        # TEXT must stay TEXT — no spurious parameterisation.
        assert by_name["INVOICE_ID"].data_type == "TEXT"

    def test_integer_like_number_emits_with_scale_zero(self, snowflake_sdk_stub):
        """``NUMBER(10,0)`` for integer-like columns survives
        round-trip — pre-fix this collapsed to bare ``NUMBER``."""
        _wire_get_table_cursor(
            snowflake_sdk_stub,
            column_rows=[
                ("SESSION_MINUTES", "NUMBER", "YES", "minutes", 1, 10, 0),
            ],
        )
        adapter = _make_adapter()
        table = adapter.get_table("TELCO_LAB.TELCO_STAGE_LOAD.SESSIONS")

        assert table.columns[0].data_type == "NUMBER(10,0)"

    def test_varchar_with_max_length_not_decorated_by_numeric_composer(self, snowflake_sdk_stub):
        """A VARCHAR / TEXT column with numeric_precision NULL must
        emit as-is — the composer must not invent parameters from
        thin air."""
        _wire_get_table_cursor(
            snowflake_sdk_stub,
            column_rows=[
                ("CUSTOMER_NAME", "TEXT", "YES", "name", 1, None, None),
                ("EMAIL", "VARCHAR", "YES", "email", 2, None, None),
            ],
        )
        adapter = _make_adapter()
        table = adapter.get_table("TELCO_LAB.TELCO_STAGE_LOAD.CUSTOMERS")

        by_name = {col.name: col for col in table.columns}
        assert by_name["CUSTOMER_NAME"].data_type == "TEXT"
        assert by_name["EMAIL"].data_type == "VARCHAR"

    def test_bare_number_when_precision_unknown(self, snowflake_sdk_stub):
        """When NUMERIC_PRECISION comes back NULL (rare external-table
        case), the adapter must keep bare ``NUMBER`` — emitter falls
        back to Snowflake's default ``NUMBER(38,0)`` semantics. We do
        NOT invent a precision."""
        _wire_get_table_cursor(
            snowflake_sdk_stub,
            column_rows=[
                ("METRIC_RAW", "NUMBER", "YES", "raw metric", 1, None, None),
            ],
        )
        adapter = _make_adapter()
        table = adapter.get_table("TELCO_LAB.TELCO_STAGE_LOAD.METRICS")

        assert table.columns[0].data_type == "NUMBER"

    def test_mixed_column_types_all_routed_correctly(self, snowflake_sdk_stub):
        """End-to-end matrix: NUMBER(15,2), NUMBER(10,0), TEXT,
        TIMESTAMP_TZ, BOOLEAN all flow through one ``get_table``
        call without collisions."""
        _wire_get_table_cursor(
            snowflake_sdk_stub,
            column_rows=[
                ("INVOICE_ID", "TEXT", "NO", None, 1, None, None),
                ("AMOUNT_CHF", "NUMBER", "YES", None, 2, 15, 2),
                ("VAT_AMOUNT", "DECIMAL", "YES", None, 3, 18, 4),
                ("INVOICE_DATE", "TIMESTAMP_TZ", "YES", None, 4, None, None),
                ("SESSION_MINUTES", "NUMBER", "YES", None, 5, 10, 0),
                ("IS_PAID", "BOOLEAN", "YES", None, 6, None, None),
            ],
        )
        adapter = _make_adapter()
        table = adapter.get_table("TELCO_LAB.TELCO_STAGE_LOAD.INVOICES")

        by_name = {col.name: col.data_type for col in table.columns}
        assert by_name == {
            "INVOICE_ID": "TEXT",
            "AMOUNT_CHF": "NUMBER(15,2)",
            "VAT_AMOUNT": "DECIMAL(18,4)",
            "INVOICE_DATE": "TIMESTAMP_TZ",
            "SESSION_MINUTES": "NUMBER(10,0)",
            "IS_PAID": "BOOLEAN",
        }
