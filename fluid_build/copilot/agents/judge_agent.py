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

"""JudgeAgent — out-of-loop quality scoring for finalised contracts (Wave 3.1).

The existing in-loop critic (:class:`CriticAgent`) and validator
(:class:`ValidatorAgent`) catch synthesis-time issues. Neither one
produces a comparable score for the finished contract — which means
we cannot tell whether a subsequent improvement (LiteLLM Router,
dbt-test generation, better RAG retrieval) actually moves outcomes.

JudgeAgent fills that gap: it runs **after** synthesis + validation
completes and writes a structured ``judge.json`` report under the
run's receipts directory. It does **not** influence the synthesis
loop and never raises into the caller's hot path — a failed judge
returns ``None`` (or a parse-error for tests that ask for one)
and logs at DEBUG.

Design choices, with citations to the prior art we surveyed
(borrow-before-build):

* **Chain-of-thought before score.** Each axis has ``reasoning``
  populated *before* its numeric score. G-Eval research (Liu et al.,
  reported by evidentlyai) shows this lifts Spearman ρ with human
  reviewers from 0.51 → 0.66 on summarisation. Same pattern is used
  by ``quotient-ai/judges`` (``Judgment{reasoning, score, score_type}``).
* **One axis per criterion.** The deepeval / G-Eval guidance is "split
  into separate judges rather than combining axes in one prompt";
  the implementation here keeps every axis in one prompt for cost
  but each axis gets its own reasoning + score so the LLM can't blur
  them together.
* **0..5 Likert scale.** ``judges`` library uses 1-5 numeric; we use
  0-5 because "missing entirely" is a meaningful score for a data-
  contract axis (e.g. no PII tagging → security 0/5).
* **Structured JSON output, low temperature.** Standard practice; the
  parser is robust to a markdown-fenced response (``safe_json_parse``).

References surveyed:

* https://github.com/quotient-ai/judges — Databricks judges library
* https://github.com/The-LLM-Data-Company/rubric — weighted-rubric judge
* https://deepeval.com/guides/guides-llm-as-a-judge — G-Eval pattern
* https://www.evidentlyai.com/llm-guide/llm-as-a-judge — CoT/CoT-before-verdict

Public surface:

* :class:`AxisScore` — per-axis score (0..5), reasoning, suggestions.
* :class:`JudgeResult` — full scorecard (6 axes + ``total``).
* :class:`JudgeAgent` — the agent itself. Instantiate explicitly and
  call :meth:`JudgeAgent.judge(contract, ...)`. The integration step
  wires this into the post-synthesis path; JudgeAgent itself stays
  out-of-loop.
* :class:`JudgeAgent.ParseError` — raised on malformed JSON; the raw
  LLM text is logged at DEBUG, never bubbled into the user message.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

LOG = logging.getLogger("fluid.copilot.judge")


# ---------------------------------------------------------------------
# Self-critique tunables
# ---------------------------------------------------------------------
#
# Gap 6 — Self-Refine-style self-critique pass over the initial judge
# response. Default ON; kill switch via ``FLUID_JUDGE_SELF_CRITIQUE=0``.
#
# Prior art surveyed (borrow-before-build):
#
# * Madaan et al., "Self-Refine: Iterative Refinement with Self-Feedback"
#   (NeurIPS 2023, arxiv 2303.17651, https://selfrefine.info/) — same-LLM
#   feedback-then-refine pattern; reports ~20% quality boost vs. single
#   pass. The critique pass here is the "feedback" step over the
#   initial JSON scorecard.
# * G-Eval / DeepEval (https://deepeval.com/docs/metrics-llm-evals) —
#   chain-of-thought before score lifts Spearman ρ with human reviewers.
#   We re-use the same JSON shape so the critique pass's reasoning
#   stays comparable to the initial pass.
# * DSPy Assertions / Suggest (Singhvi et al., arxiv 2312.13382,
#   https://dspy.ai/learn/programming/7-assertions/) — backtracking pattern
#   that injects the prior output into the retry prompt. We mirror this
#   by embedding the initial axes + reasoning in the critique system
#   prompt so the LLM has explicit anchors to revise.
# * Mervin Praison / Patronus LLM-judge best practices
#   (https://mer.vin/2025/11/llm-as-a-judge-best-practices-for-consistent-evaluation/,
#   https://www.patronus.ai/llm-testing/llm-as-a-judge) — temperature
#   0.0-0.2 for deterministic re-scoring; "high-temp primary + low-temp
#   corrector" balances exploratory depth with deterministic refinement.
#
# Merge rule: per-axis hard threshold of |Δ| > 1.
#   * |new - initial| <= 1 → keep initial (avoid noise / over-tweaking).
#   * |new - initial| >= 2 → adopt critique (the judge has a meaningful
#                            change of mind, not a one-notch quibble).
# Rationale: the spec's "merge with weight 0.5 toward critique"
# simplifies cleanly to this threshold (with weight 0.5 a 1-point delta
# rounds back to initial; a 2-point delta moves halfway, which we
# strengthen to a full move because a one-shot judge with no human in
# the loop is best served by fewer, more decisive corrections).

_SELF_CRITIQUE_DEFAULT_TEMPERATURE = 0.1
"""Lower than the initial pass's default (0.0 baseline) so the
critique is deterministic — industry consensus is 0.0-0.2 for
LLM-as-judge re-scoring. We sit at 0.1 so the critique can express
"I changed my mind by exactly one notch" without being noise-driven."""

_AXIS_DELTA_ADOPTION_THRESHOLD = 1
"""|new_score - initial_score| MUST exceed this to adopt the critique
score. Equality keeps the initial score (no over-tweaking)."""


# Re-export from the canonical catalog module so legacy imports
# (`from fluid_build.copilot.agents.judge_agent import _explicit_catalog_tier_or_none`)
# keep resolving. The helper lives in ``cli/_llm_model_catalog.py``
# alongside ``get_catalog_tier_model`` — that's the canonical home.
from fluid_build.cli._llm_model_catalog import (  # noqa: E402
    get_explicit_catalog_tier as _explicit_catalog_tier_or_none,
)


def _self_critique_enabled() -> bool:
    """True when ``FLUID_JUDGE_SELF_CRITIQUE`` is unset or non-zero.

    Default ON in v1.6+. Operators who want the legacy single-pass
    behaviour set ``FLUID_JUDGE_SELF_CRITIQUE=0``. Any unparseable
    value falls back to ON (the safer default — operators who
    actively configure the variable know what they're doing).
    """
    raw = os.environ.get("FLUID_JUDGE_SELF_CRITIQUE", "1")
    return raw.strip() not in ("0", "false", "False", "no", "off")


def _critique_within_budget() -> bool:
    """Cost-aware skip — mirror of
    ``StageCoordinator._cooperation_would_exceed_budget``.

    Returns True (= safe to run the critique) when EITHER:

    * No per-run cost ceiling is configured, OR
    * Adding the average per-call spend to the running total would
      stay under the ceiling.

    Conservative estimate: assume the critique call costs the average
    of all calls so far. Operators who set a tight ceiling expect the
    system to enforce it; the critique is observability-grade quality
    juice — not worth blowing past the budget for. Any error in the
    cost-tracker read path falls open (returns True) because a broken
    cost lookup shouldn't silently disable a quality feature.
    """
    try:
        from fluid_build.copilot.cost import (
            _resolve_cost_limit_usd,
            get_run_tracker,
        )

        limit = _resolve_cost_limit_usd()
        if limit is None:
            return True
        breakdown = get_run_tracker().breakdown()
        running = breakdown.total_usd
        if running is None:
            # Unknown spend so far — fall open so a single unknown-price
            # call doesn't silently disable critique for the rest of
            # the run.
            return True
        calls = breakdown.total_calls or 0
        # Match the coordinator's 5¢ baseline so callers see consistent
        # projection numbers across the two cost-aware short-circuits.
        avg = (running / calls) if calls else 0.05
        return (running + avg) <= limit
    except Exception:  # pragma: no cover — defensive
        return True


# ---------------------------------------------------------------------
# Result dataclasses (public)
# ---------------------------------------------------------------------


@dataclass
class AxisScore:
    """Per-axis judgement.

    ``score`` is the 0..5 Likert value (0 = missing / not present,
    5 = excellent). ``reasoning`` is the LLM's CoT for THIS axis
    (one or two sentences in practice; we don't truncate so the
    audit trail stays honest). ``suggestions`` is an optional list
    of concrete, actionable hints — the LLM may emit zero of these
    for axes that already score 5/5.
    """

    score: int
    reasoning: str
    suggestions: List[str] = field(default_factory=list)


@dataclass
class JudgeResult:
    """Full judge scorecard for one contract.

    ``axes`` keys MUST be the six axes declared on
    :data:`JudgeAgent.AXES`. ``total`` is the sum of scores (0..30).
    ``model`` records the judge model so subsequent runs are
    comparable (judge-of-A != judge-of-B). ``run_id`` is optional
    when the caller doesn't resolve one — persistence then skips
    silently.

    ``critique_applied`` (Gap 6) — True when the optional
    Self-Refine-style critique pass ran and at least produced a
    parseable response (whether or not any axis score changed).
    Default False so the legacy single-pass behaviour stays the
    documented v1.5 shape; the integration test asserts the flag
    flips True when ``FLUID_JUDGE_SELF_CRITIQUE`` is on.

    ``critique_summary`` (Gap 6) — when ``critique_applied`` is True,
    carries the list of axes whose scores changed plus the before /
    after totals so a downstream UI / CI hook can render "judge
    changed its mind on N axes" without re-deriving the diff. Stays
    ``None`` for non-critique runs to keep the legacy persisted-file
    shape byte-identical."""

    axes: Dict[str, AxisScore]
    total: int
    model: str
    run_id: Optional[str] = None
    critique_applied: bool = False
    critique_summary: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict for ``judge.json`` persistence."""
        out: Dict[str, Any] = {
            "axes": {
                axis: {
                    "score": s.score,
                    "reasoning": s.reasoning,
                    "suggestions": list(s.suggestions),
                }
                for axis, s in self.axes.items()
            },
            "total": self.total,
            "model": self.model,
            "run_id": self.run_id,
            "critique_applied": self.critique_applied,
        }
        # Only include the critique block when it ran — keeps the legacy
        # persisted-file shape unchanged for non-critique runs.
        if self.critique_summary is not None:
            out["critique_summary"] = dict(self.critique_summary)
        return out


# ---------------------------------------------------------------------
# Rubric / prompt-construction constants
# ---------------------------------------------------------------------


# Per-axis natural-language definitions. The judge prompt embeds these
# verbatim so a future axis tweak (e.g. tightening 'security' to require
# masking AND tagging) is a one-line edit here instead of a prompt rewrite.
_AXIS_DEFINITIONS: Dict[str, str] = {
    "correctness": (
        "Does the contract's declared schema (column names, types, "
        "nullability) match the sample data and the upstream sources? "
        "Are data types reasonable for the values shown?"
    ),
    "completeness": (
        "Are the required contract fields populated — owner, SLAs, "
        "descriptions, retention, refresh cadence? Anything missing "
        "that would block a downstream consumer from using this?"
    ),
    "security": (
        "Are PII / sensitive columns tagged with a classification? Is "
        "masking applied where appropriate (e.g. for emails, SSNs, "
        "tokens)? Are there obvious leakage paths in exposes[]?"
    ),
    "governance": (
        "Is the data product owned (team or person, contactable)? Are "
        "access policies declared? Is retention specified? Is the "
        "lineage clear (consumes[]) when this product depends on others?"
    ),
    "performance": (
        "Are clustering / partition keys / indexes sensible for the "
        "product type (SDP/ADP/CDP) and the query patterns implied by "
        "exposes[]? Are large tables partitioned at all?"
    ),
    "documentation": (
        "Are README and per-column descriptions present and non-trivial? "
        "Could a new analyst use this contract without asking the owner "
        "what each column means?"
    ),
}


# One-shot example showing the exact response shape we expect. Kept
# minimal (3 axes shown, not 6) because the JSON-structured-output
# directive carries the contract; the example only has to teach the
# *shape*, not enumerate every axis. The full 6-axis list is in the
# system prompt's "respond with this exact JSON shape" block.
_ONE_SHOT_EXAMPLE = """\
Example response (abbreviated to 3 axes for illustration; you must \
score all 6):

```json
{
  "axes": {
    "correctness": {
      "score": 4,
      "reasoning": "Schema columns match the sample data; one numeric column is typed as STRING which is conservative but inefficient.",
      "suggestions": ["Type 'amount_cents' as INT64 instead of STRING."]
    },
    "completeness": {
      "score": 3,
      "reasoning": "Owner and SLA are set, but column descriptions are missing for 4/12 columns.",
      "suggestions": ["Add descriptions for: customer_id, created_at, status, tier."]
    },
    "security": {
      "score": 5,
      "reasoning": "All PII columns tagged; email is masked in exposes[]; no obvious leakage.",
      "suggestions": []
    }
  }
}
```"""


def _format_artifacts_block(artifacts: Optional[Dict[str, Any]]) -> str:
    """Render the post-synthesis enrichment artifacts into the user prompt.

    Returns the empty string when ``artifacts`` is None or has no
    populated fields. Otherwise returns a YAML-fenced block plus a
    rubric instruction telling the judge to credit axes that the
    enrichment fills in (so a sparse contract still scores correctly
    when deterministic tooling has produced sensible defaults).
    """
    if not artifacts:
        return ""
    populated = {k: v for k, v in artifacts.items() if v not in (None, "", [], {})}
    if not populated:
        return ""
    try:
        artifacts_yaml = yaml.safe_dump(
            populated,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=100,
        )
    except Exception:  # noqa: BLE001 — fall back to JSON
        artifacts_yaml = json.dumps(populated, indent=2, default=str, sort_keys=False)
    return (
        "Deterministic-enrichment outputs (run AFTER contract synthesis by the "
        "post-synthesis pipeline; these are recommended additions the operator "
        "can apply verbatim — treat them as if already applied when scoring "
        "performance / governance / documentation axes):\n\n"
        "```yaml\n"
        f"{artifacts_yaml}\n"
        "```\n\n"
    )


# ---------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------


class JudgeAgent:
    """Score a finalised contract against a 6-axis rubric.

    Out-of-loop: instantiate explicitly after synthesis + validation,
    call :meth:`judge`. The result is returned to the caller AND
    (best-effort) persisted to ``.fluid/agents/<run_id>/judge.json``.

    Construction is cheap (no LLM call). The model is resolved on
    first :meth:`judge` invocation so a JudgeAgent built at import
    time doesn't probe the model catalog. Pass ``model`` explicitly
    to override the catalog-derived default.
    """

    #: Canonical axes — order matches the contract-quality rubric in the
    #: module docstring. Tests pin this exactly; downstream consumers
    #: (UI footers, regression diffs) rely on the order.
    AXES: List[str] = [
        "correctness",
        "completeness",
        "security",
        "governance",
        "performance",
        "documentation",
    ]

    class ParseError(RuntimeError):
        """Raised when the LLM response cannot be parsed into a
        :class:`JudgeResult`. The raw text is NOT included in the
        exception message — it has been logged at DEBUG by the
        caller — because judge runs out-of-loop and the raw payload
        could be many KB of CoT prose."""

    def __init__(self, model: Optional[str] = None) -> None:
        self._explicit_model = model

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def judge(
        self,
        contract: Dict[str, Any],
        *,
        build_artifacts: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> JudgeResult:
        """Score ``contract``; return a :class:`JudgeResult` and persist it.

        ``build_artifacts`` is reserved for a future pass that judges
        the dbt builds too; not consumed in v1 but kept on the
        signature so the integration call site is stable.

        Persistence is best-effort. A failed write is logged at
        DEBUG and swallowed — the returned :class:`JudgeResult` is
        the load-bearing contract.
        """
        from fluid_build.cli.forge_copilot_llm_providers import (
            call_llm,
            get_llm_provider,
            resolve_llm_config,
        )

        # Wall-clock the entire judge (initial + critique + persistence)
        # so the cost.json receipt carries an honest duration. The clock
        # starts BEFORE resolve_llm_config because the provider/model
        # ladder can do a fast catalog probe and we want that included
        # in the per-run summary (it's wall-clock the operator pays for).
        _judge_started_at = time.time()

        # Argparse-shaped namespace stand-in. resolve_llm_config()
        # reads getattr(args, ...) defensively; an empty object hits
        # every default branch (provider/model/endpoint/key from env).
        class _Args:
            pass

        llm_config = resolve_llm_config(_Args())
        # Resolve the judge-tier model. Ladder (most specific → fallback):
        #   1. explicit ``model=`` passed to the constructor (operator override)
        #   2. catalog's *explicit* ``judge`` tier (operator override via override file)
        #   3. catalog's ``fast`` tier (haiku/flash/nano per provider, kept fresh
        #      by ``.github/workflows/update-model-catalog.yml``)
        #   4. run's primary model (last resort — usually expensive)
        #
        # The cheap-tier preference matters because the judge runs on
        # EVERY synthesis + critique pass; if we defaulted to the primary
        # (often Opus / GPT-4.1 / Pro), the judge alone would eat 30-50%
        # of the run's token budget. Haiku-class models judge structured
        # rubrics within 1-2 percentage points of flagship models per
        # G-Eval correlation studies (see judge_agent module docstring).
        #
        # CRITICAL: ``get_catalog_tier_model(provider, "judge")`` silently
        # returns the FLAGSHIP when no explicit "judge" tier is defined
        # (the function falls through ``tier → flagship → default``).
        # That defeats the cheap-tier promise. We use
        # ``_explicit_catalog_tier_or_none`` to detect "really defined" vs
        # "silently fell back" so the catalog's ``fast`` tier wins when
        # no operator-supplied ``judge`` tier exists. The catalog file
        # (``cli/llm_models.json``) is the single source of truth for
        # per-provider cheap-model identifiers — no hardcoded fallback
        # dict here so a provider's model-rename can't silently regress
        # us to a deprecated identifier. Discovered live, 2026-05-27.
        judge_model = (
            self._explicit_model
            or _explicit_catalog_tier_or_none(llm_config.provider, "judge")
            or _explicit_catalog_tier_or_none(llm_config.provider, "fast")
            or llm_config.model
        )

        # Swap in the judge model on a shallow copy so the rest of the
        # run's primary-model config is preserved (endpoint resolution,
        # API key, timeout). Avoids re-running the full preflight ladder.
        import dataclasses

        judge_config = dataclasses.replace(llm_config, model=judge_model)

        provider = get_llm_provider(judge_config.provider)
        system_prompt, user_prompt = self._build_prompt(contract, build_artifacts=build_artifacts)

        # Provider errors propagate naturally — ParseError specifically
        # means "got text, can't parse"; callers wrap judge() in
        # try/except and branch on the exception type.
        raw = call_llm(
            provider,
            judge_config,
            system_prompt,
            user_prompt,
        )

        result = self._parse(raw, model=judge_model, run_id=run_id)

        # Gap 6 — Self-Refine-style critique pass. Default ON; kill
        # switch via ``FLUID_JUDGE_SELF_CRITIQUE=0``. Cost-aware skip
        # via :func:`_critique_within_budget`. Failures fall open
        # (initial result preserved) so a broken second pass can never
        # poison a good first pass.
        if _self_critique_enabled() and _critique_within_budget():
            try:
                result = self._run_self_critique(
                    initial=result,
                    contract=contract,
                    judge_config=judge_config,
                    provider=provider,
                    build_artifacts=build_artifacts,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                # Fail-open: keep the initial result. Logged at DEBUG
                # because critique is observability-grade quality juice,
                # not a hard requirement; an operator who set the
                # default-ON flag isn't expecting a noisy WARN line
                # every time the second pass blips.
                LOG.debug("judge_critique_failed error=%r", exc)
        else:
            LOG.debug(
                "judge_critique_skipped enabled=%s within_budget=%s",
                _self_critique_enabled(),
                _critique_within_budget(),
            )

        # Logging: INFO line per axis + total; reasoning at DEBUG only
        # (judge prose is long and runs out-of-loop, don't dominate the
        # terminal).
        for axis_name in self.AXES:
            axis = result.axes.get(axis_name)
            if axis is None:
                continue
            LOG.info(
                "judge axis=%s score=%d/5",
                axis_name,
                axis.score,
            )
            LOG.debug(
                "judge axis=%s reasoning=%r suggestions=%r",
                axis_name,
                axis.reasoning,
                axis.suggestions,
            )
        LOG.info("judge total=%d/%d model=%s", result.total, len(self.AXES) * 5, judge_model)

        # Persistence — best-effort. Resolve run_id with the env/file
        # fallback (matches every other receipt-emitting agent).
        try:
            self._persist(result, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — defensive
            LOG.debug("judge_persist_failed error=%r", exc)

        # H22 follow-up — also persist ``cost.json`` so judge-only runs
        # (e.g. the live OpenAI smoke that calls JudgeAgent directly,
        # bypassing ``cli/forge_data_model.py``'s receipt path) become
        # visible to ``fluid stats``. Without this, the cost tracker
        # correctly records tokens / USD in memory but the on-disk
        # receipt is never written for judge-only invocations.
        # Best-effort: any exception is swallowed at DEBUG. The judge
        # has already produced its result; a receipt write failure
        # must not turn that into an error.
        try:
            self._persist_cost_receipt(
                run_id=result.run_id or run_id,
                wall_clock_seconds=time.time() - _judge_started_at,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            LOG.debug("judge_cost_receipt_persist_failed error=%r", exc)

        return result

    # ------------------------------------------------------------------
    # Prompt construction (split out for unit tests)
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        contract: Dict[str, Any],
        *,
        build_artifacts: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        """Return ``(system_prompt, user_prompt)`` for the judge call.

        Public via the test suite — the smoke test asserts every axis
        name appears in the system prompt. Splitting prompt
        construction out also keeps :meth:`judge` readable.

        ``build_artifacts`` — the post-synthesis enrichment dict
        (``{"dbt_tests": ..., "freshness": ..., "physical_layout": ...}``).
        When supplied, the user prompt includes them so the judge can
        credit performance / governance / documentation axes for
        fields the enrichment fills in even when the raw contract is
        sparse.
        """
        # Pretty-print contract as YAML — easier for the LLM to reason
        # about than minified JSON, and we already depend on pyyaml.
        try:
            contract_yaml = yaml.safe_dump(
                contract,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
                width=100,
            )
        except Exception:  # noqa: BLE001 — fall back to JSON
            contract_yaml = json.dumps(contract, indent=2, default=str, sort_keys=False)

        axes_block = "\n".join(f"- {name}: {_AXIS_DEFINITIONS[name]}" for name in self.AXES)

        system_prompt = (
            "You are a senior data-platform reviewer judging a finalised "
            "data-product contract against a 6-axis quality rubric.\n\n"
            "For EACH of these six axes, score the contract from 0 to 5:\n"
            "  0 = missing / not present\n"
            "  1 = severely deficient\n"
            "  2 = below acceptable threshold\n"
            "  3 = acceptable / minimum viable\n"
            "  4 = good\n"
            "  5 = excellent / production-ready\n\n"
            "Axes (with definitions):\n"
            f"{axes_block}\n\n"
            "RULES OF THE JUDGEMENT:\n"
            "1. For each axis, write your reasoning BEFORE assigning the score. "
            "Chain-of-thought reasoning improves correlation with human reviewers.\n"
            "2. Reasoning should be 1-2 sentences citing specific contract fields, "
            "not generic praise or criticism.\n"
            "3. Provide actionable suggestions when the score is below 5; emit an "
            "empty suggestions list when the axis is already excellent.\n"
            "4. Score every axis. Do not skip any. Do not invent extra axes.\n"
            "5. Respond with strict JSON only — no markdown narration outside the "
            "JSON block. Top-level keys MUST be exactly 'axes' (object keyed by "
            "the six axis names above).\n\n"
            f"{_ONE_SHOT_EXAMPLE}"
        )

        artifacts_block = _format_artifacts_block(build_artifacts)

        user_prompt = (
            "Judge this contract:\n\n"
            "```yaml\n"
            f"{contract_yaml}\n"
            "```\n\n"
            f"{artifacts_block}"
            "Return the full 6-axis JSON scorecard now."
        )

        return system_prompt, user_prompt

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse(self, raw: str, *, model: str, run_id: Optional[str]) -> JudgeResult:
        """Parse the LLM's response into a :class:`JudgeResult`.

        Robust to:
        * Markdown-fenced JSON (``safe_json_parse`` strips fences).
        * Extra surrounding prose (extracts the first balanced JSON block).
        * Missing optional ``suggestions`` field.

        Raises :class:`JudgeAgent.ParseError` on:
        * No parseable JSON.
        * Missing ``axes`` top-level key.
        * Missing any of the six required axes.
        * Non-integer / out-of-range scores.
        """
        from fluid_build.copilot.utils.json import safe_json_parse

        try:
            parsed = safe_json_parse(raw or "")
        except (json.JSONDecodeError, ValueError) as exc:
            LOG.debug("judge_parse_failed raw=%r", raw)
            raise JudgeAgent.ParseError("judge response was not valid JSON") from exc

        if not isinstance(parsed, dict) or "axes" not in parsed:
            LOG.debug("judge_parse_failed missing 'axes' key; raw=%r", raw)
            raise JudgeAgent.ParseError("judge response missing 'axes' top-level key")

        axes_raw = parsed.get("axes")
        if not isinstance(axes_raw, dict):
            LOG.debug("judge_parse_failed 'axes' not an object; raw=%r", raw)
            raise JudgeAgent.ParseError("judge response 'axes' was not an object")

        axes: Dict[str, AxisScore] = {}
        for axis_name in self.AXES:
            entry = axes_raw.get(axis_name)
            if not isinstance(entry, dict):
                LOG.debug("judge_parse_failed missing axis=%s; raw=%r", axis_name, raw)
                raise JudgeAgent.ParseError(f"judge response missing axis '{axis_name}'")
            score_raw = entry.get("score")
            try:
                score = int(score_raw)
            except (TypeError, ValueError) as exc:
                LOG.debug(
                    "judge_parse_failed axis=%s score=%r non-int; raw=%r",
                    axis_name,
                    score_raw,
                    raw,
                )
                raise JudgeAgent.ParseError(f"axis '{axis_name}' score was not an integer") from exc
            if score < 0 or score > 5:
                LOG.debug(
                    "judge_parse_failed axis=%s score=%d out of range; raw=%r",
                    axis_name,
                    score,
                    raw,
                )
                raise JudgeAgent.ParseError(f"axis '{axis_name}' score {score} out of range 0..5")
            reasoning = str(entry.get("reasoning") or "")
            suggestions_raw = entry.get("suggestions") or []
            if isinstance(suggestions_raw, str):
                # Be lenient: a single-string suggestion gets wrapped.
                suggestions = [suggestions_raw]
            elif isinstance(suggestions_raw, list):
                suggestions = [str(s) for s in suggestions_raw if s is not None]
            else:
                suggestions = []
            axes[axis_name] = AxisScore(
                score=score,
                reasoning=reasoning,
                suggestions=suggestions,
            )

        total = sum(a.score for a in axes.values())
        return JudgeResult(axes=axes, total=total, model=model, run_id=run_id)

    # ------------------------------------------------------------------
    # Self-critique (Gap 6 — Self-Refine-style second pass)
    # ------------------------------------------------------------------

    def _build_critique_prompt(
        self,
        initial: JudgeResult,
        contract: Dict[str, Any],
        *,
        build_artifacts: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        """Build the critique-pass (system, user) prompts.

        The system prompt re-uses the original 6-axis rubric definitions
        so the critique has the same evaluative frame; we just swap the
        instruction to "review your own scores critically". The user
        prompt embeds the initial axes + reasoning verbatim — DSPy-style
        backtracking, the model has explicit anchors to revise rather
        than re-deriving from scratch.

        Public-ish (no leading underscore on the unit-test contract):
        kept as a regular method so the test suite can pin both passes'
        prompt shapes without monkey-patching internals.
        """
        # YAML-pretty-print the contract (same as the initial pass) so
        # the critique can reason about the same artefact.
        try:
            contract_yaml = yaml.safe_dump(
                contract,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
                width=100,
            )
        except Exception:  # noqa: BLE001 — fall back to JSON
            contract_yaml = json.dumps(contract, indent=2, default=str, sort_keys=False)

        axes_block = "\n".join(f"- {name}: {_AXIS_DEFINITIONS[name]}" for name in self.AXES)

        # Build the "you scored this contract as follows" block — the
        # axes + reasoning from the initial pass, so the LLM sees its
        # own prior judgement laid out and revises in place.
        initial_review_lines: List[str] = []
        for axis_name in self.AXES:
            axis = initial.axes.get(axis_name)
            if axis is None:
                continue
            initial_review_lines.append(f"- {axis_name}: score {axis.score}/5")
            initial_review_lines.append(f"    reasoning: {axis.reasoning}")
        initial_review_block = "\n".join(initial_review_lines)

        system_prompt = (
            "You are reviewing your OWN previous judgement of a finalised "
            "data-product contract. Your task is to critically re-examine "
            "each axis score and decide whether your initial view holds "
            "up under scrutiny.\n\n"
            "Be willing to update scores when re-examination warrants it, "
            "but don't make changes for the sake of changes. A judge who "
            "tweaks every score on the second pass is no better than a "
            "judge who never reviews their work; a judge who flips one "
            "score from 2 to 4 after spotting a missed enrichment artifact "
            "is doing exactly what the second pass is for.\n\n"
            "For EACH of the six axes, output a score 0-5:\n"
            "  0 = missing / not present\n"
            "  1 = severely deficient\n"
            "  2 = below acceptable threshold\n"
            "  3 = acceptable / minimum viable\n"
            "  4 = good\n"
            "  5 = excellent / production-ready\n\n"
            "Axes (with definitions):\n"
            f"{axes_block}\n\n"
            "RULES OF THE CRITIQUE:\n"
            "1. For each axis where your view has changed, write 1-2 "
            "sentences citing what made you revise. For axes you stand "
            "by, restate the score and write 'stands as-is' as the "
            "reasoning so the audit trail shows you considered them.\n"
            "2. Respond with strict JSON only — same shape as the "
            "initial pass: top-level key is 'axes', object keyed by the "
            "six axis names above with {score, reasoning, suggestions}.\n"
            "3. Score every axis. Do not skip any. Do not invent extras.\n"
        )

        artifacts_block = _format_artifacts_block(build_artifacts)

        user_prompt = (
            "You previously scored this contract as follows:\n\n"
            f"{initial_review_block}\n\n"
            "The contract under review:\n\n"
            "```yaml\n"
            f"{contract_yaml}\n"
            "```\n\n"
            f"{artifacts_block}"
            "Review your own scores critically. Are there axes where you "
            "were too harsh or too lenient? Be specific. For each axis "
            "where your view has changed, provide the new score 0-5 with "
            "brief reasoning. For axes you stand by, restate them as-is.\n\n"
            "Return the full 6-axis JSON scorecard now."
        )

        return system_prompt, user_prompt

    def _run_self_critique(
        self,
        *,
        initial: JudgeResult,
        contract: Dict[str, Any],
        judge_config: Any,
        provider: Any,
        build_artifacts: Optional[Dict[str, Any]] = None,
    ) -> JudgeResult:
        """Run the critique pass; merge with the initial via the
        ``|Δ| > 1`` threshold rule; return the merged
        :class:`JudgeResult`.

        Merge rule (rationale in module docstring):

        * For each axis: if |critique_score - initial_score| > 1, adopt
          the critique score and reasoning (the judge has a meaningful
          change of mind, not a one-notch quibble).
        * Otherwise: keep the initial axis score + reasoning unchanged
          (avoid noise / over-tweaking).
        * In BOTH cases, append a ``_critique:`` annotation to the
          axis's reasoning so the audit trail shows what the second
          pass thought — even when the initial score wins.

        Critique-call failures (malformed JSON, provider blip) surface
        as exceptions to the caller (which fail-opens). The caller
        wraps this in try/except; we don't catch here.

        ``judge_config`` is shallow-replaced with the critique
        temperature so the original is left intact for the caller's
        subsequent telemetry inspection. ``extra_payload`` is the
        preferred override route — it sits on top of whatever the
        provider builds — so we use it instead of mutating the
        ``LlmConfig`` dataclass.
        """
        from fluid_build.cli.forge_copilot_llm_providers import call_llm

        system_prompt, user_prompt = self._build_critique_prompt(
            initial,
            contract,
            build_artifacts=build_artifacts,
        )

        # Lower the temperature for the critique. Industry consensus
        # (Patronus, Confident-AI, Praison best-practices) is 0.0-0.2
        # for deterministic LLM-as-judge re-scoring. We sit at 0.1 so
        # the critique can express a one-notch change without being
        # noise-driven. ``extra_payload`` overrides the build_request
        # default (which reads ``getattr(config, "temperature", 0.0)``).
        raw = call_llm(
            provider,
            judge_config,
            system_prompt,
            user_prompt,
            extra_payload={"temperature": _SELF_CRITIQUE_DEFAULT_TEMPERATURE},
        )

        critique_axes = self._parse_critique(raw)

        # Merge: per-axis threshold rule with audit-trail annotation.
        merged_axes: Dict[str, AxisScore] = {}
        axes_changed: List[str] = []
        before_total = initial.total
        for axis_name in self.AXES:
            initial_axis = initial.axes.get(axis_name)
            critique_axis = critique_axes.get(axis_name)
            if initial_axis is None:
                # Shouldn't happen — the initial pass already
                # validated all six axes — but if it does, prefer
                # whichever side has data.
                if critique_axis is not None:
                    merged_axes[axis_name] = critique_axis
                continue
            if critique_axis is None:
                # Critique didn't speak to this axis — preserve initial
                # verbatim, no annotation (nothing to annotate with).
                merged_axes[axis_name] = initial_axis
                continue
            delta = abs(int(critique_axis.score) - int(initial_axis.score))
            if delta > _AXIS_DELTA_ADOPTION_THRESHOLD:
                # Adopt critique. Annotation cites the original.
                annotated_reasoning = (
                    f"{critique_axis.reasoning}\n"
                    f"_critique: revised from {initial_axis.score} → "
                    f"{critique_axis.score} (initial reasoning: "
                    f"{initial_axis.reasoning!r})"
                )
                merged_axes[axis_name] = AxisScore(
                    score=int(critique_axis.score),
                    reasoning=annotated_reasoning,
                    # Prefer the critique's suggestions when adopted —
                    # they explain the new score. Fall back to initial's
                    # when the critique didn't emit any.
                    suggestions=(critique_axis.suggestions or initial_axis.suggestions),
                )
                axes_changed.append(axis_name)
            else:
                # Keep initial; annotate that the critique reviewed it.
                if critique_axis.score == initial_axis.score:
                    note = "stands as-is on review"
                else:
                    # delta ∈ {0, 1} but not 0 → critique nudged one
                    # notch; spec says don't over-tweak.
                    note = (
                        f"second-pass proposed {critique_axis.score} "
                        f"(within {_AXIS_DELTA_ADOPTION_THRESHOLD}-pt "
                        "threshold; initial retained)"
                    )
                annotated_reasoning = f"{initial_axis.reasoning}\n_critique: {note}"
                merged_axes[axis_name] = AxisScore(
                    score=int(initial_axis.score),
                    reasoning=annotated_reasoning,
                    suggestions=list(initial_axis.suggestions),
                )

        after_total = sum(a.score for a in merged_axes.values())

        merged = JudgeResult(
            axes=merged_axes,
            total=after_total,
            model=initial.model,
            run_id=initial.run_id,
            critique_applied=True,
            critique_summary={
                "axes_changed": axes_changed,
                "before_total": before_total,
                "after_total": after_total,
            },
        )
        LOG.info(
            "judge_critique_applied axes_changed=%s before=%d after=%d",
            axes_changed,
            before_total,
            after_total,
        )
        return merged

    def _parse_critique(self, raw: str) -> Dict[str, AxisScore]:
        """Parse the critique response into a ``{axis: AxisScore}`` map.

        Distinct from :meth:`_parse` because:

        * The critique is allowed to be partial (an axis may be missing
          if the LLM has "no change of mind" on it — we just keep the
          initial).
        * We never raise :class:`JudgeAgent.ParseError` from here; the
          caller's fail-open expects an exception type it can swallow,
          and ``ParseError`` would re-poison the initial result through
          the merge path. We raise ``ValueError`` instead and let the
          caller's broad except handle it.
        * Out-of-range / non-int scores are also tolerated by dropping
          that single axis from the merge (the initial wins for that
          axis). This is more permissive than the initial pass on
          purpose: the critique is observability-grade quality juice,
          not a hard gate.
        """
        from fluid_build.copilot.utils.json import safe_json_parse

        try:
            parsed = safe_json_parse(raw or "")
        except (json.JSONDecodeError, ValueError) as exc:
            LOG.debug("judge_critique_parse_failed raw=%r", raw)
            raise ValueError("judge critique response was not valid JSON") from exc

        if not isinstance(parsed, dict) or "axes" not in parsed:
            LOG.debug("judge_critique_parse_failed missing 'axes' key; raw=%r", raw)
            raise ValueError("judge critique response missing 'axes' top-level key")

        axes_raw = parsed.get("axes")
        if not isinstance(axes_raw, dict):
            LOG.debug("judge_critique_parse_failed 'axes' not an object; raw=%r", raw)
            raise ValueError("judge critique response 'axes' was not an object")

        out: Dict[str, AxisScore] = {}
        for axis_name in self.AXES:
            entry = axes_raw.get(axis_name)
            if not isinstance(entry, dict):
                # Missing axis is permitted — initial wins on merge.
                continue
            score_raw = entry.get("score")
            try:
                score = int(score_raw)
            except (TypeError, ValueError):
                LOG.debug(
                    "judge_critique_axis_skipped axis=%s reason=non-int-score raw=%r",
                    axis_name,
                    score_raw,
                )
                continue
            if score < 0 or score > 5:
                LOG.debug(
                    "judge_critique_axis_skipped axis=%s reason=out-of-range score=%d",
                    axis_name,
                    score,
                )
                continue
            reasoning = str(entry.get("reasoning") or "")
            suggestions_raw = entry.get("suggestions") or []
            if isinstance(suggestions_raw, str):
                suggestions = [suggestions_raw]
            elif isinstance(suggestions_raw, list):
                suggestions = [str(s) for s in suggestions_raw if s is not None]
            else:
                suggestions = []
            out[axis_name] = AxisScore(
                score=score,
                reasoning=reasoning,
                suggestions=suggestions,
            )
        return out

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, result: JudgeResult, *, run_id: Optional[str]) -> None:
        """Write ``judge.json`` under the run's receipts directory.

        Best-effort: the caller wraps this in a defensive except. Run-id
        resolution falls through (1) explicit kwarg → (2)
        :func:`get_or_create_run_id` (env var / persisted file /
        freshly generated). If we can't resolve a run_id at all, the
        write is skipped silently (judge result is still returned to
        the caller).
        """
        resolved_run_id = run_id
        if not resolved_run_id:
            try:
                from fluid_build.observability.run_id import get_or_create_run_id

                # ``create_persisted_file=False`` — judge runs out of
                # loop, never the first stage. If no run-id exists yet
                # we shouldn't conjure one and pollute .fluid/run-id.txt.
                resolved_run_id = get_or_create_run_id(create_persisted_file=False)
            except Exception:  # noqa: BLE001 — defensive
                resolved_run_id = None
        if not resolved_run_id:
            LOG.debug("judge_persist_skipped reason=no_run_id")
            return

        # Stamp the resolved id back onto the result so the persisted
        # file matches the in-memory object the caller holds.
        result.run_id = resolved_run_id

        # Resolve the receipts directory. ``agent_run_dir`` returns
        # ``<workspace>/.fluid/agents/<run-id>``; create parents
        # (mkdir(parents=True, exist_ok=True)).
        from fluid_build.paths import agent_run_dir

        target_dir = agent_run_dir(resolved_run_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "judge.json"
        target_file.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        LOG.debug("judge_persist_wrote path=%s", target_file)

    def _persist_cost_receipt(
        self,
        *,
        run_id: Optional[str],
        wall_clock_seconds: float,
    ) -> None:
        """Write ``cost.json`` alongside ``judge.json`` under the run dir.

        Mirrors :func:`fluid_build.cli.forge_data_model._persist_run_cost_receipt`
        for the judge-only entry point. Without this, a forge invocation
        that only runs the judge (e.g. a quality-gate workflow that
        critiques an externally-authored contract) records cost in the
        ``RunCostTracker`` singleton but never writes the on-disk
        receipt — so ``fluid stats`` can't see the run.

        Run-id resolution falls through the same ladder as
        :meth:`_persist` so a judge run that didn't get an explicit
        ``run_id`` still lands its receipt next to the in-memory
        :class:`JudgeResult.run_id`. When no run-id can be resolved
        we skip silently (the judge result is still returned).
        """
        resolved_run_id = run_id
        if not resolved_run_id:
            try:
                from fluid_build.observability.run_id import get_or_create_run_id

                resolved_run_id = get_or_create_run_id(create_persisted_file=False)
            except Exception:  # noqa: BLE001 — defensive
                resolved_run_id = None
        if not resolved_run_id:
            LOG.debug("judge_cost_receipt_skipped reason=no_run_id")
            return
        from fluid_build.copilot.cost import get_run_tracker
        from fluid_build.paths import agent_run_dir

        target_dir = agent_run_dir(resolved_run_id)
        cost_path = get_run_tracker().persist_to_run_dir(
            target_dir,
            wall_clock_seconds=wall_clock_seconds,
        )
        LOG.debug("judge_cost_receipt_wrote path=%s", cost_path)


__all__ = [
    "AxisScore",
    "JudgeAgent",
    "JudgeResult",
]
