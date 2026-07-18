# RFC: Declarative packaging modes — isolated vs shared infrastructure

> Status: PROPOSED (2026-07-18). Preview target retargeted 0.7.5→0.7.6: this RFC was
> authored while 0.7.5 was the open preview; 0.7.5 went GA in v0.12.0, so the
> additive packaging fields land in the 0.7.6 preview per the schema lifecycle.

**Status:** Proposed · **Target schema:** 0.7.6 (preview, per `schema_manager.PREVIEW_VERSIONS`) · **Scope:** `fluid_build/iac/` emit path + schema + validators

---

## Motivation

Today every contract that names an infrastructure *container* (bucket, dataset, database, schema, warehouse) **owns** it: the IaC emitter produces a managed resource with `force_destroy: true`, in a per-contract OpenTofu state. Two contracts naming the same bucket or dataset each emit an owned copy in separate states — destroying product B can delete product A's data. The worst case is the AWS `{account}-fluid-data` fallback bucket (`providers/aws/util/warehouse.py`), which is emitted *owned by every contract that hits the fallback*. A second, related control-plane bug: `iac/backend.py:40` defaults every contract to the same `fluid/terraform.tfstate` key, so two contracts pointed at one `--state-backend` spec silently clobber each other's state — and pooled infrastructure is exactly the topology where a platform team hands out one state bucket.

Platform teams need a way to say: *this container is a platform-owned tenant pool; the product writes into it but does not own it.* The repo already has one reference-not-create emitter (the Confluent plugin) and one declarative deployment-mode enum (`acquisitionDeployment.mode`). This RFC formalizes that pattern into a first-class, additive, preview-gated schema block. Prior art: Unity Catalog's `isolation-mode: OPEN|ISOLATED`, Terraform's resource-vs-data-source ownership split, and Kubernetes dedicated-vs-shared node pools.

## Design overview

One new schema block, `packaging`, declares per-container ownership:

- **`isolated`** — this product creates and owns the container. Emit is today's exact resource emit.
- **`shared`** — the container is a pre-existing, platform-owned pool. Emit becomes an OpenTofu **data source** (`emit_data`, already in the `IacProviderPlugin` protocol) plus leaf-only owned resources (tables, prefixed objects, scoped grants). Exactly zero contracts own the pool container.

Hybrid tiers (e.g. Snowflake shared-database / owned-schema / owned-warehouse) fall out of a per-container-kind override map — a placement consequence, not a third mode.

**The compatibility invariant, enforced not promised:** an absent `packaging` block resolves to an explicit **LEGACY** branch in one pure chokepoint (`iac/packaging.py::resolve_packaging`) — *not* "implicit isolated with improvements" — and emits `main.tf.json` **byte-for-byte identical** to today, including `force_destroy: true` and the fallback bucket. A golden-file pin test (`tests/iac/test_iac_packaging_default_pin.py`) asserts this over **every existing fixture contract** and is the release gate. Combined with the 0.7.6 preview gate (untagged contracts never re-default into preview versions), no existing contract's bundle/plan digest can churn and `--shadow` parity holds mechanically.

**Honest surface accounting:** the pitch is "one enum" but the real surface is a small matrix — two-level precedence (`binding.packaging` > top-level `packaging` > absent-LEGACY) plus a six-kind `containers` override map plus a semantics-free `pool` label. The most common real pattern (Snowflake shared-DB / own-schema) needs the full three-layer form on day one, and "shared" means different things per platform (AWS: data source + prefix-scoped policies; GCP: dataset-level IAM downgraded to table-level; Snowflake: no data block, plan-time provider error on a missing pool). The docs must teach the matrix, not the one-liner. The compensating simplicity: zero new CLI flags, zero new stages, fully reversible by deleting the block.

## Contract-spec surface (0.7.6 preview)

New `$defs.packaging` in `fluid-schema-0.7.6.json`, referenced from a new top-level `packaging` property (contract-wide default) and a new `binding.packaging` property (per-exposure override). All `additionalProperties: false`, all optional, "NEW in v0.7.6" description markers per convention.

```yaml
# $defs.packaging
packaging:
  mode: isolated | shared        # default when block present: isolated
  pool: analytics-core           # optional pool id; propagated as fluid_pool label/tag
  poolManifest: pools/sales.yaml # optional platform pool file; snapshotted into the bundle
  containers:                    # optional per-kind override -> hybrid tiers
    bucket:    isolated | shared
    dataset:   isolated | shared
    database:  isolated | shared # covers Snowflake `database` AND AWS `glue_database`
    schema:    isolated | shared
    warehouse: isolated | shared
    cluster:   isolated | shared # confluent env/cluster (v1: shared only)
```

**Container-kind ↔ platform mapping (normative, fixes the vocabulary gap):**

| kind | AWS | GCP | Snowflake | Confluent |
|---|---|---|---|---|
| `bucket` | `aws_s3_bucket` | `google_storage_bucket` | — | — |
| `database` | `aws_glue_catalog_database` | — | `snowflake_database` | — |
| `dataset` | — | `google_bigquery_dataset` | — | — |
| `schema` | — | — | `snowflake_schema` | — |
| `warehouse` | — | — | `snowflake_warehouse` | — |
| `cluster` | — | — | — | environment/cluster |

**Example 1 — Snowflake hybrid tier (shared lake, isolated compute):**

```yaml
fluidVersion: "0.7.6"
id: orders-cdp
metadata: { layer: Gold, productType: CDP }
packaging:
  mode: shared
  pool: sales-domain
  containers:
    schema: isolated          # own schema inside the pooled database
    warehouse: isolated       # dedicated WH -> per-product cost attribution
exposes:
  - id: orders
    binding:
      platform: snowflake
      format: table
      location: { database: SALES_POOL, schema: ORDERS_CDP, table: ORDERS, warehouse: ORDERS_CDP_WH }
```

Emit: no `snowflake_database` resource; `snowflake_schema` / `snowflake_table` / `snowflake_warehouse` owned, with the database referenced **by literal name** (see file 5 below); grants at schema/table level only.

**Example 2 — shared S3 pool with prefix tenancy:**

```yaml
packaging: { mode: shared, pool: iot-lake }
exposes:
  - id: telemetry
    binding:
      platform: aws
      format: iceberg
      location: { bucket: acme-iot-lake, path: telemetry/, glue_database: iot_pool, table: telemetry }
```

Emit: `data.aws_s3_bucket`, `data.aws_glue_catalog_database`, owned `aws_glue_catalog_table`, IAM/LF grants scoped to `arn:…:acme-iot-lake/telemetry/*`. The `{account}-fluid-data` fallback bucket becomes referenced-not-created under shared mode.

**Example 3 — env overlay flips the mode.** `packaging` is a plain dict, so `loader._deep_merge` handles it with zero loader changes, and overlays fold at bundle stage 1 so plan-binding digests see the resolved value. **Sharp edge (called out, unlike the draft):** deep-merge is *key-wise*, so an overlay of `packaging: {mode: isolated}` retains the base's `containers:` map — a base `containers: {bucket: shared}` silently survives the flip. The validator warns when an overlay changes `mode` while inheriting a `containers` map from base; the documented idiom is to restate the block wholesale:

```yaml
# overlays/prod.yaml — restate, don't patch
packaging: { mode: isolated, containers: {} }
```

Per-env positional list-merge on `exposes[]` remains the repo's sharpest merge edge; top-level `packaging` sidesteps it for the common case and the docs model the positional-patch form explicitly.

## Architecture & files touched

1. **`fluid_build/schemas/fluid-schema-0.7.6.json`** — `$defs.packaging`; top-level + `$defs.binding` refs. Additive; every 0.7.1–0.7.6 contract stays valid.

2. **`fluid_build/iac/packaging.py`** (new, ~140 LOC, no heavy imports — cold-path safe): `resolve_packaging(contract, expose) -> PackagingDecision` mapping each container kind to `LEGACY | OWNED | REFERENCED` + pool id. **LEGACY is a distinct sentinel**, never conflated with OWNED, so the no-block path is a provable no-op. Single resolution chokepoint (mirrors `product_types.normalize_metadata_in_place`); all plugins consume it, none reimplement precedence. Pure function of the contract → deterministic, digest-stable.

3. **`fluid_build/iac/providers/aws.py`** — REFERENCED bucket/glue_database → `emit_data`; suppress `force_destroy`; scope `aws_s3_bucket_policy` / LF `registerLocation` to the `location.path` prefix; stamp `fluid_pool` into Glue table parameters alongside `fluid_layer`/`fluid_product_type`. **`discover_imports` consults `resolve_packaging` and emits no import candidate for any REFERENCED container** (see the correctness note below).

4. **`fluid_build/iac/providers/gcp.py`** — REFERENCED dataset → `data.google_bigquery_dataset`; suppress dataset `access[]` blocks and emit `google_bigquery_table_iam_member` per table (the `metadata.policies` → `_bq_access_entries` precedent moves down one level); REFERENCED bucket → data source + prefix-conditioned `google_storage_bucket_iam_member` (requires uniform bucket-level access on the pool — validator warns). Same `discover_imports` gating.

5. **`fluid_build/iac/providers/snowflake.py`** — REFERENCED database: drop the `snowflake_database` resource **and rewrite every consumer of it**. Today the schema body emits `"database": tofu_ref("snowflake_database.<db_res>.name")` (snowflake.py:319) and tables reference the same (333–334); dropping the resource alone leaves dangling references and `tofu validate` fails with "undeclared resource". The fix: when REFERENCED, `_schema_key` consumers inline the **literal database name** from `location.database` (uppercased per existing ident rules) into schema, table, and grant bodies. Isolated `warehouse` → new `snowflake_warehouse` resource with `auto_suspend`. Horizon table comment gains the pool id. Snowflake data sources are thin, so v1 emits no data block for the pool DB; a missing pool fails at `tofu plan` with a raw provider error — accepted and documented, with a friendlier pre-flight probe in `fluid verify` (file 9). `discover_imports` (snowflake.py:183 adds `snowflake_database.<db_key>` candidates today) is gated identically.

   **Why the `discover_imports` gating is load-bearing, not polish:** `_adopt_existing` runs on every apply. Ungated, shared mode would attempt `tofu import` of the pool container into this product's state — either erroring (address absent from config) or, worse, **re-owning the shared container**: the exact failure the feature exists to prevent. A dedicated test asserts each plugin's import candidates exclude REFERENCED containers.

6. **`fluid_build/iac/providers/confluent.py`** — declare existing behavior: accept `cluster: shared` (no-op), reject `cluster: isolated` with "dedicated-cluster provisioning is not yet supported".

7. **`fluid_build/iac/backend.py::parse_backend`** — fix the state-key collision: when a contract carries a `packaging` block, the default key becomes `fluid/<safe_ident(contract.id)>/terraform.tfstate`; absent-block contracts keep the legacy `fluid/terraform.tfstate` (preserving the byte-parity pin). Pooling is precisely the topology that amplifies this bug, so it ships *with* the feature rather than deferred; a follow-up migration-noted PR flips the default for everyone.

8. **Native planners** (`fluid_build/providers/{aws,gcp,snowflake}/provider.py` plan path): plan.json is the digest-bound artifact the human reviews, and untouched it would still list container-*creation* actions under shared mode — the tofu gate keeps it safe, but the reviewed artifact would misrepresent ownership, against the plan-binding-as-review-contract philosophy. v1: the planner calls `resolve_packaging`, drops container-creation actions for REFERENCED containers, and stamps a `packaging` summary block into plan.json (`{container_kind: {decision, pool}}` per exposure) so approvers see effective ownership without recomputing precedence. Actions keep both `op` and `action_type` per the existing invariant.

9. **`fluid_build/cli/contract_validation.py`** (invoked from `cli/validate.py`) + **`cli/verify.py`** — semantic checks: `shared` requires the corresponding `bindingLocation` name (a pool must be addressable; no fallback-name invention except the documented `{account}-fluid-data` case); `packaging` on `platform: local` → warning, ignored; platform-irrelevant `containers` keys → warning; overlay mode-flip-with-inherited-containers → warning (Example 3). `fluid verify` gains a lightweight pool-reachability probe (list/describe the pool container with the applier's credentials) so the "green apply, unreachable product until the platform team grants USAGE" handshake failure surfaces at verify time with a friendly message instead of a runtime permission error.

10. **`fluid_build/cli/_apply_opentofu_engine.py`** — **pre-plan ownership-transition guard**: before `tofu plan`, diff prior state (`tofu state list`) against the new emit's ownership model. If the state contains a container resource the new emit references as data, fail closed with the exact per-resource `tofu state rm` commands — ownership surgery, zero bytes touched — and emit a structured `packaging_transition_blocked` event into the run record (resolving the audit gap: the surgery is manual in v1 but prescribed and logged, never improvised). Piggybacks the existing per-contract workdir machinery. **shared→isolated is NOT waved through:** naive `_adopt_existing` would import the pool as owned with `force_destroy` restored, gated only by documentation — reintroducing the blast radius. v1 requires an explicit `--adopt-shared-container` flag which logs a WARNING-level `packaging_adoption_override` audit event (same discipline as `--allow-data-loss`); when a `poolManifest` is present the guard also refuses if the manifest lists other tenants. The existing `_data_loss_blocked` gate remains the unconditional last line.

11. **Bundle stage 1** — if `packaging.poolManifest` is set, the pool file is **snapshotted into the bundle** so `bundleDigest` covers it: any pool-file edit cryptographically invalidates every downstream plan, making plan/apply drift against shared-infra config structurally impossible. Composes with the existing plan-binding gate for free. v1 uses the manifest only for the tenant-list check in file 10; quotas/admission semantics are v2.

12. **Tests** — `tests/iac/test_iac_packaging_default_pin.py` (**the release gate**: every existing fixture emits byte-identical `main.tf.json`); `tests/iac/test_iac_packaging_modes.py` (per-provider owned/referenced/hybrid emit matrix, Snowflake literal-inlining, `discover_imports` gating); transition-guard + adoption-flag tests; overlay-flip + inherited-containers-warning tests; plan.json packaging-block test; backend-key test; extend `tests/iac/test_iac_cross_account_emit.py` for prefix-scoped grants.

13. **Docs** — forge-docs repo only (per repo rule); one paragraph in `AUTOGEN_SPIKE.md` §naming/ownership. Docs teach the per-platform meaning of `shared` and the full hybrid form first, not the one-liner.

No changes to: `loader.py`, `module.py`/`render_tofu_json`, `registry.py`, `cutover.py`, plan-digest code, cold-path CLI modules (`iac/` is never imported on `--help`; startup-budget test unaffected).

## v1 scope vs v2+

**v1 (realistically 3 comfortable PRs, not 2 — three asymmetric provider emitters + prefix-scoped grant rewrites + apply-engine guard):**

- *PR 1:* schema block, `resolve_packaging` with LEGACY sentinel, semantic validators, byte-parity pin (merged first so every later PR is gated by it).
- *PR 2:* aws/gcp/snowflake emit + `discover_imports` gating + Snowflake literal inlining + confluent validation + backend-key fix.
- *PR 3:* transition guard + adoption flag + audit events, native-planner packaging block, poolManifest snapshot, verify probe.

**v2+:** `fluid apply --migrate-packaging` (automated state-rm surgery — brownfield adopters are the motivated audience, and v1's manual tofu surgery is an honest pothole in a tool people chose so they wouldn't hand-drive tofu); pool manifests with quotas + admission control; tenancy registry to detect leaf collisions (overlapping prefixes, duplicate table names in a pool — isolated mode's implicit namespacing is removed and v1 replaces it with nothing but review); dedicated Confluent clusters; isolated-warehouse sizing knobs; global backend-key default flip; Azure plugin picks up `resolve_packaging` for free.

## Migration & compatibility

- **Non-adopters:** zero risk, verifiable — no block ⇒ LEGACY ⇒ byte-identical emit, pinned by the golden-file test; preview-gating means untagged contracts can never silently opt in. Graduating 0.7.6 out of `PREVIEW_VERSIONS` needs no packaging-specific work.
- **isolated → shared:** naive re-apply would plan a container destroy; the transition guard intercepts *before* plan and prescribes `tofu state rm` — data survives, and the run record carries the structured event.
- **shared → isolated:** the dangerous direction. Requires `--adopt-shared-container` + audit event + (when a manifest exists) an empty-tenant check. Never docs-only.
- **Explicit `mode: isolated`** = today's resources + pool-absent cost tags; opt-in churn only for contracts that opt in.

## Security & governance

Shared mode *narrows* the IAM blast radius declaratively: LF registers the `path` prefix, not the bucket; GCP grants move to table level; Snowflake grants stop at schema. Pool-container ACLs are **out of contract scope** — Unity-Catalog-style, the pool owner binds tenants; a tenant contract cannot widen the pool. The honest consequence: a shared-mode contract is **not self-sufficient** — first-apply success depends on a platform-side USAGE/read grant the contract cannot express. The verify probe (file 9) converts that from a runtime surprise into a named pre-flight failure. **Admission control does not exist in v1**: any contract can declare any container — including another product's isolated container — as its pool, bounded only by applier credentials and the optional manifest check. This is stated, not hidden; the manifest snapshot gives platform teams a cryptographically-bound hook today and quotas later. `accessPolicy`/`agentPolicy` are orthogonal; `consumes[]` stays lineage-only.

## Open questions (max 3)

1. **Should v1 require `pool` (and eventually `poolManifest`) whenever any container is `shared`?** Requiring `pool` costs one line and makes cost attribution + the future tenancy registry universal; requiring the manifest gates adoption on platform-team readiness. **Recommendation:** require `pool`, keep `poolManifest` optional in v1, revisit at 0.7.6 graduation.
2. **Should the native planners drop REFERENCED container-creation actions or keep-and-annotate them?** Dropping makes plan.json truthful but changes action counts CI parsers may key on; annotating preserves counts but shows "create" for things not created. **Recommendation:** drop, with the `packaging` summary block as the reviewer's source of truth — a plan that lists creations that won't happen is worse than a changed count.
3. **Ship the backend-key default flip for *all* contracts now, or packaging-gated as proposed?** A global flip fixes the collision universally but churns every existing backend key (state relocation for all users). **Recommendation:** packaging-gated in v1 (preserves the parity pin), global flip as a standalone migration-noted PR immediately after, since the bug exists independent of this feature.

## Risks (honest)

(1) Digest churn — mitigated by the parity pin as release gate. (2) GCP prefix IAM needs uniform bucket-level access — validator warns. (3) Snowflake missing-pool failure is a raw provider error at plan time; verify probe softens but doesn't eliminate. (4) Pre-existing dual-owned containers aren't retroactively healed — the guard only helps adopters. (5) Overlay key-wise merge on an ownership knob is a real sharp edge — validator warning + restate-wholesale idiom, but a determined footgun remains. (6) v1 shared = "whatever name you type" absent a manifest — admission control is deferred and that is a genuine governance gap, not a rounding error. (7) The teaching surface is a matrix, not an enum; docs budget must reflect that.
