# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Testcontainers fixtures — live Postgres / MySQL / MongoDB / MinIO via Docker.

Every fixture skips with a clear message when Docker is not available, so the
test suite remains green on developer machines without Docker. CI always has
Docker, so the integration matrix runs there.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Iterator

import pytest


def _docker_available() -> bool:
    """Return True if a usable Docker daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    try:
        import subprocess

        r = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _testcontainers_available() -> bool:
    """Return True if the ``testcontainers`` package is importable.

    Even with Docker running, the testcontainers Python wrapper is an
    optional dep. Pre-flight the import so fixture setup skips with a
    clear message instead of erroring on a deferred ImportError.
    """
    try:
        import testcontainers  # noqa: F401

        return True
    except ImportError:
        return False


def _live_infra_available() -> bool:
    """Combined gate: both Docker AND testcontainers must be present
    for the fixture to actually start a container."""
    return _docker_available() and _testcontainers_available()


def requires_docker(reason: str = "Docker daemon not available") -> Any:
    """Decorator that skips a test when Docker isn't reachable."""
    return pytest.mark.skipif(not _docker_available(), reason=reason)


def requires_live_infra(
    reason: str = "Live infra requires Docker + testcontainers",
) -> Any:
    """Decorator that skips a test when either Docker or
    testcontainers is unavailable. Use on tests that need to start
    a real container."""
    return pytest.mark.skipif(not _live_infra_available(), reason=reason)


# Allow CI to opt out of slow integration containers via env var.
_INTEGRATION_DISABLED = os.environ.get("FLUID_DISABLE_INTEGRATION") == "1"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[Dict[str, Any]]:
    """Session-scoped Postgres 16 container.

    Yields a dict with: ``host``, ``port``, ``user``, ``password``, ``database``, ``url``.
    Skips if Docker is unavailable.
    """
    if _INTEGRATION_DISABLED or not _live_infra_available():
        pytest.skip("Postgres container requires Docker")

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield {
            "host": pg.get_container_host_ip(),
            "port": int(pg.get_exposed_port(5432)),
            "user": pg.username,
            "password": pg.password,
            "database": pg.dbname,
            "url": pg.get_connection_url(),
        }


@pytest.fixture(scope="session")
def mysql_container() -> Iterator[Dict[str, Any]]:
    if _INTEGRATION_DISABLED or not _live_infra_available():
        pytest.skip("MySQL container requires Docker")

    from testcontainers.mysql import MySqlContainer

    with MySqlContainer("mysql:8.0") as mysql:
        yield {
            "host": mysql.get_container_host_ip(),
            "port": int(mysql.get_exposed_port(3306)),
            "user": mysql.username,
            "password": mysql.password,
            "database": mysql.dbname,
        }


@pytest.fixture(scope="session")
def mongodb_container() -> Iterator[Dict[str, Any]]:
    if _INTEGRATION_DISABLED or not _live_infra_available():
        pytest.skip("MongoDB container requires Docker")

    from testcontainers.mongodb import MongoDbContainer

    with MongoDbContainer("mongo:7.0") as mongo:
        yield {
            "host": mongo.get_container_host_ip(),
            "port": int(mongo.get_exposed_port(27017)),
            "url": mongo.get_connection_url(),
        }


@pytest.fixture(scope="session")
def minio_container() -> Iterator[Dict[str, Any]]:
    """Session-scoped MinIO container (S3-compatible object store).

    Yields a dict with: ``endpoint`` (URL incl. http://), ``host``, ``port``,
    ``access_key``, ``secret_key``, ``region``. Skips when Docker is not
    available so this remains a hard-skip on developer machines without
    Docker. Uses raw ``DockerContainer`` because the
    ``testcontainers-minio`` adapter pulls the heavyweight ``minio`` SDK
    which we do not otherwise depend on.
    """
    if _INTEGRATION_DISABLED or not _live_infra_available():
        pytest.skip("MinIO container requires Docker")

    import time

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.docker_client import DockerClient
    from testcontainers.core.waiting_utils import wait_for_logs

    access_key = "fluidtest"
    secret_key = "fluidtestsecret"
    image = "minio/minio:RELEASE.2024-08-17T01-24-54Z"

    # Pre-pull so DockerContainer.start() doesn't blow up on first use.
    # ``testcontainers-core`` does not implicitly pull missing images.
    docker = DockerClient()
    try:
        docker.client.images.get(image)
    except Exception:
        docker.client.images.pull(image)

    container = (
        DockerContainer(image)
        .with_command("server /data --address :9000")
        .with_env("MINIO_ROOT_USER", access_key)
        .with_env("MINIO_ROOT_PASSWORD", secret_key)
        .with_exposed_ports(9000)
    )
    container.start()
    try:
        wait_for_logs(container, "API:", timeout=30)
        # Grace period — MinIO occasionally finishes log boot but still
        # rejects PUTs for a few hundred ms.
        time.sleep(0.5)
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(9000))
        endpoint = f"http://{host}:{port}"
        yield {
            "endpoint": endpoint,
            "host": host,
            "port": port,
            "access_key": access_key,
            "secret_key": secret_key,
            "region": "us-east-1",
        }
    finally:
        container.stop()


@pytest.fixture(scope="session")
def redpanda_container() -> Iterator[Dict[str, Any]]:
    """Session-scoped Redpanda container — single binary, exposes Kafka API
    on 9092 and Schema Registry HTTP on 8081.

    Yields ``{"bootstrap": "host:9092", "schema_registry": "http://host:8081"}``.
    Skips when Docker is not available.
    """
    if _INTEGRATION_DISABLED or not _live_infra_available():
        pytest.skip("Redpanda container requires Docker")

    import time

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.docker_client import DockerClient
    from testcontainers.core.waiting_utils import wait_for_logs

    image = "redpandadata/redpanda:v24.2.4"
    docker = DockerClient()
    try:
        docker.client.images.get(image)
    except Exception:
        try:
            docker.client.images.pull(image)
        except Exception as exc:
            # Image-pull failure (rate-limit, DNS, registry down,
            # offline machine) — skip with diagnostics rather than
            # erroring on a deferred ``inspect_image`` 404. Operators
            # can pre-pull manually with ``docker pull <image>`` and
            # re-run.
            pytest.skip(f"Could not pull {image} (skipping live Kafka tests): {exc}")

    # Verify the image really is present after the pull attempt — some
    # registry edge cases swallow errors silently. A second ``inspect``
    # is the authoritative check.
    try:
        docker.client.images.get(image)
    except Exception as exc:
        pytest.skip(f"{image} not present after pull attempt; live Kafka tests skipped: {exc}")

    # Redpanda is a single-binary stand-in for Kafka + Schema Registry.
    # We bind 9092 (Kafka) and 8081 (Schema Registry) to ephemeral host
    # ports. ``--advertise-kafka-addr`` must match the host:port we tell
    # callers to connect to so internal/external routing lines up.
    container = (
        DockerContainer(image)
        .with_command(
            "redpanda start "
            "--smp 1 --memory 1G --reserve-memory 0M --overprovisioned "
            "--node-id 0 --check=false "
            "--kafka-addr PLAINTEXT://0.0.0.0:9092 "
            "--advertise-kafka-addr PLAINTEXT://localhost:9092 "
            "--schema-registry-addr 0.0.0.0:8081"
        )
        .with_exposed_ports(9092, 8081)
    )
    container.start()
    try:
        wait_for_logs(container, "Successfully started Redpanda", timeout=60)
        # Schema registry comes up shortly after the kafka log line.
        time.sleep(1.0)
        host = container.get_container_host_ip()
        kafka_port = int(container.get_exposed_port(9092))
        sr_port = int(container.get_exposed_port(8081))
        yield {
            "bootstrap": f"{host}:{kafka_port}",
            "schema_registry": f"http://{host}:{sr_port}",
        }
    finally:
        container.stop()


@pytest.fixture
def seeded_postgres(postgres_container: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Function-scoped Postgres seed: drops and recreates ``public.fluid_test_orders``
    before each test, so tests don't share data state. Yields the same connection
    dict as ``postgres_container``.
    """
    import duckdb

    pg = postgres_container
    con = duckdb.connect(":memory:")
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(
            f"ATTACH 'host={pg['host']} port={pg['port']} user={pg['user']} "
            f"password={pg['password']} dbname={pg['database']}' AS pg (TYPE postgres)"
        )
        con.execute("DROP TABLE IF EXISTS pg.public.fluid_test_orders")
        con.execute(
            "CREATE TABLE pg.public.fluid_test_orders AS "
            "SELECT * FROM (VALUES "
            "(1,'Alice',100.50,TIMESTAMP '2026-04-01 10:00'), "
            "(2,'Bob',250.00,TIMESTAMP '2026-04-02 14:00'), "
            "(3,'Carol',42.00,TIMESTAMP '2026-04-03 09:30'), "
            "(4,'Diane',900.00,TIMESTAMP '2026-04-04 16:15'), "
            "(5,'Eve',55.55,TIMESTAMP '2026-04-05 11:45')"
            ") t(id, customer, amount, placed_at)"
        )
    finally:
        con.close()
    yield pg
