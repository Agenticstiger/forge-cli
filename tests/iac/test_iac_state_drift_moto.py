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

"""``fluid diff`` reads the apply's OpenTofu state, end to end against moto.

A contract is applied with the real OpenTofu engine (``apply_via_opentofu``,
``tofu init/plan/apply``) against a moto server, then ``fluid diff
--exit-on-drift`` runs through the real parser from the same directory, so
it finds the apply's workdir and state the way a pipeline's drift stage does.
The AWS provider and boto3 both reach moto through ``AWS_ENDPOINT_URL``, so
the state pass and the SDK column check read the same account.

Pinned here: a clean apply reads as no drift (the refresh's ``null`` ->
``{}`` read-backs notwithstanding); a tag added by hand and a Glue table
deleted by hand are drift and fail the gate, although the SDK column check
sees a matching table and an absent one; a contract change since the apply
is pending; and a directory no apply ran in falls back to the live check
with a note.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import runner
from fluid_build.iac.naming import safe_ident

pytestmark = [pytest.mark.integration, pytest.mark.provider, pytest.mark.aws]

_LOG = logging.getLogger("test.iac.state_drift")
_REGION = "us-east-1"
_CID = "drift.moto.orders"
_BUCKET = "drift-moto-lake"


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark.append(
    pytest.mark.skipif(
        runner.tofu_path() is None or not _have_moto_server(),
        reason="needs `tofu` on PATH + moto server extra (pip install 'moto[server]')",
    )
)


@pytest.fixture(scope="module")
def plugin_cache(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One provider download for the module, not one per workdir."""
    return str(tmp_path_factory.mktemp("tofu-plugin-cache"))


@pytest.fixture
def moto(monkeypatch, tmp_path: Path, plugin_cache: str) -> Iterator[str]:
    """A fresh moto server that both ``tofu`` and boto3 are pointed at."""
    import requests
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"http://127.0.0.1:{port}"
        with contextlib.suppress(Exception):
            requests.post(f"{endpoint}/moto-api/reset", timeout=5)
        for var in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "FLUID_STATE_BACKEND", "FLUID_PROVIDER"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
        monkeypatch.setenv("AWS_REGION", _REGION)
        monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", plugin_cache)
        monkeypatch.chdir(tmp_path)
        yield endpoint
    finally:
        server.stop()


@pytest.fixture
def plan_only(monkeypatch):
    """``fluid diff``'s resource-id plan is not under test; keep it offline."""
    from fluid_build.cli import diff as diff_mod

    class _PlanOnly:
        def plan(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
            return [{"op": "ensure_table", "resource_type": "table", "resource_id": "orders"}]

    monkeypatch.setattr(diff_mod, "build_provider", lambda *a, **k: _PlanOnly())


def _expose(expose_id: str, table: str, bucket: str = _BUCKET) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": "parquet",
            "location": {
                "database": "drift_silver",
                "table": table,
                "bucket": bucket,
                "path": f"silver/{table}/",
            },
        },
        "contract": {
            "schema": [
                {"name": "order_id", "type": "VARCHAR", "required": True},
                {"name": "amount", "type": "BIGINT", "required": True},
            ]
        },
    }


def _contract(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": _CID,
        "name": "Drift Orders",
        "domain": "Sales",
        "metadata": {"layer": "Silver", "owner": {"team": "data-platform"}},
        "exposes": list(exposes) or [_expose("orders", "orders")],
    }


def _write(root: Path, contract: Dict[str, Any]) -> Path:
    path = root / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _apply(contract_path: Path, workspace: Path) -> None:
    import argparse

    args = argparse.Namespace(
        contract=str(contract_path),
        env=None,
        provider=None,
        workspace_dir=str(workspace),
        state_backend=None,
        dry_run=False,
        allow_data_loss=False,
        no_verify_plan_binding=True,
    )
    assert engine.apply_via_opentofu(args, _LOG) == 0


def _diff(argv: List[str]):
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(["diff", *argv])
    try:
        return args.func(args, _LOG), None
    except CLIError as exc:
        return exc.exit_code, exc.event


def _boto(service: str, endpoint: str):
    import boto3

    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # pragma: allowlist secret
        region_name=_REGION,
    )


def _workdir(root: Path) -> Path:
    return root / ".fluid" / "iac" / "aws" / safe_ident(_CID)


def _state_section(out: Path) -> Dict[str, Any]:
    return json.loads(out.read_text(encoding="utf-8"))["state_drift"]


def _resource(section: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    found = [r for r in section["resources"] if r["address"].startswith(prefix)]
    assert len(found) == 1, section["resources"]
    return found[0]


def test_a_clean_apply_reads_as_no_drift(moto, plan_only, tmp_path, capsys):
    contract = _write(tmp_path, _contract())
    _apply(contract, tmp_path)
    module = (_workdir(tmp_path) / "main.tf.json").read_bytes()

    out = tmp_path / "diff.json"
    code, event = _diff([str(contract), "--exit-on-drift", "--out", str(out)])

    assert (code, event) == (0, None)
    section = _state_section(out)
    assert section["status"] == "checked"
    assert section["has_drift"] is False
    assert section["counts"] == {"drift": 0, "pending": 0, "match": 3}
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["has_drift"] is False
    assert [e["status"] for e in report["live"]["exposes"]] == ["match"]
    printed = capsys.readouterr().out
    assert "State drift check: 3 resource(s) in the apply's state" in printed
    assert "object-level encryption" in printed
    # The pass leaves the apply's workdir as it found it.
    assert (_workdir(tmp_path) / "main.tf.json").read_bytes() == module
    assert not (_workdir(tmp_path) / "fluid-drift.tfplan").exists()


def test_a_tag_changed_by_hand_is_drift_the_column_check_cannot_see(
    moto, plan_only, tmp_path, capsys
):
    contract = _write(tmp_path, _contract())
    _apply(contract, tmp_path)
    _boto("s3", moto).put_bucket_tagging(
        Bucket=_BUCKET, Tagging={"TagSet": [{"Key": "owner", "Value": "someone-else"}]}
    )

    out = tmp_path / "diff.json"
    code, event = _diff([str(contract), "--exit-on-drift", "--out", str(out)])

    assert (code, event) == (1, None)
    section = _state_section(out)
    bucket = _resource(section, "aws_s3_bucket.")
    assert bucket["status"] == "drift"
    assert "tags.owner" in bucket["drifted"]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["has_drift"] is True
    assert [e["status"] for e in report["live"]["exposes"]] == ["match"]
    assert "drift: changed outside the apply, " in capsys.readouterr().out

    # Without the gate the same finding is reported and exits 0.
    assert _diff([str(contract), "--out", str(out)]) == (0, None)
    # And --no-state-drift is the live check alone, which sees nothing.
    assert _diff([str(contract), "--exit-on-drift", "--no-state-drift", "--out", str(out)]) == (
        0,
        None,
    )
    skipped = _state_section(out)
    assert (skipped["status"], skipped["detail"]) == ("not_checked", "--no-state-drift")


def test_a_table_deleted_by_hand_is_drift_not_to_be_created(moto, plan_only, tmp_path):
    contract = _write(tmp_path, _contract())
    _apply(contract, tmp_path)
    _boto("glue", moto).delete_table(DatabaseName="drift_silver", Name="orders")

    out = tmp_path / "diff.json"
    code, _ = _diff([str(contract), "--exit-on-drift", "--out", str(out)])

    assert code == 1
    table = _resource(_state_section(out), "aws_glue_catalog_table.")
    assert table["status"] == "drift"
    assert table["deleted_outside"] is True
    # The SDK check alone reads "absent, to be created" and would pass.
    live = json.loads(out.read_text(encoding="utf-8"))["live"]["exposes"]
    assert [e["status"] for e in live] == ["absent"]


def test_a_contract_change_since_the_apply_is_pending(moto, plan_only, tmp_path):
    contract = _write(tmp_path, _contract())
    _apply(contract, tmp_path)
    grown = _write(
        tmp_path,
        _contract(_expose("orders", "orders"), _expose("refunds", "refunds", "drift-moto-refunds")),
    )

    out = tmp_path / "diff.json"
    code, _ = _diff([str(grown), "--exit-on-drift", "--out", str(out)])

    assert code == 0
    section = _state_section(out)
    assert section["has_drift"] is False
    pending = {r["address"]: r["actions"] for r in section["resources"] if r["status"] == "pending"}
    assert pending and all(actions == ["create"] for actions in pending.values())
    assert any(a.startswith("aws_s3_bucket.") and "refunds" in a for a in pending)


def test_no_reachable_state_falls_back_to_the_live_check(moto, plan_only, tmp_path, capsys):
    applied_in = tmp_path / "applied"
    applied_in.mkdir()
    contract = _write(applied_in, _contract())
    _apply(contract, applied_in)
    _boto("s3", moto).put_bucket_tagging(
        Bucket=_BUCKET, Tagging={"TagSet": [{"Key": "owner", "Value": "someone-else"}]}
    )

    # From a directory no apply ran in, the state is not found.
    out = tmp_path / "diff.json"
    code, _ = _diff([str(contract), "--exit-on-drift", "--out", str(out)])
    assert code == 0
    section = _state_section(out)
    assert section["status"] == "not_checked"
    assert "no apply state for this contract" in section["detail"]
    printed = capsys.readouterr().out
    assert "State drift check: not run" in printed
    assert "Drift comes from the live checks alone" in printed
    assert not (tmp_path / ".fluid").exists()

    # Pointed at the apply's directory, it is.
    code, _ = _diff(
        [str(contract), "--exit-on-drift", "--workspace-dir", str(applied_in), "--out", str(out)]
    )
    assert code == 1
    assert _state_section(out)["status"] == "checked"
