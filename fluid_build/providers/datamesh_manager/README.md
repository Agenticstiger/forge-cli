# Data Mesh Manager / Entropy Data Provider

Publish FLUID contracts as **data products** and **data contracts** to [Entropy Data](https://www.entropy-data.com/) (formerly Data Mesh Manager).

## Quick Start

```bash
# 1. Set your API key
export DMM_API_KEY="your-secret-api-key"

# 2. Publish a data product
fluid datamesh-manager publish contract.fluid.yaml

# 3. Preview without publishing
fluid dmm publish contract.fluid.yaml --dry-run

# 4. Publish with companion data contract
fluid dmm publish contract.fluid.yaml --with-contract
```

## Features

- **Publish data products** via `PUT /api/dataproducts/{id}`
- **Publish data contracts** via `PUT /api/datacontracts/{id}`, one ODCS contract per expose at the id its output port names, carrying the expose `version`. A contract in the product's `{productId}.` namespace that its ports no longer name (an expose was removed) is retired (`status: retired`), never deleted. An output port names only a contract the publish sends or Entropy already has
- **Publish product-to-product lineage** via ODPS input ports and `Access` resources derived from `consumes[]`
- **Auto-create teams** when they don't exist
- **Input/output port mapping** from FLUID expects/exposes plus explicit source-system consumes
- **PII detection** from schema field classification
- **Multi-provider location** mapping (BigQuery, Snowflake, S3, Kafka, Redshift, etc.)
- **Dry-run mode** for previewing API payloads
- **Retry with backoff** for transient failures (429, 5xx)
- **Safe defaults** — plain HTTP is limited to local endpoints, API error bodies are redacted, and Access auto-approval is opt-in
- **Catalog adapter** — also works via `fluid publish --catalog datamesh-manager`

## CLI Commands

```bash
# Publish
fluid dmm publish contract.fluid.yaml
fluid dmm publish contract.fluid.yaml --dry-run
fluid dmm publish contract.fluid.yaml --with-contract
fluid dmm publish contract.fluid.yaml --team-id my-team

# List data products
fluid dmm list
fluid dmm list --format json

# Get a specific product
fluid dmm get search-queries-all

# Delete a product
fluid dmm delete search-queries-all

# List teams
fluid dmm teams
```

## Authentication

Generate an API key at: **Profile → Organization → Settings → API Keys**

```bash
export DMM_API_KEY="your-secret-api-key"

# Optional: custom API endpoint
export DMM_API_URL="https://api.entropy-data.com"
```

Or pass inline:
```bash
fluid dmm publish contract.yaml --api-key "your-key"
```

## FLUID → Entropy Data Mapping

| FLUID Field | Entropy Data Field |
|---|---|
| `id` / `metadata.id` | `info.id` |
| `metadata.name` | `info.name` |
| `metadata.description` | `info.description` |
| `metadata.status` (production→active) | `info.status` |
| `owner.team` | `teamId` + `info.owner` |
| `metadata.archetype` | `info.archetype` |
| `metadata.maturity` | `info.maturity` |
| `expects[]` | `inputPorts[]` |
| `exposes[]` | `outputPorts[]` |
| `metadata.tags` | `tags[]` |
| `metadata.domain`, `.version`, etc. | `custom{}` |

### Port Mapping

Input/output ports are mapped with:
- **type** — provider name → platform type (gcp→BigQuery, snowflake→Snowflake, etc.)
- **location** — assembled from provider-specific config (project.dataset.table, s3://bucket/key, etc.)
- **containsPii** — detected from schema field `classification: pii` or `pii: true`
- **ODPS display names** — DMM ODPS output ports keep their technical identifier in `name`; the publisher also writes `customProperties[displayName]` from the FLUID expose title/name so Entropy renders named output ports in Access and lineage views.
- **Access lineage** — each product-to-product `consumes[]` entry also becomes an Entropy `Access` agreement from the provider data product/output port to the consumer data product. Once approved it takes over the edge the input port draws. On each publish, agreements forge made earlier for consumes the contract no longer has are cancelled (`POST /api/access/{id}/cancel`, ending now); agreements a person made are never touched.
- **Access approval** — production-safe default is create-only. Set `DMM_AUTO_APPROVE_ACCESS=true`, pass `--auto-approve-access`, or configure `auto_approve_access: true` only for local sandboxes or policies that intentionally auto-approve.
- **ODPS input ports** — every `consumes[]` entry is an ODPS input port whose `contractId` names the upstream contract. Entropy draws the product-to-product edge from it (status `declared`), across teams too, where an Access agreement stays `requested`. A build source's input port carries `customProperties[sourceSystemId]` and `[type]`, which is how Entropy links it to its SourceSystem.
- **sourceSystem** — preserved when explicitly authored. Set `DMM_ODPS_LINEAGE_MODE=source-system` or catalog config `odps_lineage_mode: source-system` only for legacy DMM servers that require SourceSystem custom properties for retained ODPS input ports.

## Using via `fluid publish`

The provider also integrates with the generic `fluid publish` command via the catalogs framework:

```bash
# Configure in ~/.fluid/config.yaml
catalogs:
  datamesh-manager:
    endpoint: https://api.entropy-data.com
    auth:
      api_key: ${DMM_API_KEY}
    odps_lineage_mode: contract
    auto_approve_access: false
    enabled: true

# Then publish
fluid publish contract.fluid.yaml --catalog datamesh-manager
```

## API Reference

- **Swagger**: https://api.entropy-data.com/swagger/index.html
- **Docs**: https://docs.datamesh-manager.com/dataproducts
- **Auth**: https://docs.datamesh-manager.com/authentication
- **Data Contracts**: https://docs.datamesh-manager.com/datacontracts

## Requirements

- Python 3.10+
- `requests` library (`pip install requests`)
- Entropy Data API key (free tier available)
