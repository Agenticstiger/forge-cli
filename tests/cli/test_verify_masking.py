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

"""``fluid verify`` fails a local file whose masked columns hold cleartext.

Stage 9 checked a local output's column names and row count, so a file that
carried ``policy.privacy.masking`` columns in cleartext passed ``--strict``.
The masking dimension counts, per masked column, the non-null values without
their strategy's shape, and one is CRITICAL. The S3+Glue path runs the same
check through Athena (``test_verify_athena.py``).

The round-trip tests land the file with the real DuckDB runner first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import verify as verify_cmd

duckdb = pytest.importorskip("duckdb")

pytestmark = pytest.mark.unit

LOG = logging.getLogger("test.verify_masking")

SALT = "verify-test-salt-0123456789"
MSISDNS = ["+46701234567", "+46709876543", None]

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.subs
name: Subs
builds:
  - id: make_rows
    pattern: embedded-logic
    engine: sql
    properties:
      sql: "SELECT 1"
    outputs: [subs]
exposes:
  - exposeId: subs
    kind: table
    binding:
      platform: local
      format: parquet
      location: {path: ./out/subs.parquet}
    policy:
      privacy:
        masking:
          - {column: msisdn, strategy: hash}
          - {column: zip, strategy: mask, params: {keepLast: 2}}
    contract:
      schema:
        - {name: id, type: INTEGER}
        - {name: msisdn, type: VARCHAR}
        - {name: zip, type: VARCHAR}
"""


def _sha(value: str) -> str:
    return hashlib.sha256((SALT + value).encode("utf-8")).hexdigest()


@pytest.fixture
def contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_CONTRACT, encoding="utf-8")
    (tmp_path / "out").mkdir()
    return path


def _land(contract: Path, rows: List[tuple]) -> None:
    target = (contract.parent / "out" / "subs.parquet").as_posix()
    con = duckdb.connect()
    con.execute("CREATE TABLE t (id INTEGER, msisdn VARCHAR, zip VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    con.execute(f"COPY t TO '{target}' (FORMAT parquet)")
    con.close()


def _treated() -> List[tuple]:
    return [(i, _sha(v) if v else None, "***" + str(i) * 2) for i, v in enumerate(MSISDNS)]


def _verify(contract: Path, *flags: str) -> int:
    parser = argparse.ArgumentParser(prog="fluid")
    sub = parser.add_subparsers(dest="cmd")
    verify_cmd.register(sub)
    argv: List[str] = ["verify", str(contract), "--out", "report.json", *flags]
    return verify_cmd.run(parser.parse_args(argv), LOG)


def _report(contract: Path) -> Dict[str, Any]:
    doc = json.loads((contract.parent / "report.json").read_text(encoding="utf-8"))
    return doc["results"]["subs"]


def _with_rules(contract: Path, rules: List[Dict[str, Any]]) -> None:
    doc = yaml.safe_load(contract.read_text(encoding="utf-8"))
    doc["exposes"][0]["policy"]["privacy"]["masking"] = rules
    contract.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_treated_values_pass_strict(contract: Path) -> None:
    _land(contract, _treated())

    assert _verify(contract, "--strict") == 0
    masking = _report(contract)["dimensions"]["masking"]
    assert masking["status"] == "match"
    by_column = {c["column"]: c for c in masking["columns"]}
    assert by_column["msisdn"]["non_null"] == 2 and by_column["msisdn"]["offending"] == 0
    assert by_column["zip"]["non_null"] == 3 and by_column["zip"]["offending"] == 0


def test_cleartext_in_a_masked_column_fails_strict_as_critical(
    contract: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = _treated()
    rows[1] = (1, "+46709876543", rows[1][2])  # one row landed untreated
    _land(contract, rows)

    assert _verify(contract, "--strict") == 1
    result = _report(contract)
    assert result["status"] == "mismatch"
    assert result["severity"]["level"] == "CRITICAL"
    msisdn = next(c for c in result["dimensions"]["masking"]["columns"] if c["column"] == "msisdn")
    assert (msisdn["status"], msisdn["offending"], msisdn["non_null"]) == ("fail", 1, 2)
    assert "msisdn" in result["severity"]["reason"]
    # The failing value is the cleartext: it goes nowhere near the report.
    report_text = (contract.parent / "report.json").read_text(encoding="utf-8")
    assert "+46709876543" not in report_text
    assert "+46709876543" not in capsys.readouterr().out


def test_without_strict_cleartext_is_reported_as_a_mismatch(contract: Path) -> None:
    _land(contract, [(0, "+46701234567", "***00")])
    assert _verify(contract) == 0
    assert _report(contract)["dimensions"]["masking"]["status"] == "mismatch"


def test_a_masked_column_missing_from_the_file_fails(contract: Path) -> None:
    _with_rules(contract, [{"column": "imsi", "strategy": "hash"}])
    _land(contract, _treated())

    assert _verify(contract, "--strict") == 1
    (entry,) = _report(contract)["dimensions"]["masking"]["columns"]
    assert entry["status"] == "fail" and "not in the file" in entry["message"]


def test_k_anonymity_cannot_pass(contract: Path) -> None:
    """The build refuses it and no value can show it was applied."""
    _with_rules(contract, [{"column": "zip", "strategy": "k_anonymity"}])
    _land(contract, _treated())

    assert _verify(contract, "--strict") == 1
    (entry,) = _report(contract)["dimensions"]["masking"]["columns"]
    assert entry["strategy"] == "k_anonymity" and entry["status"] == "fail"


def test_a_rule_the_build_would_refuse_fails_the_dimension(contract: Path) -> None:
    _with_rules(contract, [{"column": "msisdn", "strategy": "hash", "params": {"salt": "x" * 20}}])
    _land(contract, _treated())

    assert _verify(contract, "--strict") == 1
    masking = _report(contract)["dimensions"]["masking"]
    assert masking["status"] == "mismatch"
    assert "literal 'salt'" in masking["message"]
    assert "x" * 20 not in json.dumps(masking)


def test_an_expose_without_masking_rules_has_no_masking_dimension(contract: Path) -> None:
    doc = yaml.safe_load(contract.read_text(encoding="utf-8"))
    del doc["exposes"][0]["policy"]
    contract.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    _land(contract, [(0, "+46701234567", "11122")])

    assert _verify(contract, "--strict") == 0
    assert "masking" not in _report(contract)["dimensions"]


def test_a_value_smuggled_after_a_newline_is_not_a_hash(contract: Path) -> None:
    _land(contract, [(0, "+46701234567\n" + _sha("x"), "***00")])
    assert _verify(contract, "--strict") == 1


# ── Round trip: the runner lands it, verify checks it ───────────────────


_ACQUISITION = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.subs
name: Subs
builds:
  - id: ingest
    pattern: acquisition
    engine: duckdb
    properties:
      source:
        kind: filesystem
        mode: full_refresh
        connection: {{uri: {source}}}
        reader: {{format: csv, options: {{all_varchar: true}}}}
        streams: [subs]
      sink: {{format: parquet}}
    outputs: [subs]
exposes:
  - exposeId: subs
    kind: table
    binding:
      platform: local
      format: parquet
      location: {{path: ./out/subs.parquet}}
    policy:
      privacy:
        masking:
          - {{column: msisdn, strategy: hash}}
    contract:
      schema:
        - {{name: id, type: VARCHAR}}
        - {{name: msisdn, type: VARCHAR}}
"""


def test_what_the_runner_lands_passes_and_a_cleartext_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.build_runners.duckdb.runner import execute_duckdb_build

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_PII_HASH_SECRET", SALT)
    source = tmp_path / "subs.csv"
    source.write_text("id,msisdn\n1,+46701234567\n2,+46709876543\n", encoding="utf-8")
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_ACQUISITION.format(source=source.as_posix()), encoding="utf-8")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert execute_duckdb_build(doc["builds"][0], doc, tmp_path) == 0
    assert _verify(path, "--strict") == 0
    assert _report(path)["dimensions"]["masking"]["status"] == "match"

    # The same file with the source's cleartext in it, as a build that ignored
    # the policy would have written.
    landed = (tmp_path / "out" / "subs.parquet").as_posix()
    duckdb.sql(
        f"COPY (SELECT * FROM read_csv('{source.as_posix()}', all_varchar=true)) TO '{landed}'"
    )
    assert _verify(path, "--strict") == 1
    assert _report(path)["severity"]["level"] == "CRITICAL"
