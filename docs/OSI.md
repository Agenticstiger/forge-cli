# OSI v0.1.1 Embedding in Fluid Contracts

forge-cli emits every Fluid 0.7.2 contract with an
[Open Semantic Interchange (OSI) v0.1.1](https://github.com/open-semantic-interchange/OSI)
semantic block at `exposes[*].semantics`. This makes the contract
directly consumable by dbt's semantic layer, Snowflake Cortex
catalog, Databricks Unity Catalog, Cube, and any other OSI-aware
BI tool — no translation required.

## Three-layer separation

```
┌──────────────────────────────────────────────────────────────┐
│ FLUID 0.7.2 contract  — infrastructure / build / execution   │  "HOW we run it"
│   └─ exposes[*].semantics                                    │
│        └─ OSI v0.1.1 Semantic Model — datasets, metrics, …   │  "WHAT it means"
│ Sidecar: <name>.fluid.yaml.model.json                        │
│   └─ DV2 / Dimensional IR — hubs / facts / dims / hash keys  │  "HOW it's stored"
└──────────────────────────────────────────────────────────────┘
```

The Fluid contract handles infrastructure (build engines, scheduling,
agentPolicy, sovereignty). OSI handles semantics (what the data
means, in a tool-neutral shape). The Logical IR (DV2 or Dimensional)
handles physical structure. Each layer is independent and
independently consumable.

## Where OSI lives in the contract

```yaml
fluidVersion: 0.7.2
kind: DataProduct
id: generated.customer_orders
name: Customer Orders
exposes:
  - exposeId: customer_orders
    kind: table
    binding: { platform: local, format: parquet, location: { path: runtime/customer_orders.parquet } }
    contract:
      schema:
        - { name: order_date, type: STRING }
        - { name: customer_id, type: STRING }
        - { name: amount, type: STRING }
    semantics:                              # ← OSI v0.1.1 block
      name: customer_orders
      description: Customer order facts and dimensions
      entities:
        - { name: order, type: primary, expr: order_id }
        - { name: customers, type: foreign, expr: customer_id }
      dimensions:
        - { name: order_date, type: time, typeParams: { timeGranularity: day } }
        - { name: customer_id, type: categorical }
      measures:
        - { name: total_revenue, agg: sum, expr: SUM(orders.amount), description: Total revenue from all orders }
      metrics:
        - { name: total_revenue, type: simple, measure: total_revenue, description: Total revenue from all orders }
```

The placement at `expose.semantics` (sibling of `contract`, NOT
nested inside it) follows the FLUID 0.7.2 schema's
`$defs.expose.properties.semantics` definition. A standalone
`<name>.semantics.osi.yaml` file with the same content is also
emitted alongside the contract for tools that prefer to consume
OSI directly.

## What gets propagated from the Logical model

| Logical IR field | OSI shape | Path in emitted semantics |
|---|---|---|
| `LogicalDraft.description` | `description` | `semantics.description` |
| `OSISemanticModel.name` | `name` | `semantics.name` |
| `OSIDataset.primary_key[*]` | primary entity | `semantics.entities[].type=primary` |
| `OSIRelationship[*]` | foreign entity | `semantics.entities[].type=foreign` |
| `OSIField.dimension.is_time` + `grain` | time dimension | `semantics.dimensions[].type=time, typeParams.timeGranularity` |
| `OSIField` (non-PK, non-relationship) | categorical dimension | `semantics.dimensions[].type=categorical` |
| `OSIMetric[*].expression.dialects[]` | measure SQL | `semantics.measures[].expr` (Cube-shaped) |
| `OSIMetric[*]` | metric definition | `semantics.metrics[].type=simple` (dbt-shaped) |

Every emit-shape pin lives in
`tests/test_osi_v011_conformance.py` so a refactor that silently
drops one of them surfaces in CI before it ships.

## Multi-dialect expression strategy

OSI's `expression.dialects[]` is the multi-dialect SQL
representation. forge-cli emits ANSI SQL alongside the engine-
specific dialect (Snowflake, BigQuery, Databricks, etc.) on every
metric and field expression — the modeler agent picks the right
dialect set based on the active provider's capability matrix.
Ollama collapses to ANSI-only because its single-model constraint
makes per-dialect generation expensive.

## Why we ship the standalone sidecar too

Some BI tools (Cube, raw OSI consumers, custom catalog scrapers)
prefer to read a dedicated `*.semantics.osi.yaml` file rather than
parse the Fluid contract. The sidecar carries the same OSI block
verbatim — emit it with `--emit-osi-sidecar` (default on). Disable
with `--no-emit-osi-sidecar` if you want to keep the workspace
clean.

## Conformance + verification

The plan's V1.3.7 conformance smoke is at
`tests/test_osi_v011_conformance.py` (10 tests):

* Every emitted contract has a `semantics` block on every expose.
* OSI v0.1.1 required keys (`name`) and recommended keys
  (`description`) are present.
* Logical metrics propagate to BOTH `measures[]` (Cube-shaped) AND
  `metrics[]` (dbt-shaped) — neither downstream flavour is dropped.
* Time-grain dimensions carry `typeParams.timeGranularity` so dbt's
  semantic-layer parser picks them up.
* Relationships surface as `foreign` entities for Cube/dbt joins.
* The Fluid contract round-trips through the schema validator
  cleanly with the OSI block embedded.

For the deeper field-level conformance (per-field
`expression.dialects[]` shape, `ai_context.synonyms` propagation,
`custom_extensions[]` vendor pass-through) see
`tests/copilot/test_osi_child_level_fields.py` and
`tests/copilot/test_osi_spec_enums.py`.

## Future: OSI v0.2.x

OSI v0.1.1 is the current spec. forge-cli pins to v0.1.1 in the
emit code; a future v0.2.x bump would add a `--osi-version` flag
and a per-version emit path. For now, every contract carries
v0.1.1 unconditionally.
