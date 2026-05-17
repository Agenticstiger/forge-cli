# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenTofu artifact generator for managed-mode acquisition.

Emits a single ``main.tf.json`` module that wraps the same Helm chart
referenced by the Kubernetes back-end, using the ``helm`` and
``kubernetes`` providers. ``.tf.json`` is OpenTofu's native JSON
configuration syntax — machine-generated with the standard library, with
no fragile HCL string-templating. Review the plan via ``tofu plan``
before applying.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import ArtifactBundle, ArtifactGenerator, InfraStatus, InfraValidationResult, make_file
from .charts import get_chart
from .values import build_values_overlay

# Provider + engine version pins for the emitted OpenTofu module.
_REQUIRED_TOFU_VERSION = ">= 1.6"
_REQUIRED_PROVIDERS = {
    "helm": {"source": "hashicorp/helm", "version": ">= 2.13.0"},
    "kubernetes": {"source": "hashicorp/kubernetes", "version": ">= 2.27.0"},
}


@dataclass
class OpenTofuGenerator(ArtifactGenerator):
    target: str = "opentofu"

    def generate(
        self,
        contract: Dict[str, Any],
        *,
        env: Optional[Dict[str, str]] = None,
    ) -> ArtifactBundle:
        builds = contract.get("builds") or []
        sovereignty = contract.get("sovereignty") or {}
        modules: List[Dict[str, Any]] = []
        engines: List[str] = []

        for build in builds:
            if build.get("pattern") != "acquisition":
                continue
            engine = (build.get("engine") or "").lower()
            if engine in ("duckdb", "dlt"):
                continue
            props = build.get("properties") or {}
            engine_block = props.get(engine) or {}
            deployment = engine_block.get("deployment") or {}
            if deployment.get("mode") != "managed":
                continue
            managed = deployment.get("managed") or {}
            engines.append(engine)
            modules.append(_module_spec(engine, managed, sovereignty))

        if not modules:
            return ArtifactBundle.of("opentofu", [], metadata={"engines": []})

        files = [make_file("main.tf.json", _emit_tofu_json(modules))]
        return ArtifactBundle.of("opentofu", files, metadata={"engines": sorted(set(engines))})

    def validate(self, bundle: ArtifactBundle) -> InfraValidationResult:
        errors: List[str] = []
        for f in bundle.files:
            try:
                doc = json.loads(f.content)
            except json.JSONDecodeError as exc:
                errors.append(f"{f.relative_path}: invalid JSON ({exc})")
                continue
            resources = doc.get("resource") or {}
            if "helm_release" not in resources:
                errors.append(f"{f.relative_path}: no helm_release resource emitted")
            if "required_providers" not in (doc.get("terraform") or {}):
                errors.append(f"{f.relative_path}: no required_providers block emitted")
        return InfraValidationResult(ok=not errors, errors=errors)

    def status(self, contract: Dict[str, Any]) -> InfraStatus:
        return InfraStatus(deployed=False, notes=["tofu plan/apply belongs to apply layer"])


# ── Module spec ───────────────────────────────────────────────────────


def _module_spec(
    engine: str, managed: Dict[str, Any], sovereignty: Dict[str, Any]
) -> Dict[str, Any]:
    chart_ref = managed.get("chart") or {}
    chart_pinned = get_chart(engine)
    return {
        "engine": engine,
        "namespace": managed.get("namespace")
        or (chart_pinned.namespace if chart_pinned else "forge-acquire"),
        "chart_repo": chart_ref.get("repo") or (chart_pinned.repo if chart_pinned else ""),
        "chart_name": chart_ref.get("name") or (chart_pinned.name if chart_pinned else engine),
        "chart_version": chart_ref.get("version")
        or (chart_pinned.version if chart_pinned else "0.0.0"),
        "values": build_values_overlay(
            engine,
            profile=managed.get("profile", "small"),
            user_values=managed.get("values_overlay") or {},
            sovereignty=sovereignty,
        ),
    }


# ── OpenTofu JSON emission ────────────────────────────────────────────


def _emit_tofu_json(modules: List[Dict[str, Any]]) -> str:
    """Render the modules as a canonical OpenTofu ``.tf.json`` document.

    Canonical (``sort_keys``) so the output is byte-stable across runs —
    reviewable diffs and a hashable artifact.
    """
    return json.dumps(_build_tofu_doc(modules), indent=2, sort_keys=True) + "\n"


def _build_tofu_doc(modules: List[Dict[str, Any]]) -> Dict[str, Any]:
    namespaces: Dict[str, Any] = {}
    releases: Dict[str, Any] = {}
    variables: Dict[str, Any] = {}

    for m in modules:
        eng = _safe_id(m["engine"])
        namespaces[eng] = {"metadata": {"name": m["namespace"]}}
        releases[eng] = {
            "name": f"forge-{m['engine']}",
            "namespace": f"${{kubernetes_namespace_v1.{eng}.metadata[0].name}}",
            "repository": m["chart_repo"],
            "chart": m["chart_name"],
            "version": m["chart_version"],
            "create_namespace": False,
            "values": [f"${{var.{eng}_values}}"],
        }
        variables[f"{eng}_values"] = {
            "description": f"Helm values overlay for {m['engine']}",
            "type": "string",
            # A native JSON string value — no HCL heredoc, no quote escaping.
            "default": json.dumps(m["values"], sort_keys=True),
        }

    return {
        "terraform": {
            "required_version": _REQUIRED_TOFU_VERSION,
            "required_providers": _REQUIRED_PROVIDERS,
        },
        "variable": variables,
        "resource": {
            "kubernetes_namespace_v1": namespaces,
            "helm_release": releases,
        },
    }


def _safe_id(s: str) -> str:
    return s.replace("-", "_").replace(".", "_")
