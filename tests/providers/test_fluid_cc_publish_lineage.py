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

"""``fluid publish`` to the Command Center: visibility, ``--env``, the contract
version record, and ``--dry-run``.

Four things were wrong, measured against a Postgres-sourced subscriptions
product classified ``confidential``:

1. every product was sent ``is_public: true``, because visibility was read from
   ``exposes[0].sensitivity`` with ``internal`` as its default and ``internal``
   counted as public;
2. ``fluid publish`` had no ``--env``, so a pipeline running the aws overlay
   published the base contract's local binding;
3. no contract version was recorded: ``POST /api/v1/contracts/sync`` (which
   stores the version and its git provenance) was never called;
4. ``--dry-run`` printed ``{"dry_run": true, "valid": true}`` and nothing about
   what would be sent.

The tests run the real provider and CLI against a loopback server that keeps
state the way the Command Center does (``fluid_cc_backend/app/api/v1/assets.py``
and ``app/api/v1/endpoints/contracts.py``): assets live in one organization,
``/contracts/sync`` computes and stores the ``contract_hash``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from fluid_build.cli import _publish_provenance as provenance
from fluid_build.providers.catalogs import get_catalog_provider
from fluid_build.providers.catalogs.base import CatalogAsset, contract_classification
from fluid_build.providers.catalogs.fluid_cc import (
    FluidCommandCenterProvider,
    derived_lineage_edges,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The wire contract, spelled out rather than imported.
API_KEY = "fluid_test_key_not_a_secret"  # pragma: allowlist secret
ORG_A = {"id": "0b6f6c3e-org-a", "name": "Acme", "slug": "acme", "role": "owner"}
SYNC_PATH = "/api/v1/contracts/sync"
# Commit ids as Jenkins exports them in GIT_COMMIT (made up).
COMMIT_A = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
COMMIT_B = "89abcdef0123456789abcdef0123456789abcdef"  # pragma: allowlist secret


def _cc_contract_hash(contract_yaml: str) -> str:
    """The Command Center's ``calculate_contract_hash``
    (``app/services/contract_schema.py``), restated here."""
    parsed = yaml.safe_load(contract_yaml)
    normalized = yaml.dump(parsed, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class _CommandCenter:
    """Assets, organizations and contract versions, as the routes keep them."""

    def __init__(self) -> None:
        self.organizations: List[Dict[str, Any]] = [ORG_A]
        self.assets: Dict[str, Dict[str, Any]] = {}
        self.versions: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self.sync_status = 200
        self.url = ""

    def calls(self, method: str, path: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def writes(self) -> List[Dict[str, Any]]:
        """Every request that changes something."""
        return [r for r in self.requests if r["method"] != "GET"]


def _handler_for(cc: _CommandCenter):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _record(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            entry = {
                "method": self.command,
                "path": parts.path,
                "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw) if raw else None,
            }
            cc.requests.append(entry)
            return entry

        def _org(self) -> Optional[str]:
            org = self.headers.get("X-Organization-Id")
            return org if org in {o["id"] for o in cc.organizations} else None

        def do_GET(self) -> None:  # noqa: N802
            entry = self._record()
            if entry["path"] == "/api/v1/organizations":
                if self.headers.get("X-API-Key") != API_KEY:
                    return self._send(401, {"detail": "Not authenticated"})
                return self._send(200, cc.organizations)
            if entry["path"] == "/api/v1/assets":
                wanted = entry["query"].get("fluid_contract_id")
                items = [
                    {k: v for k, v in a.items() if k != "contract_yaml"}  # AssetSummary
                    for a in cc.assets.values()
                    if wanted is None
                    or (
                        a["metadata"].get("fluid_contract_id") == wanted
                        and a["organization_id"] == self._org()
                    )
                ]
                return self._send(200, {"items": items, "total": len(items)})
            return self._send(404, {"detail": "Not Found"})

        def do_POST(self) -> None:  # noqa: N802
            entry = self._record()
            if self.headers.get("X-API-Key") != API_KEY:
                return self._send(401, {"detail": "Not authenticated"})
            org = self._org()
            if entry["path"] == "/api/v1/assets":
                if not org:
                    return self._send(400, {"detail": "organization context required"})
                asset_id = f"asset-{len(cc.assets) + 1:04d}"
                body = dict(entry["body"])
                body.update({"id": asset_id, "organization_id": org})
                body.setdefault("contract_hash", None)
                cc.assets[asset_id] = body
                return self._send(201, body)
            if entry["path"] == SYNC_PATH:
                if cc.sync_status != 200:
                    return self._send(cc.sync_status, {"detail": "Requires a steward role"})
                body = entry["body"]
                asset = cc.assets.get(body["asset_id"])
                if asset is None or asset["organization_id"] != org:
                    return self._send(404, {"detail": "Asset not found"})
                digest = _cc_contract_hash(body["contract_yaml"])
                asset["contract_hash"] = digest
                cc.versions.append(dict(body, contract_hash=digest))
                return self._send(
                    200,
                    {
                        "is_valid": True,
                        "fluid_version": body["fluid_version"],
                        "contract_hash": digest,
                        "errors": [],
                        "warnings": [],
                        "validated_at": "2026-09-26T00:00:00Z",
                    },
                )
            return self._send(404, {"detail": "Not Found"})

        def do_PATCH(self) -> None:  # noqa: N802
            entry = self._record()
            if self.headers.get("X-API-Key") != API_KEY:
                return self._send(401, {"detail": "Not authenticated"})
            asset_id = entry["path"].rsplit("/", 1)[-1]
            asset = cc.assets.get(asset_id)
            if asset is None:
                return self._send(404, {"detail": "Not Found"})
            asset.update(entry["body"])
            return self._send(200, asset)

    return Handler


# Settings a developer's shell or a CI job may export, cleared for every test
# so that what a test sees is only what it sets.
_AMBIENT = (
    "FLUID_CC_ORG_ID",
    "FLUID_CC_ENDPOINT",
    "FLUID_CATALOG_FLUID_CC_URL",
    "FLUID_API_KEY",
    "FLUID_CATALOG_FLUID_CC_TOKEN",
    "FLUID_BEARER_TOKEN",
    "FLUID_SECRETS_FILE",
    "GIT_COMMIT",
    "GIT_URL",
    "GIT_BRANCH",
    "BUILD_TAG",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@pytest.fixture
def cc(tmp_path, monkeypatch):
    for var in _AMBIENT:
        monkeypatch.delenv(var, raising=False)
    # No repository above the test's directory can answer for it.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    stub = _CommandCenter()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(stub))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# A Postgres-sourced product, classified confidential, with the aws overlay a
# multi-target pipeline applies (it patches exposes[0].binding only).
SUBSCRIPTIONS = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.customer_subscriptions
name: Customer Subscriptions
description: Source-aligned subscriptions, landed from Postgres.
domain: Customer
metadata:
  layer: Bronze
  productType: SDP
  owner:
    team: data-platform
    email: data-platform@example.com
  classification: confidential
builds:
  - id: ingest_subscriptions
    pattern: acquisition
    engine: duckdb
    properties:
      source:
        kind: postgres
        connection:
          host: "{{ env.PGHOST }}"
          port: "{{ env.PGPORT }}"
          database: "{{ env.PGDATABASE }}"
          user: "{{ env.PGUSER }}"
          password: "{{ env.PGPASSWORD }}"  # pragma: allowlist secret
        mode: full_refresh
        streams:
          - public.product_subscription
      sink:
        format: parquet
    outputs:
      - subscriptions
exposes:
  - exposeId: subscriptions
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/customer_subscriptions.parquet
    contract:
      schema:
        - name: subscription_id
          type: VARCHAR
          required: true
"""

AWS_OVERLAY = """\
exposes:
  - binding:
      platform: aws
      format: parquet
      location:
        database: demo_bronze
        table: customer_subscriptions
        bucket: northwind-demo-lake
        path: bronze/customer_subscriptions/
        region: eu-north-1
"""

SUMMARY = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: silver.subscription_status_summary
name: Subscription Status Summary
description: Live subscriptions by product and status.
domain: Customer
metadata:
  layer: Silver
  productType: ADP
  owner:
    team: data-platform
    email: data-platform@example.com
  classification: internal
consumes:
  - productId: bronze.customer_subscriptions
    exposeId: subscriptions
exposes:
  - exposeId: status_summary
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/status_summary.parquet
"""


def _write_product(root: Path, text: str = SUBSCRIPTIONS, overlay: bool = True) -> Path:
    product = root / "contracts" / "customer_subscriptions"
    (product / "overlays").mkdir(parents=True)
    contract = product / "contract.fluid.yaml"
    contract.write_text(text, encoding="utf-8")
    if overlay:
        (product / "overlays" / "aws.yaml").write_text(AWS_OVERLAY, encoding="utf-8")
    return contract


def _parser() -> argparse.ArgumentParser:
    from fluid_build.cli import publish

    parser = argparse.ArgumentParser()
    publish.register(parser.add_subparsers())
    return parser


def _publish(cc: _CommandCenter, monkeypatch, work: Path, contract: Path, *argv: str) -> int:
    """``fluid publish <contract> --target command-center *argv``, in-process."""
    from fluid_build.cli import publish

    monkeypatch.chdir(work)
    monkeypatch.setenv("FLUID_CC_ENDPOINT", cc.url)
    monkeypatch.setenv("FLUID_API_KEY", API_KEY)
    args = _parser().parse_args(
        ["publish", str(contract), "--target", "command-center", "--format", "json", *argv]
    )
    return asyncio.run(publish.run_async(args, logging.getLogger("test")))


def _document(capsys) -> List[Dict[str, Any]]:
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# 1. Visibility comes from the contract's classification
# ---------------------------------------------------------------------------


def _with_classification(label: Optional[str]) -> str:
    contract = yaml.safe_load(SUBSCRIPTIONS)
    if label is None:
        del contract["metadata"]["classification"]
    else:
        contract["metadata"]["classification"] = label
    return yaml.safe_dump(contract, sort_keys=False)


class TestVisibility:
    @pytest.mark.parametrize(
        ("label", "is_public"),
        [
            ("confidential", False),
            ("internal", False),
            ("restricted", False),
            ("public", True),
            ("Public", True),
            (None, False),
        ],
    )
    def test_is_public_only_for_a_contract_classified_public(
        self, cc, tmp_path, monkeypatch, capsys, label, is_public
    ):
        contract = _write_product(tmp_path, _with_classification(label))

        assert _publish(cc, monkeypatch, tmp_path, contract) == 0

        (create,) = cc.calls("POST", "/api/v1/assets")
        assert create["body"]["is_public"] is is_public
        expected = label.lower() if label else None
        assert create["body"]["metadata"]["classification"] == expected
        (result,) = _document(capsys)
        assert result["details"]["is_public"] is is_public

    def test_expose_policy_classification_counts_when_metadata_has_none(self):
        contract = yaml.safe_load(_with_classification(None))
        contract["exposes"][0]["policy"] = {"classification": "Public"}
        assert contract_classification(contract) == (
            "public",
            "exposes[0].policy.classification",
        )
        contract["exposes"][0]["policy"] = {"classification": "Confidential"}
        assert contract_classification(contract) == (
            "confidential",
            "exposes[0].policy.classification",
        )

    def test_metadata_classification_wins_over_the_exposes(self):
        contract = yaml.safe_load(SUBSCRIPTIONS)
        contract["exposes"][0]["policy"] = {"classification": "Public"}
        contract["exposes"][0]["sensitivity"] = "public"
        assert contract_classification(contract) == ("confidential", "metadata.classification")

    def test_one_unlabelled_expose_keeps_a_product_off_public(self):
        """The legacy ``sensitivity`` on the first expose is not enough when a
        second expose says nothing: a product is never more visible than its
        most guarded port."""
        contract = yaml.safe_load(_with_classification(None))
        contract["exposes"][0]["sensitivity"] = "public"
        contract["exposes"].append({"exposeId": "raw", "kind": "table"})
        assert contract_classification(contract) == (None, None)
        contract["exposes"][1]["policy"] = {"classification": "Public"}
        assert contract_classification(contract) == ("public", "exposes[0].sensitivity")
        contract["exposes"][1]["policy"] = {"classification": "Restricted"}
        assert contract_classification(contract) == (
            "restricted",
            "exposes[1].policy.classification",
        )

    @pytest.mark.parametrize("label", ["none", "pii", "cleartext", "", "  ", 3, None])
    def test_any_other_expose_label_is_not_public(self, label):
        contract = yaml.safe_load(_with_classification(None))
        contract["exposes"][0]["sensitivity"] = label
        assert contract_classification(contract)[0] != "public"

    def test_a_hand_built_asset_without_a_classification_is_not_public(self, cc, monkeypatch):
        """``sensitivity`` defaults to ``internal``, which used to count as public."""
        provider = get_catalog_provider(
            "fluid-command-center",
            {
                "endpoint": cc.url,
                "auth": {"type": "api_key", "api_key": API_KEY},
                "organization_id": ORG_A["id"],
                "retry_delay": 0,
            },
        )
        asset = CatalogAsset(
            id="p.x",
            name="X",
            description="",
            type="dataproduct",
            domain="d",
            owner="team",
            owner_email="",
            layer="Bronze",
            tags=[],
            version="1.0.0",
            platform="local",
            location={},
        )
        result = asyncio.run(provider.publish(asset))
        assert result.success, result.error
        (create,) = cc.calls("POST", "/api/v1/assets")
        assert create["body"]["is_public"] is False


# ---------------------------------------------------------------------------
# 2. --env publishes the overlay, into the same catalogue product
# ---------------------------------------------------------------------------


class TestEnv:
    def test_env_aws_publishes_the_aws_binding_and_records_the_env(
        self, cc, tmp_path, monkeypatch, capsys
    ):
        contract = _write_product(tmp_path)

        assert _publish(cc, monkeypatch, tmp_path, contract, "--env", "aws") == 0

        (create,) = cc.calls("POST", "/api/v1/assets")
        metadata = create["body"]["metadata"]
        assert metadata["platform"] == "aws"
        assert metadata["location"] == {
            "database": "demo_bronze",
            "table": "customer_subscriptions",
            "bucket": "northwind-demo-lake",
            "path": "bronze/customer_subscriptions/",
            "region": "eu-north-1",
        }
        assert metadata["fluid_env"] == "aws"
        assert metadata["fluid_contract_id"] == "bronze.customer_subscriptions"
        sent = yaml.safe_load(create["body"]["contract_yaml"])
        assert sent["exposes"][0]["binding"]["platform"] == "aws"
        # The overlay patches the binding only: classification is the base's.
        assert create["body"]["is_public"] is False
        (result,) = _document(capsys)
        assert result["details"]["fluid_env"] == "aws"

    def test_each_env_updates_the_one_product_for_the_contract(
        self, cc, tmp_path, monkeypatch, capsys
    ):
        contract = _write_product(tmp_path)

        assert _publish(cc, monkeypatch, tmp_path, contract) == 0
        assert _publish(cc, monkeypatch, tmp_path, contract, "--env", "aws") == 0

        assert len(cc.assets) == 1
        lookups = cc.calls("GET", "/api/v1/assets")
        assert {
            r["query"]["fluid_contract_id"] for r in lookups if "fluid_contract_id" in r["query"]
        } == {"bronze.customer_subscriptions"}
        (patch,) = cc.calls("PATCH", "/api/v1/assets/asset-0001")
        assert patch["body"]["metadata"]["fluid_env"] == "aws"
        base = cc.calls("POST", "/api/v1/assets")[0]["body"]["metadata"]
        assert "fluid_env" not in base
        # Two different contracts (local binding, then aws), two versions.
        assert len(cc.versions) == 2

    @pytest.mark.parametrize("env", ["../../etc/passwd", "a/b", "-rf", "", "x" * 65, "aws dev"])
    def test_env_must_be_a_plain_name(self, env, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _parser().parse_args(["publish", "c.yaml", f"--env={env}"])
        assert excinfo.value.code == 2
        assert "invalid environment name" in capsys.readouterr().err

    def test_a_programmatic_env_is_held_to_the_same_rule(self, tmp_path, monkeypatch):
        from fluid_build.cli import publish

        loaded: List[Any] = []
        monkeypatch.setattr(publish, "load_contract_with_overlay", lambda *a: loaded.append(a))
        result = asyncio.run(
            publish.publish_contract(
                contract_path=_write_product(tmp_path),
                catalog_name="command-center",
                config=None,  # type: ignore[arg-type]
                env="../../elsewhere",
            )
        )
        assert not result.success
        assert result.error.startswith("Invalid environment name")
        assert loaded == []


# ---------------------------------------------------------------------------
# 3. The contract version: POST /api/v1/contracts/sync after every write
# ---------------------------------------------------------------------------


class TestContractSync:
    def test_create_then_sync_with_the_same_headers_and_git_facts(
        self, cc, tmp_path, monkeypatch, capsys
    ):
        contract = _write_product(tmp_path)
        monkeypatch.setenv("FLUID_CC_ORG_ID", ORG_A["id"])
        monkeypatch.setenv("GIT_COMMIT", COMMIT_A)
        monkeypatch.setenv(
            "GIT_URL",
            "https://x-access-token:ghs_notarealtoken@git.example.com/acme/products.git",  # pragma: allowlist secret
        )
        monkeypatch.setenv("GIT_BRANCH", "origin/main")
        monkeypatch.setenv("BUILD_TAG", "jenkins-customer_subscriptions-42")

        assert _publish(cc, monkeypatch, tmp_path, contract) == 0

        (create,) = cc.calls("POST", "/api/v1/assets")
        (sync,) = cc.calls("POST", SYNC_PATH)
        assert cc.requests.index(create) < cc.requests.index(sync)
        for request in (create, sync):
            assert request["headers"]["x-api-key"] == API_KEY
            assert request["headers"]["x-organization-id"] == ORG_A["id"]
        assert "contract_hash" not in create["body"]
        assert sync["body"] == {
            "asset_id": "asset-0001",
            "contract_yaml": create["body"]["contract_yaml"],
            "fluid_version": "0.7.5",
            "git_commit_sha": COMMIT_A,
            "git_repo_url": "https://git.example.com/acme/products.git",
            "git_branch": "origin/main",
            "deployed_by": "jenkins-customer_subscriptions-42",
        }
        (result,) = _document(capsys)
        assert result["details"]["contract_sync"]["status"] == "recorded"
        assert result["details"]["contract_sync"]["contract_hash"] == _cc_contract_hash(
            create["body"]["contract_yaml"]
        )

    def test_an_unchanged_contract_is_not_recorded_again(self, cc, tmp_path, monkeypatch, capsys):
        contract = _write_product(tmp_path)

        assert _publish(cc, monkeypatch, tmp_path, contract) == 0
        capsys.readouterr()
        assert _publish(cc, monkeypatch, tmp_path, contract) == 0

        assert len(cc.calls("PATCH", "/api/v1/assets/asset-0001")) == 1
        assert len(cc.calls("POST", SYNC_PATH)) == 1
        (result,) = _document(capsys)
        assert result["details"]["operation"] == "update"
        assert result["details"]["contract_sync"]["status"] == "unchanged"

    def test_force_records_an_unchanged_contract(self, cc, tmp_path, monkeypatch):
        contract = _write_product(tmp_path)

        assert _publish(cc, monkeypatch, tmp_path, contract) == 0
        assert _publish(cc, monkeypatch, tmp_path, contract, "--force") == 0

        assert len(cc.calls("POST", SYNC_PATH)) == 2

    def test_a_patched_contract_is_recorded(self, cc, tmp_path, monkeypatch):
        contract = _write_product(tmp_path)
        assert _publish(cc, monkeypatch, tmp_path, contract) == 0
        contract.write_text(SUBSCRIPTIONS.replace("Source-aligned", "Landed"), encoding="utf-8")

        assert _publish(cc, monkeypatch, tmp_path, contract) == 0

        (patch,) = cc.calls("PATCH", "/api/v1/assets/asset-0001")
        second = cc.calls("POST", SYNC_PATH)[1]
        assert second["body"]["asset_id"] == "asset-0001"
        assert second["body"]["contract_yaml"] == patch["body"]["contract_yaml"]
        assert "Landed" in second["body"]["contract_yaml"]

    def test_a_failed_sync_warns_keeps_the_publish_and_is_retried(
        self, cc, tmp_path, monkeypatch, capsys, caplog
    ):
        contract = _write_product(tmp_path)
        cc.sync_status = 403

        with caplog.at_level(logging.WARNING):
            assert _publish(cc, monkeypatch, tmp_path, contract) == 0

        (result,) = _document(capsys)
        assert result["success"] is True
        assert result["details"]["contract_sync"] == {
            "status": "failed",
            "error": "/api/v1/contracts/sync answered HTTP 403 (Requires a steward role)",
        }
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("did not record its contract version" in m for m in warnings), warnings
        assert not [m for m in (r.getMessage() for r in caplog.records) if API_KEY in m]
        assert len(cc.calls("POST", "/api/v1/assets")) == 1

        # Nothing marked the version as recorded, so the next publish records it.
        cc.sync_status = 200
        assert _publish(cc, monkeypatch, tmp_path, contract) == 0
        assert len(cc.versions) == 1

    def test_a_contract_without_fluid_version_is_published_without_a_version(
        self, cc, tmp_path, monkeypatch, capsys
    ):
        contract = _write_product(tmp_path, SUBSCRIPTIONS.replace('fluidVersion: "0.7.5"\n', ""))

        assert _publish(cc, monkeypatch, tmp_path, contract) == 0

        assert cc.calls("POST", SYNC_PATH) == []
        (result,) = _document(capsys)
        assert result["details"]["contract_sync"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# 4. --dry-run shows what would be sent, and sends nothing
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_prints_the_bodies_and_the_lineage_and_sends_nothing(
        self, cc, tmp_path, monkeypatch, capsys
    ):
        contract = _write_product(tmp_path, SUMMARY, overlay=False)
        monkeypatch.setenv("FLUID_CC_ORG_ID", ORG_A["id"])
        monkeypatch.setenv("GIT_COMMIT", COMMIT_B)

        assert _publish(cc, monkeypatch, tmp_path, contract, "--dry-run") == 0

        assert cc.requests == []
        out = capsys.readouterr().out
        assert API_KEY not in out
        (result,) = json.loads(out)
        details = result["details"]
        assert details["dry_run"] is True and details["valid"] is True
        assert details["organization_id"] == ORG_A["id"]
        assert details["headers"]["X-API-Key"] == "<redacted>"
        assert details["headers"]["X-Organization-Id"] == ORG_A["id"]
        assert details["lookup"]["params"] == {
            "fluid_contract_id": "silver.subscription_status_summary",
            "limit": 1,
        }
        body = details["asset_write"]["body"]
        assert body["is_public"] is False
        assert body["metadata"]["classification"] == "internal"
        assert body["metadata"]["fluid_contract_id"] == "silver.subscription_status_summary"
        sync = details["contract_sync"]
        assert sync["path"] == SYNC_PATH
        assert sync["body"]["asset_id"] == "{asset_id}"
        assert sync["body"]["contract_yaml"] == body["contract_yaml"]
        assert sync["body"]["git_commit_sha"] == COMMIT_B
        assert _cc_contract_hash(body["contract_yaml"]) in sync["skipped_when"]
        assert details["lineage"] == {
            "declared_in": "consumes",
            "edges": [
                {
                    "from_product_id": "bronze.customer_subscriptions",
                    "from_expose_id": "subscriptions",
                    "to_product_id": "silver.subscription_status_summary",
                }
            ],
        }

    def test_dry_run_body_is_the_body_a_publish_sends(self, cc, tmp_path, monkeypatch, capsys):
        contract = _write_product(tmp_path)
        monkeypatch.setenv("FLUID_CC_ORG_ID", ORG_A["id"])

        assert _publish(cc, monkeypatch, tmp_path, contract, "--dry-run", "--env", "aws") == 0
        (preview,) = _document(capsys)
        assert _publish(cc, monkeypatch, tmp_path, contract, "--env", "aws") == 0

        (create,) = cc.calls("POST", "/api/v1/assets")
        (sync,) = cc.calls("POST", SYNC_PATH)
        shown = preview["details"]["asset_write"]["body"]
        shown_sync = preview["details"]["contract_sync"]["body"]

        def without_yaml(body: Dict[str, Any]) -> Dict[str, Any]:
            return {k: v for k, v in body.items() if k != "contract_yaml"}

        assert without_yaml(shown) == without_yaml(create["body"])
        assert without_yaml(shown_sync) == without_yaml(dict(sync["body"], asset_id="{asset_id}"))
        # The printed document goes through the same redaction as every
        # --format json document, so the credential-shaped line of the
        # contract is masked there. The request carries the contract as loaded.
        assert "password: '{{ env.PGPASSWORD }}'" in create["body"]["contract_yaml"]
        assert "{{ env.PGPASSWORD" not in shown["contract_yaml"].split("password:")[1][:12]
        for printed, sent in (
            (shown["contract_yaml"], create["body"]["contract_yaml"]),
            (shown_sync["contract_yaml"], sync["body"]["contract_yaml"]),
        ):
            assert [line for line in printed.splitlines() if "password" not in line] == [
                line for line in sent.splitlines() if "password" not in line
            ]

    def test_dry_run_without_an_organization_says_it_is_resolved_later(
        self, cc, tmp_path, monkeypatch, capsys
    ):
        contract = _write_product(tmp_path)

        assert _publish(cc, monkeypatch, tmp_path, contract, "--dry-run") == 0

        assert cc.requests == []
        (result,) = _document(capsys)
        assert result["details"]["organization_id"] is None
        assert result["details"]["headers"]["X-Organization-Id"] == "<resolved at publish time>"
        assert result["details"]["lineage"] == {"declared_in": None, "edges": []}

    def test_dry_run_with_a_blank_org_id_fails_with_its_error_code(
        self, cc, tmp_path, monkeypatch, capsys
    ):
        contract = _write_product(tmp_path)
        monkeypatch.setenv("FLUID_CC_ORG_ID", "")

        assert _publish(cc, monkeypatch, tmp_path, contract, "--dry-run") == 1

        assert cc.requests == []
        (result,) = _document(capsys)
        assert result["details"]["error_code"] == "cc_organization_id_blank"

    def test_the_real_cli_prints_a_parseable_dry_run(self, cc, tmp_path, monkeypatch):
        """Through ``python -m fluid_build.cli``, this checkout's code."""
        contract = _write_product(tmp_path)
        env = {k: v for k, v in os.environ.items() if k not in {"FLUID_CC_ORG_ID"}}
        env.update(
            {
                "PYTHONPATH": str(REPO_ROOT),
                "FLUID_CC_ENDPOINT": cc.url,
                "FLUID_API_KEY": API_KEY,
                "FLUID_CC_ORG_ID": ORG_A["id"],
                "HOME": str(tmp_path / "home"),
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "fluid_build.cli",
                "publish",
                str(contract),
                "--target",
                "command-center",
                "--env",
                "aws",
                "--dry-run",
                "--format",
                "json",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        (result,) = json.loads(completed.stdout)
        body = result["details"]["asset_write"]["body"]
        assert body["metadata"]["platform"] == "aws"
        assert body["metadata"]["fluid_env"] == "aws"
        assert API_KEY not in completed.stdout
        assert cc.requests == []


# ---------------------------------------------------------------------------
# The lineage edges, as the Command Center derives them
# ---------------------------------------------------------------------------


class TestDerivedLineageEdges:
    def test_lineage_upstream_is_read_when_consumes_is_absent(self):
        contract = {
            "id": "gold.x",
            "lineage": {"upstream": [{"productId": "silver.y", "exposeId": "out"}]},
        }
        assert derived_lineage_edges(contract) == (
            [{"from_product_id": "silver.y", "from_expose_id": "out", "to_product_id": "gold.x"}],
            "lineage.upstream",
        )

    def test_consumes_wins_and_aliases_and_strings_are_read(self):
        contract = {
            "id": "gold.x",
            "consumes": [
                {"sourceProductId": "a", "sourceExposeId": "p"},
                "b",
                {"exposeId": "no-product"},
                7,
            ],
            "lineage": {"upstream": [{"productId": "ignored", "exposeId": "e"}]},
        }
        edges, declared_in = derived_lineage_edges(contract)
        assert declared_in == "consumes"
        assert [(e["from_product_id"], e["from_expose_id"]) for e in edges] == [
            ("a", "p"),
            ("b", None),
        ]

    def test_no_upstream_is_no_edge(self):
        assert derived_lineage_edges({"id": "x", "consumes": []}) == ([], None)
        assert derived_lineage_edges({"id": "x", "lineage": {"upstream": "nope"}}) == ([], None)


# ---------------------------------------------------------------------------
# Where the provenance comes from
# ---------------------------------------------------------------------------


class TestProvenance:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://x-access-token:ghs_abc@github.com/acme/p.git",  # pragma: allowlist secret
                "https://github.com/acme/p.git",
            ),
            ("https://ghp_tokenonly@github.com/acme/p.git", "https://github.com/acme/p.git"),
            (
                "https://github.com/acme/p.git?access_token=abc#frag",
                "https://github.com/acme/p.git",
            ),
            ("ssh://git@git.example.com:2222/acme/p.git", "ssh://git.example.com:2222/acme/p.git"),
            ("git@github.com:acme/p.git", "git@github.com:acme/p.git"),
            ("user:secret@host:acme/p.git", "host:acme/p.git"),  # pragma: allowlist secret
            ("file:///srv/git/p.git", "file:///srv/git/p.git"),
            ("https://github.com/acme/p.git\nX-Evil: 1", None),
            ("", None),
            (None, None),
        ],
    )
    def test_repo_url_never_carries_a_credential(self, url, expected):
        assert provenance.repo_url_without_credentials(url) == expected

    def test_facts_come_from_the_repository_when_ci_sets_none(self, tmp_path, monkeypatch):
        for var in ("GIT_COMMIT", "GIT_URL", "GIT_BRANCH", "BUILD_TAG"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        repo = tmp_path / "repo"
        contract = _write_product(repo)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "init", "-q", "-b", "trunk"], check=True)
        subprocess.run(
            [
                *git,
                "remote",
                "add",
                "origin",
                "https://ci-bot:not-a-real-token@git.example.com/acme/p.git",  # pragma: allowlist secret
            ],
            check=True,
        )
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "c"], check=True)
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()

        facts = provenance.contract_provenance(contract, environ={"BUILD_TAG": "local-run"})

        assert facts == {
            "git_commit_sha": head,
            "git_branch": "trunk",
            "git_file_path": "contracts/customer_subscriptions/contract.fluid.yaml",
            "git_repo_url": "https://git.example.com/acme/p.git",
            "deployed_by": "local-run",
        }

        # Jenkins checks out a detached HEAD and names the branch itself.
        subprocess.run([*git, "checkout", "-q", "--detach"], check=True)
        facts = provenance.contract_provenance(contract, environ={"GIT_BRANCH": "origin/trunk"})
        assert facts["git_branch"] == "origin/trunk"
        facts = provenance.contract_provenance(contract, environ={})
        assert "git_branch" not in facts

    def test_outside_a_repository_only_ci_facts_are_sent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        contract = _write_product(tmp_path)

        facts = provenance.contract_provenance(
            contract,
            environ={"GIT_COMMIT": "not-a-sha", "GIT_BRANCH": "main\r\nx", "BUILD_TAG": "b-1"},
        )

        assert facts == {"deployed_by": "b-1"}


def test_the_provider_is_the_command_center_provider():
    assert isinstance(
        get_catalog_provider("command-center", {"endpoint": "http://127.0.0.1:9"}),
        FluidCommandCenterProvider,
    )
