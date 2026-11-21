# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A ``spark.sql`` statement is as long as the report it builds.

Every string the folder builds was cut off at 4,096 characters, and a statement over it (a 5 KB
triple-quoted ``INSERT``, which is a small one) was never parsed, so the job lost its table
mapping: no product for what it writes, no ``consumes`` for what it reads. A string that feeds
``spark.sql`` has a larger cap of its own (``MAX_SQL_STRING``), and the work a module may spend
building strings is bounded (``MAX_STRING_WORK``), so a long literal copied over and over by a
hostile file costs what a short one does.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from fluid_build.cli.import_workflow import _fold, _spark_sql
from fluid_build.cli.import_workflow.pyspark import PySparkImporter

HEAD = "from pyspark.sql import SparkSession\n\nspark = SparkSession.builder.getOrCreate()\n"


def statement(chars: int, target: str = "db.out", source: str = "db.src") -> str:
    """An ``INSERT ... SELECT`` of about ``chars`` characters, one column per line."""
    columns, size, n = [], 0, 0
    while size < chars:
        column = f"  col_{n} AS c{n}"
        columns.append(column)
        size += len(column) + 2
        n += 1
    return f"INSERT OVERWRITE TABLE {target} SELECT\n" + ",\n".join(columns) + f"\nFROM {source}"


def propose(root: Path, monkeypatch, files: Dict[str, str]) -> Tuple[List[Dict[str, Any]], Any]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.chdir(root)
    return PySparkImporter().import_to_contracts("jobs", options={})


def said(report: Any) -> str:
    """Everything the report says about what was not read (a file with no Spark IO says it in its notes)."""
    return " ".join([*report.notes, *report.unsupported])


def mapped(contracts: List[Dict[str, Any]]) -> bool:
    """The job's write is a product, and its read is that product's ``consumes``."""
    ids = [c["id"] for c in contracts]
    reads = [x.get("productId") for c in contracts for x in c.get("consumes") or []]
    return ids == ["db.out"] and reads == ["db.src"]


@pytest.mark.parametrize("chars", [5_000, 20_000, 150_000])
def test_a_long_triple_quoted_statement_keeps_its_tables(tmp_path, monkeypatch, chars):
    source = HEAD + 'spark.sql("""' + statement(chars) + '""")\n'
    contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert mapped(contracts), (chars, report.unsupported)
    assert not any("longer than" in line for line in report.unsupported)


def test_a_long_statement_built_by_an_f_string_a_format_and_a_concatenation(tmp_path, monkeypatch):
    body = statement(20_000, target="TARGET", source="db.src").replace("TARGET", "{}")
    files = {
        "jobs/f.py": HEAD
        + 'TARGET = "db.out"\nspark.sql(f"""'
        + statement(20_000, target="{TARGET}")
        + '""")\n',
        "jobs/m.py": HEAD + 'spark.sql("""' + body + '""".format("db.out2"))\n',
        "jobs/c.py": HEAD
        + 'HEAD = "INSERT OVERWRITE TABLE db.out3 "\nspark.sql(HEAD + """'
        + statement(20_000).split("db.out ", 1)[1]
        + '""")\n',
    }
    contracts, report = propose(tmp_path, monkeypatch, files)
    assert sorted(c["id"] for c in contracts) == [
        "db.out",
        "db.out2",
        "db.out3",
    ], report.unsupported


def test_a_long_statement_in_another_file_of_the_tree_is_still_sql(tmp_path, monkeypatch):
    files = {
        "jobs/queries.py": 'BIG = """' + statement(20_000) + '"""\n',
        "jobs/j.py": HEAD + "from queries import BIG\n\nspark.sql(BIG)\n",
    }
    contracts, report = propose(tmp_path, monkeypatch, files)
    assert mapped(contracts), report.unsupported


def test_a_long_constant_read_as_sql_is_still_too_long_where_it_is_read_as_a_path(
    tmp_path, monkeypatch
):
    files = {
        "jobs/queries.py": 'BIG = """' + statement(6_000) + '"""\n',
        "jobs/j.py": HEAD
        + "from queries import BIG\n\nspark.sql(BIG)\nspark.read.parquet(BIG).write.parquet('s3://b/out')\n",
    }
    contracts, report = propose(tmp_path, monkeypatch, files)
    assert "db.out" in [c["id"] for c in contracts]  # the statement is read as SQL
    assert f"longer than {_fold.MAX_STRING} characters" in said(report)  # the read of it as a path


def test_text_that_is_not_sql_keeps_the_old_limit(tmp_path, monkeypatch):
    source = (
        HEAD
        + f'PATH = "s3://bucket/{"a" * 5_000}"\n'
        + 'spark.read.parquet("s3://bucket/in").write.parquet(PATH)\n'
    )
    _contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert any(f"longer than {_fold.MAX_STRING} characters" in x for x in report.unsupported)
    assert _fold.MAX_STRING == 4096


def test_a_statement_past_the_sql_limit_says_how_long_it_may_be(tmp_path, monkeypatch):
    source = HEAD + 'spark.sql("""' + statement(_fold.MAX_SQL_STRING + 1_000) + '""")\n'
    contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert contracts == []
    assert f"longer than {_fold.MAX_SQL_STRING} characters" in said(report)


def test_a_statement_the_parser_is_too_short_for_is_said_not_lost(tmp_path, monkeypatch):
    chars = _spark_sql.MAX_SQL_CHARS + 20_000
    assert chars < _fold.MAX_SQL_STRING
    source = HEAD + 'spark.sql("""' + statement(chars) + '""")\n'
    contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert contracts == []
    assert f"SQL of more than {_spark_sql.MAX_SQL_CHARS:,} characters was not parsed" in said(
        report
    )


def test_the_work_a_module_spends_building_strings_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(_fold, "MAX_STRING_WORK", 500_000)
    big = statement(100_000, target="db.t0")
    calls = "".join(f'spark.sql(BIG + " -- {n}")\n' for n in range(40))
    source = HEAD + f'BIG = """{big}"""\n' + calls
    started = time.perf_counter()
    _contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert time.perf_counter() - started < 20
    assert "too much work to fold" in said(report)
