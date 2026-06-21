# RFC: Streaming extension — first-class Kafka → Apache Iceberg sink

| | |
|---|---|
| **Status** | Draft / proposed — **spike-validated** (observed green run, §14) |
| **Scope** | `fluid` CLI (`fluid_build/`) — streaming build-runners + AWS provider |
| **Created** | 2026-06-21 |
| **Owner** | _assign via GitHub_ |
| **Supersedes** | n/a |
| **Related** | `AUTOGEN_SPIKE.md` (OpenTofu cutover), `HONESTLY_TESTED.md` (coverage tiers) |

> This RFC scopes a **config-ergonomics + validation** feature. The hard runtime
> work (exactly-once commit coordination, equality deletes, the transactional
> control topic) stays owned by the upstream **Apache Iceberg Kafka Connect
> sink** (Apache-2.0). forge does **not** reinvent any of it — it derives,
> validates, and provisions the catalog it already knows how to provision.

---

## 1. Summary

forge already wires the Apache Iceberg Connect sink class and a
Debezium-server Iceberg default, but writing Kafka → Iceberg today means
**hand-authoring the entire `iceberg.catalog.*`/`iceberg.tables.*` connector
dict** in `properties.kafka-connect.sink_connector_config`. There is no catalog
auto-wiring from the platform, no declared Iceberg-table surface in the
contract, no plan-time validation of the sink config, and no routing /
exactly-once / late-arrival ergonomics.

This RFC proposes the **provider-unified** design: make **one Iceberg-table
identity authoritative** for both (a) the static Glue table forge already
provisions and (b) the streaming Kafka sink. A single pure resolver computes the
canonical `{warehouse, region, io-impl, fq_table, …}` **once**; the existing
static path provisions the table, and a new thin deriver compiles those exact
resolved values into the connector config. Because both halves read the same
resolver, the connector physically cannot point at a different
warehouse/region/table than the catalog forge created — **drift becomes a
plan-time error, not a production incident.**

---

## 2. Motivation & current state (verified)

Two halves of one Iceberg-table identity exist today and **do not talk to each
other**:

**(a) Static materialization** — `exposes[].binding.format = iceberg` →
`is_iceberg_format` → `glue.ensure_iceberg_table` → the OpenTofu Glue emitter
stamps `table_type = ICEBERG` and derives the warehouse:

```python
# fluid_build/providers/aws/plan/planner.py:507-513
raw_bucket = location.get("bucket")
bucket = _resolve_env_templates(raw_bucket) if raw_bucket else None
if not bucket or "{{" in bucket:
    bucket = f"{account_id}-fluid-data"
path = location.get("path", f"{database}/{table}/")
s3_location = f"s3://{bucket}/{path}"
```

**(b) Streaming write** — the runner wires the Apache sink class but forces a
hand-written dict:

```python
# fluid_build/build_runners/kafka_connect/runner.py:64-70
SINK_CONNECTOR_CLASS = {
    "jdbc": "io.confluent.connect.jdbc.JdbcSinkConnector",
    "s3": "io.confluent.connect.s3.S3SinkConnector",
    "snowflake": "com.snowflake.kafka.connector.SnowflakeSinkConnector",
    "iceberg": "org.apache.iceberg.connect.IcebergSinkConnector",
    "bigquery": "com.wepay.kafka.connect.bigquery.BigQuerySinkConnector",
}
# ...runner.py:274 — sink_config = kc_props.get("sink_connector_config")  (verbatim, no emitter)
```

These describe the **same physical table** — same Glue database+table, same S3
warehouse, same `icebergConfig` partition/sort/file-format — yet the streaming
path re-derives none of it.

**What works today:** the Apache sink class is dispatched; Debezium embedded mode
defaults `debezium.sink.type=iceberg`; `_late_arrival.py` injects
`fluid.late_arrival.*`; `_common.py` aliases `sink.format=kafka → iceberg`.

**The gap:** no emitter, no catalog auto-wiring, no plan-time validation, no
schema-level Iceberg-table surface, no routing/exactly-once ergonomics.

---

## 3. Goals / Non-goals

**Goals**
- A user authoring a streaming Iceberg sink writes the **same binding** they'd
  write for a static Iceberg table — no parallel config dialect.
- Zero config drift between the provisioned catalog and the connector, enforced
  at plan time.
- Maximal reuse of existing forge machinery; **no new runtime engine, no new
  dependency, no schema major bump**.
- Backward compatible: every existing hand-authored `sink_connector_config`
  keeps working **byte-for-byte**.

**Non-goals (v1)**
- Reimplementing exactly-once / commit coordination / compaction (owned upstream).
- CDC/upsert equality-deletes (deferred — see §4).
- REST/GCP catalogs end-to-end (Glue-first — see §4).
- A forge-managed compaction subsystem (advisory-only — see §4).

---

## 4. Scope decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| **Schema rollout** | **Opt-in 0.7.5, no default bump** | Adding a bundled schema shifts `latest_bundled_version()` → `BUNDLED_VERSIONS[-1]` (`schema_manager.py:349`), silently re-defaulting every untagged contract and churning plan/bundle digests install-wide. Opt-in via explicit `fluidVersion` avoids this entirely. |
| **CDC / upsert** | **Deferred to v2; `upsertMode` rejected at plan time** | Upsert needs `transforms.*` SMT co-emission (`DebeziumTransform`) + `cdc-field` wiring that is **entirely absent** in the runner tree today. Shipping `upsertMode` without it is the silent-append-only trap. v1 is append-only. |
| **Catalog scope** | **AWS Glue only for v1** | Glue is the only catalog forge can both **auto-wire** and **statically provision** today. The zero-drift guarantee is real only where a static twin exists. REST/GCP land in PR7. |
| **Maintenance** | **Advisory + runbook for v1** | The Apache connector never compacts. v1 emits a plan-time small-files advisory and documents the external `rewrite_data_files`/`expire_snapshots` runbook; a forge-managed schedule-sync job is a separate, later subsystem. |

---

## 5. Borrow-before-build receipts

```
Searched:
- "Kafka to Iceberg streaming sink open source 2026" → Apache Iceberg Connect + AutoMQ are the only OSS leaders; 7 of 9 surveyed options are proprietary/managed
- "Apache Iceberg Kafka Connect sink connector catalog configuration" → standardized iceberg.catalog.* prefix; REST/Glue/Nessie/Hive/JDBC/DynamoDB/BigQuery catalogs; iceberg.tables vs dynamic route-field
- Fetched iceberg.apache.org/docs/nightly/kafka-connect → multi-table fan-out (static+dynamic), SMTs (DebeziumTransform, DmsTransform, JsonToMap), exactly-once via control topic
- Fetched automq.com "Top 9 Ways to Stream Kafka→Iceberg (2026)" → confirms only Kafka-Connect-sink + AutoMQ are OSS; everyone else is commercial
- Fetched rmoff.net "Iceberg on S3 via Kafka Connect + Glue catalog" → minimal Glue config: catalog-impl=GlueCatalog, io-impl=S3FileIO, warehouse=s3://, client.region; creds via DefaultCredentialsProvider chain (no inline keys)
```

**Reuse strategy**

| Borrow | Project | How |
|---|---|---|
| **Depend on** | Apache Iceberg Kafka Connect (Apache-2.0) | the entire `iceberg.catalog.*`/`iceberg.tables.*` key surface is templated verbatim; `connector.class = org.apache.iceberg.connect.IcebergSinkConnector` (guard the legacy `io.tabular.*` namespace); all runtime semantics owned upstream |
| **Adapt pattern** | rmoff / Aiven Glue refs | minimal Glue config, creds via DefaultCredentialsProvider — forge emits **zero** inline AWS keys for the self-managed binding |
| **Adapt pattern** | dlt (Apache-2.0) | "infer partition / id-columns from existing declarations"; partition-transform vocabulary already aligns 1:1 with `icebergConfig.partitionSpec` |
| **Internal reuse** | forge's own static Glue path | `providers/aws/util/formats.py::get_iceberg_config` + the planner warehouse derivation become the catalog source-of-truth, not re-derived |
| **Pattern** | forge `avro_converter_config` / `extract_late_arrival_policy` | the deriver is a pure flat `str→str` dict in the identical idiom |
| **Diverge** | AutoMQ broker-native | noted as a **future second engine** (binding-level, additive) — not v1 |

---

## 6. Design

### 6.1 The shared resolver (zero-drift mechanism)

```
resolve_iceberg_catalog(binding, contract, platform) -> ResolvedIcebergCatalog
   { catalog_type, catalog_impl, warehouse, region, io_impl,
     fq_table, write_props, partition_by, id_columns }
        │                                         │
        ▼  EXISTING static path                   ▼  NEW streaming deriver
 glue.ensure_iceberg_table                  emit_iceberg_sink_config()
   → iac/providers/aws.py:_emit_glue           → flat str→str connector map
   (provisions the Glue table)                 merged UNDER hand-written config
```

New module `fluid_build/providers/_iceberg_catalog.py`:

- `resolve_iceberg_catalog(binding, contract, platform) -> ResolvedIcebergCatalog`
  (frozen dataclass) — the **single source of truth**.
- It calls a new shared helper `get_iceberg_warehouse(loc, account_id)` that is
  **the sole writer** of the `s3://…` warehouse string, called from **both** the
  planner and the resolver. This de-duplication *is* the zero-drift guarantee
  (see §7 — it is a correction, not a clean extraction).

### 6.2 The deriver

New module `fluid_build/build_runners/kafka_connect/iceberg_sink.py`:

```python
def emit_iceberg_sink_config(resolved, ctx, kc_props) -> dict[str, str]:
    """Pure, deterministic, credential-free. Mirrors avro_converter_config()."""
```

Emits:
- `connector.class = org.apache.iceberg.connect.IcebergSinkConnector` (new
  namespace; guard against legacy `io.tabular.*`)
- the `iceberg.catalog.*` block from `resolved` (prefix-passthrough: derive a
  tiny set, forward the rest verbatim via an `iceberg_catalog_overrides` escape
  hatch)
- `iceberg.tables = <resolved.fq_table>`, `default-id-columns`,
  `default-partition-by`
- `iceberg.control.topic = _iceberg-control-{product_id}` (**never** the shared
  `control-iceberg` default — the documented Redpanda/Aiven multi-connector
  collision pitfall). **Spike-validated** (§14): a custom control topic was
  accepted and the connector committed normally.
- **`key.converter` / `value.converter`** — the connector reads the worker's
  converters and **records must deserialize to a struct/map**. The spike
  (§14) confirmed schemaless JSON requires
  `value.converter=org.apache.kafka.connect.json.JsonConverter` +
  `value.converter.schemas.enable=false`. The deriver co-emits these (or
  inherits validated worker defaults) — **a v1 requirement the paper design
  had left implicit**, surfaced by the live run.

**Wiring** at `runner.py:274`, *before* reading `sink_connector_config`:

```python
derived = emit_iceberg_sink_config(resolve_iceberg_catalog(...), ctx, kc_props)
sink_config = {**derived, **(kc_props.get("sink_connector_config") or {})}
#               └ derived first → HAND-WRITTEN KEYS ALWAYS WIN (back-compat)
```

Gated by `kc_props.get("iceberg_sink_enabled", <default>)`. **Default is `False`
when a hand-written `sink_connector_config` is already present** (see §8 — this
closes the injected-key back-compat hole the critic flagged).

Debezium embedded mode gets the **same resolved values** through a thin
re-key wrapper to `debezium.sink.iceberg.*` (one resolver, two call sites; the
re-key is tested for zero key-loss — §10).

### 6.3 Catalog wiring (Glue, v1)

`resolve_iceberg_catalog` for `platform = aws`:

| Key | Value |
|---|---|
| `iceberg.catalog.type` | `glue` |
| `iceberg.catalog.catalog-impl` | `org.apache.iceberg.aws.glue.GlueCatalog` |
| `iceberg.catalog.io-impl` | `org.apache.iceberg.aws.s3.S3FileIO` |
| `iceberg.catalog.warehouse` | the **exact** `s3://{bucket}/{path}` `get_iceberg_warehouse` builds for the static table |
| `iceberg.catalog.client.region` | from `binding.location.region` |
| credentials | **none emitted** — DefaultCredentialsProvider chain (env / instance-profile / IRSA) |

REST (`type=rest`, warehouse = catalog **name**, requires `s3.*` creds via
`secret_ref`) and GCP (`GCSFileIO`) are **tagged-union variants deferred to
PR7**. Each variant declares its own required-key set, validated at plan time.

### 6.4 Contract surface (schema)

Deliberately schema-light — the Iceberg-table identity already lives in the
binding. Additive changes shipped as **opt-in `fluid-schema-0.7.5.json`**
(enum **must** retain `['0.7.3','0.7.4','0.7.5']`; `latest_bundled_version`
**not** advanced):

1. `iceberg_table` / `iceberg-table` registered as **aliases → `iceberg`** in
   `_common.py` (mirrors the existing `kafka → iceberg` / `kafka → kafka_topic`
   entries at `_common.py:342,348`). The schema validator only ever sees the
   canonical `iceberg`.
2. `$defs.icebergConfig` gains one optional nested object `streamingSink`:
   `{ commitIntervalMs?, routeField?, dynamicEnabled?, autoCreate?, evolveSchema?,
   upsertMode?, controlTopic? }` — `additionalProperties: false`, all optional,
   so every existing `icebergConfig` stays valid.
3. `metadata.primaryKey` → `default-id-columns` and `icebergConfig.partitionSpec`
   → `default-partition-by` are **reused, not re-declared**.

The only net-new authored surface is the optional `streamingSink` tuning block.

### 6.5 Routing

- **Single table** (default): `iceberg.tables = <fq_table>`, fully derived.
- **Dynamic fan-out** (opt-in): `streamingSink.dynamicEnabled = true` +
  `routeField` → `iceberg.tables.dynamic-enabled=true` +
  `iceberg.tables.route-field=<field>`.
- Routing mode is validated at the **topic/connector level** (across all
  bindings sharing a topic), never per-binding — a topic emitting both
  `iceberg.tables` and `dynamic-enabled` is rejected at plan time.

Vocabulary borrowed verbatim from the Apache sink — no invented routing surface.

### 6.6 Exactly-once & control topic

`builds[].properties.delivery.guarantee = exactly_once` (existing enum) drives:
- `iceberg.control.topic = _iceberg-control-{product_id}` (unique)
- `iceberg.coordinator.transactional.prefix = iceberg-coord-{product_id}`
- `iceberg.control.group-id-prefix = cg-control-{product_id}`

`product_id`-derived identifiers are normalized **`slugify → truncate(249) →
append a short stable hash of the original id`**: the slug keeps them readable,
the hash guarantees uniqueness even when two ids differ only in characters
illegal for a Kafka topic / transactional id (a silent cross-product EOS hazard
otherwise). Deterministic; pinned by a test (§10).

`commit.interval-ms` is derived via the **build→expose join** (the same
`build.outputs`/`exposeId` join as §6.8 #5) to read `exposeQoS.freshnessSLO`.
Precedence: `streamingSink.commitIntervalMs` > derived-from-`freshnessSLO` >
**explicit plan-time small-files advisory** — it **never** silently inherits the
connector's `300000ms` default.

### 6.7 Late arrival (honest)

**v1 decision: advisory-only.** `extract_late_arrival_policy` is reused to emit
the `fluid.late_arrival.*` connector keys **plus a plan-time advisory**, but v1
does **not** provision a `<db>.<table>__late_events` side table — those keys are
advisory until a real side table lands in v2. This is stated plainly rather than
implied as "zero-drift for free."

Two corrections (§7) still apply to the keys that *are* emitted:
- The existing call passes `target_table=connector_name_for_topic`
  (`runner.py:268`), **not** the fq_table. Switching it is a **behavior change**
  to the side-output table name and is therefore **gated to the new Iceberg
  path only** (`iceberg_sink_enabled`); non-Iceberg runners keep the old name.
- The advisory explicitly flags that nothing materializes the side table in v1,
  so an operator relying on late-event capture knows to handle it externally
  until v2.

### 6.8 Plan-time validation

New validator `validate_iceberg_sink(binding, contract, errors)` at the plan
seam (`cli/plan.py`, after action-type parse, before `plan.json` serialization):

1. **Catalog tagged-union completeness** — Glue requires
   `warehouse + region + io-impl`; reject `type` and `catalog-impl` both set.
2. **io-impl-required** — object-store warehouse (`s3://`, `gs://`, `abfss://`)
   must carry `io-impl` (the #1 "works in REST demo, fails on Glue" trap).
3. **`upsertMode` is rejected** in v1 (deferred — §4).
4. **Dynamic routing** requires `routeField`; mixed-mode topics rejected.
5. **Build→expose join (first-class)** — a streaming build with
   `sink.format=iceberg` must reference, via `build.outputs`/`exposeId`, an
   expose whose normalized `binding.format == iceberg`. Without this the two
   alias surfaces are not provably the same table.
6. **Unified cross-check** — assert the connector warehouse/region/fq_table
   **equals** the IaC-emitted values, **fail closed** on divergence — with two
   escape paths:
   - **Streaming-only (no static twin):** set
     `iceberg.tables.auto-create-enabled=true` so the **connector** creates the
     table, and **waive the cross-check (recorded waiver)**. A build-only
     streaming contract works without authoring a full static expose; the
     waiver is surfaced so the relaxed guarantee is never silent.
     **CORRECTION (spike-observed, §14):** `auto-create-enabled` creates the
     **table but NOT the namespace** — the live run failed with
     `NoSuchNamespaceException: Namespace default does not exist` until the
     namespace was created explicitly. So the auto-create fallback is
     **incomplete**: forge MUST still ensure the catalog **namespace/database
     exists** (a `createNamespace`/`ensure-database` step) even in the
     build-only case. In the provider-unified path the static Glue provisioning
     already creates the database (= namespace), so this gap bites *only* the
     streaming-only fallback — exactly where we'd assumed "auto-create handles
     it." Validator adds: streaming-only + auto-create ⇒ require a
     namespace-ensure action.
   - **Operator override present:** when a hand-written
     `iceberg.catalog.warehouse` is set, **defer to the operator (warn, not
     fail)** — preserves operator-wins.

### 6.9 Secret materialization seam

`secretRef → literal` resolution happens at **apply/runner time**, never at plan
time. The connector config is **never** materialized into `plan.json` — so
`plan.json` stays credential-free and `planDigest` is byte-identical with or
without secrets present. The resolved literal appears only in the apply-time REST
POST body, whose logs are redacted (§7, redaction correction).

---

## 7. Verified divergence correction (PR1 detail)

The "zero-drift by construction" claim is **false against current `main`** until
a pre-existing divergence is corrected. Verified by direct read:

```python
# planner.py:509-513 — env-template resolution + account fallback + path default
bucket = _resolve_env_templates(raw_bucket) if raw_bucket else None
if not bucket or "{{" in bucket:
    bucket = f"{account_id}-fluid-data"
path = location.get("path", f"{database}/{table}/")
s3_location = f"s3://{bucket}/{path}"

# iac/providers/aws.py:355 — NO template resolution, NO account fallback, .lstrip('/')
storage["location"] = f"s3://{bucket}/{(loc.get('path') or '').lstrip('/')}"
```

For any contract that **(i) omits `bucket`**, **(ii) uses an env-template
bucket**, or **(iii) has a leading-slash `path`**, the two strings **differ**.

**Therefore PR1 is a *correction*, not a clean extraction.** `get_iceberg_warehouse`
must absorb all three planner behaviors AND become the sole writer in
`_emit_glue`. It ships a regression test that is **RED against current `main`**
(proving the divergence) and **GREEN after PR1** — across the
`{no-bucket, env-template-bucket, leading-slash-path}` matrix — **before** the
§6.8 cross-check is added, or the cross-check bricks currently-valid contracts.
Use the **module-attribute-access indirection pattern** so existing planner
*and* IaC-emitter test patches flow through.

---

## 8. Backward compatibility

| Hazard | Mitigation |
|---|---|
| Hand-written `sink_connector_config` | `{**derived, **handwritten}` — operator keys always win; deriver **off by default when a hand-written config is present** (no injected `control.topic`/`group-id` into hand-tuned deployments). |
| Schema version churn | **Opt-in 0.7.5; `latest_bundled_version` not advanced.** New schema enum retains `0.7.3`/`0.7.4`. No install-wide plan-digest churn. |
| Late-events side-table rename | fq_table-based naming gated to the new Iceberg path only; non-Iceberg runners keep `connector_name`-based naming. Test pins the old name. |
| Cross-check fails existing hand-authored warehouses | Cross-check defers to operator override (warn) and skips for streaming-only contracts. |
| PR1 warehouse correction shifts `main.tf.json` | Digest-stability test asserts no change except for the divergent `{no-bucket, env-template, leading-slash}` subset, which is documented. |
| New keys under `additionalProperties:true` kc block | Optional closed sub-schema for `iceberg_sink_enabled`/`iceberg_catalog_overrides` to catch typos at validate time. |
| Redaction asymmetry | §7 redaction fix targets the correct arm in both layers. |
| Apply actions | Catalog table still provisioned through the existing `glue.ensure_iceberg_table` action → both `op` and `action_type` preserved. |

**Redaction correction (verified):** the global redactor uses **substring**
matching (`secret_redactor.py:155` `any(part in lowered …)`) — so
`iceberg.catalog.s3.secret-access-key` is masked for free — but it **misses
`sasl.jaas.config`** (no sensitive substring in the key). The provider-local
Snowflake redactor uses **exact-set** membership
(`snowflake/util/logging.py:244` `key.lower() in SENSITIVE_KEYS`) plus a separate
`SENSITIVE_PATTERNS` regex arm. So "extend both with key tokens" is a **no-op for
the provider layer**. The fix: add `sasl.jaas.config` to the global
`_SENSITIVE_KEY_PARTS`, and target the provider-local **`SENSITIVE_PATTERNS`
regex arm** (not `SENSITIVE_KEYS`). A test feeds a dotted iceberg key through
**both** paths and asserts masking in each, plus a **serialized** POST-body line.

---

## 9. Phased delivery (7 PRs, each green & reviewable)

| PR | Title | Ships |
|---|---|---|
| **1** | Correct + unify warehouse derivation | `get_iceberg_warehouse()` absorbing all 3 planner behaviors, sole writer in planner + `_emit_glue`; RED-then-GREEN drift test. **No user-visible change.** |
| **2** | Pure deriver + opt-in wiring | `emit_iceberg_sink_config()`; merge-precedence at `runner.py:274`; default-off when hand-written config present; unique control topic. |
| **3** | Schema + alias (opt-in 0.7.5) | `iceberg_table` alias; `icebergConfig.streamingSink`; enum retains 0.7.3/0.7.4/0.7.5; `latest_bundled_version` unchanged. |
| **4** | Plan-time validator | tagged-union, io-impl-required, `upsertMode` rejection, routing exclusivity, build→expose join, cross-check with skip/defer. |
| **5** | Debezium parity + late-arrival | re-key to `debezium.sink.iceberg.*`; gated fq_table side-output naming. |
| **6** | Secret-redaction hardening | `sasl.jaas.config` + correct provider-local arm; no-leak serialized-line test. |
| **7** | REST + GCP catalog profiles | tagged-union variants; per-platform required-key validation. (Static-twin caveat documented.) |

Per repo etiquette: incremental commits, conventional-commits style, each PR
green in CI before the next.

---

## 10. Testing strategy

**Unit (default suite)** — resolver↔planner warehouse equality (the zero-drift
assertion, parametrized over the divergence matrix); deriver determinism
(`json.dumps(sort_keys=True)` stable, correct `connector.class`, unique control
topic); catalog tagged-union matrix; validator matrix (io-impl, `upsertMode`
rejection, routing exclusivity, build→expose join, cross-check skip/defer);
back-compat merge precedence + default-off-when-hand-written; alias symmetry
(mirrors `tests/test_product_type_mapping.py` pinning style); redaction on dotted
keys **and** serialized lines through **both** layers; Debezium re-key parity;
plan.json credential-free + digest stability.

**Integration (gated, self-skipping)** — per repo policy Docker emulators are
**integration-stage-only**. A `FLUID_TEST_ICEBERG_LIVE=1`-gated end-to-end stands
up Kafka + Kafka Connect with the `org.apache.iceberg.connect` plugin + a catalog
+ MinIO/LocalStack, produces N records, and asserts rows **actually land** in the
targeted table with matching warehouse/fq_table — the only test that proves the
emitted strings are correct, not merely plausible. REST mocks (`respx`,
mirroring `tests/build_runners/test_kafka_connect_full_matrix.py`) cover the
create→update idempotency lifecycle in the default suite.

---

## 11. Limitations & unsolved (honest)

These are true of **every** Connect-based path (confirmed by the AutoMQ survey),
not artifacts of this design:

- **No compaction / `expire_snapshots`.** Low `commit.interval-ms` → small-files
  explosion. v1 surfaces a plan-time advisory + runbook; a forge-managed
  maintenance job is future work.
- **`RUNNING`-but-erroring-per-record. — SPIKE-OBSERVED (§14).** In the live run
  the connector reported `connector.state=RUNNING` while `tasks[0].state=FAILED`
  with a full stack trace. v1 surfaces task-level errors
  (`GET /connectors/<n>/status → tasks[].trace`) into run-record facets — **the
  runner must inspect task state, not just connector state** — and wires
  `errors.tolerance` / `errors.deadletterqueue.topic.name`.
- **Duplicates without exactly-once. — SPIKE-OBSERVED (§14).** A task restart
  before commit re-delivered records (at-least-once), producing duplicate Iceberg
  rows. Confirms the §6.6 control-topic + transactional config is **necessary,
  not optional**, whenever `delivery.guarantee=exactly_once`.
- **The unified guarantee is AWS-Glue-strongest.** REST catalogs
  (Snowflake Open Catalog/Polaris) have warehouse-as-name + external S3 creds and
  **no static IaC twin** — so for REST the cross-check has nothing to compare
  against. Documented, not hidden.
- **Connector version coupling.** The `org.apache.iceberg.connect.*` namespace,
  `group-id-prefix`, and `GCSFileIO` class names drift across Iceberg runtime
  versions; v1 pins a target connector version and surfaces it.

---

## 12. Alternatives considered

A four-way design panel scored each approach (independent multi-lens judging):

| Approach | Score | Why not |
|---|---|---|
| **Provider-unified (this RFC)** | **52/60** | — |
| Thin emitter + convention catalog | 50.67 | No catalog provisioning unification → drift possible; **grafted** its pure flat-dict emitter idiom. |
| Schema-first typed block | 49.33 | Heavier new schema surface for little gain; **grafted** its tagged-union catalog validation. |
| Pluggable catalog registry | 48.33 | Premature abstraction for one catalog in v1; **grafted** its per-platform profile shape for PR7. |

A future **AutoMQ broker-native** engine is intentionally left as an additive,
binding-level second engine (zero contract-schema churn).

---

## 13. Resolved design decisions (were open)

All four secondary questions are now decided; the design sections above reflect
them:

| # | Question | Decision | Where |
|---|---|---|---|
| 1 | Late events | **Advisory-only for v1** — emit `fluid.late_arrival.*` + a plan-time advisory; no side table until v2. | §6.7 |
| 2 | Streaming-only contract (no static twin) | **Auto-create fallback** — `iceberg.tables.auto-create-enabled=true`, connector creates the table, cross-check waived + recorded. | §6.8 #6 |
| 3 | `product_id` sanitization | **Sanitize + stable hash suffix** — `slugify → truncate(249) → append short stable hash`; collision-safe. | §6.6 |
| 4 | `commit.interval-ms` derivation | **Derive from `freshnessSLO` via the build→expose join**; precedence `commitIntervalMs` > `freshnessSLO` > advisory; never inherits `300000ms`. | §6.6 |

No open questions remain for v1 scope. Implementation-detail refinements (exact
slug charset, advisory wording, waiver record shape) are settled in their PRs.

---

## 14. Spike validation — observed end-to-end run (2026-06-21)

This RFC is **not paper.** A throwaway Docker spike ran a real Kafka → Iceberg
write end-to-end and the design was corrected against what was observed.

**Stack** (borrowed from `apache/iceberg`'s own integration compose, Apache-2.0):
`confluentinc/cp-kafka` + `cp-kafka-connect` (KRaft), the
`apache/iceberg-rest-fixture` REST catalog, and MinIO for S3. Connector: the
prebuilt `iceberg-kafka-connect-runtime` **0.6.19** (the only published runtime
zip). Verification: `pyiceberg` reads the table inside an ephemeral container.

**Version caveat (honest):** 0.6.19 carries the legacy
`io.tabular.iceberg.connect.IcebergSinkConnector` class and `iceberg.control.group-id`;
v1 ships against Apache **1.11.0** (`org.apache.iceberg.connect.*`,
`group-id-prefix`). The **config-key surface is otherwise identical** — verified
by diffing the 0.6.19 README against the 1.11.0 docs — so the observed run
validates the entire key surface; only those two constants differ and both are
independently doc-confirmed for 1.11.0.

**Result: PASS.** 15 rows physically committed to `default.events`; schema
auto-inferred (`amount:double, name:string, id:long, region:string`); 2 snapshots;
connector + task `RUNNING`. The exact config that worked is the same shape the
deriver emits (custom `iceberg.control.topic=_iceberg-control-spike` accepted;
`iceberg.catalog.{type,uri,warehouse,io-impl,client.region,s3.*}` + JSON
converters).

**Three corrections the live run forced into the design:**

| # | Observed | RFC correction |
|---|---|---|
| A | `auto-create-enabled=true` created the table but failed with `NoSuchNamespaceException: Namespace default does not exist` until the namespace was created explicitly. | The **auto-create fallback is incomplete** — forge must ensure the **namespace/database** exists even build-only. Folded into §6.8 #6. |
| B | `connector.state=RUNNING` while `tasks[0].state=FAILED` with a full trace. | The runner **must inspect task state + trace**, not just connector state. Confirmed §11 (now marked spike-observed). |
| C | Schemaless JSON required `value.converter=JsonConverter` + `schemas.enable=false`; a task restart re-delivered records (duplicates) with no EOS. | The deriver must **co-emit converters** (§6.2, was implicit) and EOS config is **necessary not optional** (§11). |

**Lightweight posture preserved:** the spike is Docker-only and ephemeral
(`/tmp`, torn down). `pyiceberg` ran *inside a container*, never in forge's venv.
What lands in-repo is a single `FLUID_TEST_ICEBERG_LIVE=1`-gated integration test
(§10) — the default suite and `pyproject.toml` are untouched; the shipped feature
remains a pure-stdlib flat-dict emitter.

---

## 15. Enterprise / managed topology — Confluent OpenTofu provider (FOLLOW-ON)

> **Framing (honest, post-adversarial-review).** This is **not** a co-equal
> third path shipped with v1. It is a **sequenced follow-on** to the
> self-managed sink above, and the honest version is **narrower** than the
> first sketch. Verified against the `confluentinc/confluent` provider (official,
> actively maintained) and forge's own `iac/` code (`base.py:18-111`).

**The opportunity.** Cloud `apply` already routes through OpenTofu, and the
`IacProviderPlugin` interface means a Confluent plugin is **one file
(`iac/providers/confluent.py`) + one `register_iac_plugin()` + one
`OPENTOFU_DEFAULT_PROVIDERS` literal — zero core edits to the emit path**
(verified against `base.py`/`module.py`/`cutover.py`; Snowflake is the SaaS
reference plugin). "Engine" becomes a binding concern:
`binding.platform=confluent` + `builds[].engine=<topology>`.

**Three candidate topologies, graded honestly:**

| Topology | Resource(s) | Verdict |
|---|---|---|
| **A. Self-managed Connect** (this RFC, §1–§14) | deriver → Connect REST | **v1 — still unbuilt; the prerequisite** |
| **B. `confluent_connector`** (managed connector) | `confluent_connector{config_*}` | **DROP (confirmed F1, §15.1)** — Confluent Cloud has no managed Iceberg sink; the OSS `org.apache.iceberg.connect` class runs only via Custom Connector upload. Pays lock-in *without* the compaction payoff; the only niche (`S3_SINK`→Parquet) is off-thesis. |
| **C. `confluent_tableflow_topic` + `confluent_catalog_integration`** (Tableflow) | managed topic→Iceberg + Glue/Snowflake/Unity catalog | **Strongest FOLLOW-ON** — the real managed Kafka→Iceberg route; **closes the compaction/snapshot-expiry gap** every Connect path (incl. self-managed) leaves to the operator; rides `main.tf.json`. **v1-of-the-follow-on restricted to `byob_aws` + `aws_glue`.** |

**Hard prerequisites & corrections (from the adversarial review):**

1. **Dependency inversion.** `resolve_iceberg_catalog` / `get_iceberg_warehouse`
   / `emit_iceberg_sink_config` **do not exist yet** (grep-confirmed). This
   topology is **gated on PRs 1–4 of this RFC landing**; the "verbatim reuse /
   four call sites / zero-drift" spine is a design contract until then.
2. **Plan-binding is TRANSITIVE, not direct.** `main.tf.json` is **re-emitted
   from the contract at apply** (`build_module`); `bundleDigest` binds the
   *contract*, not the `.tf.json` bytes. The load-bearing guarantee is therefore
   **`emit()` determinism** — needs a *same-contract → byte-identical
   `main.tf.json`* test. Do **not** claim "a tampered `plan.json` connector class
   is rejected" for contract-derived resources. (And note: plan-binding coverage
   is **not** Confluent-exclusive — a self-managed-connector IaC resource could
   close the same gap lock-in-free; weigh that baseline first.)
3. **The `(platform, engine)` validator lives in the contract-validation stage**
   (`cli/validate.py`), **not** in `emit()` (pure-by-Protocol, no error surface —
   a fall-through there is the "silent no-op apply" anti-pattern the OpenTofu
   cutover retired). RED test: `platform=confluent` + `engine=dbt|absent` must
   fail `fluid validate`.
4. **Credential-free posture only for v1.** Ship **`byob_aws` + `aws_glue` +
   `confluent_provider_integration`** (Confluent assumes a customer IAM role — no
   inline keys). **DEFER** `snowflake`/`unity` catalog integration and Tableflow
   API `credentials{}` — `iac/credentials.py` only overlays *provider-level* env
   vars; there is **no resource-body-secret seam** today (adding one is a core
   change, not a one-file plugin).
5. **`provider_integration_id` is a TWO-PHASE IAM bootstrap** — Confluent's
   external-id is known only *after* the integration is created, then the AWS
   trust policy must be updated and re-applied. Single-pass OpenTofu can't
   express this; treat it as a **manual operator prerequisite** with the
   external-id surfaced **post-apply**, not "at plan time."
6. **`managed_storage` EXCLUDED from v1.** Data lives in Confluent's account with
   **no forge catalog twin** → the zero-drift guarantee the design is named after
   **evaporates**. `byob_aws` only (exit-friendly: tables stay readable by any
   Iceberg engine after leaving Confluent).
7. **Schema surface undercount.** `bindingLocation` is `additionalProperties:false`
   — Tableflow needs **new optional keys** (`environment_id`, `cluster_id`,
   `confluent_role_arn`), additive under opt-in `0.7.5`. Not "two enum values,
   no new fields."
8. **Column-axis drift (new).** Tableflow derives **and auto-evolves** the Iceberg
   schema from the topic's Schema-Registry subject → it can drift from the
   contract's `schema[]` on the **column axis** even when warehouse/db/fq_table
   match. The zero-drift thesis (identity-only today) must be extended to the
   column axis or scoped honestly.
9. **Cost honesty at v1.** Surface Confluent metered cost (cluster eCKU +
   Tableflow throughput/storage/compaction + egress) in the **preview/cost
   panel** for managed engines — a v1 gate under the world-class bar, not later
   hardening.

**Two binary questions — ANSWERED 2026-06-21 (F1 partial, doc + live-API grounded, $0, no provisioning):**

- **(a) Create vs adopt — ANSWERED: Tableflow CREATES + OWNS the table; the
  database must pre-exist.** Confluent's required Glue IAM policy is
  `glue:GetTable` + **`glue:CreateTable`** + **`glue:UpdateTable`** on
  catalog/database/table ARNs — and conspicuously **no `glue:CreateDatabase`**.
  So Tableflow materializes and owns the *table*, and the Glue *database* must
  already exist. It does **not** cleanly adopt a forge-pre-created table — a
  forge-created `database.table` of the same name is a **double-ownership
  conflict**. **Design conclusion: forge provisions the Glue DATABASE
  (namespace); Tableflow owns the TABLE.** This is the *same split* the OSS
  Kafka-Connect spike found (§14 A: auto-create makes the table, not the
  namespace) — a satisfying cross-topology consistency: **the writer owns the
  table, forge owns the namespace.**
- **(b) Managed-class feasibility — ANSWERED: NO.** Confluent Cloud has **no
  fully-managed Iceberg sink connector**; the OSS `org.apache.iceberg.connect`
  class runs only via "Bring Your Own Connector" (custom upload). The managed
  Iceberg path **is Tableflow**. → **Path B is removed from v1** (custom-upload
  only), exactly as the adversarial review predicted.

*F1 cost: $0.* Both answers came from the Confluent Cloud API (auth-verified,
account inventory) + the published Glue catalog-integration IAM contract — no
cluster, no Tableflow, no billable resource created. A live byob_aws+Glue run
would only refine the *residual* nuance (does `CreateTable` hard-fail on a
pre-existing table, or does Tableflow `GetTable`→`UpdateTable` *take it over*?) —
which does **not** change the design conclusion (don't double-own; forge owns the
namespace only). Recommended: **skip the billable live run** unless
belt-and-suspenders confirmation of that nuance is wanted.

**Lock-in truth.** `byob_aws` + external catalog is the exit-friendly default and
the only variant where "unified/zero-drift" is coherent. Tableflow's **managed
compaction is the genuine upside** that justifies the lock-in for teams that
don't want to run Iceberg maintenance — but cost is contract-*visible* and
contract-*reversible* only; the **data layer is not free to move**
(`managed_storage` especially).

**Follow-on phasing (all gated behind this RFC's resolver/deriver PRs):**
`F0` schema + `(platform,engine)` validator → `F1` **live Confluent spike**
(questions a & b) → `F2` `iac/providers/confluent.py` emitting Tableflow +
`aws_glue` catalog integration (`byob_aws` only) + determinism/credential-free
unit tests → `F3` `FLUID_IAC_LIVE_CONFLUENT=1` tier-3 test + `fluid-iactest-*`
sweep (mirrors the existing `FLUID_IAC_LIVE_{AWS,GCP,SNOWFLAKE}` gates).

---

## Appendix A — borrowed config-key surface (Apache Iceberg Connect)

| Key | Role | forge derivation |
|---|---|---|
| `connector.class` | sink class | constant `org.apache.iceberg.connect.IcebergSinkConnector` |
| `iceberg.catalog.type` / `catalog-impl` | catalog kind | from `binding.platform` |
| `iceberg.catalog.warehouse` | warehouse root | `get_iceberg_warehouse()` (shared with static path) |
| `iceberg.catalog.io-impl` | FileIO | per-platform (`S3FileIO` for AWS) |
| `iceberg.catalog.client.region` | region | `binding.location.region` |
| `iceberg.tables` | static target | `<fq_table>` |
| `iceberg.tables.dynamic-enabled` / `route-field` | fan-out | `streamingSink.dynamicEnabled` / `routeField` |
| `iceberg.tables.default-id-columns` | upsert keys | `metadata.primaryKey` (v2 only) |
| `iceberg.tables.default-partition-by` | partitioning | `icebergConfig.partitionSpec` |
| `iceberg.control.topic` | EOS control | `_iceberg-control-{product_id}` (spike-validated custom topic) |
| `iceberg.coordinator.transactional.prefix` | EOS | `iceberg-coord-{product_id}` |
| `iceberg.tables.auto-create-enabled` | build-only fallback | `true` for streaming-only — **but namespace must pre-exist** (§14 A) |
| `key.converter` / `value.converter` | record decode | `JsonConverter` + `schemas.enable=false` (schemaless JSON; §14 C) |
| (catalog namespace) | table parent | **forge must `ensure-namespace`** — auto-create does NOT (§14 A) |

## Appendix B — verification log

All load-bearing claims independently confirmed against source (2026-06-21):

| Claim | Location |
|---|---|
| Apache sink class wired | `build_runners/kafka_connect/runner.py:68` |
| Hand-written sink config passthrough | `build_runners/kafka_connect/runner.py:274` |
| Warehouse derivation divergence | `providers/aws/plan/planner.py:509-513` vs `iac/providers/aws.py:355` |
| Redactor substring vs exact-set | `observability/secret_redactor.py:155` vs `providers/snowflake/util/logging.py:244` |
| Late-arrival target = connector name | `build_runners/kafka_connect/runner.py:268` |
| `kafka → iceberg` / `kafka_topic` aliases | `cli/_common.py:342,348` |
| Default version = newest bundled | `schema_manager.py:349` |
