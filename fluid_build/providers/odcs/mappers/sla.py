# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SLA mapper: ODCS ``slaProperties[]`` ↔ FLUID expose ``qos``.

The full ODCS list is preserved verbatim under
``metadata.odcs_passthrough.sla_properties`` so that an ODCS → FLUID → ODCS
round-trip is lossless. The first/only expose's ``qos`` block is also
populated (availability, freshnessSLO, labels) so the FLUID view is
meaningful in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Dict, List

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)

# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    sla_props = ctx.odcs.get("slaProperties")
    if not isinstance(sla_props, list) or not sla_props:
        return

    # Verbatim pass-through (canonical source of truth on re-export)
    metadata_passthrough(ctx.fluid)["sla_properties"] = [
        dict(p) for p in sla_props if isinstance(p, Mapping)
    ]

    exposes = ctx.fluid.get("exposes") or []
    target = exposes[0] if exposes else None
    if not isinstance(target, dict):
        return

    qos: Dict[str, Any] = target.get("qos") or {}
    labels: Dict[str, Any] = qos.get("labels") or {}

    for prop in sla_props:
        if not isinstance(prop, Mapping):
            continue
        _apply_sla_to_qos(prop, qos, labels)

    if labels:
        qos["labels"] = labels
    if qos:
        target["qos"] = qos


def _apply_sla_to_qos(prop: Mapping[str, Any], qos: Dict[str, Any], labels: Dict[str, Any]) -> None:
    name = prop.get("property")
    value = prop.get("value")
    unit = prop.get("unit")
    if not name:
        return
    if name == "availability":
        try:
            fraction = float(value)
            qos["availability"] = f"{fraction * 100:g}%" if fraction <= 1 else f"{fraction:g}%"
        except (TypeError, ValueError):
            qos["availability"] = str(value)
    elif name in ("interval", "frequency", "latency", "retention"):
        qos.setdefault("freshnessSLO", str(value))
    elif name.startswith("label:"):
        labels[name.removeprefix("label:")] = value
    else:
        key = f"{name}:{unit}" if unit else name
        labels[key] = value


# ----- FLUID → ODCS --------------------------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    if not ctx.options.get("include_sla", True):
        return

    # Verbatim pass-through wins
    raw = get_metadata_passthrough(ctx.fluid).get("sla_properties")
    if isinstance(raw, list) and raw:
        ctx.odcs["slaProperties"] = [dict(p) for p in raw if isinstance(p, Mapping)]
        return

    properties = list(_build_sla_properties(ctx.fluid))
    if properties:
        ctx.odcs["slaProperties"] = properties


def _build_sla_properties(fluid: Mapping[str, Any]) -> Sequence[Dict[str, Any]]:
    sla_values: Dict[str, Any] = {}
    for expose in fluid.get("exposes", []):
        if not isinstance(expose, Mapping):
            continue
        qos = expose.get("qos") or {}
        if not isinstance(qos, Mapping):
            continue
        _qos_to_sla(qos, sla_values)

    metadata = fluid.get("metadata") or {}
    if isinstance(metadata, Mapping):
        if metadata.get("update_frequency") and "interval" not in sla_values:
            sla_values["interval"] = metadata["update_frequency"]
        if metadata.get("availability") and "availability" not in sla_values:
            try:
                a = float(metadata["availability"])
                sla_values["availability"] = _percent_to_fraction(a) if a > 1 else a
            except (TypeError, ValueError):
                pass
        if metadata.get("quality_threshold"):
            try:
                sla_values["completenessKpi"] = float(metadata["quality_threshold"])
            except (TypeError, ValueError):
                pass

    out: List[Dict[str, Any]] = []
    for prop, value in sla_values.items():
        if value is None:
            continue
        out.append({"property": prop, "value": value})
    return out


def _percent_to_fraction(value: float) -> float:
    """``99.9`` → ``0.999``, not ``0.9990000000000001``.

    Binary floating point cannot represent 99.9/100 exactly, and the artifact
    is very visible: a published SLA that reads ``availability:
    0.9990000000000001`` looks like a computed number nobody intended. Six
    decimal places is four more than any availability figure needs (99.9999%)
    and keeps the value a plain float for the JSON/YAML writers.
    """
    return round(value / 100, 6)


def _qos_to_sla(qos: Mapping[str, Any], sla_values: Dict[str, Any]) -> None:
    avail = qos.get("availability")
    if avail is not None and "availability" not in sla_values:
        try:
            s = str(avail).strip()
            if s.endswith("%"):
                sla_values["availability"] = _percent_to_fraction(float(s.rstrip("%")))
            else:
                parsed = float(s)
                sla_values["availability"] = (
                    _percent_to_fraction(parsed) if parsed > 1 else parsed
                )
        except (TypeError, ValueError):
            sla_values["availability"] = str(avail)

    freshness = qos.get("freshnessSLO") or qos.get("freshness_slo")
    if freshness and "interval" not in sla_values:
        sla_values["interval"] = str(freshness)

    labels = qos.get("labels") or {}
    if isinstance(labels, Mapping):
        for k, v in labels.items():
            sla_values.setdefault(f"label:{k}", v)
