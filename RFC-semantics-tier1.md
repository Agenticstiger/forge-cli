# RFC: Tier-1 semantic-layer correctness — cumulative metrics, structured filters, time spine, relationships, SCD validity

**Status:** Proposed (2026-07-18) · **Target schema:** 0.7.6 (preview, per `schema_manager.PREVIEW_VERSIONS`) · **Scope:** `$defs.semanticModel` + its three consumers (MCP query compiler, dbt MetricFlow bridge, dbt manifest importer) + validator lint

---

## Motivation

The 2026-07-18 semantics audit benchmarked `exposes[].semantics` against dbt
MetricFlow, Cube, Snowflake semantic views, and Apache Ossie. Verdict: a
credible, well-chosen MetricFlow subset — with five gaps that either produce
**wrong numbers** or make the most-requested BI metrics **inexpressible**:

1. **No cumulative/rolling metrics.** Running totals, trailing-28-day, MTD/QTD
   are among the first metrics real BI teams ask for; dbt treats `cumulative`
   as a first-class type and Cube covers the same need with `rolling_window`.
   Today the contract simply cannot say "revenue, trailing 28 days".
2. **Filters are raw SQL strings.** A consumer can't validate that a filter
   references real semantic objects, can't bind values as parameters, and
   can't rewrite it per dialect. The governed MCP path now fails closed on
   unsafe strings (#439) — correct, but "fail closed" on a field the producer
   can't express safely is a modeling gap, not a solution.
3. **No time spine / null-filling semantics.** Without gap-filling against a
   calendar, "0 orders on Tuesday" silently disappears from time series and
   derived metrics divide by NULL. dbt makes both per-metric knobs
   (`fill_nulls_with`, `join_to_timespine`) backed by the MetricFlow time
   spine — the dbt bridge already **emits** a spine model (#426) but the
   contract has no way to ask for it.
4. **No join-path/cardinality control.** Entities imply a join graph but
   nothing declares cardinality or disambiguates paths — the fan-out
   misaggregation problem semantic layers exist to solve. Snowflake semantic
   views make `relationships` first-class; Ossie's core spec is built around
   them. The MCP compiler currently ignores entities entirely partly because
   there is nothing safe to join on.
5. **No SCD validity windows.** The `natural` entity type was copied from
   dbt-semantic-interfaces *for* slowly-changing dimensions, but without
   `validity_params` a point-in-time-correct dimension join is impossible —
   half the feature was imported.

All five shapes below are **borrowed, not invented**: field-for-field from
dbt-semantic-interfaces where it has the concept (cumulative, fill-nulls,
validity), from Snowflake semantic views + Ossie where dbt is weaker
(relationships), and from the same camelCase convention the block already
uses. That keeps the MetricFlow bridge near-mechanical and keeps fluid
aligned with where the Ossie metric-semantics working group is heading
(grain/filters/cumulative are its active deliverables).

## Design overview

Additive and preview-gated: every new field is optional, lands only in
`fluid-schema-0.7.6.json` (0.7.5 GA untouched), and carries the "NEW in
v0.7.6" description marker. Absent fields change nothing — no digest churn
for existing contracts, `--shadow` parity holds. Consumers follow the
existing posture split: the **MCP compiler is strict** (unsupported → clear
`QueryValidationError`, never wrong numbers), the **dbt bridge degrades
loudly** (skip + log, never emit YAML `dbt parse` rejects), the **importer
recovers** what the schema can hold and reports what it can't.

**Honest surface accounting:** this is five features, not one, and two of
them (cumulative, relationships) eventually need real compiler work on the
MCP side. The RFC therefore splits every feature into *schema + bridge +
importer* (cheap, mechanical, this wave) and *MCP compilation* (phase-2 per
feature, explicitly gated, fail-closed until it lands). A schema field whose
governed-query behavior is "clear error, use the dbt path" is still honest;
a field that silently returns wrong numbers is not — that rule is what
decides every v1 behavior below.

---

## 1. Cumulative metrics

```yaml
metrics:
  - name: revenue_trailing_28d
    type: cumulative              # NEW enum value (simple | derived | ratio | cumulative)
    measure: revenue
    typeParams:                   # NEW object, cumulative-only
      window: "28 days"           # count + grain; mutually exclusive with grainToDate
      # grainToDate: month        # month-to-date style accumulation
      # periodAgg: first|last|average   # how the accumulated value projects onto coarser grains (dbt >= 1.9)
```

Mirror of dbt's `cumulative` `type_params` (`window`, `grain_to_date`,
`period_agg`). `window` is `"<count> <grain>"` with the grain validated
through the shared `forge_datamodel/time_grains.py` vocabulary; `window` and
`grainToDate` are mutually exclusive (schema `oneOf`).

- **dbt bridge:** map to `type: cumulative` + `type_params` — mechanical;
  the emitted time spine (#426) already satisfies MetricFlow's requirement.
- **Importer:** the `cumulative` skip note added in #442 flips to a real
  mapping — imported dbt projects stop losing their rolling metrics.
- **MCP compiler:** phase-2 (window self-join / range frame per engine).
  v1 keeps the existing explicit rejection, message updated to name the dbt
  path as the executable consumer.
- **Validator lint:** `measure` must exist; window grain must normalize.

## 2. Structured filters (`where`)

```yaml
metrics:
  - name: completed_revenue
    type: simple
    measure: revenue
    where:                        # NEW: structured, ANDed conditions
      - dimension: status         # must be a declared dimension or schema column
        op: eq                    # eq | neq | gt | gte | lt | lte | in | notIn | isNull | isNotNull
        value: completed          # scalar, or `values: [...]` for in/notIn
    # filter: "status = 'completed'"   # legacy string form stays as the escape hatch
```

The raw-string `filter` stays (escape hatch + dbt-import round-trip), but
`where` becomes the preferred form and consumers prefer it when both are
present (lint warns on redundancy).

Why this beats adopting dbt's `{{ Dimension('...') }}` template syntax
directly: the structured form is **machine-checkable** (the referenced
dimension must exist — a lint error, not a runtime surprise), **bindable**
(the MCP compiler passes `value` as a bound parameter through the existing
`:p_<n>` machinery instead of interpolating SQL — strictly safer than any
string filter), and **renderable** into dbt's template syntax mechanically
(`{{ Dimension('<model>__<dim>') }} = 'completed'`), so nothing is lost on
the dbt side.

- **MCP compiler:** v1 support (this is the easy, high-value one) — compile
  `where` to parameterized predicates; `in`/`notIn` reuse the rowFilter
  placeholder-list pattern.
- **dbt bridge:** render to the template-syntax filter string.
- **Importer:** dbt filters arrive as `where_sql_template` strings → keep
  landing in `filter` (no lossy up-conversion guessing).
- **Validator lint:** unknown `dimension` reference = error; that is the
  referential check the audit flagged as missing.

## 3. Time spine + null filling

```yaml
metrics:
  - name: daily_orders
    type: simple
    measure: order_count
    joinToTimespine: true         # NEW: project onto the calendar spine
    fillNullsWith: 0              # NEW: number substituted for gaps
```

Field-for-field dbt (`join_to_timespine`, `fill_nulls_with` on the metric's
input measure). `fillNullsWith` implies `joinToTimespine`.

- **dbt bridge:** map into the metric's measure reference
  (`type_params.measure: {name, join_to_timespine, fill_nulls_with}`) —
  the emitted spine model already exists.
- **MCP compiler:** phase-2 (calendar generation is engine-specific:
  `generate_series` on duckdb/postgres, `SEQUENCE`/`UNNEST` elsewhere). v1:
  reject with a clear error naming the dbt path.
- **Importer:** recover both fields from imported metrics.

## 4. Relationships (join-path + cardinality)

```yaml
semantics:
  relationships:                  # NEW top-level key, sibling of entities
    - name: orders_to_customers
      to: customer_profiles      # target exposeId (same contract, v1)
      fromColumns: [customer_id]
      toColumns: [id]
      cardinality: many_to_one    # many_to_one | one_to_one
      joinType: left              # left | inner   (default left)
```

Shape borrowed from Snowflake semantic views (named relationships with
explicit cardinality) and Ossie's core `Relationship` (ordered
`from_columns`/`to_columns`, composite keys supported); deliberately NOT
from MetricFlow, which infers joins from entity types and gives producers no
disambiguation control — the exact gap being fixed.

- **Validator lint:** `to` must be an expose in the contract; column arrays
  same length, non-empty; columns must exist in the respective schemas —
  all referential checks, all v1.
- **MCP compiler:** phase-2 (cross-expose joins on the governed path — the
  feature that finally makes `entities` consumable). v1: informational.
- **dbt bridge:** v1 informational (MetricFlow derives joins from entities);
  lint cross-checks that every relationship endpoint has a matching entity
  declaration, catching entity/relationship drift at authoring time.
- **Ossie interchange:** maps 1:1 onto Ossie `relationships` — the sidecar
  emitter gains real relationship fidelity instead of deriving them from
  entity naming.

## 5. SCD validity params

```yaml
dimensions:
  - name: valid_from
    type: time
    typeParams:
      timeGranularity: day
      validityParams: {isStart: true}   # NEW
  - name: valid_to
    type: time
    typeParams:
      timeGranularity: day
      validityParams: {isEnd: true}     # NEW
```

Field-for-field dbt `validity_params`. Requires the model to declare a
`natural` entity (lint error otherwise — the two features are one mechanism,
which is the audit's point).

- **dbt bridge:** mechanical map to `validity_params` — MetricFlow then does
  point-in-time-correct SCD joins.
- **Importer:** recover from imported dimensions.
- **MCP compiler:** ignored in v1 (documented); phase-2 with relationships.

---

## Consumer impact matrix (v1 of this RFC)

| Feature | Schema 0.7.6 | Validator lint | dbt bridge | Importer | MCP compiler |
|---|---|---|---|---|---|
| cumulative metrics | ✓ | measure ref + window grain | ✓ full map | ✓ (drops #442 skip) | reject w/ clear error (phase-2) |
| structured `where` | ✓ | dimension ref = error | ✓ render to template filter | keeps `filter` string | **✓ compile, bound params** |
| time spine / fill nulls | ✓ | — | ✓ full map | ✓ | reject w/ clear error (phase-2) |
| relationships | ✓ | expose/column refs = error | informational + entity cross-check | ✓ (from entity graph where present) | informational (phase-2 joins) |
| SCD validity | ✓ | requires natural entity | ✓ full map | ✓ | ignored, documented |

Phase-2 (MCP cumulative/timespine/joins) is its own follow-up with its own
RFC-level design — it changes the compiler from single-table aggregation to
multi-table windowed compilation and must not be rushed in under this one.

## Compatibility

- Additive only; every field optional; `additionalProperties: false`
  preserved on every touched object (the self-healing repair loop depends on
  precise schema errors).
- 0.7.5 GA untouched. Preview-gated exactly like `aggParams` (#439) and the
  packaging block: untagged contracts never resolve into preview versions.
- The dbt bridge's `dbt parse` acceptance gate (real dbt-core 1.11 in the
  live test) is the hard proof for every bridge mapping, same as #444's
  governance meta.
- Round-trip invariant extended: a dbt project imported (#442) and
  re-generated must not lose cumulative metrics, validity params, or
  fill-nulls settings once this lands — pinned in the importer test matrix.

## Out of scope (deliberately)

- **Conversion metrics** (entity-level event matching) — dbt's fifth type;
  needs its own design pass.
- **Saved/verified queries, synonyms, sample values, AI-grounding fields** —
  Tier-2 of the audit; a separate RFC, likely aligned with Ossie's
  `ai_context` / `verified_queries` direction rather than invented here.
- **`meta`/extensions carve-out** on semantic objects — decided separately
  (interacts with import fidelity more than with correctness).
- **Dimension hierarchies, custom calendars, sub-day spine grains.**

## References

- dbt cumulative/fill-nulls/validity/saved-query shapes:
  docs.getdbt.com/docs/build/metrics-overview, /build/measures,
  /build/dimensions, /build/metricflow-time-spine
- Snowflake semantic view YAML (named relationships, filters):
  docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec
- Apache Ossie core spec + metric-semantics WG roadmap:
  github.com/apache/ossie (ROADMAP.md; discussions #29, #39, #5, #50)
- Internal grounding: 2026-07-18 semantics audit (produce/consume matrix +
  benchmark), #426 (MetricFlow bridge), #439 (filter fail-closed +
  aggParams), #440 (shared builder), #442 (importer), #444 (governance
  round-trip).
