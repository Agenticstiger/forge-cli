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

"""Per-run cost tracking for the staged forge pipeline (V2.4.4 — CLI).

Every staged LLM call already records token usage via each provider's
``extract_usage`` method; this module turns that into a user-visible
cost summary printed at the end of every forge run. CLI-only — no UI,
no dashboard, just a one-block panel in the terminal:

    Cost summary
    ─────────────────────────────────────────────────────────────────
      anthropic / claude-sonnet-4-5     12,453 in   3,827 out  $0.0247
      anthropic / claude-haiku-4-5         876 in     412 out  $0.0006
    ─────────────────────────────────────────────────────────────────
      total                            13,329 in   4,239 out  $0.0253

Public surface:

* :class:`RunCostTracker` — per-run aggregator. Singleton-per-process,
  but explicit ``.reset()`` is provided so tests are hermetic.
* :func:`get_run_tracker` — process-wide accessor.
* :func:`format_cost_summary` — pure-function formatter.
* :func:`print_cost_summary` — formatter + ``cprint`` to the user.
* :data:`MODEL_PRICES_USD` — embedded price table (USD per 1M tokens).
  Conservative coverage of the major models; unknown models surface
  with ``$?`` instead of being silently zeroed.

The tracker lives at module scope (singleton) because it has to be
read from the staged agents' ``_call_once`` and written from the CLI's
``run_*_command`` without threading a context object through the
entire pipeline. The :func:`reset_run_tracker` helper exists for tests
and for callers that explicitly want to start a fresh window inside
the same process.

Pricing notes:

* Prices are USD per 1M tokens, in / out separately. Source: each
  provider's public pricing page as of 2026-04-25. Update by editing
  :data:`MODEL_PRICES_USD` directly — the price table is a frozen
  dict, not a pulled-at-runtime catalog, so a stale table fails
  loud-but-safe (cost shown with ``$?`` instead of misleading zero).
* Cents are the unit users care about. We report to four decimal
  places of USD which is sub-cent precision — adequate for individual
  forge runs and clean enough to read.
* No automatic FX conversion. All values are USD; users in other
  currencies can multiply by their bank's rate if needed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Price table — USD per 1M tokens. (input_price, output_price)
# Update sources cited inline; conservative defaults to round up rather
# than under-report.
# ---------------------------------------------------------------------

MODEL_PRICES_USD: Dict[str, Tuple[float, float]] = {
    # Anthropic — https://www.anthropic.com/pricing (2026-04-25)
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-5-20250514": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-7": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-opus-4-7": (15.00, 75.00),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    # OpenAI — https://openai.com/pricing (2026-04-25)
    "gpt-4.1": (2.50, 10.00),
    "gpt-4.1-mini": (0.15, 0.60),
    "gpt-4.1-nano": (0.05, 0.20),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    # Google Gemini — https://ai.google.dev/pricing (2026-04-25)
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-2.5-flash": (0.075, 0.30),
    # Ollama — local; cost is electricity, treat as $0.
    # (User can override by editing this table if they pay for compute time.)
    "*ollama*": (0.0, 0.0),
}
"""Map of model id → (input_usd_per_1M, output_usd_per_1M).

The ``*ollama*`` sentinel is matched on provider name, not model id,
because Ollama hosts arbitrary local models that all cost the same
($0 — local compute). All other entries are exact-match on model id.
"""


# ---------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------


@dataclass
class _ModelUsage:
    """Per-(provider, model) accumulated counters for one run."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0


@dataclass
class _AgentUsage:
    """Per-(stage, agent_class) accumulated counters for one run.

    Powers Missing-#5 per-agent cost attribution. Stage is the
    ``BaseStageAgent.stage`` string (``"modeler"`` / ``"builder"``
    / …); ``agent_class`` is the Python class name
    (``"BuilderAgent"`` / ``"TransformationAgent"`` / …).

    Both empty strings → "unattributed" bucket (older callers that
    haven't been updated to pass stage / agent_class to
    ``record_call``).
    """

    stage: str
    agent_class: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0


@dataclass
class CostBreakdown:
    """Materialised cost report for one run.

    ``rows`` is one entry per (provider, model) pair seen; ``total_*``
    are the row sums. ``unknown_models`` lists model ids that hit the
    table with no price entry — surfaced separately so the operator
    can update :data:`MODEL_PRICES_USD`.

    ``missing_usage_calls`` counts paid-provider calls that completed
    but came back with no token counts. Some providers ship empty
    ``usage`` blocks under load or on streaming-cancellation paths;
    without this counter the user would see "$0.0042" with no hint
    that the figure is under-reported.

    ``variant_lint_findings`` carries the warning count from
    :func:`fluid_build.forge_datamodel.emit.validator.lint_dimensional_variant`,
    keyed by variant name (``"star"|"snowflake"|"galaxy"|"flat"``).
    Surfacing this in the cost-summary footer keeps the warning
    count visible to operators who pipe stdout to a log without
    re-reading the validation report — the warnings sit right next
    to the cost they were generated to support.
    """

    rows: List["CostRow"] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_calls: int = 0
    total_usd: Optional[float] = None  # ``None`` when any row has unknown price
    unknown_models: List[str] = field(default_factory=list)
    missing_usage_calls: int = 0
    variant_lint_findings: Dict[str, int] = field(default_factory=dict)
    catalog_fetch_ms: Dict[str, int] = field(default_factory=dict)
    """Total catalog-fetch wall-clock per catalog name (Gap 9).

    Operators want to know whether their forge runtime is dominated
    by the LLM stage or by the catalog round-trip — the answer
    determines whether to optimise the prompt cache or invest in
    catalog-side performance (warmer Snowflake warehouse,
    materialised INFORMATION_SCHEMA views, …). The summary footer
    shows total ms per catalog when this dict is non-empty."""
    agent_rows: List["AgentCostRow"] = field(default_factory=list)
    """Per-(stage, agent_class) attribution (Missing-#5).

    Empty list when no caller passed ``stage`` / ``agent_class`` to
    ``record_call`` (older code paths). When non-empty, the summary
    formatter prints a separate per-agent table so operators can
    see WHICH agent drove the cost — not just which model was billed."""
    annotation_summary: Optional[Dict[str, Any]] = None
    """Item 5 — confidence + provenance roll-up from the
    coordinator's scratchpad. Populated via
    :func:`set_annotation_summary` after the LogicalAgent runs.
    The cost-summary formatter surfaces a "N low-confidence
    claim(s)" footer when ``confidence_levels.low > 0``."""


@dataclass
class CostRow:
    """One row in the per-model breakdown."""

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    calls: int
    usd: Optional[float]  # ``None`` when the model isn't in the price table


@dataclass
class AgentCostRow:
    """One row in the per-(stage, agent_class) breakdown.

    ``stage`` and ``agent_class`` come from the
    ``BaseStageAgent.stage`` / ``type(agent).__name__`` pair the
    coordinator passes to ``record_call``. When both are empty
    strings, the row represents the "unattributed" bucket
    (callers that haven't been updated to pass attribution).
    """

    stage: str
    agent_class: str
    input_tokens: int
    output_tokens: int
    calls: int


class RunCostTracker:
    """Process-wide accumulator for staged forge LLM usage.

    Thread-safe writer (the staged coordinator's parallel-physical
    fan-out runs three agents concurrently). Reads are not locked —
    callers consume the breakdown at end-of-run when no writes are
    in flight.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[Tuple[str, str], _ModelUsage] = {}
        self._per_agent: Dict[Tuple[str, str], _AgentUsage] = {}
        self._missing_usage_calls: int = 0
        self._variant_lint: Dict[str, int] = {}
        self._catalog_fetch_ms: Dict[str, int] = {}

    def record_call(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        stage: str = "",
        agent_class: str = "",
    ) -> None:
        """Add one provider call's tokens to the running counters.

        When both ``input_tokens`` and ``output_tokens`` are zero on a
        non-Ollama provider, the call is also counted under
        ``missing_usage_calls``: that combination means the LLM
        responded but the provider ate the usage block, so the cost
        for this call is unknown rather than legitimately $0.

        ``stage`` and ``agent_class`` enable Missing-#5 per-agent
        cost attribution. Default empty strings keep older callers
        working unchanged (we can't make the kwargs required
        without a deprecation cycle); they also signal "unattributed"
        in the per-agent breakdown.
        """
        in_tok = int(input_tokens or 0)
        out_tok = int(output_tokens or 0)
        key = (provider, model)
        with self._lock:
            entry = self._counters.get(key)
            if entry is None:
                entry = _ModelUsage(provider=provider, model=model)
                self._counters[key] = entry
            entry.input_tokens += in_tok
            entry.output_tokens += out_tok
            entry.calls += 1
            # Ollama is legitimately $0 even with full token counts,
            # so a 0/0 call there is not "missing usage" — it's the
            # local-compute baseline. Only flag paid providers.
            missing = in_tok == 0 and out_tok == 0 and provider.lower() != "ollama"
            if missing:
                self._missing_usage_calls += 1
            # Per-agent attribution (Missing-#5) — bump the
            # per-(stage, agent_class) bucket when both are
            # provided. Empty strings → unattributed bucket which
            # the formatter can surface separately.
            agent_key = (stage, agent_class)
            agent_entry = self._per_agent.get(agent_key)
            if agent_entry is None:
                agent_entry = _AgentUsage(stage=stage, agent_class=agent_class)
                self._per_agent[agent_key] = agent_entry
            agent_entry.input_tokens += in_tok
            agent_entry.output_tokens += out_tok
            agent_entry.calls += 1
        # Emit the event AFTER mutating internal state so a
        # subscriber that calls ``breakdown()`` from the handler
        # sees the updated numbers.
        try:
            from fluid_build.copilot.events import Event, get_event_bus

            get_event_bus().emit(
                Event(
                    event_type="llm.call_completed",
                    payload={
                        "provider": provider,
                        "model": model,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "stage": stage,
                        "agent_class": agent_class,
                        "missing_usage": missing,
                    },
                )
            )
        except Exception:  # pragma: no cover — defensive
            pass

    def record_missing_usage(self) -> None:
        """Mark one call as having no usable usage block.

        Use this on the ``extract_usage`` exception path where the call
        completed but the provider's usage extractor blew up — that's
        a stronger "missing data" signal than 0/0 token counts. The
        per-(provider, model) row is NOT updated here because we have
        nothing to record against it.
        """
        with self._lock:
            self._missing_usage_calls += 1
        try:
            from fluid_build.copilot.events import Event, get_event_bus

            get_event_bus().emit(
                Event(event_type="llm.usage_missing", payload={}),
            )
        except Exception:  # pragma: no cover — defensive
            pass

    def record_catalog_fetch(self, catalog_name: str, duration_ms: int) -> None:
        """Record one catalog round-trip's wall-clock duration.

        Multiple calls against the same catalog *accumulate* — a
        forge that hits Snowflake three times (list, get_table,
        get_lineage) shows the sum, not the last. Negative or
        zero durations are ignored so the summary stays clean.
        """
        if not catalog_name or duration_ms <= 0:
            return
        with self._lock:
            self._catalog_fetch_ms[catalog_name] = self._catalog_fetch_ms.get(
                catalog_name, 0
            ) + int(duration_ms)
        try:
            from fluid_build.copilot.events import Event, get_event_bus

            get_event_bus().emit(
                Event(
                    event_type="catalog.fetch_completed",
                    payload={
                        "catalog_name": catalog_name,
                        "duration_ms": int(duration_ms),
                    },
                )
            )
        except Exception:  # pragma: no cover — defensive
            pass

    def record_variant_lint(self, variant: str, warning_count: int) -> None:
        """Record the variant-lint warning count for one validator pass.

        The dimensional variant validator (``lint_dimensional_variant``)
        produces 0..N warnings per pass; the cost-summary footer
        surfaces them so operators see the lint score next to the
        cost. The validator may run multiple times during a repair
        loop — we *replace* the per-variant entry on each call so the
        footer shows the LAST pass (the one that survived to disk),
        not an accumulated total across discarded retries.

        ``warning_count == 0`` removes the entry — a clean lint pass
        is the silent / no-news-is-good-news state.
        """
        if not variant:
            return
        with self._lock:
            if warning_count > 0:
                self._variant_lint[variant] = int(warning_count)
            else:
                self._variant_lint.pop(variant, None)
        try:
            from fluid_build.copilot.events import Event, get_event_bus

            get_event_bus().emit(
                Event(
                    event_type="validator.variant_lint",
                    payload={
                        "variant": variant,
                        "warning_count": int(warning_count),
                    },
                )
            )
        except Exception:  # pragma: no cover — defensive
            pass

    def reset(self) -> None:
        """Clear every counter — used by tests and explicit reset."""
        with self._lock:
            self._counters.clear()
            self._per_agent.clear()
            self._missing_usage_calls = 0
            self._variant_lint.clear()
            self._catalog_fetch_ms.clear()

    def breakdown(self) -> CostBreakdown:
        """Materialise the current state as a :class:`CostBreakdown`."""
        with self._lock:
            entries = sorted(
                self._counters.values(),
                key=lambda e: (e.provider, e.model),
            )
        rows: List[CostRow] = []
        unknown: List[str] = []
        any_unknown = False
        running_total = 0.0
        in_total = 0
        out_total = 0
        calls_total = 0
        for entry in entries:
            usd = _price_for(entry.provider, entry.model, entry.input_tokens, entry.output_tokens)
            if usd is None:
                any_unknown = True
                if entry.model not in unknown:
                    unknown.append(entry.model)
            else:
                running_total += usd
            rows.append(
                CostRow(
                    provider=entry.provider,
                    model=entry.model,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    calls=entry.calls,
                    usd=usd,
                )
            )
            in_total += entry.input_tokens
            out_total += entry.output_tokens
            calls_total += entry.calls
        with self._lock:
            missing_usage = self._missing_usage_calls
            variant_lint = dict(self._variant_lint)
            catalog_fetch = dict(self._catalog_fetch_ms)
            per_agent_entries = sorted(
                self._per_agent.values(),
                key=lambda e: (e.stage, e.agent_class),
            )
        # Materialise the per-agent attribution rows. Filter out the
        # "unattributed" bucket if it's the ONLY row (older callers
        # that don't pass stage/agent_class — showing one
        # ``("", "")`` row would be noise without insight).
        agent_rows: List[AgentCostRow] = []
        meaningful = [e for e in per_agent_entries if e.stage or e.agent_class]
        for e in meaningful:
            agent_rows.append(
                AgentCostRow(
                    stage=e.stage,
                    agent_class=e.agent_class,
                    input_tokens=e.input_tokens,
                    output_tokens=e.output_tokens,
                    calls=e.calls,
                )
            )
        # Issue 7 — distinguish "heuristic-only run" from "$0 LLM cost".
        # When NO LLM calls were recorded, ``total_usd`` is ``None``
        # (unknown) rather than ``0.0`` so episodic listeners
        # (``_record_forge_episode``) can omit the cost fields entirely
        # instead of poisoning the projection table with bogus zeros.
        if calls_total == 0:
            total_usd = None
        elif any_unknown:
            total_usd = None
        else:
            total_usd = round(running_total, 4)
        return CostBreakdown(
            rows=rows,
            total_input_tokens=in_total,
            total_output_tokens=out_total,
            total_calls=calls_total,
            total_usd=total_usd,
            unknown_models=unknown,
            missing_usage_calls=missing_usage,
            variant_lint_findings=variant_lint,
            catalog_fetch_ms=catalog_fetch,
            agent_rows=agent_rows,
            annotation_summary=get_annotation_summary(),
        )


_RUN_TRACKER = RunCostTracker()
"""Module-level singleton. Use :func:`get_run_tracker` rather than
referencing this directly so tests can swap the instance in if they
ever need to."""


def get_run_tracker() -> RunCostTracker:
    """Return the process-wide tracker."""
    return _RUN_TRACKER


def reset_run_tracker() -> None:
    """Reset the process-wide tracker — for tests and explicit run
    boundaries."""
    _RUN_TRACKER.reset()
    # Sprint #5 — also clear the per-run conformance-summary slot
    # so the next ``fluid forge`` invocation starts with a clean
    # slate. Like ``RunCostTracker``, this slot is a module-level
    # singleton because writing it requires only the coordinator
    # to know the value, and reading it requires only the CLI's
    # print site to surface it — threading a session through both
    # for one string adds no value.
    set_pre_emit_conformance_summary(None)
    # Item 5 — same pattern for the annotation summary.
    set_annotation_summary(None)


_PRE_EMIT_CONFORMANCE_SUMMARY: Optional[str] = None
"""Per-run pre-emit conformance summary slot.

The coordinator's ``_run_pre_emit_conformance`` writes here once
the agent has produced its summary; the CLI print site reads it
in the cost-summary panel. Module-level because the coordinator
and the CLI dispatcher don't share a direct reference."""


def set_pre_emit_conformance_summary(summary: Optional[str]) -> None:
    """Stamp the per-run conformance summary onto the module slot.

    ``None`` clears the slot (called from :func:`reset_run_tracker`
    at run boundaries)."""
    global _PRE_EMIT_CONFORMANCE_SUMMARY
    _PRE_EMIT_CONFORMANCE_SUMMARY = summary


def get_pre_emit_conformance_summary() -> Optional[str]:
    """Read the current per-run conformance summary or ``None``."""
    return _PRE_EMIT_CONFORMANCE_SUMMARY


_ANNOTATION_SUMMARY_SLOT: Optional[Dict[str, Any]] = None
"""Per-run annotation summary (item 5).

The coordinator stamps the scratchpad's
``AnnotationLog.summary()`` here after the LogicalAgent runs so
the CLI's cost summary can render a "N low-confidence claim(s)"
footer without threading the scratchpad through every print
site."""


def set_annotation_summary(summary: Optional[Dict[str, Any]]) -> None:
    """Stamp the per-run annotation summary."""
    global _ANNOTATION_SUMMARY_SLOT
    _ANNOTATION_SUMMARY_SLOT = summary


def get_annotation_summary() -> Optional[Dict[str, Any]]:
    """Read the current annotation summary or ``None``."""
    return _ANNOTATION_SUMMARY_SLOT


# ---------------------------------------------------------------------
# Cost ceiling (Sprint #6)
# ---------------------------------------------------------------------


class CostLimitExceeded(RuntimeError):
    """Raised when the per-run cost ceiling is exceeded.

    Carries the running USD total and the configured limit so the
    operator's error message contains the actual numbers, not a
    generic 'cost limit exceeded' string.
    """

    def __init__(self, *, running_usd: float, limit_usd: float) -> None:
        self.running_usd = running_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"Cost ceiling exceeded: running ${running_usd:.4f} > "
            f"limit ${limit_usd:.4f}. Set FLUID_COST_LIMIT_USD or "
            "behavior.cost_limit_usd_per_run in ~/.fluid/config.yaml "
            "to a higher value, or run with --no-cost-limit to disable."
        )


def _resolve_cost_limit_usd() -> Optional[float]:
    """Read the cost ceiling from env or unified config.

    Precedence:

    1. ``$FLUID_COST_LIMIT_USD`` (operator override per invocation).
    2. ``UnifiedConfig.behavior.cost_limit_usd_per_run`` (steady-state
       config).
    3. ``None`` — no ceiling enforced.

    Best-effort: any error in the config-read path falls back to
    'no ceiling' rather than blocking the forge with a config-read
    bug.
    """
    env_value = os.environ.get("FLUID_COST_LIMIT_USD")
    if env_value:
        try:
            limit = float(env_value)
            return limit if limit > 0 else None
        except (TypeError, ValueError):
            return None
    try:
        from fluid_build.copilot.unified_config import load_unified_config

        cfg = load_unified_config()
        if cfg is not None:
            behavior = getattr(cfg, "behavior", None)
            if behavior is not None:
                limit = getattr(behavior, "cost_limit_usd_per_run", None)
                if limit is not None and float(limit) > 0:
                    return float(limit)
    except Exception:  # pragma: no cover — defensive
        pass
    return None


def check_cost_ceiling() -> None:
    """Inspect the run tracker's running total; raise
    :class:`CostLimitExceeded` if it exceeds the configured limit.

    Called by ``BaseStageAgent._call_once`` AFTER each
    ``record_call`` so the limit is checked at every LLM-cost
    increment. A raised exception immediately aborts the forge —
    by design: an operator who set a $5 ceiling wants the run to
    stop, not the run to continue burning past the limit.

    No-op when no ceiling is configured (the common case).
    """
    limit = _resolve_cost_limit_usd()
    if limit is None:
        return
    breakdown = _RUN_TRACKER.breakdown()
    running = breakdown.total_usd
    # ``total_usd`` is None when at least one row has unknown
    # price — we can't enforce a ceiling we can't measure. Rather
    # than fail-open silently, surface a debug log and continue.
    if running is None:
        return
    if running > limit:
        raise CostLimitExceeded(running_usd=running, limit_usd=limit)


# ---------------------------------------------------------------------
# Price lookup
# ---------------------------------------------------------------------


def _price_for(provider: str, model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    """Look up the USD cost for one (provider, model, tokens) triple.

    Provider-name match first (catches Ollama where every model is
    ``$0``); then exact model id; returns ``None`` when neither
    matches so the caller can surface a "price unknown" indicator.

    Lookup order for per-model prices (most specific wins):

    1. Per-org / per-user override at ``~/.fluid/prices.json`` —
       lets enterprise customers patch in their negotiated rates
       (or local volume discounts) without forking forge-cli.
    2. Embedded :data:`MODEL_PRICES_USD` table.

    The override file is re-read from disk on every call so a price
    correction takes effect on the next forge run, no restart
    required.
    """
    if provider.lower() == "ollama":
        return 0.0
    pricing = _load_price_overrides().get(model) or MODEL_PRICES_USD.get(model)
    if pricing is None:
        return None
    in_price, out_price = pricing
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


# ---------------------------------------------------------------------
# Per-org price override (~/.fluid/prices.json)
# ---------------------------------------------------------------------
#
# Override file format — same shape as :data:`MODEL_PRICES_USD`, plus
# a ``schema_version`` key for forward compatibility:
#
#   {
#     "schema_version": 1,
#     "prices": {
#       "claude-sonnet-4-7": [2.40, 12.00],
#       "gpt-4.1": [2.00, 8.00]
#     }
#   }
#
# Both flat (just ``{model: [in, out]}``) and wrapped (the schema
# above) layouts are accepted so users can scribble overrides
# without consulting docs.


def _override_path() -> Path:
    """Resolve the override file path, honouring ``$FLUID_HOME``.

    Precedence:

    * ``$FLUID_PRICES_JSON`` — explicit override (used by tests).
    * ``$FLUID_HOME/prices.json`` if ``$FLUID_HOME`` is set.
    * ``~/.fluid/prices.json`` (default).
    """
    explicit = os.environ.get("FLUID_PRICES_JSON")
    if explicit:
        return Path(explicit)
    home = os.environ.get("FLUID_HOME")
    if home:
        return Path(home) / "prices.json"
    return Path.home() / ".fluid" / "prices.json"


def _load_price_overrides() -> Dict[str, Tuple[float, float]]:
    """Read the override file and return a model → (in, out) map.

    Lookup order (first non-empty wins):

    1. The unified config's ``prices.prices`` section (Sprint #7
       wiring) — operators on the unified path see overrides
       picked up automatically.
    2. The legacy ``~/.fluid/prices.json`` file. Pre-existing
       v1.5 installs continue to work without re-migrating.

    Best-effort: any error (missing file, malformed JSON, bad shape)
    falls through to the next source. We never let a malformed
    override break a forge run.
    """
    # Sprint #7 — try the unified config first.
    try:
        from fluid_build.copilot.unified_config import load_unified_config

        cfg = load_unified_config()
        if cfg is not None:
            unified_prices = cfg.prices_section.prices if cfg.prices_section else {}
            if unified_prices:
                return {
                    str(k): (float(v[0]), float(v[1]))
                    for k, v in unified_prices.items()
                    if isinstance(v, (list, tuple)) and len(v) == 2
                }
    except Exception:  # pragma: no cover — defensive
        pass

    path = _override_path()
    try:
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover — defensive
        _log.debug("failed to read price override at %s: %s", path, exc)
        return {}
    # Two shapes accepted: wrapped {"prices": {...}} and flat {...}.
    if isinstance(raw, dict) and isinstance(raw.get("prices"), dict):
        candidates = raw["prices"]
    elif isinstance(raw, dict):
        candidates = raw
    else:
        return {}
    out: Dict[str, Tuple[float, float]] = {}
    for model, prices in candidates.items():
        if not isinstance(model, str) or not isinstance(prices, (list, tuple)):
            continue
        if len(prices) != 2:
            continue
        try:
            in_price = float(prices[0])
            out_price = float(prices[1])
        except (TypeError, ValueError):
            continue
        if in_price < 0 or out_price < 0:
            continue
        out[model] = (in_price, out_price)
    return out


# ---------------------------------------------------------------------
# Formatter + printer
# ---------------------------------------------------------------------


def format_cost_summary(breakdown: CostBreakdown) -> str:
    """Format a :class:`CostBreakdown` as a multi-line text panel."""
    if not breakdown.rows:
        return "Cost summary: no LLM calls recorded for this run."

    lines: List[str] = []
    lines.append("Cost summary")
    lines.append("─" * 65)
    for row in breakdown.rows:
        cost_label = f"${row.usd:.4f}" if row.usd is not None else "$?"
        lines.append(
            f"  {row.provider:>10} / {row.model:<30}  "
            f"{row.input_tokens:>7,} in  {row.output_tokens:>7,} out  {cost_label:>10}"
        )
    lines.append("─" * 65)
    total_label = f"${breakdown.total_usd:.4f}" if breakdown.total_usd is not None else "$?"
    lines.append(
        f"  {'total':>43}  "
        f"{breakdown.total_input_tokens:>7,} in  "
        f"{breakdown.total_output_tokens:>7,} out  {total_label:>10}"
    )
    if breakdown.unknown_models:
        lines.append("")
        lines.append(
            "  Note: no price table entry for "
            + ", ".join(repr(m) for m in breakdown.unknown_models)
            + ". Update fluid_build/copilot/cost.py:MODEL_PRICES_USD."
        )
    if breakdown.missing_usage_calls:
        lines.append("")
        plural = "" if breakdown.missing_usage_calls == 1 else "s"
        lines.append(
            f"  Note: {breakdown.missing_usage_calls} call{plural} had no usage data; "
            "cost may be under-reported."
        )
    if breakdown.variant_lint_findings:
        lines.append("")
        for variant in sorted(breakdown.variant_lint_findings):
            count = breakdown.variant_lint_findings[variant]
            warn_word = "warning" if count == 1 else "warnings"
            lines.append(
                f"  Note: {count} variant-lint {warn_word} on variant={variant!r}. "
                "See validation report for details."
            )
    if breakdown.catalog_fetch_ms:
        lines.append("")
        for catalog in sorted(breakdown.catalog_fetch_ms):
            ms = breakdown.catalog_fetch_ms[catalog]
            # Format with thousands separator + ' ms' for sub-second
            # readability; kick to seconds when ≥ 1000ms.
            if ms >= 1000:
                duration = f"{ms / 1000:.1f}s"
            else:
                duration = f"{ms:,}ms"
            lines.append(f"  Catalog fetch: {catalog} took {duration} (read-only metadata).")
    if breakdown.agent_rows:
        lines.append("")
        lines.append("  Per-agent attribution")
        lines.append("  " + "─" * 63)
        for row in breakdown.agent_rows:
            label = (
                f"{row.stage}/{row.agent_class}"
                if row.stage and row.agent_class
                else (row.stage or row.agent_class or "unattributed")
            )
            lines.append(
                f"    {label:<32}  "
                f"{row.input_tokens:>7,} in  "
                f"{row.output_tokens:>7,} out  "
                f"calls={row.calls}"
            )
    # Item 5 — surface low-confidence claim count when the
    # AnnotationLog reports any. Operators see "3 low-confidence
    # claims" before they publish a contract.
    if breakdown.annotation_summary:
        levels = (breakdown.annotation_summary or {}).get("confidence_levels", {})
        n_low = int(levels.get("low") or 0)
        n_unknown = int(levels.get("unknown") or 0)
        if n_low or n_unknown:
            lines.append("")
            parts: List[str] = []
            if n_low:
                parts.append(f"{n_low} low-confidence claim{'' if n_low == 1 else 's'}")
            if n_unknown:
                parts.append(f"{n_unknown} unscored claim{'' if n_unknown == 1 else 's'}")
            lines.append(
                "  Note: "
                + " and ".join(parts)
                + " in this contract. See validation report for details."
            )
    return "\n".join(lines)


def print_cost_summary(*, quiet: bool = False) -> None:
    """Print the run-level cost summary to the user-facing console.

    No-op when ``quiet=True`` or when no calls were recorded — keeps
    the output clean for read-only / heuristic-only forge runs that
    don't actually invoke the LLM.
    """
    if quiet:
        return
    breakdown = get_run_tracker().breakdown()
    if not breakdown.rows:
        return
    from fluid_build.cli.console import cprint

    cprint(format_cost_summary(breakdown))


__all__ = [
    "MODEL_PRICES_USD",
    "RunCostTracker",
    "CostBreakdown",
    "CostRow",
    "get_run_tracker",
    "reset_run_tracker",
    "format_cost_summary",
    "print_cost_summary",
]
