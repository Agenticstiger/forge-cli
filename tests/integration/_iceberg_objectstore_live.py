# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared scaffolding for the GCS / Azure Iceberg-sink object-store live tests.

The AWS/S3 twin already exists (``test_debezium_server_iceberg_live.py`` — MinIO +
``apache/iceberg-rest-fixture`` + an ephemeral ``pyiceberg`` read-back container).
These helpers let the GCS (fake-gcs-server) and Azure (Azurite / live ADLS) tests
reuse the SAME shape: forge's REAL deriver builds the sink config, a real object
store + Iceberg catalog then round-trip an Iceberg table at the forge-derived
warehouse, and an independent ``pyiceberg`` read-back proves the rows landed.

Design decisions (mirroring the debezium precedent):

* **Zero new Python deps.** Every heavy Iceberg / pyiceberg / gcsfs / adlfs import
  runs INSIDE ephemeral ``python:3.11-slim`` containers on the compose network —
  never in the forge-cli test venv. The light suite stays untouched.
* **Self-skip, never fail, when the tier is absent.** The gate is an opt-in env
  var AND a Docker reachability probe, exactly like the LocalStack / GCP-emulator
  tiers. Missing Docker / a failed image pull / an unreachable service ⇒
  ``pytest.skip`` with a clear reason, so the default ``pytest`` run is unaffected.
"""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Sequence

# Reuse the single Docker-reachability probe the rest of the suite already uses,
# rather than hand-rolling another one.
from tests._infrastructure.testcontainers_fixtures import _docker_available

__all__ = [
    "_docker_available",
    "compose",
    "wait_for_http",
    "wait_for_tcp",
    "run_pyiceberg_container",
    "EPHEMERAL_PYTHON_IMAGE",
]

# The same base image the debezium read-back uses — already cached on CI runners.
EPHEMERAL_PYTHON_IMAGE = "python:3.11-slim"


def compose(
    args: Sequence[str], cwd: Path, project: str, *, timeout: int = 300
) -> "subprocess.CompletedProcess[str]":
    """Run ``docker compose -p <project> <args>`` in ``cwd`` (mirrors the
    debezium test's ``_compose`` helper)."""
    return subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def wait_for_http(url: str, *, attempts: int = 30, delay: float = 2.0) -> bool:
    """Poll ``url`` until it answers. Returns True once reachable, False if it
    never came up within ``attempts * delay`` seconds."""
    for _ in range(attempts):
        try:
            urllib.request.urlopen(url, timeout=3).read()  # noqa: S310 — localhost emulator
            return True
        except Exception:  # noqa: BLE001 — emulator not ready yet
            time.sleep(delay)
    return False


def wait_for_tcp(host: str, port: int, *, attempts: int = 30, delay: float = 1.0) -> bool:
    """Poll a TCP ``host:port`` until it accepts a connection. Used for servers
    that answer 4xx to an unauthenticated probe (e.g. Azurite's Blob endpoint),
    where an HTTP 2xx wait would spin until timeout even though the server is up."""
    for _ in range(attempts):
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(delay)
    return False


def run_pyiceberg_container(
    *,
    script_path: Path,
    network: str,
    pip_spec: str,
    env: Dict[str, str],
    timeout: int = 300,
) -> "subprocess.CompletedProcess[str]":
    """Run ``script_path`` in an ephemeral ``python:3.11-slim`` container joined to
    the compose ``network``, after ``pip install <pip_spec>``.

    ``pip_spec`` pins the exact ``pyiceberg[...]`` extras + ``pyarrow`` (+ any
    cloud SDK) — kept inside the container so the forge-cli venv never grows an
    Iceberg dependency. ``env`` is passed through as ``-e KEY=VALUE`` flags.
    """
    env_flags: List[str] = []
    for key, value in env.items():
        env_flags += ["-e", f"{key}={value}"]
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            *env_flags,
            "-v",
            f"{script_path}:/rw.py:ro",
            EPHEMERAL_PYTHON_IMAGE,
            "sh",
            "-c",
            f"pip -q install {pip_spec} >/dev/null 2>&1 && python /rw.py",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
