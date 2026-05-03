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

"""World-class UX telemetry (Phase 0.6 #9).

Captures the metrics that make UX decisions evidence-based:

* ``time_to_first_panel_ms`` — how long from CLI invocation to the
  first user-visible output. World-class is <100ms.
* ``questions_asked`` — count of interview prompts the user actually
  saw (vs. inferred + skipped). Lower is better.
* ``inferences_used`` — count of facets the system filled without
  asking. Higher is better — proof of detect-first.
* ``picker_choice`` — which mode the user picked (ai/blank/refine/
  template/from_product). Tells us which paths matter.
* ``mode`` — bootstrap mode (standard/compose/refine).
* ``preview_accept_rate`` — did the user hit Y or n at the preview?
  Y rate >90% = the contract matches the user's intent.
* ``schema_repair_attempts`` — how many self-healing rounds before a
  valid contract emerged. Lower is better.
* ``cost_usd`` / ``tokens`` / ``wall_clock_seconds`` — already on
  cost.json; surfaced here for at-a-glance correlation with the
  qualitative metrics.

Emitted on every successful or aborted forge run via the existing
OTel span (``forge.invocation``) — no new transport required.
Telemetry is a no-op when OTEL exporters aren't configured; it's
purely additive on the spans we already emit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


@dataclass
class UXTelemetry:
    """One run's worth of UX measurements."""

    started_monotonic: float = field(default_factory=time.monotonic)
    first_panel_at: Optional[float] = None
    questions_asked: int = 0
    inferences_used: int = 0
    picker_choice: str = ""
    mode: str = "standard"  # standard / compose / refine / template / blank
    preview_rendered: bool = False
    preview_accepted: bool = False
    schema_repair_attempts: int = 0
    refine_round_trips: int = 0
    welcome_scan_ms: int = 0
    extras: Dict[str, Any] = field(default_factory=dict)

    # ----- recording API ----------------------------------------------

    def mark_first_panel(self) -> None:
        if self.first_panel_at is None:
            self.first_panel_at = time.monotonic()

    def record_question(self) -> None:
        self.questions_asked += 1

    def record_inference(self, count: int = 1) -> None:
        self.inferences_used += max(0, int(count))

    def record_preview(self, *, accepted: bool) -> None:
        self.preview_rendered = True
        self.preview_accepted = bool(accepted)

    def record_repair(self) -> None:
        self.schema_repair_attempts += 1

    # ----- summary --------------------------------------------------

    @property
    def time_to_first_panel_ms(self) -> int:
        if self.first_panel_at is None:
            return 0
        return int((self.first_panel_at - self.started_monotonic) * 1000)

    def to_span_attributes(self) -> Dict[str, Any]:
        """Project into OTel-safe (str/int/float/bool) attributes."""
        attrs: Dict[str, Any] = {
            "ux.time_to_first_panel_ms": self.time_to_first_panel_ms,
            "ux.questions_asked": int(self.questions_asked),
            "ux.inferences_used": int(self.inferences_used),
            "ux.mode": str(self.mode or ""),
            "ux.picker_choice": str(self.picker_choice or ""),
            "ux.preview_rendered": bool(self.preview_rendered),
            "ux.preview_accepted": bool(self.preview_accepted),
            "ux.schema_repair_attempts": int(self.schema_repair_attempts),
            "ux.welcome_scan_ms": int(self.welcome_scan_ms),
            "ux.refine_round_trips": int(self.refine_round_trips),
        }
        for key, value in (self.extras or {}).items():
            if isinstance(value, (str, int, float, bool)):
                attrs[f"ux.extra.{key}"] = value
        return attrs


# ---------------------------------------------------------------------------
# Process-singleton accessor — one telemetry record per CLI invocation
# ---------------------------------------------------------------------------


_CURRENT: Optional[UXTelemetry] = None


def reset_telemetry() -> UXTelemetry:
    """Start a fresh telemetry record for this run."""
    global _CURRENT
    _CURRENT = UXTelemetry()
    return _CURRENT


def get_telemetry() -> UXTelemetry:
    """Return the in-flight telemetry record, creating one if needed."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = UXTelemetry()
    return _CURRENT


def emit_telemetry_to_active_span() -> None:
    """Write the current record onto the active OTel span as ux.* attrs.

    Best-effort — never blocks the run on a missing tracer or a
    misbehaving exporter. Idempotent: calling twice writes the
    most-recent values.
    """
    try:
        from opentelemetry import trace as _otel_trace
    except Exception:  # noqa: BLE001
        return
    span = _otel_trace.get_current_span()
    if not span or not span.is_recording():  # type: ignore[attr-defined]
        return
    record = get_telemetry()
    for key, value in record.to_span_attributes().items():
        try:
            span.set_attribute(key, value)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("ux_telemetry_attribute_failed: %s=%s — %s", key, value, exc)


__all__ = [
    "UXTelemetry",
    "emit_telemetry_to_active_span",
    "get_telemetry",
    "reset_telemetry",
]
