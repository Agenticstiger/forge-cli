# RFC: Data profile output port (`kind: data_profile`)

| | |
|---|---|
| Status | Proposed (2026-11-08) |
| Target schema | 0.7.7 (the open preview, per `schema_manager.PREVIEW_VERSIONS`) |
| Scope | `$defs.expose` (one new kind and one kind block), the validator, the ODPS and ODCS exporters, the OpenLineage emitter. User docs go to forge_docs. |
| Related | Companion FLUX RFC (Agenticstiger/flux) that reads a Playback's source through this port. `RFC-packaging-modes.md` for the preview lifecycle. |

## Summary

Add one expose kind, `data_profile`. A data profile is an output port that publishes aggregate statistics about another expose of the same product: bucket counts on declared edges, moments, quantile positions, distinct counts, and (for event data) transition and dwell counts. It never publishes a row.

The contract holds only the declaration: which expose is profiled, which columns, which statistic families, the minimum cell size, the freshness SLO and the access policy. The numbers themselves are written as profile states to the port's binding, one immutable document per refresh, and announced over OpenLineage. They never appear in the contract.

## Motivation

Some consumers need the shape of a dataset and must not see its rows. A simulation fitted against observed behavior is the sharpest case: the FLUX spec's Playback kind names the data a simulation is calibrated against, and today that is a bare URI to raw telemetry. Drift monitors, synthetic data generators and a marketplace preview of a product are others. In each case the data owner wants to share counts and distributions with a wider audience than the one allowed to read the records.

FLUID has no way to say this. `exposes[].kind` has no profile concept. The fields that look close are declarations of checks, not carriers of values: `contract.dq` and `contract.quality` say what to test, `observability.metrics` says what to watch. A team that wants to share a profile today has two options and both are bad.

The first is to write the values into the contract. That moves the contract digest on every refresh, leaks the statistics to everyone who can read the catalog, and turns a daily refresh into a daily contract version. The section on the contract digest below goes through each effect.

The second is a `kind: file` expose and a private convention. Nothing can then check the declaration, no reader can tell that the file holds aggregates only, and no consumer can tell whether small cells were suppressed.

## Goals and non-goals

Goals:

- One new kind and one kind block, additive, in the 0.7.7 preview only.
- Values flow through the port. The contract digest moves only when the declaration changes.
- Aggregate-only by construction, with minimum cell suppression and optional differential privacy declared in the contract where a reviewer can see them.
- A fixed, versioned state document so producers and consumers written by different people agree byte for byte.
- Announcement over OpenLineage: one custom facet that commits to each state, plus the existing `DataQualityMetricsInputDatasetFacet` when the owner opts in to summary counts.
- Ordinary composition: other products consume a profile with an ordinary `consumes[]` entry.

Non-goals:

- Proving that a state was computed correctly from the source. A state's digests bind its bytes, its declaration and its contract. They prove nothing about the computation (see Limits).
- Profiles that join several sources. Build a joined expose first, then profile it.
- Replacing data quality rules. A rule says what must hold; a profile reports what was observed.
- Record-level synthetic data. A consumer may synthesize from a profile; this RFC only defines what the profile carries.

## Design

### The expose

A `data_profile` expose is an ordinary expose with a `profile` block. `binding` says where states are written, `qos.freshnessSLO` says how old the newest state may get, and `policy` (with any root `accessPolicy` grants) says who may read states. All three are existing fields with their existing meaning. It has no `contract`: the shape of what it serves is the state document this RFC fixes, not a schema the author writes.

```yaml
exposes:
  - exposeId: payments
    kind: table
    binding: { platform: aws, format: iceberg, location: { database: billing, table: payments } }
    contract:
      schema:
        - { name: customer_id, type: string, sensitivity: pii }
        - { name: amount, type: "decimal(12,2)" }
        - { name: plan, type: string }
        - { name: segment, type: string }
        - { name: event_type, type: string }
        - { name: event_ts, type: timestamp }

  - exposeId: payments_profile
    kind: data_profile
    version: 1.0.0
    binding:
      platform: aws
      format: json
      location: { bucket: billing-profiles, path: payments/ }
    qos:
      freshnessSLO: P8D
    policy:
      authz:
        readers: [pricing_analysts]
    profile:
      source: payments
      entity: customer_id
      window: { column: event_ts, length: P7D, mode: tumbling }
      columns:
        - name: amount
          domain: range
          edges: ["0", "10", "25", "50", "100", "250", "1000"]
          resolution: "0.01"
        - name: plan
          domain: category
        - name: segment
          domain: category
        - name: event_type
          domain: category
      families: [histogram, moments, quantiles, distinct, transitions, dwell]
      quantiles: ["0.5", "0.9"]
      groupBy: [segment]
      sequence:
        activity: event_type
        time: event_ts
        dwellEdges: [PT1H, P1D, P7D, P30D]
      disclosure:
        minCellSize: 10
```

`profile.source` names an expose of the same contract. A profile can only describe data its own product owns, because only the owner can read the rows inside its boundary. A product that wants a profile of someone else's data consumes that data, builds its own expose, and profiles that.

### Columns

Every profiled column must exist in the source's `contract.schema`. Each column has a `domain`:

- `range`: a numeric column. `edges` are the bucket boundaries, written as decimal strings so no binary float enters a hashed document. The first and last edge are the declared range. Buckets are half-open, `[e_i, e_i+1)`, except the last, which is closed so a value equal to the top edge is counted. `resolution` is the step values are rounded to (half-even) before any sum, so sums are exact integers in units of `resolution`.
- `category`: a string column. Its categories are counted as they occur. A category below the minimum cell size is dropped from the histogram and its count pooled into `belowMinimum`, so a rare label never appears. The pooled count is itself suppressed when it is below the minimum.

A value outside its declared range refuses the whole state: no state is written, and the run reports the column and the row position, never the value. The fix is to widen the edges, which is a declaration change and is reviewed as one. Silent clamping was considered and rejected: it would hide exactly the drift a profile exists to show.

`entity` names the column that identifies the person or thing a row is about. It is optional, never published, and only ever counted. When it is set it is the privacy unit: the minimum cell size counts distinct entities, the dominance rule and noise bound each entity's contribution, and the sequence families order events per entity. Declare it whenever a row is not already one person or thing. Without it the minimum counts rows, and on event data ten rows can be one customer.

### Statistic families

`families` lists what a state carries. Each family is computed per cell (see grouping below).

- `histogram`: the count per bucket of a range column, or per category of a category column. For a category column the histogram's keys are its support.
- `moments`: for a range column, the sum and the sum of squares of its rounded values. `pairs` (optional, a list of two-column lists) adds the sum of products for each declared pair. These are the sufficient statistics for means, variances, correlations, ratio metrics and regression adjustment, which is what experiment analysis needs (gbstats takes exactly `n`, `sum`, `sum_squares` and a sum of products).
- `quantiles`: for each requested probability in `profile.quantiles`, the index of the bucket that holds that quantile. It says "the median lies in [50, 100)", never the median value.
- `distinct`: the number of distinct values of a column in the cell. On `entity` it is the number of entities.
- `transitions`: for event data, the count of each directly-follows pair (activity a, then activity b, for the same entity, ordered by time), plus how many sequences start and end at each activity. This is the object-centric directly-follows graph that process mining uses (OCEL 2.0), reduced to its counts.
- `dwell`: for each directly-follows pair, a histogram of the time between the two events on `sequence.dwellEdges` (ISO 8601 durations).

`transitions` and `dwell` need `entity` and a `sequence` block: the activity column and the time column. The activity column must also be declared in `columns` as a category column, so every label rule applies to it: a rare activity is pooled, and a transition that involves a pooled activity is counted under `belowMinimum`, never keyed by its label. Sequences are cut at window boundaries: the first event of an entity inside a window starts a sequence.

### Grouping and windows

`groupBy` lists up to two category columns from `columns`. Every family is computed for each cell of the cross product of their categories, and for the ungrouped total. An experiment's outcome per variant and segment is a grouped `moments` profile: `groupBy: [variant, segment]` with `moments` on the outcome column.

`window` says which rows a state covers. `mode: tumbling` cuts the time column into consecutive windows of `length`. A window is published once, when it closes, so each row lands in at most one state; rows that arrive after their window closed are not counted in any state. That is why a `qos.freshnessSLO` shorter than `window.length` is refused: meeting it would mean publishing windows that are still open. `mode: snapshot` profiles the whole source at each refresh. The two behave very differently under differencing; see Disclosure.

### Profile states

A state is one JSON document, written to the binding as an immutable object named by its sequence number, plus a small `latest.json` that names the newest sequence and its digest. Its shape is fixed by a published JSON Schema (`fluid-profile-state-0.1.0.json`, shipped beside the FLUID schemas). A sketch:

```json
{
  "profileVersion": "0.1.0",
  "productId": "billing.payments",
  "exposeId": "payments_profile",
  "source": "payments",
  "sequence": 42,
  "previousStateDigest": "sha256:<64 hex>",
  "window": { "start": "2026-11-01T00:00:00Z", "end": "2026-11-08T00:00:00Z" },
  "contractDigest": "sha256:<64 hex>",
  "declarationDigest": "sha256:<64 hex>",
  "disclosure": { "minCellSize": 10, "suppressedCells": 3, "noise": null },
  "cells": [
    {
      "group": { "segment": "family" },
      "n": 1840,
      "suppressed": ["columns.plan.histogram.basic", "columns.plan.belowMinimum"],
      "columns": {
        "amount": {
          "histogram": [12, 140, 410, 600, 513, 151],
          "moments": { "sum": "10423315", "sumSquares": "981244877101" },
          "quantiles": { "0.5": 3, "0.9": 4 }
        },
        "plan": { "histogram": { "basic": null, "plus": 928 }, "belowMinimum": null }
      }
    }
  ]
}
```

A suppressed value is written as `null` and listed in its cell's `suppressed` array, so a reader cannot mistake it for zero. In the sketch, one plan had fewer than ten customers, so its label is gone and its count went into `belowMinimum`, which is itself below ten and so suppressed. The count for `basic` was then suppressed too, so the pooled count cannot be recovered from `n` by subtraction (see Disclosure). Sums are strings because they can pass 2^53. A sum is in units of `resolution`, a sum of squares in units of `resolution` squared, and a sum of products in units of the two resolutions multiplied. The `stateDigest` is the SHA-256 of the document in the same canonical JSON that `compute_contract_digest` uses (NFC strings, sorted keys, compact separators, UTF-8). The document holds only strings, integers, booleans and nulls, so there is no float formatting to disagree about.

`previousStateDigest` chains each state to the one before it. A consumer that holds state 41 can tell whether state 42 replaced, skipped or rewrote history. `declarationDigest` is the digest of the canonical `profile` block alone. A consumer that pins it accepts unrelated contract edits (a new description, another expose) and still notices any change to what is computed.

## Why the contract digest stays stable

`compute_contract_digest` hashes every key of the parsed contract and masks nothing, by design: a contract has no volatile field, so every byte is part of its identity. Put a profile's values in the contract and every refresh is a new contract. Concretely:

- Federation pins go stale daily. A cross-mesh consumer pins `consumes[].upstreamDigest`, and `fluid apply` compares it with the live digest and warns on a mismatch. Every refresh would raise "the upstream changed since composition", and a warning that fires every day is a warning people stop reading.
- FLUX provenance breaks daily. `Simulation.emits[].provenance.contractDigest` is checked against the in-bundle contract, so a bundle proven yesterday fails today.
- Plan binding churns. The bundle digest covers the contract, so a plan made before a refresh no longer binds to the bundle after it.
- Versions churn. A changed contract is a new contract version under the product's own compatibility rules, so consumers' `versionConstraint` ranges would track refreshes rather than meaning.
- Statistics leak. The contract is what `fluid publish` and the ODPS and ODCS exports carry into catalogs, and catalogs are usually readable far more widely than any one port. The values would be published under the catalog's access rules instead of the profile's.

With the port, the contract changes when the declaration changes (a new column, new edges, a new minimum cell size), and that is exactly when consumers should look again. A refresh writes a new state with its own digest, and the state names the contract digest it was computed under, so a consumer can always tell "new state, same declaration" from "new declaration".

## Announcing a state

Each state is announced with one OpenLineage `RunEvent` (`COMPLETE`) from the job `<product id>.profile.<exposeId>` in namespace `fluid`, following the product-first job names the build and apply events already use. The profiled source is the run's input and the profile port is its output.

### The input facet: `DataQualityMetricsInputDatasetFacet`

By default (`announce: commitment`) the event carries the commitment facet below and no metrics. A lineage backend's readers are not the profile's readers, and exact counts there would sit beside states that may be noised or suppressed differently.

An owner who wants the summary that lineage backends already show for most tables sets `announce: summary`. The source dataset then carries the standard `dataQualityMetrics` input facet (OpenLineage 1.55.0, facet schema 1-0-3). It is an input facet on purpose: the metrics describe the rows this run read when it read them, which is what the input facet means. The generic `DataQualityMetricsDatasetFacet` would read as a claim about the dataset at large. The summary carries this and nothing else:

```json
"inputFacets": {
  "dataQualityMetrics": {
    "_producer": "https://github.com/Agenticstiger/forge-cli/tree/v<version>",
    "_schemaURL": "https://openlineage.io/spec/facets/1-0-3/DataQualityMetricsInputDatasetFacet.json#/$defs/DataQualityMetricsInputDatasetFacet",
    "rowCount": 5230,
    "columnMetrics": {
      "amount": { "count": 5191, "nullCount": 39 },
      "plan": { "count": 5230, "nullCount": 0, "distinctCount": 3 }
    }
  }
}
```

`rowCount`, `count`, `nullCount` and `distinctCount` are filled under the same suppression as the state. `min` and `max` are never filled: each is one record's value. `sum` and `quantiles` stay in the state, behind the port's access policy. Because the backend's audience is unknown, the summary never names a column the source restricts for anyone, and it is refused for `mode: snapshot` and for profiles with `noise`: exact counts published on every refresh could be subtracted, and they would spend nothing from the privacy budget.

### The output facet: `fluid_profileCommitment`

The profile dataset carries one custom facet. OpenLineage asks that a custom facet use a prefix named after its project and carry `_producer` and an immutable `_schemaURL`. The key follows the existing `fluid_run` and `fluid_engine` keys; the schema is `FluidProfileCommitmentDatasetFacet`, published at a tagged URL, never a branch.

```json
"facets": {
  "fluid_profileCommitment": {
    "_producer": "https://github.com/Agenticstiger/forge-cli/tree/v<version>",
    "_schemaURL": "<tagged URL>/FluidProfileCommitmentDatasetFacet.json",
    "profileVersion": "0.1.0",
    "sequence": 42,
    "stateDigest": "sha256:<64 hex>",
    "previousStateDigest": "sha256:<64 hex>",
    "contractDigest": "sha256:<64 hex>",
    "declarationDigest": "sha256:<64 hex>",
    "window": { "start": "2026-11-01T00:00:00Z", "end": "2026-11-08T00:00:00Z" },
    "sourceSnapshot": "iceberg:<snapshot id>",
    "disclosure": { "minCellSize": 10, "suppressedCells": 3, "noise": null }
  }
}
```

A consumer reads the state from the port, recomputes its digest, and checks it against `stateDigest`. `sourceSnapshot` is optional and opaque: an Iceberg snapshot id, a Delta version, a Kafka offset range. With it, an auditor who may read the source can recompute a noise-free state and compare digests. Readers ignore facet members they do not know, so a later facet version can add members without breaking them.

## Disclosure

### Aggregate-only

A state may contain counts, sums of rounded values, sums of squares and products, bucket indices, and category labels that cleared the minimum cell size. It may not contain a row, an identifier, a single record's value, a minimum or maximum, or a sample. `fluid validate` refuses a `category` domain on a source column marked `sensitivity: pii` or `phi`, since its labels would be identifiers; such a column can still be bucketed as a `range` or used as `entity`, which is never published.

Restrictions on the source carry over. Every column the profile reads (`columns`, `groupBy`, `pairs`, `window.column`, `sequence.activity`, `sequence.time`) is refused if the source restricts it with `policy.authz.columnRestrictions` for any of the profile's readers. The one exception is `entity`: it is never published, only counted, and refusing a restricted identifier there would only push owners to profile without a privacy unit. A profile may have a wider audience than its source, which is the point of it, but never by accident: no grant on the source reaches the profile, and no grant on the profile reaches the source.

### Minimum cell size

`disclosure.minCellSize` is required and must be at least 5. It counts distinct entities when `entity` is set, and rows otherwise. Any cell, bucket, category, transition, dwell bucket or group whose count is from 1 up to `minCellSize - 1` is suppressed, and so is everything computed from it. Zero is published, since it describes no one.

A sum has a second risk: one large contributor. A sum, a sum of squares or a sum of products is also suppressed when its two largest contributors (entities, or rows without `entity`, by absolute value) make up more than `disclosure.maxDominance` of it, default 0.8. This is the dominance rule statistical agencies apply to magnitude tables. Counting the top two matters because the second-largest contributor can subtract its own value, and what is left must not be mostly the largest one.

Suppression alone can be undone by subtraction, because bucket counts add up to `n` and groups add up to their total. So suppression is applied in two passes, the usual practice in statistical disclosure control (tau-ARGUS and sdcTable both call this primary and secondary suppression). The first pass suppresses small and dominated cells. The second runs on the full table, before rare labels are pooled, over every sum the state publishes: the buckets or categories of one cell, and each bucket or label across the cells of a group and the total. Wherever a sum has at least one suppressed part and its suppressed parts together count fewer than `minCellSize`, it suppresses the smallest published part that is not zero (ties broken by label order), and it repeats until no sum is left like that. A zero is never chosen, since suppressing it hides nothing.

The second pass stops recovery by one subtraction: whatever a reader can subtract out of a sum covers at least `minCellSize` contributors. It does not stop every linear recovery in a two-way table with published margins. Optimal secondary suppression is a hard problem, and that is what the dedicated tools solve. This is why `groupBy` is capped at two columns in this version, and why the stronger guarantee is noise.

### Differencing across states

Two states that count overlapping rows can be subtracted. If a snapshot profile shows `n = 1840` today and `1841` tomorrow, the difference in every sum is one new record. Minimum cell size does nothing against this.

`mode: tumbling` publishes each window once, when it closes, so each row is in at most one state and there is nothing to subtract. `mode: snapshot` re-counts the same rows on every refresh, so `fluid validate` refuses it unless the profile declares `noise`, or the owner writes `disclosure.differencing: accepted`. The second form exists for profiles whose consumers already hold the rows (an internal drift check, say) and puts the decision in the contract diff where a reviewer sees it.

A producer that has to correct a published tumbling window (late rows, a fixed bug) publishes a restatement. A restatement counts the same rows again, so it is allowed only for a profile that would pass as a snapshot: one with `noise` or `differencing: accepted`. Otherwise a published window is final.

### Optional differential privacy

`disclosure.noise` adds calibrated noise before suppression:

```yaml
disclosure:
  minCellSize: 10
  noise:
    mechanism: laplace          # or gaussian, which also needs delta
    epsilon: "0.5"              # spent per state
    budget: { epsilon: "4", period: P30D }
```

The declared edges double as the clipping bounds, so the sensitivity of every sum is known. When `entity` is set, the privacy unit is the entity and `maxRowsPerEntity` bounds each entity's contribution; otherwise it is the row. A producer refuses to publish a state that would exceed the budget within its period, and the commitment facet records what each state spent. Noise makes a state impossible to recompute from the source, so `sourceSnapshot` audits only noise-free profiles. The reference implementation would use OpenDP, as an optional extra so the base install stays light.

## Composition and lineage

A profile is an output port of the product that owns its source. It does not change the product's `metadata.productType` or `metadata.layer`: a source-aligned product can publish a profile of its own table.

Other products consume it like any expose, with an ordinary `consumeRef`: `productId`, `exposeId`, `versionConstraint`, `requiredPolicies`, and `qosExpectations.freshnessMax` if they need a recent state. The composition rules are unchanged; a profile counts as an upstream like any other. The composition pipeline must not try to infer columns from a profile upstream, since a profile has no `contract.schema` to infer them from.

Versioning follows the declaration. Adding a column or a family is a minor version. Removing one, or changing edges, resolution or the minimum cell size, is a major version, because states before and after are no longer comparable bucket for bucket. A consumer such as a simulation pins `^1.0` and keeps working across refreshes and additive changes.

Lineage records what was actually read. Inside the product, the source to profile edge is an aggregation (`transformation.type: aggregation` in field-level lineage). A consumer's `lineage.upstream` names the profile, not the source, so impact analysis on the source still reaches every profile consumer, through the profile. A schema change that drops a profiled column on the source fails `fluid validate` on the profile, in the same contract, before it can ship.

Cross-mesh consumers pin `upstreamDigest` as today. Because refreshes do not move the contract digest, that pin holds until the owner changes a declaration.

## Validation rules

`fluid validate` refuses, naming the field path, the rule and the fix, and never printing a data value:

1. `kind: data_profile` without a `profile` block, or a `profile` block on any other kind.
2. `profile.source` that is not an exposeId of this contract, names another `data_profile`, or has no `contract.schema`.
3. A profiled column missing from the source schema.
4. A `range` column without at least two strictly increasing decimal-string `edges`, or `moments` on a column without `resolution`.
5. `moments` or `quantiles` on a `category` column, or `quantiles` without `profile.quantiles`.
6. `transitions` or `dwell` without `entity` and `sequence`, `dwell` without `dwellEdges`, or a `sequence.activity` that is not a `category` column in `columns`.
7. A `groupBy` column that is not a `category` column of the profile, or more than two of them.
8. `minCellSize` missing or below 5, or `maxDominance` outside (0, 1).
9. A `category` domain on a `pii` or `phi` source column, or any column the profile reads, other than `entity`, that the source restricts for a profile reader.
10. `mode: snapshot` with neither `noise` nor `differencing: accepted`.
11. `noise` with a non-positive `epsilon`, or `gaussian` without `delta`.
12. A `contract` block on a `data_profile` expose: the state format is fixed by the spec.
13. `mode: tumbling` with a `qos.freshnessSLO` shorter than `window.length`.
14. `announce: summary` together with `mode: snapshot` or `noise`.

It also warns when one contract declares two profiles of the same source, because their published sums can combine like the margins of one larger table (see Limits).

## Compatibility and rollback

The change is additive and lands only in the 0.7.7 preview: one enum value, one optional block, and an if/then that ties them together. Two existing constraints become conditional on the kind. `contract` is required on every expose today, and `$defs.exposeContract` requires `schema` or `openapiRef` through an `anyOf`. Both move into an if/then on the expose that applies to every kind except `data_profile`, so each existing kind accepts exactly what it accepts today, and a golden test over every fixture contract pins that. Stable 0.7.6 is unchanged, untagged contracts never default to a preview version, and so no existing contract's digest, bundle or plan moves.

Code that lists expose kinds as a closed set gains one entry: `cli/product_add.py::_EXPOSE_KINDS`, the ODCS physical type map in `providers/odcs/mappers/types.py`, and the ODPS output port writer in `providers/opds/opds.py`, which writes `type: data_profile` and no schema. The ODCS export for a profile expose points at the state schema and carries no columns. Neither export ever carries a state.

Rollback is deleting the expose. Inside the contract only `accessPolicy` grants that point at it by JSONPath refer to it. Outside, its consumers fail validation the way they would for any removed expose, which is the right signal. If review withdraws the kind before 0.7.7 goes stable, only contracts that opted into the preview and used it are affected.

A forge-cli release older than the one that ships the schema change rejects `kind: data_profile` in a 0.7.7 contract. That is the normal preview story: a preview contract needs a forge-cli at least as new as the preview features it uses.

## Delivery plan

1. Schema and validator: the kind, the block, the rules above, the kind lists and the exporters, with a fixture contract per rule and a golden check that every existing fixture validates unchanged.
2. The state: the state JSON Schema, a canonical writer, the digests, the commitment facet schema, and the announcement builder in `build_runners/_lineage.py`. Tests include a round trip and a check that a facet with `min` or `max` set is refused.
3. A reference profiler that computes states on the local DuckDB path with both suppression passes and the dominance rule, and optional noise through OpenDP as an extra. The exact command surface is left to that PR.
4. `fluid verify` checks the newest state: freshness against `qos.freshnessSLO`, the digest chain, and the contract and declaration digests.

User docs land in forge_docs alongside step 1.

## Alternatives considered

Values in the contract: rejected for the five reasons in the digest section.

Values in `contract.quality`, `contract.dq` or `observability.metrics`: the same problem in a different field. Those blocks declare checks; mixing observed values in would also blur what a contract promises with what a run saw.

A `kind: file` expose with a convention: nothing could check the declaration or tell a reader that suppression was applied.

DataHub's `DatasetProfile` or OpenMetadata's `columnProfile` as the state format: both are good catalog formats and neither is aggregate-only. DataHub's field profile carries `min`, `max` and `sampleValues`; OpenMetadata's column profile carries `min` and `max`. A state can be projected into either for display later; the reverse is not safe.

whylogs profiles: the project has had no release since 1.6.4 (December 2024), and its frequent-items sketches carry raw values. Its profile-to-profile drift pattern is worth adapting for consumers.

OpenLineage as the only carrier: lineage backends have their own readership, the facets have no fields for histograms, moments or transitions, and an event is not a durable, ordered history. OpenLineage announces; the port carries.

## Prior art (borrow-before-build receipts)

From the receipts recorded 2026-11-08, not re-run here:

- OpenLineage 1.55.0 ships `DataQualityMetricsInputDatasetFacet`, already reachable through forge-cli's `lineage` extra. Used for the opt-in summary announcement.
- whylogs: rejected as a dependency (dormant since early 2025); drift pattern adapted.
- OpenDP 0.16.0 (MIT): the optional noise mechanism.
- Apache DataSketches 5.2.0 (Apache-2.0): only if mergeable sketches are ever needed. Not in this version.
- OCEL 2.0 (CC BY 4.0): the event-log shape the sequence families reduce. pm4py is AGPL-3.0, so its directly-follows pattern is adapted, not depended on.
- gbstats (MIT) works from summary statistics, which fixed what `moments` must carry.

Checked for this RFC (2026-11-08):

- OpenLineage spec: custom facets need `_producer` and `_schemaURL`, a project prefix, and an immutable schema URL.
- DataHub `DatasetFieldProfile.pdl` and OpenMetadata `table.json` `columnProfile`: field lists as quoted above.
- gbstats `models/statistics.py`: `SampleMeanStatistic(n, sum, sum_squares)`, `RatioStatistic` adds `m_d_sum_of_products`, `RegressionAdjustedStatistic` adds `post_pre_sum_of_products`.
- sdcTable 0.34.0 (GPL) and tau-ARGUS 4.2.5.2 (EUPL-1.2): primary and secondary cell suppression. Copyleft, so the pattern is adapted and nothing is vendored.
- Open Data Contract Standard 3.2.0: no profile or statistics concept to align with.

Inside forge-cli: `compute_contract_digest` and its canonical JSON are reused for state digests, and the `fluid_*` facet keys, job naming and producer URI follow `build_runners/_lineage.py`.

## Open questions

1. Should `announce` default to `summary` for tumbling profiles without noise, where each row is counted once and the counts cannot be subtracted across states? This RFC defaults to `commitment` everywhere, because the lineage backend's audience is still not the profile's.
2. Are 5 for the `minCellSize` floor and 0.8 for the `maxDominance` default right? Agencies set both per release; the floor only stops a declaration from being meaningless.
3. Should a profile be allowed on a `stream` or `topic` source in this version, or only on tables until the reference profiler covers streams?

## Limits

Minimum cell suppression is not a formal privacy guarantee. Differencing, linkage with outside data, and margins in two-way tables remain risks; noise is the formal option, and it costs accuracy.

The second suppression pass blocks recovery by one subtraction, not every linear recovery.

Two profiles of the same source are not checked against each other. Their sums can combine like the margins of one larger table, which is why the validator warns about them.

Without `entity`, the minimum counts rows. On event data that protects far less than it appears to, and the validator cannot tell event data from one row per person, so the docs have to say it.

Quantiles have bucket resolution only, by design.

A profile covers one source expose. Joint structure across products needs a joined expose first.

The digests bind a state to its bytes, its declaration and its contract. Nothing here shows that a state was computed correctly from the source: a consumer trusts the producer for that, or audits it through `sourceSnapshot` when it may read the source.
