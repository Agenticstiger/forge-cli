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
import subprocess
import time
from unittest.mock import patch

import httpx
import pytest

from fluid_build.streaming.compiler import compile_contract
from fluid_build.streaming.policy import SECRET_REF_ALLOWLIST_ENV
from fluid_build.streaming.runtime import (
    Kubectl,
    StreamingError,
    _DeliveryPending,
    _iceberg_fields,
    _iceberg_type,
    catalog_evidence,
    inspect_drift,
    ready,
    reconcile,
    verify,
)
from fluid_build.streaming.types import parse_fields, parse_type


def running(artifact):
    resource = copy.deepcopy(artifact.resources[1])
    resource["metadata"]["generation"] = 2
    resource["status"] = {
        "observedGeneration": 2,
        "jobManagerDeploymentStatus": "READY",
        "reconciliationStatus": {"state": "DEPLOYED"},
        "jobStatus": {"state": "RUNNING", "jobId": "a" * 32},
    }
    return resource


def test_ready_requires_current_generation_and_digest(contract):
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    assert ready(resource, artifact)
    resource["status"]["observedGeneration"] = 1
    assert not ready(resource, artifact)
    resource["status"]["observedGeneration"] = 2
    resource["metadata"]["annotations"]["fluid.build/digest"] = "old"
    assert not ready(resource, artifact)


def test_unplanned_flink_configuration_is_not_an_api_default(contract):
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    resource["spec"]["flinkConfiguration"]["unplanned.option"] = "value"
    assert not ready(resource, artifact)
    with patch.object(Kubectl, "get", side_effect=[resource, artifact.resources[0]]):
        assert inspect_drift(contract)[0]["has_drift"]


def test_operator_serialized_generation(contract):
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    del resource["status"]["observedGeneration"]
    resource["status"]["reconciliationStatus"]["lastReconciledSpec"] = json.dumps(
        {
            "meta": {"generation": 2},
            "spec": resource["spec"],
        }
    )
    assert ready(resource, artifact)
    resource["status"]["reconciliationStatus"]["lastReconciledSpec"] = json.dumps(
        {
            "meta": {"apiVersion": "flink.apache.org/v1beta1", "firstDeployment": False},
            "spec": {**resource["spec"], "image": "stale"},
        }
    )
    assert not ready(resource, artifact)
    resource["status"]["reconciliationStatus"]["lastReconciledSpec"] = "invalid"
    assert not ready(resource, artifact)


def test_dry_run_never_contacts_kubernetes(contract):
    with patch.object(Kubectl, "command", side_effect=AssertionError("network")):
        assert reconcile(contract, dry_run=True)[0]["status"] == "planned"


def test_existing_matching_deployment_is_not_reapplied(contract):
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    with (
        patch.object(
            Kubectl,
            "get",
            side_effect=lambda kind, name: (
                resource if kind == "flinkdeployment" else artifact.resources[0]
            ),
        ),
        patch.object(Kubectl, "apply") as apply,
    ):
        assert reconcile(contract)[0]["status"] == "running"
        apply.assert_not_called()


def test_new_deployment_applies_config_before_job(contract):
    (artifact,) = compile_contract(contract)
    with (
        patch.object(Kubectl, "get", side_effect=[None, None, running(artifact)]),
        patch.object(Kubectl, "apply") as apply,
    ):
        assert reconcile(contract)[0]["status"] == "running"
        assert [c.args[0]["kind"] for c in apply.call_args_list] == ["ConfigMap", "FlinkDeployment"]


def test_ownership_refusal_precedes_writes(contract):
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    resource["metadata"]["annotations"]["fluid.build/product"] = "another-product"
    with (
        patch.object(Kubectl, "get", return_value=resource),
        patch.object(Kubectl, "apply") as apply,
    ):
        with pytest.raises(StreamingError, match="another product"):
            reconcile(contract)
        apply.assert_not_called()


def test_create_only_and_replace_refused(contract):
    (artifact,) = compile_contract(contract)
    with patch.object(Kubectl, "get", return_value=running(artifact)):
        with pytest.raises(StreamingError, match="already exists"):
            reconcile(contract, mode="create-only")
    with pytest.raises(StreamingError, match="replace/reset"):
        reconcile(contract, mode="replace")


def test_timeout_and_failed_operator_are_not_success(contract):
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    resource["status"]["jobStatus"]["state"] = "RECONCILING"
    with (
        patch.object(Kubectl, "get", side_effect=[None, None, resource]),
        patch.object(Kubectl, "apply"),
        patch("fluid_build.streaming.runtime.time.monotonic", side_effect=[0, 1]),
    ):
        with pytest.raises(StreamingError, match="timeout"):
            reconcile(contract, timeout_seconds=0.001)
    resource["status"]["error"] = "failed"
    with (
        patch.object(Kubectl, "get", side_effect=[None, None, resource]),
        patch.object(Kubectl, "apply"),
    ):
        with pytest.raises(StreamingError, match="failed"):
            reconcile(contract)


def test_missing_kubectl_is_actionable():
    with patch("fluid_build.streaming.runtime.shutil.which", return_value=None):
        with pytest.raises(StreamingError, match="kubectl is required"):
            Kubectl("test", "dev").get("flinkdeployment", "job")


def test_kubectl_uses_explicit_context_and_bounded_stdin():
    with (
        patch("fluid_build.streaming.runtime.shutil.which", return_value="/usr/bin/kubectl"),
        patch("fluid_build.streaming.runtime.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        Kubectl("test", "dev").apply({"kind": "ConfigMap"})
        argv = run.call_args.args[0]
        assert "--context=dev" in argv and "--namespace=test" in argv
        assert "--server-side" in argv and "--force-conflicts" not in argv
        assert json.loads(run.call_args.kwargs["input"]) == {"kind": "ConfigMap"}
        assert run.call_args.kwargs["timeout"] == 65


@pytest.mark.parametrize("timeout", [False, True])
def test_kubectl_errors_are_explicit_and_do_not_leak_resource_bodies(timeout):
    with (
        patch("fluid_build.streaming.runtime.shutil.which", return_value="/usr/bin/kubectl"),
        patch("fluid_build.streaming.runtime.subprocess.run") as run,
    ):
        if timeout:
            run.side_effect = subprocess.TimeoutExpired(["kubectl"], 60)
        else:
            run.return_value.returncode = 1
            run.return_value.stderr = "private-resource-body"
        with pytest.raises(StreamingError) as error:
            Kubectl("test", "dev").get("configmap", "job")
        assert "private-resource-body" not in str(error.value)


def test_configmap_conflict_prevents_all_writes(contract):
    (artifact,) = compile_contract(contract)
    config = copy.deepcopy(artifact.resources[0])
    config["data"]["job.properties"] += "unexpected"
    with (
        patch.object(Kubectl, "get", side_effect=[None, config]),
        patch.object(Kubectl, "apply") as apply,
    ):
        with pytest.raises(StreamingError, match="planned content"):
            reconcile(contract)
        apply.assert_not_called()


def test_suspend_waits_for_current_terminal_operator_status(contract):
    contract["builds"][0]["properties"]["parameters"]["flink"]["deployment"]["state"] = "suspended"
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    resource["status"]["jobStatus"]["state"] = "FINISHED"
    with (
        patch.object(Kubectl, "get", side_effect=[None, None, resource]),
        patch.object(Kubectl, "apply"),
    ):
        assert reconcile(contract)[0]["status"] == "suspended"


@pytest.mark.parametrize("changed", [False, True])
def test_drift_uses_last_applied_spec_not_the_next_plan(contract, changed):
    (artifact,) = compile_contract(contract)
    resource = running(artifact)
    if changed:
        resource["spec"]["job"]["parallelism"] += 1
    contract["builds"][0]["properties"]["parameters"]["flink"]["deployment"]["parallelism"] = 3
    with patch.object(Kubectl, "get", side_effect=[resource, artifact.resources[0]]):
        (result,) = inspect_drift(contract)
        assert result["has_drift"] == changed
        assert result["status"] == ("drifted" if changed else "pending")


def test_new_stream_is_pending_not_drift(contract):
    with patch.object(Kubectl, "get", return_value=None):
        assert inspect_drift(contract) == [
            {
                "build": "ingest-orders",
                "status": "pending",
                "has_drift": False,
            }
        ]


def test_missing_drift_baseline_is_unknown_not_clean(contract):
    (artifact,) = compile_contract(contract)
    with patch.object(Kubectl, "get", side_effect=[running(artifact), None]):
        with pytest.raises(StreamingError, match="drift is unknown"):
            inspect_drift(contract)


@pytest.mark.parametrize("auth_type", ["bearer", "oauth2"])
def test_catalog_auth_is_resolved_locally_and_sent_only_as_headers(
    contract, monkeypatch, auth_type
):
    contract["exposes"][0]["contract"]["schema"] = [{"name": "id", "type": "long"}]
    contract["exposes"][0]["binding"]["location"]["uri"] = "https://catalog.example"
    cfg = contract["builds"][0]["properties"]["parameters"]["flink"]
    if auth_type == "bearer":
        cfg["catalogAuth"] = {"type": "bearer", "tokenEnv": "CATALOG_TOKEN"}
        monkeypatch.setenv("CATALOG_TOKEN", "synthetic-token")
        monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CATALOG_TOKEN@catalog.example")
    else:
        cfg["catalogAuth"] = {
            "type": "oauth2",
            "credentialEnv": "CATALOG_CREDENTIAL",
            "oauth2ServerUri": "https://identity.example/token",
            "scope": "catalog",
            "audience": "lake",
            "resource": "warehouse",
        }
        monkeypatch.setenv("CATALOG_CREDENTIAL", "synthetic-client:synthetic-secret")
        # The credential goes to the identity provider and the token minted from it
        # to the catalog, so the operator pairs the variable with both hosts.
        monkeypatch.setenv(
            SECRET_REF_ALLOWLIST_ENV,
            "CATALOG_CREDENTIAL@identity.example,CATALOG_CREDENTIAL@catalog.example",
        )
    (artifact,) = compile_contract(contract)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "identity.example":
            assert request.method == "POST"
            assert b"grant_type=client_credentials" in request.content
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        assert "synthetic" not in str(request.url)
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"defaults": {"prefix": "warehouse"}})
        assert "/v1/warehouse/namespaces/commerce/tables/orders" == request.url.path
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "current-schema-id": 1,
                    "schemas": [
                        {
                            "schema-id": 1,
                            "fields": [
                                {"id": 1, "name": "id", "type": "long", "required": False},
                            ],
                        }
                    ],
                    "current-snapshot-id": 2,
                    "snapshots": [{"snapshot-id": 2, "timestamp-ms": int(time.time() * 1000)}],
                }
            },
        )

    def client(**kwargs):
        return httpx.Client(
            base_url=kwargs.get("base_url", ""),
            headers=kwargs.get("headers"),
            transport=httpx.MockTransport(handler),
        )

    with patch("fluid_build.streaming.runtime.safe_httpx_client", side_effect=client):
        evidence = catalog_evidence(contract, contract["builds"][0], artifact)
    assert evidence["snapshotId"] == 2
    assert len(calls) == (3 if auth_type == "oauth2" else 2)


def test_dry_run_mode_itself_prevents_network_calls(contract):
    with patch.object(Kubectl, "command", side_effect=AssertionError("network")):
        assert reconcile(contract, mode="dry-run")[0]["status"] == "planned"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_readiness_wait_is_always_bounded(contract, timeout):
    with pytest.raises(
        StreamingError,
        match="timeout must be positive",
    ):
        reconcile(contract, timeout_seconds=timeout)


def test_verification_waits_only_for_pending_delivery_evidence(contract):
    with (
        patch(
            "fluid_build.streaming.runtime._verify_once",
            side_effect=[
                _DeliveryPending("checkpoint pending"),
                [{"status": "delivery-verified"}],
            ],
        ) as attempt,
        patch("fluid_build.streaming.runtime.time.sleep") as sleep,
    ):
        assert verify(contract)[0]["status"] == "delivery-verified"
        assert attempt.call_count == 2
        sleep.assert_called_once()
    with (
        patch(
            "fluid_build.streaming.runtime._verify_once", side_effect=StreamingError("schema drift")
        ),
        patch("fluid_build.streaming.runtime.time.sleep") as sleep,
    ):
        with pytest.raises(StreamingError, match="schema drift"):
            verify(contract)
        sleep.assert_not_called()


@pytest.mark.parametrize("timestamp", [0, "invalid", float("inf")])
def test_invalid_or_stale_checkpoints_are_never_delivery(contract, timestamp):
    (artifact,) = compile_contract(contract)
    with (
        patch.object(Kubectl, "get", return_value=running(artifact)),
        patch.object(
            Kubectl,
            "command",
            return_value=json.dumps(
                {
                    "latest": {"completed": {"id": 7, "latest_ack_timestamp": timestamp}},
                }
            ),
        ),
        patch("fluid_build.streaming.runtime.catalog_evidence") as evidence,
    ):
        with pytest.raises(StreamingError, match="stale|timestamp|clock skew"):
            verify(contract, timeout_seconds=0)
        evidence.assert_not_called()


@pytest.mark.parametrize("baseline", [None, "invalid-json", "[]", "{}"])
def test_invalid_last_applied_specs_never_look_clean(contract, baseline):
    (artifact,) = compile_contract(contract)
    config = copy.deepcopy(artifact.resources[0])
    if baseline is None:
        config["data"].pop("deployment.json")
    else:
        config["data"]["deployment.json"] = baseline
    with patch.object(Kubectl, "get", side_effect=[running(artifact), config]):
        with pytest.raises(StreamingError, match="specification"):
            inspect_drift(contract)


@pytest.mark.parametrize(
    "actual",
    [
        None,
        [{"name": "id"}],
        [{"name": "id", "type": "long", "required": "false"}],
        [{"name": "id", "type": "long"}, {"name": "id", "type": "long"}],
    ],
)
def test_malformed_catalog_field_metadata_is_rejected(actual):
    with pytest.raises(StreamingError, match="metadata|duplicate"):
        _iceberg_fields(parse_fields([{"name": "id", "type": "long"}]), actual)


def test_nested_catalog_schema_comparison():
    fields = parse_fields(
        [
            {
                "name": "events",
                "type": "array",
                "element": {
                    "type": "struct",
                    "fields": [{"name": "id", "type": "long", "required": True}],
                },
            }
        ]
    )
    actual = [
        {
            "name": "events",
            "id": 1,
            "required": False,
            "type": {
                "type": "list",
                "element-id": 2,
                "element-required": False,
                "element": {
                    "type": "struct",
                    "fields": [{"id": 3, "name": "id", "required": True, "type": "long"}],
                },
            },
        }
    ]
    _iceberg_fields(fields, actual)
    actual[0]["type"]["element"]["fields"][0]["type"] = "string"
    with pytest.raises(StreamingError, match="type drift"):
        _iceberg_fields(fields, actual)


def test_array_nullability_and_map_drift():
    with pytest.raises(StreamingError, match="element nullability"):
        _iceberg_type(
            parse_type({"type": "array", "element": {"type": "int", "required": True}}),
            {"type": "list", "element": "int"},
            "$",
        )
    with pytest.raises(StreamingError, match="map value"):
        _iceberg_type(
            parse_type(
                {
                    "type": "map",
                    "key": {"type": "string", "required": True},
                    "value": {"type": "string"},
                }
            ),
            {"type": "map", "key": "string", "value": "string", "value-required": True},
            "$",
        )


def test_running_without_checkpoint_is_not_delivery(contract):
    (artifact,) = compile_contract(contract)
    with (
        patch.object(Kubectl, "get", return_value=running(artifact)),
        patch.object(Kubectl, "command", return_value='{"latest":{}}'),
        patch("fluid_build.streaming.runtime.catalog_evidence") as evidence,
    ):
        with pytest.raises(StreamingError, match="No successful"):
            verify(contract, timeout_seconds=0)
        evidence.assert_not_called()


def test_successful_checkpoint_and_catalog_evidence(contract):
    (artifact,) = compile_contract(contract)
    checkpoint = {
        "latest": {"completed": {"id": 7, "latest_ack_timestamp": int(time.time() * 1000)}}
    }
    with (
        patch.object(Kubectl, "get", return_value=running(artifact)),
        patch.object(Kubectl, "command", return_value=json.dumps(checkpoint)),
        patch("fluid_build.streaming.runtime.catalog_evidence", return_value={"snapshotId": 10}),
    ):
        (result,) = verify(contract)
        assert result["status"] == "delivery-verified"
        assert result["checkpointId"] == 7
        assert result["snapshotId"] == 10


@pytest.mark.parametrize(
    "snapshots,current", [([], None), ([{"snapshot-id": 1, "timestamp-ms": 1}], 1)]
)
def test_missing_or_stale_commit_refused(contract, snapshots, current):
    # Keep one scalar to isolate snapshot evidence from recursive schema checks.
    contract["exposes"][0]["contract"]["schema"] = [{"name": "id", "type": "long"}]
    (artifact,) = compile_contract(contract)
    metadata = {
        "current-schema-id": 0,
        "schemas": [
            {
                "schema-id": 0,
                "fields": [
                    {"id": 1, "name": "id", "type": "long", "required": False},
                ],
            }
        ],
        "current-snapshot-id": current,
        "snapshots": snapshots,
    }

    def handler(request):
        body = {"defaults": {}} if request.url.path.endswith("/config") else {"metadata": metadata}
        return httpx.Response(200, json=body)

    with patch(
        "fluid_build.streaming.runtime.safe_httpx_client",
        return_value=httpx.Client(
            base_url="http://catalog/",
            transport=httpx.MockTransport(handler),
        ),
    ):
        with pytest.raises(StreamingError, match="snapshot|freshness"):
            catalog_evidence(contract, contract["builds"][0], artifact)
