# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Infrastructure layer — artifact generators for ``managed`` deployment mode.

Three back-ends:
- ``docker``     — emit ``docker-compose.yaml`` (local dev / CI)
- ``kubernetes`` — emit Helm values overlay + Helm release manifest (prod, hyperscaler-agnostic)
- ``opentofu``   — emit an OpenTofu ``.tf.json`` module wrapping the same Helm chart (audit-friendly)

The generators are pure functions of (contract, env) and produce
checksummed artifacts under ``.fluid/artifacts/<contract-id>/infra/``.
The ``generate-artifacts`` pipeline stage invokes them; ``apply`` then
shells out to ``docker compose`` / ``helm`` / ``tofu`` to execute.

**No hyperscaler SDK** is allowed in this package — Helm runs identically
on EKS / GKE / AKS / on-prem. Cloud-specific concerns (LoadBalancer type,
storage class, IAM bindings) live in cluster values, not here.
"""

from __future__ import annotations

from .base import (
    GENERATORS,
    ArtifactBundle,
    ArtifactGenerator,
    InfraStatus,
    InfraValidationResult,
    get_generator,
    register_generator,
)
from .charts import CHART_REGISTRY, ChartRef, get_chart, register_chart
from .docker import DockerComposeGenerator
from .kubernetes import HelmGenerator
from .opentofu import OpenTofuGenerator
from .values import build_values_overlay

__all__ = [
    "ArtifactBundle",
    "ArtifactGenerator",
    "InfraStatus",
    "InfraValidationResult",
    "DockerComposeGenerator",
    "HelmGenerator",
    "OpenTofuGenerator",
    "CHART_REGISTRY",
    "ChartRef",
    "GENERATORS",
    "register_generator",
    "register_chart",
    "get_generator",
    "get_chart",
    "build_values_overlay",
]


# Auto-register the built-in generators.
register_generator("docker", DockerComposeGenerator())
register_generator("kubernetes", HelmGenerator())
_opentofu_generator = OpenTofuGenerator()
register_generator("opentofu", _opentofu_generator)
# ``terraform`` stays a recognised target for back-compat: the contract
# schema still accepts it for products authored before the OpenTofu
# rename. Both keys resolve to the same generator.
register_generator("terraform", _opentofu_generator)


def assert_chart_in_sync(*, chart: str, declared_version: str, live_version: str) -> None:
    """Raise ``InfraDriftError`` when declared and live chart versions diverge.

    The verify stage / scheduled drift sweep calls this to convert a chart
    version mismatch into the typed catalog error so ops get a five-field
    Panel pointing at the next-step (``fluid plan`` then ``fluid apply``)
    instead of a flat log line.
    """
    if declared_version == live_version:
        return
    from fluid_build.cli._errors import InfraDriftError

    raise InfraDriftError.for_chart(
        chart=chart,
        declared=declared_version,
        live=live_version,
    )
