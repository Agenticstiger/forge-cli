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

# ruff: noqa: T201 — this helper module owns CLI prompt output (print) by design;
# user-facing output flows through console.cprint elsewhere.
"""World-class fresh-product interview (Phase 0.6).

The legacy bootstrap interview asked generic questions ("what data
sources?", "got sample data?") even when the welcome scan, project
memory, and CLI flags had already answered them. This module is the
opinionated replacement:

* **Detect-first**. Reads the welcome scan + project memory + workspace
  config + CLI flags. Inferred fields render as "→ Inferred" lines,
  not as questions.

* **Examples in every prompt**. Each free-text question carries a
  concrete example (e.g. "daily Stripe pricing snapshots into Snowflake")
  so the user knows the granularity expected.

* **Adaptive, not sequential**. One smart question, evaluate the answer
  + every signal so far, decide the next question. No fixed phases.

* **`:auto` escape**. Type ``:auto`` at any point to let the system pick
  defaults for the rest. Useful for power users.

* **Progress indicator + cost estimate**. Each prompt prefixes with
  "Q3/5 · ~$0.04 left to hit cap". The cap is the cumulative cost
  ceiling read from copilot config or env.

* **productType is question #1** (or inferred). It changes which other
  questions matter — SDP needs source/mode, ADP/CDP need composition
  context. Buried deep was a UX bug.

* **Captures every schema-relevant facet** in a single pass:
  productType / domain / owner / sovereignty / sensitivity / engine /
  delivery / sample data / DDL / use case. Each only when the value
  isn't already known.

* **Schema-coverage check** runs at the end so the LLM seed payload is
  guaranteed to carry every required field.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inference — every signal we can pull without asking
# ---------------------------------------------------------------------------


@dataclass
class InterviewSignals:
    """Everything the system already knows before the interview starts."""

    workspace_root: Optional[str] = None
    workspace_lock: str = ""
    domain: str = ""
    owner_team: str = ""
    owner_email: str = ""
    cwd_name: str = ""
    has_workspace_yaml: bool = False
    sample_files: List[str] = field(default_factory=list)
    sql_files: List[str] = field(default_factory=list)
    existing_products: int = 0
    suggested_data_product_type: str = ""
    ai_configured: bool = False
    ai_provider: str = ""
    cost_estimate_usd: float = 0.04
    estimated_seconds: int = 18
    # Phase 1.3: which Data Mesh product types the active agent allows.
    # Empty / missing means "no filter" — every type is offered.
    # Populated from the resolved agent's
    # ``supported_data_product_types`` field on the spec.
    allowed_data_product_types: List[str] = field(default_factory=list)
    active_agent: str = ""


# ---------------------------------------------------------------------------
# Question catalog — schema-aware, every entry maps to a contract field
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    """One adaptive question + its inference + its target contract field."""

    key: str  # context key the answer lands on
    schema_field: str  # JSON path the answer maps to in the contract
    prompt: str
    example: str
    when_to_skip: Callable[[Dict[str, Any], InterviewSignals], bool]
    infer: Optional[Callable[[Dict[str, Any], InterviewSignals], Optional[str]]] = None
    auto_default: Optional[Callable[[Dict[str, Any], InterviewSignals], Any]] = None
    required: bool = False


def _has(ctx: Dict[str, Any], key: str) -> bool:
    val = ctx.get(key)
    return val not in (None, "", [], {})


def _question_data_product_type(_ctx, sig):
    """Infer the product type when possible; respect the agent allowlist."""
    allowed = list(sig.allowed_data_product_types or [])

    def _coerce(candidate: str) -> Optional[str]:
        """Return ``candidate`` only if it's in the agent's allowlist
        (or no allowlist applies)."""
        if not candidate:
            return None
        if not allowed or candidate.upper() in {a.upper() for a in allowed}:
            return candidate
        return None

    if sig.workspace_lock:
        coerced = _coerce(sig.workspace_lock)
        if coerced:
            return coerced
    if sig.suggested_data_product_type:
        coerced = _coerce(sig.suggested_data_product_type)
        if coerced:
            return coerced
    if sig.existing_products >= 1:
        coerced = _coerce("ADP")
        if coerced:
            return coerced  # likely composing
    # Fall through: when we have an inference but the allowlist forbids
    # it, fall back to the first allowed type so the picker still
    # converges. Empty allowlist returns None (ask the user).
    if allowed:
        return allowed[0]
    return None  # ask


def _question_domain(_ctx, sig):
    if sig.domain:
        return sig.domain
    # Infer from cwd name when it looks domain-shaped (kebab-case w/ no dot)
    cwd = (sig.cwd_name or "").strip()
    if cwd and "-" in cwd and "." not in cwd:
        # Pick the most "domain-y" segment: drop common scaffolding words
        parts = [p for p in cwd.split("-") if p not in {"app", "data", "project", "dp"}]
        if parts:
            return parts[0]
    return None


def _question_owner_team(_ctx, sig):
    return sig.owner_team or None


def _question_data_sources(ctx, sig):
    """Don't ask if we already detected sample data or SQL files."""
    if _has(ctx, "data_sources"):
        return "<already-set>"
    if sig.sample_files or sig.sql_files:
        bits = []
        if sig.sample_files:
            bits.append(f"detected: {', '.join(sig.sample_files[:3])}")
        if sig.sql_files:
            bits.append(f"sql: {', '.join(sig.sql_files[:3])}")
        return "; ".join(bits)
    return None


_QUESTIONS: Tuple[Question, ...] = (
    Question(
        key="project_goal",
        schema_field="$.description",
        prompt="What's the data product goal?",
        example="daily Stripe pricing snapshots loaded into Snowflake for trading desk",
        required=True,
        when_to_skip=lambda ctx, sig: _has(ctx, "project_goal"),
        auto_default=lambda _ctx, sig: "AI-generated FLUID data product"
        + (f" in the {sig.domain} domain" if sig.domain else ""),
    ),
    Question(
        key="data_product_type",
        schema_field="$.metadata.productType (and $.metadata.layer)",
        prompt="Type? SDP (Bronze, raw acquisition), ADP (Silver, "
        "joined/cleaned), CDP (Gold, consumption mart).",
        example="SDP for ingestion · ADP if composing from upstreams",
        when_to_skip=lambda ctx, sig: _has(ctx, "data_product_type") or _has(ctx, "productType"),
        infer=_question_data_product_type,
        auto_default=lambda _ctx, sig: sig.workspace_lock
        or sig.suggested_data_product_type
        or "SDP",
    ),
    Question(
        key="domain",
        schema_field="$.domain",
        prompt="Domain?",
        example="commerce, finance, healthcare, telco",
        when_to_skip=lambda ctx, sig: _has(ctx, "domain"),
        infer=_question_domain,
        auto_default=lambda _ctx, _sig: "analytics",
    ),
    Question(
        key="owner_team",
        schema_field="$.metadata.owner.team",
        prompt="Owning team?",
        example="data-platform, growth-analytics, fraud-ops",
        when_to_skip=lambda ctx, sig: _has(ctx, "owner_team") or _has(ctx, "owner"),
        infer=_question_owner_team,
        auto_default=lambda _ctx, _sig: "data-team",
    ),
    Question(
        key="data_sources",
        schema_field="$.builds[].properties.source / $.consumes[]",
        prompt="What data sources? (paste a URI, table name, or 'none' if "
        "they're already in this workspace)",
        example="https://api.stripe.com/v1/prices · postgres://db/orders · " "data/orders.csv",
        when_to_skip=lambda ctx, sig: _has(ctx, "data_sources")
        or sig.existing_products >= 1
        or bool(sig.sample_files),
        infer=_question_data_sources,
        auto_default=lambda _ctx, sig: (
            ", ".join(sig.sample_files[:2]) if sig.sample_files else "tbd"
        ),
    ),
    Question(
        key="data_sensitivity",
        schema_field="$.exposes[].policy.agentPolicy / $.metadata.labels",
        prompt="Data sensitivity?",
        example="public, internal, confidential, restricted",
        when_to_skip=lambda ctx, _sig: _has(ctx, "data_sensitivity"),
        auto_default=lambda _ctx, _sig: "internal",
    ),
)


# ---------------------------------------------------------------------------
# Signal collection
# ---------------------------------------------------------------------------


def collect_signals(
    *,
    target_dir: Optional[Path] = None,
    project_memory: Any = None,
    active_agent: str = "",
) -> InterviewSignals:
    """Pull every signal the system already has into a single record.

    Welcome-scan findings, workspace config, project memory,
    environment markers — all rolled up here so the interview's
    skip-when-known logic has one place to look.

    ``active_agent`` (Phase 1.3): when set, the interview filters the
    data-product-type picker to the codes the agent's spec declares
    in ``supported_data_product_types``. Empty / unknown agent names
    leave the picker unfiltered (all three types).
    """
    cwd = (target_dir or Path.cwd()).resolve()
    sig = InterviewSignals(cwd_name=cwd.name, active_agent=active_agent)

    # Resolve the agent's supported types. ``get_supported_data_product_types``
    # fails open with all types when the agent is unknown, so empty
    # / typo'd agent names never block the interview.
    try:
        from fluid_build.cli.forge_agents import get_supported_data_product_types

        sig.allowed_data_product_types = list(get_supported_data_product_types(active_agent))
    except Exception as exc:  # noqa: BLE001 — fail open
        LOG.debug("supported_data_product_types_lookup_failed: %s", exc)

    try:
        from fluid_build.cli._welcome_scan import run_welcome_scan

        findings = run_welcome_scan(start=cwd)
        sig.workspace_root = findings.workspace_root
        sig.workspace_lock = findings.workspace_lock
        sig.has_workspace_yaml = findings.in_workspace
        sig.existing_products = findings.existing_products
        sig.sample_files = list(findings.sample_data_candidates)
        sig.suggested_data_product_type = findings.suggested_data_product_type
        sig.ai_configured = findings.ai_configured
        sig.ai_provider = findings.ai_provider_hint
    except Exception as exc:  # noqa: BLE001
        LOG.debug("welcome_scan_unavailable: %s", exc)

    try:
        from fluid_build.cli.workspace_config import (
            find_workspace_root,
            load_workspace_config,
        )

        ws_root = find_workspace_root(cwd)
        if ws_root:
            cfg = load_workspace_config(ws_root)
            sig.domain = cfg.domain
            sig.owner_team = cfg.owner_team
            sig.owner_email = cfg.owner_email
    except Exception as exc:  # noqa: BLE001
        LOG.debug("workspace_config_unavailable: %s", exc)

    if project_memory is not None:
        try:
            sig.domain = sig.domain or getattr(project_memory, "preferred_domain", "") or ""
            sig.owner_team = sig.owner_team or getattr(project_memory, "preferred_owner", "") or ""
        except Exception:  # noqa: BLE001
            pass

    return sig


def needs_asking(question: Question, ctx: Dict[str, Any], sig: InterviewSignals) -> bool:
    """True only when the question still has no resolved value.

    Order of precedence: explicit context > inference. Inference returns
    a string like ``<already-set>`` to flag "skipped because we
    detected something equivalent" without polluting the context.
    """
    if question.when_to_skip(ctx, sig):
        return False
    if question.infer is not None:
        inferred = question.infer(ctx, sig)
        if inferred and inferred != "<already-set>":
            ctx[question.key] = inferred
            return False
    return True


# ---------------------------------------------------------------------------
# Adaptive driver
# ---------------------------------------------------------------------------


@dataclass
class InterviewProgress:
    """What the user sees in the prompt prefix."""

    asked: int = 0
    total: int = 0
    cumulative_usd: float = 0.0
    cap_usd: float = 0.0


def _format_progress(p: InterviewProgress) -> str:
    parts = [f"Q{p.asked + 1}/{p.total}"]
    if p.cap_usd > 0:
        remaining = max(0.0, p.cap_usd - p.cumulative_usd)
        parts.append(f"~${remaining:.2f} budget left")
    return f"[{' · '.join(parts)}]"


def _resolve_cap_usd() -> float:
    """Read the per-run cost cap from env/config — defaults to a soft ceiling."""
    raw = os.environ.get("FLUID_COST_LIMIT_USD_PER_RUN", "")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    try:
        from fluid_build.copilot.cost import _resolve_cost_limit_usd

        return float(_resolve_cost_limit_usd() or 0.0)
    except Exception:  # noqa: BLE001
        return 1.0  # default $1 per run


def render_inferences_panel(sig: InterviewSignals, ctx: Dict[str, Any], *, console: Any) -> None:
    """Print the "what I see" panel BEFORE asking any question.

    This is the detect-first promise: the user sees what we already
    know, then answers only the gaps.
    """
    if console is None:
        return
    rows: List[Tuple[str, str]] = []
    if sig.workspace_root:
        rows.append(("Workspace", sig.workspace_root))
    if sig.workspace_lock:
        rows.append(("Lock", sig.workspace_lock))
    if sig.existing_products:
        rows.append(("Products", f"{sig.existing_products} in workspace"))
    if sig.sample_files:
        rows.append(("Sample data", ", ".join(sig.sample_files[:3])))
    if sig.ai_configured:
        rows.append(("AI", sig.ai_provider or "configured"))
    if sig.domain:
        rows.append(("Domain", sig.domain))
    if sig.owner_team:
        rows.append(("Team", sig.owner_team))
    if not rows:
        return
    try:
        from rich.panel import Panel as _Panel
        from rich.table import Table as _Table

        t = _Table.grid(padding=(0, 2))
        t.add_column(justify="right", style="dim cyan")
        t.add_column()
        for label, value in rows:
            t.add_row(label, value)
        console.print(
            _Panel(
                t,
                title="[bold]👀 What I see[/bold]",
                subtitle="[dim]I'll only ask about the gaps[/dim]",
                border_style="cyan",
            )
        )
    except Exception:  # noqa: BLE001
        for label, value in rows:
            print(f"  {label}: {value}")


def run_world_class_bootstrap(
    *,
    state: Any,
    console: Any,
    target_dir: Optional[Path] = None,
    project_memory: Any = None,
    auto_mode: bool = False,
    active_agent: str = "",
) -> None:
    """Drive the world-class interview against ``state``.

    *state* must expose ``normalized_context`` (dict) and ``apply_patch``
    / ``record_turn`` / ``add_assumptions`` like the legacy
    :class:`CopilotInterviewState`.

    ``active_agent`` (Phase 1.3): forwarded to :func:`collect_signals`
    so the data-product-type picker honours the agent's
    ``supported_data_product_types`` allowlist. The interview's
    inference path falls back to the first allowed type when the
    workspace lock or scan-suggested type is forbidden by the agent.
    """
    from fluid_build.cli.forge_dialogs import ask_friendly_text

    ctx: Dict[str, Any] = state.normalized_context
    # Resolve active agent in priority order: explicit kwarg → context
    # (set by upstream picker / --agent flag) → empty (unfiltered).
    active_agent = active_agent or str(ctx.get("agent_name") or ctx.get("active_agent") or "")
    sig = collect_signals(
        target_dir=target_dir,
        project_memory=project_memory,
        active_agent=active_agent,
    )

    # Pre-fill anything we can infer without asking, BEFORE rendering
    # the panel — so "What I see" reflects what the interview will skip.
    pre_filled = []
    for q in _QUESTIONS:
        if q.when_to_skip(ctx, sig):
            continue
        if q.infer is not None:
            inferred = q.infer(ctx, sig)
            if inferred and inferred != "<already-set>":
                ctx[q.key] = inferred
                pre_filled.append(f"{q.key}={inferred}")

    if pre_filled:
        try:
            state.add_assumptions([f"Inferred without asking: {f}" for f in pre_filled[:8]])
        except Exception:  # noqa: BLE001
            pass
    try:
        from fluid_build.cli._ux_telemetry import get_telemetry as _get_tel

        _tel = _get_tel()
        _tel.mark_first_panel()
        _tel.record_inference(len(pre_filled))
        _tel.welcome_scan_ms = int(getattr(sig, "scan_duration_ms", 0))
        _tel.mode = "standard"
    except Exception:  # noqa: BLE001
        pass
    render_inferences_panel(sig, ctx, console=console)

    open_questions = [q for q in _QUESTIONS if needs_asking(q, ctx, sig)]
    progress = InterviewProgress(
        asked=0,
        total=len(open_questions),
        cumulative_usd=0.0,
        cap_usd=_resolve_cap_usd(),
    )

    if auto_mode:
        # ``:auto`` mode: pick defaults for everything, ask nothing.
        for q in open_questions:
            default = q.auto_default(ctx, sig) if q.auto_default else None
            if default is not None:
                ctx[q.key] = default
        if console:
            try:
                console.print(
                    "[green]✓[/green] :auto mode — defaults applied for "
                    f"{len(open_questions)} question(s).\n"
                )
            except Exception:  # noqa: BLE001
                pass
        return

    # Streaming contract preview (Phase 3 #4) — render the seed
    # contract as it grows. Default ON; set
    # ``FLUID_FORGE_NO_STREAMING_PREVIEW=1`` to suppress (e.g. very
    # narrow terminals or noisy CI logs).
    show_growing_contract = console is not None and not os.environ.get(
        "FLUID_FORGE_NO_STREAMING_PREVIEW"
    )

    try:
        from fluid_build.cli._ux_telemetry import get_telemetry as _get_tel
    except Exception:  # noqa: BLE001
        _get_tel = None  # type: ignore[assignment]

    for q in open_questions:
        progress.asked += 1
        if _get_tel is not None:
            try:
                _get_tel().record_question()
            except Exception:  # noqa: BLE001
                pass
        prompt = (
            f"{_format_progress(progress)} {q.prompt}\n"
            f"  [dim]e.g. {q.example}[/dim]\n"
            f"  [dim]Type :auto to let me pick defaults for the rest · "
            ":help for commands[/dim]"
        )
        # Try-loop in case the user sends a slash command.
        for _ in range(5):
            answer = ask_friendly_text(console, prompt, required=q.required)
            if answer is None or not answer.strip():
                # Required → loop one more time; not required → take auto default.
                if q.required:
                    if console:
                        try:
                            console.print(
                                "[dim]A short answer is enough — "
                                "or type :auto to let me handle it.[/dim]"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                default = q.auto_default(ctx, sig) if q.auto_default else None
                if default is not None:
                    ctx[q.key] = default
                break
            # ``:auto`` mid-flow — fill remaining defaults and exit.
            stripped = answer.strip()
            if stripped == ":auto":
                # Consume remaining open questions as auto defaults.
                for remaining in open_questions[progress.asked - 1 :]:
                    if remaining.key in ctx and ctx[remaining.key]:
                        continue
                    default = remaining.auto_default(ctx, sig) if remaining.auto_default else None
                    if default is not None:
                        ctx[remaining.key] = default
                if console:
                    try:
                        console.print("[green]✓[/green] :auto — taking defaults for the rest.\n")
                    except Exception:  # noqa: BLE001
                        pass
                return
            # Slash commands (other than :auto) are handled by the dialog
            # layer's wrapper already; here we just record the answer.
            ctx[q.key] = stripped
            try:
                state.record_turn(
                    role="user",
                    content=stripped,
                    field=q.key,
                    question_id=f"world_class_{q.key}",
                    raw_input=stripped,
                    resolved_value=stripped,
                    resolution_status="matched",
                )
            except Exception:  # noqa: BLE001
                pass
            # Streaming contract preview (Phase 3 #4) — show the contract
            # growing after each answer so the user sees the ROI of
            # answering immediately, not after a 30-second LLM call.
            if show_growing_contract:
                try:
                    from fluid_build.cli._streaming_contract_preview import (
                        render_growing_contract,
                    )

                    render_growing_contract(ctx, console=console)
                except Exception:  # noqa: BLE001
                    pass
            break

    # Resolve productType ↔ layer canonical pair (the equivalence axiom)
    # so the seed contract carries both fields no matter which one the
    # user typed.
    raw_pt = (ctx.get("data_product_type") or ctx.get("productType") or "").strip()
    if raw_pt:
        try:
            from fluid_build.forge.product_types import get_product_type

            pt = get_product_type(raw_pt)
            if pt is not None:
                ctx["data_product_type"] = pt.code
                ctx["productType"] = pt.code
                ctx["layer"] = pt.layer
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Schema-coverage check
# ---------------------------------------------------------------------------


@dataclass
class CoverageReport:
    """One-row report of which schema-required fields are populated.

    Used as a post-interview gate: the runtime checks the report and
    falls back to defaults for anything missing so the LLM seed can't
    accidentally produce a contract that fails ``fluid validate``.
    """

    has_id: bool = False
    has_name: bool = False
    has_domain: bool = False
    has_owner_team: bool = False
    has_layer_or_product_type: bool = False
    has_data_source_or_consumes: bool = False
    missing: List[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing


def assess_coverage(ctx: Mapping[str, Any]) -> CoverageReport:
    """Audit the resolved context against schema-required facets.

    Each "required" facet has a fallback: ``id`` defaults from goal +
    domain; ``name`` from goal; ``layer/productType`` from registry.
    The runtime applies these defaults; this function reports what's
    missing so we can log + populate.
    """
    rep = CoverageReport()
    rep.has_id = bool(ctx.get("id"))
    rep.has_name = bool(ctx.get("name") or ctx.get("project_goal"))
    rep.has_domain = bool(ctx.get("domain"))
    rep.has_owner_team = bool(ctx.get("owner_team") or ctx.get("owner"))
    rep.has_layer_or_product_type = bool(
        ctx.get("layer") or ctx.get("productType") or ctx.get("data_product_type")
    )
    rep.has_data_source_or_consumes = bool(
        ctx.get("data_sources")
        or ctx.get("consumes")
        or ctx.get("composition")
        or ctx.get("seed_contract_override")
    )
    if not rep.has_name:
        rep.missing.append("name (or project_goal)")
    if not rep.has_domain:
        rep.missing.append("domain")
    if not rep.has_owner_team:
        rep.missing.append("owner_team")
    if not rep.has_layer_or_product_type:
        rep.missing.append("layer/productType")
    if not rep.has_data_source_or_consumes:
        rep.missing.append("data_sources or consumes")
    return rep


__all__ = [
    "CoverageReport",
    "InterviewProgress",
    "InterviewSignals",
    "Question",
    "assess_coverage",
    "collect_signals",
    "needs_asking",
    "render_inferences_panel",
    "run_world_class_bootstrap",
]
