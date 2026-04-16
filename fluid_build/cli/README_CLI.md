# FLUID Forge — Modular CLI (bundle 2025-10-11T12:26:03Z)

This folder contains a production-grade, modular CLI for FLUID Forge / FLUID Build.

## Quickstart

```bash
# From repo root
python -m fluid_build.cli --provider local validate examples/customer360/contract.fluid.yaml
python -m fluid_build.cli --provider local plan examples/customer360/contract.fluid.yaml --out runtime/plan.json
python -m fluid_build.cli --provider local apply runtime/plan.json --yes --report runtime/apply_report.json
python -m fluid_build.cli providers
python -m fluid_build.cli version
```

## Commands

- validate — validate a FLUID contract.
- plan — compile provider actions.
- apply — execute a plan/contract.
- preview — validate → plan → visualize.
- viz-graph — Graphviz DOT for lineage.
- viz-plan — static HTML for plan.json.
- contract-tests — schema compatibility.
- scaffold-ci — emit GitLab/GitHub pipeline.
- scaffold-composer — emit Airflow DAG.
- product-new — scaffold a new product.
- product-add — add source/exposure/dq (prompt embedded).
- policy-compile / policy-apply — IAM flows.
- export-opds — FLUID → OPDS export.
- providers — list registered providers.
- auth — provider auth flows.
- context — default provider/project/region.
- docs — static docs index.
- doctor — run diagnose script.
- diff — drift detection (prompt embedded).
- market — discover data products from enterprise catalogs and marketplaces.
