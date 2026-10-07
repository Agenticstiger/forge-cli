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

"""LIVE proof: forge-derived Iceberg sink configs on a real Kafka Connect worker.

The Kafka Connect Iceberg-sink deriver once emitted BOTH ``iceberg.catalog.type``
and ``iceberg.catalog.catalog-impl``. Apache Iceberg's ``CatalogUtil`` refuses
that ("both type and catalog-impl are set"), so every Glue sink crashed at
startup, and no unit test could see it: the refusal lives in the worker's
runtime. This module runs forge's REAL derivation chain
(``find_iceberg_expose_binding`` -> ``resolve_iceberg_catalog`` ->
``emit_iceberg_sink_config``) and hands the result to a real worker:

* ``location.catalog: Lakekeeper`` streams records into a real Lakekeeper REST
  catalog: ``type=rest``, the warehouse addressed by NAME, no FileIO and no
  storage keys, because Lakekeeper vends the table's storage credentials (STS
  against Silo, an S3-compatible store). The Apache sink creates the namespace
  itself (apache/iceberg#10186).
* The same worker refuses a config carrying both selector keys, which is the
  failure every Glue sink used to hit.
* The forge-derived Glue config (``catalog-impl`` only) gets past that gate on
  the same worker. It never writes: the worker has no AWS account, and no record
  is produced for it.

Stack (every image pinned by digest, all multi-arch): Postgres + Lakekeeper,
Silo for S3 + STS (``minio/minio`` and ``minio/mc`` left Docker Hub on
2026-09-11), a single-node KRaft Kafka, and cp-kafka-connect with the ASF-owned
Apache Iceberg sink ZIP, which a one-shot container downloads and verifies by
sha1 (no confluent-hub CLI: that is under the Confluent Enterprise License).

Gating: ``integration`` + ``emulated_heavy``; self-skips unless
``FLUID_TEST_LAKEKEEPER=1`` and Docker is reachable. Docker and heavy emulator
tests run only in the CI integration stage (the ``lakekeeper-integration`` job
of ``.github/workflows/integration-emulated-heavy.yml``), never in the light
suite. Locally:

    FLUID_TEST_LAKEKEEPER=1 \\
    FLUID_LK_PLUGIN_CACHE=~/.cache/fluid/iceberg-kafka-connect \\
      python -m pytest -v -m emulated_heavy \\
      tests/integration/test_lakekeeper_kafka_connect_live.py

Optional environment:

* ``FLUID_LK_PLUGIN_CACHE``: host directory for the unzipped sink plugin, kept
  across runs (CI caches it). Unset, a compose volume holds it and teardown
  removes it, so every run downloads it again.
* ``FLUID_LK_PROJECT``: compose project name (default ``fluid-lk-<random>``), so
  CI can collect logs from, and tear down, a run the test could not clean up.
* ``FLUID_LK_LOG_DIR``: on failure, the compose logs are also written here.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
)
from fluid_build.providers._iceberg_catalog import (
    GLUE_CATALOG_IMPL,
    find_iceberg_expose_binding,
    resolve_iceberg_catalog,
)
from tests.integration._iceberg_objectstore_live import _docker_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated_heavy,
    pytest.mark.slow,
    # The first test also pays for the module stack: image pulls, the plugin
    # download and the worker's warm-up commit rounds. The 600 s default from
    # pyproject.toml covers setup + call + teardown, which is not enough.
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        os.environ.get("FLUID_TEST_LAKEKEEPER") != "1" or not _docker_available(),
        reason=(
            "set FLUID_TEST_LAKEKEEPER=1 and have Docker running to run the "
            "Lakekeeper + Kafka Connect live test"
        ),
    ),
]

# Throwaway credentials for containers that live only as long as this module.
_PG_PASSWORD = "postgres"  # pragma: allowlist secret
_LK_ENCRYPTION_KEY = "fluid-lakekeeper-live-test-only"  # pragma: allowlist secret
_S3_USER = "fluid-silo"
_S3_PASSWORD = "fluid-silo-live-test-only"  # pragma: allowlist secret

_BUCKET = "forge"
_WAREHOUSE = "forge"
_S3_REGION = "local-01"
_NAMESPACE = "streaming"
_RECORDS = 5

#: Connect-side address of the catalog (the compose network, not the host).
_CATALOG_URI = "http://lakekeeper:8181/catalog"
_KAFKA_BIN = "/opt/kafka/bin"
_BOTH_KEYS_ERROR = "both type and catalog-impl are set"

_COMPOSE = """\
services:
  db:
    image: postgres:17.11@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f
    environment:
      POSTGRES_PASSWORD: ${LK_PG_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d postgres"]
      interval: 2s
      timeout: 5s
      retries: 60

  migrate:
    image: quay.io/lakekeeper/catalog:v0.13.6@sha256:d6829722cac0d00dfc5665b0955766387b93ccadd8b1e70e679b49619bc30ea5
    command: ["migrate"]
    restart: "no"
    environment: &lakekeeper-env
      LAKEKEEPER__PG_DATABASE_URL_READ: postgresql://postgres:${LK_PG_PASSWORD}@db:5432/postgres
      LAKEKEEPER__PG_DATABASE_URL_WRITE: postgresql://postgres:${LK_PG_PASSWORD}@db:5432/postgres
      LAKEKEEPER__PG_ENCRYPTION_KEY: ${LK_ENCRYPTION_KEY}
      LAKEKEEPER__AUTHZ_BACKEND: allowall
    depends_on:
      db:
        condition: service_healthy

  silo:
    image: pgsty/silo:RELEASE.2026-09-03T13-18-01Z@sha256:b616a0cf8cb281e7e6bb3c9b1fb53875b4016a2878223925541c18f82d6c5ca3
    command: ["server", "/data"]
    environment:
      MINIO_ROOT_USER: ${LK_S3_USER}
      MINIO_ROOT_PASSWORD: ${LK_S3_PASSWORD}
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 2s
      timeout: 5s
      retries: 60

  create-bucket:
    image: pgsty/silo:RELEASE.2026-09-03T13-18-01Z@sha256:b616a0cf8cb281e7e6bb3c9b1fb53875b4016a2878223925541c18f82d6c5ca3
    restart: "no"
    entrypoint: ["/bin/sh", "-ec"]
    command:
      - mc alias set local http://silo:9000 "$$S3_USER" "$$S3_PASSWORD" && mc mb --ignore-existing local/forge
    environment:
      S3_USER: ${LK_S3_USER}
      S3_PASSWORD: ${LK_S3_PASSWORD}
    depends_on:
      silo:
        condition: service_healthy

  lakekeeper:
    image: quay.io/lakekeeper/catalog:v0.13.6@sha256:d6829722cac0d00dfc5665b0955766387b93ccadd8b1e70e679b49619bc30ea5
    command: ["serve"]
    environment: *lakekeeper-env
    depends_on:
      migrate:
        condition: service_completed_successfully
      create-bucket:
        condition: service_completed_successfully
    ports:
      - "127.0.0.1:${LK_CATALOG_PORT}:8181"
    healthcheck:
      # Distroless image: no shell, so exec form only.
      test: ["CMD", "/home/nonroot/lakekeeper", "healthcheck"]
      interval: 2s
      timeout: 5s
      retries: 60

  kafka:
    image: apache/kafka:3.9.1@sha256:4ceccc577f03f51f6af8dbfda55194d0d892f4fa7913ffbded567ce3895622ed
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:19092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:19092
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@localhost:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      # The sink's commit coordinator writes through a transactional producer;
      # with the default RF of 3 a single broker can never commit.
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
      KAFKA_HEAP_OPTS: -Xmx512m
    healthcheck:
      test: ["CMD-SHELL", "/opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:19092 >/dev/null 2>&1"]
      interval: 5s
      timeout: 20s
      retries: 40
      start_period: 10s

  connect-plugins:
    image: alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
    restart: "no"
    volumes:
      - ${LK_PLUGIN_SOURCE}:/plugins
    entrypoint: ["/bin/sh", "-euc"]
    command:
      - |
        sha1=52dab2ff2b9659008deec803d1c1e92813c5ffa7
        if [ "$$(cat /plugins/.sha1 2>/dev/null || true)" = "$$sha1" ]; then
          echo "iceberg sink plugin cached"; exit 0
        fi
        wget -q -O /tmp/sink.zip https://hub-downloads.confluent.io/api/plugins/iceberg/iceberg-kafka-connect/versions/1.9.2/iceberg-iceberg-kafka-connect-1.9.2.zip
        echo "$$sha1  /tmp/sink.zip" | sha1sum -c -
        rm -rf /plugins/iceberg-iceberg-kafka-connect-*
        unzip -q /tmp/sink.zip -d /plugins
        rm -rf /plugins/__MACOSX
        chmod -R a+rX /plugins
        echo "$$sha1" > /plugins/.sha1
        echo "iceberg sink plugin installed"

  connect:
    image: confluentinc/cp-kafka-connect:7.9.10@sha256:b174fd9317b1864a22b01da266407a802a56c5d1ab9ee77aef03d2df9854fdc9
    depends_on:
      kafka:
        condition: service_healthy
      connect-plugins:
        condition: service_completed_successfully
      lakekeeper:
        condition: service_healthy
    ports:
      - "127.0.0.1:${LK_CONNECT_PORT}:8083"
    volumes:
      - ${LK_PLUGIN_SOURCE}:/plugins:ro
    environment:
      CONNECT_BOOTSTRAP_SERVERS: kafka:19092
      CONNECT_REST_ADVERTISED_HOST_NAME: connect
      CONNECT_GROUP_ID: fluid-lakekeeper-live
      CONNECT_CONFIG_STORAGE_TOPIC: _connect-configs
      CONNECT_OFFSET_STORAGE_TOPIC: _connect-offsets
      CONNECT_STATUS_STORAGE_TOPIC: _connect-status
      CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR: 1
      CONNECT_OFFSET_STORAGE_REPLICATION_FACTOR: 1
      CONNECT_STATUS_STORAGE_REPLICATION_FACTOR: 1
      CONNECT_KEY_CONVERTER: org.apache.kafka.connect.storage.StringConverter
      CONNECT_VALUE_CONVERTER: org.apache.kafka.connect.json.JsonConverter
      CONNECT_VALUE_CONVERTER_SCHEMAS_ENABLE: "false"
      CONNECT_PLUGIN_PATH: /usr/share/java,/plugins
      CONNECT_CONFIG_PROVIDERS: env
      CONNECT_CONFIG_PROVIDERS_ENV_CLASS: org.apache.kafka.common.config.provider.EnvVarConfigProvider
      KAFKA_HEAP_OPTS: -Xms256m -Xmx768m
    healthcheck:
      test: ["CMD-SHELL", "curl -sf localhost:8083/connector-plugins | grep -q IcebergSinkConnector"]
      interval: 5s
      timeout: 10s
      retries: 60
      start_period: 30s

volumes:
  plugins: {}
"""


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _free_ports(count: int) -> List[int]:
    """``count`` distinct free loopback ports (all held open while picking)."""
    socks = []
    try:
        for _ in range(count):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _parse(body: str) -> Any:
    """JSON from a Connect or Lakekeeper response. ``strict=False``: a Connect
    task trace is a Java stack trace and may carry raw control characters."""
    return json.loads(body, strict=False) if body.strip() else {}


def _http(
    method: str,
    url: str,
    body: Optional[Mapping[str, Any]] = None,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
) -> Tuple[int, str]:
    """One request against a loopback service; returns ``(status, body)``,
    including for 4xx/5xx, so callers can assert on the error body."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 — loopback only
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


@dataclass
class _Stack:
    project: str
    workdir: Path
    env: Dict[str, str]
    catalog_url: str
    connect_url: str
    prefix: str = ""

    def compose(
        self, *args: str, stdin: Optional[str] = None, timeout: int = 120
    ) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["docker", "compose", "-p", self.project, *args],
            cwd=self.workdir,
            env={**os.environ, **self.env},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def kafka(self, script: str, *args: str, stdin: Optional[str] = None) -> str:
        """Run a Kafka CLI inside the broker container; no host Kafka client."""
        done = self.compose("exec", "-T", "kafka", f"{_KAFKA_BIN}/{script}", *args, stdin=stdin)
        assert done.returncode == 0, f"{script} {args}: {done.stdout}\n{done.stderr}"
        return done.stdout

    def worker_log(self) -> str:
        return self.compose("logs", "--no-color", "connect", timeout=60).stdout

    def dump_logs(self, label: str) -> str:
        logs = self.compose("logs", "--no-color", "--tail=200", timeout=60)
        text = logs.stdout + logs.stderr
        print(f"\n===== docker compose -p {self.project} logs ({label}) =====\n{text}")
        log_dir = os.environ.get("FLUID_LK_LOG_DIR")
        if log_dir:
            out = Path(log_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{self.project}-{label}.log").write_text(text)
        return text

    @contextlib.contextmanager
    def logs_on_failure(self, label: str) -> Iterator[None]:
        try:
            yield
        except pytest.skip.Exception:
            raise
        except BaseException:
            self.dump_logs(label)
            raise


def _bootstrap_lakekeeper(stack: _Stack) -> str:
    """Bootstrap Lakekeeper, create the ``forge`` warehouse on Silo, and return
    the REST path prefix the catalog assigned it (never hard-coded)."""
    base = stack.catalog_url
    status, body = _http("GET", f"{base}/health")
    assert status == 200, f"GET /health -> {status}: {body}"

    status, body = _http("POST", f"{base}/management/v1/bootstrap", {"accept-terms-of-use": True})
    assert status == 204 or (
        status == 400 and "CatalogAlreadyBootstrapped" in body
    ), f"bootstrap -> {status}: {body}"

    warehouse = {
        "warehouse-name": _WAREHOUSE,
        "storage-profile": {
            "type": "s3",
            "bucket": _BUCKET,
            "region": _S3_REGION,
            "endpoint": "http://silo:9000",
            "sts-endpoint": "http://silo:9000",
            "path-style-access": True,
            "flavor": "s3-compat",
            "sts-enabled": True,
        },
        "storage-credential": {
            "type": "s3",
            "credential-type": "access-key",
            "aws-access-key-id": _S3_USER,
            "aws-secret-access-key": _S3_PASSWORD,
        },
    }
    status, body = _http("POST", f"{base}/management/v1/warehouse", warehouse)
    assert status in (200, 201, 409), f"create warehouse -> {status}: {body}"

    status, body = _http("GET", f"{base}/catalog/v1/config?warehouse={_WAREHOUSE}")
    assert status == 200, f"GET /catalog/v1/config -> {status}: {body}"
    config = _parse(body)
    # The order an Iceberg REST client applies them: defaults, then overrides.
    merged = {**(config.get("defaults") or {}), **(config.get("overrides") or {})}
    prefix = merged.get("prefix")
    assert prefix, f"Lakekeeper /v1/config carries no prefix: {config}"
    return str(prefix)


@pytest.fixture(scope="module")
def lakekeeper_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    workdir = tmp_path_factory.mktemp("lakekeeper-live")
    (workdir / "docker-compose.yml").write_text(_COMPOSE)
    project = os.environ.get("FLUID_LK_PROJECT") or f"fluid-lk-{uuid.uuid4().hex[:8]}"

    cache = os.environ.get("FLUID_LK_PLUGIN_CACHE")
    if cache:
        cache_dir = Path(cache).expanduser().resolve()
        # Created here, not by the Docker daemon, so the directory is ours.
        cache_dir.mkdir(parents=True, exist_ok=True)
        plugin_source = str(cache_dir)
    else:
        plugin_source = "plugins"  # the compose volume; removed by `down -v`

    catalog_port, connect_port = _free_ports(2)
    stack = _Stack(
        project=project,
        workdir=workdir,
        env={
            "LK_PG_PASSWORD": _PG_PASSWORD,
            "LK_ENCRYPTION_KEY": _LK_ENCRYPTION_KEY,
            "LK_S3_USER": _S3_USER,
            "LK_S3_PASSWORD": _S3_PASSWORD,
            "LK_CATALOG_PORT": str(catalog_port),
            "LK_CONNECT_PORT": str(connect_port),
            "LK_PLUGIN_SOURCE": plugin_source,
        },
        catalog_url=f"http://127.0.0.1:{catalog_port}",
        connect_url=f"http://127.0.0.1:{connect_port}",
    )
    try:
        pull = stack.compose("pull", "--quiet", "--policy", "missing", timeout=600)
        if pull.returncode != 0:
            pytest.fail(f"docker compose pull failed:\n{pull.stderr[-3000:]}")
        # Name only the long-running leaves: `--wait` on a one-shot fails the
        # moment it exits, even with status 0. Their dependencies come up too.
        up = stack.compose(
            "up", "-d", "--wait", "--wait-timeout", "420", "lakekeeper", "connect", timeout=600
        )
        if up.returncode != 0:
            stack.dump_logs("bring-up")
            pytest.fail(
                f"the Lakekeeper + Kafka Connect stack did not come up:\n{up.stderr[-3000:]}"
            )
        with stack.logs_on_failure("bootstrap"):
            stack.prefix = _bootstrap_lakekeeper(stack)
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", timeout=300)


# ---------------------------------------------------------------------------
# Contract + derivation (forge's real code only)
# ---------------------------------------------------------------------------


def _contract(product_id: str, location: Mapping[str, Any]) -> Dict[str, Any]:
    """A contract with one Iceberg expose and one Kafka Connect build writing it."""
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": product_id,
        "name": product_id.rsplit(".", 1)[-1],
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "aws", "format": "iceberg", "location": dict(location)},
            }
        ],
        "builds": [
            {
                "id": "stream_orders",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "outputs": ["orders"],
                "properties": {
                    "sink": {"format": "iceberg"},
                    "kafka-connect": {
                        "streamingSink": {"autoCreate": True, "commitIntervalMs": 5000}
                    },
                },
            }
        ],
    }


def _derive(contract: Mapping[str, Any], topic: str) -> Dict[str, str]:
    """The sink config exactly as the Kafka Connect runner derives it."""
    assert iceberg_sink_preflight(contract, "stream_orders") is None
    binding = find_iceberg_expose_binding(contract)
    assert binding is not None, "the contract's Iceberg expose was not found"
    resolved = resolve_iceberg_catalog(binding, contract=contract)
    kc_props = contract["builds"][0]["properties"]["kafka-connect"]
    return emit_iceberg_sink_config(
        resolved, product_id=contract["id"], topics=[topic], kc_props=kc_props
    )


def _lakekeeper_location(table: str) -> Dict[str, Any]:
    return {
        "catalog": "Lakekeeper",  # any spelling classifies the same way
        "uri": _CATALOG_URI,
        "warehouse": _WAREHOUSE,
        "database": _NAMESPACE,
        "table": table,
        "region": _S3_REGION,
    }


# ---------------------------------------------------------------------------
# Kafka Connect + catalog probes
# ---------------------------------------------------------------------------


def _put_connector(stack: _Stack, name: str, config: Mapping[str, str]) -> None:
    status, body = _http("PUT", f"{stack.connect_url}/connectors/{name}/config", config)
    assert status in (200, 201), f"PUT /connectors/{name}/config -> {status}: {body}"


def _delete_connector(stack: _Stack, name: str) -> None:
    _http("DELETE", f"{stack.connect_url}/connectors/{name}")


def _status(stack: _Stack, name: str) -> Optional[Dict[str, Any]]:
    status, body = _http("GET", f"{stack.connect_url}/connectors/{name}/status")
    if status == 404:  # Connect's status store lags a fresh PUT
        return None
    assert status == 200, f"GET /connectors/{name}/status -> {status}: {body}"
    return _parse(body)


def _failed_task(status: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for task in status.get("tasks") or []:
        if task.get("state") == "FAILED":
            return task
    return None


def _wait_running(stack: _Stack, name: str, *, deadline_s: float = 180) -> None:
    """Connector and every task RUNNING; a FAILED task fails with its trace."""
    deadline = time.monotonic() + deadline_s
    last: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        last = _status(stack, name)
        if last:
            failed = _failed_task(last)
            if failed or last.get("connector", {}).get("state") == "FAILED":
                trace = (failed or last.get("connector") or {}).get("trace", "")
                pytest.fail(f"connector {name} FAILED:\n{trace}")
            tasks = last.get("tasks") or []
            if (
                last.get("connector", {}).get("state") == "RUNNING"
                and tasks
                and all(t.get("state") == "RUNNING" for t in tasks)
            ):
                return
        time.sleep(2)
    pytest.fail(f"connector {name} not RUNNING after {deadline_s:.0f}s: {last}")


def _create_topic(stack: _Stack, topic: str) -> None:
    stack.kafka(
        "kafka-topics.sh",
        "--bootstrap-server",
        "localhost:19092",
        "--create",
        "--if-not-exists",
        "--topic",
        topic,
        "--partitions",
        "1",
        "--replication-factor",
        "1",
    )


def _table_path(stack: _Stack, table: str) -> str:
    return f"{stack.catalog_url}/catalog/v1/{stack.prefix}/namespaces/{_NAMESPACE}/tables/{table}"


def _load_table(stack: _Stack, table: str) -> Optional[Dict[str, Any]]:
    status, body = _http(
        "GET",
        _table_path(stack, table),
        headers={"X-Iceberg-Access-Delegation": "client-managed"},
    )
    if status == 404:
        return None
    assert status == 200, f"loadTable {_NAMESPACE}.{table} -> {status}: {body}"
    return _parse(body)


def _total_records(loaded: Mapping[str, Any]) -> int:
    """``total-records`` of the CURRENT snapshot (0 before the first commit)."""
    metadata = loaded.get("metadata") or {}
    current = metadata.get("current-snapshot-id")
    for snapshot in metadata.get("snapshots") or []:
        if snapshot.get("snapshot-id") == current:
            return int((snapshot.get("summary") or {}).get("total-records", 0))
    return 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_derived_lakekeeper_config_streams_into_lakekeeper(request: pytest.FixtureRequest):
    """forge's derived ``catalog: Lakekeeper`` config commits every record into a
    real Lakekeeper through a real worker, with the namespace and table created
    by the sink and the storage credentials vended by the catalog."""
    suffix = uuid.uuid4().hex[:8]
    table = f"orders_{suffix}"
    topic = f"orders-{suffix}"
    connector = f"fluid-lk-sink-{suffix}"
    config = _derive(
        _contract(f"live.lakekeeper.orders_{suffix}", _lakekeeper_location(table)), topic
    )

    # Checked before any container starts: a deriver regression fails here in
    # well under a second.
    assert config["iceberg.catalog.type"] == "rest"
    assert "iceberg.catalog.catalog-impl" not in config
    assert "iceberg.catalog.io-impl" not in config  # Lakekeeper vends the FileIO config
    assert not [k for k in config if k.startswith("iceberg.catalog.s3.")]
    assert config["iceberg.catalog.uri"] == _CATALOG_URI
    assert config["iceberg.catalog.warehouse"] == _WAREHOUSE  # a NAME, not s3://
    assert config["iceberg.tables"] == f"{_NAMESPACE}.{table}"

    stack: _Stack = request.getfixturevalue("lakekeeper_stack")
    with stack.logs_on_failure(request.node.name):
        try:
            _create_topic(stack, topic)
            _put_connector(stack, connector, config)
            _wait_running(stack, connector)

            records = "".join(
                json.dumps({"id": i, "customer": f"c{i}", "amount_cents": 100 * i}) + "\n"
                for i in range(1, _RECORDS + 1)
            )
            stack.kafka(
                "kafka-console-producer.sh",
                "--bootstrap-server",
                "localhost:19092",
                "--topic",
                topic,
                stdin=records,
            )

            # The worker's first commit rounds time out while it warms up (up
            # to about 2.5 minutes observed), hence the generous deadline.
            deadline = time.monotonic() + 360
            loaded: Optional[Dict[str, Any]] = None
            total = -1
            while time.monotonic() < deadline:
                status = _status(stack, connector) or {}
                failed = _failed_task(status)
                if failed:
                    pytest.fail(f"sink task FAILED while committing:\n{failed.get('trace', '')}")
                loaded = _load_table(stack, table)
                total = _total_records(loaded) if loaded else -1
                if total >= _RECORDS:
                    break
                time.sleep(5)
            assert loaded is not None, f"the sink never created {_NAMESPACE}.{table} in Lakekeeper"
            assert total == _RECORDS, f"current snapshot holds {total} records, want {_RECORDS}"

            # Lakekeeper placed the table in the warehouse's bucket: the
            # warehouse NAME resolved to its storage profile.
            location = (loaded.get("metadata") or {}).get("location", "")
            assert location.startswith(f"s3://{_BUCKET}/"), location

            # The sink created the namespace on its own (apache/iceberg#10186).
            status_code, body = _http(
                "GET", f"{stack.catalog_url}/catalog/v1/{stack.prefix}/namespaces/{_NAMESPACE}"
            )
            assert status_code == 200, f"namespace {_NAMESPACE}: {status_code} {body}"
        finally:
            _delete_connector(stack, connector)


def test_both_type_and_catalog_impl_fail_on_a_real_worker(
    lakekeeper_stack: _Stack, request: pytest.FixtureRequest
):
    """The negative control for the bug class: the same config, plus the second
    selector key the old deriver added, never starts on a real worker."""
    stack = lakekeeper_stack
    suffix = uuid.uuid4().hex[:8]
    table = f"orders_{suffix}"
    connector = f"fluid-lk-both-{suffix}"
    config = _derive(
        _contract(f"live.lakekeeper.both_{suffix}", _lakekeeper_location(table)),
        f"orders-{suffix}",
    )
    config["iceberg.catalog.catalog-impl"] = "org.apache.iceberg.rest.RESTCatalog"
    assert config["iceberg.catalog.type"] == "rest"

    with stack.logs_on_failure(request.node.name):
        try:
            # No topic is needed: sink 1.9.2 loads the catalog in
            # IcebergSinkTask.start() (IcebergSinkTask.java:48), so the task
            # dies before it consumes anything.
            _put_connector(stack, connector, config)
            deadline = time.monotonic() + 180
            status: Dict[str, Any] = {}
            failed: Optional[Mapping[str, Any]] = None
            while time.monotonic() < deadline and failed is None:
                status = _status(stack, connector) or {}
                failed = _failed_task(status)
                if failed is None:
                    time.sleep(2)
            assert failed is not None, f"a sink with both selector keys did not fail: {status}"

            # Both surfaces carry the refusal (seen on cp-kafka-connect 7.9.10
            # with sink 1.9.2). The task status trace, which forge's runner
            # reads, starts with the IllegalArgumentException itself; the
            # worker logs "Task threw an uncaught and unrecoverable exception"
            # for the task, then the same exception. The connector stays
            # RUNNING: only the task fails, which is why the runner polls the
            # task states and not just the connector's.
            assert status.get("connector", {}).get("state") == "RUNNING", status
            trace = str(failed.get("trace", ""))
            head = trace.splitlines()[0] if trace else ""
            assert head.startswith("java.lang.IllegalArgumentException: "), trace
            assert f"Cannot create catalog iceberg, {_BOTH_KEYS_ERROR}" in head, trace
            assert "type=rest, catalog-impl=org.apache.iceberg.rest.RESTCatalog" in head, trace

            log = stack.worker_log()
            killed = f"WorkerSinkTask{{id={connector}-0}} Task threw an uncaught and unrecoverable"
            assert killed in log, f"no task-killed line for {connector} in the worker log"
            assert any(
                _BOTH_KEYS_ERROR in line and "RESTCatalog" in line for line in log.splitlines()
            ), "the worker log does not carry the refusal"

            # And nothing reached the catalog.
            assert _load_table(stack, table) is None
        finally:
            _delete_connector(stack, connector)


def test_derived_glue_config_gets_past_the_catalog_gate(request: pytest.FixtureRequest):
    """The forge-derived Glue config (``catalog-impl`` only) gets past the
    ``CatalogUtil`` check on the same worker that refuses both keys.

    No record is produced, so the sink never calls AWS: the GlueCatalog is
    built (region from the binding, credentials resolved lazily) and the task
    stays RUNNING with its commit coordinator started.
    """
    suffix = uuid.uuid4().hex[:8]
    topic = f"orders-glue-{suffix}"
    connector = f"fluid-lk-glue-{suffix}"
    location = {
        "database": _NAMESPACE,
        "table": f"orders_{suffix}",
        "bucket": _BUCKET,
        "region": "us-east-1",
    }  # no ``catalog``: AWS defaults to Glue
    config = _derive(_contract(f"live.glue.orders_{suffix}", location), topic)

    # The startup crash, checked before any container starts.
    assert config["iceberg.catalog.catalog-impl"] == GLUE_CATALOG_IMPL
    assert "iceberg.catalog.type" not in config

    stack: _Stack = request.getfixturevalue("lakekeeper_stack")
    with stack.logs_on_failure(request.node.name):
        try:
            _create_topic(stack, topic)
            _put_connector(stack, connector, config)
            # RUNNING is the proof: sink 1.9.2 builds the catalog in
            # IcebergSinkTask.start(), and Connect reports a task RUNNING only
            # after start() returns. The both-keys config never gets there.
            _wait_running(stack, connector)

            # The leader task then starts the commit coordinator with that
            # catalog, whose consumer group joins the control topic.
            coordinator = f"groupId=connect-{connector}-coord] Adding newly assigned partitions"
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and coordinator not in stack.worker_log():
                time.sleep(2)
            assert coordinator in stack.worker_log(), f"{connector} never started a coordinator"

            status = _status(stack, connector) or {}
            assert [t.get("state") for t in status.get("tasks") or []] == ["RUNNING"], status
            # The worker is shared with the both-keys test, whose refusal names
            # RESTCatalog; the old Glue crash named GlueCatalog.
            refusals = [
                line
                for line in stack.worker_log().splitlines()
                if _BOTH_KEYS_ERROR in line and "GlueCatalog" in line
            ]
            assert not refusals, refusals
        finally:
            _delete_connector(stack, connector)
