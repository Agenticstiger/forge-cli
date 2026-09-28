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

"""An embedded-SQL build on DuckDB reads BigQuery upstreams and lands in BigQuery.

Each of these was wrong on 0.16.5, measured against the goccy emulator:

* a ``consumes[]`` entry whose upstream is a gcp ``bigquery_table`` was an
  ``UnreadableBindingError``, so a silver or gold product could not build on
  GCP at all;
* with the documented ``parameters.inputs`` escape hatch, the result was
  written to a LOCAL file named ``gs:/...`` (the binding's staging path) and
  the build reported success with 0 rows in the table;
* a GCS or other-cloud landing did the same;
* the load wrote DuckDB's naive ``TIMESTAMP`` as a Parquet timestamp with
  ``isAdjustedToUTC=false``, which BigQuery reads as ``DATETIME``;
* against the emulator, the load failed ("loaded 0 rows") because its job
  reports no ``outputRows``, and a client with no ADC could not start.

The BigQuery client is faked through ``_bigquery_load._bigquery_module`` (the
seam the acquisition load tests use), holding each table as an Arrow table, so
these run with no cloud and no emulator. The emulator lane runs the same chain
for real: ``tests/providers/test_bigquery_emulated_embedded_sql_chain.py``.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import yaml

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from fluid_build.build_runners import _bigquery_load  # noqa: E402
from fluid_build.build_runners._embedded_sql_io import (  # noqa: E402
    ConsumesResolutionError,
    EmbeddedSqlLandingError,
    MaskingNotAppliedError,
    plan_embedded_sql_io,
    resolve_consumes,
)
from fluid_build.build_runners.base import _execute_embedded_sql_build  # noqa: E402

BRONZE = "bronze.customer_subscriptions"
SILVER = "silver.subscription_status_summary"
BRONZE_TABLE = "northwind-demo.demo_bronze.customer_subscriptions"
SILVER_TABLE = "northwind-demo.demo_silver.subscription_status_summary"
SQL = (
    "SELECT product_id, status, COUNT(*) AS subscription_count "
    "FROM subscriptions GROUP BY product_id, status"
)


# ── A BigQuery that keeps each table as an Arrow table ──────────────────


class _Field(SimpleNamespace):
    pass


def _schema(*cols: tuple) -> List[_Field]:
    return [_Field(name=n, field_type=t, mode="NULLABLE") for n, t in cols]


class FakeBigQuery:
    """The names ``_bigquery_load`` / ``_bigquery_read`` use, over in-memory tables."""

    def __init__(self, *, output_rows: Any = "exact", load_error: Optional[Exception] = None):
        self.tables: Dict[str, Dict[str, Any]] = {}
        self.loads: List[Dict[str, Any]] = []
        self.queries: List[str] = []
        self.clients: List[Dict[str, Any]] = []
        fake = self

        class LoadJobConfig:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class QueryJobConfig(LoadJobConfig):
            pass

        class _Rows:
            def __init__(self, table: Any, limit: Optional[int] = None):
                self._table = table if limit is None else table.slice(0, limit)

            def to_arrow_iterable(self):
                # One "page" per two rows, as tabledata.list pages.
                yield from self._table.to_batches(max_chunksize=2)

            def to_arrow(self):
                return self._table

        class _Job:
            job_id = "job_1"

            def __init__(self, rows=None, values=None):
                self.output_rows = rows
                self._values = values

            def result(self, timeout=None):
                if self._values is None:
                    return self
                return [SimpleNamespace(values=lambda v=self._values: tuple(v))]

        class Client:
            def __init__(self, project=None, credentials=None):
                self.project = project or "adc-default"
                fake.clients.append({"project": project, "credentials": credentials})

            def get_table(self, table_id):
                if table_id not in fake.tables:
                    raise LookupError(f"404 Not found: Table {table_id}")
                t = fake.tables[table_id]
                return SimpleNamespace(schema=t["schema"], num_rows=None, table_id=table_id)

            def list_rows(self, table, max_results=None):
                return _Rows(fake.tables[table.table_id]["data"], max_results)

            def load_table_from_file(self, fh, table_id, job_config=None, location=None):
                data = pq.read_table(io.BytesIO(fh.read()))
                fake.loads.append(
                    {"table": table_id, "location": location, "config": dict(job_config.__dict__)}
                )
                if load_error is not None:
                    raise load_error
                entry = fake.tables[table_id]
                if job_config.write_disposition == "WRITE_TRUNCATE" or not entry["data"].num_rows:
                    entry["data"] = data
                else:
                    entry["data"] = pa.concat_tables([entry["data"], data])
                entry["uploaded"] = data
                if output_rows == "exact":
                    return _Job(data.num_rows)
                return _Job(output_rows if not callable(output_rows) else output_rows(data))

            def query(self, sql, job_config=None, location=None):
                fake.queries.append(sql)
                m = re.search(r"FROM `([^`]+)`\.`([^`]+)`\.`([^`]+)`", sql)
                table_id = ".".join(m.groups())
                return _Job(values=[fake.tables[table_id]["data"].num_rows])

        self.module = SimpleNamespace(
            Client=Client,
            LoadJobConfig=LoadJobConfig,
            QueryJobConfig=QueryJobConfig,
            ScalarQueryParameter=lambda *a: a,
        )

    def create(self, table_id: str, schema: List[_Field], data: Optional[Any] = None) -> None:
        if data is None:
            data = pa.table({f.name: pa.array([], type=pa.string()) for f in schema})
        self.tables[table_id] = {"schema": schema, "data": data}


#: What the fake hands out for ``AnonymousCredentials``: the unit lanes install
#: ``.[dev,local]``, which has no google-auth, so the real class is not imported.
ANONYMOUS = SimpleNamespace(kind="anonymous")


@pytest.fixture
def bq(monkeypatch):
    def install(**kw) -> FakeBigQuery:
        fake = FakeBigQuery(**kw)
        monkeypatch.setattr(_bigquery_load, "_bigquery_module", lambda: fake.module)
        monkeypatch.setattr(
            _bigquery_load, "_anonymous_credentials", lambda: ANONYMOUS, raising=False
        )
        return fake

    return install


# ── The workspace: bronze in BigQuery, silver built from it ─────────────


def _dump(path: Path, doc: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _gcp_binding(dataset: str, table: str, *, project: str = "northwind-demo") -> Dict[str, Any]:
    # The shape make-targets writes: a gs:// staging path beside the table,
    # which the embedded-SQL path used to "land" as a local file.
    return {
        "platform": "gcp",
        "format": "bigquery_table",
        "location": {
            "path": f"gs://northwind-demo-lake/staging/{table}/",
            "project": project,
            "dataset": dataset,
            "table": table,
            "region": "europe-west1",
        },
    }


def _workspace(root: Path, *, silver_binding: Optional[Dict[str, Any]] = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fluid.workspace.yaml").write_text("workspace: {name: t}\n", encoding="utf-8")
    bronze_dir = root / "contracts" / "customer_subscriptions"
    _dump(
        bronze_dir / "contract.fluid.yaml",
        {
            "fluidVersion": "0.7.6",
            "kind": "DataProduct",
            "id": BRONZE,
            "name": "Customer Subscriptions",
            "exposes": [
                {
                    "exposeId": "subscriptions",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "out/customer_subscriptions.parquet"},
                    },
                }
            ],
        },
    )
    _dump(
        bronze_dir / "overlays" / "gcp.yaml",
        {"exposes": [{"binding": _gcp_binding("demo_bronze", "customer_subscriptions")}]},
    )
    silver_dir = root / "contracts" / "subscription_status_summary"
    _dump(silver_dir / "contract.fluid.yaml", _silver_contract())
    _dump(
        silver_dir / "overlays" / "gcp.yaml",
        {
            "exposes": [
                {
                    "binding": silver_binding
                    or _gcp_binding("demo_silver", "subscription_status_summary")
                }
            ]
        },
    )
    return root


def _silver_contract(sql: str = SQL) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": SILVER,
        "name": "Subscription Status Summary",
        "metadata": {"layer": "Silver", "productType": "ADP"},
        "consumes": [{"productId": BRONZE, "exposeId": "subscriptions"}],
        "builds": [
            {
                "id": "summarize_subscription_status",
                "pattern": "embedded-logic",
                "engine": "duckdb",
                "properties": {"sql": sql},
                "outputs": ["status_summary"],
            }
        ],
        "exposes": [
            {
                "exposeId": "status_summary",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/subscription_status_summary.parquet"},
                },
            }
        ],
    }


def _silver_dir(root: Path) -> Path:
    return root / "contracts" / "subscription_status_summary"


def _load(root: Path, env: str = "gcp") -> Dict[str, Any]:
    from fluid_build._contract_loader import load_contract_with_overlay

    return load_contract_with_overlay(
        str(_silver_dir(root) / "contract.fluid.yaml"), env, logging.getLogger("t")
    )


_BRONZE_ROWS = {
    "subscription_id": ["s1", "s2", "s3", "s4", "s5"],
    "product_id": ["p1", "p1", "p1", "p2", "p2"],
    "status": ["active", "active", "suspended", "active", "expired"],
    "created_at": pa.array(
        [1767323045000000 + i * 3_600_000_000 for i in range(5)],
        type=pa.timestamp("us", tz="UTC"),
    ),
}


def _seed(fake: FakeBigQuery) -> None:
    fake.create(
        BRONZE_TABLE,
        _schema(
            ("subscription_id", "STRING"),
            ("product_id", "STRING"),
            ("status", "STRING"),
            ("created_at", "TIMESTAMP"),
        ),
        pa.table(_BRONZE_ROWS),
    )
    fake.create(
        SILVER_TABLE,
        _schema(("product_id", "STRING"), ("status", "STRING"), ("subscription_count", "INTEGER")),
    )


def _gs_files(root: Path) -> List[Path]:
    return [p for p in root.rglob("*") if p.name.startswith("gs:")]


def _build(contract: Dict[str, Any], root: Path) -> int:
    return _execute_embedded_sql_build(
        contract["builds"][0], contract, _silver_dir(root), env="gcp"
    )


# ── Reads ───────────────────────────────────────────────────────────────


def test_a_bigquery_upstream_resolves_to_its_table_not_its_gs_path(bq, tmp_path):
    bq()
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    [r], covered, _ = resolve_consumes(
        contract, contract["builds"][0], _silver_dir(root), env="gcp"
    )
    assert not covered
    assert (r.table, r.platform, r.region) == (BRONZE_TABLE, "gcp", "europe-west1")
    assert r.uri == f"bigquery://{BRONZE_TABLE}"
    assert r.view == "subscriptions"


def test_the_upstream_project_is_resolved_from_the_environment(bq, tmp_path, monkeypatch):
    bq()
    root = _workspace(tmp_path / "ws")
    overlay = root / "contracts" / "customer_subscriptions" / "overlays" / "gcp.yaml"
    _dump(
        overlay,
        {
            "exposes": [
                {
                    "binding": _gcp_binding(
                        "demo_bronze",
                        "customer_subscriptions",
                        project="{{ env.FLUID_DEMO_GCP_PROJECT }}",
                    )
                }
            ]
        },
    )
    monkeypatch.setenv("FLUID_DEMO_GCP_PROJECT", "acme-eu-demo")
    contract = _load(root)
    [r], _, _ = resolve_consumes(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert r.table == "acme-eu-demo.demo_bronze.customer_subscriptions"

    monkeypatch.delenv("FLUID_DEMO_GCP_PROJECT")
    with pytest.raises(ConsumesResolutionError, match="FLUID_DEMO_GCP_PROJECT"):
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root), env="gcp")


def test_the_sql_reads_the_bigquery_rows_and_lands_them_in_bigquery(bq, tmp_path):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    rc = _build(_load(root), root)
    assert rc == 0

    landed = fake.tables[SILVER_TABLE]["data"].to_pylist()
    got = sorted((r["product_id"], r["status"], r["subscription_count"]) for r in landed)
    assert got == [
        ("p1", "active", 2),
        ("p1", "suspended", 1),
        ("p2", "active", 1),
        ("p2", "expired", 1),
    ]
    [load] = fake.loads
    assert load["table"] == SILVER_TABLE
    assert load["location"] == "europe-west1"
    assert load["config"]["write_disposition"] == "WRITE_TRUNCATE"
    assert load["config"]["create_disposition"] == "CREATE_NEVER"
    # Nothing was written to a local file named after the binding's gs:// path.
    assert _gs_files(tmp_path) == []
    # The staged copy of bronze's table is removed after the build.
    assert not list((_silver_dir(root) / ".fluid" / "staging").rglob("inputs/*.parquet"))


def test_the_sql_sees_a_bigquery_timestamp_as_its_utc_wall_clock(bq, tmp_path, monkeypatch):
    """What the same SQL sees on local and aws, where bronze lands naive timestamps."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    fake = bq()
    _seed(fake)
    fake.create(SILVER_TABLE, _schema(("subscription_id", "STRING"), ("created", "STRING")))
    root = _workspace(tmp_path / "ws")
    silver = _silver_dir(root) / "contract.fluid.yaml"
    doc = yaml.safe_load(silver.read_text())
    doc["builds"][0]["properties"]["sql"] = (
        "SELECT subscription_id, CAST(created_at AS VARCHAR) AS created FROM subscriptions "
        "WHERE subscription_id = 's1'"
    )
    _dump(silver, doc)
    assert _build(_load(root), root) == 0
    [row] = fake.tables[SILVER_TABLE]["data"].to_pylist()
    assert row == {"subscription_id": "s1", "created": "2026-01-02 03:04:05"}


def test_a_missing_upstream_table_fails_the_build_naming_it(bq, tmp_path, capsys):
    fake = bq()
    fake.create(SILVER_TABLE, _schema(("product_id", "STRING")))
    root = _workspace(tmp_path / "ws")
    assert _build(_load(root), root) == 1
    assert BRONZE_TABLE in " ".join(capsys.readouterr().out.split())
    assert fake.loads == []


# ── Landing ─────────────────────────────────────────────────────────────


def test_a_bigquery_landing_is_planned_as_a_load_not_a_local_file(bq, tmp_path):
    bq()
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    io_plan = plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert io_plan.landing is None
    assert io_plan.bigquery_landing is not None
    assert io_plan.bigquery_landing.table_id == SILVER_TABLE
    assert io_plan.bigquery_landing.location == "europe-west1"


def test_a_local_upstream_can_still_land_in_bigquery(bq, tmp_path):
    """The local and gcp targets mix: a local bronze file, a BigQuery silver table."""
    fake = bq()
    fake.create(
        SILVER_TABLE,
        _schema(("product_id", "STRING"), ("status", "STRING"), ("subscription_count", "INTEGER")),
    )
    root = _workspace(tmp_path / "ws")
    (root / "contracts" / "customer_subscriptions" / "overlays" / "gcp.yaml").unlink()
    local = root / "contracts" / "customer_subscriptions" / "out" / "customer_subscriptions.parquet"
    local.parent.mkdir(parents=True)
    duckdb.sql(
        "COPY (SELECT * FROM (VALUES ('p1','active'), ('p1','active')) t(product_id, status)) "
        f"TO '{local}' (FORMAT parquet)"
    )
    assert _build(_load(root), root) == 0
    assert fake.tables[SILVER_TABLE]["data"].to_pylist() == [
        {"product_id": "p1", "status": "active", "subscription_count": 2}
    ]


@pytest.mark.parametrize(
    "binding, fragment",
    [
        (
            {"platform": "gcp", "format": "parquet", "location": {"path": "gs://lake/silver/s/"}},
            "gs://",
        ),
        (
            {"platform": "gcp", "format": "parquet", "location": {"bucket": "lake", "path": "s/"}},
            "gcp binding",
        ),
        (
            {"platform": "local", "format": "parquet", "location": {"path": "gs://lake/s.parquet"}},
            "gs://",
        ),
        (
            {"platform": "azure", "format": "parquet", "location": {"path": "abfss://c@a/s/"}},
            "abfss://",
        ),
    ],
)
def test_a_landing_this_path_cannot_write_is_refused_not_written_locally(
    bq, tmp_path, binding, fragment, capsys
):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws", silver_binding=binding)
    contract = _load(root)
    with pytest.raises(EmbeddedSqlLandingError, match=re.escape(fragment)):
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert _build(contract, root) == 1
    assert _gs_files(tmp_path) == [] and not list(tmp_path.rglob("abfss:*"))
    assert fake.loads == []


def test_a_non_inline_sql_build_bound_to_bigquery_is_refused(bq, tmp_path):
    bq()
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    contract["builds"][0] = {"id": "multi_stage", "engine": "sql", "properties": {}}
    assert _build(contract, root) == 1
    assert _gs_files(tmp_path) == []


def test_masking_on_a_bigquery_expose_is_refused_before_any_read(bq, tmp_path):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    contract["exposes"][0]["policy"] = {
        "privacy": {"masking": [{"column": "status", "strategy": "hash"}]}
    }
    with pytest.raises(MaskingNotAppliedError):
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert _build(contract, root) == 1
    assert fake.loads == []


def test_a_second_bigquery_output_is_refused(bq, tmp_path):
    bq()
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    contract["exposes"].append(
        {"exposeId": "extra", "kind": "table", "binding": _gcp_binding("demo_silver", "extra")}
    )
    contract["builds"][0]["outputs"] = ["status_summary", "extra"]
    with pytest.raises(EmbeddedSqlLandingError, match="one (result|table)"):
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")


def test_a_failed_load_fails_the_build(bq, tmp_path, capsys):
    fake = bq(load_error=RuntimeError("quota exceeded"))
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    assert _build(_load(root), root) == 1
    # The load was attempted (the build got past reading and the SQL), it
    # failed loudly, and the table was left as it was.
    [load] = fake.loads
    assert load["table"] == SILVER_TABLE
    out = " ".join(capsys.readouterr().out.split())
    assert f"BigQuery load into {SILVER_TABLE} failed" in out and "quota exceeded" in out
    assert fake.tables[SILVER_TABLE]["data"].num_rows == 0


def test_a_short_load_fails_the_build(bq, tmp_path, capsys):
    fake = bq(output_rows=lambda data: data.num_rows - 1)
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    assert _build(_load(root), root) == 1
    [load] = fake.loads
    assert load["table"] == SILVER_TABLE
    out = " ".join(capsys.readouterr().out.split())
    assert f"loaded 3 rows into {SILVER_TABLE}, but the landed file holds 4" in out


# ── The load itself: timestamps, and a job with no row count ────────────


def _naive_parquet(path: Path) -> Path:
    duckdb.sql(
        "COPY (SELECT 1 AS id, TIMESTAMP '2026-01-02 03:04:05' AS created_at, "
        "TIMESTAMP '2026-01-02 03:04:05' AS local_time) "
        f"TO '{path}' (FORMAT parquet)"
    )
    return path


_TS_TABLE = "p.d.events"
_TS_TARGET = {"project": "p", "dataset": "d", "table": "events", "location": "EU"}


def _ts_fake(bq) -> FakeBigQuery:
    fake = bq()
    fake.create(
        _TS_TABLE,
        _schema(("id", "INTEGER"), ("created_at", "TIMESTAMP"), ("local_time", "DATETIME")),
    )
    return fake


def _timestamp_logical(table: Any, column: str) -> Any:
    return table.schema.field(column).type


def test_a_timestamp_column_is_loaded_utc_adjusted(bq, tmp_path):
    """BigQuery reads a Parquet timestamp with isAdjustedToUTC=false as DATETIME."""
    fake = _ts_fake(bq)
    staged = _naive_parquet(tmp_path / "events.parquet")
    facts = _bigquery_load.load_file(
        str(staged),
        _TS_TARGET,
        mode="full_refresh",
        sink_format="parquet",
        expected_rows=1,
        logger=logging.getLogger("t"),
    )
    assert facts["rows"] == 1
    uploaded = fake.tables[_TS_TABLE]["uploaded"]
    # The TIMESTAMP column is UTC-adjusted; the DATETIME one stays naive.
    assert _timestamp_logical(uploaded, "created_at") == pa.timestamp("us", tz="UTC")
    assert _timestamp_logical(uploaded, "local_time") == pa.timestamp("us")
    assert uploaded.column("created_at")[0].value == 1767323045000000
    # The build's own landed file is left as it was, and the copy is removed.
    assert pq.read_schema(staged).field("created_at").type == pa.timestamp("us")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["events.parquet"]


def test_a_job_with_no_row_count_is_checked_by_counting_the_table(bq, tmp_path):
    """The goccy emulator's load jobs carry no outputRows; that is not 0 rows loaded."""
    fake = bq(output_rows=None)
    fake.create(_TS_TABLE, _schema(("id", "INTEGER")))
    staged = tmp_path / "events.parquet"
    duckdb.sql(f"COPY (SELECT range AS id FROM range(3)) TO '{staged}' (FORMAT parquet)")
    facts = _bigquery_load.load_file(
        str(staged),
        _TS_TARGET,
        mode="full_refresh",
        sink_format="parquet",
        expected_rows=3,
        logger=logging.getLogger("t"),
    )
    assert (facts["rows"], facts["rows_from"]) == (3, "count_after_load")
    assert any("COUNT(*)" in q for q in fake.queries)


def test_a_count_after_load_that_disagrees_fails(bq, tmp_path):
    fake = bq(output_rows=None)
    fake.create(_TS_TABLE, _schema(("id", "INTEGER")))
    staged = tmp_path / "events.parquet"
    duckdb.sql(f"COPY (SELECT range AS id FROM range(3)) TO '{staged}' (FORMAT parquet)")
    with pytest.raises(_bigquery_load.BigQueryLoadError, match="loaded 3 rows"):
        _bigquery_load.load_file(
            str(staged),
            _TS_TARGET,
            mode="full_refresh",
            sink_format="parquet",
            expected_rows=4,
            logger=logging.getLogger("t"),
        )


def test_an_append_with_no_row_count_is_refused_off_an_emulator(bq, tmp_path, monkeypatch):
    monkeypatch.delenv("BIGQUERY_EMULATOR_HOST", raising=False)
    fake = bq(output_rows=None)
    fake.create(_TS_TABLE, _schema(("id", "INTEGER")))
    staged = tmp_path / "events.parquet"
    duckdb.sql(f"COPY (SELECT range AS id FROM range(3)) TO '{staged}' (FORMAT parquet)")
    with pytest.raises(_bigquery_load.BigQueryLoadError, match="no output row count"):
        _bigquery_load.load_file(
            str(staged),
            _TS_TARGET,
            mode="incremental_append",
            sink_format="parquet",
            expected_rows=3,
            logger=logging.getLogger("t"),
        )


def test_an_append_on_an_emulator_is_held_to_the_rows_it_added(bq, tmp_path, monkeypatch):
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "http://127.0.0.1:9")
    fake = bq(output_rows=None)
    fake.create(_TS_TABLE, _schema(("id", "INTEGER")), pa.table({"id": [100, 101]}))
    staged = tmp_path / "events.parquet"
    duckdb.sql(f"COPY (SELECT range AS id FROM range(3)) TO '{staged}' (FORMAT parquet)")
    facts = _bigquery_load.load_file(
        str(staged),
        _TS_TARGET,
        mode="incremental_append",
        sink_format="parquet",
        expected_rows=3,
        logger=logging.getLogger("t"),
    )
    assert (facts["rows"], facts["rows_from"]) == (3, "count_after_load")
    assert fake.tables[_TS_TABLE]["data"].num_rows == 5


# ── The client: anonymous against an emulator, ADC otherwise ────────────


def test_an_emulator_client_is_handed_anonymous_credentials(bq, monkeypatch):
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "http://127.0.0.1:9")
    fake = bq()
    _bigquery_load.bigquery_client(fake.module, "forge-emulated")
    assert fake.clients == [{"project": "forge-emulated", "credentials": ANONYMOUS}]


def test_an_emulator_client_sends_no_credentials(monkeypatch):
    bigquery = pytest.importorskip("google.cloud.bigquery")
    from google.auth.credentials import AnonymousCredentials

    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", "http://127.0.0.1:19999")
    client = _bigquery_load.bigquery_client(bigquery, "forge-emulated")
    assert isinstance(client._credentials, AnonymousCredentials)
    assert client._connection.API_BASE_URL == "http://127.0.0.1:19999"
    assert client.project == "forge-emulated"


def test_without_an_emulator_the_client_is_the_plain_adc_one(bq, monkeypatch):
    monkeypatch.delenv("BIGQUERY_EMULATOR_HOST", raising=False)
    fake = bq()
    _bigquery_load.bigquery_client(fake.module, "acme-eu")
    assert fake.clients == [{"project": "acme-eu", "credentials": None}]


def test_the_plan_prints_the_bigquery_read_and_load(bq, tmp_path, capsys):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    assert _build(_load(root), root) == 0
    out = " ".join(capsys.readouterr().out.split())  # the console wraps long lines
    assert f"bigquery://{BRONZE_TABLE}" in out
    assert f"lands BigQuery table {SILVER_TABLE}" in out
    assert f"read 5 row(s) from BigQuery table {BRONZE_TABLE}" in out
    assert f"loaded 4 row(s) into BigQuery table {SILVER_TABLE}" in out


# ── Sovereignty: the locations the reads and the load actually use ─────
#
# PR review, measured on this branch before the fix: an EU-only silver whose
# gcp overlay named no region planned its read from europe-west1 and its load
# into "US" (the IaC default), and the build loaded there, rc 0. On main the
# same build stopped earlier (UnreadableBindingError), so the copy was new.

_EU_ONLY = {
    "jurisdiction": "EU",
    "allowedRegions": ["eu-north-1", "eu-west-1", "europe-west1"],
    "dataResidency": True,
    "crossBorderTransfer": False,
}


def _sovereign(root: Path, sovereignty: Dict[str, Any]) -> Dict[str, Any]:
    contract = _load(root)
    contract["sovereignty"] = dict(sovereignty)
    return contract


def _no_region(dataset: str, table: str) -> Dict[str, Any]:
    binding = _gcp_binding(dataset, table)
    del binding["location"]["region"]
    return binding


def test_a_sovereign_landing_with_no_region_is_refused_before_any_read(bq, tmp_path):
    fake = bq()
    _seed(fake)
    root = _workspace(
        tmp_path / "ws", silver_binding=_no_region("demo_silver", "subscription_status_summary")
    )
    contract = _sovereign(root, _EU_ONLY)
    with pytest.raises(EmbeddedSqlLandingError, match="names no region") as err:
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert err.value.code == "EmbeddedSqlSovereigntyError"
    assert "not in allowedRegions" in err.value.why  # US is not an allowed region either
    assert _build(contract, root) == 1
    assert fake.loads == [] and fake.queries == []


def test_a_sovereign_upstream_with_no_region_is_refused(bq, tmp_path):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    overlay = root / "contracts" / "customer_subscriptions" / "overlays" / "gcp.yaml"
    _dump(overlay, {"exposes": [{"binding": _no_region("demo_bronze", "customer_subscriptions")}]})
    contract = _sovereign(root, _EU_ONLY)
    with pytest.raises(EmbeddedSqlLandingError) as err:
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert err.value.code == "EmbeddedSqlSovereigntyError"
    assert f"{BRONZE_TABLE} names no region" in err.value.why
    # Read from US, landed in europe-west1, crossBorderTransfer false.
    assert "crossBorderTransfer is false" in err.value.why
    assert _build(contract, root) == 1 and fake.loads == []


@pytest.mark.parametrize(
    "region, fragment",
    [
        ("us-central1", "not in allowedRegions"),
        ("europe-west2", "is in UK, not the required jurisdiction EU"),
    ],
)
def test_a_sovereign_landing_outside_the_contract_is_refused(bq, tmp_path, region, fragment):
    bq()
    binding = _gcp_binding("demo_silver", "subscription_status_summary")
    binding["location"]["region"] = region
    root = _workspace(tmp_path / "ws", silver_binding=binding)
    sovereignty = dict(_EU_ONLY)
    if region == "europe-west2":
        sovereignty["allowedRegions"] = [*_EU_ONLY["allowedRegions"], "europe-west2"]
    contract = _sovereign(root, sovereignty)
    with pytest.raises(EmbeddedSqlLandingError) as err:
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert err.value.code == "EmbeddedSqlSovereigntyError"
    assert fragment in err.value.why


def test_an_eu_read_and_an_eu_load_meet_the_contract(bq, tmp_path, capsys):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    contract = _sovereign(root, _EU_ONLY)
    io_plan = plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert io_plan.warnings == []
    assert _build(contract, root) == 0
    assert [load["location"] for load in fake.loads] == ["europe-west1"]
    assert "sovereignty" not in capsys.readouterr().out


def test_the_eu_multi_region_counts_as_eu(bq, tmp_path):
    bq()
    binding = _gcp_binding("demo_silver", "subscription_status_summary")
    binding["location"]["region"] = "EU"
    root = _workspace(tmp_path / "ws", silver_binding=binding)
    contract = _sovereign(root, {**_EU_ONLY, "allowedRegions": ["EU", "europe-west1"]})
    io_plan = plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert io_plan.bigquery_landing.location == "EU" and io_plan.warnings == []


def test_an_advisory_contract_warns_and_builds(bq, tmp_path, capsys):
    fake = bq()
    _seed(fake)
    root = _workspace(
        tmp_path / "ws", silver_binding=_no_region("demo_silver", "subscription_status_summary")
    )
    contract = _sovereign(root, {**_EU_ONLY, "enforcementMode": "advisory"})
    io_plan = plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert any("names no region" in w for w in io_plan.warnings)
    assert _build(contract, root) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "sovereignty: expose status_summary: BigQuery table" in out


def test_a_contract_without_sovereignty_is_unchanged(bq, tmp_path):
    """No sovereignty block, no check: the default location stays the IaC's."""
    bq()
    root = _workspace(
        tmp_path / "ws", silver_binding=_no_region("demo_silver", "subscription_status_summary")
    )
    contract = _load(root)
    io_plan = plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert io_plan.bigquery_landing.location == "US" and io_plan.warnings == []


# ── A landing that is one of the build's own inputs ────────────────────
#
# PR review, measured before the fix: silver's overlay named bronze's table,
# and the SQL kept the active rows. The build returned rc 0 and bronze went
# from 5 rows to 3: a WRITE_TRUNCATE of another product's table, which no
# --allow-data-loss gate sees.


@pytest.mark.parametrize(
    "dataset, table, project",
    [
        ("demo_bronze", "customer_subscriptions", "northwind-demo"),
        ("Demo_Bronze", "Customer_Subscriptions", "NORTHWIND-DEMO"),
        # A project left to the client may be the upstream's: refused, not assumed.
        ("demo_bronze", "customer_subscriptions", ""),
    ],
)
def test_a_landing_into_an_input_table_is_refused(bq, tmp_path, dataset, table, project):
    fake = bq()
    _seed(fake)
    binding = _gcp_binding(dataset, table, project=project)
    if not project:
        del binding["location"]["project"]
    root = _workspace(tmp_path / "ws", silver_binding=binding)
    silver = _silver_dir(root) / "contract.fluid.yaml"
    doc = yaml.safe_load(silver.read_text())
    doc["builds"][0]["properties"]["sql"] = "SELECT * FROM subscriptions WHERE status = 'active'"
    _dump(silver, doc)
    contract = _load(root)
    with pytest.raises(EmbeddedSqlLandingError, match="which consumes bronze"):
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert _build(contract, root) == 1
    assert fake.loads == []
    assert fake.tables[BRONZE_TABLE]["data"].num_rows == 5


def test_a_landing_in_another_project_with_the_same_names_is_not_an_input(bq, tmp_path):
    bq()
    binding = _gcp_binding("demo_bronze", "customer_subscriptions", project="acme-silver")
    root = _workspace(tmp_path / "ws", silver_binding=binding)
    contract = _load(root)
    io_plan = plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert io_plan.bigquery_landing.table_id == "acme-silver.demo_bronze.customer_subscriptions"


def test_an_s3_landing_inside_an_input_prefix_is_refused(tmp_path):
    """The AWS form of the same mistake: the object would become rows of bronze's table."""
    aws = {
        "platform": "aws",
        "format": "parquet",
        "location": {"bucket": "lake", "path": "bronze/customer_subscriptions/"},
    }
    root = _workspace(tmp_path / "ws", silver_binding=aws)
    for name in ("customer_subscriptions", "subscription_status_summary"):
        (root / "contracts" / name / "overlays" / "gcp.yaml").rename(
            root / "contracts" / name / "overlays" / "aws.yaml"
        )
    _dump(
        root / "contracts" / "customer_subscriptions" / "overlays" / "aws.yaml",
        {"exposes": [{"binding": aws}]},
    )
    contract = _load(root, env="aws")
    with pytest.raises(EmbeddedSqlLandingError, match="inside s3://lake/bronze/"):
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="aws")


# ── Further outputs this path never lands ───────────────────────────────
#
# PR review, measured before the fix: a second output bound to a GCS prefix
# planned without complaint, and the build returned rc 0 with nothing written
# for it. Only a second BigQuery output was refused.


@pytest.mark.parametrize(
    "binding",
    [
        {
            "platform": "gcp",
            "format": "parquet",
            "location": {"bucket": "lake", "path": "gs://l/x/"},
        },
        {"platform": "aws", "format": "parquet", "location": {"bucket": "lake", "path": "x/"}},
        {"platform": "local", "format": "parquet", "location": {"path": "s3://lake/x/"}},
        _gcp_binding("demo_silver", "extra"),
    ],
)
def test_a_further_remote_output_is_refused(bq, tmp_path, binding):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    contract["exposes"].append({"exposeId": "extra", "kind": "table", "binding": binding})
    contract["builds"][0]["outputs"] = ["status_summary", "extra"]
    # A second BigQuery output was already refused ("loads its result into one
    # table"); the others were planned and silently not landed.
    with pytest.raises(EmbeddedSqlLandingError, match="one (result|table)"):
        plan_embedded_sql_io(contract, contract["builds"][0], _silver_dir(root), env="gcp")
    assert _build(contract, root) == 1 and fake.loads == []


def test_a_further_local_output_is_warned_about_not_dropped_silently(bq, tmp_path, capsys):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    contract["exposes"].append(
        {
            "exposeId": "extra",
            "kind": "table",
            "binding": {"platform": "local", "format": "parquet", "location": {"path": "x.pq"}},
        }
    )
    contract["builds"][0]["outputs"] = ["status_summary", "extra"]
    assert _build(contract, root) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "extra is not written by this build" in out
    assert [load["table"] for load in fake.loads] == [SILVER_TABLE]


# ── The run record fluid verify holds the table to ──────────────────────


def _records(root: Path) -> List[Dict[str, Any]]:
    import json

    runs = _silver_dir(root) / ".fluid" / "runs" / SILVER / "summarize_subscription_status" / "runs"
    return [json.loads(p.read_text()) for p in sorted(runs.glob("*.json"))]


def test_a_bigquery_landing_is_recorded_as_a_run(bq, tmp_path):
    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    assert _build(_load(root), root) == 0
    [record] = _records(root)
    assert record["state"] == "succeeded" and record["records_total"] == 4
    load = record["facets"]["bigquery_load"]
    assert (load["table"], load["rows"]) == (SILVER_TABLE, 4)
    assert record["facets"]["landed"]["mode"] == "full_refresh"
    assert record["facets"]["landed"]["rows_from"] == "write"
    assert fake.tables[SILVER_TABLE]["data"].num_rows == 4


def test_a_failed_load_is_recorded_as_a_failed_run_without_a_count(bq, tmp_path):
    fake = bq(load_error=RuntimeError("quota exceeded"))
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    assert _build(_load(root), root) == 1
    [record] = _records(root)
    assert record["state"] == "failed" and "bigquery_load" not in record["facets"]
    assert "quota exceeded" in record["error"]


def test_the_recorded_run_is_the_count_the_bigquery_verifier_holds_the_table_to(bq, tmp_path):
    """Writer and reader agree: the run the build records is the one verify compares."""
    from fluid_build.cli._verify_athena import _landed_rows
    from fluid_build.cli._verify_bigquery import _loaded_into

    fake = bq()
    _seed(fake)
    root = _workspace(tmp_path / "ws")
    contract = _load(root)
    assert _build(contract, root) == 0
    landed, info = _landed_rows(
        contract,
        "status_summary",
        _silver_dir(root),
        SILVER_TABLE,
        wrote_into=_loaded_into,
        embedded_sql_records=True,
    )
    assert (landed, info["rule"], info["build_id"]) == (4, "equal", "summarize_subscription_status")
