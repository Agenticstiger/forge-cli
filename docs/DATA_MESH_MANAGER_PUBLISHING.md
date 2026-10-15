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
pip install data-product-forge          # install the CLI

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

#### Where `fluid publish --target dmm` takes its endpoint from

`fluid publish` reads catalog settings from, in rising priority: `/etc/fluid/config.yaml`,
your user config (`~/.fluid/config.yaml`), a project config in the working directory
(`fluid.config.yaml`, `.fluidrc.yaml`, `.fluidrc` or `.fluid/config.yaml`), the environment,
and `--target dmm:<endpoint>`. A YAML anchor, alias or merge key in any of these files
shares a value, never a setting: writing under one name never changes another, so a project
config cannot reach one of your catalog blocks through an alias you use for it. A file whose
aliases or merge keys (`<<:`) nest so deep that they would expand far past what is written
in it (the limit is go-yaml's "excessive aliasing" rule, which also counts each key a merge
key copies) is refused with a parse error that names the file, rather than expanded;
`fluid market` skips such a `market.yaml`, in `~/.fluid` or the working directory, with a
warning that names it.

A project config is part of the checkout, so the CLI does not trust it like your own config:

- `${VAR}` placeholders in it are kept as written, not filled from the environment.
- An endpoint it sets is only sent a key that the same file carries. It is never sent
  `DMM_API_KEY` or a key from `~/.fluid/config.yaml`. When the file carries its own key
  where the catalog reads one (`api_token`, `token`, `auth.api_token`, `auth.token`,
  `auth.api_key` or `auth.bearer_token`), that key is sent and `DMM_API_KEY` is left out.
  When it carries none, `fluid publish --target dmm` stops and says so, and a contract that
  lists the catalog under `properties.catalog.register` reports that target as not
  registered. Set the endpoint with `DMM_API_URL`, in `~/.fluid/config.yaml`, or with
  `--target dmm:<endpoint>` instead.
- A key the file sets to anything but a non-empty string (`""`, `null`, spaces, `false`,
  `0`, `no`, `[]`, `{}`) is no key: such an endpoint is treated exactly as if the file left
  the key out. Quote a key that looks like a number or a date. (In your own config files an
  unquoted number or date is still sent as your key, as YAML reads it, so `1.50` goes out
  as `1.5`, with a warning that names the setting and says to quote it; an unquoted `true`,
  `yes` or `on` there is no key, and `DMM_API_KEY`, if set, is sent instead.) A Command Center
  `auth.api_key` or `auth.token` that the file sets to a number is no key even when the
  endpoint is yours (`FLUID_CC_ENDPOINT`): the log names the setting and says to quote it,
  and `FLUID_API_KEY` or `FLUID_BEARER_TOKEN`, if set, is sent in its place.
- Nothing else from your environment or your own config files reaches such an endpoint,
  whatever its name and however deep in a list or map it sits, except these settings:
  the organization (`organization_id`, `organization`, `FLUID_CC_ORG_ID`),
  `DMM_PROVIDER_HINT`, `DMM_DATA_PRODUCT_SPECIFICATION`, `odps_lineage_mode` (a string),
  `auto_approve_access` (`true` or `false`), `auth.type`, `enabled`,
  `force_contract_sync`, `timeout`, `max_retries`, `retry_delay` and the circuit-breaker
  settings. Any other value, an empty map or list included, is named in the refusal. So
  the provider README's block in `~/.fluid/config.yaml` does not stop a checkout whose
  own config carries its own endpoint and key, whether `DMM_API_KEY` is exported, empty
  or unset (its `${DMM_API_KEY}`, then empty or left as written, is no key): that
  endpoint gets the checkout's key, never `DMM_API_KEY`, and the README's settings. A
  key your config file sets there itself is still never sent, and still named.
- `{{ env.NAME }}` placeholders in the contract are sent as written to such an endpoint,
  not filled from the environment; `fluid publish --target dmm` logs a warning naming
  them. Registration under `properties.catalog.register` never fills them, whatever the
  endpoint.
- Such an endpoint must be `https://`, unless it is loopback (`localhost`, `127.0.0.1`,
  `::1`) or its host is on `FLUID_COMMAND_CENTER_HOST_ALLOWLIST` (comma-separated host
  suffixes). A host that resolves to a private or cloud-metadata address also needs that
  allow-list. This holds even when the project config carries its own key, for
  `--target dmm` and for `properties.catalog.register` alike; a refused target is
  reported as not published.

An endpoint from `DMM_API_URL`, `~/.fluid/config.yaml` or `--target dmm:<endpoint>` is
yours: it gets your key, and the host check above does not apply to it. Run from your home
directory, `~/.fluid/config.yaml` (or `~/.fluidrc.yaml`) is still your user config, not a
project config.

The same rule covers `fluid market`: with no `~/.fluid/market.yaml`, it reads `market.yaml`
from the working directory, and an `mcp` block from that file may not name `token_env`,
`env_from`, a `command` or the `stdio` transport, and its `url` must pass the host check
above. A `token` the file carries itself is sent. The Command Center that `fluid market`
and `fluid market --blueprints` use comes only from `FLUID_COMMAND_CENTER_URL` or
`~/.fluid/config.yaml`, never from a `fluid.yaml` in the working directory, and no request
to it (the blueprint search, `--blueprint-id`, `--instantiate`, the catalog connector)
follows a redirect to another origin. A blueprint registry you name yourself, with
`FLUID_API_URL` or with `FLUID_PUBLIC_REGISTRY` and `FLUID_MARKETPLACE_FALLBACK=public`, is
not the Command Center: a search or a `--blueprint-id` lookup there sends no credential and
no parameters, so it follows a registry that moved to another host. `--instantiate`, which
sends your parameters, still does not follow a redirect off the registry's origin.

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

This is the same mechanism used by `fluid bundle --format yaml --env staging`
(formerly `fluid compile --env staging`, renamed in 0.7.3 when the bundle
command absorbed the compile surface), so your CI validation step and your
publish step are always consistent.

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
