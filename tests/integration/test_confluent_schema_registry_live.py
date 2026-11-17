# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Forge's Confluent schema-registry writes, applied by OpenTofu to a real registry.

``fluid generate iac --provider confluent`` writes a ``confluent_subject_config`` and a
``confluent_schema`` per produced subject (an Avro one and a Protobuf one here); the
``confluentinc/confluent`` provider the module requires (``~> 2.0``, so the newest 2.x)
registers them. This proves what unit tests and ``tofu validate`` cannot: the registry
holds the schemas forge generated, with the schema type and compatibility forge set, a
second plan finds nothing to change, a new column registers as version 2, and a destroy
deletes the versions the module manages. The provider deletes only the version in its
state, so the evolved subject keeps version 1; the test removes what is left itself.

The job in ``.github/workflows/integration-emulated-heavy.yml`` starts a single-node KRaft
Kafka and ``cp-schema-registry`` and sets ``FLUID_SR_LIVE_ENDPOINT``; with it unset,
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


def _versions(subject: str) -> List[int]:
    """The subject's live versions: none once every one is deleted."""
    response = httpx.get(f"{ENDPOINT}/subjects/{subject}/versions", timeout=15)
    if response.status_code == 404:
        return []
    assert response.status_code == 200, response.text
    return response.json()


def _purge(*subjects: str) -> None:
    """Delete what is left of ``subjects``: soft first, which the permanent delete needs."""
    for subject in subjects:
        for params in ({}, {"permanent": "true"}):
            try:
                httpx.delete(f"{ENDPOINT}/subjects/{subject}", params=params, timeout=15)
            except httpx.HTTPError:
                pass


def _registered_as(subject: str, schema: Dict[str, Any]) -> int:
    """The version under ``subject`` that holds ``schema`` (a ``confluent_schema`` of the module).

    The registry's own lookup (``POST /subjects/{subject}``): it compares by its canonical
    form, as a serializer looking up its schema's id does, so a re-formatting by the
    registry (it stores Protobuf in its own layout) is not a difference.
    """
    body: Dict[str, Any] = {"schema": schema["schema"]}
    if schema["format"] != "AVRO":
        body["schemaType"] = schema["format"]
    response = httpx.post(f"{ENDPOINT}/subjects/{subject}", json=body, timeout=15)
    assert response.status_code == 200, f"{subject}: {response.status_code} {response.text}"
    return response.json()["version"]


def test_forge_registers_evolves_and_removes_confluent_subjects(tmp_path, monkeypatch):
    run = uuid.uuid4().hex[:8]
    cache = tmp_path / "plugin-cache"
    cache.mkdir()
    stack = _Stack(tmp_path, run, cache, monkeypatch)
    avro_subject = f"fluid-ci-sr-{run}.avro-value"
    proto_subject = f"fluid-ci-sr-{run}.proto-value"

    destroyed = None
    left: Dict[str, List[int]] = {}
    try:
        generated = stack.generate(_contract(run))
        schemas = _by_subject(generated)
        assert sorted(schemas) == [avro_subject, proto_subject]
        assert sorted(generated["resource"]) == ["confluent_schema", "confluent_subject_config"]

        stack.tofu("init", "-input=false")
        stack.tofu("apply", "-auto-approve", "-input=false")

        # What the registry holds is what forge generated, with the type forge set.
        avro = _latest(avro_subject)
        assert avro.get("schemaType", "AVRO") == "AVRO" and avro["version"] == 1
        assert _registered_as(avro_subject, schemas[avro_subject]) == 1
        proto = _latest(proto_subject)
        assert proto["schemaType"] == "PROTOBUF" and proto["version"] == 1
        assert _registered_as(proto_subject, schemas[proto_subject]) == 1
        assert "confluent.type.Decimal" in proto["schema"]
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
        assert _versions(avro_subject) == [1, 2]
        assert _registered_as(avro_subject, _by_subject(evolved)[avro_subject]) == 2
        assert _registered_as(avro_subject, schemas[avro_subject]) == 1
        assert "note" in _latest(avro_subject)["schema"]
        # The Protobuf subject was not touched.
        assert _versions(proto_subject) == [1]
    finally:
        if (stack.module / "main.tf.json").exists():
            destroyed = stack.tofu("destroy", "-auto-approve", "-input=false", check=False)
            try:
                left = {subject: _versions(subject) for subject in (avro_subject, proto_subject)}
            except (AssertionError, httpx.HTTPError):
                pass  # asserted below, so a failure above is not masked
        _purge(avro_subject, proto_subject)

    # Destroy deletes the version each confluent_schema manages: the Protobuf subject's
    # only one and the Avro subject's version 2. Its version 1 is the registry's history,
    # which the provider leaves (and _purge removed).
    assert destroyed is not None and destroyed.returncode == 0, destroyed and destroyed.stderr
    assert left, "could not read the subjects after destroy"
    assert left[proto_subject] == [] and 2 not in left[avro_subject], left
