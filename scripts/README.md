# FLUID Build — **Full System Diagnostic** (`diagnose.sh`)

A one-command, end-to-end **health check** for your FLUID Build project.
It validates your environment, providers, contracts, planning, visualization, scaffolding, consumer-impact tests, and (optionally) a local apply run.
When it’s done, it produces a **timestamped diagnostics bundle** you can share for precise triage.

---

## ✨ What this script does

`diagnose.sh` executes a comprehensive test suite and collects **logs + artifacts**:

1. **Environment snapshot**

   * OS, Python, OpenSSL/LibreSSL, PATH, virtualenv, and full `pip freeze`.

2. **Tooling presence checks**

   * `python`, `pip`, `jq`, `gcloud`, `dot` (Graphviz).

3. **Python module imports**

   * `fluid_build`, `google-cloud-bigquery`, `google-auth`, `duckdb`, `snowflake-connector-python`.

4. **Provider diagnostics**

   * Runs `fluid_build.cli doctor` with your selected provider.

5. **Contract validation**

   * Validates the primary **GCP** contract and (optionally) a **local** example.

6. **Plan generation (GCP)**

   * Creates a plan JSON for the contract and (if possible) a **Graphviz DOT** + **HTML** visualizer.

7. **Contract tests**

   * Consumer-impact checks: schema signature vs baseline.

8. **Scaffolding**

   * Generates **GitLab CI** template and **Composer DAG** stubs.

9. **Optional local apply**

   * If the local example exists and `duckdb` is installed, runs a smoke apply and captures outputs.

10. **Bundle**

    * Zips everything into `runtime/diag/<timestamp>.bundle.tar.gz`.

---

## 📦 Repository Layout Assumptions

The script assumes these defaults (override via env vars):

```
examples/
  customer360/
    contract.fluid.yaml
    baseline.schema.json
  local/
    high_value_churn/
      contract.fluid.yaml

runtime/
  plan/
  composer/
    dags/
  diag/
```

If your paths differ, set `CONTRACT_GCP`, `BASELINE_SCHEMA`, and `CONTRACT_LOCAL` environment variables before running.

---

## ✅ Prerequisites

* **Bash** on macOS/Linux (Windows WSL is fine).
* **Python 3.9+** (project virtualenv recommended).
* **Installed CLI** for your project (`fluid_build` Python package visible in the venv).
* Optional but recommended:

  * `jq` (for pretty JSON output)
  * `graphviz` (the `dot` executable) for plan visualization
  * `gcloud` for GCP provider checks
  * `duckdb` for optional local apply

---

## 🚀 Quick Start

```bash
# From your project root, create the scripts folder (if not present)
mkdir -p scripts

# Save the script
# (Paste the script content into scripts/diagnose.sh)

# Make executable
chmod +x scripts/diagnose.sh

# Run for GCP provider
PROVIDER=gcp PROJECT=your-gcp-project REGION=europe-west3 ./scripts/diagnose.sh

# Or run for local-only checks
PROVIDER=local ./scripts/diagnose.sh

# Or run Snowflake checks
PROVIDER=snowflake ./scripts/diagnose.sh
```

At the end, you’ll see a line like:

```
✔ Diagnostic bundle -> runtime/diag/20250101T120314Z.bundle.tar.gz
```

Share that bundle for exact, targeted help.

---

## ⚙️ Environment Variables

| Variable          | Default / Required                                    | Purpose                                                        |
| ----------------- | ----------------------------------------------------- | -------------------------------------------------------------- |
| `PROVIDER`        | `gcp` (choices: `gcp`, `local`, `snowflake`)          | Which provider to run diagnostics against.                     |
| `PROJECT`         | *(required for GCP)*                                  | GCP project id for planning/scaffolding.                       |
| `REGION`          | `europe-west3`                                        | GCP region for planning/scaffolding.                           |
| `CONTRACT_GCP`    | `examples/customer360/contract.fluid.yaml`            | Path to the main GCP contract.                                 |
| `BASELINE_SCHEMA` | `examples/customer360/baseline.schema.json`           | Consumer-impact baseline for schema compatibility checks.      |
| `CONTRACT_LOCAL`  | `examples/local/high_value_churn/contract.fluid.yaml` | Optional local example contract for a duckdb apply smoke test. |

---

## 🧪 What gets executed (high level)

* `python -m fluid_build.cli --provider <PROVIDER> doctor`
* `python -m fluid_build.cli validate <CONTRACT_GCP>`
* `python -m fluid_build.cli --provider gcp --project <PROJECT> --region <REGION> plan <CONTRACT_GCP> --out <plan.json>`
* `python -m fluid_build.cli visualize-plan <plan.json>` (if plan exists)
* `python -m fluid_build.cli contract-tests <CONTRACT_GCP> --baseline <BASELINE_SCHEMA>`
* `python -m fluid_build.cli --provider <PROVIDER> --project <PROJECT> --region <REGION> scaffold-ci <CONTRACT_GCP> --out <.gitlab-ci.yml>`
* `python -m fluid_build.cli --provider <PROVIDER> --project <PROJECT> --region <REGION> scaffold-composer <CONTRACT_GCP> --out-dir <dags/>`
* `python -m fluid_build.cli --provider local apply <CONTRACT_LOCAL>` (if local contract exists and `duckdb` installed)

All STDOUT/STDERR are captured to `runtime/diag/<timestamp>/logs/*.log`.

---

## 🗂️ Output & Artifacts

After a successful run, examine:

```
runtime/
  diag/
    2025...Z/
      env.txt                 # Environment snapshot (Python, OpenSSL, pip freeze)
      logs/
        doctor.log
        validate_customer360.log
        validate_local_high_value_churn.log
        plan_gcp_customer360.log
        visualize_plan.log
        contract_tests.log
        scaffold_ci.log
        scaffold_composer.log
        apply_local_high_value_churn.log
        fluid_build.log       # CLI internal log (if FLUID_LOG_FILE is set)
      artifacts/
        plan.json             # Planning output (if provider=gcp and project set)
        plan.dot              # Graphviz DOT (if visualize succeeded)
        plan.html             # HTML plan visualization (if available)
        plan.png              # PNG snapshot (if generated)
        .gitlab-ci.yml        # CI skeleton (scaffold)
        dags/
          <product>.py        # Composer DAG skeleton (scaffold)
        out/                  # Optional — outputs from local apply (if any)
    2025...Z.bundle.tar.gz    # The zipped diagnostics bundle
```

> **Tip:** Open `plan.html` in a browser for a quick glance at the plan structure.

---

## 🔍 Interpreting Results

* **Doctor**
  Confirms provider settings, auth context, and any missing environment variables.
  Look for `cli_success` and provider-specific checks.

* **Validate**
  Ensures the contract adheres to the FLUID schema.
  If it fails, `validate_failed` will include the error; fix the contract and rerun.

* **Plan**
  Produces a deterministic set of actions the provider will perform.

  * If empty: your contract may already match reality or provider settings are incomplete.
  * If present: inspect `plan.json` and `plan.html` to understand the changes.

* **Contract tests**
  Compares the *declared* schema to a baseline to prevent consumer breakage.

  * If incompatible: adjust your contract or introduce versioning & migration notes.

* **Scaffolding**

  * `.gitlab-ci.yml`: Confirm stages, jobs, and variables align with your org’s runner setup.
  * Composer DAGs: These are runnable skeletons—import into your Composer environment for scheduling.

* **Local apply** (optional)
  If `duckdb` is installed and local data/paths are correct, you should see files in `artifacts/out/`.

---

## 🧰 Troubleshooting (Common Issues)

### 1) `unrecognized arguments: --provider gcp ...`

Your CLI expects **global options before the subcommand**. Ensure the order:

✅ **Correct:**

```bash
python -m fluid_build.cli --provider gcp plan examples/customer360/contract.fluid.yaml --out runtime/plan.json
```

❌ Incorrect:

```bash
python -m fluid_build.cli plan --provider gcp examples/customer360/contract.fluid.yaml ...
```

The diagnostic script already uses the correct order.

---

### 2) `NotOpenSSLWarning / LibreSSL` on macOS

Homebrew / system Python may link to LibreSSL. Some packages (e.g., `urllib3`) warn.
**Workarounds:**

* Use a **python.org** installer (OpenSSL-backed).
* Or use **miniforge/conda** env with OpenSSL 1.1.1+.
* Warnings are often safe; upgrade path recommended for production.

---

### 3) `ModuleNotFoundError: No module named 'fluid_build.<...>'`

Your project package isn’t installed or importable.

**Fix:**

```bash
# In your venv, from project root:
pip install -e .
# or (if you use pyproject.toml/poetry):
pip install .
# or: python -m pip install -e .
```

Ensure `fluid_build/__init__.py` exists and your package metadata (`pyproject.toml` or `setup.py`) is correct.

---

### 4) `duckdb not installed — skipping local apply`

Install it if you want the local smoke test:

```bash
pip install duckdb
```

---

### 5) `IO Error: No files found that match the pattern ".../data/*.csv"`

Your local contract points to missing files.
Update paths in the contract or create the sample data files in the specified directories.

---

### 6) GCP Auth Issues

* Ensure you’ve run:

  ```bash
  gcloud auth application-default login
  gcloud config set project <YOUR_PROJECT>
  ```
* Or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key JSON (for CI).

---

## 🧪 Example Runs

### GCP Full Diagnostics

```bash
PROVIDER=gcp PROJECT=my-project REGION=europe-west3 ./scripts/diagnose.sh
```

### Local Only (no GCP, no Snowflake)

```bash
PROVIDER=local ./scripts/diagnose.sh
```

### Snowflake Checks

```bash
PROVIDER=snowflake ./scripts/diagnose.sh
```

### Override Contract Paths

```bash
PROVIDER=gcp PROJECT=my-project \
CONTRACT_GCP=some/other/contract.yaml \
BASELINE_SCHEMA=some/other/baseline.json \
./scripts/diagnose.sh
```

---

## 🔒 Security Notes

* The diagnostics bundle includes environment metadata, filesystem paths, and pipeline outputs.
  **Review before sharing** to ensure it doesn’t contain secrets.
* The script does **not** read secret files; however, logs may contain **resource names**.

---

## 🧩 Extending the Script

* Add provider-specific smoke tests (e.g., Snowflake DDL dry runs).
* Emit SARIF/JSON results for CI dashboards.
* Parse plan and assert invariants (e.g., **no destructive ops** in `main`).

---

## 🧱 CI Integration (Optional)

Add a lightweight CI job to run diagnostics on PRs (without apply):

```yaml
diagnostics:
  image: python:3.11
  stage: test
  before_script:
    - pip install . graphviz jq duckdb google-cloud-bigquery google-auth
  script:
    - PROVIDER=gcp PROJECT=$GCP_PROJECT REGION=$GCP_REGION ./scripts/diagnose.sh
  artifacts:
    when: always
    expire_in: 7 days
    paths:
      - runtime/diag/*/*.log
      - runtime/diag/*/artifacts/*
      - runtime/diag/*.bundle.tar.gz
```

---

## 🧭 Exit Codes

* **0** — all steps completed (some warnings may still exist; check logs)
* **non-zero** — at least one step failed (see `logs/*.log`)

> The script continues past non-critical failures to collect as much signal as possible.

---

## 📣 Need Help?

After you run:

1. Note the bundle path printed at the end (e.g., `runtime/diag/2025...Z.bundle.tar.gz`).
2. Share it with me. I’ll read:

   * `env.txt`
   * `logs/*.log`
   * `artifacts/plan.*`, `.gitlab-ci.yml`, `dags/*`
3. I’ll respond with precise, next-step fixes.

---

**Happy diagnosing!**



# 1) Save as scripts/diagnose.sh
chmod +x scripts/diagnose.sh

# 2) Run for GCP (recommended)
PROVIDER=gcp PROJECT=your-gcp-project REGION=europe-west3 ./scripts/diagnose.sh

# 3) Or run for local only
PROVIDER=local ./scripts/diagnose.sh

# 4) Or include snowflake checks
PROVIDER=snowflake ./scripts/diagnose.sh


# What this script covers

Env snapshot: Python, OpenSSL/LibreSSL, PATH, pip freeze.

Binary checks: python, pip, dot, gcloud, jq.

Python module imports: fluid_build, google-cloud-bigquery, google-auth, duckdb, snowflake-connector-python.

Provider diagnostics: doctor (with your chosen provider).

Contract validation: Customer 360 + Local example (if present).

Planning (GCP): Writes plan JSON, visualizes via Graphviz/HTML.

Contract tests: Compares schema to baseline (if provided).

Scaffolding: GitLab CI + Composer DAGs captured as artifacts.

Optional local apply: If DuckDB is installed and the local contract/data exist.

Bundling: Tarball containing all logs & artifacts for easy sharing.