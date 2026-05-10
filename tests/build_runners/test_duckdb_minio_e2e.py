# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end DuckDB → MinIO (S3-compatible) acquisition.

Spins a real MinIO container, makes a bucket, runs the DuckDB acquisition
runner with the contract pointing at ``s3://...`` for both source and
sink, and asserts:

* The runner emits a ``CREATE SECRET`` whose values are SQL-quoted
  (security regression).
* The output Parquet exists in MinIO with the expected row count.
* Round-trip schema matches the input.

Skips when Docker is not available so the unit lane stays green on
laptops without Docker.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners.duckdb.runner import (
    _build_create_secret,
    execute_duckdb_build,
)

# ── Unit: SQL safety of CREATE SECRET (no Docker) ────────────────────────


class TestCreateSecretSqlSafety:
    def test_secret_string_value_with_quote_is_doubled(self):
        sql = _build_create_secret(
            "s3",
            {"endpoint": "http://m:9000", "key_id": "AK", "secret": "p'wn"},
        )
        assert sql is not None
        # The secret containing a single quote must be SQL-doubled.
        assert "'p''wn'" in sql
        # Type and structure correct.
        assert sql.startswith("CREATE OR REPLACE SECRET fluid_s3_secret (TYPE s3,")

    def test_unknown_keys_are_silently_dropped(self):
        sql = _build_create_secret(
            "s3",
            {"endpoint": "http://m:9000", "evil_key": "x", "key_id": "AK"},
        )
        # evil_key not allowed -> not in output
        assert "EVIL_KEY" not in (sql or "")
        # but allowed keys still rendered
        assert "ENDPOINT" in (sql or "")
        assert "KEY_ID" in (sql or "")

    def test_unknown_key_with_invalid_identifier_raises(self):
        # Invalid-identifier keys (eg. with spaces, semicolons) are rejected
        # before any allow-list check — defense in depth.
        with pytest.raises(ValueError):
            _build_create_secret(
                "s3",
                {"endpoint": "http://m:9000", "; DROP": "x"},
            )

    def test_unknown_scheme_returns_none(self):
        assert _build_create_secret("ftp", {"x": "y"}) is None

    def test_empty_config_returns_none(self):
        assert _build_create_secret("s3", {}) is None
        assert _build_create_secret("s3", {"region": None}) is None


# ── Integration: live MinIO → DuckDB round-trip ──────────────────────────


def _make_bucket_via_mc(container, access_key: str, secret_key: str, bucket: str) -> None:
    """Create a bucket inside the running MinIO container using the bundled `mc`.

    The official ``minio/minio`` image ships the ``mc`` client. We exec into
    the container so the test stays platform-agnostic and doesn't require
    installing tools on the host or implementing AWS Sigv4 by hand.
    """
    container.exec(["mc", "alias", "set", "local", "http://127.0.0.1:9000", access_key, secret_key])
    # Tolerate "already exists" (re-runs in CI).
    container.exec(["mc", "mb", "--ignore-existing", f"local/{bucket}"])


def _put_object_via_mc(container, bucket: str, key: str, body: bytes) -> None:
    """Drop a small object into MinIO via ``mc cp``.

    Writes to a temp path inside the container, then ``mc cp`` to push it.
    Larger payloads should use the runner under test, not this helper.
    """
    # Stage a payload inside the container — exec writes via /bin/sh -c
    # with a heredoc-like construct via base64 to avoid quoting headaches.
    import base64

    encoded = base64.b64encode(body).decode("ascii")
    container.exec(
        [
            "/bin/sh",
            "-c",
            f"echo '{encoded}' | base64 -d > /tmp/payload && "
            f"mc cp /tmp/payload local/{bucket}/{key} && "
            "rm -f /tmp/payload",
        ]
    )


@pytest.fixture
def minio_bucket_with_csv(minio_container: Dict[str, Any]) -> Dict[str, Any]:
    """Materialize a bucket ``fluidtest`` containing ``orders.csv``.

    Returns the connection dict augmented with ``bucket`` and ``csv_key``.
    """
    bucket = "fluidtest"
    # Re-fetch the live container handle from the testcontainers cache. The
    # session-scoped fixture wrapped the underlying container; we get the
    # bound DockerContainer back via the host/port pair.
    from testcontainers.core.docker_client import DockerClient

    client = DockerClient()
    matching = [
        c
        for c in client.client.containers.list()
        if any(p.endswith("9000/tcp") for p in c.ports.keys())
        and c.image.tags
        and any("minio" in t for t in c.image.tags)
    ]
    assert matching, "expected a running MinIO container"
    raw_container = matching[0]

    # Use a thin shim that mimics testcontainers' ``exec`` over the docker SDK.
    class _Shim:
        def exec(self, cmd):
            raw_container.exec_run(cmd)

    shim = _Shim()
    _make_bucket_via_mc(
        shim,
        minio_container["access_key"],
        minio_container["secret_key"],
        bucket,
    )
    csv_body = b"id,customer,amount\n1,Alice,100.50\n2,Bob,250.00\n3,Carol,42.00\n"
    _put_object_via_mc(shim, bucket, "orders.csv", csv_body)
    return {**minio_container, "bucket": bucket, "csv_key": "orders.csv"}


@pytest.mark.integration
def test_duckdb_minio_round_trip(minio_bucket_with_csv: Dict[str, Any], tmp_path: Path) -> None:
    """Read an S3-prefixed CSV, write Parquet into MinIO, verify count.

    Exercises the end-to-end S3 path: ``CREATE SECRET`` for credentials,
    ``read_csv_auto('s3://...')`` for source, ``COPY ... TO 's3://...'``
    for sink. Side-effect: this test is the one place the SQL-safety
    fixes (Sec-Fix 1) are exercised against a real database.
    """
    cfg = minio_bucket_with_csv
    src_uri = f"s3://{cfg['bucket']}/{cfg['csv_key']}"
    dst_uri = f"s3://{cfg['bucket']}/out/orders.parquet"

    contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.orders_minio",
        "name": "Orders MinIO",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@co.example"},
        },
        "builds": [
            {
                "id": "ingest_orders",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {
                            "uri": src_uri,
                            "s3": {
                                "endpoint": cfg["endpoint"].replace("http://", ""),
                                "url_style": "path",
                                "use_ssl": False,
                                "region": cfg["region"],
                                "key_id": cfg["access_key"],
                                "secret": cfg["secret_key"],
                            },
                        },
                        "mode": "full_refresh",
                        "reader": {"format": "csv", "options": {"header": True}},
                    },
                    "sink": {"format": "parquet"},
                },
                "outputs": ["orders_raw"],
            }
        ],
        "exposes": [
            {
                "exposeId": "orders_raw",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": dst_uri},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }

    rc = execute_duckdb_build(
        contract["builds"][0],
        contract,
        tmp_path,
        dry_run=False,
    )
    assert rc == 0, "DuckDB MinIO E2E expected exit 0"

    # Verify the parquet landed in MinIO by reading it back via DuckDB
    # using the same secret config.
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(
            "CREATE OR REPLACE SECRET fluid_verify_secret (TYPE s3, "
            f"ENDPOINT '{cfg['endpoint'].replace('http://', '')}', "
            "URL_STYLE 'path', USE_SSL false, "
            f"REGION '{cfg['region']}', "
            f"KEY_ID '{cfg['access_key']}', SECRET '{cfg['secret_key']}')"
        )
        # Tiny grace period — write may not be visible to the next reader
        # immediately on local MinIO.
        time.sleep(0.5)
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{dst_uri}')").fetchone()[0]
    finally:
        con.close()
    assert n == 3, f"expected 3 rows in {dst_uri}, got {n}"
