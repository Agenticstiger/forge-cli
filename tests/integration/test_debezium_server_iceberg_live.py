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

"""PR8 — LIVE end-to-end proof of the embedded Debezium-Server Iceberg sink.

forge's REAL deriver (``emit_debezium_iceberg_sink_config``) produces the
``debezium.sink.iceberg.*`` block; a real Debezium Server + the memiiso Iceberg
consumer then snapshots a Postgres table into an Iceberg REST catalog backed by
MinIO/S3, and pyiceberg reads the rows back. This mirrors the RFC §14 OSS spike
(Kafka-Connect → MinIO) for the Debezium-Server topology.

Gated OFF by default (heavy: pulls 4 images, ~2 min under emulation). Enable with
``FLUID_TEST_DEBEZIUM_ICEBERG=1`` + a running Docker daemon — the CI integration
stage. Self-skips everywhere else so the light suite stays green offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from fluid_build.build_runners.debezium.iceberg_sink import (
    emit_debezium_iceberg_sink_config,
)
from fluid_build.build_runners.debezium.runner import render_application_properties
from fluid_build.providers._iceberg_catalog import resolve_iceberg_catalog
from tests._infrastructure.testcontainers_fixtures import _docker_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("FLUID_TEST_DEBEZIUM_ICEBERG") != "1" or not _docker_available(),
        reason="set FLUID_TEST_DEBEZIUM_ICEBERG=1 and have Docker running to run the live test",
    ),
]

_COMPOSE = """\
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: appdb
    command: ["postgres", "-c", "wal_level=logical", "-c", "max_replication_slots=4", "-c", "max_wal_senders=4"]
    volumes:
      - ./seed.sql:/docker-entrypoint-initdb.d/seed.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d appdb"]
      interval: 3s
      timeout: 3s
      retries: 30
  # Silo: the S3-compatible MinIO fork the Lakekeeper lane uses (minio/minio
  # and minio/mc left Docker Hub on 2026-09-11). It ships ``mc`` too.
  minio:
    image: pgsty/silo:RELEASE.2026-09-03T13-18-01Z@sha256:b616a0cf8cb281e7e6bb3c9b1fb53875b4016a2878223925541c18f82d6c5ca3
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    command: ["server", "/data"]
  mc:
    image: pgsty/silo:RELEASE.2026-09-03T13-18-01Z@sha256:b616a0cf8cb281e7e6bb3c9b1fb53875b4016a2878223925541c18f82d6c5ca3
    depends_on: [minio]
    entrypoint: >
      /bin/sh -c "
      until mc alias set local http://minio:9000 minioadmin minioadmin; do sleep 1; done;
      mc mb -p local/warehouse || true;
      mc anonymous set public local/warehouse || true;
      echo bucket-ready;
      "
  iceberg-rest:
    image: apache/iceberg-rest-fixture:latest
    depends_on:
      mc:
        condition: service_completed_successfully
    environment:
      CATALOG_WAREHOUSE: s3://warehouse/
      CATALOG_IO__IMPL: org.apache.iceberg.aws.s3.S3FileIO
      CATALOG_S3_ENDPOINT: http://minio:9000
      CATALOG_S3_PATH__STYLE__ACCESS: "true"
      AWS_ACCESS_KEY_ID: minioadmin
      AWS_SECRET_ACCESS_KEY: minioadmin
      AWS_REGION: us-east-1
    ports:
      - "8181:8181"
  debezium:
    image: ghcr.io/memiiso/debezium-server-iceberg:latest
    platform: linux/amd64
    depends_on:
      postgres:
        condition: service_healthy
      iceberg-rest:
        condition: service_started
    env_file: [./debezium.env]
    volumes:
      - ./application.properties:/debezium/config/application.properties:ro
"""

_SEED = """\
CREATE TABLE public.orders (
    id integer PRIMARY KEY,
    customer text NOT NULL,
    amount_cents integer NOT NULL
);
ALTER TABLE public.orders REPLICA IDENTITY FULL;
INSERT INTO public.orders (id, customer, amount_cents) VALUES
    (1, 'alice', 1000), (2, 'bob', 2500), (3, 'carol', 4200),
    (4, 'dave', 999), (5, 'erin', 7777);
"""

_READ_BACK = """\
import sys, time
from pyiceberg.catalog.rest import RestCatalog
S3 = {"s3.endpoint": "http://minio:9000", "s3.access-key-id": "minioadmin",
      "s3.secret-access-key": "minioadmin", "s3.path-style-access": "true",
      "s3.region": "us-east-1"}
cat = RestCatalog("live", uri="http://iceberg-rest:8181", **S3)
data = []
for _ in range(40):
    tables = []
    try:
        for ns in cat.list_namespaces():
            tables.extend(cat.list_tables(ns))
    except Exception as exc:
        print("catalog not ready:", exc)
    data = [t for t in tables if "orders" in t[-1].lower()]
    if data:
        break
    time.sleep(5)
if not data:
    print("FAIL: no orders table"); sys.exit(2)
tbl = cat.load_table(data[0])
n = tbl.scan().to_arrow().num_rows
print("ROWCOUNT=%d" % n)
sys.exit(0 if n >= 5 else 3)
"""


def _build_application_properties() -> tuple[str, dict[str, str]]:
    """The Iceberg sink block comes from forge's REAL deriver — the unit under
    test — and the file from the runner's REAL renderer, with a standard
    Postgres source / JSON-format / file-offset Debezium Server config. The
    renderer moves the credentials to the returned environment, which the
    container gets through ``env_file``: the server must resolve them."""
    binding = {
        "platform": "local",
        "format": "iceberg",
        "location": {
            "database": "cdc",
            "table": "orders",
            "catalog": "rest",
            "uri": "http://iceberg-rest:8181",
            "warehouse": "s3://warehouse/",
            "region": "us-east-1",
        },
    }
    derived = emit_debezium_iceberg_sink_config(
        resolve_iceberg_catalog(binding),
        overrides={
            "s3.endpoint": "http://minio:9000",
            "s3.path-style-access": "true",
            "s3.access-key-id": "minioadmin",
            "s3.secret-access-key": "minioadmin",
        },
    )
    source = {
        "offset.storage": "org.apache.kafka.connect.storage.FileOffsetBackingStore",
        "offset.storage.file.filename": "/debezium/data/offsets.dat",
        "offset.flush.interval.ms": "0",
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "database.hostname": "postgres",
        "database.port": "5432",
        "database.user": "postgres",
        "database.password": "postgres",  # pragma: allowlist secret
        "database.dbname": "appdb",
        "database.server.id": "1234",
        "topic.prefix": "cdcsrv",
        "plugin.name": "pgoutput",
        "slot.name": "dbz_slot",
        "publication.name": "dbz_pub",
        "publication.autocreate.mode": "filtered",
        "snapshot.mode": "initial",
        "table.include.list": "public.orders",
    }
    props, env = render_application_properties(source, "iceberg", derived)
    lines = [
        props,
        "debezium.format.value=json",
        "debezium.format.value.schemas.enable=true",
        "debezium.format.key=json",
        "debezium.format.key.schemas.enable=true",
    ]
    return "\n".join(lines) + "\n", env


def _assert_credentials_only_in_env(props: str, env: dict[str, str]) -> None:
    """No credential in the file: the server resolves each from its environment."""
    assert "debezium.source.database.password=${FLUID_DBZ_SOURCE_DATABASE_PASSWORD}" in props
    assert (
        "debezium.sink.iceberg.s3.secret-access-key="
        "${FLUID_DBZ_SINK_ICEBERG_S3_SECRET_ACCESS_KEY}" in props
    )
    assert env == {
        "FLUID_DBZ_SOURCE_DATABASE_PASSWORD": "postgres",  # pragma: allowlist secret
        "FLUID_DBZ_SINK_ICEBERG_S3_SECRET_ACCESS_KEY": "minioadmin",  # pragma: allowlist secret
    }


def _compose(args, cwd, project):
    return subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_debezium_server_iceberg_lands_rows(tmp_path: Path):
    project = "fluiddbzice"
    (tmp_path / "docker-compose.yml").write_text(_COMPOSE)
    (tmp_path / "seed.sql").write_text(_SEED)
    (tmp_path / "read_back.py").write_text(_READ_BACK)
    props, env = _build_application_properties()
    (tmp_path / "application.properties").write_text(props)
    (tmp_path / "debezium.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))

    # the deriver's output is asserted directly (cheap, runs even on slow CI)
    assert "debezium.sink.iceberg.type=rest" in props
    _assert_credentials_only_in_env(props, env)
    assert "debezium.sink.iceberg.io-impl=org.apache.iceberg.aws.s3.S3FileIO" in props
    assert "debezium.sink.iceberg.table-namespace=cdc" in props
    assert "iceberg.control.topic" not in props  # KC-only key must not leak

    try:
        up = _compose(["up", "-d", "postgres", "minio", "mc", "iceberg-rest"], tmp_path, project)
        assert up.returncode == 0, up.stderr

        # wait for the REST catalog, then create the namespace
        for _ in range(30):
            try:
                urllib.request.urlopen("http://localhost:8181/v1/config", timeout=3).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        urllib.request.urlopen(
            urllib.request.Request(
                "http://localhost:8181/v1/namespaces",
                data=json.dumps({"namespace": ["cdc"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=5,
        ).read()

        dbz = _compose(["up", "-d", "debezium"], tmp_path, project)
        assert dbz.returncode == 0, dbz.stderr

        # poll for the snapshot commit (amd64 emulation is slow)
        committed = False
        for _ in range(36):  # ~3 min
            logs = _compose(["logs", "debezium"], tmp_path, project).stdout
            if "Committed 5 events to table" in logs:
                committed = True
                break
            time.sleep(5)
        assert committed, "Debezium did not commit the snapshot to Iceberg in time"

        # independent catalog-side read-back via pyiceberg in an ephemeral container
        read = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                f"{project}_default",
                "-v",
                f"{tmp_path / 'read_back.py'}:/read_back.py:ro",
                "python:3.11-slim",
                "sh",
                "-c",
                "pip -q install 'pyiceberg[s3fs]==0.8.1' pyarrow >/dev/null 2>&1 "
                "&& python /read_back.py",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert "ROWCOUNT=5" in read.stdout or "ROWCOUNT=6" in read.stdout, read.stdout + read.stderr
        assert read.returncode == 0, read.stdout + read.stderr
    finally:
        _compose(["down", "-v"], tmp_path, project)
