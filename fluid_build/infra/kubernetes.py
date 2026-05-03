# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Helm artifact generator for managed-mode acquisition deployments.

Emits:
- ``release.yaml`` — pinned Helm release reference (chart repo, name, version,
  namespace).
- ``values.yaml``  — values overlay built from contract + sovereignty + secrets.
- ``external-secrets.yaml`` — ExternalSecret CRs for declared secret refs.
- ``network-policy.yaml`` — NetworkPolicy locking egress to declared hosts.

The apply layer shells out to ``helm install / upgrade`` with these
artifacts. We never bake values into the chart — they're an overlay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml

from .base import ArtifactBundle, ArtifactGenerator, InfraStatus, InfraValidationResult, make_file
from .charts import get_chart
from .values import (
    build_external_secrets,
    build_network_policy,
    build_values_overlay,
)


@dataclass
class HelmGenerator(ArtifactGenerator):
    target: str = "kubernetes"

    def generate(
        self,
        contract: Dict[str, Any],
        *,
        env: Optional[Dict[str, str]] = None,
    ) -> ArtifactBundle:
        builds = contract.get("builds") or []
        sovereignty = contract.get("sovereignty") or {}

        files: List[Any] = []
        engines: List[str] = []
        for build in builds:
            if build.get("pattern") != "acquisition":
                continue
            engine = (build.get("engine") or "").lower()
            if engine in ("duckdb", "dlt"):
                # Embedded-only engines have no managed-mode chart.
                continue
            props = build.get("properties") or {}
            engine_block = props.get(engine) or {}
            deployment = engine_block.get("deployment") or {}
            if deployment.get("mode") != "managed":
                continue
            managed = deployment.get("managed") or {}
            engines.append(engine)
            files.extend(_emit_for_engine(engine, build, managed, sovereignty))

        if not files:
            # No managed-mode acquisition builds; return an empty bundle so the
            # pipeline can still call `validate` without surprise.
            return ArtifactBundle.of("kubernetes", [], metadata={"engines": []})

        return ArtifactBundle.of("kubernetes", files, metadata={"engines": list(set(engines))})

    def validate(self, bundle: ArtifactBundle) -> InfraValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        # Each YAML must parse and required Helm-release fields must be present.
        for f in bundle.files:
            if not f.relative_path.endswith(("yaml", "yml")):
                continue
            try:
                docs = list(yaml.safe_load_all(f.content))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{f.relative_path}: invalid YAML: {exc}")
                continue
            for d in docs:
                if not isinstance(d, dict):
                    continue
                if d.get("kind") == "HelmRelease":
                    spec = d.get("spec") or {}
                    chart = (spec.get("chart") or {}).get("spec") or {}
                    for required in ("chart", "version", "sourceRef"):
                        if required not in chart:
                            errors.append(
                                f"{f.relative_path}: HelmRelease.spec.chart.spec.{required} missing"
                            )
        return InfraValidationResult(ok=not errors, errors=errors, warnings=warnings)

    def status(self, contract: Dict[str, Any]) -> InfraStatus:
        # Live status (Helm release exists? chart version drift?) lives in the
        # apply layer that talks to kubectl/helm. Generator doesn't probe.
        return InfraStatus(deployed=False, notes=["live status check belongs to apply layer"])


# ── Per-engine emission ────────────────────────────────────────────────


def _emit_for_engine(
    engine: str,
    build: Dict[str, Any],
    managed: Dict[str, Any],
    sovereignty: Dict[str, Any],
) -> List[Any]:
    chart_ref = managed.get("chart") or {}
    chart_pinned = get_chart(engine)
    chart_repo = chart_ref.get("repo") or (chart_pinned.repo if chart_pinned else None)
    chart_name = chart_ref.get("name") or (chart_pinned.name if chart_pinned else None)
    chart_version = chart_ref.get("version") or (chart_pinned.version if chart_pinned else None)
    namespace = managed.get("namespace") or (
        chart_pinned.namespace if chart_pinned else "forge-acquire"
    )
    profile = managed.get("profile", "small")

    user_values = managed.get("values_overlay") or {}
    values = build_values_overlay(
        engine, profile=profile, user_values=user_values, sovereignty=sovereignty
    )

    files: List[Any] = []
    # 1. Helm release manifest (Flux-style HelmRelease CR — works with FluxCD,
    #    or as a documentation artifact for plain `helm install`).
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2beta2",
        "kind": "HelmRelease",
        "metadata": {"name": f"forge-{engine}", "namespace": namespace},
        "spec": {
            "interval": "5m",
            "chart": {
                "spec": {
                    "chart": chart_name,
                    "version": chart_version,
                    "sourceRef": {
                        "kind": "HelmRepository",
                        "name": f"{engine}-repo",
                        "namespace": namespace,
                    },
                }
            },
            "values": values,
        },
    }
    repo_doc = {
        "apiVersion": "source.toolkit.fluxcd.io/v1beta2",
        "kind": "HelmRepository",
        "metadata": {"name": f"{engine}-repo", "namespace": namespace},
        "spec": {"interval": "10m", "url": chart_repo},
    }
    namespace_doc = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace},
    }
    release_yaml = yaml.safe_dump_all(
        [namespace_doc, repo_doc, release], sort_keys=True, default_flow_style=False
    )
    files.append(make_file(f"{engine}/release.yaml", release_yaml))

    # 2. Values overlay (separate file for `helm install -f values.yaml`).
    values_yaml = yaml.safe_dump(values, sort_keys=True, default_flow_style=False)
    files.append(make_file(f"{engine}/values.yaml", values_yaml))

    # 3. ExternalSecret manifests for declared secrets.
    secrets = managed.get("secrets") or []
    if secrets:
        es_docs = build_external_secrets(name_prefix=f"forge-{engine}", secrets=secrets)
        es_yaml = yaml.safe_dump_all(es_docs, sort_keys=True, default_flow_style=False)
        files.append(make_file(f"{engine}/external-secrets.yaml", es_yaml))

    # 4. NetworkPolicy from sovereignty + managed.network.
    network = managed.get("network") or {}
    egress = network.get("egressAllowList") or []
    if egress:
        np = build_network_policy(
            namespace=namespace, name=f"forge-{engine}-egress", allow_list=egress
        )
        np_yaml = yaml.safe_dump(np, sort_keys=True, default_flow_style=False)
        files.append(make_file(f"{engine}/network-policy.yaml", np_yaml))

    return files
