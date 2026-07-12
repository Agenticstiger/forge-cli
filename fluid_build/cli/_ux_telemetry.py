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
* ``provider`` — which LLM provider authored the contract
  (anthropic/openai/gemini/local/…). Low-cardinality enum-like value —
  the analytics analogue of dbt's ``adapter_type``. Never a model id,
  endpoint, key, or any free-form string.
* ``run_completed`` — did the forge run reach a written contract? Emitted
  ``True`` on success and ``False`` on failure so an aggregator can
  compute a real **completion rate** (numerator / denominator) rather
  than a success-only sample. Mirrors dbt's "whether the invocation
  succeeded" and Next.js's ``*_COMPLETED`` events.
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

# Low-cardinality allowlist for the ``ux.provider`` attribute. Mirrors the
# ``fluid forge --llm-provider`` argparse choices plus the internal LiteLLM
# backend and the ``local`` fallback. Any value outside this set collapses to
# ``"other"`` so a mislabelled provenance value can never turn ``ux.provider``
# into a high-cardinality / free-form (potentially PII-bearing) attribute.
# Enum-like naming + bounded cardinality follows the OpenTelemetry
# semantic-convention guidance for span attributes.
_KNOWN_PROVIDERS = frozenset(
    {
        "openai",
        "anthropic",
        "claude",
        "gemini",
        "ollama",
        "mcp-sampling",
        "claude-code",
        "codex",
        "cursor",
        "kiro",
        "litellm",
        "local",
    }
)


def _normalize_provider(name: Any) -> str:
    """Collapse an arbitrary provider label to a bounded enum-like slug.

    Returns ``""`` for unset/blank input, the canonical slug for a known
    provider, and ``"other"`` for anything unrecognised — never the raw
    free-form string (privacy + low-cardinality guarantee).
    """
    if not name:
        return ""
    slug = str(name).strip().lower()
    if not slug:
        return ""
    return slug if slug in _KNOWN_PROVIDERS else "other"


@dataclass
class UXTelemetry:
    """One run's worth of UX measurements."""

    started_monotonic: float = field(default_factory=time.monotonic)
    first_panel_at: Optional[float] = None
    questions_asked: int = 0
    inferences_used: int = 0
    picker_choice: str = ""
    mode: str = "standard"  # standard / compose / refine / template / blank
    provider: str = ""  # anthropic / openai / gemini / local / … (enum-like)
    run_completed: bool = False
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

    def record_provider(self, name: Any) -> None:
        """Record the LLM provider that authored the contract.

        Normalised to a bounded enum-like slug (see
        :func:`_normalize_provider`) so ``ux.provider`` stays low-cardinality
        and can never carry a model id, endpoint, key, or other free-form
        value. A blank/unknown input leaves the previously recorded value
        untouched rather than clobbering it with ``""``.
        """
        slug = _normalize_provider(name)
        if slug:
            self.provider = slug

    def mark_completed(self, completed: bool = True) -> None:
        """Flag whether this forge run reached a written contract.

        Emitted on both the success and failure paths so an aggregator can
        compute a genuine completion *rate* (completed / total) instead of a
        success-only sample.
        """
        self.run_completed = bool(completed)

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
            "ux.provider": str(self.provider or ""),
            "ux.run_completed": bool(self.run_completed),
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

    Privacy gate: no-ops unless the user has explicitly opted in to
    telemetry (default OFF). ``DO_NOT_TRACK`` and ``FLUID_TELEMETRY=0``
    force this off regardless of any persisted choice — see
    :mod:`fluid_build.cli._telemetry_consent`.
    """
    try:
        from fluid_build.cli._telemetry_consent import telemetry_enabled

        if not telemetry_enabled():
            return
    except Exception:  # noqa: BLE001 — fail closed (no emit) on gate error
        return
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


def emit_forge_run_span(base_attrs: Optional[Dict[str, Any]] = None) -> None:
    """Open a fresh ``forge.invocation`` span carrying the current record.

    Used by forge exit paths that don't otherwise open the invocation span —
    principally the failure branches, so a ``run_completed=False`` run still
    lands and contributes to the completion-rate denominator.

    ``base_attrs`` (e.g. ``fluid.flow=forge``) are non-PII operational
    attributes and are always applied; the behavioural ``ux.*`` attributes are
    added only when the user has opted in (default OFF; ``DO_NOT_TRACK`` /
    ``FLUID_TELEMETRY=0`` force them off). Best-effort and a no-op when no OTel
    exporter is configured — telemetry must never block or crash a forge run.
    """
    attrs: Dict[str, Any] = dict(base_attrs or {})
    try:
        from fluid_build.cli._telemetry_consent import telemetry_enabled

        if telemetry_enabled():
            attrs.update(get_telemetry().to_span_attributes())
    except Exception:  # noqa: BLE001 — fail closed (no ux.* attrs) on gate error
        pass
    try:
        from fluid_build.observability.tracing import traced_span

        with traced_span("forge.invocation", attributes=attrs):
            pass
    except Exception as exc:  # noqa: BLE001
        LOG.debug("emit_forge_run_span_failed: %s", exc)


__all__ = [
    "UXTelemetry",
    "emit_forge_run_span",
    "emit_telemetry_to_active_span",
    "get_telemetry",
    "reset_telemetry",
]
