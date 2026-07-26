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
populated (availability, freshnessSLO, latencyP95, labels) so the FLUID view
is meaningful in isolation.

That second, *projected* view is where this mapper has to be careful. FLUID's
``qos`` fields are schema-constrained — ``availability`` must match
``$defs/availabilityPct``, ``freshnessSLO`` and ``latencyP95`` must match
``$defs/isoDuration``, and every ``labels`` value must be a string — while
ODCS ``slaProperties[].value`` is deliberately open (string | number |
integer | boolean | null) with the magnitude carried in a *separate* ``unit``
field. Projecting one onto the other with ``str(value)`` produced contracts
the importer's own validator rejected: ``{property: latency, value: 4, unit:
h}`` became ``freshnessSLO: '4'`` (unit dropped, not a duration) and
``{property: errorRate, value: 10, unit: count}`` became a non-string label.

The rule this module now follows is: **a value is only written into a
constrained ``qos`` field when it demonstrably satisfies that field's
constraint.** Anything else falls through to ``labels`` as a string, where it
is legal and still visible. Nothing is dropped either way — the verbatim
``slaProperties`` pass-through remains the lossless source of truth and is
what re-export replays.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)

# Copied verbatim from the FLUID schema so this module can check a value
# *before* writing it. Keeping the patterns here (rather than reaching into
# schema_manager) keeps the mapper pure and off the jsonschema import path,
# which the CLI startup budget forbids on ``--help``.
#   fluid-schema-0.7.x  $defs/isoDuration
_ISO_DURATION_RE = re.compile(r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$")
#   fluid-schema-0.7.x  $defs/availabilityPct
_AVAILABILITY_RE = re.compile(r"^(100(\.0+)?|\d{2}(\.\d+)?|\d{1}\d(\.\d+)?)%$")

# ODCS defers unit spelling to "the ISO standard" without enumerating it
# ("**d**, day, days for days; **y**, yr, years for years, etc." —
# odcs-schema-v3.1.0 $defs/ServiceLevelAgreementProperty). We accept the
# common spellings and deliberately REFUSE the ambiguous ones: a bare ``m`` is
# *minutes* in an ISO-8601 time part and *months* in the date part, and
# guessing either way silently rescales the SLA by a factor of ~44,000. An
# unmapped unit is not an error — the pair falls through to a label.
#
# Shape mirrors ``_DATEPART_TO_ISO`` in cli/import_workflow/dbt.py, the
# in-repo precedent for unit → ISO-8601 duration templating.
_UNIT_TO_ISO: Dict[str, str] = {
    "y": "P{n}Y", "yr": "P{n}Y", "yrs": "P{n}Y", "year": "P{n}Y", "years": "P{n}Y",
    "mo": "P{n}M", "mon": "P{n}M", "month": "P{n}M", "months": "P{n}M",
    "w": "P{n}W", "wk": "P{n}W", "wks": "P{n}W", "week": "P{n}W", "weeks": "P{n}W",
    "d": "P{n}D", "day": "P{n}D", "days": "P{n}D",
    "h": "PT{n}H", "hr": "PT{n}H", "hrs": "PT{n}H", "hour": "PT{n}H", "hours": "PT{n}H",
    "min": "PT{n}M", "mins": "PT{n}M", "minute": "PT{n}M", "minutes": "PT{n}M",
    "s": "PT{n}S", "sec": "PT{n}S", "secs": "PT{n}S", "second": "PT{n}S", "seconds": "PT{n}S",
}  # fmt: skip

# ODCS SLA property → the FLUID ``qos`` duration field it projects onto.
# ``retention`` is deliberately absent: it says how long data is *kept*, not
# how fresh it is, and FLUID ``qos`` has no field for it — so it belongs in a
# label, not in ``freshnessSLO``.
_DURATION_QOS_FIELD: Dict[str, str] = {
    "interval": "freshnessSLO",
    "frequency": "freshnessSLO",
    "latency": "latencyP95",
}

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

    # ``label:x`` is our own export spelling for a FLUID qos label — it is the
    # exact inverse of ``_qos_to_sla`` and never needs coercion beyond the
    # string-typing every FLUID label requires.
    if name.startswith("label:"):
        _set_label(labels, name.removeprefix("label:"), value)
        return

    if name == "availability":
        pct = _as_availability_pct(value)
        if pct is not None:
            qos.setdefault("availability", pct)
            return
        # Not expressible as $defs/availabilityPct — fall through to a label
        # rather than publish a value the FLUID validator rejects.
    else:
        field = _DURATION_QOS_FIELD.get(name)
        if field is not None and field not in qos:
            duration = _as_iso_duration(value, unit)
            if duration is not None:
                qos[field] = duration
                return
            # Not expressible as $defs/isoDuration — fall through to a label.

    _set_label(labels, f"{name}:{unit}" if unit else name, value)


def _set_label(labels: Dict[str, Any], key: str, value: Any) -> None:
    """Write one FLUID label, or nothing.

    ``$defs/labels`` is ``additionalProperties: {type: string}``, so an ODCS
    numeric/boolean SLA value has to be rendered. ``None`` is skipped rather
    than stringified into the literal ``"None"``; the verbatim
    ``slaProperties`` pass-through still carries it losslessly.
    """
    if value is None:
        return
    if isinstance(value, bool):
        labels[key] = "true" if value else "false"
    elif isinstance(value, str):
        labels[key] = value
    else:
        labels[key] = str(value)


def _as_iso_duration(value: Any, unit: Any) -> Optional[str]:
    """``4`` + ``h`` → ``PT4H``; ``"PT15M"`` → itself; otherwise ``None``.

    ``None`` means "not representable as an ISO-8601 duration", and the caller
    must then route the pair to a label. Returning a best-effort string here is
    precisely what produced ``freshnessSLO: '4'`` — an importer that reported
    success while writing a contract its own validator rejected.
    """
    # An already-formed duration round-trips verbatim. This is the path a
    # FLUID-native contract takes: qos.freshnessSLO ``PT15M`` exports as
    # ``{property: interval, value: PT15M}`` (no unit) and must import back
    # unchanged. Mirrors the ``fluid_window`` check in _recency_window().
    if isinstance(value, str) and _ISO_DURATION_RE.match(value.strip()):
        return value.strip()

    template = _UNIT_TO_ISO.get(str(unit).strip().lower()) if unit else None
    if template is None:
        return None
    # bool is an int subclass; ``True`` must not silently become ``PT1H``.
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # The schema's duration grammar is integer-only (``\d+``). A fractional or
    # negative SLA cannot be spelled in it and must not be silently rounded.
    if not math.isfinite(number) or number < 0 or number != int(number):
        return None
    return template.format(n=int(number))


def _as_availability_pct(value: Any) -> Optional[str]:
    """``99.5`` / ``0.995`` / ``"99.5%"`` → ``"99.5%"``; otherwise ``None``.

    Verified against ``$defs/availabilityPct`` before returning, so a value the
    pattern cannot express (``9.5`` — one leading digit — or a non-numeric
    string) becomes a label instead of an invalid ``qos.availability``.
    """
    if isinstance(value, bool) or value is None:
        return None
    text = value.strip() if isinstance(value, str) else value
    if isinstance(text, str) and _AVAILABILITY_RE.match(text):
        return text
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    # ODCS records availability as a fraction (0.999); humans and the FLUID
    # schema want a percentage. Values >1 are already percentages.
    rendered = f"{number * 100:g}%" if number <= 1 else f"{number:g}%"
    return rendered if _AVAILABILITY_RE.match(rendered) else None


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
                sla_values["availability"] = _percent_to_fraction(parsed) if parsed > 1 else parsed
        except (TypeError, ValueError):
            sla_values["availability"] = str(avail)

    freshness = qos.get("freshnessSLO") or qos.get("freshness_slo")
    if freshness and "interval" not in sla_values:
        sla_values["interval"] = str(freshness)

    # Inverse of the ``latency`` → ``latencyP95`` import mapping, so a
    # FLUID-native contract carrying latencyP95 survives FLUID → ODCS → FLUID.
    # Emitted without a ``unit``: the value is already an ISO-8601 duration,
    # which the importer recognises verbatim.
    latency = qos.get("latencyP95")
    if latency and "latency" not in sla_values:
        sla_values["latency"] = str(latency)

    labels = qos.get("labels") or {}
    if isinstance(labels, Mapping):
        for k, v in labels.items():
            sla_values.setdefault(f"label:{k}", v)
