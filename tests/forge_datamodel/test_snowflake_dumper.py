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

"""Tests for :mod:`fluid_build.forge_datamodel.from_ddl.snowflake_dumper`.

We mock out ``snowflake.connector`` so the test runs on machines
without the driver installed (matching the soft-import contract). The
assertions cover:

* Input validation (empty database / schema).
* GET_DDL query shape for a whole-schema dump.
* GET_DDL query shape for a per-table dump, including uppercase folding.
* File-writing wrapper produces a self-describing header + the DDL body.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _FakeCursor:
    """In-memory cursor that records executed SQL and replays canned rows."""

    def __init__(self, rows_by_pattern: list[tuple[str, str]]) -> None:
        # rows_by_pattern: list of (substring_match, ddl_payload) pairs;
        # first match wins. This keeps the test readable when multiple
        # GET_DDL calls are expected.
        self._rows_by_pattern = rows_by_pattern
        self.executed: list[str] = []
        self.params: list[tuple[object, ...]] = []
        self._current: str | None = None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append(sql)
        bound = tuple(params or ())
        self.params.append(bound)
        self._current = " ".join([sql, *[str(value) for value in bound]])

    def fetchone(self) -> tuple[str] | None:
        assert self._current is not None
        for pattern, payload in self._rows_by_pattern:
            if pattern in self._current:
                return (payload,)
        return (None,)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _install_fake_connector(monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor) -> MagicMock:
    """Patch :func:`snowflake_dumper._import_connector` + connection params."""
    fake_connector = MagicMock()
    fake_connector.connect = MagicMock(return_value=_FakeConnection(cursor))
    import fluid_build.forge_datamodel.from_ddl.snowflake_dumper as mod

    monkeypatch.setattr(mod, "_import_connector", lambda: fake_connector)
    # Short-circuit credential resolution — the real path reads env vars
    # and keyring; the test doesn't need that surface.
    import fluid_build.providers.snowflake.util.config as cfg

    monkeypatch.setattr(
        cfg,
        "get_connection_params",
        lambda **kwargs: {"account": "ACCT", "user": "U", "warehouse": "WH"},
    )
    return fake_connector


def test_dump_schema_ddl_requires_database_and_schema() -> None:
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import dump_schema_ddl

    with pytest.raises(ValueError):
        dump_schema_ddl("", "SEEDED")
    with pytest.raises(ValueError):
        dump_schema_ddl("BIZ_LAB", "")


def test_dump_schema_ddl_whole_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import dump_schema_ddl

    ddl_payload = (
        "CREATE TABLE BIZ_LAB.SEEDED.PARTY (PARTY_ID NUMBER);\n"
        "CREATE TABLE BIZ_LAB.SEEDED.SERVICE (SERVICE_ID NUMBER);\n"
    )
    cursor = _FakeCursor(rows_by_pattern=[("SCHEMA", ddl_payload)])
    _install_fake_connector(monkeypatch, cursor)

    result = dump_schema_ddl("biz_lab", "seeded")

    assert result.database == "BIZ_LAB"
    assert result.schema == "SEEDED"
    assert "PARTY" in result.ddl
    # GET_DDL for a whole schema counts CREATE TABLE occurrences.
    assert result.table_count == 2
    # Exactly one SQL call for the whole-schema path.
    assert len(cursor.executed) == 1
    assert cursor.executed[0] == "SELECT GET_DDL(%s, %s, TRUE)"
    assert cursor.params[0] == ("SCHEMA", '"BIZ_LAB"."SEEDED"')


def test_dump_schema_ddl_per_table_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import dump_schema_ddl

    cursor = _FakeCursor(
        rows_by_pattern=[
            ("PARTY", "CREATE TABLE BIZ_LAB.SEEDED.PARTY (PARTY_ID NUMBER);"),
            ("SERVICE", "CREATE TABLE BIZ_LAB.SEEDED.SERVICE (SERVICE_ID NUMBER);"),
        ]
    )
    _install_fake_connector(monkeypatch, cursor)

    result = dump_schema_ddl(
        "biz_lab",
        "seeded",
        tables=["party", "service"],
    )

    assert result.table_count == 2
    assert len(cursor.executed) == 2
    for sql in cursor.executed:
        assert sql == "SELECT GET_DDL(%s, %s, TRUE)"
    # Identifiers must be uppercased + double-quoted and passed as bound
    # parameters rather than interpolated into SQL text.
    assert cursor.params == [
        ("TABLE", '"BIZ_LAB"."SEEDED"."PARTY"'),
        ("TABLE", '"BIZ_LAB"."SEEDED"."SERVICE"'),
    ]


def test_dump_schema_ddl_rejects_unsafe_identifiers() -> None:
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import dump_schema_ddl

    with pytest.raises(ValueError, match="Invalid Snowflake database identifier"):
        dump_schema_ddl("BIZ_LAB'; select current_user(); --", "SEEDED")
    with pytest.raises(ValueError, match="Invalid Snowflake table identifier"):
        dump_schema_ddl("BIZ_LAB", "SEEDED", tables=["PARTY'; drop table X; --"])


def test_dump_schema_ddl_accepts_quoted_identifiers_with_bound_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import dump_schema_ddl

    cursor = _FakeCursor(
        rows_by_pattern=[
            (
                '"Biz Lab"."Seeded Schema"."Party Table"',
                'CREATE TABLE "Biz Lab"."Seeded Schema"."Party Table" (PARTY_ID NUMBER);',
            )
        ]
    )
    _install_fake_connector(monkeypatch, cursor)

    result = dump_schema_ddl('"Biz Lab"', '"Seeded Schema"', tables=['"Party Table"'])

    assert result.database == "Biz Lab"
    assert result.schema == "Seeded Schema"
    assert cursor.executed == ["SELECT GET_DDL(%s, %s, TRUE)"]
    assert cursor.params == [("TABLE", '"Biz Lab"."Seeded Schema"."Party Table"')]


def test_dump_schema_ddl_to_file_writes_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import (
        dump_schema_ddl_to_file,
    )

    ddl_payload = "CREATE TABLE BIZ_LAB.SEEDED.PARTY (PARTY_ID NUMBER);"
    cursor = _FakeCursor(rows_by_pattern=[("SCHEMA", ddl_payload)])
    _install_fake_connector(monkeypatch, cursor)

    output = tmp_path / "dump" / "biz_lab.sql"
    dump_schema_ddl_to_file("BIZ_LAB", "SEEDED", output)

    text = output.read_text(encoding="utf-8")
    assert text.startswith("-- Dumped by fluid forge data-model dump-ddl")
    assert "BIZ_LAB.SEEDED" in text
    assert ddl_payload in text


def test_dump_schema_ddl_empty_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.copilot.agents.errors import (
        DDLGenerationError,
        FluidGenerationError,
    )
    from fluid_build.forge_datamodel.from_ddl.snowflake_dumper import dump_schema_ddl

    # Cursor returns empty payload — simulates missing privileges / empty schema.
    cursor = _FakeCursor(rows_by_pattern=[("SCHEMA", "")])
    _install_fake_connector(monkeypatch, cursor)

    # V1.3.4: empty-DDL failures now raise the typed
    # ``DDLGenerationError`` so callers can branch on failure class
    # rather than string-matching exception messages. The plan-named
    # ``FluidGenerationError`` parent still catches it for legacy
    # handlers, so we belt-and-brace the type check below.
    with pytest.raises(DDLGenerationError, match="GET_DDL returned empty") as exc_info:
        dump_schema_ddl("BIZ_LAB", "EMPTY")
    assert isinstance(exc_info.value, FluidGenerationError)
