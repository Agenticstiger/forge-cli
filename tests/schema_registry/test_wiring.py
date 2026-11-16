# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The registry checks run where they are wired: ``fluid plan`` writes what it checked into
the digest-bound plan, and ``fluid apply`` checks before anything else runs."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest
import yaml

from fluid_build.schema_registry.clients import SubjectVersion
from fluid_build.schema_registry.schemas import avro_schema, schema_text

pytestmark = pytest.mark.unit

COLUMNS = [{"name": "order_id", "type": "string", "required": True}]


def _contract(registry=None):
    stream = {"kind": "kafka", "topic": "orders", "format": "avro-confluent", "schema": COLUMNS}
    if registry is not None:
        stream["registry"] = registry
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "t.consumer",
        "name": "t",
        "domain": "t",
        "metadata": {"layer": "Silver", "owner": {"team": "t"}},
        "builds": [
            {
                "id": "read-orders",
                "engine": "python",
                "pattern": "embedded-logic",
                "outputs": ["sink"],
                "properties": {"sql": "SELECT order_id FROM orders", "stream": stream},
            }
        ],
        "exposes": [
            {
                "exposeId": "sink",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": "b-sink", "path": "s/", "region": "us-east-1"},
                },
                "contract": {"schema": COLUMNS},
            }
        ],
    }


class _Registry:
    version = 4

    def __init__(self, url, **kw):
        pass

    def get_version(self, subject, version="latest"):
        text = schema_text(avro_schema(COLUMNS, name="orders"))
        return SubjectVersion(subject, self.version, "AVRO", text, schema_id="9")

    def check_compatibility(self, *a):
        return True, []

    def compatibility_level(self, subject):
        return "BACKWARD"

    def close(self):
        pass


def _plan(tmp_path, contract, name):
    from fluid_build.cli import plan as plan_cli

    path = tmp_path / f"{name}.fluid.yaml"
    path.write_text(yaml.safe_dump(contract))
    out = tmp_path / f"{name}.json"
    args = argparse.Namespace(
        contract=str(path),
        env=None,
        out=str(out),
        verbose=False,
        validate_actions=False,
        estimate_cost=False,
        check_sovereignty=None,
        provider=None,
        project=None,
        region=None,
    )
    assert plan_cli.run(args, plan_cli.logging.getLogger("t")) == 0
    return json.loads(out.read_text())


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("fluid_build.schema_registry.clients.ConfluentRegistry", _Registry)
    return _Registry


def test_plan_records_the_version_it_checked_under_the_digest(tmp_path, monkeypatch, registry):
    monkeypatch.chdir(tmp_path)
    contract = _contract({"kind": "confluent", "url": "http://127.0.0.1:18081"})
    first = _plan(tmp_path, contract, "a")
    (checked,) = first["schemaRegistry"]["checked"]
    assert (checked["subject"], checked["version"], checked["result"]) == (
        "orders-value",
        4,
        "compatible",
    )
    monkeypatch.setattr(registry, "version", 5)
    second = _plan(tmp_path, contract, "b")
    assert second["schemaRegistry"]["checked"][0]["version"] == 5
    assert first["planDigest"] != second["planDigest"]


def test_a_plan_without_a_registry_gets_no_key(tmp_path, monkeypatch, registry):
    monkeypatch.chdir(tmp_path)
    assert "schemaRegistry" not in _plan(tmp_path, _contract(), "plain")


def test_apply_checks_before_the_engine_plans_or_applies(monkeypatch):
    from fluid_build.cli import _apply_opentofu_engine as engine

    order = []
    contract = {"id": "x"}
    monkeypatch.setattr(engine, "_verify_plan_binding_for_opentofu", lambda a, l: None)
    monkeypatch.setattr(engine, "_bound_tofu_plan", lambda a, p, l: None)
    monkeypatch.setattr(engine, "_open_engine", lambda *a, **k: SimpleNamespace(contract=contract))

    def verify(given, *, dry_run):
        order.append(("verify", given, dry_run))
        raise RuntimeError("stop")

    monkeypatch.setattr("fluid_build.schema_registry.verify.verify_at_apply", verify)
    monkeypatch.setattr(engine, "_guard_before_plan", lambda *a: order.append("guard"))
    with pytest.raises(RuntimeError, match="stop"):
        engine._apply_via_opentofu(argparse.Namespace(dry_run=False), None, contract)
    assert order == [("verify", contract, False)]
