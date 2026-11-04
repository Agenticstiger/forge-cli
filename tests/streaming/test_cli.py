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

import copy
import json
import logging
from unittest.mock import patch

import pytest
import yaml

from fluid_build._contract_loader import CLIError
from fluid_build.cli import build_parser
from fluid_build.cli.apply import run as apply
from fluid_build.cli.plan import run as plan
from fluid_build.cli.verify import run as verify
from fluid_build.streaming.compiler import compile_contract
from fluid_build.streaming.policy import CONTEXT_ALLOWLIST_ENV, FlinkPolicyError
from fluid_build.streaming.runtime import Kubectl


def test_generate_command_registered_and_emits_artifacts(contract, tmp_path):
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    out = tmp_path / "generated"
    parser = build_parser()
    args = parser.parse_args(["generate", "flink", str(source), "--out", str(out)])
    assert args.func(args, logging.getLogger("test")) == 0
    records = json.loads((out / "manifest.json").read_text())
    assert len(records) == 1
    assert (out / records[0]["sql"]).is_file()
    resources = list(yaml.safe_load_all((out / records[0]["resources"]).read_text()))
    assert [r["kind"] for r in resources] == ["ConfigMap", "FlinkDeployment"]


def test_plan_and_bound_dry_apply_do_not_need_cloud_or_cluster(contract, tmp_path):
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    output = tmp_path / "plan.json"
    parser = build_parser()
    args = parser.parse_args(["plan", str(source), "--out", str(output), "--mode", "dry-run"])
    assert plan(args, logging.getLogger("test")) == 0
    result = json.loads(output.read_text())
    assert result["planDigest"]
    assert result["actions"][0]["op"] == "flink.reconcile"
    assert result["actions"][0]["step"] == 1
    assert result["actions"][0]["action_id"] == "ingest-orders"
    assert result["actions"][0]["provider"] == "flink-operator"
    args = parser.parse_args(["apply", str(output), "--mode", "dry-run", "--yes"])
    with patch(
        "fluid_build.streaming.runtime.Kubectl.command", side_effect=AssertionError("network")
    ):
        assert apply(args, logging.getLogger("test")) == 0


def test_apply_reconciles_without_running_batch_provider(contract, tmp_path):
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    args = build_parser().parse_args(["apply", str(source), "--yes"])
    with (
        patch(
            "fluid_build.streaming.runtime.reconcile",
            return_value=[
                {"build": "ingest-orders", "status": "running"},
            ],
        ) as reconcile,
        patch(
            "fluid_build.cli._apply_opentofu_engine.apply_via_opentofu",
            side_effect=AssertionError("tofu"),
        ),
    ):
        assert apply(args, logging.getLogger("test")) == 0
        reconcile.assert_called_once()


def test_verify_routes_to_streaming_and_writes_evidence(contract, tmp_path):
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    output = tmp_path / "evidence.json"
    args = build_parser().parse_args(["verify", str(source), "--out", str(output), "--strict"])
    evidence = [
        {
            "build": "ingest-orders",
            "status": "delivery-verified",
            "snapshotId": 10,
            "checkpointId": 4,
        }
    ]
    with patch("fluid_build.streaming.runtime.verify", return_value=evidence) as check:
        assert verify(args, logging.getLogger("test")) == 0
        check.assert_called_once()
    assert json.loads(output.read_text()) == evidence


@pytest.mark.parametrize("tamper", [True, False])
def test_bound_plan_gates_precede_managed_reconciliation(contract, tmp_path, tamper):
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    output = tmp_path / "plan.json"
    parser = build_parser()
    assert (
        plan(
            parser.parse_args(["plan", str(source), "--out", str(output)]),
            logging.getLogger("test"),
        )
        == 0
    )
    if tamper:
        body = json.loads(output.read_text())
        body["actions"][0]["op"] = "modified"
        output.write_text(json.dumps(body))
    args = parser.parse_args(
        ["apply", str(output), "--yes"] + ([] if tamper else ["--mode", "create-only"])
    )
    with patch("fluid_build.streaming.runtime.reconcile") as reconcile:
        with pytest.raises(CLIError) as error:
            apply(args, logging.getLogger("test"))
        assert error.value.event == (
            "apply_plan_digest_plan_tamper" if tamper else "apply_plan_mode_mismatch"
        )
        reconcile.assert_not_called()


def test_live_drift_routes_to_operator_not_warehouse(contract, tmp_path):
    from fluid_build.cli.diff import run as diff

    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    output = tmp_path / "drift.json"
    args = build_parser().parse_args(["diff", str(source), "--out", str(output), "--exit-on-drift"])
    with (
        patch(
            "fluid_build.streaming.runtime.inspect_drift",
            return_value=[
                {"build": "ingest-orders", "has_drift": True, "status": "drifted"},
            ],
        ),
        patch("fluid_build.cli.diff.build_provider", side_effect=AssertionError("warehouse")),
    ):
        assert diff(args, logging.getLogger("test")) != 0
    assert json.loads(output.read_text())["summary"]["has_drift"]


def test_a_policy_refusal_reaches_the_user_as_itself(contract, tmp_path, monkeypatch):
    from fluid_build.cli.diff import run as diff

    monkeypatch.delenv(CONTEXT_ALLOWLIST_ENV)
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    args = build_parser().parse_args(["diff", str(source), "--out", str(tmp_path / "drift.json")])
    with (
        patch.object(Kubectl, "command", side_effect=AssertionError("kubectl ran")),
        patch("fluid_build.cli.diff.build_provider", side_effect=AssertionError("warehouse")),
    ):
        with pytest.raises(FlinkPolicyError) as refused:  # not flattened into diff_failed
            diff(args, logging.getLogger("test"))
    assert CONTEXT_ALLOWLIST_ENV in refused.value.fix


def test_recompiled_resources_must_match_the_bound_plan(contract, tmp_path):
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    output = tmp_path / "plan.json"
    parser = build_parser()
    assert (
        plan(
            parser.parse_args(["plan", str(source), "--out", str(output)]),
            logging.getLogger("test"),
        )
        == 0
    )
    changed = copy.deepcopy(compile_contract(contract))
    changed[0].resources[1]["spec"]["job"]["parallelism"] += 1
    args = parser.parse_args(["apply", str(output), "--yes"])
    with (
        patch("fluid_build.streaming.compiler.compile_contract", return_value=changed),
        patch("fluid_build.streaming.runtime.reconcile") as reconcile,
        patch("fluid_build.cli.apply._run_apply_hooks_for_source") as hooks,
    ):
        with pytest.raises(CLIError) as error:
            apply(args, logging.getLogger("test"))
        assert error.value.event == "apply_streaming_artifact_mismatch"
        reconcile.assert_not_called()
        hooks.assert_not_called()


def test_generated_filenames_do_not_collide_between_namespaces(contract, tmp_path):
    first = contract["builds"][0]["properties"]["parameters"]["flink"]["deployment"]
    first.update(namespace="data", name="a-b")
    other = copy.deepcopy(contract["builds"][0])
    other["id"] = "other"
    other["outputs"] = ["other-output"]
    other_deployment = other["properties"]["parameters"]["flink"]["deployment"]
    other_deployment.update(
        namespace="data-a",
        name="b",
        checkpointUri="s3://flink-state/other/checkpoints",
        savepointUri="s3://flink-state/other/savepoints",
        haUri="s3://flink-state/other/ha",
    )
    contract["builds"].append(other)
    target = copy.deepcopy(contract["exposes"][0])
    target["exposeId"] = "other-output"
    target["binding"]["location"]["table"] = "other_table"
    contract["exposes"].append(target)
    source = tmp_path / "contract.fluid.yaml"
    source.write_text(yaml.safe_dump(contract))
    out = tmp_path / "generated"
    args = build_parser().parse_args(["generate", "flink", str(source), "--out", str(out)])
    assert args.func(args, logging.getLogger("test")) == 0
    records = json.loads((out / "manifest.json").read_text())
    assert len({record["resources"] for record in records}) == 2
    for record in records:
        resources = list(yaml.safe_load_all((out / record["resources"]).read_text()))
        assert resources[1]["metadata"]["annotations"]["fluid.build/build"] == record["build"]
