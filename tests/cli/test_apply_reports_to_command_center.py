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

"""``fluid apply`` reports each run to the Command Center, against a stub server.

A run is registered at ``POST /api/v1/executions`` and closed at ``PATCH
/api/v1/executions/{id}``, the Command Center's executions API
(``app/api/v1/executions.py``: ``ExecutionCreate`` / ``ExecutionUpdate``),
with the credential and the organization ``fluid publish`` uses. Before this,
``get_reporter`` had no callers and a Jenkins apply left no run behind.

The stub is a real HTTP server on loopback that records every request. The
apply goes through the real parser and the real OpenTofu engine; only the
``tofu`` binary is stubbed (init, plan and apply answer with the change
summary a real run prints), and the native planner is skipped. Pinned: what
a run says (product, contract version, environment, provider, resources,
counts, timings, status), that the organization comes from the publish
configuration (id or slug), that the credential is only ever a header, that
tofu's own output never reaches the Command Center, and that an outage, a
refusal or a 500 never changes the apply's exit code.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import runner

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test.apply_cc_report")
_KEY = "cc-test-key-not-a-secret"  # pragma: allowlist secret
_TOFU_OUTPUT_MARKER = "tofu-printed-this-attribute-value"
_ORG = "0f5e3c1a-telco"


class _Recorder:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.status = {"POST": 201, "PATCH": 200, "GET": 200}


@pytest.fixture
def cc() -> Iterator[Any]:
    """A stub Command Center on loopback: ``(base_url, recorder)``."""
    recorder = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            recorder.requests.append(
                {
                    "method": method,
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "raw": raw.decode("utf-8"),
                    "body": json.loads(raw) if raw else None,
                }
            )
            status = recorder.status[method]
            if method == "GET" and self.path == "/api/v1/organizations":
                payload: Any = [{"id": _ORG, "slug": "northwind-telco", "name": "Telco"}]
            else:
                payload = {"ok": status < 400}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            self._answer("POST")

        def do_PATCH(self):  # noqa: N802
            self._answer("PATCH")

        def do_GET(self):  # noqa: N802
            self._answer("GET")

        def log_message(self, *_a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", recorder
    finally:
        server.shutdown()
        server.server_close()


_CONTRACT = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "bronze.customer_subscriptions",
    "name": "Customer Subscriptions",
    "version": "1.4.0",
    "description": "Run report fixture.",
    "domain": "Customer",
    "metadata": {"layer": "Bronze", "owner": {"team": "data-platform", "email": "dp@example.com"}},
    "exposes": [
        {
            "exposeId": "subscriptions",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "data/customer_subscriptions.parquet"},
            },
            "contract": {"schema": [{"name": "subscription_id", "type": "STRING"}]},
        }
    ],
}

_GCP_OVERLAY = {
    "exposes": [
        {
            "binding": {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {
                    "project": "northwind-demo",
                    "dataset": "demo_bronze",
                    "table": "customer_subscriptions",
                    "region": "europe-west1",
                },
            }
        }
    ]
}


@pytest.fixture
def product(tmp_path: Path, monkeypatch) -> Path:
    for var in (
        "FLUID_CC_ENDPOINT",
        "FLUID_CATALOG_FLUID_CC_URL",
        "FLUID_API_KEY",
        "FLUID_BEARER_TOKEN",
        "FLUID_CC_ORG_ID",
        "FLUID_COMMAND_CENTER_URL",
        "FLUID_COMMAND_CENTER_API_KEY",
        "FLUID_COMMAND_CENTER_ENABLED",
        "FLUID_STATE_BACKEND",
        "FLUID_PROVIDER",
        "JENKINS_URL",
        "BUILD_TAG",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "gcp.yaml").write_text(yaml.safe_dump(_GCP_OVERLAY), encoding="utf-8")
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(yaml.safe_dump(_CONTRACT, sort_keys=False), encoding="utf-8")
    return contract


def _summary(add: int) -> List[Dict[str, Any]]:
    return [{"type": "change_summary", "changes": {"add": add, "change": 0, "remove": 0}}]


@pytest.fixture
def tofu(monkeypatch) -> Dict[str, Any]:
    """The ``tofu`` binary, answered: init ok, plan +2, apply +2."""
    behaviour: Dict[str, Any] = {"apply_ok": True}
    ok = runner.TofuResult
    monkeypatch.setattr(runner, "tofu_path", lambda: "/usr/bin/tofu")
    monkeypatch.setattr(runner, "require_tofu_version", lambda *a, **k: None)
    monkeypatch.setattr(runner, "tofu_init", lambda *a, **k: ok("init", 0, "", ""))
    monkeypatch.setattr(runner, "tofu_state_list", lambda *a, **k: [])
    monkeypatch.setattr(runner, "tofu_state_resources", lambda *a, **k: [])
    monkeypatch.setattr(runner, "tofu_prior_state_resources", lambda *a, **k: [])
    monkeypatch.setattr(
        runner, "tofu_import", lambda *a, **k: ok("import", 1, "", "not found (stub)")
    )
    monkeypatch.setattr(
        runner, "tofu_plan", lambda *a, **k: ok("plan", 0, "", "", events=_summary(2))
    )

    def _apply(*_a, **_k):
        if behaviour["apply_ok"]:
            return ok("apply", 0, "", "", events=_summary(2))
        return ok("apply", 1, "", f"Error: googleapi: 403 {_TOFU_OUTPUT_MARKER}")

    monkeypatch.setattr(runner, "tofu_apply", _apply)
    monkeypatch.setattr(engine, "native_actions", lambda contract, logger: [])
    return behaviour


def _apply(contract: Path, *extra: str) -> int:
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(
        ["apply", str(contract), "--env", "gcp", "--yes", "--no-verify-federation", *extra]
    )
    return args.func(args, _LOG)


def _by(recorder: _Recorder, method: str) -> List[Dict[str, Any]]:
    return [r for r in recorder.requests if r["method"] == method]


def _configure(monkeypatch, url: str, *, org: bool = True) -> None:
    monkeypatch.setenv("FLUID_CC_ENDPOINT", url)
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    if org:
        monkeypatch.setenv("FLUID_CC_ORG_ID", _ORG)


def test_an_apply_is_registered_and_closed_with_what_it_did(product, tofu, cc, monkeypatch, capsys):
    url, recorder = cc
    _configure(monkeypatch, url)

    assert _apply(product) == 0

    (post,) = _by(recorder, "POST")
    (patch,) = _by(recorder, "PATCH")
    assert post["path"] == "/api/v1/executions"
    body = post["body"]
    assert body["command"] == "apply"
    assert body["status"] == "running"
    assert body["provider"] == "gcp"
    assert body["environment"] == "gcp"
    assert body["runner"] == "cli"
    meta = body["metadata"]
    assert meta["product_id"] == "bronze.customer_subscriptions"
    assert meta["contract_version"] == "1.4.0"
    assert meta["fluid_version"] == "0.7.5"
    assert meta["platform"] == "gcp"
    assert meta["environment"] == "gcp"
    assert len(meta["contract_hash"]) == 64
    assert meta["state"].startswith("local: ")

    assert patch["path"] == f"/api/v1/executions/{body['execution_id']}"
    update = patch["body"]
    assert update["status"] == "success"
    result = update["result"]
    assert result["planned_changes"] == {"add": 2, "change": 0, "remove": 0}
    assert result["applied_changes"] == {"add": 2, "change": 0, "remove": 0}
    assert result["dry_run"] is False
    assert result["exit_code"] == 0
    assert result["duration_seconds"] >= 0
    assert result["started_at"] <= result["finished_at"]
    assert "google_bigquery_dataset.bronze_customer_subscriptions_demo_bronze" in (
        result["resources"]
    )
    assert "google_bigquery_table.bronze_customer_subscriptions_customer_subscriptions" in (
        result["resources"]
    )

    # The organization and the credential travel as headers, and only there.
    for request in (post, patch):
        assert request["headers"]["X-API-Key"] == _KEY
        assert request["headers"]["X-Organization-Id"] == _ORG
        assert _KEY not in request["raw"]
    assert f"command center: run {body['execution_id']} reported" in capsys.readouterr().out


def test_the_organization_named_by_slug_in_fluid_config_is_resolved(product, tofu, cc, monkeypatch):
    """The demo lab names each product's organization by slug in its own
    fluid.config.yaml and never sets FLUID_CC_ORG_ID."""
    url, recorder = cc
    _configure(monkeypatch, url, org=False)
    (product.parent / "fluid.config.yaml").write_text(
        yaml.safe_dump({"catalogs": {"fluid-command-center": {"organization": "northwind-telco"}}}),
        encoding="utf-8",
    )

    assert _apply(product) == 0

    assert [r["path"] for r in _by(recorder, "GET")] == ["/api/v1/organizations"]
    for request in _by(recorder, "POST") + _by(recorder, "PATCH"):
        assert request["headers"]["X-Organization-Id"] == _ORG


def test_a_failed_apply_is_closed_failed_and_tofu_output_stays_home(product, tofu, cc, monkeypatch):
    url, recorder = cc
    _configure(monkeypatch, url)
    tofu["apply_ok"] = False

    with pytest.raises(CLIError) as exc:
        _apply(product)
    assert exc.value.event == "opentofu_apply_failed"

    (patch,) = _by(recorder, "PATCH")
    assert patch["body"]["status"] == "failed"
    assert patch["body"]["result"]["error_event"] == "opentofu_apply_failed"
    assert patch["body"]["result"]["applied_changes"] is None
    for request in recorder.requests:
        assert _TOFU_OUTPUT_MARKER not in request["raw"]


@pytest.mark.parametrize("status", [500, 401])
def test_a_command_center_that_refuses_never_changes_the_exit_code(
    product, tofu, cc, monkeypatch, capsys, status
):
    url, recorder = cc
    _configure(monkeypatch, url)
    recorder.status.update(POST=status, PATCH=status)

    assert _apply(product) == 0
    assert "not fully reported" in capsys.readouterr().out


def test_a_command_center_that_is_down_never_changes_the_exit_code(
    product, tofu, monkeypatch, capsys
):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    _configure(monkeypatch, f"http://127.0.0.1:{closed}")
    monkeypatch.setenv("FLUID_COMMAND_CENTER_TIMEOUT", "1")

    assert _apply(product) == 0
    assert "not fully reported" in capsys.readouterr().out


def test_nothing_is_sent_or_said_when_no_command_center_is_configured(product, tofu, cc, capsys):
    _url, recorder = cc
    assert _apply(product) == 0
    assert recorder.requests == []
    assert "command center" not in capsys.readouterr().out


def test_the_opt_out_sends_nothing(product, tofu, cc, monkeypatch):
    url, recorder = cc
    _configure(monkeypatch, url)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_ENABLED", "false")

    assert _apply(product) == 0
    assert recorder.requests == []


def test_a_private_address_is_refused_before_the_credential_is_sent(
    product, tofu, monkeypatch, capsys
):
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    called: List[str] = []
    monkeypatch.setattr(
        FluidCommandCenterProvider,
        "_list_organizations",
        lambda self: called.append("organizations"),
    )
    _configure(monkeypatch, "http://10.20.30.40:5200", org=False)

    assert _apply(product) == 0
    assert called == []
    assert "private or metadata address" in " ".join(capsys.readouterr().out.split())


# ── A run refused before the engine registered it still names its product ──


def test_a_sovereignty_refusal_is_reported_with_its_product(product, tofu, cc, monkeypatch):
    """The gcp overlay loses its region under a strict policy: the emitter
    refuses before the run is registered, and the run still says whose it is."""
    from fluid_build._errors import SovereigntyViolationError

    url, recorder = cc
    _configure(monkeypatch, url)
    doc = yaml.safe_load(product.read_text(encoding="utf-8"))
    doc["sovereignty"] = {
        "jurisdiction": "EU",
        "allowedRegions": ["europe-west1"],
        "enforcementMode": "strict",
    }
    product.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    overlay = product.parent / "overlays" / "gcp.yaml"
    placed = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    placed["exposes"][0]["binding"]["location"].pop("region")
    overlay.write_text(yaml.safe_dump(placed), encoding="utf-8")

    with pytest.raises(SovereigntyViolationError):
        _apply(product)

    (post,) = _by(recorder, "POST")
    (patch,) = _by(recorder, "PATCH")
    assert post["body"]["provider"] == "gcp"
    assert post["body"]["environment"] == "gcp"
    metadata = post["body"]["metadata"]
    assert metadata["product_id"] == "bronze.customer_subscriptions"
    assert metadata["contract_version"] == "1.4.0"
    assert metadata["platform"] == "gcp"
    assert metadata["contract_hash"]
    assert patch["body"]["status"] == "failed"
    assert patch["body"]["result"]["error_event"] == "SovereigntyViolationError"


def test_a_run_refused_before_the_contract_loads_names_the_base_product(
    product, tofu, cc, monkeypatch
):
    """``--env prod`` that the workspace expects, with no prod overlay: the
    loader refuses, and the run is still attached to the product."""
    url, recorder = cc
    _configure(monkeypatch, url)
    (product.parent / "fluid.workspace.yaml").write_text(
        yaml.safe_dump(
            {
                "workspace": {"name": "cc"},
                "expected-environments": {"bronze.customer_subscriptions": ["gcp", "prod"]},
            }
        ),
        encoding="utf-8",
    )
    from fluid_build import loader
    from fluid_build.cli import build_parser

    loader._NOTED_MISSING_OVERLAYS.clear()
    args = build_parser().parse_args(
        ["apply", str(product), "--env", "prod", "--yes", "--no-verify-federation"]
    )
    with pytest.raises(CLIError) as exc:
        args.func(args, _LOG)
    assert exc.value.event == "overlay_declared_but_missing"

    (post,) = _by(recorder, "POST")
    (patch,) = _by(recorder, "PATCH")
    metadata = post["body"]["metadata"]
    assert metadata["product_id"] == "bronze.customer_subscriptions"
    assert metadata["contract_version"] == "1.4.0"
    assert post["body"]["environment"] == "prod"
    # No platform (no overlay settled one) and no hash of a contract never compiled.
    assert "contract_hash" not in metadata
    assert post["body"]["provider"] is None
    assert patch["body"]["result"]["error_event"] == "overlay_declared_but_missing"


def test_a_provider_that_cannot_be_resolved_is_reported_with_its_product(
    product, tofu, cc, monkeypatch
):
    """``--provider aws`` against the gcp overlay: refused while the apply
    picks its engine, before the engine runs; the base names the product."""
    url, recorder = cc
    _configure(monkeypatch, url)

    with pytest.raises(CLIError) as exc:
        _apply(product, "--provider", "aws")
    assert exc.value.event == "generate_iac_provider_mismatch"

    (post,) = _by(recorder, "POST")
    metadata = post["body"]["metadata"]
    assert metadata["product_id"] == "bronze.customer_subscriptions"
    assert metadata["contract_version"] == "1.4.0"
    assert "platform" not in metadata
    assert "contract_hash" not in metadata
    assert post["body"]["environment"] == "gcp"


# ── A build-augmented apply reports its builds ────────────────────────────
#
# Measured against a stub Command Center on the integration branch: `fluid
# apply --mode amend-and-build` on a silver product whose build loaded 28 rows
# into BigQuery was closed with an infra-only result (planned and applied
# changes, resources) in phase "apply", and a bronze run whose load failed was
# closed "failed" with no error event and no error message.

_BUILD_ID = "summarise_subscriptions"
_LOADED_TABLE = "northwind-demo.demo_bronze.customer_subscriptions"


def _with_build(product: Path) -> Path:
    doc = yaml.safe_load(product.read_text(encoding="utf-8"))
    doc["builds"] = [
        {
            "id": _BUILD_ID,
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1 AS subscription_id"},
        }
    ]
    product.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return product


@pytest.fixture
def build(monkeypatch) -> Dict[str, Any]:
    """The embedded-SQL build, answered: it writes the run record a BigQuery load writes."""
    from fluid_build.build_runners import base

    behaviour: Dict[str, Any] = {"rc": 0, "calls": 0}

    def _execute(build, contract, contract_dir, **_kwargs):
        behaviour["calls"] += 1
        runs = Path(contract_dir) / ".fluid" / "runs" / contract["id"] / build["id"] / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        ok = behaviour["rc"] == 0
        record: Dict[str, Any] = {
            "run_id": f"01RUN{behaviour['calls']:08d}",
            "state": "succeeded" if ok else "failed",
            "records_total": 28 if ok else 0,
            "facets": {"engine": "duckdb", "pattern": "embedded-logic"},
        }
        if ok:
            record["facets"]["bigquery_load"] = {"table": _LOADED_TABLE, "rows": 28}
            record["facets"]["landed"] = {
                "mode": "full_refresh",
                "rows_from": "write",
                "destinations": {"subscriptions": f"bigquery://{_LOADED_TABLE}"},
            }
        (runs / f"{record['run_id']}.json").write_text(json.dumps(record), encoding="utf-8")
        return behaviour["rc"]

    monkeypatch.setattr(base, "_execute_embedded_sql_build", _execute)
    return behaviour


def test_a_build_augmented_apply_reports_each_build_and_what_it_landed(
    product, tofu, build, cc, monkeypatch
):
    url, recorder = cc
    _configure(monkeypatch, url)
    _with_build(product)

    assert _apply(product, "--mode", "amend-and-build") == 0
    assert build["calls"] == 1

    (post,) = _by(recorder, "POST")
    (patch,) = _by(recorder, "PATCH")
    assert post["body"]["metadata"]["mode"] == "amend-and-build"
    update = patch["body"]
    assert update["status"] == "success"
    assert update["current_phase"] == "build"
    assert update.get("error_message") is None
    result = update["result"]
    assert result["applied_changes"] == {"add": 2, "change": 0, "remove": 0}
    assert result["builds"] == [
        {
            "build_id": _BUILD_ID,
            "status": "succeeded",
            "run_id": "01RUN00000001",
            "table": _LOADED_TABLE,
            "rows": 28,
            "destinations": {"subscriptions": f"bigquery://{_LOADED_TABLE}"},
        }
    ]
    assert "error_event" not in result


def test_a_failed_build_is_reported_with_its_build_id(product, tofu, build, cc, monkeypatch):
    url, recorder = cc
    _configure(monkeypatch, url)
    _with_build(product)
    build["rc"] = 1

    assert _apply(product, "--mode", "amend-and-build") == 1

    (patch,) = _by(recorder, "PATCH")
    update = patch["body"]
    assert update["status"] == "failed"
    assert update["current_phase"] == "build"
    assert update["error_message"] == f"fluid apply failed: build_failed:{_BUILD_ID}"
    result = update["result"]
    assert result["error_event"] == f"build_failed:{_BUILD_ID}"
    assert result["exit_code"] == 1
    (reported,) = result["builds"]
    assert reported == {"build_id": _BUILD_ID, "status": "failed", "run_id": "01RUN00000001"}


def test_a_build_id_that_is_not_in_the_contract_is_the_reason(
    product, tofu, build, cc, monkeypatch
):
    url, recorder = cc
    _configure(monkeypatch, url)
    _with_build(product)

    assert _apply(product, "--mode", "amend-and-build", "--build-id", "no_such_build") == 1

    (patch,) = _by(recorder, "PATCH")
    assert patch["body"]["result"]["error_event"] == "build_not_found:no_such_build"
    assert patch["body"]["current_phase"] == "build"
    assert build["calls"] == 0
