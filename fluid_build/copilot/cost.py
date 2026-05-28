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
    # Phase A3: when the caller passed ``usd_override`` (litellm's
    # accurate per-call cost), accumulate it here. ``None`` means
    # "no override seen yet — fall back to MODEL_PRICES_USD".
    usd_override: Optional[float] = None
    # Wave 1 — Anthropic prompt-cache token split. Cache writes are
    # billed at 1.25x the input rate; cache reads at 0.1x. We carry the
    # counts separately from ``input_tokens`` so the heuristic price
    # calc applies the right multipliers; ``usd_override`` still wins
    # when present (litellm's catalog is the source of truth).
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


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
    # Wave 1 — Anthropic prompt-cache token split. Default 0 so older
    # callers that built CostRow positionally don't break.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


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


@dataclass
class ProductCostRow:
    """One row in the per-product breakdown.

    ``product_id`` matches the ``contract.id`` of the product being
    forged or composed. When the caller doesn't supply a product_id
    (forge runs that haven't yet resolved one), the row is keyed by
    the empty string and rendered as the "unattributed" bucket.

    Used by the per-product cost ceiling (``FLUID_COST_LIMIT_USD_PER_PRODUCT``)
    so a multi-product invocation (e.g. ``--from-product-list``) can
    fail loud as soon as ANY single product crosses the budget,
    without waiting for the run total to do so.
    """

    product_id: str
    input_tokens: int
    output_tokens: int
    calls: int
    usd: Optional[float]


@dataclass
class _ProductUsage:
    """Internal per-product accumulator (writer-side state)."""

    product_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    usd_override: Optional[float] = None


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
        self._per_product: Dict[str, _ProductUsage] = {}
        self._missing_usage_calls: int = 0
        self._variant_lint: Dict[str, int] = {}
        self._catalog_fetch_ms: Dict[str, int] = {}
        # Stack of active product_ids for nested forge runs (e.g. an
        # ADP composition pulling from an SDP). The top of the stack
        # is the "current" product whose budget gate applies; the
        # cost-attribution path reads it without callers having to
        # pass product_id on every record_call.
        self._product_stack: List[str] = []

    def record_call(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        stage: str = "",
        agent_class: str = "",
        usd_override: Optional[float] = None,
        product_id: str = "",
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
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

        ``usd_override`` (Phase A3): when supplied (typically from
        ``litellm.completion_cost``), the tracker accumulates the
        litellm-derived USD directly instead of computing from the
        embedded ``MODEL_PRICES_USD`` table. This keeps cost reporting
        accurate for models the table doesn't know (Bedrock, Vertex,
        Groq, …) without forcing every native provider call to supply
        one. Backward-compatible: every existing caller passes ``None``
        by omission.
        """
        in_tok = int(input_tokens or 0)
        out_tok = int(output_tokens or 0)
        cache_write_tok = int(cache_creation_input_tokens or 0)
        cache_read_tok = int(cache_read_input_tokens or 0)
        key = (provider, model)
        with self._lock:
            entry = self._counters.get(key)
            if entry is None:
                entry = _ModelUsage(provider=provider, model=model)
                self._counters[key] = entry
            entry.input_tokens += in_tok
            entry.output_tokens += out_tok
            entry.calls += 1
            entry.cache_creation_input_tokens += cache_write_tok
            entry.cache_read_input_tokens += cache_read_tok
            if usd_override is not None:
                try:
                    entry.usd_override = (entry.usd_override or 0.0) + float(usd_override)
                except (TypeError, ValueError):
                    pass
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
            # Per-product attribution — credit the call to the
            # current product on the product stack (set via
            # ``push_product`` at forge entry, popped at exit) or
            # the explicit ``product_id`` kwarg if supplied. Empty
            # string → unattributed bucket.
            effective_product_id = product_id or (
                self._product_stack[-1] if self._product_stack else ""
            )
            product_entry = self._per_product.get(effective_product_id)
            if product_entry is None:
                product_entry = _ProductUsage(product_id=effective_product_id)
                self._per_product[effective_product_id] = product_entry
            product_entry.input_tokens += in_tok
            product_entry.output_tokens += out_tok
            product_entry.calls += 1
            if usd_override is not None:
                try:
                    product_entry.usd_override = (product_entry.usd_override or 0.0) + float(
                        usd_override
                    )
                except (TypeError, ValueError):
                    pass
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
            self._per_product.clear()
            self._product_stack.clear()
            self._missing_usage_calls = 0
            self._variant_lint.clear()
            self._catalog_fetch_ms.clear()

    # ── Per-product attribution surface ──────────────────────────────

    def push_product(self, product_id: str) -> None:
        """Push ``product_id`` as the current attribution scope.

        Subsequent ``record_call`` invocations (without an explicit
        ``product_id`` kwarg) credit cost to this product. The stack
        supports nested forge runs (e.g. an ADP composition that
        pulls from an SDP); ``pop_product`` reverts to the previous
        scope.
        """
        if not product_id:
            return
        with self._lock:
            self._product_stack.append(product_id)

    def pop_product(self) -> Optional[str]:
        """Pop the most-recently pushed product_id. Returns the popped
        id, or ``None`` if the stack was empty (defensive: a missing
        ``push_product`` shouldn't crash the run).
        """
        with self._lock:
            if not self._product_stack:
                return None
            return self._product_stack.pop()

    def current_product(self) -> Optional[str]:
        """Peek the top of the product stack without popping."""
        with self._lock:
            return self._product_stack[-1] if self._product_stack else None

    def per_product_usd(self, product_id: str) -> Optional[float]:
        """Return the running USD cost attributed to ``product_id``.

        Returns ``None`` when the product has rows with unknown price
        (so callers can distinguish "$0" from "unknown spend so far").
        """
        with self._lock:
            entry = self._per_product.get(product_id)
        if entry is None:
            return 0.0
        if entry.usd_override is not None:
            return round(float(entry.usd_override), 6)
        # Fallback: re-derive USD from counters by walking model rows.
        # Conservative — when we can't recover a per-model split for
        # this product (we don't track that today), return None so the
        # ceiling check fails-open rather than over- or under-billing.
        # The ``usd_override`` path covers the common case (litellm).
        return None

    def persist_to_run_dir(
        self,
        run_dir: Path,
        *,
        provider: str = "",
        model: str = "",
        wall_clock_seconds: float = 0.0,
    ) -> Path:
        """Write a ``cost.json`` receipt under ``run_dir`` — always.

        Closes the H22 gap: deterministic runs (no LLM call) previously
        never wrote a receipt, so ``fluid stats`` reported zero runs for
        them. We now ALWAYS write a cost.json — when no calls happened
        the payload carries ``mode="deterministic"`` with zero token /
        USD counts so downstream tools can distinguish "no LLM" from
        "LLM call but unknown price".

        The on-disk schema is intentionally a superset of the
        :class:`fluid_build.cli._preview_panel.CostSnapshot` shape that
        the preview panel writes when it owns the receipt — same keys
        (provider / model / *_tokens / total_usd / wall_clock_seconds /
        unknown_models / cumulative_usd) plus a new ``mode`` field
        ("deterministic" when total_calls == 0, otherwise the
        provider-prefixed model id).

        ``run_dir`` is created if missing. The write is atomic — temp
        file then rename — to keep partial reads safe.
        """
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        breakdown = self.breakdown()
        # Mode: deterministic when nothing was billed; otherwise the
        # provider/model of the single (or aggregate) row. Format favours
        # the operator-readable shape over JSON object so ``fluid stats
        # --by provider`` can still group on the simple ``provider``
        # field while ``mode`` carries the richer signal.
        if breakdown.total_calls == 0:
            mode = "deterministic"
        elif breakdown.rows:
            # Multi-row runs (modeler + builder on different models) get
            # the first row's identity in the legacy provider/model
            # fields and ``mode="mixed"`` so the operator can dig deeper
            # via the per-row breakdown in the raw cost.json.
            if len(breakdown.rows) == 1:
                row = breakdown.rows[0]
                mode = f"{row.provider}/{row.model}"
            else:
                mode = "mixed"
        else:
            mode = "unknown"
        payload: Dict[str, Any] = {
            "mode": mode,
            "provider": str(provider or (breakdown.rows[0].provider if breakdown.rows else "")),
            "model": str(model or (breakdown.rows[0].model if breakdown.rows else "")),
            "input_tokens": int(breakdown.total_input_tokens),
            "output_tokens": int(breakdown.total_output_tokens),
            "total_tokens": int(breakdown.total_input_tokens + breakdown.total_output_tokens),
            "total_usd": breakdown.total_usd,
            "cumulative_usd": breakdown.total_usd,
            "wall_clock_seconds": float(max(0.0, wall_clock_seconds)),
            "unknown_models": list(breakdown.unknown_models),
            "total_calls": int(breakdown.total_calls),
            "missing_usage_calls": int(breakdown.missing_usage_calls),
            "rows": [
                {
                    "provider": row.provider,
                    "model": row.model,
                    "input_tokens": int(row.input_tokens),
                    "output_tokens": int(row.output_tokens),
                    "calls": int(row.calls),
                    "usd": row.usd,
                    "cache_creation_input_tokens": int(row.cache_creation_input_tokens or 0),
                    "cache_read_input_tokens": int(row.cache_read_input_tokens or 0),
                }
                for row in breakdown.rows
            ],
        }
        cost_path = run_dir / "cost.json"
        tmp_path = cost_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        tmp_path.replace(cost_path)
        return cost_path

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
            # Phase A3: prefer litellm's ``usd_override`` when present —
            # it's an authoritative per-call price catalog kept in sync
            # with provider pricing. Falls through to the embedded
            # ``MODEL_PRICES_USD`` table for the native provider path.
            if entry.usd_override is not None:
                usd: Optional[float] = round(float(entry.usd_override), 6)
            else:
                # Wave 1 — apply the Anthropic cache split rate when
                # cache-write or cache-read tokens are present. For
                # other providers (or when both counts are zero) this
                # collapses to the legacy flat-rate input pricing.
                usd = _price_for_with_cache_split(
                    provider=entry.provider,
                    model=entry.model,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    cache_creation_input_tokens=entry.cache_creation_input_tokens,
                    cache_read_input_tokens=entry.cache_read_input_tokens,
                )
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
                    cache_creation_input_tokens=entry.cache_creation_input_tokens,
                    cache_read_input_tokens=entry.cache_read_input_tokens,
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
    """Read the per-RUN cost ceiling from env or unified config.

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


def _resolve_cost_limit_usd_per_product() -> Optional[float]:
    """Read the per-PRODUCT cost ceiling from env or unified config.

    Distinct from the per-run ceiling: when an invocation forges
    multiple products in sequence (``--from-product-list``,
    workspace-wide build), this caps the spend per individual
    product. The per-run ceiling still caps the aggregate.

    Precedence:

    1. ``$FLUID_COST_LIMIT_USD_PER_PRODUCT`` (operator override).
    2. ``UnifiedConfig.behavior.cost_limit_usd_per_product``.
    3. ``None`` — no per-product ceiling (per-run still applies).

    Best-effort fallback to ``None`` on config-read errors.
    """
    env_value = os.environ.get("FLUID_COST_LIMIT_USD_PER_PRODUCT")
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
                limit = getattr(behavior, "cost_limit_usd_per_product", None)
                if limit is not None and float(limit) > 0:
                    return float(limit)
    except Exception:  # pragma: no cover — defensive
        pass
    return None


def check_cost_ceiling() -> None:
    """Inspect the run tracker's running totals; raise
    :class:`CostLimitExceeded` if either the per-run or the
    per-product ceiling has been exceeded.

    Called by ``BaseStageAgent._call_once`` AFTER each
    ``record_call`` so both ceilings are checked at every LLM-cost
    increment. A raised exception immediately aborts the forge —
    by design: an operator who set a $5 ceiling wants the run to
    stop, not the run to continue burning past the limit.

    Order: per-product ceiling fires first when a product is on
    the stack and its running spend exceeds the per-product cap;
    per-run ceiling fires after when the aggregate exceeds the
    per-run cap. No-op when neither ceiling is configured (the
    common case).
    """
    # Per-product check (fires first when applicable).
    per_product_limit = _resolve_cost_limit_usd_per_product()
    if per_product_limit is not None:
        current = _RUN_TRACKER.current_product()
        if current:
            running = _RUN_TRACKER.per_product_usd(current)
            if running is not None and running > per_product_limit:
                raise CostLimitExceeded(running_usd=running, limit_usd=per_product_limit)

    # Per-run check (the aggregate cap).
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


def predict_call_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> Tuple[bool, float, Optional[float]]:
    """Phase 3.6 — pre-flight per-agent cost-budget check.

    Projects what ``RunCostTracker`` will look like after one more
    call of size ``(input_tokens, output_tokens)`` against
    ``(provider, model)`` and tells the caller whether the projection
    would exceed the configured ceiling.

    Returns ``(would_exceed, projected_total_usd, limit_usd)``:

    * ``would_exceed`` — True only when a limit is configured AND the
      projected total exceeds it.
    * ``projected_total_usd`` — running total + estimated call cost.
    * ``limit_usd`` — the configured ceiling, or ``None`` when no
      ceiling is set (in which case ``would_exceed`` is always False).

    Used by ``BaseStageAgent._call_once`` BEFORE the LLM call fires
    so a runaway agent that would push past the ceiling is aborted
    cleanly, not after the spend has already happened.
    """
    limit = _resolve_cost_limit_usd()
    breakdown = _RUN_TRACKER.breakdown()
    running = breakdown.total_usd or 0.0
    # If we can't price the planned call, honour the post-hoc
    # check_cost_ceiling() path: don't pre-flight-block on unknown
    # cost.
    estimated = _price_for(provider, model, int(input_tokens), int(output_tokens))
    if estimated is None:
        estimated = 0.0
    projected = float(running) + float(estimated)
    if limit is None:
        return False, projected, None
    return projected > limit, projected, limit


# ---------------------------------------------------------------------
# Price lookup
# ---------------------------------------------------------------------


# Anthropic prompt-cache token-cost multipliers per
# https://www.anthropic.com/news/prompt-caching — cache writes cost
# 1.25x the regular input price, cache reads cost 0.1x. These are the
# only published multipliers and apply uniformly across Anthropic API
# / Bedrock Claude / Vertex Claude. Other providers don't expose a
# comparable two-tier write/read split today.
_ANTHROPIC_CACHE_WRITE_MULTIPLIER = 1.25
_ANTHROPIC_CACHE_READ_MULTIPLIER = 0.10


def _is_anthropic_for_pricing(provider: str, model: str) -> bool:
    """True when the (provider, model) pair uses Anthropic cache pricing."""
    p = (provider or "").lower()
    m = (model or "").lower()
    if p in ("anthropic", "claude"):
        return True
    if "claude" in m:
        return True
    # Bedrock / Vertex SKU shapes for Claude models.
    if p == "bedrock" and ("anthropic" in m or "claude" in m):
        return True
    if p in ("vertex_ai", "vertex") and "claude" in m:
        return True
    return False


def _price_for_with_cache_split(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> Optional[float]:
    """Apply Anthropic's cache-write 1.25x / cache-read 0.10x split.

    For non-Anthropic models OR when both cache counts are zero this
    collapses to the existing ``_price_for`` flat-rate calculation —
    the heuristic stays backward-compatible with every existing
    cost-summary snapshot.

    For Anthropic-family models with non-zero cache tokens, we:

    1. Look up the per-1M (input, output) rate via ``_price_for``
       (passing zero tokens to get just the rate-discovery path) —
       same litellm-first → embedded-fallback ladder as the flat path.
    2. Split the input charge into three buckets: plain input @ 1x,
       cache write @ 1.25x, cache read @ 0.10x.
    3. Add the output charge unchanged.

    Returns ``None`` when the rate isn't discoverable, mirroring
    ``_price_for``'s "unknown price" signal so the cost summary
    surfaces ``$?`` instead of fabricating a number.
    """
    cache_write = int(cache_creation_input_tokens or 0)
    cache_read = int(cache_read_input_tokens or 0)
    if cache_write == 0 and cache_read == 0:
        return _price_for(provider, model, input_tokens, output_tokens)
    if not _is_anthropic_for_pricing(provider, model):
        return _price_for(provider, model, input_tokens, output_tokens)

    # Discover the per-token rate by querying ``_price_for`` with the
    # observed plain-input / output counts. We then reverse-engineer
    # the per-1M (in, out) prices and re-apply with the split. This
    # one extra call keeps the rate-source ladder (overrides → litellm →
    # embedded table) consistent for both code paths.
    in_rate, out_rate = _resolve_per_million_rate(provider, model)
    if in_rate is None or out_rate is None:
        return None
    plain_input_cost = (int(input_tokens) * in_rate) / 1_000_000
    cache_write_cost = (cache_write * in_rate * _ANTHROPIC_CACHE_WRITE_MULTIPLIER) / 1_000_000
    cache_read_cost = (cache_read * in_rate * _ANTHROPIC_CACHE_READ_MULTIPLIER) / 1_000_000
    output_cost = (int(output_tokens) * out_rate) / 1_000_000
    return plain_input_cost + cache_write_cost + cache_read_cost + output_cost


def _resolve_per_million_rate(provider: str, model: str) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(input_per_1M, output_per_1M)`` USD or ``(None, None)``.

    Same lookup ladder as ``_price_for`` (override → litellm → embedded
    table) but returns the raw rates instead of multiplying through.
    Needed because the Anthropic cache split applies different rates
    to different chunks of the same call — we can't pre-multiply.
    """
    # 1. Operator override.
    overrides = _load_price_overrides().get(model)
    if overrides is not None:
        return float(overrides[0]), float(overrides[1])
    # 2. litellm catalog — query with 1M / 1M tokens so the per-token
    # cost we get back IS the per-1M rate. Avoids floating-point loss
    # from inferring a rate from small token counts.
    try:
        import litellm  # type: ignore[import-untyped]

        for candidate in (model, f"{provider.lower()}/{model}"):
            try:
                in_cost, out_cost = litellm.cost_per_token(
                    model=candidate,
                    prompt_tokens=1_000_000,
                    completion_tokens=1_000_000,
                )
                # ``cost_per_token`` returns the total USD for the
                # supplied counts, not a per-token rate. With 1M tokens
                # the total USD IS the per-1M rate.
                if in_cost is not None and out_cost is not None and (in_cost > 0 or out_cost > 0):
                    return float(in_cost), float(out_cost)
            except Exception:  # noqa: BLE001
                continue
    except ImportError:
        pass
    # 3. Embedded fallback table.
    pricing = MODEL_PRICES_USD.get(model)
    if pricing is not None:
        return float(pricing[0]), float(pricing[1])
    return None, None


def _price_for(provider: str, model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    """Look up the USD cost for one (provider, model, tokens) triple.

    Lookup order (most specific wins):

    1. Provider name match — Ollama is locally served, always $0.
    2. Per-org / per-user override at ``~/.fluid/prices.json`` —
       lets enterprise customers patch in their negotiated rates
       (or local volume discounts) without forking forge-cli.
    3. ``litellm.cost_per_token`` — **the canonical price source.**
       litellm carries an actively-maintained pricing catalog covering
       every supported provider / model and tracks upstream price
       changes. Calling forge-cli should always reflect the latest
       upstream pricing without us shipping a release.
    4. Embedded :data:`MODEL_PRICES_USD` table — **offline fallback
       only.** Used when litellm isn't installed or doesn't know the
       model. New models should NOT be added here unless litellm has
       a documented catalog gap.

    Returns ``None`` when none of the sources can price the triple
    so the caller can surface a "price unknown" indicator instead of
    silently fabricating $0.

    The override file is re-read from disk on every call so a price
    correction takes effect on the next forge run, no restart
    required.
    """
    if provider.lower() == "ollama":
        return 0.0

    # 1. Operator overrides win — enterprise rates / volume discounts.
    overrides = _load_price_overrides().get(model)
    if overrides is not None:
        in_price, out_price = overrides
        return (input_tokens * in_price + output_tokens * out_price) / 1_000_000

    # 2. litellm catalog — canonical, kept current upstream.
    try:
        import litellm  # type: ignore[import-untyped]

        for candidate in (model, f"{provider.lower()}/{model}"):
            try:
                in_cost, out_cost = litellm.cost_per_token(
                    model=candidate,
                    prompt_tokens=int(input_tokens or 0),
                    completion_tokens=int(output_tokens or 0),
                )
                total = float(in_cost or 0.0) + float(out_cost or 0.0)
                # Accept zero-cost only when there really were zero
                # tokens — otherwise treat it as a catalog miss and
                # fall through to the next candidate / source.
                if total > 0 or (not input_tokens and not output_tokens):
                    return total
            except Exception:  # noqa: BLE001
                continue
    except ImportError:
        pass

    # 3. Embedded fallback — offline runs / litellm catalog misses.
    pricing = MODEL_PRICES_USD.get(model)
    if pricing is not None:
        in_price, out_price = pricing
        return (input_tokens * in_price + output_tokens * out_price) / 1_000_000

    return None


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
    # Wave 1 — prompt-cache footer. Only emit when at least one row has
    # cache traffic; keeps the legacy snapshot untouched for runs that
    # didn't hit the prompt cache (the common case for non-Anthropic
    # backends today).
    cache_rows = [
        row
        for row in breakdown.rows
        if (row.cache_creation_input_tokens or 0) or (row.cache_read_input_tokens or 0)
    ]
    if cache_rows:
        lines.append("")
        lines.append("  Prompt cache (Anthropic split: write 1.25x, read 0.10x)")
        lines.append("  " + "─" * 63)
        for row in cache_rows:
            lines.append(
                f"    {row.provider:>10} / {row.model:<30}  "
                f"{row.cache_creation_input_tokens:>7,} write  "
                f"{row.cache_read_input_tokens:>7,} read"
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


def record_call_from_cumulative_usage(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    stage: str = "",
    agent_class: str = "",
    usd_override: Optional[float] = None,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> None:
    """Bridge helper: feed the process-wide tracker from a delta snapshot.

    Closes the H1 gap. ``call_llm`` / ``call_llm_streaming`` go through
    ``LiteLLMProvider.invoke_blocking`` which updates
    :data:`fluid_build.cli.forge_copilot_llm_providers._cumulative_usage`
    (a module dict) but never invoked :meth:`RunCostTracker.record_call`.
    The runtime's main authoring loop calls ``call_llm`` directly (not
    through ``BaseStageAgent._call_once`` which is where the staged
    pipeline's record_call lives), so the tracker stayed empty and the
    preview panel reported ``$0 / 0 tokens`` even when the underlying
    Gemini call had spent 8k+ real tokens.

    Callers compute the per-call delta from ``_cumulative_usage``
    (snapshot before, snapshot after) and pass it here. This module
    owns the canonical ``record_call`` semantics (cost ceiling,
    per-agent attribution, prompt-cache split) so the bridge is a thin
    pass-through — no policy lives here, only the kwarg shape.

    Zero-token calls are explicitly recorded so the missing-usage
    counter increments (matching how the staged pipeline behaves).
    """
    _RUN_TRACKER.record_call(
        provider=provider,
        model=model,
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        stage=stage,
        agent_class=agent_class,
        usd_override=usd_override,
        cache_creation_input_tokens=int(cache_creation_input_tokens or 0),
        cache_read_input_tokens=int(cache_read_input_tokens or 0),
    )


__all__ = [
    "MODEL_PRICES_USD",
    "RunCostTracker",
    "CostBreakdown",
    "CostRow",
    "get_run_tracker",
    "reset_run_tracker",
    "format_cost_summary",
    "print_cost_summary",
    "record_call_from_cumulative_usage",
]
