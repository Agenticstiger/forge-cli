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

"""A BigQuery binding is built locally, then loaded into the table the IaC made.

What these pin, each of which was wrong before: the build wrote Parquet to the
binding's ``gs://`` path, which DuckDB cannot authenticate to with Application
Default Credentials, and nothing ever loaded the table ``tofu apply`` created.
Now the file stages under ``.fluid/staging``, one load job moves it into the
declared table with the table's own schema, and every way that can go wrong
fails the build instead of reporting success.

The BigQuery client is faked through ``_bigquery_load._bigquery_module``, so
these run without the ``gcp`` extra, which CI does not install.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from fluid_build.build_runners import _bigquery_load

# The iac-tests job collects every file with no duckdb installed.
duckdb = pytest.importorskip("duckdb")
from fluid_build.build_runners.duckdb.runner import execute_duckdb_build


class _FakeBigQuery:
    """The four names ``load_file`` uses, recording every call."""

    def __init__(self, *, rows_delta: int = 0, load_error: Exception = None, missing: bool = False):
        self.calls: List[tuple] = []
        self.uploaded: bytes = b""
        fake = self

        class LoadJobConfig:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Job:
            job_id = "job_1"

            def __init__(self, rows):
                self.output_rows = rows

            def result(self):
                return self

        class Client:
            def __init__(self, project=None):
                self.project = project or "adc-default"
                fake.calls.append(("Client", project))

            def get_table(self, table_id):
                fake.calls.append(("get_table", table_id))
                if missing:
                    raise LookupError(f"Not found: Table {table_id}")
                return SimpleNamespace(schema=["the-declared-schema"])

            def load_table_from_file(self, fh, table_id, job_config=None, location=None):
                fake.uploaded = fh.read()
                fake.calls.append(("load", table_id, location, dict(job_config.__dict__)))
                if load_error is not None:
                    raise load_error
                rows = duckdb.sql("SELECT * FROM read_parquet($p)", params={"p": fake._tmp()})
                return _Job(len(rows.fetchall()) + rows_delta)

        self.module = SimpleNamespace(Client=Client, LoadJobConfig=LoadJobConfig)

    def _tmp(self) -> str:
        p = Path(self._dir) / "uploaded.parquet"
        p.write_bytes(self.uploaded)
        return str(p)


@pytest.fixture
def bq(monkeypatch, tmp_path):
    def install(**kw) -> _FakeBigQuery:
        fake = _FakeBigQuery(**kw)
        fake._dir = tmp_path
        monkeypatch.setattr(_bigquery_load, "_bigquery_module", lambda: fake.module)
        return fake

    return install


def _contract(
    tmp_path: Path, location: Dict[str, Any], mode: str = "full_refresh"
) -> Dict[str, Any]:
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    (src / "orders.csv").write_text("id,amount\n1,10.5\n2,20.0\n3,4.25\n", encoding="utf-8")
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.orders",
        "name": "Orders",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@co.example"}},
        "builds": [
            {
                "id": "ingest_orders",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": [mode],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": str(src / "*.csv")},
                        "mode": mode,
                        "reader": {"format": "csv", "options": {"header": True}},
                    },
                    "sink": {"format": "parquet"},
                },
                "outputs": ["orders"],
            }
        ],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "gcp", "format": "bigquery_table", "location": location},
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


_LOC = {
    "project": "acme-eu",
    "dataset": "bronze",
    "table": "orders",
    "region": "europe-west1",
    "path": "gs://acme-lake/staging/orders/",
}


def _run(contract, tmp_path) -> int:
    return execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)


def test_the_built_file_is_loaded_into_the_declared_table(bq, tmp_path):
    fake = bq()
    assert _run(_contract(tmp_path, _LOC), tmp_path) == 0
    assert ("Client", "acme-eu") in fake.calls
    assert ("get_table", "acme-eu.bronze.orders") in fake.calls
    (load,) = [c for c in fake.calls if c[0] == "load"]
    _, table_id, location, cfg = load
    assert table_id == "acme-eu.bronze.orders"
    assert location == "europe-west1"
    assert cfg["source_format"] == "PARQUET"
    assert cfg["write_disposition"] == "WRITE_TRUNCATE"
    assert cfg["create_disposition"] == "CREATE_NEVER", "the table is the IaC's"
    assert cfg["schema"] == ["the-declared-schema"], "the table's own schema, not the file's"
    staged = tmp_path / ".fluid" / "staging" / "ingest_orders" / "orders.parquet"
    assert staged.read_bytes() == fake.uploaded
    assert not (tmp_path / "out" / "orders.parquet").exists(), "out/ is the local target's"


def test_the_project_comes_from_the_environment_when_the_binding_names_none(
    bq, tmp_path, monkeypatch
):
    monkeypatch.setenv("GOOGLE_PROJECT", "from-env")
    fake = bq()
    loc = {k: v for k, v in _LOC.items() if k != "project"}
    assert _run(_contract(tmp_path, loc), tmp_path) == 0
    assert ("Client", "from-env") in fake.calls


def test_a_short_load_fails_the_build(bq, tmp_path, caplog):
    bq(rows_delta=-1)
    assert _run(_contract(tmp_path, _LOC), tmp_path) != 0
    assert "loaded 2 rows into acme-eu.bronze.orders, but the landed file holds 3" in caplog.text


def test_a_failed_load_fails_the_build(bq, tmp_path, caplog):
    bq(load_error=PermissionError("403 Access Denied"))
    assert _run(_contract(tmp_path, _LOC), tmp_path) != 0
    assert "bigquery load: PermissionError: 403 Access Denied" in caplog.text


def test_a_missing_table_fails_the_build(bq, tmp_path, caplog):
    fake = bq(missing=True)
    assert _run(_contract(tmp_path, _LOC), tmp_path) != 0
    assert "Not found: Table acme-eu.bronze.orders" in caplog.text
    assert not [
        c for c in fake.calls if c[0] == "load"
    ], "never load into a table apply did not make"


def test_an_unsupported_mode_is_refused_before_any_work(bq, tmp_path, caplog):
    fake = bq()
    assert _run(_contract(tmp_path, _LOC, mode="incremental_merge"), tmp_path) != 0
    assert "this build is 'incremental_merge'" in caplog.text
    assert fake.calls == [], "refused before a client was ever made"
    assert not (tmp_path / ".fluid" / "staging").exists(), "and before anything landed"


def test_a_missing_gcp_extra_is_refused_before_any_work(monkeypatch, tmp_path, caplog):
    def missing():
        raise _bigquery_load.BigQueryLoadError(_bigquery_load._MISSING_EXTRA)

    monkeypatch.setattr(_bigquery_load, "_bigquery_module", missing)
    assert _run(_contract(tmp_path, _LOC), tmp_path) != 0
    assert "data-product-forge[gcp]" in caplog.text
    assert not (tmp_path / ".fluid" / "staging").exists()


@pytest.mark.parametrize(
    "mode, sink, streams, fragment",
    [
        ("full_refresh", "parquet", 2, "exactly one stream"),
        ("full_refresh", "csv", 1, "sink format"),
        ("cdc", "parquet", 1, "modes"),
    ],
)
def test_unsupported_shapes_are_named(bq, mode, sink, streams, fragment):
    bq()
    assert fragment in _bigquery_load.unsupported_reason(mode, sink, streams)


def test_a_local_binding_never_touches_bigquery(bq, tmp_path):
    fake = bq()
    contract = _contract(tmp_path, _LOC)
    out = tmp_path / "out" / "orders.parquet"
    contract["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "parquet",
        "location": {"path": str(out)},
    }
    assert _run(contract, tmp_path) == 0
    assert fake.calls == []
    assert out.exists()


def _customers_build(tmp_path: Path) -> Dict[str, Any]:
    src = tmp_path / "in_customers"
    src.mkdir(exist_ok=True)
    (src / "customers.csv").write_text("id,name\n1,Ada\n2,Lin\n", encoding="utf-8")
    return {
        "id": "ingest_customers",
        "pattern": "acquisition",
        "engine": "duckdb",
        "capabilities": ["full_refresh"],
        "properties": {
            "source": {
                "kind": "filesystem",
                "connection": {"uri": str(src / "*.csv")},
                "mode": "full_refresh",
                "reader": {"format": "csv", "options": {"header": True}},
            },
            "sink": {"format": "parquet"},
        },
        "outputs": ["customers"],
    }


def test_each_build_loads_its_own_table(bq, tmp_path):
    """The target was ``exposes[0]`` whatever the build, so the second build of
    a two-build contract truncated the FIRST build's table with its own rows."""
    fake = bq()
    contract = _contract(tmp_path, _LOC)
    contract["builds"].append(_customers_build(tmp_path))
    contract["exposes"].append(
        {
            "exposeId": "customers",
            "kind": "table",
            "binding": {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {**_LOC, "table": "customers", "path": "gs://acme-lake/c/"},
            },
            "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
        }
    )
    assert execute_duckdb_build(contract["builds"][1], contract, tmp_path, dry_run=False) == 0
    loads = [c[1] for c in fake.calls if c[0] == "load"]
    assert loads == ["acme-eu.bronze.customers"]


def test_an_unrelated_first_expose_does_not_capture_the_build(bq, tmp_path):
    """A local expose listed first used to take the rows, and the BigQuery
    table the build names was never loaded, with the build reporting success."""
    fake = bq()
    contract = _contract(tmp_path, _LOC)
    decoy = tmp_path / "decoy" / "decoy.parquet"
    contract["exposes"].insert(
        0,
        {
            "exposeId": "decoy",
            "kind": "table",
            "binding": {"platform": "local", "format": "parquet", "location": {"path": str(decoy)}},
            "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
        },
    )
    assert _run(contract, tmp_path) == 0
    assert [c[1] for c in fake.calls if c[0] == "load"] == ["acme-eu.bronze.orders"]
    assert not decoy.exists()
