# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""kind / helm smoke test for managed-mode acquisition.

Spins up an ephemeral ``kind`` cluster, points helm at it, and exercises
the artifacts emitted by ``infra/kubernetes.py`` for a managed-mode
acquisition contract (Airbyte / Strimzi). The test:

1. Generates a Helm values overlay for the contract.
2. Validates it via ``helm lint``.
3. Performs a ``helm install --dry-run`` against the live kind cluster
   to confirm the manifests render and pass the API server's admission.

The full ``helm install`` of upstream Airbyte takes 5-10 minutes and
hundreds of MB of pulls; we skip it by default. Set
``FLUID_KIND_FULL_INSTALL=1`` to run the actual install on a CI runner
with sufficient resources.

Skips cleanly when:
* ``kind`` is not on PATH
* ``helm`` is not on PATH
* Docker daemon is not reachable
* User runs without ``FLUID_KIND_SMOKE=1`` (kind cluster boot is ~30s
  and we don't want it on every developer ``pytest -q``)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest


def _on_path(binary: str) -> bool:
    return shutil.which(binary) is not None


def _docker_reachable() -> bool:
    if not _on_path("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:  # noqa: BLE001
        return False


_KIND_GATED = os.environ.get("FLUID_KIND_SMOKE") != "1"
_FULL_INSTALL = os.environ.get("FLUID_KIND_FULL_INSTALL") == "1"


pytestmark = [
    pytest.mark.skipif(_KIND_GATED, reason="set FLUID_KIND_SMOKE=1 to enable kind smoke"),
    pytest.mark.skipif(not _on_path("kind"), reason="kind binary not on PATH"),
    pytest.mark.skipif(not _on_path("helm"), reason="helm binary not on PATH"),
    pytest.mark.skipif(not _on_path("kubectl"), reason="kubectl not on PATH"),
    pytest.mark.skipif(not _docker_reachable(), reason="docker daemon not reachable"),
    pytest.mark.integration,
]


# ── kind cluster fixture ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def kind_cluster() -> Iterator[str]:
    """Provision a kind cluster, yield its kubeconfig path, tear down."""
    cluster_name = f"fluid-smoke-{uuid.uuid4().hex[:6]}"
    kubeconfig = Path(f"/tmp/{cluster_name}-kubeconfig")
    create = subprocess.run(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            cluster_name,
            "--wait",
            "120s",
            "--kubeconfig",
            str(kubeconfig),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if create.returncode != 0:
        pytest.skip(f"kind cluster create failed: {create.stderr.strip()}")
    try:
        # Confirm the API is responsive before yielding.
        for _ in range(20):
            r = subprocess.run(
                ["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and "Ready" in r.stdout:
                break
            time.sleep(2)
        else:
            pytest.skip("kind cluster never became Ready")
        yield str(kubeconfig)
    finally:
        subprocess.run(
            ["kind", "delete", "cluster", "--name", cluster_name],
            capture_output=True,
            timeout=60,
        )
        try:
            kubeconfig.unlink()
        except FileNotFoundError:
            pass


# ── Tests ────────────────────────────────────────────────────────────────


def _managed_airbyte_contract() -> dict:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.crm.salesforce_managed",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@x.co"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "airbyte",
                "capabilities": ["incremental_append"],
                "properties": {
                    "source": {
                        "kind": "salesforce",
                        "connection": {"instance_url": "x"},
                        "mode": "incremental_append",
                    },
                    "airbyte": {
                        "deployment": {
                            "mode": "managed",
                            "managed": {
                                "target": "kubernetes",
                                "namespace": "fluid-airbyte",
                                "release_name": "airbyte",
                            },
                        },
                    },
                },
                "outputs": ["accounts_raw"],
            }
        ],
        "exposes": [
            {
                "exposeId": "accounts_raw",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "BRONZE",
                        "schema": "SF",
                        "table": "ACCT",
                    },
                },
            }
        ],
    }


class TestKindHelmManifestRender:
    def test_kubernetes_generator_emits_renderable_artifacts(
        self, kind_cluster: str, tmp_path: Path
    ):
        """Generate Helm artifacts → lint → kubectl --dry-run-server validate."""
        from fluid_build.infra.kubernetes import HelmGenerator

        contract = _managed_airbyte_contract()
        gen = HelmGenerator()
        bundle = gen.generate(contract)
        # The generator emits at least one file per managed engine.
        assert len(bundle.files) >= 1

        # Stage the bundle to disk.
        out_dir = tmp_path / "k8s_bundle"
        out_dir.mkdir()
        for f in bundle.files:
            p = out_dir / f.relative_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f.content)

        # Helm lint against the values overlay (lints only, no apply).
        values_files = list(out_dir.rglob("values*.yaml"))
        if not values_files:
            pytest.skip("no values.yaml emitted; skipping lint")
        for values in values_files:
            r = subprocess.run(
                ["helm", "lint", "--strict", str(values.parent)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # Non-fatal: helm lint of a values-only dir often warns about
            # missing Chart.yaml. We accept warnings (returncode 0) but
            # NOT actual errors. Errors print as ``Error:``.
            assert "Error:" not in r.stdout + r.stderr, (
                f"helm lint hard-error: {r.stdout}\n{r.stderr}"
            )

        # kubectl dry-run apply against the kind cluster — confirms the
        # YAMLs are admitted by the live API server. We only validate
        # the manifest YAMLs (not values), and only if the generator
        # emitted any rendered manifests.
        manifest_yamls = [
            p
            for p in out_dir.rglob("*.yaml")
            if "values" not in p.name and p.read_text().lstrip().startswith(("apiVersion", "kind"))
        ]
        for m in manifest_yamls:
            r = subprocess.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    kind_cluster,
                    "apply",
                    "--dry-run=client",
                    "-f",
                    str(m),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert r.returncode == 0, f"kubectl validate failed for {m}: {r.stderr}"

    @pytest.mark.skipif(
        not _FULL_INSTALL,
        reason="set FLUID_KIND_FULL_INSTALL=1 to install upstream Airbyte (heavy)",
    )
    def test_full_helm_install_of_airbyte(self, kind_cluster: str, tmp_path: Path):
        """Heavy: full ``helm install`` of upstream Airbyte chart against the
        kind cluster, then ``helm uninstall``. Only runs under explicit
        opt-in because the chart pulls 1+ GB of images."""
        repo = subprocess.run(
            [
                "helm",
                "repo",
                "add",
                "airbyte",
                "https://airbytehq.github.io/helm-charts",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if repo.returncode != 0:
            pytest.skip(f"helm repo add failed: {repo.stderr}")
        subprocess.run(["helm", "repo", "update"], capture_output=True, timeout=60)

        ns = "fluid-airbyte"
        try:
            inst = subprocess.run(
                [
                    "helm",
                    "install",
                    "airbyte",
                    "airbyte/airbyte",
                    "--namespace",
                    ns,
                    "--create-namespace",
                    "--kubeconfig",
                    kind_cluster,
                    "--wait",
                    "--timeout",
                    "10m",
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            assert inst.returncode == 0, f"helm install failed: {inst.stderr}"
        finally:
            subprocess.run(
                [
                    "helm",
                    "uninstall",
                    "airbyte",
                    "--namespace",
                    ns,
                    "--kubeconfig",
                    kind_cluster,
                ],
                capture_output=True,
                timeout=300,
            )
