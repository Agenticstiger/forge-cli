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

"""Keyless emulator fixtures — in-process AWS (moto) and Snowflake (fakesnow).

These let provider integration tests run on every PR — including from forks —
with zero cloud credentials. ``moto`` mocks the AWS APIs in-process; ``fakesnow``
patches ``snowflake-connector-python`` and runs queries on an embedded DuckDB.

Each fixture skips with a clear message when its emulator package is missing,
so the suite stays green without the ``test-emulators`` extra. The ci.yml
``emulated-integration`` job installs the extra and runs the ``emulated``-marked
tests for real.

Adding a new keyless integration test: drop a ``test_*.py`` in
``tests/providers/``, set ``pytestmark = [pytest.mark.integration,
pytest.mark.emulated]``, and request one of these fixtures. No workflow edit is
needed — ``ci.yml`` selects by the ``emulated`` marker.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Iterator

import pytest

# Stable identifiers the emulator fixtures provision. Tests can rely on these.
EMULATED_AWS_REGION = "us-east-1"
EMULATED_GLUE_DATABASE = "forge_emulated"


def _have_moto() -> bool:
    """Return True if the ``moto`` AWS emulator is importable."""
    try:
        import moto  # noqa: F401

        return True
    except ImportError:
        return False


def _have_fakesnow() -> bool:
    """Return True if the ``fakesnow`` Snowflake emulator is importable."""
    try:
        import fakesnow  # noqa: F401

        return True
    except ImportError:
        return False


def requires_moto(
    reason: str = "moto not installed — pip install -e '.[test-emulators]'",
) -> Any:
    """Decorator skipping a test when ``moto`` is unavailable."""
    return pytest.mark.skipif(not _have_moto(), reason=reason)


def requires_fakesnow(
    reason: str = "fakesnow not installed — pip install -e '.[test-emulators]'",
) -> Any:
    """Decorator skipping a test when ``fakesnow`` is unavailable."""
    return pytest.mark.skipif(not _have_fakesnow(), reason=reason)


@pytest.fixture
def moto_glue_client() -> Iterator[Any]:
    """In-process AWS Glue client (moto).

    Yields a ``boto3`` Glue client wired to the moto mock, with an empty
    ``forge_emulated`` database already created. No real AWS credentials are
    read or required — dummy credentials are passed explicitly so boto3 cannot
    pick up ambient ones.
    """
    if not _have_moto():
        pytest.skip("moto not installed — pip install -e '.[test-emulators]'")

    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client(
            "glue",
            region_name=EMULATED_AWS_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # noqa: S106 — dummy, moto only
            aws_session_token="testing",
        )
        client.create_database(DatabaseInput={"Name": EMULATED_GLUE_DATABASE})
        yield client


@pytest.fixture
def fakesnow_patch() -> Iterator[None]:
    """Activate the ``fakesnow`` Snowflake emulator for the test's duration.

    ``snowflake-connector-python`` is patched in-process, so any code under test
    that opens a Snowflake connection — including forge-cli's
    ``SnowflakeConnection`` — runs against an embedded DuckDB with no account,
    no network, and no credentials. The test body executes inside the patched
    context.
    """
    if not _have_fakesnow():
        pytest.skip("fakesnow not installed — pip install -e '.[test-emulators]'")

    import fakesnow

    with fakesnow.patch():
        yield


# ── GCP — bigquery-emulator (Docker) ────────────────────────────────────

EMULATED_BQ_PROJECT = "forge-emulated"
_BQ_EMULATOR_IMAGE = "ghcr.io/goccy/bigquery-emulator:latest"


def _have_bigquery() -> bool:
    """Return True if the ``google-cloud-bigquery`` client is importable."""
    try:
        import google.cloud.bigquery  # noqa: F401

        return True
    except ImportError:
        return False


def _docker_available() -> bool:
    """Return True if a usable Docker daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def bigquery_emulator_client() -> Iterator[Any]:
    """BigQuery client backed by the ``bigquery-emulator`` container.

    Starts ``goccy/bigquery-emulator`` (a local BigQuery API emulator),
    waits for its REST API, and yields a ``google-cloud-bigquery`` Client
    wired to it with anonymous credentials — no GCP project, no service
    account, no credentials.

    The emulator image is published for amd64 only, so ``--platform
    linux/amd64`` is forced: native on CI runners, emulated on arm64 dev
    machines. Skips when ``google-cloud-bigquery`` or Docker is unavailable.
    """
    if not _have_bigquery():
        pytest.skip("google-cloud-bigquery not installed — pip install -e '.[test-emulators]'")
    if not _docker_available():
        pytest.skip("Docker not available — required for bigquery-emulator")

    import time
    import uuid

    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    name = f"fluid-bq-emu-{uuid.uuid4().hex[:8]}"
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--platform",
            "linux/amd64",
            "--name",
            name,
            "-p",
            "9050",
            _BQ_EMULATOR_IMAGE,
            f"--project={EMULATED_BQ_PROJECT}",
            "--port=9050",
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start bigquery-emulator: {started.stderr.strip()[:200]}")

    try:
        mapping = subprocess.run(
            ["docker", "port", name, "9050/tcp"], capture_output=True, text=True
        ).stdout.strip()
        if not mapping:
            pytest.skip("bigquery-emulator: Docker reported no host port mapping")
        host_port = int(mapping.splitlines()[0].rsplit(":", 1)[-1])

        client = bigquery.Client(
            project=EMULATED_BQ_PROJECT,
            client_options=ClientOptions(api_endpoint=f"http://localhost:{host_port}"),
            credentials=AnonymousCredentials(),
        )

        # Readiness: poll a trivial round-trip until the REST API answers.
        last_error: Exception | None = None
        for _ in range(60):
            try:
                list(client.list_datasets())
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.5)
        else:
            pytest.skip(f"bigquery-emulator not ready within 30s: {last_error}")

        yield client
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ── Snowflake — fakesnow server mode (over the wire) ─────────────────────
#
# ``fakesnow_patch`` above replaces snowflake-connector-python in-process:
# the connector's own internals never run. That proves forge-cli calls the
# connector correctly, and nothing below it.
#
# fakesnow also ships a Starlette app speaking the real Snowflake HTTP
# wire protocol (``fakesnow -s``; https://github.com/tekumara/fakesnow,
# Apache-2.0). Served locally, the UNPATCHED connector performs a real
# login request, submits real queries and decodes real result sets — so a
# connector-version regression in any of those surfaces fails the test.
#
# What is still patched, and why: ``snowflake.connector.connect`` resolves
# its URL from the account name (``<account>.snowflakecomputing.com``) and
# offers no endpoint override, so the fixture injects ``host``/``port``/
# ``protocol`` and nothing else. Every other argument forge-cli passes is
# its own. This is transport redirection, not behaviour substitution.
#
# Fidelity ceiling, unchanged from in-process mode: fakesnow executes on
# DuckDB and accepts a more liberal dialect than Snowflake. Snowflake SQL
# correctness remains the job of the Stage-3 live suite.

_FAKESNOW_SERVER_ACCOUNT = "forge_emulated"


def _have_fakesnow_server() -> bool:
    """Return True if fakesnow's optional server extra is importable."""
    try:
        import fakesnow.server  # noqa: F401
        import uvicorn  # noqa: F401

        return True
    except ImportError:
        return False


def requires_fakesnow_server(
    reason: str = "fakesnow server extra not installed — pip install -e '.[test-emulators]'",
) -> Any:
    """Decorator skipping a test when fakesnow's server extra is unavailable."""
    return pytest.mark.skipif(not _have_fakesnow_server(), reason=reason)


@pytest.fixture(scope="session")
def fakesnow_server() -> Iterator[int]:
    """Serve fakesnow's Snowflake-wire-protocol app; yield the bound port.

    Session-scoped: one uvicorn thread serves every test in the run. The
    port is claimed from the OS rather than hard-coded so parallel CI jobs
    on one runner cannot collide.
    """
    if not _have_fakesnow_server():
        pytest.skip("fakesnow server extra not installed")

    import socket
    import threading
    import time as _time

    import fakesnow.server
    import uvicorn

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = uvicorn.Server(
        uvicorn.Config(fakesnow.server.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            _time.sleep(0.1)
        else:
            pytest.skip("fakesnow server did not start within 10s")
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def fakesnow_server_target(fakesnow_server: int, monkeypatch: Any) -> Iterator[str]:
    """Point ``snowflake.connector.connect`` at the local fakesnow server.

    Injects ``host``/``port``/``protocol`` only — see the module note above.
    Yields the account name callers should pass to forge-cli.
    """
    import snowflake.connector

    real_connect = snowflake.connector.connect

    def _connect_to_emulator(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(host="127.0.0.1", port=fakesnow_server, protocol="http")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snowflake.connector, "connect", _connect_to_emulator)
    yield _FAKESNOW_SERVER_ACCOUNT
