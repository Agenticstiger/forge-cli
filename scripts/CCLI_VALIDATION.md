# FLUID Build — Comprehensive CLI Validation

This utility exercises the **entire FLUID CLI surface** in one run:
- Verifies environment & tools (Python, `jq`, `yq`, `dot`, `gcloud`)
- Confirms **provider discovery** (`local`, `gcp`, `snowflake`, `odps`)
- Validates one or more **FLUID contracts**
- Generates a **plan**, applies it, emits **Graphviz DOT** (and PNG if Graphviz is present)
- Produces **HTML plan** previews, **Markdown docs**, **contract tests**, and scaffolds **CI / Composer**
- Writes a clean **HTML report**, a human `SUMMARY.txt`, and a machine-readable `summary.json`

> The script **does not crash** on individual failures — it records them, continues, and returns a non-zero exit code only if any step fails.

---

## Quick Start

```bash
# From repo root:
chmod +x scripts/ccli_validate.sh

# Local provider (no cloud auth needed)
PROVIDER=local ./scripts/ccli_validate.sh \
  --contracts examples/customer360/contract.fluid.yaml,examples/local/high_value_churn/contract.fluid.yaml

# GCP (requires gcloud auth)
PROVIDER=gcp PROJECT=your-proj REGION=europe-west3 ./scripts/ccli_validate.sh \
  --contracts examples/customer360/contract.fluid.yaml

  Artifacts are written to runtime/cli_validate/<TIMESTAMP>/:

SUMMARY.txt — human summary

summary.json — machine-readable snapshot (mergeable in CI)

report.html — browsable dashboard with links to each step’s stdout/stderr

*.out / *.err — raw outputs for deep debugging

Parameters

--provider : local | gcp | snowflake | odps (default from PROVIDER or FLUID_PROVIDER)

--project : Cloud project (optional; used by cloud providers)

--region : Region (default europe-west3)

--contracts: Comma-separated list of contract paths. If omitted, defaults to:

examples/customer360/contract.fluid.yaml

examples/local/high_value_churn/contract.fluid.yaml

--outdir : Output directory (default runtime/cli_validate/<ts>)

Environment alternatives:

PROVIDER / FLUID_PROVIDER

PROJECT / FLUID_PROJECT

REGION / FLUID_REGION

What the Script Tests

Environment & Tooling

Captures env, Python version, top pip packages → env.txt

Checks jq, yq, dot, gcloud and prints versions if present.

CLI Identity

Uses fluid --help & fluid version when available.

Always runs python -m fluid_build.cli --help as a fallback.

Provider Discovery

Calls fluid providers and records the JSON response.

You should see your providers listed (e.g., local, gcp, snowflake, odps).

Per-contract Flow

validate → ensures the contract is structurally OK.

plan → writes *_plan.json.

apply → executes the plan (for local provider: produces demo file at runtime/out/demo_artifact.csv).

graph → emits Graphviz DOT; if dot is present, a PNG is generated.

visualize-plan → *_plan.html mini report.

docs → docs_<contract>/README.md.

contract-tests → runs if a baseline.schema.json exists (e.g., examples/customer360/baseline.schema.json).

Scaffolding

scaffold-ci → .gitlab-ci.yml in the output folder.

scaffold-composer → minimal DAG under composer/dags/.

Reporting

results.jsonl — one JSON per test step (rc, stdout, stderr paths).

summary.json — totals + results merged into a single JSON.

report.html — quick, readable dashboard with links to logs.

Interpreting Results

Console shows ✔ / ⚠ / ✖ for each step.

For failures (✖), open the corresponding *.err file in the output folder.

report.html is meant for shareable status in reviews / CI artifacts.

Common Warnings

NotOpenSSLWarning from urllib3 (macOS system Python). Prefer a Python built against OpenSSL 3 (e.g., pyenv) for clean logs.

Missing dot → Graphviz PNG export is skipped, DOT still produced.

Missing yq → YAML-specific convenience is skipped (no functional impact).

CI Usage

This script is CI-friendly:

# .gitlab-ci.yml (example)
stages: [validate]

cli-validate:
  stage: validate
  image: python:3.11
  script:
    - pip install -e .
    - bash scripts/ccli_validate.sh --provider ${FLUID_PROVIDER:-local} --contracts examples/customer360/contract.fluid.yaml
  artifacts:
    when: always
    paths:
      - runtime/cli_validate/*/report.html
      - runtime/cli_validate/*/summary.json
      - runtime/cli_validate/*/*.out
      - runtime/cli_validate/*/*.err

Troubleshooting

“Unknown provider”
Run fluid providers and ensure your provider is listed. If not:

Verify provider modules under fluid_build/providers/<name>/.

Ensure __init__.py registers the provider into the registry.

apply does nothing on local
The local provider demo creates runtime/out/demo_artifact.csv.
If you need real transformations, wire your local provider’s apply() to your engine (e.g., DuckDB SQL).

Graph PNG not created
Install Graphviz: brew install graphviz (macOS), apt-get install graphviz (Debian/Ubuntu).

Extending

Add more contracts to --contracts.

Integrate additional steps (policy compile, OPDS export) by appending more run_cmd calls.

Style report.html further as you evolve plan visualizations.

Support

If you share SUMMARY.txt, summary.json, and report.html, we can quickly pinpoint issues and suggest targeted fixes.


---

If you want this wired into your repo immediately:
1) Save both files exactly as shown.
2) `chmod +x scripts/ccli_validate.sh`
3) Run a local validation:  
   `PROVIDER=local ./scripts/ccli_validate.sh --contracts examples/customer360/contract.fluid.yaml`
::contentReference[oaicite:0]{index=0}
 