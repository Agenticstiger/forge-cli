# RFC: Mission-based deep agents — goal-driven autonomous missions

> Status: PROPOSED (2026-07-18). Zero contract-schema changes — composes existing
> copilot/agent machinery; see 'v1 scope' for the 2-PR plan.

**Status:** Proposed · **Scope:** `fluid_build/` only (docs go to forge_docs) · **Target:** 2 PRs

## Motivation

forge-cli today has two agent surfaces and a gap between them. Domain agents
(`cli/agent_specs/*.yaml` + `DeclarativeDomainAgent`) are declarative but
non-autonomous — they shape the interview and prompt, call no tools, run no
loop. The staged coordinator (`copilot/agents/coordinator.py`) is autonomous
but single-shot: it synthesizes one contract and stops. Nothing today can take
a *goal* — "make this product GDPR-compliant", "reach full DQ-rule coverage" —
and iterate against it until deterministic evidence says it is met.

A **mission** fills that gap: a declarative YAML spec stating a goal, a set of
*deterministically verifiable* success criteria, budgets, and gates, executed
by a thin outer loop (`MissionRunner`) that composes exclusively from existing
machinery. The load-bearing design decision, kept from every reviewed variant,
is the **termination inversion**: mission completion is decided only by a
registry of code-owned checks re-run against the re-read, re-hashed on-disk
contract. The LLM plans and proposes; it can never declare done. The corollary
is **resume-by-re-verification**: because VERIFY is idempotent, the scorecard
is both the termination authority and the resume pointer — a paused, stalled,
crashed, or stale run always re-enters at VERIFY with zero replay machinery.

## Design overview

The existing `run_copilot_agent_loop` stays the bounded **inner executor**
(12-iteration cap, one contract JSON out). The mission runner is an **outer**
verification loop:

```
load MissionSpec → trust-check spec → open/resume run dir → loop (≤ max_iterations):
  1. VERIFY   — re-read contract from disk, re-hash, run all checks → scorecard
                (persisted, digest-bound to the contract hash). All non-advisory
                checks pass → status "complete", receipt, done.
  2. PLAN     — one cheap LLM call (BaseStageAgent, tier=fast): failing checks'
                REDACTED diagnostics + plan_hint → ordered step list.
  3. EXECUTE  — per step: a deterministic helper (enforce_ai_ready,
                enrich_contract) when one maps directly; else the inner agent
                loop with an explicit tool allowlist and step-scoped goal
                (new parameters — see "honest surgery" below).
  4. GATE     — diff old→new contract; destructive classifier (fail-closed:
                unknown diff shapes are destructive) → interactive confirm.
                Non-TTY or gates.destructive: deny → reject the step outright
                and emit mission_destructive_gate_rejected. --yes never
                approves a destructive diff.
  5. PROGRESS — improvement metric = count of passing non-advisory checks.
                Not strictly increased for 2 consecutive iterations → pause
                (reason "stalled"). Budget ceiling hit → pause (reason
                "budget"). Both are manifest status "paused" + pause_reason.
```

Three properties fall out of this shape:

- **No LLM-faked success.** Checks wrap existing CLI internals (never parallel
  implementations, pinned by tests that run both paths on the same contract),
  and they run against the on-disk artifact, not the model's claim.
- **Self-healing for free.** Failing-check diagnostics are recycled verbatim
  (post-redaction) as the next cycle's repair feedback — the same shape as the
  existing `build_corrective_messages` loop, so verification failure *is* the
  repair prompt.
- **Anti-gaming.** The destructive gate lands in the same PR as the executor,
  so the LLM cannot satisfy a criterion by deleting the columns it was asked
  to fix.

**Borrow receipts** (searched before designing): plan-do-check-act machine-state
YAML (Fmind "Agent Levers"), typed verification gates (ksimback/looper),
spec-first acceptance criteria (SDD), goal/verification loop framing
(MindStudio). Nothing importable as a dependency; the borrowed *shape* —
machine-state file + deterministic gates + pointer-advance resume — maps onto
`FileCheckpointStore` we already ship. LangGraph/Temporal were considered and
rejected: file-granularity durable resume already exists, and the startup-budget
test (`tests/perf/test_startup_budget.py`) forbids heavy runtimes on the cold
path.

## Contract-spec surface

Frozen dataclass `MissionSpec` in `cli/forge_mission_spec.py`, loader mirroring
`forge_agent_specs.py` conventions: built-ins in `cli/mission_specs/*.yaml`,
user missions from `.fluid/missions/` then `~/.fluid/missions/` (same
`_user_agent_dirs()` shadowing), `scaffold_user_mission()` analog. **No
contract-schema changes**; missions are workspace artifacts, reversible by
deletion.

v1 ships **three check types** — `validate`, `ai_ready`, `predicate` — enough
to express both flagship missions and teach the whole model. (`agent_policy`,
`judge`, and a live-state `verify` check are v2; see scope.) Built-in
`cli/mission_specs/gdpr_clean.yaml`:

```yaml
name: gdpr-clean
description: Make a data product GDPR-compliant end to end.
goal: >
  Every PII column is classified with provenance, every MCP-exposed port
  carries an agentPolicy with retention limits, and the contract validates.
success_criteria:                 # ALL non-advisory checks must pass; each deterministic
  - check: validate               # in-process cli/validate stage, exit-0 semantics
  - check: ai_ready
    require: {sensitive_exposes_annotated: true, missing_descriptions: 0}
  - check: predicate              # dotted-path + [*] over the contract dict; nothing more
    path: "exposes[*].policy.agentPolicy.retentionPolicy.maxRetentionDays"
    op: lte
    value: 30
budgets:
  max_usd: 5.00                   # plumbed into RunCostTracker per-product HARD ceiling
  max_iterations: 6               # outer loops (inner loop keeps its 12)
  max_wall_seconds: 1800          # deadline checked before every LLM/tool call;
                                  # remaining time passed as the per-call timeout
gates:
  destructive: ask                # ask | deny; non-TTY ⇒ deny (fail closed)
tools:
  allow: [discover_workspace, read_sample_schema, check_pii_classification,
          validate_contract, propose_contract]
plan_hint: [inspect, classify_pii, stamp_policies]   # ordering hint only; the
                                  # planner may reorder or drop, never add tools
```

Second built-in: `quality_coverage.yaml` (predicate on dq-rule coverage ratio +
`validate`). The previously drafted `rag_ready.yaml` is **cut from v1**: it
referenced a `judge` axis (`ai_readiness`) that does not exist in the
test-pinned `JudgeAgent.AXES`, and a `min_score: 0.85` that mismatches
`AxisScore`'s integer 0–5 scale. When `judge` lands in v2 it will validate
`axis` against `JudgeAgent.AXES` at spec-load time and take an integer
`min_score`; a spec whose only criteria are judge checks fails validation at
load (LLM is never the sole gate).

The predicate mini-language is deliberately frozen: dotted paths, `[*]` array
fan-out, ops `{eq, ne, lt, lte, gt, gte, exists, contains}`. No filters, no
functions. Requests for more get pointed at v2's plugin checks — mini-languages
never stay lite, so we refuse to grow this one.

## Architecture & files touched

New module `copilot/mission/` (mirrors `copilot/agents/` placement):

- `runner.py` — `MissionRunner` + `MissionState`; reuses `StageSession`,
  `new_run_id()`, `get_or_create_run_id`.
- `checks.py` — `MISSION_CHECKS` dict + `register_mission_check(name, fn)`
  (exact `IAC_PLUGINS` registry shape). Each check returns
  `CheckResult{name, passed, advisory, detail, diagnostics}`. **Every**
  `detail`/`diagnostics` string — built-in or plugin — passes through the
  secret redactor before persistence or LLM exposure; the PRs #28–#33
  invariant (exception text never round-trips into LLM context) applies to the
  whole registry, not just third-party checks.
- `destructive.py` — pure-function contract-diff classifier. v1 taxonomy is
  intentionally coarse and fail-closed: any column/port/`consumes[]` removal,
  any type narrowing, any policy loosening (retention shrink, allowlist
  widening), and **any diff shape the classifier does not recognize** classify
  destructive. Pinned test matrix; refinement is v2.

**Honest surgery on the inner loop.** The prior draft claimed EXECUTE could
scope the inner loop "for free". It cannot: `run_copilot_agent_loop` takes no
tools filter (tools are `get_tool_definitions()` unconditionally), has a fixed
full-synthesis system prompt, and seed-contract injection lives in
`forge_copilot_runtime`, not the loop. v1 therefore adds two explicit,
default-preserving parameters — `tool_allowlist: list[str] | None` (filtered
against `FORGE_TOOL_REGISTRY`, inheriting the `workspace_root` confinement)
and `goal_scope: str | None` (appended step framing) — to
`forge_copilot_agent_loop.py`, with seed plumbing via the existing
`_seed_contract_override` seam in `forge_copilot_runtime.py`. Both files are in
the touched list. Because each EXECUTE step still runs a full inner loop, the
$5 default budget is honest only with per-call enforcement (below) and the
deterministic-helper fast path taken whenever a step maps to
`enforce_ai_ready`/`enrich_contract`.

**Budgets are hard, cumulative, and enforced at the call site.**
`spec.budgets.max_usd` is plumbed into `RunCostTracker`'s existing per-product
ceiling mechanism (the same enforcement path as
`FLUID_COST_LIMIT_USD_PER_PRODUCT`) under the product scope
`mission:<name>`, so **every LLM call** inside the inner loop is capped —
not just outer-iteration boundaries. Effective cap = min(spec, env). Spend is
re-summed on resume from on-disk per-run receipts, never trusted from mutable
state, so budgets are cumulative across pause/resume. `max_wall_seconds` has a
concrete enforcement point: the runner computes the deadline at start, checks
it before every step and every check, and passes the remaining time as the
per-call timeout to LLM/tool invocations (same posture as
`FLUID_TOFU_TIMEOUT_SECONDS`). A hung call can therefore overshoot by at most
one call's timeout — stated, not hidden.

**Checkpointing.** No new store. `FileCheckpointStore` writes under
`.fluid/agents/<run-id>/checkpoints/`; the manifest gains additive optional
fields `{mission_spec, mission_goal, criteria_status, pause_reason,
mission_spec_sha256}`. Status values stay within the documented literal set
(`running|paused|complete|failed`) — "stalled" and "budget" are `pause_reason`
values on a `paused` status, so `_forge_resume.py` and the welcome scan's
paused-run filters keep working unmodified. Because `CheckpointStore.put()`
only flips `complete` on the coordinator's `_LAST_STAGE`, `MissionRunner`
**explicitly writes** manifest status `complete`/`failed` at termination —
finished missions do not linger as resumable.

**Attestation.** A green scorecard is a compliance-flavored artifact, so it is
digest-bound: each persisted scorecard carries the sha256 of the exact contract
it verified (reusing the canonical-hash helper behind `StaleContractError`;
same pattern as `forge/core/plan_digest.py`). `fluid mission status` marks a
scorecard STALE when the on-disk contract no longer matches. Concurrency inside
a live run is handled the same way: the runner re-hashes immediately before
every write and aborts the step (re-entering VERIFY) if the contract changed
out-of-band — `StaleContractError` semantics extended from pause/resume to the
per-iteration read-modify-write window.

**Gate mechanics.** The confirm primitive is the module-level function in
`cli/_preview_panel.py` (not a `PreviewPanel` method), and it is fail-open on
non-TTY — unacceptable for a destructive gate. v1 adds
`confirm_fail_closed()`: non-TTY returns False and emits
`mission_destructive_gate_rejected` (audit-WARNING, same posture as the
OpenTofu `--allow-data-loss` event). `--yes` skips *non-destructive* previews
only. There is no `FLUID_MISSION_AUTO_APPROVE` env var — one knob (`--yes`),
one documented rule ("`--yes` never approves destructive diffs"), which we
accept will still generate "why is it prompting?" reports; the alternative
(silent destructive approval in CI) is worse.

**Files touched (v1).** New: `cli/forge_mission_spec.py`,
`cli/mission_specs/{gdpr_clean,quality_coverage}.yaml`,
`copilot/mission/{__init__,runner,checks,destructive}.py`, `cli/mission.py`.
Modified: `cli/forge.py` (deferred subcommand wiring),
`cli/forge_copilot_agent_loop.py` + `cli/forge_copilot_runtime.py` (allowlist +
goal-scope + seed plumbing), `copilot/checkpoint.py` (optional manifest
fields), `cli/_preview_panel.py` (`confirm_fail_closed`), `cli/agents_cmd.py`
(scorecard render), `cli/_forge_resume.py` (mission-aware prompt text). Tests:
`tests/copilot/mission/test_{spec,checks,destructive,runner}.py`,
`tests/test_mission_cli.py`, plus a startup-budget run — `cli/mission.py`
follows the PEP 562 `__getattr__` + module-self-indirection +
`lru_cache(maxsize=1)` pattern.

## v1 scope (2 PRs) vs v2+

The earlier 3–4-PR plan fails the ~2-PR bar; this is the cut-list that reaches
it. **Cut from v1:** the `judge` check (broken axis reference; advisory-only
value), the `verify` check (it shells a post-apply live-state reconciliation
stage that is meaningless in a pure authoring mission), the `agent_policy`
check (expressible today as `validate` + predicates), the
`fluid_build.mission_checks` entry_points group (raises stakes from suggestion
to attestation with no provenance story — needs its own design), the mode-picker
6th entry and `fluid forge --mission` flag (one entry point until demand is
proven), welcome-scan surfacing, and taxonomy refinement of the destructive
classifier.

- **PR 1 — spec + checks + gate (no LLM, no loop).** `MissionSpec` + loader +
  trust pinning; `MISSION_CHECKS` with the three built-ins; destructive
  classifier + `confirm_fail_closed`; `fluid mission list` and
  `fluid mission check <name> --contract PATH` (runs VERIFY standalone —
  independently useful as a CI gate, and it proves the check registry before
  any autonomy exists).
- **PR 2 — runner + CLI.** `MissionRunner`, inner-loop allowlist/goal-scope
  parameters, budget plumbing, manifest fields, `fluid mission run|status`,
  resume via `_forge_resume.py`.

**v2+:** `judge` (with load-time axis validation and integer scores),
`agent_policy`, entry_points plugin checks with provenance, mode-picker entry,
welcome-scan surfacing, classifier taxonomy, cross-product missions (walk
`consumes[]`, reuse the replay-marker pattern), scheduled re-verification via
`_forge_watch.py` (verify-only and network-free, and now safe: the
non-interactive destructive gate is fail-closed by construction), coding-agent
executor (route EXECUTE through `CodingAgentProvider` agentic mode), ACP
transport (rides the `_run_agent` seam, official `agent-client-protocol` SDK).

## Migration & compatibility

Purely additive. `agent_specs`, coordinator, inner agent loop
(new parameters default to today's behavior, guarded by existing tests),
checkpoint format (old manifests load fine; new fields optional), receipts,
and all env vars are untouched. Rollback = revert; user mission YAMLs become
inert files. New env vars: `FLUID_MISSION_TIMEOUT_SECONDS` (override
`max_wall_seconds`), added to the `fluid doctor` dump. Both PRs go through the
standing borrow→security-review→live-test gate before opening.

## Security & governance

**Workspace specs are a new trust boundary — treated as such.** Unlike
`agent_specs` (prompt-shaping only), a mission spec configures autonomous
execution: tool allowlist, gate mode, budgets, and goal text injected into the
planner LLM. A cloned repo shipping `.fluid/missions/` must not silently
control any of that. v1 rule, in PR 1: workspace-local specs require first-run
approval, pinned by content hash in `~/.fluid/mission_trust.json` (direnv-style
allow); a changed spec re-prompts; non-TTY with an unpinned/changed spec
refuses to run with a structured event. Built-ins and `~/.fluid/missions/`
(user-authored, outside any repo) are trusted implicitly. `fluid mission run
<path>` on an unpinned file gets the same prompt — closing the CI abuse path.

Further posture: authoring-side vs consuming-side (`agentPolicy`) stays
cleanly separated — zero `$defs/agentPolicy` or schema changes; all mutations
pass `_forge_ai_guardrails` (connection/secretRef/sovereignty hard-blocks
untouched by any mission); tool access is `spec.tools.allow ∩
FORGE_TOOL_REGISTRY` with `workspace_root` confinement inherited;
check output is redacted before persistence and before any LLM sees it;
scorecards are digest-bound; destructive gates fail closed on non-TTY and on
unclassified diffs. Residual Goodhart risk is real and stated: metadata checks
can be satisfied in the letter (stamp `maxRetentionDays: 30` everywhere) while
degrading unstated properties. Mitigations — spec authors own criteria quality,
every gate/receipt is auditable, and the v2 `judge` check adds a holistic
advisory signal — reduce but do not eliminate it. Missions verify metadata
truthfully; they do not certify semantics.

## Open questions

1. **Should PR 1's `fluid mission check` be marketed as a standalone CI gate?**
   Recommendation: yes, quietly — it is the zero-LLM half of the feature,
   de-risks the check registry, and gives CI users value even if they never
   run autonomous missions. Document it; don't build extra surface for it.
2. **Trust pinning UX for teams** — per-user hash pinning means every teammate
   approves every spec change. Recommendation: accept for v1 (matches direnv,
   and specs change rarely); revisit signed/committed trust manifests only if
   real friction is reported.
3. **Deterministic-helper routing table** — hardcode the two v1 mappings
   (`enforce_ai_ready`, `enrich_contract`) in the runner, or make them a
   registry? Recommendation: hardcode. Two entries do not justify a registry;
   extract one when v2's check plugins force the question.

## Risks (honest)

(1) Budget overshoot bounded but nonzero: per-call ceiling + wall-clock
timeout still allow one in-flight call past the line. (2) Coarse fail-closed
classifier will over-prompt on legitimate removals; v1 accepts friction over
false negatives. (3) The predicate DSL will attract extension requests; the
freeze is policy, and policy gets argued with. (4) Two flagship missions is a
thin catalog; better than shipping a spec that references APIs that don't
exist. (5) Stall metric (count of passing non-advisory checks) can plateau
legitimately mid-refactor; pause-with-reason and one-command resume is the
mitigation, not a solution.
