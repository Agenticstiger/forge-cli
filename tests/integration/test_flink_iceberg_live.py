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

"""Actual generated Flink job -> nested Iceberg rows, including task recovery.

Run explicitly with FLUID_TEST_FLINK_ICEBERG=1 after building the example image.
This tests the data plane, not Kubernetes Operator lifecycle certification.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

import pytest
import yaml

from fluid_build.streaming.compiler import compile_contract

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated,
    pytest.mark.skipif(
        os.environ.get("FLUID_TEST_FLINK_ICEBERG") != "1",
        reason="set FLUID_TEST_FLINK_ICEBERG=1 and build examples/flink-iceberg/runtime",
    ),
]


def _wait_json(url, predicate, timeout=120):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=5) as response:  # noqa: S310 - isolated local fixture
                last = json.load(response)
            if predicate(last):
                return last
        except OSError:
            pass
        time.sleep(1)
    raise AssertionError(f"Fixture did not become ready: {url}; last response: {last}")


def test_generated_flink_job_preserves_nested_values_after_task_restart(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker is required")
    image = os.environ.get("FLUID_FLINK_TEST_IMAGE", "forge-flink:test")
    inspection = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if inspection.returncode:
        pytest.skip("Build the example Flink runtime image first")
    root = Path(__file__).resolve().parents[2]
    example = root / "examples/flink-iceberg"
    contract = yaml.safe_load((example / "contract.fluid.yaml").read_text())
    cfg = contract["builds"][0]["properties"]["parameters"]["flink"]
    cfg["source"]["startupMode"] = "earliest-offset"
    cfg["catalogProperties"] = {
        "s3.endpoint": "http://minio:9000",
        "s3.path-style-access": "true",
        "client.region": "us-east-1",
    }
    (artifact,) = compile_contract(contract)
    (tmp_path / "job.properties").write_text(artifact.resources[0]["data"]["job.properties"])
    shutil.copyfile(example / "compose.yaml", tmp_path / "compose.yaml")
    project = "forge-flink-" + uuid.uuid4().hex[:12]

    def compose(*args, input=None, timeout=180):
        result = subprocess.run(
            ["docker", "compose", "-p", project, "-f", str(tmp_path / "compose.yaml"), *args],
            input=input,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    try:
        compose("up", "-d", timeout=300)
        port = compose("port", "jobmanager", "8081").rsplit(":", 1)[1]
        rest_port = compose("port", "iceberg-rest", "8181").rsplit(":", 1)[1]
        flink = f"http://127.0.0.1:{port}"
        catalog = f"http://127.0.0.1:{rest_port}"
        _wait_json(flink + "/overview", lambda x: x.get("taskmanagers", 0) > 0)
        _wait_json(catalog + "/v1/config", lambda x: "defaults" in x)
        deadline = time.monotonic() + 60
        while True:
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    str(tmp_path / "compose.yaml"),
                    "exec",
                    "-T",
                    "kafka",
                    "rpk",
                    "topic",
                    "create",
                    "orders",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                break
            assert time.monotonic() < deadline, result.stderr
            time.sleep(1)
        compose(
            "exec",
            "-T",
            "jobmanager",
            "flink",
            "run",
            "-d",
            "-c",
            "io.fluid.forge.ForgeSqlRunner",
            "/opt/flink/usrlib/forge-sql-runner.jar",
            "/etc/forge/job.properties",
        )
        overview = _wait_json(
            flink + "/jobs/overview",
            lambda x: any(j["state"] == "RUNNING" for j in x.get("jobs", [])),
        )
        job_id = next(j["jid"] for j in overview["jobs"] if j["state"] == "RUNNING")
        events = [
            {
                "event_id": f"evt-{i}",
                "customer": (
                    None
                    if i % 7 == 0
                    else {
                        "id": f"c-{i}",
                        "address": (
                            None
                            if i % 5 == 0
                            else {
                                "country": "DE",
                                "geo": {
                                    "location": {
                                        "coordinates": {
                                            "latitude": 52.52,
                                            "longitude": 13.405,
                                        }
                                    }
                                },
                            }
                        ),
                    }
                ),
                "items": (
                    None
                    if i % 13 == 0
                    else (
                        [{"sku": "p-4", "price": "19.95"}, {"sku": None, "price": None}, None]
                        if i % 2
                        else []
                    )
                ),
                "attributes": (
                    None
                    if i % 11 == 0
                    else ({"channel": "mobile", "campaign": None} if i % 3 else {})
                ),
                "event_time": "2026-01-01T00:00:01.123Z",
            }
            for i in range(100)
        ]
        compose(
            "exec",
            "-T",
            "kafka",
            "rpk",
            "topic",
            "produce",
            "orders",
            input="\n".join(json.dumps(e) for e in events) + "\n",
        )
        _wait_json(
            flink + f"/jobs/{job_id}/checkpoints",
            lambda x: x.get("counts", {}).get("completed", 0) >= 2,
        )
        metadata_url = catalog + "/v1/namespaces/commerce/tables/orders"
        _wait_json(
            metadata_url, lambda x: x.get("metadata", {}).get("current-snapshot-id", -1) != -1
        )
        compose("restart", "taskmanager")
        _wait_json(
            flink + "/jobs/overview",
            lambda x: any(
                j["jid"] == job_id and j["state"] == "RUNNING" for j in x.get("jobs", [])
            ),
        )
        # A second batch proves progress after recovery, not just stale row count.
        more = [{**e, "event_id": f"after-{i}"} for i, e in enumerate(events)]
        compose(
            "exec",
            "-T",
            "kafka",
            "rpk",
            "topic",
            "produce",
            "orders",
            input="\n".join(json.dumps(e) for e in more) + "\n",
        )
        readback = """
import time
import json
from decimal import Decimal
from datetime import datetime
from pyiceberg.catalog.rest import RestCatalog
cat = RestCatalog("test", uri="http://iceberg-rest:8181",
                  **{"s3.endpoint":"http://minio:9000", "s3.access-key-id":"minioadmin",
                     "s3.secret-access-key":"minioadmin", "s3.region":"us-east-1"})
rows = []
for _ in range(90):
    rows = cat.load_table("commerce.orders").scan().to_arrow().to_pylist()
    if len(rows) >= 200:
        break
    time.sleep(1)
assert len(rows) == 200, len(rows)
assert {r["event_id"] for r in rows} == (
    {f"evt-{i}" for i in range(100)} | {f"after-{i}" for i in range(100)})
expected = json.loads(EXPECTED_EVENTS)
for event in expected:
    event["event_time"] = datetime.fromisoformat(event["event_time"].replace("Z", "+00:00"))
    if event["items"] is not None:
        for item in event["items"]:
            if item is not None and item["price"] is not None:
                item["price"] = Decimal(item["price"])
for row in rows:
    if row["attributes"] is not None:
        row["attributes"] = dict(row["attributes"])
assert sorted(rows, key=lambda x: x["event_id"]) == sorted(expected, key=lambda x: x["event_id"])
print("PASS: 200 unique records; five nested structs, nulls, empty containers, exact decimals; task recovery")
""".replace(
            "EXPECTED_EVENTS", repr(json.dumps(events + more))
        )
        network = project + "_default"
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "python:3.11-slim",
                "sh",
                "-ec",
                "pip -q install 'pyiceberg[s3fs,pyarrow]==0.10.0' && python -",
            ],
            input=readback,
            text=True,
            capture_output=True,
            timeout=240,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # Existing tables must be checked before INSERT planning/submission,
        # including changes the planner could otherwise coerce implicitly.
        drifted = copy.deepcopy(contract)
        drifted["exposes"][0]["contract"]["schema"][1]["fields"][1]["fields"][0]["type"] = "int"
        (mismatch,) = compile_contract(drifted)
        (tmp_path / "job.properties").write_text(mismatch.resources[0]["data"]["job.properties"])
        rejected = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(tmp_path / "compose.yaml"),
                "exec",
                "-T",
                "jobmanager",
                "flink",
                "run",
                "-c",
                "io.fluid.forge.ForgeSqlRunner",
                "/opt/flink/usrlib/forge-sql-runner.jar",
                "/etc/forge/job.properties",
                "--validate-only",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert rejected.returncode != 0
        assert (
            "Existing Iceberg schema differs from the contract" in rejected.stdout + rejected.stderr
        )
    finally:
        compose("down", "-v", "--remove-orphans")
