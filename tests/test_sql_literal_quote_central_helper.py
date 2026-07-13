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

"""Regression: SQL string-literal sites route through the central chokepoint.

CLAUDE.md invariant: every DDL f-string routes string literals through
``providers._sql_safety.quote_string_literal`` (single-quote doubling).
Two documented regressions diverge from it:

* ``repr()`` — the *bug* fixed here. ``repr("O'brien")`` emits
  ``"O'brien"`` (DOUBLE quotes), which DuckDB parses as an *identifier*,
  not a string literal, so any path/option value carrying a single quote
  breaks the ``read_csv_auto(...)`` load outright.
* inline ``.replace("'", "''")`` — functionally correct single-quote
  doubling, but a maintenance divergence from the one helper (and it
  breaks under non-default Snowflake escape settings).

This module pins all four sites the lane consolidated:

1. ``providers/local/local.py::_register_one`` (the ``repr()`` bug — a
   value with a single quote must produce a *single-quote-doubled literal*,
   never a repr double-quoted identifier).
2. ``output_ports/iam_compiler.py`` — Snowflake RAP / Postgres RLS / AWS
   Lake Formation constant-equality predicates.
3. ``build_runners/meltano/runner.py::_sql_literal`` — untrusted Singer
   record values feeding a ``CREATE TABLE AS SELECT ... VALUES``.
4. ``output_ports/mcp/drivers/duckdb.py::_configure_connection``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from fluid_build.providers._sql_safety import quote_string_literal

pytestmark = pytest.mark.unit

# A path fragment carrying a single quote — the canonical trigger. Under the
# old ``repr()`` path this became a DOUBLE-quoted DuckDB identifier and the
# load broke; the fix must emit a single-quote-doubled string literal.
APOS = "o'brien"


class _SpyConnection:
    """Captures every SQL string handed to ``execute`` for assertion."""

    def __init__(self) -> None:
        self.executed: List[str] = []

    def execute(self, sql: str, *args: Any, **kwargs: Any):  # noqa: ANN401
        self.executed.append(sql)
        return self

    def close(self) -> None:  # pragma: no cover - trivial
        pass


# ── Site 1: local provider _register_one (the repr() bug) ────────────────


class TestLocalRegisterOneLiteral:
    def _register(self, path: Path, fmt: str, options=None):
        from fluid_build.providers.local.local import LocalProvider

        con = _SpyConnection()
        # A glob path skips the on-disk ``exists()`` pre-check so we can pin
        # the emitted SQL without touching the filesystem.
        LocalProvider()._register_one(con, "t", path, fmt, options)
        assert len(con.executed) == 1
        return con.executed[0]

    def test_csv_path_with_quote_is_single_quote_doubled(self):
        path = Path(f"/tmp/{APOS}/*.csv")
        sql = self._register(path, "csv")
        # The path must appear as a single-quote-doubled string literal …
        assert quote_string_literal(str(path)) in sql
        assert "o''brien" in sql
        # … and NEVER as the old repr() double-quoted identifier form.
        assert repr(str(path)) not in sql
        assert f'"{path}"' not in sql

    def test_parquet_path_with_quote_is_single_quote_doubled(self):
        path = Path(f"/tmp/{APOS}/*.parquet")
        sql = self._register(path, "parquet")
        assert "read_parquet(" in sql
        assert quote_string_literal(str(path)) in sql
        assert repr(str(path)) not in sql

    def test_fallback_path_with_quote_is_single_quote_doubled(self):
        # An unknown extension falls through to the read_csv_auto fallback.
        path = Path(f"/tmp/{APOS}/*.dat")
        sql = self._register(path, "unknownfmt")
        assert "read_csv_auto(" in sql
        assert quote_string_literal(str(path)) in sql
        assert repr(str(path)) not in sql

    def test_string_option_value_with_quote_is_single_quote_doubled(self):
        # A string CSV option value carrying a quote used repr() too.
        path = Path("/tmp/plain/*.csv")
        sql = self._register(path, "csv", options={"nullstr": "O'Brien"})
        assert "O''Brien" in sql
        # The old repr() form of the option value must be gone …
        assert repr("O'Brien") not in sql
        # … and the option KEY stays an unquoted DuckDB parameter name.
        assert "NULLSTR:=" in sql

    def test_malicious_option_key_is_rejected(self):
        # An option KEY becomes a DuckDB named-parameter name interpolated
        # into ``read_csv_auto(... KEY:=value ...)``. ``options`` is
        # contract-supplied (``item.get("options")``), so a key carrying DDL
        # must be rejected by ``validate_ident`` BEFORE any SQL reaches the
        # connection — never interpolated raw. (Sibling to the value-literal
        # hardening: the value routes through ``quote_string_literal``; the
        # key must route through ``validate_ident``.)
        from fluid_build.providers.local.local import LocalProvider

        con = _SpyConnection()
        with pytest.raises(ValueError):
            LocalProvider()._register_one(
                con, "t", Path("/tmp/plain/*.csv"), "csv", {"x); DROP TABLE t; --": "1"}
            )
        # The injection never reached the connection.
        assert con.executed == []

    def test_benign_custom_option_key_passes_through(self):
        # A legitimate custom option key survives as an unquoted DuckDB
        # parameter name (``validate_ident`` returns simple identifiers
        # unchanged), so the happy path is unaffected by the key guard.
        path = Path("/tmp/plain/*.csv")
        sql = self._register(path, "csv", options={"sample_size": 1000})
        assert "SAMPLE_SIZE:=1000" in sql

    def test_benign_path_is_not_over_escaped(self):
        # Negative assertion: a quote-free path yields a clean literal with
        # no spurious quote-doubling artifacts. A glob path skips the on-disk
        # ``exists()`` pre-check.
        path = Path("/data/plain/*.csv")
        sql = self._register(path, "csv")
        assert "'/data/plain/*.csv'" in sql
        assert "''" not in sql

    def test_live_real_duckdb_loads_quote_bearing_path(self, tmp_path):
        """End-to-end: a real DuckDB engine loads a CSV whose directory name
        contains a single quote. Under the old repr() path this raised because
        ``read_csv_auto("…o'brien…")`` was parsed as an identifier."""
        duckdb = pytest.importorskip("duckdb")
        from fluid_build.providers.local.local import LocalProvider

        data_dir = tmp_path / APOS
        data_dir.mkdir()
        csv_path = data_dir / "data.csv"
        csv_path.write_text("id,name\n1,alice\n2,bob\n", encoding="utf-8")

        con = duckdb.connect(database=":memory:")
        try:
            LocalProvider()._register_one(con, "t", csv_path, "csv", None)
            rows = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            assert rows == 2
        finally:
            con.close()


# ── Site 2: iam_compiler constant-equality predicates ────────────────────


class TestIamCompilerLiteral:
    @staticmethod
    def _contract(binding, value):
        expose = {
            "exposeId": "demo",
            "binding": binding,
            "policy": {
                "agentPolicy": {},
                "rowFilters": [{"column": "region", "equals": value}],
            },
        }
        return {
            "fluidVersion": "0.7.4",
            "kind": "DataProduct",
            "id": "test.iam",
            "exposes": [expose],
        }

    def _sql(self, target, binding, value):
        from fluid_build.output_ports.iam_compiler import compile_agent_policy_to_iam

        compiled = compile_agent_policy_to_iam(
            contract=self._contract(binding, value), target=target
        )
        return compiled[0].sql

    _SNOWFLAKE = {
        "platform": "snowflake",
        "format": "snowflake_table",
        "location": {"database": "PROD", "schema": "T", "table": "C"},
    }
    _POSTGRES = {
        "platform": "postgres",
        "format": "postgres_table",
        "location": {"database": "appdb", "schema": "t", "table": "c"},
    }
    _AWS = {
        "platform": "aws",
        "format": "athena_table",
        "location": {"database": "analytics", "table": "events"},
    }

    def test_snowflake_rap_quotes_literal(self):
        sql = self._sql("snowflake", self._SNOWFLAKE, APOS)
        assert f"= {quote_string_literal(APOS)}" in sql
        assert "= 'o''brien'" in sql

    def test_postgres_rls_quotes_literal(self):
        sql = self._sql("postgres", self._POSTGRES, APOS)
        assert f"= {quote_string_literal(APOS)}" in sql
        assert "= 'o''brien'" in sql

    def test_aws_lake_formation_quotes_literal(self):
        sql = self._sql("aws", self._AWS, APOS)
        assert "o''brien" in sql

    def test_benign_value_not_over_escaped(self):
        sql = self._sql("snowflake", self._SNOWFLAKE, "us")
        assert "= 'us'" in sql
        assert "''" not in sql


# ── Site 3: meltano _sql_literal ─────────────────────────────────────────


class TestMeltanoSqlLiteral:
    def test_string_with_quote_is_single_quote_doubled(self):
        from fluid_build.build_runners.meltano.runner import _sql_literal

        assert _sql_literal("O'Brien") == "'O''Brien'"

    def test_benign_string_is_plain_literal(self):
        from fluid_build.build_runners.meltano.runner import _sql_literal

        assert _sql_literal("plain") == "'plain'"

    def test_non_string_branches_unchanged(self):
        from fluid_build.build_runners.meltano.runner import _sql_literal

        assert _sql_literal(None) == "NULL"
        assert _sql_literal(True) == "TRUE"
        assert _sql_literal(False) == "FALSE"
        assert _sql_literal(5) == "5"

    def test_live_ctas_round_trips_quote_bearing_value(self, tmp_path):
        """Untrusted Singer record value with a single quote must survive the
        CREATE TABLE AS SELECT ... VALUES round-trip on a real DuckDB."""
        duckdb = pytest.importorskip("duckdb")
        from fluid_build.build_runners.meltano.runner import write_records_to_duckdb

        out = tmp_path / "x.duckdb"
        records = {"orders": [{"id": 1, "note": "O'Brien & co"}]}
        # Default dataset is ``bronze`` → table is ``bronze.orders``.
        counts = write_records_to_duckdb(records, duckdb_path=out)
        assert counts["orders"] == 1

        con = duckdb.connect(str(out))
        try:
            value = con.execute("SELECT note FROM bronze.orders WHERE id = 1").fetchone()[0]
            assert value == "O'Brien & co"
        finally:
            con.close()


# ── Site 4: duckdb MCP driver _configure_connection ──────────────────────


class TestDuckdbDriverLiteral:
    def _driver_for(self, csv_path: Path):
        from fluid_build.output_ports.mcp.drivers import build_driver
        from tests.output_ports._fixtures import make_expose

        expose = make_expose(
            binding={
                "platform": "local",
                "format": "csv",
                "location": {"path": str(csv_path), "table": "t"},
            },
        )
        return build_driver(expose=expose, contract={"exposes": [expose]})

    def test_configure_connection_quotes_path_literal(self, tmp_path):
        csv_path = tmp_path / APOS / "data.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text("id\n1\n", encoding="utf-8")

        driver = self._driver_for(csv_path)
        con = _SpyConnection()
        driver._configure_connection(con)
        assert len(con.executed) == 1
        sql = con.executed[0]
        assert quote_string_literal(csv_path.as_posix()) in sql
        assert "o''brien" in sql
        # No repr / raw double-quoted identifier form for the path literal.
        assert f'"{csv_path.as_posix()}"' not in sql

    def test_live_real_duckdb_samples_quote_bearing_path(self, tmp_path):
        pytest.importorskip("duckdb")
        csv_path = tmp_path / APOS / "data.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text("id,name\n1,alice\n2,bob\n", encoding="utf-8")

        driver = self._driver_for(csv_path)
        result = driver.sample(limit=5)
        assert len(result.rows) == 2
