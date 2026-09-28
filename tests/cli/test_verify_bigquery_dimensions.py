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

"""``fluid verify`` on a BigQuery table counts it, and checks its masked columns.

On 0.16.5 the BigQuery verifier read only the table's metadata: a seeded
cleartext ``msisdn`` in a column the contract lands hashed passed ``--strict``,
an empty table passed, and on the goccy emulator (``numRows`` unset) the
console crashed formatting ``None`` with ``:,``. These pin the two dimensions
the Glue + Athena verifier already has (``row_count``, ``masking``), computed
from one GoogleSQL query.

The fake client runs that query for real: the GoogleSQL is rewritten into
DuckDB's dialect (backticks, ``COUNTIF``, ``REGEXP_CONTAINS`` against the bound
parameter, both RE2) and executed over the table's rows, so a quoting or
pattern mistake fails here rather than in BigQuery.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
import yaml

duckdb = pytest.importorskip("duckdb")

from fluid_build.build_runners import _bigquery_load  # noqa: E402
from fluid_build.cli.verify import run, verify_bigquery_table  # noqa: E402

_LOG = logging.getLogger("test")
PRODUCT = "bronze.customer_subscriptions"
TABLE_ID = "northwind-demo.demo_bronze.customer_subscriptions"
HASHED = "a" * 64


class _Field(SimpleNamespace):
    pass


_SCHEMA = [
    _Field(name="subscription_id", field_type="STRING", mode="REQUIRED"),
    _Field(name="msisdn", field_type="STRING", mode="NULLABLE"),
]


class FakeBigQuery:
    """A table's metadata, and a query engine for the verifier's count query."""

    def __init__(
        self,
        rows: List[tuple],
        *,
        query_error: Optional[Exception] = None,
        adc_project: Optional[str] = "adc-project",
    ):
        self.rows = rows
        self.sql: List[str] = []
        self.params: List[Any] = []
        self.tables_read: List[str] = []
        fake = self

        class QueryJobConfig:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Job:
            job_id = "q_1"

            def __init__(self, values):
                self._values = values

            def result(self, timeout=None):
                return [SimpleNamespace(values=lambda: tuple(self._values))]

        class Client:
            def __init__(self, project=None, credentials=None):
                # As google-cloud-core: only None falls back to the default
                # (ADC's); an empty string is kept as the project.
                self.project = adc_project if project is None else project

            def get_table(self, table_id):
                fake.tables_read.append(table_id)
                if table_id.startswith("."):
                    # python-bigquery on a table id with no project.
                    raise ValueError("Could not determine project ID")
                return SimpleNamespace(schema=_SCHEMA, num_rows=None, created=None, modified=None)

            def get_dataset(self, ref):
                return SimpleNamespace(location="europe-west1")

            def query(self, sql, job_config=None, location=None):
                if query_error is not None:
                    raise query_error
                fake.sql.append(sql)
                params = list(getattr(job_config, "query_parameters", []) or [])
                fake.params.append(params)
                return _Job(fake._run(sql, params))

        self.module = SimpleNamespace(
            Client=Client,
            QueryJobConfig=QueryJobConfig,
            ScalarQueryParameter=lambda name, kind, value: (name, kind, value),
        )

    def _run(self, sql: str, params: List[tuple]) -> tuple:
        """The GoogleSQL count query, run by DuckDB over ``rows``."""
        duck = re.sub(r"FROM `[^`]+`\.`[^`]+`\.`[^`]+`", "FROM t", sql)
        duck = duck.replace("`", '"').replace("COUNTIF(", "count_if(")
        duck = duck.replace("REGEXP_CONTAINS(", "regexp_matches(").replace(
            " AS STRING)", " AS VARCHAR)"
        )
        values = {name: value for name, _kind, value in params}
        duck = re.sub(r"@(\w+)", lambda m: "'" + values[m.group(1)].replace("'", "''") + "'", duck)
        con = duckdb.connect()
        con.execute("CREATE TABLE t (subscription_id VARCHAR, msisdn VARCHAR)")
        if self.rows:
            con.executemany("INSERT INTO t VALUES (?, ?)", self.rows)
        return con.execute(duck).fetchone()


@pytest.fixture
def bq(monkeypatch):
    def install(rows, **kw) -> FakeBigQuery:
        fake = FakeBigQuery(rows, **kw)
        monkeypatch.setattr(_bigquery_load, "_bigquery_module", lambda: fake.module)
        return fake

    return install


def _expose(project: str = "northwind-demo") -> Dict[str, Any]:
    return {
        "exposeId": "subscriptions",
        "kind": "table",
        "policy": {"privacy": {"masking": [{"column": "msisdn", "strategy": "hash"}]}},
        "binding": {
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {
                "project": project,
                "dataset": "demo_bronze",
                "table": "customer_subscriptions",
                "region": "europe-west1",
            },
        },
        "contract": {
            "schema": [
                {"name": "subscription_id", "type": "VARCHAR", "required": True},
                {"name": "msisdn", "type": "VARCHAR"},
            ]
        },
    }


def _contract(expose: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Customer Subscriptions",
        "builds": [
            {
                "id": "ingest_subscriptions",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {"source": {"kind": "postgres", "mode": "full_refresh"}},
                "outputs": ["subscriptions"],
            }
        ],
        "exposes": [expose or _expose()],
    }


def _verify(tmp_path: Path, contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    contract = contract or _contract()
    return verify_bigquery_table(
        "northwind-demo",
        "demo_bronze",
        "customer_subscriptions",
        contract["exposes"][0]["contract"]["schema"],
        "europe-west1",
        expose=contract["exposes"][0],
        contract=contract,
        workdir=tmp_path,
    )


def _run_record(
    tmp_path: Path,
    run_id: str,
    *,
    rows: int,
    table: Optional[str] = TABLE_ID,
    state: str = "succeeded",
    build_id: str = "ingest_subscriptions",
) -> None:
    facets: Dict[str, Any] = {
        "landed": {"mode": "full_refresh", "rows_from": "write", "destinations": {}}
    }
    if table is not None:
        facets["bigquery_load"] = {"table": table, "rows": rows, "job_id": "j"}
    path = tmp_path / ".fluid" / "runs" / PRODUCT / build_id / "runs"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "state": state, "records_total": rows, "facets": facets}),
        encoding="utf-8",
    )


# ── Masking ─────────────────────────────────────────────────────────────


def test_treated_values_pass_the_masking_dimension(bq, tmp_path):
    bq([("s1", HASHED), ("s2", HASHED), ("s3", None)])
    result = _verify(tmp_path)
    masking = result["dimensions"]["masking"]
    assert masking["status"] == "pass"
    assert masking["columns"][0]["non_null"] == 2
    assert result["status"] == "match"
    assert result["severity"]["level"] == "SUCCESS"


def test_a_seeded_cleartext_value_fails_masking_as_critical(bq, tmp_path):
    bq([("s1", HASHED), ("s2", "+46701234567"), ("s3", HASHED)])
    result = _verify(tmp_path)
    masking = result["dimensions"]["masking"]
    assert masking["status"] == "fail"
    assert masking["columns"][0]["offending"] == 1
    assert "+46701234567" not in json.dumps(result)  # counts only, never the value
    assert result["status"] == "mismatch"
    assert result["severity"]["level"] == "CRITICAL"


def test_a_hash_with_a_trailing_newline_is_not_a_hash(bq, tmp_path):
    """The pattern is anchored with \\A and \\z: REGEXP_CONTAINS finds, it does not match."""
    bq([("s1", HASHED + "\n"), ("s2", "x" + HASHED)])
    assert _verify(tmp_path)["dimensions"]["masking"]["columns"][0]["offending"] == 2


def test_the_query_quotes_with_backticks_and_binds_the_pattern(bq, tmp_path):
    fake = bq([("s1", HASHED)])
    _verify(tmp_path)
    [sql] = fake.sql
    # A double-quoted name is a string literal in GoogleSQL: count("msisdn") counts rows.
    assert '"msisdn"' not in sql and "count(`msisdn`)" in sql
    assert "FROM `northwind-demo`.`demo_bronze`.`customer_subscriptions`" in sql
    [[(name, kind, value)]] = fake.params
    assert (kind, value) == ("STRING", r"\A(?:[0-9a-f]{64})\z")
    assert f"@{name}" in sql


# ── Row count ───────────────────────────────────────────────────────────


def test_the_count_equal_to_the_last_load_passes(bq, tmp_path):
    bq([("s1", HASHED), ("s2", HASHED)])
    _run_record(tmp_path, "20260928T000001Z", rows=2)
    rc = _verify(tmp_path)["dimensions"]["row_count"]
    assert (rc["status"], rc["actual"], rc["expected"]) == ("pass", 2, 2)
    assert rc["compared_with"]["rule"] == "equal"


def test_a_count_below_the_last_load_fails_as_critical(bq, tmp_path):
    bq([("s1", HASHED)])
    _run_record(tmp_path, "20260928T000001Z", rows=2)
    result = _verify(tmp_path)
    assert result["dimensions"]["row_count"]["status"] == "fail"
    assert "BigQuery counted 1 rows" in result["dimensions"]["row_count"]["message"]
    assert result["severity"]["level"] == "CRITICAL"


def test_a_newer_local_run_of_the_same_build_is_passed_over(bq, tmp_path):
    bq([("s1", HASHED), ("s2", HASHED)])
    _run_record(tmp_path, "20260928T000001Z", rows=2)
    _run_record(tmp_path, "20260928T000002Z", rows=7, table=None)  # a local run
    rc = _verify(tmp_path)["dimensions"]["row_count"]
    assert (rc["status"], rc["expected"]) == ("pass", 2)
    assert rc["compared_with"]["other_target_runs_skipped"] == 1


def test_a_failed_last_run_is_not_a_count_to_hold_the_table_to(bq, tmp_path):
    bq([("s1", HASHED)])
    _run_record(tmp_path, "20260928T000001Z", rows=5)
    _run_record(tmp_path, "20260928T000002Z", rows=0, table=None, state="failed")
    rc = _verify(tmp_path)["dimensions"]["row_count"]
    assert rc["status"] == "pass" and rc["expected"] is None


def test_an_empty_table_fails(bq, tmp_path):
    bq([])
    result = _verify(tmp_path)
    assert result["dimensions"]["row_count"]["status"] == "fail"
    assert result["severity"]["level"] == "CRITICAL"


def test_a_count_that_cannot_run_is_an_error_not_a_pass(bq, tmp_path):
    bq([("s1", HASHED)], query_error=PermissionError("403 bigquery.jobs.create"))
    result = _verify(tmp_path)
    assert result["status"] == "error"
    assert "bigquery.jobs.create" in result["error"]


def test_the_direct_call_without_an_expose_keeps_its_four_dimensions(bq, tmp_path):
    fake = bq([("s1", HASHED)])
    result = verify_bigquery_table(
        "northwind-demo", "demo_bronze", "customer_subscriptions", [], "europe-west1"
    )
    assert "row_count" not in result["dimensions"] and fake.sql == []


# ── Through the CLI ─────────────────────────────────────────────────────


def _args(contract_path: Path, *, strict: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        contract=str(contract_path),
        expose_id=None,
        strict=strict,
        out=None,
        show_diffs=False,
        env=None,
    )


def _write(tmp_path: Path, contract: Dict[str, Any]) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def test_strict_verify_fails_on_seeded_cleartext_and_passes_when_treated(bq, tmp_path):
    path = _write(tmp_path, _contract())
    _run_record(tmp_path, "20260928T000001Z", rows=2)
    bq([("s1", HASHED), ("s2", HASHED)])
    assert run(_args(path), _LOG) == 0
    bq([("s1", HASHED), ("s2", "+46701234567")])
    assert run(_args(path), _LOG) == 1


def test_a_table_that_reports_no_row_count_is_rendered_not_a_crash(tmp_path, capsys):
    """0.16.5 formatted ``numRows`` with ``:,``, and the emulator leaves it None."""
    path = _write(tmp_path, _contract())
    result = {
        "status": "match",
        "exists": True,
        "table_id": TABLE_ID,
        "severity": {"level": "SUCCESS", "symbol": "🟢", "impact": "NONE", "remediation": "NONE"},
        "dimensions": {},
        "metadata": {"num_rows": None},
    }
    with patch("fluid_build.cli.verify.verify_bigquery_table", return_value=result):
        assert run(_args(path, strict=False), _LOG) == 0
    assert "Table Rows: unknown" in capsys.readouterr().out


def test_verify_resolves_the_project_the_way_the_load_does(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_DEMO_GCP_PROJECT", "acme-eu-demo")
    path = _write(tmp_path, _contract(_expose(project="{{ env.FLUID_DEMO_GCP_PROJECT }}")))
    seen: Dict[str, Any] = {}

    def fake_verify(**kw):
        seen.update(kw)
        return {"status": "match", "dimensions": {}}

    with patch("fluid_build.cli.verify.verify_bigquery_table", side_effect=fake_verify):
        run(_args(path, strict=False), _LOG)
    assert seen["project"] == "acme-eu-demo"
    assert seen["expose"]["exposeId"] == "subscriptions"


# ── The project, when the binding names none ────────────────────────────
#
# PR review: the load and the read name the table from ``client.project``
# (ADC's when nothing else names one), but verify passed ``project=""``, which
# the client keeps, and looked up ``.demo_bronze.customer_subscriptions``.


def test_a_binding_with_no_project_is_verified_in_the_clients_project(bq, tmp_path):
    fake = bq([("s1", HASHED)])
    result = verify_bigquery_table("", "demo_bronze", "customer_subscriptions", [], "europe-west1")
    assert fake.tables_read == ["adc-project.demo_bronze.customer_subscriptions"]
    assert result["exists"] is True
    assert result["table_id"] == "adc-project.demo_bronze.customer_subscriptions"


def test_no_project_anywhere_is_an_error_naming_the_table(bq, tmp_path):
    fake = bq([("s1", HASHED)], adc_project=None)
    result = verify_bigquery_table("", "demo_bronze", "customer_subscriptions", [], "europe-west1")
    assert result["status"] == "error"
    assert "No project for demo_bronze.customer_subscriptions" in result["error"]
    assert fake.tables_read == []


# ── An embedded-SQL build's load is held to, as an acquisition load is ──


def _embedded_sql_contract() -> Dict[str, Any]:
    contract = _contract()
    contract["builds"] = [
        {
            "id": "summarize",
            "pattern": "embedded-logic",
            "engine": "duckdb",
            "properties": {"sql": "SELECT * FROM upstream"},
            "outputs": ["subscriptions"],
        }
    ]
    return contract


def _record_embedded_sql_load(tmp_path: Path, rows: int) -> None:
    """The record an embedded-SQL build writes after its load: the acquisition
    load's shape (``_embedded_sql_io.write_bigquery_run_record``; the build
    side is pinned in ``tests/build_runners/test_embedded_sql_bigquery.py``)."""
    _run_record(tmp_path, "20260928T000001Z", rows=rows, build_id="summarize")


def test_an_embedded_sql_table_is_held_to_the_rows_its_load_landed(bq, tmp_path):
    contract = _embedded_sql_contract()
    _record_embedded_sql_load(tmp_path, rows=2)
    bq([("s1", HASHED), ("s2", HASHED)])
    rc = _verify(tmp_path, contract)["dimensions"]["row_count"]
    assert (rc["status"], rc["actual"], rc["expected"]) == ("pass", 2, 2)
    assert rc["compared_with"]["rule"] == "equal"
    assert rc["compared_with"]["build_id"] == "summarize"


def test_an_embedded_sql_table_short_of_its_load_fails_as_critical(bq, tmp_path):
    contract = _embedded_sql_contract()
    _record_embedded_sql_load(tmp_path, rows=3)
    bq([("s1", HASHED), ("s2", HASHED)])
    result = _verify(tmp_path, contract)
    assert result["dimensions"]["row_count"]["status"] == "fail"
    assert result["severity"]["level"] == "CRITICAL"
