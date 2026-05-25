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

"""Tests for ``snowflake.actions.table.alter_table``.

Pins the alteration data-shape contract (BUG-ALTER-TABLE): the alteration
KIND is read from ``"kind"``, a key deliberately distinct from a column's
``"type"`` data-type key. Previously both were read from ``"type"``, which
made the ``add_column`` branch structurally unreachable with a real column
type. Also confirms the ``validate_sql_type`` allowlist (BUG-SQL-TYPE)
guards the ALTER TABLE ... ADD COLUMN path.

The CREATE TABLE and schema-evolution ADD COLUMN injection paths are
covered by ``tests/providers/test_sql_type_safety.py`` — this file is the
``alter_table`` data-shape + ADD-COLUMN-type coverage only.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers._sql_safety import SqlTypeError
from fluid_build.providers.snowflake.actions import table as table_action

pytestmark = pytest.mark.unit

# The canonical "break out of the type slot and append DDL" payload.
_INJECTION_TYPE = "decimal(18); DROP TABLE victims; CREATE TABLE r (a INT"


def _provider() -> MagicMock:
    """A provider double exposing only what ``alter_table`` touches."""
    prov = MagicMock()
    prov.warehouse = "WH"
    prov._kwargs = {}
    return prov


@contextmanager
def _patched_connection():
    """Patch ``SnowflakeConnection`` + ``get_connection_params`` on the
    table action module; yield the connection mock for SQL assertions."""
    conn = MagicMock()
    conn.execute.return_value = []
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    with (
        patch.object(table_action, "SnowflakeConnection", return_value=ctx),
        patch.object(table_action, "get_connection_params", return_value={}),
    ):
        yield conn


def _action(alterations: list) -> dict:
    return {
        "op": "sf.table.alter",
        "database": "MYDB",
        "schema": "MYSCHEMA",
        "table": "MYTABLE",
        "account": "acct",
        "alterations": alterations,
    }


def _executed_sql(conn: MagicMock) -> str:
    return "\n".join(str(c.args[0]) for c in conn.execute.call_args_list)


class TestAlterTableShape:
    """The alteration ``kind`` key is distinct from a column ``type`` key."""

    def test_add_column_kind_and_type_are_distinct(self) -> None:
        prov = _provider()
        with _patched_connection() as conn:
            table_action.alter_table(
                _action([{"kind": "add_column", "name": "amount", "type": "NUMBER(38,0)"}]),
                prov,
            )
        sql = _executed_sql(conn)
        assert "ADD COLUMN" in sql
        # The column DATA TYPE reaches the DDL — not collapsed to the kind.
        assert "NUMBER(38,0)" in sql
        assert "add_column" not in sql

    def test_add_column_not_null(self) -> None:
        prov = _provider()
        with _patched_connection() as conn:
            table_action.alter_table(
                _action(
                    [{"kind": "add_column", "name": "amt", "type": "NUMBER", "nullable": False}]
                ),
                prov,
            )
        assert "NOT NULL" in _executed_sql(conn)

    def test_drop_column(self) -> None:
        prov = _provider()
        with _patched_connection() as conn:
            table_action.alter_table(_action([{"kind": "drop_column", "name": "stale"}]), prov)
        assert "DROP COLUMN" in _executed_sql(conn)

    def test_rename_column(self) -> None:
        prov = _provider()
        with _patched_connection() as conn:
            table_action.alter_table(
                _action([{"kind": "rename_column", "old_name": "a", "new_name": "b"}]), prov
            )
        assert "RENAME COLUMN" in _executed_sql(conn)

    def test_all_three_kinds_in_one_call(self) -> None:
        prov = _provider()
        with _patched_connection() as conn:
            table_action.alter_table(
                _action(
                    [
                        {"kind": "add_column", "name": "c1", "type": "VARCHAR"},
                        {"kind": "drop_column", "name": "c2"},
                        {"kind": "rename_column", "old_name": "c3", "new_name": "c4"},
                    ]
                ),
                prov,
            )
        sql = _executed_sql(conn)
        assert "ADD COLUMN" in sql
        assert "DROP COLUMN" in sql
        assert "RENAME COLUMN" in sql

    def test_set_nullable_true_emits_drop_not_null(self) -> None:
        """Toggling a column to nullable emits ``ALTER COLUMN ... DROP
        NOT NULL`` — the missing branch surfaced by the lab's pre3
        Meltano scenario where relaxing ``required: true`` on a bronze
        contract was previously a silent no-op against the warehouse,
        forcing operators to DROP+CREATE the table."""
        prov = _provider()
        with _patched_connection() as conn:
            table_action.alter_table(
                _action([{"kind": "set_nullable", "name": "RATING_STATUS", "nullable": True}]),
                prov,
            )
        sql = _executed_sql(conn)
        assert "ALTER COLUMN" in sql
        assert "DROP NOT NULL" in sql
        assert "RATING_STATUS" in sql.upper()

    def test_set_nullable_false_emits_set_not_null(self) -> None:
        """The inverse: tightening a column to NOT NULL emits ``SET
        NOT NULL``. Snowflake will reject the ALTER if any existing
        row has a NULL there, which is correct strict-by-default
        semantics — the planner / operator owns choosing when this is
        safe."""
        prov = _provider()
        with _patched_connection() as conn:
            table_action.alter_table(
                _action([{"kind": "set_nullable", "name": "USAGE_ID", "nullable": False}]),
                prov,
            )
        sql = _executed_sql(conn)
        assert "ALTER COLUMN" in sql
        assert "SET NOT NULL" in sql
        assert "USAGE_ID" in sql.upper()


class TestAlterTableTypeSafety:
    """ADD COLUMN routes the column type through the validate_sql_type allowlist."""

    def test_injection_laden_column_type_raises(self) -> None:
        prov = _provider()
        with _patched_connection():
            with pytest.raises(SqlTypeError):
                table_action.alter_table(
                    _action([{"kind": "add_column", "name": "x", "type": _INJECTION_TYPE}]),
                    prov,
                )

    def test_injection_type_never_reaches_execute(self) -> None:
        prov = _provider()
        with _patched_connection() as conn:
            with pytest.raises(SqlTypeError):
                table_action.alter_table(
                    _action([{"kind": "add_column", "name": "x", "type": _INJECTION_TYPE}]),
                    prov,
                )
        assert "DROP TABLE" not in _executed_sql(conn)
