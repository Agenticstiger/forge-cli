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

# tests/providers/test_sql_type_safety.py
"""SQL data-type / language allowlisting for the Snowflake DDL surfaces.

Covers BUG-SQL-TYPE — column ``type`` strings and procedure/UDF
``param.type`` / ``return_type`` / ``language`` were interpolated raw into
``CREATE TABLE`` / ``CREATE PROCEDURE`` / ``CREATE FUNCTION`` DDL f-strings.

The allowlist lives in ``fluid_build.providers._sql_safety``:

* :func:`validate_sql_type` — base-type allowlist + parameterised suffix
* :func:`validate_sql_language` — procedure/UDF language allowlist
* :func:`validate_sql_type_param_payload` — bare ``(N[,N])`` payload check

Wired into ``actions/{table,procedure,udf,share}.py`` and the
``util/types.map_fluid_type_to_snowflake`` parameterised passthrough.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers._sql_safety import (
    SqlTypeError,
    validate_sql_language,
    validate_sql_type,
    validate_sql_type_param_payload,
)
from fluid_build.providers.snowflake.actions import procedure as procedure_action
from fluid_build.providers.snowflake.actions import share as share_action
from fluid_build.providers.snowflake.actions import table as table_action
from fluid_build.providers.snowflake.actions import udf as udf_action
from fluid_build.providers.snowflake.util.types import map_fluid_type_to_snowflake

pytestmark = pytest.mark.unit


# A representative injection payload reused across surfaces — the canonical
# "break out of the type slot and append DDL" string from the bug report.
_INJECTION_TYPE = "decimal(18); DROP TABLE victims; CREATE TABLE r (a INT"


# ── Helpers ────────────────────────────────────────────────────────────


def _provider() -> MagicMock:
    """A provider double exposing only what the action functions touch."""
    prov = MagicMock()
    prov.warehouse = "WH"
    prov._kwargs = {}
    return prov


@contextmanager
def _patched_connection(module):
    """Patch ``SnowflakeConnection`` + ``get_connection_params`` on a module.

    Yields the connection mock so a test can assert on the SQL handed to
    ``execute`` without a live warehouse.
    """
    conn = MagicMock()
    # ``execute`` is also used for the table-existence probe — return an empty
    # result so ``ensure_table`` takes the create path.
    conn.execute.return_value = []
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    with (
        patch.object(module, "SnowflakeConnection", return_value=ctx),
        patch.object(module, "get_connection_params", return_value={}),
    ):
        yield conn


# ── validate_sql_type — direct unit coverage ───────────────────────────


class TestValidateSqlType:
    """The base-type allowlist + parameterised-suffix check."""

    @pytest.mark.parametrize(
        "type_str",
        [
            # Bare base types.
            "NUMBER",
            "DECIMAL",
            "NUMERIC",
            "INT",
            "INTEGER",
            "BIGINT",
            "SMALLINT",
            "FLOAT",
            "DOUBLE",
            "VARCHAR",
            "CHAR",
            "STRING",
            "TEXT",
            "BOOLEAN",
            "DATE",
            "TIME",
            "TIMESTAMP",
            "TIMESTAMP_NTZ",
            "TIMESTAMP_TZ",
            "TIMESTAMP_LTZ",
            "VARIANT",
            "OBJECT",
            "ARRAY",
            "GEOGRAPHY",
            "GEOMETRY",
            "BINARY",
            # Multi-word base types.
            "DOUBLE PRECISION",
            # Parameterised suffixes.
            "NUMBER(38,0)",
            "DECIMAL(18,4)",
            "VARCHAR(255)",
            "CHAR(1)",
            "NUMBER(38, 10)",  # whitespace inside the suffix is tolerated
            # Case / whitespace insensitivity.
            "varchar(100)",
            "Decimal(12,2)",
            "  NUMBER  ",
        ],
    )
    def test_accepts_valid_types(self, type_str):
        # The validated string is returned UNCHANGED (caller controls casing).
        assert validate_sql_type(type_str) == type_str

    @pytest.mark.parametrize(
        "payload",
        [
            # The canonical injection from the bug report.
            _INJECTION_TYPE,
            # Statement-terminator break-outs.
            "INT; DROP TABLE x",
            "VARCHAR(100); --",
            "NUMBER(38,0); DELETE FROM t",
            "DECIMAL(18,4) /* comment */",
            # Unknown / bogus base types.
            "evil_type",
            "BLOB",
            "CLOB",
            "ENUM",
            "MY_UDT",
            # Malformed parameter payloads.
            "NUMBER(a,b)",
            "VARCHAR(100, 200, 300)",
            "DECIMAL(18,4,9)",
            "NUMBER()",
            "NUMBER(-1)",
            "VARCHAR(10",  # unbalanced — base 'VARCHAR(10' is not on allowlist
            "NUMBER(38);",
            # Trailing garbage after a valid-looking suffix.
            "NUMBER(38,0) NOT NULL DEFAULT 1",
            # Quoted-string break-out.
            "VARCHAR(100)' OR '1'='1",
            # Empty / whitespace / wrong type.
            "",
            "   ",
        ],
    )
    def test_rejects_injection_and_bogus_types(self, payload):
        with pytest.raises(SqlTypeError):
            validate_sql_type(payload)

    @pytest.mark.parametrize("bad", [None, 123, ["NUMBER"], {"t": "INT"}])
    def test_rejects_non_string(self, bad):
        with pytest.raises(SqlTypeError):
            validate_sql_type(bad)  # type: ignore[arg-type]

    def test_sql_type_error_is_a_value_error(self):
        # Existing ``except ValueError`` handlers around the SQL-safety
        # helpers must keep catching the new failure mode.
        assert issubclass(SqlTypeError, ValueError)

    def test_error_message_carries_the_repr_for_the_author(self):
        with pytest.raises(SqlTypeError) as exc:
            validate_sql_type(_INJECTION_TYPE)
        # The repr is intentionally surfaced so a contract author can fix it,
        # but it is the *input* repr, never an executed-SQL echo.
        assert "DROP TABLE" in str(exc.value)


class TestValidateSqlLanguage:
    """The procedure/UDF language allowlist."""

    @pytest.mark.parametrize(
        "language", ["SQL", "JAVASCRIPT", "PYTHON", "JAVA", "SCALA", "python", "  Java  "]
    )
    def test_accepts_allowlisted_languages(self, language):
        assert validate_sql_language(language) == language

    @pytest.mark.parametrize(
        "bad",
        [
            "SQL; DROP TABLE t",
            "BASH",
            "RUST",
            "PYTHON RUNTIME_VERSION = '3.11'",
            "",
            "   ",
        ],
    )
    def test_rejects_non_allowlisted_languages(self, bad):
        with pytest.raises(SqlTypeError):
            validate_sql_language(bad)

    @pytest.mark.parametrize("bad", [None, 1, ["SQL"]])
    def test_rejects_non_string(self, bad):
        with pytest.raises(SqlTypeError):
            validate_sql_language(bad)  # type: ignore[arg-type]


class TestValidateSqlTypeParamPayload:
    """The bare ``(N[,N])`` payload check used by the type-mapping passthrough."""

    @pytest.mark.parametrize("payload", ["18", "38,0", "18,4", "100", "38, 10", " 12,2 "])
    def test_accepts_digit_payloads(self, payload):
        assert validate_sql_type_param_payload(payload) == payload

    @pytest.mark.parametrize(
        "bad",
        [
            "18); DROP TABLE t; --",
            "a,b",
            "18,4,9",
            "",
            "  ",
            "-1",
            "18;",
        ],
    )
    def test_rejects_non_digit_payloads(self, bad):
        with pytest.raises(SqlTypeError):
            validate_sql_type_param_payload(bad)


# ── util/types — parameterised passthrough is now an injection boundary ─


class TestMapFluidTypePassthroughHardening:
    """``map_fluid_type_to_snowflake`` is the planner's type translator.

    Its parameterised-passthrough branch returns the type verbatim — that is
    the channel through which a malicious ``decimal(...)`` payload would reach
    ``actions/table.py`` and ``conn.execute()``.
    """

    @pytest.mark.parametrize(
        "fluid_type,expected",
        [
            ("decimal(18,4)", "DECIMAL(18,4)"),
            ("number(38,0)", "NUMBER(38,0)"),
            ("varchar(255)", "VARCHAR(255)"),
            ("char(1)", "CHAR(1)"),
            ("numeric(12, 2)", "NUMERIC(12, 2)"),
        ],
    )
    def test_legitimate_parameterised_types_still_pass_through(self, fluid_type, expected):
        assert map_fluid_type_to_snowflake(fluid_type) == expected

    def test_unparameterised_and_unknown_paths_unaffected(self):
        # Regression guard: the non-passthrough branches must keep working.
        assert map_fluid_type_to_snowflake("string") == "VARCHAR"
        assert map_fluid_type_to_snowflake("integer") == "NUMBER(38,0)"
        assert map_fluid_type_to_snowflake("totally_unknown") == "VARCHAR"

    @pytest.mark.parametrize(
        "payload",
        [
            _INJECTION_TYPE,
            "varchar(100) ; DROP TABLE x",
            "decimal(18,4) NOT NULL",
            "number(a,b)",
            "decimal(18,4,9)",
            "varchar(100))",
        ],
    )
    def test_injection_laden_parameterised_type_is_rejected(self, payload):
        with pytest.raises(SqlTypeError):
            map_fluid_type_to_snowflake(payload)


# ── actions/table.py — CREATE TABLE / ALTER TABLE wiring ───────────────


class TestEnsureTableTypeSafety:
    def _action(self, columns: list[dict]) -> dict:
        return {
            "database": "DB",
            "schema": "SCH",
            "table": "T",
            "account": "a",
            "columns": columns,
            "op": "sf.table.ensure",
        }

    def test_legitimate_column_types_build_create_table(self):
        prov = _provider()
        action = self._action(
            [
                {"name": "id", "type": "NUMBER(38,0)", "nullable": False},
                {"name": "amount", "type": "DECIMAL(18,4)"},
                {"name": "label", "type": "VARCHAR(255)"},
            ]
        )
        with _patched_connection(table_action) as conn:
            result = table_action.ensure_table(action, prov)
        assert result["changed"] is True
        # The CREATE TABLE statement is the second execute call (after the
        # existence probe). Confirm every validated type made it in.
        create_sql = "\n".join(str(c.args[0]) for c in conn.execute.call_args_list)
        assert "NUMBER(38,0)" in create_sql
        assert "DECIMAL(18,4)" in create_sql
        assert "VARCHAR(255)" in create_sql

    def test_injection_laden_column_type_is_rejected(self):
        prov = _provider()
        action = self._action([{"name": "id", "type": _INJECTION_TYPE}])
        with _patched_connection(table_action):
            with pytest.raises(SqlTypeError):
                table_action.ensure_table(action, prov)

    def test_injection_column_type_never_reaches_execute(self):
        # The rejection must happen while building the CREATE TABLE DDL — the
        # malicious type must never be handed to ``conn.execute``.
        prov = _provider()
        action = self._action(
            [
                {"name": "ok", "type": "INT"},
                {"name": "evil", "type": _INJECTION_TYPE},
            ]
        )
        with _patched_connection(table_action) as conn:
            with pytest.raises(SqlTypeError):
                table_action.ensure_table(action, prov)
        for call in conn.execute.call_args_list:
            assert "DROP TABLE victims" not in str(call.args[0])

    def test_schema_evolution_add_column_rejects_injection_type(self):
        # When the table already exists, ``ensure_table`` routes through
        # ``_apply_schema_evolution`` which builds ALTER TABLE ... ADD COLUMN
        # DDL — a second raw-type interpolation site, also allowlisted.
        prov = _provider()
        action = self._action([{"name": "evil", "type": _INJECTION_TYPE}])
        conn = MagicMock()
        # Probe 1 — table exists (count > 0). Probe 2 — existing columns is
        # empty, so the desired column is "missing" and the ADD COLUMN path
        # (and its type validation) is exercised.
        conn.execute.side_effect = [[(1,)], []]
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=conn)
        ctx.__exit__ = MagicMock(return_value=False)
        with (
            patch.object(table_action, "SnowflakeConnection", return_value=ctx),
            patch.object(table_action, "get_connection_params", return_value={}),
        ):
            with pytest.raises(SqlTypeError):
                table_action.ensure_table(action, prov)
        for call in conn.execute.call_args_list:
            assert "DROP TABLE victims" not in str(call.args[0])


# ── actions/procedure.py — CREATE PROCEDURE wiring ─────────────────────


class TestEnsureProcedureTypeSafety:
    def _action(self, **over) -> dict:
        base = {
            "database": "DB",
            "schema": "SCH",
            "name": "my_proc",
            "account": "a",
            "language": "SQL",
            "body": "BEGIN RETURN 1; END;",
            "return_type": "NUMBER",
            "parameters": [{"name": "p1", "type": "VARCHAR"}],
            "op": "sf.procedure.ensure",
        }
        base.update(over)
        return base

    def test_legitimate_procedure_builds_ddl(self):
        prov = _provider()
        with _patched_connection(procedure_action) as conn:
            result = procedure_action.ensure_procedure(self._action(), prov)
        assert result["changed"] is True
        emitted = str(conn.execute.call_args.args[0])
        assert "RETURNS NUMBER" in emitted
        assert "LANGUAGE SQL" in emitted
        assert "p1 VARCHAR" in emitted
        # The body is legitimately arbitrary code — it is passed through.
        assert "BEGIN RETURN 1; END;" in emitted

    def test_injection_param_type_is_rejected(self):
        prov = _provider()
        action = self._action(parameters=[{"name": "p1", "type": _INJECTION_TYPE}])
        with _patched_connection(procedure_action) as conn:
            with pytest.raises(SqlTypeError):
                procedure_action.ensure_procedure(action, prov)
        conn.execute.assert_not_called()

    def test_injection_return_type_is_rejected(self):
        prov = _provider()
        action = self._action(return_type="NUMBER; DROP TABLE t")
        with _patched_connection(procedure_action) as conn:
            with pytest.raises(SqlTypeError):
                procedure_action.ensure_procedure(action, prov)
        conn.execute.assert_not_called()

    def test_injection_language_is_rejected(self):
        prov = _provider()
        action = self._action(language="SQL\nAS $$ malicious $$; DROP TABLE t; --")
        with _patched_connection(procedure_action) as conn:
            with pytest.raises(SqlTypeError):
                procedure_action.ensure_procedure(action, prov)
        conn.execute.assert_not_called()


# ── actions/udf.py — CREATE FUNCTION wiring ────────────────────────────


class TestEnsureUdfTypeSafety:
    def _action(self, **over) -> dict:
        base = {
            "database": "DB",
            "schema": "SCH",
            "name": "my_udf",
            "account": "a",
            "language": "PYTHON",
            "body": "def handler(x): return x",
            "return_type": "VARIANT",
            "parameters": [{"name": "x", "type": "NUMBER(38,0)"}],
            "op": "sf.udf.ensure",
        }
        base.update(over)
        return base

    def test_legitimate_udf_builds_ddl(self):
        prov = _provider()
        with _patched_connection(udf_action) as conn:
            result = udf_action.ensure_udf(self._action(), prov)
        assert result["changed"] is True
        emitted = str(conn.execute.call_args.args[0])
        assert "RETURNS VARIANT" in emitted
        assert "LANGUAGE PYTHON" in emitted
        assert "x NUMBER(38,0)" in emitted
        assert "def handler(x): return x" in emitted

    def test_injection_param_type_is_rejected(self):
        prov = _provider()
        action = self._action(parameters=[{"name": "x", "type": _INJECTION_TYPE}])
        with _patched_connection(udf_action) as conn:
            with pytest.raises(SqlTypeError):
                udf_action.ensure_udf(action, prov)
        conn.execute.assert_not_called()

    def test_injection_return_type_is_rejected(self):
        prov = _provider()
        action = self._action(return_type="VARIANT; DROP TABLE t; --")
        with _patched_connection(udf_action) as conn:
            with pytest.raises(SqlTypeError):
                udf_action.ensure_udf(action, prov)
        conn.execute.assert_not_called()

    def test_injection_language_is_rejected(self):
        prov = _provider()
        action = self._action(language="JAVASCRIPT'; DROP TABLE t; --")
        with _patched_connection(udf_action) as conn:
            with pytest.raises(SqlTypeError):
                udf_action.ensure_udf(action, prov)
        conn.execute.assert_not_called()


# ── actions/share.py — ALTER SHARE ... ADD ACCOUNTS wiring ─────────────


class TestEnsureShareAccountSafety:
    def _action(self, accounts: list[str]) -> dict:
        return {
            "name": "MY_SHARE",
            "account": "a",
            "accounts": accounts,
            "op": "sf.share.ensure",
        }

    @pytest.mark.parametrize(
        "account",
        ["CONSUMER_ACCT", "orgname.accountname", "org-account", "ABC12345"],
    )
    def test_legitimate_account_locators_pass(self, account):
        prov = _provider()
        with _patched_connection(share_action) as conn:
            result = share_action.ensure_share(self._action([account]), prov)
        assert result["changed"] is True
        emitted = "\n".join(str(c.args[0]) for c in conn.execute.call_args_list)
        assert f"ADD ACCOUNTS = {account}" in emitted

    @pytest.mark.parametrize(
        "account",
        [
            "CONSUMER; DROP TABLE secrets; --",
            "ACCT' OR '1'='1",
            "ACCT ADD ACCOUNTS = EVIL",
            "",
        ],
    )
    def test_injection_account_locator_is_rejected(self, account):
        prov = _provider()
        with _patched_connection(share_action) as conn:
            with pytest.raises(ValueError):
                share_action.ensure_share(self._action([account]), prov)
        # The CREATE SHARE probe may have run, but no ALTER SHARE with the
        # malicious account must have been emitted.
        for call in conn.execute.call_args_list:
            assert "DROP TABLE secrets" not in str(call.args[0])
