# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A moto S3 server for duckdb runner tests that land rows in a bucket.

The server runs in this process. Every ``AWS_*`` credential source is removed for
the test, so nothing can reach a real account: the contract hands DuckDB moto's
endpoint and dummy keys through ``connection.s3``. Skips when moto's ``server``
extra, boto3 or DuckDB's ``httpfs`` extension is missing.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Tuple

import pytest

#: moto's dummy key pair, as the contract's ``connection.s3`` hands it to DuckDB.
DUMMY_KEY = "testing"


def httpfs_available() -> bool:
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        return True
    except Exception:  # noqa: BLE001 — offline, or no extension repository
        return False
    finally:
        con.close()


def moto_s3(monkeypatch: pytest.MonkeyPatch, bucket: str) -> Iterator[Tuple[str, Any]]:
    """Yield ``(host:port, boto3 client)`` with ``bucket`` created; for a fixture."""
    try:
        import boto3
        from moto.server import ThreadedMotoServer
    except Exception:  # noqa: BLE001 — the test-emulators extra is absent
        pytest.skip("needs moto[server] and boto3 (pip install -e '.[test-emulators]')")
    if not httpfs_available():
        pytest.skip("DuckDB's httpfs extension cannot be installed here")
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"127.0.0.1:{port}"
        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://{endpoint}",
            region_name="us-east-1",
            aws_access_key_id=DUMMY_KEY,
            aws_secret_access_key=DUMMY_KEY,
        )
        s3.create_bucket(Bucket=bucket)
        yield endpoint, s3
    finally:
        server.stop()


def duckdb_secret(endpoint: str) -> Dict[str, Any]:
    """The ``connection.s3`` block that points DuckDB at the moto server."""
    return {
        "endpoint": endpoint,
        "url_style": "path",
        "use_ssl": False,
        "region": "us-east-1",
        "key_id": DUMMY_KEY,
        "secret": DUMMY_KEY,
    }
