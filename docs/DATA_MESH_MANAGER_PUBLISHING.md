# Publishing to Data Mesh Manager

> **TL;DR** — one FLUID contract file → one CLI command → your data product is
> registered in Entropy Data with a machine-readable ODCS data contract
> auto-generated for every expose port. No manual JSON editing required.

---

## Why This Pattern Matters

Most teams end up with **three separate places** to maintain the same information:
a data pipeline definition, a data catalog entry, and a data contract document.
They drift apart within weeks.

FLUID solves this with a single source of truth — `contract.fluid.yaml` — and
the forge CLI derives everything else from it automatically:

```
contract.fluid.yaml
     │
     ├─► ODPS data product record  → registered in Entropy Data
     └─► ODCS contract (per port)  → linked to that product, consumable by any tool
```

Changes to your contract (new field, updated SLA, deprecated port) propagate to
the mesh manager in a single command. Governance stays in sync with the pipeline.

---

## Prerequisites

```bash
pip install fluid-forge          # install the CLI

export DMM_API_KEY=your-api-key  # Entropy Data API key
                                 # (or pass --api-key on every command)
```

---

## Commands, One by One

### 1. Preview everything before touching the API

```bash
fluid datamesh-manager publish contract.fluid.yaml --dry-run --with-contract
```

Prints the exact JSON that *would* be sent for:
- the ODPS data product record
- each ODCS contract (one per expose port)

Nothing is written to the API. Use this to review in a PR or a pre-deploy check.

---

### 2. Publish the data product record only

```bash
fluid datamesh-manager publish contract.fluid.yaml
```

Registers (or updates) the data product in Entropy Data.  Output ports are
included in the registration but **no data contracts are created yet** — the
ports show a `dataContractId` field so they're ready to be linked once contracts
are published.

---

### 3. Publish the data product AND all ODCS contracts

```bash
fluid datamesh-manager publish contract.fluid.yaml --with-contract
```

This is the full publish. It:

1. Registers the data product via `PUT /api/dataproducts/{id}`
2. For every expose port, generates an ODCS v3.1.0 contract and sends it via
   `PUT /api/datacontracts/{productId}.{exposeId}`
3. Each output port's `dataContractId` field in the product record already
   points to its contract — Entropy Data automatically links them.

---

### 4. Publish for a specific environment (staging / prod)

```bash
fluid datamesh-manager publish contract.fluid.yaml \
  --overlay overlays/staging.yaml \
  --with-contract
```

The overlay patches `binding.location` for each expose (project, dataset,
region) before anything is generated. The same contract.fluid.yaml works across
all environments — only the locations change.

A staging overlay looks like this:

```yaml
# overlays/staging.yaml
exposes:
  - binding:
      location:
        project: staging-gcp-project
        dataset: crypto_data_staging
        region: EU
  - binding:
      location:
        project: staging-gcp-project
        dataset: crypto_data_staging
        region: EU
```

One entry per expose (by position), patching only the fields you want to
override. Everything else — `exposeId`, schema, QoS, lifecycle state — is
preserved from the base contract.

---

### 5. In a CI/CD pipeline

```bash
# Typical Jenkinsfile / GitHub Actions step
DMM_API_KEY=${{ secrets.DMM_API_KEY }} \
  fluid datamesh-manager publish contract.fluid.yaml \
    --overlay overlays/$ENV.yaml \
    --with-contract
```

No interactive prompts. The API key is read from the environment variable
`DMM_API_KEY`. The `--overlay` path is parameterised by the deploy stage.

---

### 6. Other useful commands

```bash
# List all data products visible to your API key
fluid datamesh-manager list

# Inspect one product (returns raw JSON)
fluid datamesh-manager get crypto.bitcoin_prices_gcp_governed

# Delete a product (prompts for confirmation unless --yes is passed)
fluid datamesh-manager delete crypto.bitcoin_prices_gcp_governed

# List teams
fluid datamesh-manager teams

# Short alias — 'dmm' works everywhere instead of 'datamesh-manager'
fluid dmm publish contract.fluid.yaml --dry-run
```

---

## What Gets Generated and How

### ODCS contract (per expose port)

Each expose in your FLUID contract becomes one ODCS v3.1.0 document. The
mapping is straightforward:

| FLUID field | ODCS field | Notes |
|---|---|---|
| `exposeId` | `id`, `name` | |
| `lifecycle.state` | `status` | `active` or `deprecated` |
| `binding.platform` + `binding.location` | `servers[]` | project, dataset, table, region |
| `schema[]` | `schema[]` | field names and types |
| `qos.availability` | `slaProperties.availability` | `"99.5%"` → `0.995` |
| `qos.freshnessSLO` | `slaProperties.interval` | ISO 8601 duration, e.g. `PT5M` |
| `qos.labels` | `slaProperties.customProperties` | arbitrary key/value pairs |
| `metadata.owner` | `team` | name + email |

A contract with two expose ports produces two ODCS documents, each published
independently at its own URL:

```
https://api.entropy-data.com/api/datacontracts/{productId}.{exposeId}
```

### ODPS data product record

The top-level FLUID contract maps to Entropy Data's ODPS-aligned data product
shape:

| FLUID field | ODPS / Entropy Data field |
|---|---|
| `id` | `info.id` |
| `name` | `info.name` |
| `description` | `info.description` |
| `domain` | `info.domain` |
| `metadata.owner` | `teamId` |
| `exposes[]` | `outputPorts[]` (each with `dataContractId`) |
| `expects[]` | `inputPorts[]` |

The `dataContractId` on every output port follows the pattern
`{productId}.{exposeId}`, which is exactly the URL path used when the ODCS
contracts are PUT — so the link is automatic.

---

## Why One Contract Per Expose Port?

A data product often exposes multiple tables or views at different maturity
levels — for example a `v1` (deprecated) and a `v2` (active). These have
different schemas, different SLAs, and potentially different lifecycle states.
Bundling them all into one contract would mean consumers can't selectively
subscribe to or enforce contracts on individual ports.

ODCS v3.1.0 is designed as a per-dataset contract. Generating one document per
expose port means:

- Consumers can reference exactly the port they depend on
- Deprecating `v1` doesn't invalidate `v2`'s contract
- Each contract has its own `slaProperties`, schema, and server binding
- Entropy Data can track lineage and drift per port independently

---

## Environment Overlay Deep Merge

When `--overlay` is supplied, the CLI calls `load_with_overlay()` which
performs a **positional deep merge** of the overlay's `exposes` list into the
base contract's `exposes` list before any generation happens. This means:

- Overlay entry at index `0` patches base entry at index `0`, and so on
- Only the fields present in the overlay entry are changed
- All other fields (`exposeId`, `schema`, `qos`, `lifecycle`) are untouched
- Both the ODPS product record and the ODCS contracts see the merged result

This is the same mechanism used by `fluid compile --env staging`, so your
CI validation step and your publish step are always consistent.

---

## Full Example

Given this layout:

```
bitcoin-price-tracker/
  contract.fluid.yaml        ← two expose ports (v1 deprecated, v2 active)
  overlays/
    staging.yaml             ← patches binding.location for staging GCP
    prod.yaml                ← patches binding.location for production GCP
```

A full deploy sequence looks like:

```bash
# 1. Validate the contract
fluid validate contract.fluid.yaml

# 2. Dry-run the mesh manager publish (CI gate)
fluid dmm publish contract.fluid.yaml --overlay overlays/$ENV.yaml \
  --dry-run --with-contract

# 3. Publish on merge to main
fluid dmm publish contract.fluid.yaml --overlay overlays/$ENV.yaml \
  --with-contract
```

After step 3, Entropy Data shows:
- One data product entry with two output ports
- Two ODCS contracts, each linked to its port via `dataContractId`
- The correct staging or production BigQuery locations in each `servers[]` block
