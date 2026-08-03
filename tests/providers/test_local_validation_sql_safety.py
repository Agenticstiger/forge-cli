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

"""SQL-injection regression tests for the local (DuckDB) validation provider.

Finding: ``get_resource_schema`` and ``run_quality_checks`` built
``table_ref = f'"{schema}"."{table}"'`` straight from
``binding.location.{schema,table}`` with no sanitization, while the sibling
parquet/csv branches route the path through ``quote_string_literal``. A
double-quote-bearing table name closed the identifier quote and let a
``COPY ... TO`` / ``ATTACH`` statement run. The fix routes both identifiers
through the central ``_sql_safety.validate_ident`` allowlist and fails closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.providers.local_validation import LocalValidationProvider

pytestmark = pytest.mark.unit


# A table name that, unsanitized, breaks out of the ``"..."`` identifier quote.
# The historical exploit appended ``ATTACH``/``COPY ... TO`` to exfiltrate
# another on-disk DB; we only need the quote-break to prove the guard fires.
MALICIOUS_TABLE = "orders\" ; ATTACH 'evil.db' AS e; COPY (SELECT 1) TO 'x.csv'; --"
MALICIOUS_SCHEMA = 'main" ; DROP TABLE secrets; --'


class _SpyConnection:
    """A connection whose ``execute`` fails the test if it is ever reached.

    Reaching ``execute`` means a query was built and run with the (malicious)
    identifier — exactly the regression we are guarding against.
    """

    def execute(self, *_args: Any, **_kwargs: Any):  # noqa: ANN401
        raise AssertionError(
            "conn.execute() was reached — a malicious identifier flowed into a "
            "query instead of being rejected (fail-open regression)"
        )

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class _ExplodingDuckDB:
    """Stand-in duckdb module.

    The harmless first ``connect(":memory:")`` is allowed (it carries no
    user-controlled identifier) but returns a connection whose ``execute``
    explodes, and opening any *on-disk* path is itself an immediate failure —
    the on-disk DB must never be opened for a malicious identifier. Either
    breach fails the test.
    """

    @staticmethod
    def connect(target: Any = ":memory:", *_args: Any, **_kwargs: Any):  # noqa: ANN401
        if target != ":memory:":
            raise AssertionError(
                f"duckdb.connect({target!r}) opened an on-disk DB — a malicious "
                "identifier was NOT rejected before the DB was opened "
                "(fail-open regression)"
            )
        return _SpyConnection()


def _duckdb_resource_spec(tmp_path: Path, *, schema: str, table: str) -> Dict[str, Any]:
    """A resource spec pointing at a (real) ``.duckdb`` file path.

    The file is created so the ``path.exists()`` pre-check passes and execution
    proceeds to the identifier-handling code under test.
    """
    db_path = tmp_path / "data.duckdb"
    db_path.write_bytes(b"")  # presence is enough; we never open it on the bad path
    return {
        "id": "bronze.orders",
        "binding": {
            "platform": "local",
            "location": {"path": str(db_path), "schema": schema, "table": table},
        },
    }


# ── get_resource_schema (schema-introspection branch) ───────────────────


class TestGetResourceSchemaInjection:
    def test_malicious_table_never_opens_db(self, tmp_path, monkeypatch):
        provider = LocalValidationProvider({"base_dir": str(tmp_path)})
        # Force any DB access to fail loudly — the guard must run first.
        monkeypatch.setattr(provider, "_get_duckdb", lambda: _ExplodingDuckDB())
        spec = _duckdb_resource_spec(tmp_path, schema="main", table=MALICIOUS_TABLE)

        # The outer handler re-raises validation failures as a clean Exception;
        # the point is that NO query executed (the _ExplodingDuckDB would have
        # raised AssertionError instead, failing the test) and we fail closed.
        with pytest.raises(Exception) as exc_info:
            provider.get_resource_schema(spec)
        assert not isinstance(exc_info.value, AssertionError)
        assert "Invalid SQL identifier" in str(exc_info.value)

    def test_malicious_schema_never_opens_db(self, tmp_path, monkeypatch):
        provider = LocalValidationProvider({"base_dir": str(tmp_path)})
        monkeypatch.setattr(provider, "_get_duckdb", lambda: _ExplodingDuckDB())
        spec = _duckdb_resource_spec(tmp_path, schema=MALICIOUS_SCHEMA, table="orders")

        with pytest.raises(Exception) as exc_info:
            provider.get_resource_schema(spec)
        assert not isinstance(exc_info.value, AssertionError)
        assert "Invalid SQL identifier" in str(exc_info.value)

    def test_normal_table_introspects_successfully(self, tmp_path):
        """Positive path: a legitimate schema/table introspects against a real
        DuckDB file (proves the guard does not break normal use)."""
        duckdb = pytest.importorskip("duckdb")

        db_path = tmp_path / "data.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute("CREATE SCHEMA IF NOT EXISTS main")
        con.execute("CREATE TABLE main.orders (id BIGINT, name VARCHAR)")
        con.execute("INSERT INTO main.orders VALUES (1, 'a'), (2, 'b')")
        con.close()

        provider = LocalValidationProvider({"base_dir": str(tmp_path)})
        spec = {
            "id": "bronze.orders",
            "binding": {
                "platform": "local",
                "location": {"path": str(db_path), "schema": "main", "table": "orders"},
            },
        }
        schema = provider.get_resource_schema(spec)
        assert schema is not None
        assert {f.name for f in schema.fields} == {"id", "name"}
        assert schema.row_count == 2


# ── run_quality_checks branch ───────────────────────────────────────────


class TestRunQualityChecksInjection:
    def test_malicious_table_returns_issue_without_query(self, tmp_path, monkeypatch):
        provider = LocalValidationProvider({"base_dir": str(tmp_path)})
        monkeypatch.setattr(provider, "_get_duckdb", lambda: _ExplodingDuckDB())
        spec = _duckdb_resource_spec(tmp_path, schema="main", table=MALICIOUS_TABLE)

        # run_quality_checks fails closed by returning an error ValidationIssue
        # (its established failure convention) — NOT by executing a query.
        issues = provider.run_quality_checks(spec, rules=[{"type": "not_null", "column": "id"}])
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].category == "quality"

    def test_malicious_schema_returns_issue_without_query(self, tmp_path, monkeypatch):
        provider = LocalValidationProvider({"base_dir": str(tmp_path)})
        monkeypatch.setattr(provider, "_get_duckdb", lambda: _ExplodingDuckDB())
        spec = _duckdb_resource_spec(tmp_path, schema=MALICIOUS_SCHEMA, table="orders")

        issues = provider.run_quality_checks(spec, rules=[{"type": "not_null", "column": "id"}])
        assert len(issues) == 1
        assert issues[0].severity == "error"

    def test_normal_table_runs_quality_checks(self, tmp_path):
        """Positive path: a legitimate identifier runs a real DQ rule."""
        duckdb = pytest.importorskip("duckdb")

        db_path = tmp_path / "data.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute("CREATE TABLE main.orders (id BIGINT)")
        con.execute("INSERT INTO main.orders VALUES (1), (2), (3)")
        con.close()

        provider = LocalValidationProvider({"base_dir": str(tmp_path)})
        spec = {
            "id": "bronze.orders",
            "binding": {
                "platform": "local",
                "location": {"path": str(db_path), "schema": "main", "table": "orders"},
            },
        }
        issues = provider.run_quality_checks(spec, rules=[{"type": "not_null", "column": "id"}])
        # not_null on a fully-populated column → no error issues raised.
        assert [i for i in issues if i.severity == "error"] == []
