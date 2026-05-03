# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenTofu / Terraform artifact generator for managed-mode acquisition.

Emits a minimal OpenTofu module that wraps the same Helm chart referenced
by the Kubernetes back-end, using the ``helm`` and ``kubernetes`` providers.
This is the audit-friendly path: review the plan via ``tofu plan`` before
applying.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import ArtifactBundle, ArtifactGenerator, InfraStatus, InfraValidationResult, make_file
from .charts import get_chart
from .values import build_values_overlay


@dataclass
class TerraformGenerator(ArtifactGenerator):
    target: str = "terraform"

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
            return ArtifactBundle.of("terraform", [], metadata={"engines": []})

        files = [
            make_file("main.tf", _emit_main_tf(modules)),
            make_file("variables.tf", _emit_variables_tf(modules)),
            make_file("versions.tf", _emit_versions_tf()),
        ]
        return ArtifactBundle.of("terraform", files, metadata={"engines": list(set(engines))})

    def validate(self, bundle: ArtifactBundle) -> InfraValidationResult:
        errors: List[str] = []
        # Heuristic validation: file must contain a `helm_release` resource and
        # required required_providers block.
        seen_helm_release = False
        seen_required_providers = False
        for f in bundle.files:
            if "helm_release" in f.content:
                seen_helm_release = True
            if "required_providers" in f.content:
                seen_required_providers = True
        if bundle.files and not seen_helm_release:
            errors.append("no helm_release resource emitted")
        if bundle.files and not seen_required_providers:
            errors.append("no required_providers block emitted")
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


# ── Terraform emission ────────────────────────────────────────────────


def _emit_versions_tf() -> str:
    return """terraform {
  required_version = ">= 1.5"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.13.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.27.0"
    }
  }
}
"""


def _emit_variables_tf(modules: List[Dict[str, Any]]) -> str:
    lines = []
    for m in modules:
        eng = _safe_id(m["engine"])
        lines.append(f'variable "{eng}_values" {{')
        lines.append(f"  description = \"Helm values overlay for {m['engine']}\"")
        lines.append("  type        = string")
        # Default to the generated values JSON, escaping it so it survives Terraform expansion.
        default_json = json.dumps(m["values"])
        # Embed default via heredoc so we do not have to escape quotes manually.
        lines.append(f"  default     = <<EOT\n{default_json}\nEOT")
        lines.append("}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _emit_main_tf(modules: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for m in modules:
        eng = _safe_id(m["engine"])
        lines.append(
            f'resource "kubernetes_namespace" "{eng}" {{\n'
            f"  metadata {{\n"
            f'    name = "{m["namespace"]}"\n'
            f"  }}\n"
            f"}}"
        )
        lines.append("")
        lines.append(
            f'resource "helm_release" "{eng}" {{\n'
            f'  name             = "forge-{m["engine"]}"\n'
            f"  namespace        = kubernetes_namespace.{eng}.metadata[0].name\n"
            f'  repository       = "{m["chart_repo"]}"\n'
            f'  chart            = "{m["chart_name"]}"\n'
            f'  version          = "{m["chart_version"]}"\n'
            f"  create_namespace = false\n"
            f"  values           = [var.{eng}_values]\n"
            f"}}"
        )
        lines.append("")
    return "\n".join(lines) + "\n"


def _safe_id(s: str) -> str:
    return s.replace("-", "_").replace(".", "_")
