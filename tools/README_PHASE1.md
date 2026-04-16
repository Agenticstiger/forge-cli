
# FLUID Build — Phase 1 Bootstrapper

This package adds a frictionless first‑run experience **alongside** your existing `fluid_build` project.

## Installation
```bash
unzip fluid-phase1-bootstrap.zip
cd fluid-phase1-bootstrap
pip install pyyaml  # required for YAML edits
```

## Commands
```bash
# Initialize a workspace at the current path (adds folders, providers.yaml, diagnose shim)
python -m fluid_bootstrap init --path .

# Copy curated examples (local CSV demo + Snowflake Customer360) into ./examples/
python -m fluid_bootstrap examples --path .

# Scaffold a new product
python -m fluid_bootstrap new-product silver.demo.my_product_v1       --name "My Product" --domain Demo --layer Silver --provider local

# Incrementally edit a contract
python -m fluid_bootstrap add-source products/silver/demo/my_product_v1/contract.fluid.yaml       --id hv_customers --ref file://examples/local_high_value_churn/data/high_value_customers.csv

python -m fluid_bootstrap add-exposure products/silver/demo/my_product_v1/contract.fluid.yaml       --id demo_tbl --type table --format csv       --properties '{"path":"runtime/out/demo.csv"}'       --schema '[{"name":"id","type":"STRING","nullable":false}]'

python -m fluid_bootstrap add-dq products/silver/demo/my_product_v1/contract.fluid.yaml       --exposure-id demo_tbl --rule not_null --columns id --on-failure alert

# Diagnostics wrapper (runs built-in doctor checks and optional extended diagnostics)
python -m fluid_bootstrap doctor --path . --provider local
```

## Next steps with your main CLI
```bash
# Validate a contract
python -m fluid_build.cli validate products/silver/demo/my_product_v1/contract.fluid.yaml

# Plan/apply depending on provider
python -m fluid_build.cli plan --provider local products/silver/demo/my_product_v1/contract.fluid.yaml --out runtime/plan.json
python -m fluid_build.cli apply --provider local products/silver/demo/my_product_v1/contract.fluid.yaml
```
