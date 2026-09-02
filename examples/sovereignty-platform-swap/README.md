# Demo — Sovereignty & Platform Swap

**The claim:** the contract owns the *what* (schema, quality, SLO, ownership).
The `binding` block owns the *where*. Change five lines of `binding` and the same
data product compiles to AWS, GCP or Snowflake — no logic, schema or governance
rewritten, no vendor SDK in the contract.

One product: `analytics.eu.customer_events_v1` — pseudonymised customer events, EU-resident.

Everything below was run against this checkout; the resource counts are real output.

---

## Setup (once, before the audience is watching)

```bash
cd examples/sovereignty-platform-swap
```

Use the project venv so the demo is deterministic:

```bash
alias fluid=../../.venv/bin/fluid
```

> Optional: `unset AWS_PROFILE AWS_ACCESS_KEY_ID` first. `generate iac` will pick
> up real AWS credentials if present and print your account id — harmless, but it
> puts your account number on screen.

---

## Step 1 — Show the contract (30s)

Open [`contract.fluid.yaml`](contract.fluid.yaml) in the IDE. Talk through the two halves:

- **Invariant half** — `id`, `metadata.owner`, `contract.schema`, `contract.dq.rules`, `qos`.
  This is the business agreement. It never changes across clouds.
- **Swappable half** — `exposes[].binding`. Platform, format, location. That's it.

```yaml
binding:
  platform: aws
  format: parquet
  location:
    database: customer_analytics
    table: customer_events
    bucket: acme-eu-lake
    path: curated/customer_analytics/customer_events/
    region: eu-central-1
```

## Step 2 — Validate (10s)

```bash
fluid validate contract.fluid.yaml
```

→ `✅ Valid FLUID contract (schema v0.7.6)` in ~1ms. Point out this is schema +
provider-rule validation, offline, no cloud call.

## Step 3 — Governance gate (30s)

```bash
fluid policy-check contract.fluid.yaml
```

→ compliance score panel, 5 categories, `✅ PASSED`. This is the gate you put in
CI *before* anything reaches a cloud account.

## Step 4 — Compile to AWS (30s)

```bash
fluid generate iac contract.fluid.yaml --out infra-aws
```

→ `Wrote OpenTofu module: infra-aws/main.tf.json (provider: aws, 3 resources)`

Open `infra-aws/main.tf.json` in the IDE:

| resource | |
|---|---|
| `aws_s3_bucket` | `acme-eu-lake` |
| `aws_glue_catalog_database` | `customer_analytics` |
| `aws_glue_catalog_table` | columns `string / timestamp / double`, location `s3://acme-eu-lake/curated/...` |

**Nothing was applied.** This is reviewable OpenTofu you hand to your platform team.

## Step 5 — The swap (the money shot, 45s)

Diff the binding, then swap it in:

```bash
diff contract.fluid.yaml contract.gcp.yaml
```

Only the `binding` block differs. Now:

```bash
fluid generate iac contract.gcp.yaml --out infra-gcp
```

→ `Wrote OpenTofu module: infra-gcp/main.tf.json (provider: gcp, 2 resources)`

| resource | |
|---|---|
| `google_bigquery_dataset` | `customer_analytics` |
| `google_bigquery_table` | schema `STRING / TIMESTAMP / FLOAT64` |

Same five columns. Same nullability. **Native BigQuery types**, derived — not hand-mapped.

And once more:

```bash
fluid generate iac contract.snowflake.yaml --out infra-snowflake
```

→ `(provider: snowflake, 3 resources)` — `snowflake_database`, `snowflake_schema`,
`snowflake_table` with `VARCHAR / TIMESTAMP_NTZ / FLOAT`.

## Step 6 — Show the three side by side (30s)

```bash
for p in aws gcp snowflake; do echo "── $p"; python3 -c "
import json,sys
d=json.load(open('infra-$p/main.tf.json'))
[print('  ',t,n) for t,rs in d['resource'].items() for n in rs]"; done
```

Three clouds, three resource graphs, **one contract**. The line that changed:
`platform: aws` → `platform: gcp` → `platform: snowflake`.

## Step 7 — The exit door (30s)

Sovereignty is not only *where it runs* — it's *can you leave*.

```bash
fluid generate standard contract.fluid.yaml --format odps
cat runtime/exports/product.odps.yaml
```

→ Bitol **Open Data Product Standard v1.0.0**. Open spec, no FLUID types in it.
`--format odcs` gives you the Open Data Contract Standard per output port.
Your metadata outlives this tool.

---

## The one-line version, if you only get 60 seconds

```bash
fluid validate contract.fluid.yaml \
  && fluid generate iac contract.fluid.yaml       --out infra-aws \
  && fluid generate iac contract.gcp.yaml         --out infra-gcp \
  && fluid generate iac contract.snowflake.yaml   --out infra-snowflake
```

---

## Talk track

> "Most 'multi-cloud' means an abstraction layer that gives you the lowest common
> denominator on every cloud. This is the opposite. The contract stays declarative
> and portable; the compiler emits **native** resources per platform — real Glue
> tables, real BigQuery schemas, real Snowflake DDL — so you lose nothing by being
> portable. The lock-in you're avoiding isn't the cloud. It's the *rewrite*."

## Cleanup

```bash
rm -rf infra-aws infra-gcp infra-snowflake runtime
```
