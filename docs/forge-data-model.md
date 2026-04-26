# Forge Data Model — User Guide

`fluid forge data-model` forges a reviewable data-model contract from
either raw DDL files or a business intent file. The forged artefact is a
Fluid 0.7.2 contract with an embedded OSI v0.1.1 semantic block, plus
a `<name>.fluid.yaml.model.json` sidecar that downstream commands
(`fluid generate transformation`, `fluid forge data-model diff`)
consume.

## Two entry points

### From a business intent

```bash
fluid forge data-model from-intent \
  intent.yaml \
  --technique dimensional \
  --output customer_orders.fluid.yaml
```

`intent.yaml` is a YAML or JSON description of the data product you
want. The modeler reads `data_product`, `grain`, `dimensions`,
`metrics`, `data_sources`, and `business_rules` to forge a complete
logical model.

Discover the format from the CLI:

```bash
fluid forge data-model from-intent --example
fluid forge data-model from-intent --example retail
fluid forge data-model from-intent --example telco
fluid forge data-model from-intent --schema
fluid forge data-model from-intent --validate intent.yaml
```

Minimal intent:

```yaml
data_product:
  name: customer_orders
  domain: retail
  description: Order facts and customer dimensions for analytics
grain:
  entity: order_line
  time_dimension: order_date
dimensions:
  entities: [customer, product, store]
metrics:
  - name: total_revenue
    description: Sum of order line amounts
```

### Business Intent File Format

Required minimum:

- `data_product.name`: contract/model name.
- `data_product.domain`: business domain such as retail, finance, healthcare, or telco.
- At least one of `grain`, `dimensions.entities`, `metrics`, or `data_sources`.

Useful optional fields:

- `business_context`: problem, decision, and consumer context for model assumptions.
- `grain`: fact grain or central DV2 entity.
- `dimensions.entities`: dimensions in a star model or hubs in DV2.
- `metrics`: semantic measures and metrics.
- `data_sources`: source-system hints for reviewers and transformation generation.
- `business_rules`: assumptions and logic notes to preserve in the model.
- `modeling.technique`: default modeling technique unless the CLI overrides it.

The full machine-readable contract for editors and automation is available as JSON Schema:

```bash
fluid forge data-model from-intent --schema
```

Advanced YAML examples are bundled in `examples/intents/`:

- `customer_orders.intent.yaml`: dimensional retail order-line analytics.
- `telco_service.intent.yaml`: Data Vault 2.0 service usage analytics.
- `finance_risk.intent.yaml`: dimensional finance risk analytics.

Retail dimensional example:

```yaml
data_product:
  name: customer_orders
  domain: retail
  description: Order-line revenue analytics for merchandising and operations
grain:
  entity: order_line
  time_dimension: order_date
dimensions:
  entities: [customer, product, store, promotion]
metrics:
  - name: gross_revenue
    description: Sum of order line gross amount
data_sources:
  - name: pos_orders
    system: point_of_sale
modeling:
  technique: dimensional
```

Telco Data Vault 2.0 example:

```yaml
data_product:
  name: telco_service_usage
  domain: telecommunications
grain:
  entity: usage_event
  time_dimension: event_timestamp
dimensions:
  entities: [party, account, subscription, service, resource, usage_event]
metrics:
  - name: billable_usage_quantity
    description: Sum of rated usage units
business_rules:
  - Usage events are associated to the active subscription at event time.
modeling:
  technique: data_vault_2
```

### From DDL files

```bash
fluid forge data-model from-ddl \
  --ddl legacy/orders.sql legacy/customers.sql \
  --source-type snowflake \
  --technique data-vault-2 \
  --output customer_orders.fluid.yaml
```

The DDL parser uses `sqlglot` for major dialects (Snowflake,
BigQuery, Postgres, Oracle, MySQL) and falls back to a native
parser when sqlglot fails. For richer profiling (column null rates,
distinct counts, sample values), point `--profile` at a directory
of Parquet/Avro files — the optional `pyarrow`/`fastavro` extras
enable that path.

## What gets emitted

```
./
├── customer_orders.fluid.yaml          # Fluid 0.7.2 contract (OSI semantics embedded)
├── customer_orders.fluid.yaml.model.json   # Logical IR sidecar (DV2 / Dimensional)
└── customer_orders.semantics.osi.yaml  # Standalone OSI for direct BI consumption
```

The `.model.json` sidecar is what downstream commands consume — it's
the canonical Logical IR.

## Companion subcommands

```bash
fluid forge data-model validate customer_orders.fluid.yaml
fluid forge data-model diff old.model.json new.model.json
fluid forge data-model dump-ddl --database <DATABASE> --schema <SCHEMA> -o /tmp/snapshot.sql
```

`validate` runs the full Fluid 0.7.2 schema check + OSI conformance
+ industry-pack lint (when a pack is available). `diff` produces a
structural diff (added hubs/links/sats, renamed columns, changed
hash-key strategy) — much cleaner than `git diff` against the JSON.
`dump-ddl` exists so the `from-ddl` round-trip works on Snowflake
without the user wrangling `snowsql` invocations by hand.

## Determinism + reproducibility

Default behaviour uses `temperature=0` and the OpenAI seed `42` —
same intent + same model + same workspace produces byte-identical
output. The `--deterministic` flag tightens this further:

```bash
fluid forge data-model from-intent intent.yaml -o out.yaml --deterministic
```

`--deterministic` forces `temperature=0`, disables tiered model
selection (everything runs on one provider tier), and disables the
LLM cache so audit replays go straight to the live provider. The
audit metadata records the determinism flag so downstream consumers
can verify the run shape after the fact.

See `tests/test_provider_determinism_payloads.py` for the per-provider
contract: every provider's `build_request` pins `temperature=0`,
OpenAI/Ollama/Azure-OpenAI also pin `seed=42`, and Anthropic/Gemini
pin temperature only (their APIs lack public seed parameters).

## Caching

The staged pipeline caches every LLM call by `sha256(model || prompt
|| params)`. A repeat invocation against the same intent returns
near-instantly — the plan's "warm-cache ≥70% latency reduction"
target is enforced as a regression test in
`tests/perf/test_warm_cache_regression.py`. To bypass:

```bash
fluid forge data-model from-intent intent.yaml -o out.yaml --no-cache
```

`--no-cache` skips both reads and writes; `--deterministic` implies
`--no-cache` for audit reproducibility.

## Banner suppression

Every banner-emitting CLI surface honours both an env-var path
(`FLUID_QUIET=1` or `FLUID_NONINTERACTIVE=1`) and a `--quiet` / `-q`
CLI flag:

```bash
fluid forge data-model from-intent intent.yaml -o out.yaml --quiet
```

The banner auto-expires on **2026-05-07** regardless — one week
after the v1.1 target date.

## Memory namespaces

Forge runs that opt into semantic memory (`FLUID_COPILOT_SEMANTIC_MEMORY=1`)
write a slim payload of every successful forge to
`~/.fluid/store/memory/semantic/<slug>.<hash>` so subsequent runs in
the same workspace can retrieve similar prior models. The opt-in is
deliberate — privacy-sensitive multi-tenant workstations should not
accumulate cross-tenant signal silently.

`fluid memory show project|team|episodic|semantic` lists what's
stored. `fluid memory clear --ns memory/semantic` clears it. See
`docs/MEMORY.md` for the full namespace model.

## Industry packs

Four packs ship by default — `telco`, `retail`, `healthcare`,
`finance` — each with a default modelling technique (DV2 for telco /
healthcare, dimensional for retail / finance). When `--industry`
matches a pack, the validator lints the forged model against the
pack's canonical skeleton (TMF SID for telco, NRF ARTS for retail,
HL7 FHIR for healthcare, ISO 20022 for finance) and the modeler
seeds its first draft from that skeleton. Cross-technique skeletons
(retail-DV2, telco-dimensional, etc.) are also available so the
`--technique` override still gets a starting shape.

Custom packs in `~/.fluid/agents/<industry>.yaml` are picked up
automatically. See `docs/INDUSTRY-PACKS.md` for the YAML format.
