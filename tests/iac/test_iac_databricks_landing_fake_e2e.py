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

"""``fluid apply`` creates the table, the acquisition build lands bronze into it, against a stand-in.

The real ``fluid`` CLI, ``tofu`` and the real ``databricks-sdk`` run in-process; only the workspace
is ``_fake_databricks.FakeDatabricks``. A contract exposes a managed Delta table that a DuckDB
acquisition build writes from a local CSV, with ``policy.privacy.masking`` on one column, a column
restriction and a row filter:

* ``fluid apply`` accepts the masking (the runner applies it) and creates the table, its grants, its
  mask and its row filter;
* ``fluid apply --mode amend-and-build`` then hashes the column, writes Parquet locally, uploads it
  to ``$DATABRICKS_STAGING_VOLUME`` through the Files API and runs ``INSERT OVERWRITE ... read_files``
  on the warehouse: the rows are in the table with the column hashed, the staged files are deleted,
  and the table keeps its definition, grants, row filter and mask (no statement but the load);
* a second run overwrites the rows, it does not append;
* a plan afterwards is empty: loading data does not drift the table.

Not proven here: what real Databricks accepts of ``read_files``, ``INSERT OVERWRITE`` over a managed
table that has a mask and a row filter, or the Files API's limits; that is the manual live tier.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from fluid_build.build_runners import _masking as masking
from fluid_build.iac import runner

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    write_contract,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("databricks.sdk", reason="the [databricks] extra")

TABLE = "main.sales.orders"
VOLUME = "main.staging.landing"
SALT = "e2e-landing-salt-0123456789"
FIRST: List[Tuple[int, float, str]] = [(1, 10.5, "emea"), (2, 20.0, "apac"), (3, 4.25, "emea")]
SECOND: List[Tuple[int, float, str]] = [(7, 1.5, "amer"), (8, 2.5, "amer")]


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    server.add_volume(VOLUME)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=plugin_cache,
    )
    monkeypatch.setenv("DATABRICKS_STAGING_VOLUME", "/Volumes/" + VOLUME.replace(".", "/"))
    monkeypatch.setenv(masking.DEFAULT_SALT_ENV, SALT)
    # A directory of its own: the runner refuses a contract directory that holds more than the
    # product (here the test's HOME and credential file), as it should.
    product = tmp_path / "product"
    product.mkdir()
    return product


def _csv(root: Path, rows: List[Tuple[int, float, str]]) -> Path:
    path = root / "orders.csv"
    path.write_text(
        "id,amount,region\n" + "".join(f"{i},{a},{r}\n" for i, a, r in rows), encoding="utf-8"
    )
    return path


def _contract(root: Path) -> Dict[str, Any]:
    contract = make_contract()
    expose = contract["exposes"][0]
    # varchar, not string: the run-time schema gate compares the declared type with DuckDB's own
    for column in expose["contract"]["schema"]:
        if column["type"] == "string":
            column["type"] = "varchar"
    expose["policy"]["privacy"] = {"masking": [{"column": "region", "strategy": "hash"}]}
    expose["policy"]["authz"]["columnRestrictions"] = [
        {"principal": "group:analysts", "columns": ["amount"], "access": "deny"}
    ]
    expose["policy"]["authz"]["rowFilters"] = [{"principal": "group:analysts", "where": "id > 0"}]
    contract["builds"] = [
        {
            "id": "ingest_orders",
            "pattern": "acquisition",
            "engine": "duckdb",
            "properties": {
                "source": {
                    "kind": "filesystem",
                    "mode": "full_refresh",
                    "connection": {"uri": str(root / "orders.csv")},
                    "reader": {"format": "csv", "options": {"header": True}},
                    "streams": ["orders"],
                },
                "sink": {"format": "parquet"},
            },
            "outputs": ["orders"],
        }
    ]
    return contract


def _sha(value: str) -> str:
    return hashlib.sha256((SALT + value).encode("utf-8")).hexdigest()


def _by_id(fake: FakeDatabricks) -> List[Dict[str, Any]]:
    return sorted(fake.rows(TABLE), key=lambda r: r["id"])


def test_apply_creates_the_table_then_the_build_lands_masked_bronze_in_it(workspace, fake, capfd):
    _csv(workspace, FIRST)
    path = write_contract(workspace, _contract(workspace))

    # 1. apply: the masking is accepted (the runner applies it); the table, grants, mask and filter exist
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "databricks-governance-unmapped" not in out
    assert fake.rows(TABLE) == [], "apply creates the table empty"
    assert fake.tables[TABLE]["row_filter"], "the row filter is bound"
    assert any(c.get("mask") for c in fake.tables[TABLE]["columns"]), "and the column mask"
    grants = dict(fake.grants[("table", TABLE)])
    definition_before = repr(fake.tables[TABLE])
    sql_before = len(fake.sql_log)

    # 2. the build: hash at landing, upload, INSERT OVERWRITE, delete the staged files
    rc, out = apply_contract(capfd, path, "--mode", "amend-and-build")
    assert rc == 0, out
    rows = _by_id(fake)
    assert [(r["id"], r["amount"]) for r in rows] == [(i, a) for i, a, _ in FIRST]
    assert [r["region"] for r in rows] == [_sha(r) for _, _, r in FIRST]
    assert "emea" not in repr(fake.rows(TABLE)), "the cleartext never left the machine"
    assert fake.volume_files() == [], "no landed data is left in the volume"
    uploads = [p for m, p in fake.file_ops if m == "PUT"]
    assert len(uploads) == 1 and uploads[0].startswith(
        "/Volumes/main/staging/landing/dbx_fake_orders/"
    )
    assert not (workspace / "out").exists() or list((workspace / "out").glob("*")) == []
    assert fake.unknown == []

    # the table keeps its definition, grants, row filter and mask: the load is the only statement
    assert repr(fake.tables[TABLE]) == definition_before
    assert dict(fake.grants[("table", TABLE)]) == grants
    assert fake.replaced == []
    after = fake.sql_log[sql_before:]
    loads = [s for s in after if "INSERT OVERWRITE" in s]
    assert len(loads) == 1 and "read_files(" in loads[0]
    assert [s for s in after if "INSERT OVERWRITE" not in s] == [], "nothing else ran"

    # 3. loading data does not drift the table
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out


def test_a_second_run_replaces_the_rows(workspace, fake, capfd):
    _csv(workspace, FIRST)
    path = write_contract(workspace, _contract(workspace))
    assert apply_contract(capfd, path)[0] == 0
    assert apply_contract(capfd, path, "--mode", "amend-and-build")[0] == 0
    assert len(fake.rows(TABLE)) == 3

    _csv(workspace, SECOND)
    marker = fake.tables[TABLE]["marker"]
    rc, out = apply_contract(capfd, path, "--mode", "amend-and-build")
    assert rc == 0, out
    assert [r["id"] for r in _by_id(fake)] == [7, 8], "overwritten, not appended"
    assert fake.volume_files() == []
    assert fake.tables[TABLE]["marker"] == marker, "the table was never replaced"
