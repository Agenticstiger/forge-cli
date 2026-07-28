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

"""GCS object-store LIVE proof for the Iceberg streaming sink (GCS twin of
``test_debezium_server_iceberg_live.py``, which validates the S3/MinIO path).

What this proves end-to-end, against a REAL Google Cloud Storage API (the
``fsouza/fake-gcs-server`` emulator):

1. forge's REAL deriver (``emit_iceberg_sink_config`` over ``resolve_iceberg_catalog``)
   emits the GCS catalog block for a ``gs://`` warehouse — crucially
   ``iceberg.catalog.io-impl = org.apache.iceberg.gcp.gcs.GCSFileIO`` (the #1
   "works in the REST demo, silently fails on cloud" trap the RFC §6.8 io-impl
   check guards) and ``iceberg.catalog.warehouse = gs://…``.
2. An Apache ``iceberg-rest-fixture`` catalog wired to GCSFileIO + fake-gcs, plus
   an independent ``pyiceberg`` client, CREATE + WRITE + READ an Iceberg table AT
   the forge-derived warehouse — and the parquet/metadata objects actually land
   in the GCS bucket under the derived warehouse prefix.

Gated OFF by default (heavy: pulls fake-gcs-server + iceberg-rest-fixture +
python:3.11-slim). Enable with ``FLUID_TEST_ICEBERG_GCS=1`` + a running Docker
daemon — the CI integration stage. Self-skips everywhere else so the light suite
stays green offline. GCSFileIO points at the emulator via ``gcs.service.host`` +
``gcs.no-auth`` (Apache Iceberg's documented emulator hooks).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.providers._iceberg_catalog import GCS_FILE_IO, resolve_iceberg_catalog
from tests.integration._iceberg_objectstore_live import (
    _docker_available,
    compose,
    run_pyiceberg_container,
    wait_for_http,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("FLUID_TEST_ICEBERG_GCS") != "1" or not _docker_available(),
        reason="set FLUID_TEST_ICEBERG_GCS=1 and have Docker running to run the live GCS test",
    ),
]

# The warehouse the sink writes to. The bucket name (``warehouse``) is what the
# emulator init pre-creates; the forge deriver carries this string verbatim.
_GCS_WAREHOUSE = "gs://warehouse/"
_REST_URI = "http://iceberg-rest:8181"
_NAMESPACE = "streaming"
_TABLE = "orders"

_COMPOSE = """\
services:
  fake-gcs:
    image: fsouza/fake-gcs-server:latest
    command: ["-scheme", "http", "-host", "0.0.0.0", "-port", "4443",
              "-public-host", "fake-gcs:4443", "-external-url", "http://fake-gcs:4443"]
    ports:
      - "4443:4443"
  gcs-init:
    image: python:3.11-slim
    depends_on: [fake-gcs]
    entrypoint: >
      /bin/sh -c "
      pip -q install requests >/dev/null 2>&1;
      python -c \\"import requests, time;
      [time.sleep(1) for _ in range(3)];
      r = requests.post('http://fake-gcs:4443/storage/v1/b?project=test', json={'name': 'warehouse'});
      print('bucket-create', r.status_code)\\";
      echo init-done;
      "
  iceberg-rest:
    image: apache/iceberg-rest-fixture:latest
    depends_on:
      gcs-init:
        condition: service_completed_successfully
    environment:
      CATALOG_WAREHOUSE: gs://warehouse/
      CATALOG_IO__IMPL: org.apache.iceberg.gcp.gcs.GCSFileIO
      CATALOG_GCS_SERVICE_HOST: http://fake-gcs:4443
      CATALOG_GCS_NO__AUTH: "true"
      CATALOG_GCS_PROJECT__ID: test
    ports:
      - "8181:8181"
"""

# pyiceberg (gcsfs) driving the SAME warehouse + REST catalog forge derived.
# gcs.service.host + an anon token route gcsfs at fake-gcs (no real GCP auth).
_RW = """\
import os, sys
import pyarrow as pa
from pyiceberg.catalog.rest import RestCatalog

props = {
    "gcs.project-id": "test",
    "gcs.service.host": "http://fake-gcs:4443",
    "gcs.oauth2.token": "anon",
    "gcs.oauth2.token-expires-at": "9999999999000",
}
cat = RestCatalog("gcslive", uri=os.environ["REST_URI"],
                  warehouse=os.environ["WAREHOUSE"], **props)
try:
    cat.create_namespace(os.environ["NAMESPACE"])
except Exception as exc:  # namespace may already exist
    print("ns:", type(exc).__name__, str(exc)[:100])
schema = pa.schema([("id", pa.int64()), ("customer", pa.string())])
data = pa.table({"id": [1, 2, 3, 4, 5], "customer": ["a", "b", "c", "d", "e"]}, schema=schema)
fq = os.environ["NAMESPACE"] + "." + os.environ["TABLE"]
tbl = cat.create_table(fq, schema=schema)
tbl.append(data)
n = cat.load_table(fq).scan().to_arrow().num_rows
print("ROWCOUNT=%d" % n)
sys.exit(0 if n == 5 else 3)
"""


def _bucket_objects(host_port: int, bucket: str) -> list[str]:
    """List object names in the fake-gcs bucket (host-side verification)."""
    raw = urllib.request.urlopen(  # noqa: S310 — localhost emulator
        f"http://localhost:{host_port}/storage/v1/b/{bucket}/o", timeout=5
    ).read()
    return [o["name"] for o in json.loads(raw).get("items", [])]


def test_iceberg_sink_writes_to_gcs(tmp_path: Path):
    project = "fluidicegcs"

    # ── forge's REAL deriver builds the GCS sink config (the unit under test) ──
    binding = {
        "platform": "gcp",
        "format": "iceberg",
        "location": {
            "database": _NAMESPACE,
            "table": _TABLE,
            "catalog": "rest",
            "uri": _REST_URI,
            "warehouse": _GCS_WAREHOUSE,
        },
    }
    resolved = resolve_iceberg_catalog(binding)
    cfg = emit_iceberg_sink_config(resolved, product_id=f"{_NAMESPACE}.{_TABLE}", topics=[_TABLE])
    # The load-bearing GCS assertions — asserted directly (cheap, always runs
    # once gated on): object-store warehouse ⇒ GCSFileIO, not S3FileIO.
    assert cfg["iceberg.catalog.type"] == "rest"
    assert cfg["iceberg.catalog.io-impl"] == GCS_FILE_IO == "org.apache.iceberg.gcp.gcs.GCSFileIO"
    assert cfg["iceberg.catalog.warehouse"] == _GCS_WAREHOUSE
    assert cfg["iceberg.catalog.uri"] == _REST_URI
    assert cfg["iceberg.tables"] == f"{_NAMESPACE}.{_TABLE}"

    (tmp_path / "docker-compose.yml").write_text(_COMPOSE)
    (tmp_path / "rw.py").write_text(_RW)

    try:
        up = compose(["up", "-d", "fake-gcs", "gcs-init", "iceberg-rest"], tmp_path, project)
        if up.returncode != 0:
            pytest.skip(f"could not start GCS/Iceberg stack (image pull?): {up.stderr[:300]}")

        if not wait_for_http("http://localhost:8181/v1/config"):
            pytest.skip("iceberg-rest catalog did not come up within timeout")

        # create the namespace over REST before the sink writes (RFC §14: the
        # connector auto-creates the TABLE but not the NAMESPACE).
        urllib.request.urlopen(  # noqa: S310 — localhost emulator
            urllib.request.Request(
                "http://localhost:8181/v1/namespaces",
                data=json.dumps({"namespace": [_NAMESPACE]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=5,
        ).read()

        # the sink's warehouse + catalog identity come from forge's derived cfg
        result = run_pyiceberg_container(
            script_path=tmp_path / "rw.py",
            network=f"{project}_default",
            pip_spec="'pyiceberg[gcsfs]==0.8.1' pyarrow",
            env={
                "REST_URI": _REST_URI,
                "WAREHOUSE": cfg["iceberg.catalog.warehouse"],
                "NAMESPACE": _NAMESPACE,
                "TABLE": _TABLE,
            },
        )
        assert "ROWCOUNT=5" in result.stdout, result.stdout + result.stderr
        assert result.returncode == 0, result.stdout + result.stderr

        # host-side proof: the Iceberg data + metadata objects landed in the GCS
        # bucket under the forge-derived warehouse prefix.
        objects = _bucket_objects(4443, "warehouse")
        assert any(
            o.startswith(f"{_NAMESPACE}/{_TABLE}/data/") and o.endswith(".parquet") for o in objects
        ), objects
        assert any(o.endswith(".metadata.json") for o in objects), objects
    finally:
        compose(["down", "-v"], tmp_path, project)
