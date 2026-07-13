# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Azure object-store LIVE proof for the Iceberg streaming sink (Azure twin of the
S3/MinIO ``test_debezium_server_iceberg_live.py`` and the GCS
``test_iceberg_sink_gcs_live.py``).

Two honest tiers, because Azure's object-store emulator has a hard, documented
gap (below):

* **Always (cheap):** forge's REAL deriver (``emit_iceberg_sink_config`` over
  ``resolve_iceberg_catalog``) emits the ADLS catalog block for an ``abfss://``
  warehouse — ``iceberg.catalog.io-impl =
  org.apache.iceberg.azure.adlsv2.ADLSFileIO`` (the object-store io-impl trap the
  RFC §6.8 guards) and ``iceberg.catalog.warehouse = abfss://…``.

* **Emulator (Azurite, default):** a REAL Azure Blob endpoint round-trips an
  object at the forge-derived warehouse path (container + prefix) — proving the
  Azure object-store LEG the sink writes to. It does NOT run a full Iceberg-table
  write, because **Azurite serves the Blob API but NOT the ADLS Gen2 (DFS)
  endpoint that Iceberg's ``ADLSFileIO`` speaks** — a documented upstream
  limitation (apache/iceberg PR #8303 uses mocks for ADLS prefix ops;
  HADOOP-19379 tracks Azurite ADLSv2 support). Full Iceberg-on-Azure is validated
  by the live-creds tier.

* **Live creds (opt-in):** set ``FLUID_ICEBERG_AZURE_WAREHOUSE`` (a real
  ``abfss://container@account.dfs.core.windows.net/path`` warehouse) +
  ``FLUID_ICEBERG_AZURE_CONNECTION_STRING`` to run the FULL pyiceberg
  create+write+read against real ADLS Gen2 — the true "sink writes an Iceberg
  table to an Azure container" proof.

Gated OFF by default. Enable with ``FLUID_TEST_ICEBERG_AZURE=1`` + a running
Docker daemon. Self-skips everywhere else so the light suite stays green offline.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.providers._iceberg_catalog import ADLS_FILE_IO, resolve_iceberg_catalog
from tests.integration._iceberg_objectstore_live import (
    _docker_available,
    compose,
    run_pyiceberg_container,
    wait_for_tcp,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("FLUID_TEST_ICEBERG_AZURE") != "1" or not _docker_available(),
        reason="set FLUID_TEST_ICEBERG_AZURE=1 and have Docker running to run the live Azure test",
    ),
]

# Azurite's well-known development account (fixed by the emulator; never a secret).
_AZURITE_ACCOUNT = "devstoreaccount1"
_AZURITE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/" "K1SZFPTOtr/KBHBeksoGMGw=="
)
_CONTAINER = "warehouse"
_WAREHOUSE_PATH = "wh"
# The abfss warehouse the sink writes to; forge carries this string verbatim.
_ABFSS_WAREHOUSE = f"abfss://{_CONTAINER}@{_AZURITE_ACCOUNT}.dfs.core.windows.net/{_WAREHOUSE_PATH}"
_NAMESPACE = "streaming"
_TABLE = "orders"

_COMPOSE = """\
services:
  azurite:
    image: mcr.microsoft.com/azure-storage/azurite:latest
    command: ["azurite", "--blobHost", "0.0.0.0", "--blobPort", "10000",
              "--skipApiVersionCheck", "--loose"]
    ports:
      - "10000:10000"
"""

# Azurite BLOB-leg round-trip: prove an object lands + reads back in the Azure
# container at the forge-derived warehouse path. (Not a full Iceberg write — see
# the module docstring on Azurite's missing ADLS Gen2 endpoint.)
_BLOB_RW = """\
import os, sys
from azure.storage.blob import BlobServiceClient

bsc = BlobServiceClient.from_connection_string(os.environ["AZ_CONN"])
container = os.environ["CONTAINER"]
try:
    bsc.create_container(container)
    print("container-created")
except Exception as exc:
    print("container:", type(exc).__name__, str(exc)[:80])
key = os.environ["WAREHOUSE_PATH"] + "/metadata/00000.metadata.json"
payload = b'{"format-version": 2, "table-uuid": "forge-azure-live"}'
bsc.get_blob_client(container, key).upload_blob(payload, overwrite=True)
got = bsc.get_blob_client(container, key).download_blob().readall()
names = [b.name for b in bsc.get_container_client(container).list_blobs()]
print("BLOBS=" + ",".join(names))
print("BLOB_ROUNDTRIP_OK" if got == payload else "MISMATCH")
sys.exit(0 if got == payload else 3)
"""

# Live-creds tier: full pyiceberg Iceberg create+write+read against real ADLS.
_ICEBERG_RW = """\
import os, sys
import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog

props = {
    "py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO",
    "adls.connection-string": os.environ["AZ_CONN"],
}
cat = SqlCatalog("azlive", uri="sqlite:///:memory:",
                 warehouse=os.environ["WAREHOUSE"], **props)
cat.create_namespace(os.environ["NAMESPACE"])
schema = pa.schema([("id", pa.int64()), ("customer", pa.string())])
data = pa.table({"id": [1, 2, 3, 4, 5], "customer": ["a", "b", "c", "d", "e"]}, schema=schema)
fq = os.environ["NAMESPACE"] + "." + os.environ["TABLE"]
tbl = cat.create_table(fq, schema=schema)
tbl.append(data)
n = cat.load_table(fq).scan().to_arrow().num_rows
print("ROWCOUNT=%d" % n)
sys.exit(0 if n == 5 else 3)
"""


def _assert_forge_emits_adls(warehouse: str) -> dict:
    """forge's REAL deriver builds the Azure sink config (the unit under test)."""
    binding = {
        "platform": "azure",
        "format": "iceberg",
        "location": {
            "database": _NAMESPACE,
            "table": _TABLE,
            "catalog": "rest",
            "uri": "http://catalog:8181",
            "warehouse": warehouse,
        },
    }
    cfg = emit_iceberg_sink_config(
        resolve_iceberg_catalog(binding), product_id=f"{_NAMESPACE}.{_TABLE}", topics=[_TABLE]
    )
    assert cfg["iceberg.catalog.io-impl"] == ADLS_FILE_IO
    assert ADLS_FILE_IO == "org.apache.iceberg.azure.adlsv2.ADLSFileIO"
    assert cfg["iceberg.catalog.warehouse"] == warehouse
    return cfg


def test_iceberg_sink_writes_to_azure_live_creds(tmp_path: Path):
    """Full Iceberg-table write to a REAL Azure ADLS container (opt-in)."""
    warehouse = os.environ.get("FLUID_ICEBERG_AZURE_WAREHOUSE")
    conn = os.environ.get("FLUID_ICEBERG_AZURE_CONNECTION_STRING")
    if not (warehouse and conn):
        pytest.skip(
            "live Azure ADLS not configured — set FLUID_ICEBERG_AZURE_WAREHOUSE "
            "(abfss://…) + FLUID_ICEBERG_AZURE_CONNECTION_STRING for the full "
            "Iceberg-on-Azure round-trip (Azurite cannot back ADLSFileIO)"
        )

    cfg = _assert_forge_emits_adls(warehouse)

    script = tmp_path / "iceberg_rw.py"
    script.write_text(_ICEBERG_RW)
    result = run_pyiceberg_container(
        script_path=script,
        network="bridge",  # default bridge has outbound internet to real Azure
        pip_spec="'pyiceberg[adlfs,sql-sqlite]==0.8.1' pyarrow",
        env={
            "AZ_CONN": conn,
            "WAREHOUSE": cfg["iceberg.catalog.warehouse"],
            "NAMESPACE": _NAMESPACE,
            "TABLE": _TABLE,
        },
    )
    assert "ROWCOUNT=5" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def test_iceberg_sink_azure_object_store_leg_on_azurite(tmp_path: Path):
    """Azurite Blob-leg proof: forge derives the ADLS sink config, and an object
    round-trips in the Azure container at the derived warehouse path.

    The full Iceberg-table write is NOT attempted here — Azurite has no ADLS Gen2
    (DFS) endpoint (see the module docstring). That path is covered by
    ``test_iceberg_sink_writes_to_azure_live_creds`` under live creds.
    """
    project = "fluidiceazure"

    cfg = _assert_forge_emits_adls(_ABFSS_WAREHOUSE)
    assert cfg["iceberg.catalog.warehouse"] == _ABFSS_WAREHOUSE

    (tmp_path / "docker-compose.yml").write_text(_COMPOSE)
    (tmp_path / "blob_rw.py").write_text(_BLOB_RW)

    try:
        up = compose(["up", "-d", "azurite"], tmp_path, project)
        if up.returncode != 0:
            pytest.skip(f"could not start Azurite (image pull?): {up.stderr[:300]}")
        if not wait_for_tcp("localhost", 10000):
            pytest.skip("Azurite Blob endpoint did not come up within timeout")

        conn = (
            "DefaultEndpointsProtocol=http;"
            f"AccountName={_AZURITE_ACCOUNT};AccountKey={_AZURITE_KEY};"
            f"BlobEndpoint=http://azurite:10000/{_AZURITE_ACCOUNT};"
        )
        result = run_pyiceberg_container(
            script_path=tmp_path / "blob_rw.py",
            network=f"{project}_default",
            pip_spec="azure-storage-blob",
            env={
                "AZ_CONN": conn,
                "CONTAINER": _CONTAINER,
                "WAREHOUSE_PATH": _WAREHOUSE_PATH,
            },
        )
        assert "BLOB_ROUNDTRIP_OK" in result.stdout, result.stdout + result.stderr
        assert result.returncode == 0, result.stdout + result.stderr
        # the object landed under the forge-derived warehouse prefix
        assert f"{_WAREHOUSE_PATH}/metadata/" in result.stdout, result.stdout
    finally:
        compose(["down", "-v"], tmp_path, project)
