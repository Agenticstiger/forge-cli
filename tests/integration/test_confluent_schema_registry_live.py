# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Forge's Confluent schema-registry writes, applied by OpenTofu to a real registry.

``fluid generate iac --provider confluent`` writes a ``confluent_subject_config`` and a
``confluent_schema`` per produced subject (an Avro one and a Protobuf one here); the
pinned ``confluentinc/confluent`` provider registers them. This proves what unit tests
and ``tofu validate`` cannot: the registry accepts forge's schema text, reports the schema
type and compatibility forge set, a second plan finds nothing to change, a new column
registers as version 2, and a destroy removes the subjects.

The job in ``.github/workflows/integration-emulated-heavy.yml`` starts a single-node KRaft
``cp-kafka`` and ``cp-schema-registry`` and sets ``FLUID_SR_LIVE_ENDPOINT``; with it unset,
or with nothing answering there, every test here skips. The community registry has no
rule executor, so the subjects carry no ``rules`` (``tofu validate`` proves those in
``tests/iac/test_iac_tofu_validate.py``).

The CI registry has no authentication. The provider still wants a registry id and an API
key pair, so the test passes placeholders: a registry that does not authenticate ignores
the Authorization header they produce.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
import yaml

from fluid_build.cli import main

ENDPOINT = os.environ.get("FLUID_SR_LIVE_ENDPOINT", "").rstrip("/")
_TOFU = shutil.which("tofu")


def _registry_answers() -> bool:
    if not ENDPOINT:
        return False
    try:
        return httpx.get(f"{ENDPOINT}/subjects", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated_heavy,
    pytest.mark.slow,
    # Provider download, two applies, a plan and a destroy.
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        _TOFU is None or not _registry_answers(),
        reason=(
            "set FLUID_SR_LIVE_ENDPOINT to a running Confluent schema registry and put "
            "`tofu` on PATH to run the Confluent schema-registry live test"
        ),
    ),
]

# A registry without authentication ignores these; the provider only needs them set.
_PLACEHOLDER_ID = "lsrc-fluid-ci"
_PLACEHOLDER_KEY = "fluid-ci-key"
_PLACEHOLDER_SECRET = "fluid-ci-secret"  # pragma: allowlist secret


def _contract(run: str, *, extra_columns: bool = False) -> Dict[str, Any]:
    """One product with an Avro and a Protobuf topic on the registry under test."""
    registry = {"kind": "confluent", "url": ENDPOINT}
    avro_columns: List[Dict[str, Any]] = [
        {"name": "order_id", "type": "string", "required": True},
        {"name": "amount", "type": "long"},
    ]
    if extra_columns:
        avro_columns.append({"name": "note", "type": "string"})
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": f"ci.sr.{run}",
        "name": "Schema registry live proof",
        "exposes": [
            {
                "exposeId": "orders_avro",
                "kind": "stream",
                "binding": {
                    "platform": "confluent",
                    "format": "kafka_topic",
                    "location": {"topic": f"fluid-ci-sr-{run}.avro"},
                    "serialization": {"value": "avro"},
                    "properties": {"registry": dict(registry)},
                },
                "contract": {"schema": avro_columns},
            },
            {
                "exposeId": "orders_proto",
                "kind": "stream",
                "binding": {
                    "platform": "confluent",
                    "format": "kafka_topic",
                    "location": {"topic": f"fluid-ci-sr-{run}.proto"},
                    "serialization": {"value": "protobuf"},
                    "properties": {"registry": dict(registry)},
                },
                "contract": {
                    "schema": [
                        {
                            "name": "order_id",
                            "type": "string",
                            "required": True,
                            "protobuf": {"field": 1},
                        },
                        {
                            "name": "amount",
                            "type": "decimal(18,2)",
                            "required": True,
                            "protobuf": {"field": 2},
                        },
                    ]
                },
            },
        ],
    }


class _Stack:
    """One module directory: `fluid generate iac`, then `tofu` against the registry."""

    def __init__(self, root: Path, run: str, plugin_cache: Path, monkeypatch) -> None:
        self.root = root
        self.run = run
        self.module = root / "iac"
        self.contract_path = root / "contract.fluid.yaml"
        # forge checks a produced subject's url against the endpoint the provider uses.
        monkeypatch.setenv("SCHEMA_REGISTRY_REST_ENDPOINT", ENDPOINT)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", str(root)),
            "TF_IN_AUTOMATION": "1",
            "TF_PLUGIN_CACHE_DIR": str(plugin_cache),
            "SCHEMA_REGISTRY_ID": _PLACEHOLDER_ID,
            "SCHEMA_REGISTRY_REST_ENDPOINT": ENDPOINT,
            "SCHEMA_REGISTRY_API_KEY": _PLACEHOLDER_KEY,
            "SCHEMA_REGISTRY_API_SECRET": _PLACEHOLDER_SECRET,
        }

    def generate(self, contract: Dict[str, Any]) -> Dict[str, Any]:
        self.contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        rc = main(
            [
                "generate",
                "iac",
                str(self.contract_path),
                "--provider",
                "confluent",
                "--out",
                str(self.module),
            ]
        )
        assert rc == 0, "fluid generate iac --provider confluent failed"
        return json.loads((self.module / "main.tf.json").read_text(encoding="utf-8"))

    def tofu(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(
            [_TOFU, *args, "-no-color"],
            cwd=self.module,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if check:
            assert done.returncode == 0, f"tofu {args[0]}:\n{done.stdout}\n{done.stderr}"
        return done


def _by_subject(module: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {s["subject_name"]: s for s in module["resource"]["confluent_schema"].values()}


def _latest(subject: str) -> Dict[str, Any]:
    response = httpx.get(f"{ENDPOINT}/subjects/{subject}/versions/latest", timeout=15)
    assert response.status_code == 200, response.text
    return response.json()


def _squash(text: str) -> str:
    """Protobuf text with its whitespace collapsed: the registry may re-format a schema."""
    return re.sub(r"\s+", " ", text).strip()


def test_forge_registers_evolves_and_removes_confluent_subjects(tmp_path, monkeypatch):
    run = uuid.uuid4().hex[:8]
    cache = tmp_path / "plugin-cache"
    cache.mkdir()
    stack = _Stack(tmp_path, run, cache, monkeypatch)
    avro_subject = f"fluid-ci-sr-{run}.avro-value"
    proto_subject = f"fluid-ci-sr-{run}.proto-value"

    try:
        generated = stack.generate(_contract(run))
        schemas = _by_subject(generated)
        assert sorted(schemas) == [avro_subject, proto_subject]
        assert sorted(generated["resource"]) == ["confluent_schema", "confluent_subject_config"]

        stack.tofu("init", "-input=false")
        stack.tofu("apply", "-auto-approve", "-input=false")

        # What the registry holds is what forge generated.
        avro = _latest(avro_subject)
        assert avro.get("schemaType", "AVRO") == "AVRO"
        assert avro["version"] == 1
        assert json.loads(avro["schema"]) == json.loads(schemas[avro_subject]["schema"])
        proto = _latest(proto_subject)
        assert proto["schemaType"] == "PROTOBUF"
        assert _squash(proto["schema"]) == _squash(schemas[proto_subject]["schema"])
        assert ".confluent.type.Decimal amount = 2;" in proto["schema"]
        for subject in (avro_subject, proto_subject):
            config = httpx.get(f"{ENDPOINT}/config/{subject}", timeout=15)
            assert config.status_code == 200, config.text
            assert config.json()["compatibilityLevel"] == "BACKWARD_TRANSITIVE"

        # A second plan has nothing to do.
        plan = stack.tofu("plan", "-detailed-exitcode", "-input=false", check=False)
        assert plan.returncode == 0, f"plan found changes (exit {plan.returncode}):\n{plan.stdout}"

        # A new nullable column is a compatible change: version 2.
        evolved = stack.generate(_contract(run, extra_columns=True))
        stack.tofu("apply", "-auto-approve", "-input=false")
        versions = httpx.get(f"{ENDPOINT}/subjects/{avro_subject}/versions", timeout=15).json()
        assert versions == [1, 2]
        latest = _latest(avro_subject)
        assert latest["version"] == 2
        assert json.loads(latest["schema"]) == json.loads(
            _by_subject(evolved)[avro_subject]["schema"]
        )
        assert "note" in latest["schema"]
        # The Protobuf subject was not touched.
        assert httpx.get(f"{ENDPOINT}/subjects/{proto_subject}/versions", timeout=15).json() == [1]
    finally:
        if (stack.module / "main.tf.json").exists():
            stack.tofu("destroy", "-auto-approve", "-input=false", check=False)

    live = httpx.get(f"{ENDPOINT}/subjects", timeout=15).json()
    assert avro_subject not in live and proto_subject not in live
