"""Bounded operator reconciliation and independent delivery-health checks."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from fluid_build.providers._iceberg_catalog import resolve_iceberg_catalog
from fluid_build.providers._iceberg_catalog_auth import parse_catalog_auth
from fluid_build.streaming.compiler import FlinkArtifacts, compile_contract, managed_config
from fluid_build.streaming.types import LogicalType
from fluid_build.util.safe_http import safe_httpx_client


class StreamingError(ValueError):
    """A streaming operation could not establish its promised outcome."""


class _DeliveryPending(StreamingError):
    """A running stream has not yet produced fresh delivery evidence."""


@dataclass
class Kubectl:
    namespace: str
    context: str
    timeout: int = 60

    def command(self, *args: str, document: Mapping[str, Any] | None = None) -> str:
        executable = shutil.which("kubectl")
        if executable is None:
            raise StreamingError("kubectl is required; install it and select deployment.context")
        try:
            result = subprocess.run(
                [
                    executable,
                    f"--context={self.context}",
                    f"--namespace={self.namespace}",
                    f"--request-timeout={self.timeout}s",
                    *args,
                ],
                input=json.dumps(document) if document is not None else None,
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise StreamingError(
                "Kubernetes request timed out; inspect the deployment before retrying"
            ) from exc
        if result.returncode:
            # kubectl errors can echo resource bodies including user SQL/config.
            raise StreamingError(
                f"kubectl {args[0]} failed (exit {result.returncode}); check context, "
                "RBAC, operator CRDs and cluster diagnostics"
            )
        return result.stdout

    def get(self, kind: str, name: str) -> dict[str, Any] | None:
        raw = self.command("get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(raw) if raw.strip() else None

    def apply(self, resource: Mapping[str, Any]) -> None:
        self.command(
            "apply", "--server-side", "--field-manager=fluid-forge", "-f", "-", document=resource
        )


def _client(build: Mapping[str, Any]) -> Kubectl:
    cfg = managed_config(build)
    if cfg is None:
        raise StreamingError("Expected a managed Flink build")
    deployment = cfg["deployment"]
    return Kubectl(deployment["namespace"], deployment["context"])


def _owned(resource: Mapping[str, Any], artifact: FlinkArtifacts) -> None:
    actual = (resource.get("metadata") or {}).get("annotations") or {}
    expected = artifact.resources[1]["metadata"]["annotations"]
    for key in ("fluid.build/product", "fluid.build/build"):
        if actual.get(key) != expected[key]:
            raise StreamingError(
                "Refusing to adopt a Kubernetes object owned by another product/build"
            )


def _generation_reconciled(resource: Mapping[str, Any]) -> bool:
    generation = resource.get("metadata", {}).get("generation")
    status = resource.get("status", {})
    if generation is None:
        return False
    observed = status.get("observedGeneration")
    if observed is not None:
        return bool(observed == generation)
    raw = status.get("reconciliationStatus", {}).get("lastReconciledSpec")
    if not isinstance(raw, str):
        return False
    try:
        reconciled = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(reconciled, dict):
        return False
    spec = reconciled.get("spec")
    return isinstance(spec, dict) and bool(spec) and _contains(spec, resource.get("spec"))


def ready(resource: Mapping[str, Any], artifact: FlinkArtifacts) -> bool:
    status = resource.get("status", {})
    return (
        resource.get("metadata", {}).get("annotations", {}).get("fluid.build/digest")
        == artifact.digest
        and _generation_reconciled(resource)
        and status.get("jobManagerDeploymentStatus") == "READY"
        and status.get("jobStatus", {}).get("state") == "RUNNING"
        and status.get("reconciliationStatus", {}).get("state") == "DEPLOYED"
        and not status.get("error")
        and _contains(resource.get("spec"), artifact.resources[1]["spec"])
    )


def _contains(actual: Any, expected: Any) -> bool:
    """Allow API-server defaults but require every contract-owned value."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual
            and (
                actual[key] == value
                if key == "flinkConfiguration"
                else _contains(actual[key], value)
            )
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_contains(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return bool(actual == expected)


def reconcile(
    contract: Mapping[str, Any],
    *,
    mode: str = "amend",
    dry_run: bool = False,
    timeout_seconds: float = 300,
) -> list[dict[str, Any]]:
    artifacts = compile_contract(contract)
    if mode.startswith("replace"):
        raise StreamingError(
            "replace/reset is not supported for managed streams; restore state explicitly"
        )
    if mode not in {"dry-run", "create-only", "amend", "amend-and-build"}:
        raise StreamingError("Unsupported managed Flink apply mode")
    dry_run = dry_run or mode == "dry-run"
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise StreamingError("Readiness timeout must be positive")
    builds = {b["id"]: b for b in contract.get("builds") or []}
    pending: list[tuple[FlinkArtifacts, Kubectl, dict[str, Any] | None]] = []
    for artifact in artifacts:
        build = builds[artifact.build_id]
        client = _client(build)
        desired = artifact.resources[1]
        name = desired["metadata"]["name"]
        if dry_run:
            pending.append((artifact, client, None))
            continue
        existing = client.get("flinkdeployment", name)
        if existing:
            _owned(existing, artifact)
            if mode == "create-only":
                raise StreamingError(f"Managed Flink deployment {name} already exists")
        config_map = client.get("configmap", artifact.resources[0]["metadata"]["name"])
        if config_map:
            _owned(config_map, artifact)
            if config_map.get("data") != artifact.resources[0]["data"]:
                raise StreamingError(
                    "Content-addressed SQL ConfigMap differs from the planned content"
                )
        pending.append((artifact, client, existing))
    # Preflight every identity before changing any resources.
    results = []
    for artifact, client, existing in pending:
        desired = artifact.resources[1]
        name = desired["metadata"]["name"]
        if dry_run:
            results.append(
                {"build": artifact.build_id, "status": "planned", "digest": artifact.digest}
            )
            continue
        same = (
            existing is not None
            and existing.get("metadata", {}).get("annotations", {}).get("fluid.build/digest")
            == artifact.digest
            and _contains(existing.get("spec"), desired["spec"])
        )
        if not same:
            for resource in artifact.resources:
                client.apply(resource)
        if desired["spec"]["job"]["state"] == "suspended":
            # A request being accepted is not proof the savepoint/suspend completed.
            desired_state = "SUSPENDED"
        else:
            desired_state = "RUNNING"
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = client.get("flinkdeployment", name)
            if current is None:
                raise StreamingError(f"Deployment {name} disappeared during reconciliation")
            status = current.get("status", {})
            if status.get("error") or status.get("jobStatus", {}).get("state") == "FAILED":
                raise StreamingError(f"Deployment {name} failed; inspect operator/job diagnostics")
            if desired_state == "RUNNING" and ready(current, artifact):
                break
            if desired_state == "SUSPENDED" and (
                _generation_reconciled(current)
                and status.get("jobStatus", {}).get("state")
                in {"FINISHED", "CANCELED", "SUSPENDED"}
                and status.get("reconciliationStatus", {}).get("state") == "DEPLOYED"
                and current.get("metadata", {}).get("annotations", {}).get("fluid.build/digest")
                == artifact.digest
                and _contains(current.get("spec"), desired["spec"])
            ):
                break
            if time.monotonic() >= deadline:
                raise StreamingError(
                    f"Deployment {name} did not reach {desired_state} before timeout"
                )
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        results.append(
            {"build": artifact.build_id, "status": desired_state.lower(), "digest": artifact.digest}
        )
    return results


def _iceberg_type(expected: LogicalType, actual: Any, path: str) -> None:
    if expected.kind == "struct":
        if not isinstance(actual, Mapping) or actual.get("type") != "struct":
            raise StreamingError(f"{path}: expected an Iceberg struct")
        _iceberg_fields(expected.fields, actual.get("fields"), path)
    elif expected.kind == "array":
        if (
            not isinstance(actual, Mapping)
            or actual.get("type") != "list"
            or expected.element is None
        ):
            raise StreamingError(f"{path}: expected an Iceberg list")
        if actual.get("element-required", False) != expected.element.required:
            raise StreamingError(f"{path}: array element nullability drift")
        _iceberg_type(expected.element, actual.get("element"), path + "[]")
    elif expected.kind == "map":
        if (
            not isinstance(actual, Mapping)
            or actual.get("type") != "map"
            or expected.key is None
            or expected.value is None
        ):
            raise StreamingError(f"{path}: expected an Iceberg map")
        if actual.get("value-required", False) != expected.value.required:
            raise StreamingError(f"{path}: map value nullability drift")
        _iceberg_type(expected.key, actual.get("key"), path + "{key}")
        _iceberg_type(expected.value, actual.get("value"), path + "{value}")
    else:
        kind = {"timestamp_ltz": "timestamptz"}.get(expected.kind, expected.kind)
        if kind == "decimal":
            kind = f"decimal({expected.precision}, {expected.scale})"
        if str(actual).replace(" ", "") != kind.replace(" ", ""):
            raise StreamingError(f"{path}: Iceberg logical type drift")


def _iceberg_fields(expected: Any, actual: Any, path: str = "$") -> None:
    if not isinstance(actual, list):
        raise StreamingError(f"{path}: missing Iceberg field metadata")
    if any(
        not isinstance(field, Mapping)
        or not isinstance(field.get("name"), str)
        or "type" not in field
        or not isinstance(field.get("required", False), bool)
        for field in actual
    ):
        raise StreamingError(f"{path}: invalid Iceberg field metadata")
    fields = {f["name"]: f for f in actual}
    if len(fields) != len(actual):
        raise StreamingError(f"{path}: duplicate Iceberg fields")
    if set(fields) != {name for name, _ in expected}:
        raise StreamingError(f"{path}: Iceberg field set differs from the contract")
    for name, dtype in expected:
        if bool(fields[name].get("required", False)) != dtype.required:
            raise StreamingError(f"{path}.{name}: nullability drift")
        _iceberg_type(dtype, fields[name]["type"], f"{path}.{name}")


def catalog_evidence(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    artifact: FlinkArtifacts,
) -> dict[str, Any]:
    cfg = managed_config(build)
    if cfg is None:
        raise StreamingError("Expected a managed build")
    target = next(e for e in contract["exposes"] if e["exposeId"] == artifact.expose_id)
    resolved = resolve_iceberg_catalog(target["binding"])
    if not resolved.uri:
        raise StreamingError("REST catalog URI is required")
    auth, errors = parse_catalog_auth(cfg.get("catalogAuth"))
    if errors:
        raise StreamingError("; ".join(errors))
    headers = {}
    if auth:
        token = os.environ.get(auth.env_var)
        if not token:
            raise StreamingError("Catalog token variable must be set for CLI verification")
        if auth.type == "oauth2":
            client_id, separator, client_secret = token.partition(":")
            if not separator or not client_id or not client_secret:
                raise StreamingError(
                    "Catalog credential variable must contain client_id:client_secret"
                )
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": auth.scope,
            }
            if auth.audience:
                data["audience"] = auth.audience
            if auth.resource:
                data["resource"] = auth.resource
            with safe_httpx_client(allow_private=True, require_https=True, timeout=30) as oauth:
                response = oauth.post(auth.oauth2_server_uri, data=data)
                response.raise_for_status()
                token = response.json().get("access_token")
                if not isinstance(token, str) or not token:
                    raise StreamingError("Catalog OAuth2 response contains no access token")
        headers["Authorization"] = f"Bearer {token}"
    with safe_httpx_client(
        base_url=resolved.uri.rstrip("/") + "/",
        headers=headers,
        allow_private=True,
        require_https=bool(headers),
        timeout=30,
    ) as client:
        config = client.get("v1/config", params={"warehouse": resolved.warehouse})
        config.raise_for_status()
        body = config.json()
        prefix = (body.get("overrides") or {}).get(
            "prefix", (body.get("defaults") or {}).get("prefix", "")
        )
        prefix = f"{quote(str(prefix), safe='')}/" if prefix else ""
        namespace, table = resolved.fq_table.split(".")
        response = client.get(
            f"v1/{prefix}namespaces/{quote(namespace, safe='')}/tables/{quote(table, safe='')}"
        )
        response.raise_for_status()
        metadata = response.json().get("metadata") or {}
    schema_id = metadata.get("current-schema-id")
    schemas = [s for s in metadata.get("schemas", []) if s.get("schema-id") == schema_id]
    if len(schemas) != 1:
        raise StreamingError("Catalog returned no unique current Iceberg schema")
    _iceberg_fields(artifact.target_fields, schemas[0].get("fields"))
    snapshot_id = metadata.get("current-snapshot-id")
    snapshots = [s for s in metadata.get("snapshots", []) if s.get("snapshot-id") == snapshot_id]
    if len(snapshots) != 1:
        raise _DeliveryPending("No committed Iceberg snapshot: RUNNING is not delivered data")
    snapshot = snapshots[0]
    timestamp = snapshot.get("timestamp-ms")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise StreamingError("Iceberg snapshot has no valid timestamp")
    age = time.time() - timestamp / 1000
    if not math.isfinite(age) or age < -60:
        raise StreamingError("Iceberg snapshot has an invalid timestamp or clock skew")
    if age > cfg.get("maxSnapshotAgeSeconds", 300):
        raise _DeliveryPending("Iceberg commit freshness budget exceeded")
    return {
        "snapshotId": snapshot_id,
        "snapshotAgeSeconds": max(0, age),
        "schemaId": schema_id,
        "target": resolved.fq_table,
        "proof": "catalog schema and committed snapshot; not row-level read-back",
    }


def inspect_drift(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compare live resources to their immutable last-applied spec, not the next plan."""
    builds = {b["id"]: b for b in contract.get("builds") or []}
    results = []
    for artifact in compile_contract(contract):
        client = _client(builds[artifact.build_id])
        name = artifact.resources[1]["metadata"]["name"]
        current = client.get("flinkdeployment", name)
        if current is None:
            results.append({"build": artifact.build_id, "status": "pending", "has_drift": False})
            continue
        _owned(current, artifact)
        digest = current.get("metadata", {}).get("annotations", {}).get("fluid.build/digest", "")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise StreamingError("Managed deployment is missing its applied artifact digest")
        previous = client.get("configmap", f"{name[:40]}-{digest[:16]}")
        if previous is None:
            raise StreamingError("Last-applied immutable ConfigMap is missing; drift is unknown")
        _owned(previous, artifact)
        raw = previous.get("data", {}).get("deployment.json")
        if not isinstance(raw, str):
            raise StreamingError("Last-applied deployment specification is missing")
        try:
            applied_spec = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StreamingError("Last-applied deployment specification is invalid") from exc
        if not isinstance(applied_spec, dict) or not applied_spec:
            raise StreamingError("Last-applied deployment specification must be a nonempty object")
        drift = not _contains(current.get("spec"), applied_spec)
        results.append(
            {
                "build": artifact.build_id,
                "status": (
                    "drifted" if drift else ("in-sync" if digest == artifact.digest else "pending")
                ),
                "has_drift": drift,
                "appliedDigest": digest,
                "plannedDigest": artifact.digest,
            }
        )
    return results


def _verify_once(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    results = []
    builds = {b["id"]: b for b in contract.get("builds") or []}
    for artifact in compile_contract(contract):
        build = builds[artifact.build_id]
        cfg = managed_config(build)
        if cfg is None:
            raise StreamingError("Expected managed configuration")
        client = _client(build)
        name = artifact.resources[1]["metadata"]["name"]
        current = client.get("flinkdeployment", name)
        if current is None or not ready(current, artifact):
            raise StreamingError(f"Deployment {name} is not ready at the planned generation")
        _owned(current, artifact)
        job_id = current["status"]["jobStatus"].get("jobId")
        if not isinstance(job_id, str) or not re_full_job_id(job_id):
            raise StreamingError("Operator has not reported a valid Flink job id")
        namespace = cfg["deployment"]["namespace"]
        raw = client.command(
            "get",
            "--raw",
            f"/api/v1/namespaces/{namespace}/services/http:{name}-rest:8081/proxy/jobs/{job_id}/checkpoints",
        )
        checkpoints = json.loads(raw)
        completed = (checkpoints.get("latest") or {}).get("completed")
        if not completed:
            raise _DeliveryPending("No successful Flink checkpoint")
        timestamp = completed.get("latest_ack_timestamp", completed.get("trigger_timestamp", 0))
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise StreamingError("Successful Flink checkpoint has no valid timestamp")
        age = time.time() - timestamp / 1000
        if not math.isfinite(age) or age < -60:
            raise StreamingError("Successful Flink checkpoint has clock skew")
        if age > cfg.get("maxSnapshotAgeSeconds", 300):
            raise _DeliveryPending("Successful Flink checkpoint is stale")
        try:
            evidence = catalog_evidence(contract, build, artifact)
        except httpx.HTTPError as exc:
            raise StreamingError(
                "Iceberg catalog request failed; check connectivity, auth and table identity"
            ) from exc
        results.append(
            {
                "build": artifact.build_id,
                "status": "delivery-verified",
                "jobId": job_id,
                "checkpointId": completed["id"],
                **evidence,
            }
        )
    return results


def verify(contract: Mapping[str, Any], *, timeout_seconds: float = 120) -> list[dict[str, Any]]:
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise StreamingError("Delivery wait timeout must be finite and nonnegative")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return _verify_once(contract)
        except _DeliveryPending:
            if time.monotonic() >= deadline:
                raise
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def re_full_job_id(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)
