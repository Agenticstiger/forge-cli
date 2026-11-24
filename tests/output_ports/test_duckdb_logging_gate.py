# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Caller-supplied SQL may not use DuckDB's logging functions.

On DuckDB 1.5.x ``enable_logging(storage='file')`` with no path makes the process
abort at exit (SIGABRT), and ``lock_configuration`` does not stop the table
function. Each gate that admits caller SQL refuses these names by name.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers
from fluid_build.output_ports.mcp.query_compiler import QueryValidationError, compile_free_form_sql
from fluid_build.providers._sql_safety import (
    DUCKDB_LOG_RELATIONS,
    DUCKDB_REFUSED_FUNCTIONS,
    validate_sql_expression_allowlist,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _statements():
    out = ["SELECT * FROM enable_logging(level='debug', storage='file')"]
    for name in sorted(DUCKDB_REFUSED_FUNCTIONS):
        for ref in (
            f"{name}()",
            f"{name.upper()}()",
            f'"{name.upper()}"()',
            f"system.main.{name}()",
            f"{name} ()",
            f'"{name}" ()',
            f"`{name}`()",
            f"system.main.`{name.upper()}` ()",
        ):
            out.append(f"SELECT * FROM {ref}")
    for name in sorted(DUCKDB_LOG_RELATIONS):
        for ref in (
            f"{name}()",
            f"{name}",
            f'"{name}"',
            f"main.{name}",
            f'system.main."{name}"',
        ):
            out.append(f"SELECT * FROM {ref}")
    out.append("SELECT (SELECT count(*) FROM duckdb_logs) AS n")
    # A statement given as text never shows the name it runs to a text check.
    out.append("SELECT * FROM query('SEL' || 'ECT * FROM enable_' || 'logging()')")
    out.append("SELECT * FROM query_table('duckdb_' || 'logs')")
    out.append("SELECT * FROM histogram_values('duckdb_' || 'logs', 'type')")
    out.append("SELECT * FROM `query`('SEL' || 'ECT * FROM duckdb_' || 'logs')")
    out.append("SELECT write_log('x')")
    return out


STATEMENTS = _statements()


def _compile(sql: str) -> None:
    compile_free_form_sql(sql=sql, table_reference="analytics.events", limit=10)


def _validate(sql: str) -> None:
    validate_sql_expression_allowlist(sql.split(None, 1)[1])


def _guard(sql: str) -> None:
    guard_against_injection_markers(sql)


GATES = {
    "compile_free_form_sql": (_compile, QueryValidationError),
    "validate_sql_expression_allowlist": (_validate, ValueError),
    "guard_against_injection_markers": (_guard, ValueError),
}


@pytest.mark.parametrize("sql", STATEMENTS)
@pytest.mark.parametrize("gate", sorted(GATES))
def test_gate_refuses_logging_names(gate, sql):
    call, error = GATES[gate]
    with pytest.raises(error):
        call(sql)


@pytest.mark.parametrize("gate", sorted(GATES))
def test_gate_still_accepts_an_ordinary_select(gate):
    call, _ = GATES[gate]
    call("SELECT id, amount FROM analytics.events WHERE amount > 1")


@pytest.mark.parametrize("gate", sorted(GATES))
def test_gate_accepts_a_column_that_only_shares_a_name(gate):
    call, _ = GATES[gate]
    call(
        "SELECT enable_logging, disable_logging, query, write_log, histogram_values "
        "FROM analytics.events "
        "WHERE enable_logging = 1"
    )


def test_query_compiler_message_names_the_fix():
    with pytest.raises(QueryValidationError, match="logging functions"):
        _compile("SELECT * FROM enable_logging()")


_CHILD = r"""
import json, sys
from fluid_build.providers._duckdb_sandbox import DuckDBAllowlist, secure_duckdb_connect
from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers
from fluid_build.output_ports.mcp.query_compiler import QueryValidationError, compile_free_form_sql
from fluid_build.providers._sql_safety import validate_sql_expression_allowlist

con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
unrefused = []
for sql in json.loads(sys.argv[1]):
    for gate, call, error in (
        ("compile", lambda s: compile_free_form_sql(sql=s, table_reference="t", limit=5),
         QueryValidationError),
        ("validate", lambda s: validate_sql_expression_allowlist(s.split(None, 1)[1]), ValueError),
        ("guard", guard_against_injection_markers, ValueError),
    ):
        try:
            call(sql)
        except error:
            continue
        unrefused.append([gate, sql])
# Only statements every gate refused get this far; the connection runs a plain one.
con.execute("SELECT 1").fetchall()
con.close()
print(json.dumps(unrefused))
"""


def test_connection_survives_after_every_gate_refuses_logging_statements():
    import duckdb

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, json.dumps(STATEMENTS)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "", "HOME": str(REPO_ROOT)},
        timeout=120,
    )
    assert (
        proc.returncode >= 0
    ), f"killed by signal {-proc.returncode} on duckdb {duckdb.__version__}"
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
