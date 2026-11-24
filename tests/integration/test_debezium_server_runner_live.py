# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""LIVE proof that the embedded runner starts a stock Debezium Server outside the project.

forge's runner (``DebeziumRunner.run``, the path ``fluid apply`` takes for an
``embedded`` Debezium build) starts the stock ``run.sh`` of a Debezium Server
distribution against a real Postgres. The project holds files that Quarkus and the
launcher would use if the server ran there: a ``.env``, a
``config/application.properties`` and a ``jmx/enable_jmx.sh`` that creates a marker
file. The launcher sources ``./jmx`` and reads ``.env`` from its working directory,
and ``run.sh`` ignores its arguments, so a server started in the project would run
the planted script and read the planted settings. This test shows that:

- the server reads forge's file (the HTTP sink URL exists only there, so the snapshot
  rows reach the real recorder);
- the planted URL receives nothing, the planted ``table.exclude.list`` does not
  apply, and the planted script does not run;
- a second run sends no snapshot rows (the offsets forge names survived);
- after the offsets file is moved to ``<project>/data/offsets.dat`` (where a server
  kept it before forge named the file), a third run still sends no snapshot rows:
  the runner copied the old file across.

Gated off by default. It needs ``FLUID_TEST_DEBEZIUM_SERVER_HOME`` (an unpacked stock
``debezium-server-dist``, the directory that holds ``run.sh``), a JDK that
distribution runs on, and Docker for one Postgres container. The ``Debezium Server +
runner`` job in ``.github/workflows/integration-emulated-heavy.yml`` provides all
three. Everywhere else the module skips.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set

import pytest

from fluid_build.api.runner import RunState
from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
from fluid_build.build_runners.debezium.runner import DebeziumRunner
from tests._infrastructure.testcontainers_fixtures import _docker_available

SERVER_HOME = os.environ.get("FLUID_TEST_DEBEZIUM_SERVER_HOME", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated_heavy,
    pytest.mark.slow,
    # Three server boots of up to a minute each, and a Postgres pull.
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not SERVER_HOME or not _docker_available(),
        reason=(
            "set FLUID_TEST_DEBEZIUM_SERVER_HOME to an unpacked stock Debezium Server "
            "distribution and have Docker running to run the live runner test"
        ),
    ),
]

_POSTGRES_IMAGE = "postgres:16-alpine"
_ROWS = {1, 2, 3}
# One server run: long enough to boot Quarkus and snapshot three rows, short
# enough to keep three runs cheap. The runner kills the server at this limit.
_RUN_SECONDS = 40


class _Recorder:
    """An HTTP endpoint that records every JSON body posted to it."""

    def __init__(self) -> None:
        self.bodies: List[Any] = []
        lock = threading.Lock()
        bodies = self.bodies

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"null")
                except ValueError:
                    body = {"unparsed": raw.decode("utf-8", "replace")}
                with lock:
                    bodies.append(body)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def snapshot_ids(self) -> Set[int]:
        """Ids of the rows read by the snapshot (Debezium op ``r``)."""
        ids: Set[int] = set()
        for body in list(self.bodies):
            if not isinstance(body, dict):
                continue
            payload = body.get("payload", body)
            after = payload.get("after") if isinstance(payload, dict) else None
            if isinstance(payload, dict) and payload.get("op") == "r" and isinstance(after, dict):
                ids.add(int(after["id"]))
        return ids

    def clear(self) -> None:
        del self.bodies[:]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker(
    *args: str, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=300,
        check=check,
    )


@pytest.fixture(scope="module")
def postgres_port() -> Iterator[int]:
    """One Postgres with logical decoding and a three-row table; removed afterwards."""
    name = f"fluid-dbz-runner-live-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_PASSWORD=postgres",
        "-e",
        "POSTGRES_DB=appdb",
        "-p",
        f"127.0.0.1:{port}:5432",
        _POSTGRES_IMAGE,
        "postgres",
        "-c",
        "wal_level=logical",
        "-c",
        "max_replication_slots=4",
        "-c",
        "max_wal_senders=4",
    )
    try:
        # Over TCP: the image's first, temporary server listens on its socket only.
        for _ in range(60):
            ready = _docker(
                "exec",
                name,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "appdb",
                "-tAc",
                "select 1",
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(2)
        else:
            pytest.fail("Postgres did not accept TCP connections within 120s")
        _docker(
            "exec",
            "-i",
            name,
            "psql",
            "-U",
            "postgres",
            "-d",
            "appdb",
            "-v",
            "ON_ERROR_STOP=1",
            stdin=(
                "CREATE TABLE public.orders (id integer PRIMARY KEY, customer text NOT NULL);\n"
                "ALTER TABLE public.orders REPLICA IDENTITY FULL;\n"
                "INSERT INTO public.orders VALUES (1, 'alice'), (2, 'bob'), (3, 'carol');\n"
            ),
        )
        yield port
    finally:
        _docker("rm", "-f", name, check=False)


def _contract(port: int, sink_url: str) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.dbz_live",
        "name": "Debezium live",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "debezium",
                "capabilities": ["cdc", "streaming"],
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "mode": "cdc",
                        "streams": ["public.orders"],
                        "connection": {
                            "host": "127.0.0.1",
                            "port": port,
                            "database": "appdb",
                            "schema": "public",
                            "user": "postgres",
                            "password": "postgres",  # pragma: allowlist secret
                        },
                    },
                    "sink": {"format": "iceberg"},
                    "debezium": {
                        "deployment": {"mode": "embedded"},
                        # The stock launcher, named by path: it lives outside the project.
                        "server_binary": str(Path(SERVER_HOME) / "run.sh"),
                        "timeout_seconds": _RUN_SECONDS,
                        "server": {"sink": {"type": "http", "config": {"url": sink_url}}},
                    },
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [],
    }


def _plant_hostile_project_files(project: Path, decoy_url: str, marker: Path) -> None:
    """Files a server would use if it ran in the project."""
    (project / ".env").write_text(
        f"DEBEZIUM_SINK_HTTP_URL={decoy_url}\nDEBEZIUM_SOURCE_TABLE_EXCLUDE_LIST=public.orders\n"
    )
    (project / "config").mkdir()
    (project / "config" / "application.properties").write_text(
        f"debezium.sink.http.url={decoy_url}\ndebezium.source.table.exclude.list=public.orders\n"
    )
    (project / "jmx").mkdir()
    (project / "jmx" / "enable_jmx.sh").write_text(f'#!/bin/bash\ntouch "{marker}"\n')


def _run_server(project: Path, port: int, sink_url: str) -> None:
    contract = _contract(port, sink_url)
    ctx = build_acquisition_run_context(contract["builds"][0], contract, project)
    result = DebeziumRunner().run(ctx)
    if result.state != RunState.SUCCEEDED:
        config = project / ".fluid" / "debezium" / "bronze.dbz_live" / "ingest"
        config = config / "application.properties"
        # The file holds ${FLUID_DBZ_*} references, never a credential.
        print("--- forge's application.properties ---")
        print(config.read_text() if config.is_file() else "(not written)")
        print("--- the run's error ---")
        print(result.error)
    assert result.state == RunState.SUCCEEDED, result.error


def test_server_runs_outside_the_project_and_keeps_its_offsets(
    postgres_port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (Path(SERVER_HOME) / "run.sh").is_file(), "FLUID_TEST_DEBEZIUM_SERVER_HOME has no run.sh"
    real, decoy = _Recorder(), _Recorder()
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "planted-script-ran"
    _plant_hostile_project_files(project, decoy.url, marker)
    # The runner kills the server at the time limit, so the offsets must already be
    # on disk: flush after every batch rather than rely on the distribution's default.
    monkeypatch.setenv("DEBEZIUM_SOURCE_OFFSET_FLUSH_INTERVAL_MS", "0")
    # The http sink reads debezium.format.value and has no default for it (3.7):
    # the operator names the format, as for any sink that needs one.
    monkeypatch.setenv("DEBEZIUM_FORMAT_VALUE", "json")
    state_dir = project / ".fluid" / "debezium" / "bronze.dbz_live" / "ingest"
    offsets = state_dir / "offsets.dat"
    try:
        # 1. First run: the server reads forge's file and snapshots the table.
        _run_server(project, postgres_port, real.url)
        assert real.snapshot_ids() == _ROWS, (
            "the real recorder did not get the snapshot rows, so the server did not read "
            f"forge's file (or the planted settings applied): {real.bodies[:3]}"
        )
        assert decoy.bodies == [], "the planted sink URL received events"
        assert not marker.exists(), "the project's jmx/enable_jmx.sh ran"
        assert offsets.is_file() and offsets.stat().st_size > 0, "no offsets file under forge's dir"
        assert not (project / "data").exists(), "the server wrote state into the project"

        # 2. Second run: the offsets survived, so there is no new snapshot.
        real.clear()
        _run_server(project, postgres_port, real.url)
        assert real.snapshot_ids() == set(), f"a second run snapshotted again: {real.bodies[:3]}"
        assert decoy.bodies == [] and not marker.exists()

        # 3. The offsets move to where a server kept them before forge named the file;
        # the runner copies them back, so there is still no new snapshot.
        (project / "data").mkdir()
        old = project / "data" / "offsets.dat"
        shutil.move(str(offsets), str(old))
        real.clear()
        _run_server(project, postgres_port, real.url)
        assert (
            real.snapshot_ids() == set()
        ), f"the old offsets were not picked up: {real.bodies[:3]}"
        assert offsets.is_file() and old.is_file()
        assert decoy.bodies == [] and not marker.exists()
    finally:
        real.close()
        decoy.close()
