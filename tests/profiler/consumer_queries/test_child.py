# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The locked child: results, the locks that hold inside it, time limits and a dead process."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

pytest.importorskip("duckdb")

from fluid_build.profiler.consumer_queries import (  # noqa: E402
    ChildRun,
    ChildUnavailable,
    Statement,
    StatementResult,
    _child,
    run_in_child,
    runner,
)

MIB = 1 << 20
DAY = 86_400_000

TABLES: Dict[str, List[Any]] = {
    "readings": [
        ("id", "BIGINT", [1, 2, 3, 4, 5, 6]),
        ("station", "VARCHAR", ["a", "b", "a", None, "it's", 'say "hi"']),
        ("read_on", "DATE", [0, DAY, 2 * DAY, None, 10 * DAY, 11 * DAY]),
        ("taken", "TIMESTAMP", [0, 1_500, 86_399_999, None, DAY, 2 * DAY + 7]),
        (
            "level",
            "DECIMAL(12,3)",
            [Decimal("1.500"), "2.250", None, Decimal("0.001"), Decimal("123456789.123"), 3],
        ),
        ("valid", "BOOLEAN", [True, False, True, None, True, True]),
    ]
}


def run(
    statements: Sequence[Statement],
    tables: Dict[str, List[Any]] | None = None,
    *,
    seconds: int = 20,
    memory: int = 512 * MIB,
    **kwargs: Any,
) -> ChildRun:
    return run_in_child(
        TABLES if tables is None else tables, statements, seconds=seconds, memory=memory, **kwargs
    )


def one(sql: str, returns: str = "count", **kwargs: Any) -> StatementResult:
    (result,) = run([Statement("s", sql, returns)], **kwargs).results
    return result


# -- results --------------------------------------------------------------------------------


def test_a_count() -> None:
    result = one("SELECT count(*) FROM readings WHERE id > 2")
    assert (result.status, result.count, result.reason) == ("ok", 4, None)


def test_grouped_counts_carry_the_group_keys_and_a_whole_count() -> None:
    result = one("SELECT valid, count(*) FROM readings GROUP BY valid ORDER BY valid", "counts")
    assert result.status == "ok"
    assert result.groups == ((False, 1), (True, 4), (None, 1))


def test_grouped_counts_over_text_and_whole_number_keys() -> None:
    result = one(
        "SELECT station, id % 2, count(*) FROM readings GROUP BY station, id % 2 "
        "ORDER BY ALL NULLS LAST",
        "counts",
    )
    assert result.status == "ok"
    assert ("a", 1, 2) in (result.groups or ())
    assert (None, 0, 1) in (result.groups or ())


def test_a_decimal_date_or_timestamp_group_key_arrives_as_text() -> None:
    result = one(
        "SELECT read_on, count(*) FROM readings WHERE read_on IS NOT NULL GROUP BY read_on "
        "ORDER BY read_on LIMIT 2",
        "counts",
    )
    assert result.groups == (("1970-01-01", 1), ("1970-01-02", 1))


def test_bits_are_one_boolean_per_row_in_row_order() -> None:
    result = one("SELECT id % 2 = 0 FROM readings ORDER BY id", "bits")
    assert result.status == "ok" and result.bits is not None
    assert result.bits.n == 6
    assert result.bits.to_list() == [False, True, False, True, False, True]
    assert result.bits[1] and not result.bits[0]
    assert result.bits.packed == bytes([0b101010])


def test_bits_pack_past_a_byte() -> None:
    values = list(range(20))
    result = one(
        "SELECT v >= 10 FROM t ORDER BY v",
        "bits",
        tables={"t": [("v", "INTEGER", values)]},
    )
    assert result.bits is not None and result.bits.to_list() == [v >= 10 for v in values]
    assert len(result.bits.packed) == 3


def test_a_null_bit_is_an_error() -> None:
    result = one("SELECT valid FROM readings ORDER BY id", "bits")
    assert result.status == "error" and result.bits is None


def test_an_empty_table_counts_zero() -> None:
    result = one("SELECT count(*) FROM t", tables={"t": [("a", "BIGINT", [])]})
    assert (result.status, result.count) == ("ok", 0)


def test_statements_may_be_mappings() -> None:
    (result,) = run_in_child(
        TABLES,
        [{"id": "m", "sql": "SELECT count(*) FROM readings", "returns": "count"}],
        seconds=20,
        memory=512 * MIB,
    ).results
    assert result.count == 6


def test_results_come_back_in_the_order_of_the_statements() -> None:
    done = run(
        [
            Statement("first", "SELECT count(*) FROM readings", "count"),
            Statement("second", "SELECT count(*) FROM readings WHERE id = 1", "count"),
            Statement("third", "SELECT count(*) FROM readings WHERE id > 100", "count"),
        ]
    )
    assert [(r.id, r.count) for r in done.results] == [("first", 6), ("second", 1), ("third", 0)]
    assert done.restarts == 0


# -- what loads exactly ---------------------------------------------------------------------


def test_a_bare_file_path_is_no_table() -> None:
    result = one("SELECT count(*) FROM '/etc/hosts'")
    assert result.status == "error" and result.count is None


def test_a_long_column_crosses_as_several_frames() -> None:
    n = 70_000
    tables = {"t": [("v", "BIGINT", list(range(n))), ("s", "VARCHAR", ["x"] * n)]}
    result = one("SELECT count(*) FROM t WHERE s = 'x' AND v >= 60000", tables=tables)
    assert (result.status, result.count) == ("ok", 10_000)


def test_decimals_dates_timestamps_null_and_quoted_text_load_exactly() -> None:
    checks = {
        "decimal 1.500": "SELECT count(*) FROM readings WHERE level = 1.500 AND id = 1",
        "decimal text 2.250": "SELECT count(*) FROM readings WHERE level = 2.250 AND id = 2",
        "decimal big": "SELECT count(*) FROM readings WHERE level = 123456789.123 AND id = 5",
        "decimal int": "SELECT count(*) FROM readings WHERE level = 3 AND id = 6",
        "decimal small": "SELECT count(*) FROM readings WHERE level = 0.001 AND id = 4",
        "decimal null": "SELECT count(*) FROM readings WHERE level IS NULL AND id = 3",
        "date": "SELECT count(*) FROM readings WHERE read_on = DATE '1970-01-02' AND id = 2",
        "date later": "SELECT count(*) FROM readings WHERE read_on = DATE '1970-01-11' AND id = 5",
        "date null": "SELECT count(*) FROM readings WHERE read_on IS NULL AND id = 4",
        "timestamp ms": (
            "SELECT count(*) FROM readings WHERE taken = TIMESTAMP '1970-01-01 00:00:01.5' "
            "AND id = 2"
        ),
        "timestamp end of day": (
            "SELECT count(*) FROM readings WHERE taken = TIMESTAMP '1970-01-01 23:59:59.999' "
            "AND id = 3"
        ),
        "text quote": "SELECT count(*) FROM readings WHERE station = 'it''s' AND id = 5",
        "text double quote": "SELECT count(*) FROM readings WHERE station = 'say \"hi\"' AND id = 6",
        "text null": "SELECT count(*) FROM readings WHERE station IS NULL AND id = 4",
        "boolean null": "SELECT count(*) FROM readings WHERE valid IS NULL AND id = 4",
    }
    done = run([Statement(name, sql, "count") for name, sql in checks.items()])
    assert {r.id: (r.status, r.count) for r in done.results} == {name: ("ok", 1) for name in checks}


def test_a_column_and_a_table_named_with_quotes_load_as_named() -> None:
    tables = {'we"ird': [('co"l', "BIGINT", [1, 2]), ("plain", "VARCHAR", ["x", "y"])]}
    result = one(
        'SELECT count(*) FROM "we""ird" WHERE "co""l" = 2 AND plain = \'y\'', tables=tables
    )
    assert (result.status, result.count) == ("ok", 1)


def test_text_that_looks_like_sql_is_a_value_and_never_sql() -> None:
    nasty = "'); DROP TABLE readings; --"
    tables = {"readings": [("v", "VARCHAR", [nasty, "x"])]}
    done = run(
        [
            Statement("a", "SELECT count(*) FROM readings WHERE v LIKE '%DROP TABLE%'", "count"),
            Statement("b", "SELECT count(*) FROM readings", "count"),
        ],
        tables=tables,
    )
    assert [(r.status, r.count) for r in done.results] == [("ok", 1), ("ok", 2)]


def test_non_ascii_text_loads() -> None:
    tables = {"t": [("v", "VARCHAR", ["é", "日", "\U0001f600"])]}
    result = one("SELECT count(*) FROM t WHERE length(v) = 1", tables=tables)
    assert result.count == 3


def test_a_table_that_holds_a_value_of_another_type_is_a_value_error_naming_no_value() -> None:
    for bad in (
        {"t": [("a", "BIGINT", [1, "secret-9999"])]},
        {"t": [("a", "BIGINT", [True])]},
        {"t": [("a", "VARCHAR", [5])]},
        {"t": [("a", "BOOLEAN", [1])]},
        {"t": [("a", "DECIMAL(5,2)", ["secret-9999"])]},
        {"t": [("a", "VARCHAR", ["\ud800"])]},
        {"t": [("a", "BIGINT", [1]), ("b", "BIGINT", [1, 2])]},
        {"t": [("a", "BIGINT", [1]), ("A", "BIGINT", [1])]},
        {"t": [("a", "BLOB", [None])]},
        {"t": [("a", "DECIMAL(40,2)", [None])]},
        {"t": []},
    ):
        with pytest.raises(ValueError) as caught:
            run([Statement("s", "SELECT 1", "count")], tables=bad)
        assert "secret-9999" not in str(caught.value)


def test_arguments_that_are_not_as_described_are_value_errors() -> None:
    sel = Statement("s", "SELECT 1", "count")
    for statements in (
        [Statement("s", "SELECT 1", "rows")],
        [sel, sel],
        [Statement("", "SELECT 1", "count")],
    ):
        with pytest.raises(ValueError):
            run(statements)
    for kwargs in ({"seconds": 0}, {"memory": 5}, {"seconds": True}):
        with pytest.raises(ValueError):
            run([sel], **kwargs)


# -- what the child refuses -----------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM read_csv('/etc/hosts', header=false, columns={'a': 'VARCHAR'})",
        "SELECT count(*) FROM read_text('/etc/hosts')",
        "SELECT count(*) FROM glob('/*')",
        "SELECT count(*) FROM 'https://example.com/data.csv'",
        "SELECT count(*) FROM read_parquet('/etc/hosts')",
    ],
)
def test_a_statement_that_reads_a_file_or_a_url_fails_with_a_permission_error(sql: str) -> None:
    result = one(sql)
    assert result.status == "error"
    assert result.reason is not None and result.reason.startswith("PermissionException")
    assert result.count is None


@pytest.mark.parametrize(
    "sql",
    [
        "COPY readings TO '/tmp/consumer-queries-must-not-exist.csv'",
        "ATTACH '/tmp/consumer-queries-must-not-exist.db' AS other",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET threads = 2",
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "PRAGMA enable_external_access",
        "CREATE TABLE x AS SELECT 1",
        "SELECT 1; SELECT 2",
        "",
    ],
)
def test_the_child_runs_only_one_select(sql: str) -> None:
    result = one(sql)
    assert result.status == "error" and result.count is None
    assert (
        result.reason is not None
        and "SELECT" in result.reason
        or "statement" in (result.reason or "")
    )
    assert not Path("/tmp/consumer-queries-must-not-exist.csv").exists()
    assert not Path("/tmp/consumer-queries-must-not-exist.db").exists()


PROBE = (
    "import runpy, sys; "
    "ns = runpy.run_path(sys.argv[1], run_name='consumer_queries_probe'); "
    "raise SystemExit(ns['main'](select_only=False))"
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/hosts')",
        "SELECT * FROM glob('/*')",
        "SELECT * FROM 'https://example.com/data.csv'",
        "COPY readings TO '/tmp/consumer-queries-must-not-exist.csv'",
        "ATTACH '/tmp/consumer-queries-must-not-exist.db' AS other",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET threads = 2",
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "SET autoload_known_extensions = true",
        "SET allow_unsigned_extensions = true",
        "EXPORT DATABASE '/tmp/consumer-queries-must-not-exist-dir'",
    ],
)
def test_the_locks_hold_inside_the_child_with_only_the_select_gate_removed(sql: str) -> None:
    """The same path as a real statement; the one thing bypassed is the SELECT-only gate."""
    done = run_in_child(
        TABLES,
        [Statement("s", sql, "count")],
        seconds=20,
        memory=512 * MIB,
        _command=(sys.executable, "-I", "-c", PROBE, runner._CHILD_FILE),
    )
    (result,) = done.results
    assert result.status == "error", result
    assert result.reason is not None
    assert result.reason.split(":")[0] in (
        "PermissionException",
        "InvalidInputException",
        "CatalogException",
    ), result.reason
    assert not Path("/tmp/consumer-queries-must-not-exist.csv").exists()
    assert not Path("/tmp/consumer-queries-must-not-exist.db").exists()
    assert not Path("/tmp/consumer-queries-must-not-exist-dir").exists()


def test_the_probe_runs_the_same_child_so_the_test_above_proves_something() -> None:
    done = run_in_child(
        TABLES,
        [Statement("s", "SELECT count(*) FROM readings", "count")],
        seconds=20,
        memory=512 * MIB,
        _command=(sys.executable, "-I", "-c", PROBE, runner._CHILD_FILE),
    )
    assert done.results[0].count == 6


def test_duckdb_message_never_crosses_the_pipe() -> None:
    result = one("SELECT CAST(station AS BIGINT) FROM readings WHERE id = 5")
    assert result.status == "error"
    assert result.reason is not None and "it's" not in result.reason
    assert result.reason.endswith("the engine refused the statement")


# -- the child's world ----------------------------------------------------------------------


def test_the_environment_holds_only_path_and_lc_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("DUCKDB_HOME", "/nowhere")
    monkeypatch.setenv("PYTHONPATH", os.getcwd())
    monkeypatch.setenv("PYTHONSTARTUP", "/nowhere")
    done = run([Statement("s", "SELECT 1", "count")])
    # the system may add its own text-encoding variable to a process it starts on macOS
    assert set(done.environment) <= {"PATH", "LC_ALL", "__CF_USER_TEXT_ENCODING"}
    assert {"PATH", "LC_ALL"} <= set(done.environment)


def test_the_parent_starts_the_child_with_isolation_and_no_extra_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: Dict[str, Any] = {}
    real = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["args"], seen["kwargs"] = args, kwargs
        return real(*args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    run([Statement("s", "SELECT 1", "count")])
    command = seen["args"][0]
    assert command[0] == sys.executable and command[1] == "-I"
    assert command[2] == str(Path(_child.__file__).resolve()) and os.path.isabs(command[2])
    assert len(command) == 3
    kwargs = seen["kwargs"]
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    assert kwargs["close_fds"] is True and kwargs["pass_fds"] == ()
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["stdin"] == subprocess.PIPE and kwargs["stdout"] == subprocess.PIPE
    assert kwargs.get("shell") is not True


def test_the_child_cannot_import_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "planted"
    (tmp_path / "json.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (tmp_path / "duckdb.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    done = run([Statement("s", "SELECT count(*) FROM readings", "count")])
    assert done.results[0].count == 6
    assert not marker.exists()


def test_the_child_file_imports_only_the_standard_library_duckdb_and_the_sandbox() -> None:
    # The child opens DuckDB through the repo's one sandboxed connect, and imports nothing else of
    # forge: outside docstrings, the module name appears only in that one import.
    import ast

    source = Path(_child.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    named = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
        and "fluid_build" in n.value
    ]
    assert named == ["fluid_build.providers._duckdb_sandbox"]
    roots = {
        alias.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for alias in n.names
    } | {
        (n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.level == 0
    }
    assert roots <= set(sys.stdlib_module_names) | {"duckdb", "__future__"}


def test_limits_are_lowered_before_duckdb_and_reported() -> None:
    done = run([Statement("s", "SELECT 1", "count")], seconds=3)
    limits = done.limits
    assert set(limits) == {"fsize", "cpu", "as", "nofile"}
    assert limits["fsize"] == 0
    assert limits["cpu"] is not None and limits["cpu"] >= 3
    assert limits["nofile"] is not None and limits["nofile"] < 64
    if sys.platform == "darwin":
        assert limits["as"] is None  # accepted and not enforced there, so never reported
    else:
        assert limits["as"] is not None


def test_the_child_cannot_write_a_file() -> None:
    target = Path("/tmp/consumer-queries-fsize-probe")
    target.unlink(missing_ok=True)
    code = (
        "import runpy, sys; ns = runpy.run_path(sys.argv[1], run_name='probe'); "
        "ns['_lower_before_imports'](1 << 30, 100); "
        f"f = open({str(target)!r}, 'wb'); f.write(b'x'); f.flush()"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, runner._CHILD_FILE], capture_output=True, check=False
    )
    assert out.returncode != 0
    assert not target.exists() or target.stat().st_size == 0
    target.unlink(missing_ok=True)


def test_the_version_floor_is_the_sandbox_floor() -> None:
    from fluid_build.providers._duckdb_sandbox import MIN_DUCKDB_VERSION

    assert _child.MIN_DUCKDB == MIN_DUCKDB_VERSION


def test_the_run_reports_the_duckdb_version() -> None:
    import duckdb

    assert run([Statement("s", "SELECT 1", "count")]).duckdb_version == duckdb.__version__


# -- time, memory and a dead child ------------------------------------------------------------

RUNAWAY = "SELECT count(*) FROM range(1000000000) a, range(100000) b"


def test_a_runaway_statement_times_out_and_the_next_one_still_runs() -> None:
    started = time.monotonic()
    done = run(
        [
            Statement("slow", RUNAWAY, "count"),
            Statement("after", "SELECT count(*) FROM readings", "count"),
        ],
        seconds=1,
    )
    assert time.monotonic() - started < 15
    slow, after = done.results
    assert slow.status == "timedOut" and slow.count is None
    assert (after.status, after.count) == ("ok", 6)
    assert done.restarts == 0  # the child survived its interrupt


def test_a_statement_past_the_memory_budget_takes_out_of_memory_and_the_next_one_runs() -> None:
    big = {"t": [("v", "BIGINT", list(range(200_000)))]}
    hog = (
        "SELECT count(*) FROM (SELECT a.v AS x, b.v AS y, repeat('x', 200) AS pad FROM t a, t b "
        "ORDER BY pad, y, x DESC) AS q"
    )
    done = run(
        [
            Statement("hog", hog, "count"),
            Statement("after", "SELECT count(*) FROM t", "count"),
        ],
        tables=big,
        seconds=60,
        memory=64 * MIB,
    )
    hog_result, after = done.results
    assert hog_result.status == "outOfMemory", hog_result
    assert (after.status, after.count) == ("ok", 200_000)


def test_tables_past_the_memory_budget_give_every_statement_out_of_memory() -> None:
    big = {"t": [("s", "VARCHAR", ["x" * 1000] * 400_000)]}
    done = run(
        [Statement("a", "SELECT 1", "count"), Statement("b", "SELECT 2", "count")],
        tables=big,
        memory=32 * MIB,
        seconds=60,
    )
    assert {r.status for r in done.results} <= {"outOfMemory", "error", "crashed"}
    assert all(r.status != "ok" for r in done.results)
    assert [r.id for r in done.results] == ["a", "b"]


def test_a_child_that_dies_gives_crashed_and_a_fresh_one_runs_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: List[subprocess.Popen[bytes]] = []
    real = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        process = real(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", spy)

    def kill_first_when_busy() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if started:
                time.sleep(1.5)  # the runaway statement is running by now
                started[0].kill()
                return
            time.sleep(0.05)

    killer = threading.Thread(target=kill_first_when_busy)
    killer.start()
    done = run(
        [
            Statement("before", "SELECT count(*) FROM readings", "count"),
            Statement("victim", RUNAWAY, "count"),
            Statement("after", "SELECT count(*) FROM readings WHERE id <= 3", "count"),
            Statement("last", "SELECT valid, count(*) FROM readings GROUP BY valid", "counts"),
        ],
        seconds=30,
    )
    killer.join()
    before, victim, after, last = done.results
    assert (before.status, before.count) == ("ok", 6)
    assert victim.status == "crashed" and victim.reason is not None
    assert "signal" in victim.reason
    assert (after.status, after.count) == ("ok", 3)
    assert last.status == "ok" and last.groups is not None
    assert done.restarts == 1
    assert len(started) == 2
    assert all(p.poll() is not None for p in started)  # no child is left running


def test_a_child_that_cannot_start_leaves_every_statement_crashed() -> None:
    done = run(
        [Statement("a", "SELECT 1", "count"), Statement("b", "SELECT 1", "count")],
        _command=(sys.executable, "-I", "-c", "raise SystemExit(7)"),
    )
    assert [r.status for r in done.results] == ["crashed", "crashed"]
    assert "exit status 7" in (done.results[0].reason or "")


def test_a_child_that_ignores_its_interrupt_is_ended_by_the_parent_clock() -> None:
    hang = "import sys, time; sys.stdin.buffer.read(4); time.sleep(600)"
    started = time.monotonic()
    done = run(
        [Statement("a", "SELECT 1", "count")],
        _command=(sys.executable, "-I", "-c", hang),
        seconds=1,
        grace_seconds=1,
        start_seconds=2,
    )
    assert time.monotonic() - started < 30
    assert done.results[0].status in ("timedOut", "crashed")


def test_no_child_is_left_when_the_run_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    started: List[subprocess.Popen[bytes]] = []
    real = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        process = real(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", spy)
    monkeypatch.setattr(runner, "_version", lambda text: (1, 4, 3))  # a DuckDB older than the floor
    with pytest.raises(ChildUnavailable):
        run([Statement("s", "SELECT 1", "count")])
    assert started and all(p.poll() is not None for p in started)


def test_a_missing_duckdb_is_unavailable_with_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    code = (
        "import runpy, sys; sys.modules['duckdb'] = None; "
        "ns = runpy.run_path(sys.argv[1], run_name='probe'); raise SystemExit(ns['main']())"
    )
    with pytest.raises(ChildUnavailable) as caught:
        run(
            [Statement("s", "SELECT 1", "count")],
            _command=(sys.executable, "-I", "-c", code, runner._CHILD_FILE),
        )
    assert "profile" in caught.value.fix


def test_a_lock_that_does_not_read_back_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _child.settings_read_back()
    expected["enable_external_access"] = "true"
    monkeypatch.setattr(_child, "settings_read_back", lambda: expected)
    with pytest.raises(ChildUnavailable) as caught:
        run([Statement("s", "SELECT 1", "count")])
    assert "locks" in caught.value.what


def test_nothing_is_written_to_the_parents_output(capfd: pytest.CaptureFixture[str]) -> None:
    run([Statement("s", "SELECT count(*) FROM readings", "count")])
    out, err = capfd.readouterr()
    assert out == "" and err == ""


# -- the framing ----------------------------------------------------------------------------


def test_frames_refuse_floats_and_oversize_lengths() -> None:
    import io

    with pytest.raises(_child.FrameError):
        _child.decode_json(b'{"a": 1.5}')
    with pytest.raises(_child.FrameError):
        _child.decode_json(b'{"a": NaN}')
    with pytest.raises(_child.FrameError):
        _child.encode_json({"a": float("nan")})
    stream = io.BytesIO((10).to_bytes(4, "big") + b"x" * 10)
    with pytest.raises(_child.FrameError):
        _child.read_frame(stream, 5)
    with pytest.raises(EOFError):
        _child.read_frame(io.BytesIO(b""), 5)
    with pytest.raises(EOFError):
        _child.read_frame(io.BytesIO((10).to_bytes(4, "big") + b"xx"), 20)
