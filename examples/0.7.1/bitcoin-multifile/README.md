# Bitcoin Price Tracker — Multi-File Example

This example demonstrates **$ref composition** in FLUID contracts. The same
Bitcoin price tracker from the single-file example is split across multiple
files, each owned by a different team.

## File Structure

```
bitcoin-multifile/
├── contract.fluid.yaml                  # Root contract with $ref pointers
├── fragments/
│   ├── sovereignty.yaml                 # Compliance team owns this
│   ├── access-policy.yaml               # Security / IAM team owns this
│   ├── builds/
│   │   └── ingestion.yaml               # Data engineering owns this
│   └── exposes/
│       └── bigquery-table.yaml          # Platform team owns this
├── overlays/
│   └── prod.yaml                        # Production overrides
└── README.md
```

## Usage

```bash
# Compile to a single document (inspect what the engine will see)
fluid compile contract.fluid.yaml

# Compile with production overlay
fluid compile contract.fluid.yaml --env prod --out contract.bundled.yaml

# All existing commands work transparently (refs resolve automatically)
fluid validate contract.fluid.yaml
fluid plan contract.fluid.yaml --out plan.json
fluid apply contract.fluid.yaml --yes
```

## Why Split?

| Benefit | How |
|---------|-----|
| **Team ownership** | Security team owns `access-policy.yaml`, compliance owns `sovereignty.yaml` |
| **Independent versioning** | Add a new expose without touching governance configs |
| **Reusable fragments** | `sovereignty.yaml` can be `$ref`'d by every EU data product |
| **Smaller diffs** | PRs touch only the fragment that changed |
| **The engine stays simple** | validate/plan/apply always receive one resolved document |
