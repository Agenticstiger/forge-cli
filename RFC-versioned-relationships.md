# RFC: Versioned tables and the relationships that reach them

**Status:** Proposed (2026-11-24) · **Target schema:** 0.7.7 (preview, per `schema_manager.PREVIEW_VERSIONS`), additive
· **Scope:** `$defs.semanticModel` dimensions (`typeParams.validityParams`) and entities, `$defs.semanticRelationship`
(`asOf`), FLUID SQL/1 paths and `sharedColumns` through a relationship, and the consumers that read them: the
validator, the dbt bridge, the ODCS exporter, the MCP query compiler, `fluid profile` and FLUX

**Related:** `RFC-semantics-tier1.md` §5 (validity windows) · `RFC-data-rules.md` §1.2 (functional dependencies), §2
(FLUID SQL/1) and §5 (shared attributes) · `RFC-data-profile-port.md` (measured statistics stay out of the
contract) · flux-engine ADR 0017 (histories and `asOf`) and ADR 0027 (snapshots, events and periods)

---

## Motivation

Since 0.7.7 a contract can declare a type II slowly changing dimension: one `natural` entity and a validity window, a
time dimension with `isStart` and one with `isEnd` (`RFC-semantics-tier1.md` §5). Other tables then relate to it by
its natural key. A subscription has versions, and its invoice lines, usage rows, events and current-state row all name
the subscription.

Three things keep the tables of such a source from agreeing.

1. **A relationship into a versioned table has no precise meaning.** The natural key repeats once per version, so a
   foreign key names an entity, not a row. FLUID SQL/1 v1 therefore refuses every read and every copy through such a
   relationship: "reads no slowly changing dimension through a relationship" and "copies nothing from a slowly
   changing dimension through a relationship". So none of these rules can be written, and no consumer checks them:
   - an invoice line is billed to its subscription's account;
   - a usage row carries the product of the version valid in its month;
   - an event's new product is the product of the version it opens;
   - the current-state row equals the subscription's current version.
2. **The window's conventions are unstated.** A reader has to guess four things:
   - how one version's end meets the next version's start: on the same instant, as dbt snapshots write it, or on the
     day before, as Kimball's type 2 writes it;
   - what an open version's end holds: `NULL`, or a sentinel such as `9999-12-31`;
   - which column marks the version that ends the entity;
   - which column every version carries as the entity's start.

   A guess that differs from the writer's convention produces overlaps or gaps that are not in the data.
3. **Measured instead of declared, these facts are degenerate.** In a clean history every consecutive pair of versions
   meets the same way and every last version is open. A profile's count table for them holds one non-zero cell and
   zeros. Output checks that treat a zero cell as disclosive withhold the zeros, and the remaining cell follows from
   the total, so the whole table is withheld. The same facts declared in the contract are public structure, and they
   release nothing about any record.

A test on an invented 12-table world of 692,704 rows shows what this costs. A synthetic copy built from the
contract and a profile kept a subscription's account the same across its versions, its current-state row and its
invoice lines on 0.04 % of rows; the source keeps it on 100 %. A history profiled under minimum-cell checks, with
zeros treated as disclosive, released nothing a generator could use. The same history, with its conventions taken as
declared, lifted the share of invoice lines that fall inside a valid version from 0.70 to 0.91.

Every shape below is borrowed:
- the open-end value, from dbt's `dbt_valid_to_current`;
- the two ways versions meet: dbt snapshots (an end equals the next start) and Kimball's type 2 (an end is the day
  before);
- the end marker, from dbt's `hard_deletes: new_record` and its `dbt_is_deleted` row;
- the current-row flag, from Kimball's current-row indicator;
- the as-of reading, from SQL:2011 application-time periods. PostgreSQL 18 checks them as temporal keys, and Flink
  joins on them with `FOR SYSTEM_TIME AS OF`. MetricFlow joins an SCD dimension at the start of a time grain.

## Design overview

Two additions. Both are opt-in, and every consumer reads them.

1. **Window conventions** (§1): the open end and the chain on the window's end dimension. The activation column, the
   end marker and the current-row flag go on the natural entity.
2. **Relationships into a versioned table** (§2). A relationship whose `toColumns` are a versioned table's natural
   entity reaches an entity. It reads that entity in one of two ways:
   - **by entity**, for the columns the target declares stable per entity. No date is needed (§2.1).
   - **as of a date**, for every other column, through a relationship that names an `asOf` column (§2.2).

Together these state the four rules above as contract rules (§2.3).

```yaml
# subscription_version: one row per version of a subscription
semantics:
  entities:
    - name: subscription
      type: natural
      expr: subs_id
      lifecycle:                                       # NEW
        activation: activation_date
        endMarker: { column: status, value: TERMINATED }
  dimensions:
    - name: valid_from
      type: time
      typeParams: { timeGranularity: day, validityParams: { isStart: true } }
    - name: valid_to
      type: time
      typeParams:
        timeGranularity: day
        validityParams: { isEnd: true, openEnd: null, chain: dayBefore }   # NEW: openEnd, chain
contract:
  invariants:
    - id: account_stable_per_subscription               # RFC-data-rules §1.2, unchanged
      dependency: { from: [subs_id], to: [account_id] }

# invoice_line: one row per billed line and month
semantics:
  relationships:
    - name: subscription                                # by entity: reads only stable columns
      to: subscription_version
      fromColumns: [subs_id]
      toColumns: [subs_id]
      cardinality: many_to_one
      sharedColumns: [account_id]
    - name: billed_version                              # as of the billed month
      to: subscription_version
      fromColumns: [subs_id]
      toColumns: [subs_id]
      cardinality: many_to_one
      asOf: billing_month                               # NEW
      sharedColumns: [product_id]
```

## 1. Window conventions

### 1.1 On the end dimension

| member | values | default | rule it adds |
|---|---|---|---|
| `openEnd` | `null`, or a literal of the end column's type (`"9999-12-31"`, `"9999-12-31T23:59:59Z"`) | `null` | An entity's last version may be open, and its end then holds this value. No other version holds it. Under `null`, an end that is `NULL` is open and every other end is closed. |
| `chain` | `nextStart`, `dayBefore` or `none` | `nextStart` | How a closed version's end meets the same entity's next start. Under `nextStart` the end equals the next start, as dbt snapshots write it, and a version is valid on `[start, end)`. Under `dayBefore` the end is the day before the next start, as Kimball's type 2 writes it, and a version is valid on `[start, end]`. `dayBefore` takes DATE columns only. Under `none` versions may leave gaps, and a version is valid on `[start, end]`. |

Every window keeps one rule whatever it declares: **versions of one entity never overlap**, and at most one of them,
the last, is open. This is PostgreSQL's `WITHOUT OVERLAPS` on the natural key and the period. A window that declares
neither member keeps the 0.7.7 meaning: `openEnd: null`, `chain: nextStart`.

### 1.2 On the natural entity

`lifecycle` is new and optional. It is allowed only on a `natural` entity of a model that declares a validity
window.

| member | value | rule it adds |
|---|---|---|
| `activation` | a column of the expose, of the window's type | Every version of an entity holds the same value, the start of the entity's first version. |
| `endMarker` | `{column, value}`, a column of the expose and a literal of its type | The entity ends with a final version that holds `value` in `column`. That version's start is the entity's end, and no earlier version holds the value. This is dbt's `hard_deletes: new_record`: a deleted record becomes one more row, marked. Without `endMarker`, an entity has ended when its last version is closed. |
| `currentFlag` | `{column, value}` | The column holds `value` on exactly one version of each entity: its last one. This is Kimball's current-row indicator. |

### 1.3 What a reader learns

A reader no longer guesses. A version's interval follows from `chain`. The open version follows from `openEnd`. The
entity's start and end follow from `activation` and `endMarker`. §2.2 uses all of these to find the version valid on a
date.

## 2. Relationships into a versioned table

A relationship **reaches an entity** when two things hold: its target declares a validity window, and its
`toColumns` are exactly the target's natural entity. Its `cardinality` and `joinType` speak about entities.
`many_to_one` means each row names one entity. `inner` means the entity exists, and §2.2 refines that for an as-of
relationship.

### 2.1 By entity

- **Stable columns.** A column is stable per entity when the target declares a `dependency` whose `from` is exactly
  the natural entity's columns and whose `to` holds the column (`RFC-data-rules.md` §1.2). All versions of an entity
  then agree on it.
- **Reads.** A path `<relationship>.<column>` may read a stable column through a relationship that reaches an entity,
  and so may a `sharedColumns` entry. No date is needed, because every version gives the same value. The natural key
  agrees by definition.
- **Anything else is still refused.** The refusal names both ways out: declare the column stable, or name an `asOf`.
- **Counting.** A count of children per parent through such a relationship counts one parent per entity, not one per
  version. Profilers count per entity, and generators lay children out per entity.

### 2.2 As of a date

`asOf` is new. It names a DATE or TIMESTAMP column of the declaring expose, of the target window's type. It may be a
derived column. RFC-data-rules §2.3 still applies: a date never meets a timestamp.

```yaml
- name: billed_version
  to: subscription_version
  fromColumns: [subs_id]
  toColumns: [subs_id]
  cardinality: many_to_one
  joinType: inner
  asOf: billing_month                  # NEW
```

- **Which version.** On each row the relationship reaches the version of the named entity that is valid on the row's
  `asOf` value. "Valid" uses the interval of §1.1. An open version is valid from its start on, whatever `openEnd`
  holds. Versions never overlap, so at most one version qualifies.
- **No version.** When the date falls before the entity's first start, after a closed last version, or in a gap, the
  relationship reaches no row. A path then reads `NULL`, and the row is not covered by `sharedColumns`, as with a
  `NULL` foreign key (`RFC-data-rules.md` §5). `joinType: inner` asks for a version on every row whose foreign key is
  not `NULL`; `left` allows none.
- **Reads.** Through an as-of relationship, a path may read any column of the version, and `sharedColumns` may copy
  any column. RFC-data-rules' one-source-per-value rule applies unchanged.
- **Several dates.** One expose may reach one target through several as-of relationships, one per date, such as an
  event's `version_before` and `version_after`.
- **One to one.** `one_to_one` is allowed when each version is reached by at most one row, as with an event per
  transition.
- **Rendering.** An as-of path renders as a join on the natural key plus the interval. Under
  `nextStart` and `openEnd: null` that is `t.start <= r.asOf AND (t.end IS NULL OR r.asOf < t.end)`. Under `dayBefore`
  and `none` it is `r.asOf <= t.end`. Under an `openEnd` literal, `t.end = <literal>` stands for the open case.

**Periodic facts.** A monthly row that falls in a month two versions share reads the version valid on its `asOf`
value, such as the month's first day. MetricFlow's default is the same: a metric takes the dimension value valid at the
start of its grain. A row that belongs to the second version should carry its own date as `asOf`, such as the
version's start or the period's last day, not the month's first day.

### 2.3 The four rules, written

| rule | how it is written |
|---|---|
| An invoice line is billed to its subscription's account. | `account_id` is stable per subscription (a `dependency` from `subs_id`), and `sharedColumns: [account_id]` goes on the by-entity relationship. |
| A usage row carries the product of the version valid in its month. | An as-of relationship with `asOf: usage_month` and `sharedColumns: [product_id]`. |
| An event's products are those of the versions it closes and opens. | Two as-of relationships. `version_after` has `asOf: event_date`, and a `derive` sets `after_product_id` from `version_after.product_id`. `version_before` has `asOf: day_before`, where `day_before` is derived as `ADD_DAYS(event_date, -1)`, and a `derive` sets `before_product_id` from `version_before.product_id`. |
| The current-state row equals the current version. | The current-state expose declares an as-of relationship to the versions with `asOf: snapshot_date`, and `sharedColumns` lists the columns it repeats. |

### 2.4 Validation errors

`fluid validate` refuses each of these:
- `asOf` on a relationship whose target declares no validity window, or whose `toColumns` are not the target's natural
  entity;
- an `asOf` column that does not exist, is not DATE or TIMESTAMP, or does not match the window's type;
- a path or `sharedColumns` entry through a by-entity relationship naming a column that is not stable per entity;
- `lifecycle` on an entity that is not `natural`, or in a model without a window;
- a `lifecycle` or `endMarker` column that does not exist;
- an `openEnd` literal that is not of the end column's type;
- `chain: dayBefore` on TIMESTAMP columns;
- two sources for one column (RFC-data-rules §5).

### 2.5 Schema additions

These are JSON Schema draft 2020-12 fragments. Each one joins an object that is closed today
(`additionalProperties: false`).

```json
{
  "validityParams": {
    "properties": {
      "openEnd": {
        "type": ["string", "null"],
        "description": "The end an open version holds: null, or a literal of the end column's type. Only on the isEnd dimension."
      },
      "chain": {
        "enum": ["nextStart", "dayBefore", "none"],
        "description": "How a closed version's end meets the same entity's next start. Only on the isEnd dimension."
      }
    }
  },
  "entity": {
    "properties": {
      "lifecycle": {
        "type": "object",
        "additionalProperties": false,
        "minProperties": 1,
        "properties": {
          "activation": { "type": "string", "minLength": 1 },
          "endMarker": { "$ref": "#/$defs/columnValue" },
          "currentFlag": { "$ref": "#/$defs/columnValue" }
        }
      }
    }
  },
  "semanticRelationship": {
    "properties": {
      "asOf": {
        "type": "string",
        "minLength": 1,
        "description": "A DATE or TIMESTAMP column of this expose; the relationship reaches the version valid on it."
      }
    }
  },
  "columnValue": {
    "type": "object",
    "additionalProperties": false,
    "required": ["column", "value"],
    "properties": {
      "column": { "type": "string", "minLength": 1 },
      "value": { "type": ["string", "number", "boolean"] }
    }
  }
}
```

The rules that need the rest of the contract, such as which dimension carries `isEnd`, types and the natural entity,
are the validator's §2.4 errors, not schema rules.

## 3. What each consumer does

| consumer | reads |
|---|---|
| **Validator** (`fluid validate`) | Every §2.4 error. On data, through the existing invariant checks: no overlap, one open version, the chain, activation, the end marker, the current-row flag, each as-of path and copy. |
| **FLUID SQL/1** | A path through an as-of relationship, or through a by-entity relationship to a stable column, is legal. Everything else into a window keeps v1's refusal, with the new remedy in the message. |
| **dbt bridge** | The window maps to MetricFlow's `validity_params` as today. `openEnd` maps to a snapshot's `dbt_valid_to_current`, and a boolean `endMarker` to `hard_deletes: new_record` with `dbt_is_deleted` named by `snapshot_meta_column_names`. As-of copies and checks become singular tests over the rendered join. |
| **ODCS exporter** | ODCS (v3.1.0, and v3.2.0, the current release) has no validity-window fields, so the exporter carries the members under `customProperties`, as it does other FLUID-only members. |
| **MCP query compiler** | Joins through an as-of relationship with the rendered interval, and refuses as before otherwise. |
| **`fluid profile`** | Checks each declared convention as a fact, the share of rows or entities that keep it, instead of publishing a histogram of it. Counts children per entity for by-entity and as-of relationships. Publishes a history's own distributions only where §1 leaves them open: versions per entity, version lengths, transitions. |
| **FLUX** | Reads every member into its own keys (§4). |

## 4. What FLUX reads

FLUX keeps its generation facts in its bundle: how many versions, how long, which transitions, what share ends. The
contract states only what valid data looks like. FLUX already has a key for each new member:

| contract | FLUX bundle |
|---|---|
| `validityParams.openEnd` | `history.openEnd` |
| `validityParams.chain` (`nextStart`, `dayBefore`) | `history.chain` |
| `lifecycle.activation` | `history.activation` |
| `lifecycle.endMarker` | `history.end`, `mode: finalVersion`, with `column` and `value` |
| a by-entity relationship and a stable foreign key | `history.parent`, the relationship whose foreign key every version shares |
| `asOf` | `asOf: {emit, relationship, date}` |
| `sharedColumns` through an as-of relationship | copies through that link |
| the current-state expose's as-of relationship | `snapshots: {emit, of}` |

`fluxsim init` writes these keys from the contract. Its bundle then holds the history's structure with no profile
statistic behind it. FLUX's generator also has to set a foreign key by copy and compute an emit's derives after its
period columns are set. Those are FLUX's own changes, not part of this RFC.

## Compatibility

- **Additive and opt-in.** A contract without the new members validates and behaves as before. SQL/1 v1's refusals
  stay for every read the new members do not declare.
- **Target.** The members land in the 0.7.7 preview. FLUX vendors that preview schema byte for byte, so FLUX
  re-vendors it when this lands.
- **dbt 1.12.** dbt 1.12 moved `validity_params` from a dimension's `type_params` onto the dimension itself. FLUID
  keeps `typeParams.validityParams`, and the dbt bridge writes the layout of the dbt version it targets.

## Security and disclosure

The new members are declarations, and they name no record. The only literals are an open-end value and an end-marker
value, constants of the schema such as `9999-12-31` and `TERMINATED`. A profiler that checks them publishes shares
under its usual output checks. A declared convention replaces the degenerate count table a profile would otherwise
withhold whole, so the release holds more and discloses nothing more. An as-of path reads only through relationships
the contract already declares.

## Out of scope (deliberately)

- System time and bitemporal tables: when a row was recorded, as opposed to when it applied. PostgreSQL has no system
  time either.
- Summaries (`aggregate`) through an as-of relationship, and paths over several links, which stay FLUX-only.
- A contract-wide as-of parameter: the as-of value here is always a column of the row.
- Generating gaps. `chain: none` is declared and checked, but FLUX generates `nextStart` and `dayBefore` histories
  only.
- Relationships across contracts.

## Open questions

1. **Names.** `chain` reuses FLUX's word; `meets` would read more plainly. `endMarker` versus dbt's `is_deleted`.
2. **Target.** Should these members wait for a 0.7.8 preview instead of joining 0.7.7?
3. **Missing versions.** Should `joinType: inner` on an as-of relationship be a hard rule, or carry a share band, as
   other invariants do, for sources with known gaps?

## Implementation plan

1. **Schema.** The new members on `validityParams`, the natural entity and `semanticRelationship`, with their
   descriptions and the §2.4 errors.
2. **FLUID SQL/1.** The checker accepts by-entity reads of stable columns and as-of paths. Rendering emits §2.2's
   join, and the shared test vectors gain as-of cases.
3. **Consumers.** The validator's data checks, the dbt bridge's snapshot configs and tests, the ODCS exporter's
   `customProperties`, and the MCP compiler's as-of join.
4. **`fluid profile`.** Declared conventions become checked facts, children are counted per entity, and the history's
   remaining distributions are published under its output checks.
5. **FLUX** (in flux-engine). `init` maps the members onto its keys (§4), and the preview schema is re-vendored.

## References

- dbt snapshots: `dbt_valid_to_current`, `hard_deletes`, `snapshot_meta_column_names` (dbt-core 1.12):
  https://docs.getdbt.com/docs/build/snapshots
- dbt semantic layer, SCD type II dimensions (`validity_params`, natural entities):
  https://docs.getdbt.com/docs/build/dimensions
- Kimball Group, type 2 (effective date, expiration date, current-row indicator):
  https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/type-2/
- PostgreSQL 18, temporal tables (`WITHOUT OVERLAPS`, `PERIOD` foreign keys):
  https://www.postgresql.org/docs/current/ddl-temporal-tables.html
- Apache Flink, event-time temporal joins (`FOR SYSTEM_TIME AS OF`):
  https://nightlies.apache.org/flink/flink-docs-master/docs/dev/table/sql/queries/joins/
- Databricks AUTO CDC, SCD type 2 (`__START_AT`, `__END_AT`, `NULL` for the current row):
  https://learn.microsoft.com/azure/databricks/ldp/cdc
- Open Data Contract Standard v3.1.0 and v3.2.0 schemas: https://github.com/bitol-io/open-data-contract-standard
