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

"""Three products, one contract each, on the local target and on GCP (bigquery-emulator).

Bronze acquires a file and lands it with ``msisdn`` hashed; silver aggregates
bronze with an embedded-SQL build on DuckDB; gold joins bronze to silver. Each
contract is written once, with a ``gcp`` overlay that changes nothing but
``exposes[0].binding`` (a ``bigquery_table``, with the ``gs://`` staging path
the demo's make-targets forces into every overlay). The chain runs twice:

* ``--env local``: every product lands a local Parquet file;
* ``--env gcp``: bronze is loaded into BigQuery, and silver and gold read
  their upstreams FROM BigQuery and load their results INTO BigQuery.

and each gcp table must hold exactly the rows its local file holds. Then
``fluid verify --env gcp --strict`` passes on bronze, and fails once one
``msisdn`` in the table is overwritten with a cleartext number.

What stands in for ``fluid apply``'s OpenTofu step: the datasets and tables
are created in the emulator from the module forge-cli's own GCP emitter
writes for each contract (same ids, same schema JSON). ``tofu apply`` itself
cannot run here: terraform-provider-google 6.50.0 panics reading back a
goccy dataset (nil interface conversion in resourceBigQueryDatasetRead),
measured. Everything after it is ``run_builds_from_args``, the function
``fluid apply --mode amend-and-build`` calls, and ``fluid verify``'s ``run``.

What no emulator can prove, and this does not claim: that real BigQuery
accepts these loads (the goccy emulator does not check the Parquet
timestamp's ``isAdjustedToUTC`` against the column type; the unit tests pin
that the file sent is UTC-adjusted), IAM on real datasets, Workload Identity
Federation, and the Storage Read API (not used).

Keyless. Runs when ``FLUID_GCP_BIGQUERY_EMULATOR`` names a reachable
goccy/bigquery-emulator (the heavy emulated lane starts one from
``tests/iac/_gcp_emulator/docker-compose.yml``), and skips otherwise;
``scripts/ci/assert_lane_coverage.py`` fails that lane if it only skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.emulated_heavy]

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
bigquery = pytest.importorskip("google.cloud.bigquery")

_LOG = logging.getLogger("test.emulated.chain")
BRONZE = "bronze.customer_subscriptions"
SILVER = "silver.subscription_status_summary"
GOLD = "gold.retention_candidates"
HASH_RE = re.compile(r"[0-9a-f]{64}")
ROWS = 600

SILVER_SQL = """
SELECT product_id, status, COUNT(*) AS subscription_count
FROM subscriptions
GROUP BY product_id, status
"""
GOLD_SQL = """
SELECT s.customer_id, s.subscription_id, s.product_id, s.status, s.msisdn,
       t.subscription_count AS cohort_size
FROM subscriptions s
JOIN status_summary t ON s.product_id = t.product_id AND s.status = t.status
WHERE s.status IN ('suspended', 'expired') AND t.subscription_count >= 10
"""


def _emulator() -> str:
    host = os.environ.get("FLUID_GCP_BIGQUERY_EMULATOR", "").strip()
    if not host:
        pytest.skip("FLUID_GCP_BIGQUERY_EMULATOR is not set (the heavy emulated lane sets it)")
    return host


@pytest.fixture
def emulator(monkeypatch, tmp_path) -> Iterator[Dict[str, Any]]:
    """The emulator, a client for it, and a process pointed at it with no credentials."""
    from google.auth.credentials import AnonymousCredentials

    host = _emulator()
    project = os.environ.get("FLUID_GCP_BIGQUERY_EMULATOR_PROJECT", "fluid-emulator")
    # The process under test: no ADC anywhere, only the emulator variable,
    # which python-bigquery reads for its endpoint.
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", host)
    client = bigquery.Client(project=project, credentials=AnonymousCredentials())
    try:
        list(client.list_datasets(max_results=1))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"bigquery-emulator at {host} is not reachable: {exc}")
    empty = tmp_path / "no-gcloud"
    empty.mkdir()
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(empty))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("FLUID_PII_HASH_SECRET", "wf43-emulated-chain-salt-0123456789")
    suffix = uuid.uuid4().hex[:8]
    yield {"client": client, "project": project, "suffix": suffix}


# ── The workspace ───────────────────────────────────────────────────────


def _dump(path: Path, doc: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _schema(*cols: tuple) -> List[Dict[str, Any]]:
    return [{"name": n, "type": t, "required": req} for n, t, req in cols]


def _overlay(project: str, dataset: str, table: str) -> Dict[str, Any]:
    return {
        "exposes": [
            {
                "binding": {
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
            }
        ]
    }


def _product(
    root: Path,
    folder: str,
    doc: Dict[str, Any],
    *,
    project: str,
    suffix: str,
) -> Path:
    path = _dump(root / "contracts" / folder / "contract.fluid.yaml", doc)
    # Unique table names as well as datasets: goccy/bigquery-emulator stores
    # every table under its bare name ("SELECT * FROM `customer_subscriptions`"
    # in its own log), so two datasets holding a table of the same name share
    # its rows and columns, across projects too, and the client then retries
    # the emulator's 500s for minutes.
    _dump(
        path.parent / "overlays" / "gcp.yaml",
        _overlay(project, f"{folder[0]}_{suffix}", f"{folder}_{suffix}"),
    )
    return path


def _local_binding(folder: str) -> Dict[str, Any]:
    return {"platform": "local", "format": "parquet", "location": {"path": f"out/{folder}.parquet"}}


def _workspace(root: Path, project: str, suffix: str) -> Dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fluid.workspace.yaml").write_text("workspace: {name: chain}\n", encoding="utf-8")
    source = root / "source" / "product_subscription.parquet"
    source.parent.mkdir(parents=True)
    duckdb.sql(
        "COPY (SELECT printf('SUB%05d', i) AS subscription_id, "
        "printf('CUS%04d', i % 97) AS customer_id, printf('P%02d', i % 7) AS product_id, "
        "['active', 'suspended', 'expired', 'pending'][1 + ((i * 7) % 4)::INTEGER] AS status, "
        "CASE WHEN i % 11 = 0 THEN NULL ELSE printf('+4670%07d', i) END AS msisdn, "
        "TIMESTAMP '2026-01-01 00:00:00' + to_hours(i) AS created_at "
        f"FROM range({ROWS}) t(i)) TO '{source}' (FORMAT parquet)"
    )
    bronze = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": BRONZE,
        "name": "Customer Subscriptions",
        "metadata": {"layer": "Bronze", "productType": "SDP"},
        "builds": [
            {
                "id": "ingest_subscriptions",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": str(source)},
                        "mode": "full_refresh",
                        "reader": {"format": "parquet"},
                    },
                    "sink": {"format": "parquet"},
                },
                "outputs": ["subscriptions"],
            }
        ],
        "exposes": [
            {
                "exposeId": "subscriptions",
                "kind": "table",
                "policy": {"privacy": {"masking": [{"column": "msisdn", "strategy": "hash"}]}},
                "binding": _local_binding("customer_subscriptions"),
                "contract": {
                    "schema": _schema(
                        ("subscription_id", "VARCHAR", True),
                        ("customer_id", "VARCHAR", True),
                        ("product_id", "VARCHAR", True),
                        ("status", "VARCHAR", True),
                        ("msisdn", "VARCHAR", False),
                        ("created_at", "TIMESTAMP", True),
                    ),
                    "schemaPolicy": "strict",
                },
            }
        ],
    }
    silver = {
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
                "properties": {"sql": SILVER_SQL},
                "outputs": ["status_summary"],
            }
        ],
        "exposes": [
            {
                "exposeId": "status_summary",
                "kind": "table",
                "binding": _local_binding("subscription_status_summary"),
                "contract": {
                    "schema": _schema(
                        ("product_id", "VARCHAR", True),
                        ("status", "VARCHAR", True),
                        ("subscription_count", "BIGINT", True),
                    )
                },
            }
        ],
    }
    gold = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": GOLD,
        "name": "Retention Candidates",
        "metadata": {"layer": "Gold", "productType": "CDP"},
        "consumes": [
            {"productId": BRONZE, "exposeId": "subscriptions"},
            {"productId": SILVER, "exposeId": "status_summary"},
        ],
        "builds": [
            {
                "id": "identify_retention_candidates",
                "pattern": "embedded-logic",
                "engine": "duckdb",
                "properties": {"sql": GOLD_SQL},
                "outputs": ["candidates"],
            }
        ],
        "exposes": [
            {
                "exposeId": "candidates",
                "kind": "table",
                "binding": _local_binding("retention_candidates"),
                "contract": {
                    "schema": _schema(
                        ("customer_id", "VARCHAR", True),
                        ("subscription_id", "VARCHAR", True),
                        ("product_id", "VARCHAR", True),
                        ("status", "VARCHAR", True),
                        ("msisdn", "VARCHAR", False),
                        ("cohort_size", "BIGINT", True),
                    )
                },
            }
        ],
    }
    return {
        "bronze": _product(root, "customer_subscriptions", bronze, project=project, suffix=suffix),
        "silver": _product(
            root, "subscription_status_summary", silver, project=project, suffix=suffix
        ),
        "gold": _product(root, "retention_candidates", gold, project=project, suffix=suffix),
    }


# ── What fluid apply's OpenTofu step would have created ─────────────────


def _create_from_emitted_module(client: Any, contract_path: Path) -> str:
    """Create the dataset and table forge-cli's GCP emitter declares; return the table id."""
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.iac import get_iac_plugin

    contract = load_contract_with_overlay(str(contract_path), "gcp", _LOG)
    resources = get_iac_plugin("gcp").emit(contract, [])
    datasets = resources["google_bigquery_dataset"]
    for ds in datasets.values():
        dataset = bigquery.Dataset(f"{ds['project']}.{ds['dataset_id']}")
        dataset.location = ds["location"]
        client.create_dataset(dataset)
    [table] = resources["google_bigquery_table"].values()
    dataset_id = table["dataset_id"]
    if dataset_id.startswith("${"):
        dataset_id = datasets[dataset_id.split(".")[1]]["dataset_id"]
    table_id = f"{table['project']}.{dataset_id}.{table['table_id']}"
    schema = [bigquery.SchemaField.from_api_repr(f) for f in json.loads(table["schema"])]
    client.create_table(bigquery.Table(table_id, schema=schema))
    return table_id


def _build(contract_path: Path, env: str) -> int:
    from fluid_build.build_runners.base import run_builds_from_args

    args = argparse.Namespace(
        contract=str(contract_path),
        env=env,
        build_id=None,
        dry_run=False,
        fail_fast=True,
        delay=0,
        no_output=False,
        sample_rows=None,
        mode=None,
    )
    return run_builds_from_args(args, _LOG, force_run=True)


def _rows(client: Any, table_id: str, columns: List[str]) -> List[tuple]:
    """Every row of ``table_id``, read with list_rows (the emulator's query path
    returns TIMESTAMP cells python-bigquery cannot parse, so no query here)."""
    table = client.get_table(table_id)
    data = client.list_rows(table).to_arrow(create_bqstorage_client=False)
    return sorted(tuple(r[c] for c in columns) for r in data.to_pylist())


def _local_rows(contract_path: Path, folder: str, columns: List[str]) -> List[tuple]:
    path = contract_path.parent / "out" / f"{folder}.parquet"
    cols = ", ".join(columns)
    return sorted(duckdb.sql(f"SELECT {cols} FROM read_parquet('{path}')").fetchall())


def _verify(contract_path: Path) -> int:
    from fluid_build.cli.verify import run

    args = argparse.Namespace(
        contract=str(contract_path),
        expose_id=None,
        strict=True,
        out=None,
        show_diffs=False,
        env="gcp",
    )
    return run(args, _LOG)


# ── The chain ───────────────────────────────────────────────────────────


SILVER_COLS = ["product_id", "status", "subscription_count"]
GOLD_COLS = ["customer_id", "subscription_id", "product_id", "status", "msisdn", "cohort_size"]


def test_one_contract_per_product_builds_the_same_rows_on_local_and_on_bigquery(
    emulator, tmp_path, capsys
):
    client, project, suffix = emulator["client"], emulator["project"], emulator["suffix"]
    paths = _workspace(tmp_path / "ws", project, suffix)

    # --env local: the chain as it has always run.
    for name in ("bronze", "silver", "gold"):
        assert _build(paths[name], "local") == 0, f"local {name} build failed"
    local_silver = _local_rows(paths["silver"], "subscription_status_summary", SILVER_COLS)
    local_gold = _local_rows(paths["gold"], "retention_candidates", GOLD_COLS)
    assert local_silver and local_gold

    # --env gcp: the IaC's tables, then the same three builds.
    tables = {name: _create_from_emitted_module(client, paths[name]) for name in paths}
    for name in ("bronze", "silver", "gold"):
        assert _build(paths[name], "gcp") == 0, f"gcp {name} build failed"
    out = " ".join(capsys.readouterr().out.split())
    assert f"read {ROWS:,} row(s) from BigQuery table {tables['bronze']}" in out
    assert f"loaded {len(local_silver)} row(s) into BigQuery table {tables['silver']}" in out

    # Bronze: every source row, every msisdn hashed, none in cleartext.
    bronze = _rows(client, tables["bronze"], ["subscription_id", "msisdn"])
    assert len(bronze) == ROWS
    msisdns = [m for _, m in bronze if m is not None]
    assert msisdns and all(HASH_RE.fullmatch(m) for m in msisdns)
    assert len(msisdns) == ROWS - len(range(0, ROWS, 11))

    # Silver and gold read BigQuery, landed BigQuery, and match the local target.
    assert _rows(client, tables["silver"], SILVER_COLS) == local_silver
    assert _rows(client, tables["gold"], GOLD_COLS) == local_gold
    # No local file named after an overlay's gs:// staging path.
    assert not [p for p in tmp_path.rglob("*") if p.name.startswith("gs:")]

    # fluid verify --env gcp --strict: bronze passes, row count and masking.
    assert _verify(paths["bronze"]) == 0
    assert _verify(paths["silver"]) == 0

    # One cleartext msisdn in the table, and --strict fails.
    first = bronze[1][0]
    client.query(
        f"UPDATE `{tables['bronze']}` SET msisdn = '+46701234567' "
        f"WHERE subscription_id = '{first}'"
    ).result()
    capsys.readouterr()
    assert _verify(paths["bronze"]) == 1
    report = " ".join(capsys.readouterr().out.split())
    assert "masked column(s) did not land treated" in report
    assert "+46701234567" not in report
