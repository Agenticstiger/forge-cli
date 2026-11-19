# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Stage 8 end to end for a Lakekeeper table: compile the contract, then apply.

``fluid policy compile`` writes the Lakekeeper bindings and ``fluid policy
apply`` sends them to Lakekeeper (an in-process fake with v0.14.0 shapes),
while every other binding in the file still goes to the provider it names.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import policy_apply, policy_compile
from fluid_build.policy import lakekeeper_apply

from ..policy import _fake_lakekeeper as fake

pytestmark = pytest.mark.unit

TABLE_ID = "0190c5b4-0000-7000-8000-0000000000d1"
LOG = logging.getLogger("test.policy_apply_lakekeeper")

_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "sales.events",
    "name": "Events",
    "metadata": {"owner": {"team": "sales"}},
    "accessPolicy": {
        "grants": [
            {"principal": "serviceAccount:trino", "permissions": ["read"]},
            {"principal": "group:sales-analysts@corp.com", "permissions": ["read"]},
        ]
    },
    "exposes": [
        {
            "exposeId": "events_daily",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "catalog": "lakekeeper",
                    "uri": fake.CATALOG_URI,
                    "warehouse": fake.WAREHOUSE,
                    "database": "curated",
                    "table": "events_daily",
                    "bucket": "sales-lake",
                    "region": "eu-west-1",
                },
                "principals": {
                    "serviceAccount:trino": "user:service-account-trino",
                    "group:sales-analysts@corp.com": "role:sales-analysts",
                },
            },
        }
    ],
}


@pytest.fixture
def server(monkeypatch) -> fake.FakeLakekeeper:
    lk = fake.FakeLakekeeper()
    lk.add_table(("curated",), "events_daily", TABLE_ID)
    lk.add_user("oidc~trino", "service-account-trino")
    lk.add_user("oidc~probe", "service-account-lab-probe")
    lk.add_role("0190c5b4-0000-7000-8000-0000000000e1", "sales-analysts")
    lk.assignments[TABLE_ID] = {
        ("select", "user", "oidc~probe"),
        ("ownership", "user", "oidc~spark"),
    }
    monkeypatch.setattr(lakekeeper_apply, "_new_client", lk.client)
    for name, value in {
        "LK_CRED": f"{fake.CLIENT_ID}:{fake.CLIENT_SECRET}",
        "FLUID_LAKEKEEPER_CREDENTIAL_REF": "env://LK_CRED",
        "FLUID_LAKEKEEPER_OAUTH2_SERVER_URI": fake.TOKEN_URL,
        "FLUID_LAKEKEEPER_SCOPE": "lakekeeper",
        "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST": "LK_CRED@lakekeeper.test",  # pragma: allowlist secret
        "FLUID_LAKEKEEPER_NAMESPACE_ALLOWLIST": "sales-wh/curated",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("FLUID_LAKEKEEPER_TOKEN_REF", raising=False)
    return lk


class _NoApplier:
    """A provider with no standalone policy applier (aws, local, snowflake)."""


@pytest.fixture
def providers(monkeypatch) -> List[Dict[str, Any]]:
    seen: List[Dict[str, Any]] = []

    def build(name, project, region, logger):
        seen.append({"name": name})
        return _NoApplier()

    monkeypatch.setattr(policy_apply, "build_provider", build)
    return seen


def _compile(tmp_path: Path) -> Path:
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(yaml.safe_dump(_CONTRACT, sort_keys=False), encoding="utf-8")
    out = tmp_path / "policy" / "bindings.json"
    assert policy_compile.run(argparse.Namespace(contract=str(contract), out=str(out)), LOG) == 0
    return out


def _apply(path: Path, mode: str) -> int:
    args = argparse.Namespace(bindings=str(path), mode=mode, provider=None, project=None)
    return policy_apply.run(args, LOG)


def test_compile_writes_lakekeeper_bindings_beside_the_bucket_grant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    doc = json.loads(_compile(tmp_path).read_text())
    kinds = sorted((b["provider"], b["resource_type"]) for b in doc["bindings"])
    assert kinds == [
        ("aws", "s3.bucket"),
        ("aws", "s3.bucket"),
        ("lakekeeper", "lakekeeper.table"),
        ("lakekeeper", "lakekeeper.table"),
    ]
    assert not any("cataloged in" in w for w in doc["warnings"])


def test_enforce_applies_the_contract_and_sends_the_rest_to_the_provider(
    tmp_path, monkeypatch, server, providers
):
    monkeypatch.chdir(tmp_path)
    bindings = _compile(tmp_path)

    assert _apply(bindings, "enforce") == 0

    assert server.held(TABLE_ID) == {
        ("select", "user", "oidc~trino"),
        ("select", "role", "0190c5b4-0000-7000-8000-0000000000e1"),
        ("ownership", "user", "oidc~spark"),
    }
    # The bucket statements still went to the AWS provider, as before.
    assert providers == [{"name": "aws"}]


def test_check_mode_after_enforce_reports_nothing_to_do(tmp_path, monkeypatch, server, providers):
    monkeypatch.chdir(tmp_path)
    bindings = _compile(tmp_path)
    assert _apply(bindings, "enforce") == 0
    posts = len(server.posts_to_assignments())

    assert _apply(bindings, "check") == 0
    assert len(server.posts_to_assignments()) == posts


def test_check_mode_changes_nothing(tmp_path, monkeypatch, server, providers):
    monkeypatch.chdir(tmp_path)
    before = server.held(TABLE_ID)
    assert _apply(_compile(tmp_path), "check") == 0
    assert server.held(TABLE_ID) == before
    assert server.posts_to_assignments() == []


def test_a_failure_in_lakekeeper_fails_the_stage(tmp_path, monkeypatch, server, providers):
    monkeypatch.chdir(tmp_path)
    server.assignments_status = 403
    assert _apply(_compile(tmp_path), "enforce") == 1


def test_lakekeeper_bindings_alone_build_no_provider(tmp_path, monkeypatch, server, providers):
    monkeypatch.chdir(tmp_path)
    doc = json.loads(_compile(tmp_path).read_text())
    doc["bindings"] = [b for b in doc["bindings"] if b["provider"] == "lakekeeper"]
    path = tmp_path / "lakekeeper-only.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    assert _apply(path, "enforce") == 0
    assert providers == []
