<div align="center">

<img src="https://raw.githubusercontent.com/Agenticstiger/forge-cli/main/assets/fluid-forge-logo.png" alt="FLUID Forge" width="480">

<br><br>

### Stop writing boilerplate. Start declaring Data Products.

**The declarative control plane for data engineering in the Agentic Era.**

<a href="https://github.com/Agenticstiger/forge-cli/stargazers"><img src="https://img.shields.io/github/stars/Agenticstiger/forge-cli?style=social" alt="GitHub stars"></a>
<a href="https://github.com/Agenticstiger/forge-cli/network/members"><img src="https://img.shields.io/github/forks/Agenticstiger/forge-cli?style=social" alt="GitHub forks"></a>

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://github.com/Agenticstiger/forge-cli/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/data-product-forge.svg)](https://pypi.org/project/data-product-forge/)
[![Downloads](https://img.shields.io/pypi/dm/data-product-forge.svg)](https://pypi.org/project/data-product-forge/)
[![CI](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml)
[![Last commit](https://img.shields.io/github/last-commit/Agenticstiger/forge-cli)](https://github.com/Agenticstiger/forge-cli/commits/main)
[![Open issues](https://img.shields.io/github/issues/Agenticstiger/forge-cli)](https://github.com/Agenticstiger/forge-cli/issues)
[![Open PRs](https://img.shields.io/github/issues-pr/Agenticstiger/forge-cli)](https://github.com/Agenticstiger/forge-cli/pulls)

[Documentation](https://agenticstiger.github.io/forge_docs/) · [Getting Started](https://agenticstiger.github.io/forge_docs/getting-started/) · [FLUID Specification](https://open-data-protocol.github.io/fluid/) · [The Book](https://a.co/d/04zTi7aQ) · [Community](https://github.com/Agenticstiger/forge-cli/discussions)

<br>

<img src="https://raw.githubusercontent.com/Agenticstiger/forge_docs/main/docs/.vuepress/public/demos/local-quickstart.svg" alt="FLUID Forge — install through deploy in 30 seconds" width="800">

<sub><i>Install through deploy in 30 seconds, against the local DuckDB provider. Click through to <a href="https://agenticstiger.github.io/forge_docs/demos/">the demos library</a> for 8 more casts (GCP, AWS, Snowflake, agentPolicy, AI copilot…).</i></sub>

</div>


---

> 🚀 **Latest: [`v0.14.0`](CHANGELOG.md)** on PyPI · contract schema **`fluidVersion 0.7.5`** (GA) · 📖 **[Full documentation →](https://agenticstiger.github.io/forge_docs/)**

**Jump to:** [Why FLUID Forge?](#why-fluid-forge) · [What is FLUID?](#-what-is-fluid) · [Why We Built This](#-why-we-built-this) · [60 Seconds to Magic](#-60-seconds-to-magic) · [Installation](#installation) · [Contract Anatomy](#-anatomy-of-a-fluid-contract) · [Providers](#-providers--bring-your-own-cloud) · [CLI Reference](#-cli-command-reference) · [Templates](#-templates) · [Contributing](#-contributing)

## Why FLUID Forge?

<table>
<tr>
<td width="25%" align="center">
<strong>1 file. 4 clouds. 0 rewrites.</strong><br>
<sub>One contract.fluid.yaml. Swap <code>binding.platform</code> and ship.</sub>
</td>
<td width="25%" align="center">
<strong>Validate → deploy in 30s.</strong><br>
<sub>Local DuckDB out of the box. No cloud account, no credit card.</sub>
</td>
<td width="25%" align="center">
<strong>Compiles to native IAM.</strong><br>
<sub>policy-compile emits real BigQuery/Snowflake/AWS bindings.</sub>
</td>
<td width="25%" align="center">
<strong>Agentic governance, enforced.</strong><br>
<sub>agentPolicy gates which LLMs can read which fields, via the Fluid MCP output port (<code>fluid mcp output-port serve</code>).</sub>
</td>
</tr>
</table>

---

## 🌊 What is FLUID?

**FLUID** (Federated Layered Universal Instructional Declaration) is an open-source declarative framework for building, validating, and deploying data products across any cloud. You write a single YAML contract describing *what* your data product is — its schema, quality rules, access policies, and scheduling — and FLUID Forge compiles that into provider-specific infrastructure (BigQuery, Snowflake, AWS Glue, Athena, or local DuckDB) with full governance baked in.

Think of it as **Terraform for data products**: one contract, many clouds, zero boilerplate.

The everyday flow is three commands — validate, plan, apply. Behind the scenes, FLUID Forge ships an **11-stage deterministic pipeline** with cryptographic plan-binding and per-stage hard gates:

```
bundle → validate → generate-artifacts → validate-artifacts → diff →
plan → apply → policy-apply → verify → publish → schedule-sync
```

You rarely run all 11 by hand; `fluid generate ci` emits a parameterized CI pipeline (Jenkins, GitHub Actions, GitLab CI, Azure DevOps, Bitbucket, CircleCI, Tekton) that runs every stage in order with cryptographic binding between stages 6 and 7 — apply refuses to proceed if the plan has been tampered with since it was computed. See [AGENTS.md](AGENTS.md#the-11-stage-pipeline) for the full stage lifecycle and [the apply mode matrix](AGENTS.md#apply-mode-matrix) (`dry-run`, `create-only`, `amend`, `amend-and-build`, `replace`, `replace-and-build`).

**Under the hood (cloud providers): OpenTofu autogen.** `fluid apply` compiles the contract to a deterministic OpenTofu `main.tf.json` and delegates apply / state / drift / idempotency to the `tofu` binary — battle-tested infrastructure code instead of hand-rolled per-cloud apply. The emitter is modular: one `IacProviderPlugin` per cloud (`fluid_build/iac/providers/{aws,gcp,snowflake,confluent}.py`, dbt-adapter pattern). Preview the emitted module before applying with `fluid generate iac <contract>`. See [AUTOGEN_SPIKE.md](AUTOGEN_SPIKE.md) for the architecture and [HONESTLY_TESTED.md](HONESTLY_TESTED.md) for the coverage matrix (unit / emulator / live cloud).

**Packaging modes (0.7.6 preview).** Contracts can declare, per infrastructure container, whether the product **owns** it (`packaging: isolated` — today's default) or writes into a **platform-owned shared pool** (`shared`, emitted as an OpenTofu data source + leaf-only resources, so a tenant's product structurally cannot destroy the pool). Ownership transitions are guarded: `fluid apply --adopt-shared-container` is required for a shared→isolated flip and logs a WARNING-level audit event. See [RFC-packaging-modes.md](RFC-packaging-modes.md).

---

## 🤯 Why We Built This

Data engineering is stuck in the dark ages of imperative spaghetti code. You want to ship data products fast, but compliance teams demand governance. You end up with **Configuration Sprawl**: `.tf` files for Terraform, `schema.yml` for dbt, `.rego` for OPA, and a web of Airflow DAGs.

**FLUID Forge is the compiler that ends the chaos.** You declare what your data product *is*. The CLI compiles that into a validated, deterministic execution plan across any supported cloud.

### The Old Way vs. The FLUID Way

| 🛑 **The Old Way** (Imperative Chaos) | ✨ **The FLUID Way** (Declarative Order) |
|----------------------------------------|------------------------------------------|
| Weeks of boilerplate to wire up IaC, SQL, and DAGs. | **Minutes** to deploy. Just declare your intent and apply. |
| Vendor lock-in. Your DAGs only work on one cloud. | **Provider-agnostic.** Switch clouds by changing one line of YAML. |
| Governance as an afterthought. Manual compliance tickets. | **Governance-as-Code.** Policies compile to native IAM before deployment. |
| "Works on my machine." Broken production deploys. | **Deterministic plans.** See exactly what will change before it runs. |
| AI hallucinations. Agents don't understand your tables. | **Semantic Truth.** Built-in OSI semantics so LLMs query perfectly. |

---


## ⚡ 60 Seconds to Magic

Data engineering shouldn't require weeks of handwritten infrastructure code, bespoke CI/CD pipelines, and copy-pasted SQL.

**What Terraform did for infrastructure, FLUID Forge does for data products.**

```bash
# 1. Install the CLI (the [local] extra bundles the DuckDB dev provider)
pip install "data-product-forge[local]"

# 2. Validate your data product contract
fluid validate contract.fluid.yaml

# 3. See exactly what will happen
fluid plan contract.fluid.yaml

# 4. Deploy infrastructure, logic, and governance — instantly
fluid apply contract.fluid.yaml
```

That's it. You just deployed a **versioned, governed, and orchestrated Data Product** from a single YAML file.

> **Want to move from local to Google Cloud?**
> `pip install "data-product-forge[gcp]"` → change `platform: local` to `platform: gcp` → run `fluid apply`. **Done.**

### From an existing catalog — `fluid forge data-model from-source` (V1.5)

Already have your tables registered in **Snowflake Horizon, Databricks Unity, BigQuery, Dataplex, AWS Glue, DataHub, or Data Mesh Manager**? Forge a Fluid contract directly from the metadata you've already curated — descriptions, tags, lineage, classifications, ownership — instead of re-typing it.

```bash
# 1. Install the catalog extra (or use [catalogs] for all of them).
pip install "data-product-forge[snowflake]"

# 2. One-time source setup (saves to ~/.fluid/sources.yaml; secrets to OS keyring).
fluid ai setup --source snowflake --name snowflake-prod

# 3. Forge a Data Vault 2.0 model from a schema.
fluid forge data-model from-source \
  --source snowflake --credential-id snowflake-prod \
  --database <DATABASE> --schema <SCHEMA> \
  --technique data-vault-2 \
  -o customer_orders.fluid.yaml

# 4. Generate dbt transformations from the same contract.
fluid generate speed-transformation customer_orders.fluid.yaml -o ./dbt_customer_orders
```

Three guarantees that hold across every catalog: **read-only metadata access** (no `SELECT *` against any data table), **per-call credentials** (the MCP server never holds your secrets), and **full audit trail** (every catalog read writes a redacted event under `~/.fluid/store/audit/`).

The same flow is exposed via the MCP `forge_from_source` tool — Claude Code, Cursor, and any MCP client can drive a catalog forge from inside the editor. See the [catalogs walkthrough](https://agenticstiger.github.io/forge_docs/cli/catalogs/) for per-catalog privilege grants and end-to-end demos.

### Which ODPS? — Bitol center stage, LF/ODPI in the back pocket

Two different specs share the **ODPS** acronym. FLUID treats **Bitol Open Data Product Standard v1.0.0** as the default, center-stage target; the Linux Foundation / ODPI Open Data Product Specification v4.1 is a secondary, opt-in export for catalogs that require it.

| Standard | Role | What it is | Spec | CLI selector | `--format` |
|---|---|---|---|---|---|
| **Bitol ODPS v1.0.0** | **default** | Open Data Product **Standard**. Bidirectional; product wrapper that references ODCS contracts by `contractId`. | [bitol-io/open-data-product-standard](https://github.com/bitol-io/open-data-product-standard) | `fluid odps export --spec bitol-1.0.0` *(default)* | `--format odps` / `--format odps-bitol` |
| **LF/ODPI ODPS v4.1** | opt-in | Open Data Product **Specification**. Hosted by the Linux Foundation / Open Data Product **Initiative** (ODPI). Export-only; single JSON document. | [Open-Data-Product-Initiative/v4.1](https://github.com/Open-Data-Product-Initiative/v4.1) | `fluid odps export --spec odps-4.1` | `--format odps-v4.1` |

Bitol export emits **1 ODPS doc + N sibling `<contractId>.odcs.yaml`** fragments; import reverses it — a single ODPS file, a directory bundle, or a lone ODCS file all converge on one validated FLUID contract (and `fluid forge --seed-from <path>` accepts the same three shapes as an AI-authoring seed).

**Bitol v1.1.0** (the RFC 0029 top-level data-product `type`, mapping FLUID's SDP / ADP / CDP to `sourceAligned` / `aggregate` / `consumerAligned`) is supported opt-in via `--api-version v1.1.0` or `ODPS_API_VERSION=v1.1.0`; the emit default stays v1.0.0 until v1.1.0 is released upstream. v1.1.0 documents validate and import against their own declared `apiVersion`.

<details>
<summary>Back-compat aliases</summary>

All emit a WARNING pointing at the canonical form:
- `--spec odpi-4.1` and `--version 4.1` — historical labels for the LF/ODPI v4.1 spec

</details>

→ [full ODPS guide](https://agenticstiger.github.io/forge_docs/cli/odps.html)

### From dev to CI — `fluid generate ci`

When you're ready to run the full 11-stage pipeline in CI instead of manually, one command emits the pipeline file for your CI system:

```bash
# Jenkins (ships the richest template today — every stage parameterized)
fluid generate ci --system jenkins --out Jenkinsfile

# GitHub Actions, GitLab, Azure DevOps, Bitbucket, CircleCI, Tekton
fluid generate ci --system github --out .github/workflows/fluid.yml
```

The generated Jenkinsfile has a **"Build with Parameters"** dialog where operators pick the subset of stages to run + the apply mode + the publish targets + schedule-sync scheduler — all without editing Groovy:

| Param | Default | Purpose |
|---|---|---|
| `RUN_STAGE_N_*` (11 booleans) | stages 1–9 `true`; 10 (publish) & 11 (schedule-sync) `false` | Toggle individual stages |
| `APPLY_MODE` | `dry-run` | 6-mode choice (pick `amend` for an additive apply) |
| `ALLOW_DATA_LOSS` | `false` | Required for `replace*` outside dev |
| `PUBLISH_TARGETS` | `datamesh-manager` | Space-separated list |
| `SCHEDULER` | `` (none) | DAG push target for stage 11 |

Generated Jenkinsfiles install `fluid` via `pip install data-product-forge` at build time by default. For lab iteration against a local forge-cli checkout, `fluid generate ci --install-mode dev-source` emits a Jenkinsfile that bind-mounts your checkout inside the Jenkins container — zero pip install, changes reflect live.

---

## Installation

FLUID Forge is modular — install only what you need. **Requires Python 3.10+.**

```bash
pip install "data-product-forge[local]"       # Recommended start — CLI + Local/DuckDB provider
pip install data-product-forge                # Core CLI only (validate/plan/generate; no DuckDB)
pip install "data-product-forge[gcp]"         # + Google Cloud (BigQuery, GCS, Composer)
pip install "data-product-forge[aws]"         # + AWS (S3, Glue, Athena, Redshift)
pip install "data-product-forge[snowflake]"   # + Snowflake
pip install "data-product-forge[all]"         # All providers + dev tools (catalog adapters: [catalogs])
```

> 💡 **Tip:** We recommend [pipx](https://pipx.pypa.io/) for an isolated global install:
> `pipx install "data-product-forge[all]"`

### Docker (GHCR)

Every release also publishes a container image to GitHub Container Registry, tagged `latest`, `X.Y.Z`, and `X.Y` — each scanned for HIGH/CRITICAL CVEs before push:

```bash
docker pull ghcr.io/agenticstiger/forge-cli:latest
docker run --rm ghcr.io/agenticstiger/forge-cli --version
docker run --rm -v "$(pwd)":/workspace ghcr.io/agenticstiger/forge-cli validate /workspace/contract.fluid.yaml
```

Verify, then scaffold your first product:

```bash
fluid --version
fluid init demo --template hello-world
```

<details>
<summary><strong>Install from source (for contributors)</strong></summary>

Choose your platform below for step-by-step instructions.

### Prerequisites

| Requirement | Version |
|-------------|---------|
| **Python** | 3.10 or higher |
| **pip** | Latest recommended |
| **Git** | Any recent version |

---

<details>
<summary><strong>🍎 macOS</strong></summary>

#### 1. Install system dependencies

```bash
# Install Xcode Command Line Tools (includes Git and make)
xcode-select --install

# Or install Python & Git via Homebrew (recommended)
brew install python git
```

#### 2. Verify Python

```bash
python3 --version   # Should print Python 3.10+
```

#### 3. Clone and install

```bash
git clone https://github.com/Agenticstiger/forge-cli.git
cd forge-cli

# Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode with the local provider
pip install --upgrade pip wheel
pip install -e ".[local]"
```

#### 4. Verify the installation

```bash
fluid --version
fluid validate examples/01-hello-world/contract.fluid.yaml
```

> **Tip:** You can also run the automated setup script:
> ```bash
> chmod +x setup.sh && ./setup.sh
> ```

</details>

<details>
<summary><strong>🐧 Ubuntu / Debian Linux</strong></summary>

#### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git build-essential
```

#### 2. Verify Python

```bash
python3 --version   # Should print Python 3.10+
```

#### 3. Clone and install

```bash
git clone https://github.com/Agenticstiger/forge-cli.git
cd forge-cli

# Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode with the local provider
pip install --upgrade pip wheel
pip install -e ".[local]"
```

#### 4. Verify the installation

```bash
fluid --version
fluid validate examples/01-hello-world/contract.fluid.yaml
```

> **Tip:** You can also run the automated setup script:
> ```bash
> chmod +x setup.sh && ./setup.sh
> ```

</details>

<details>
<summary><strong>🪟 Windows</strong></summary>

#### 1. Install Python

Download and install Python 3.10+ from [python.org](https://www.python.org/downloads/).

> **Important:** Check **"Add Python to PATH"** during installation.

#### 2. Install Git

Download and install Git from [git-scm.com](https://git-scm.com/download/win), or install via a package manager:

```powershell
# Via winget
winget install Git.Git

# Or via Chocolatey
choco install git
```

#### 3. Verify prerequisites

```powershell
python --version   # Should print Python 3.10+
git --version
```

#### 4. Clone and install

**PowerShell:**
```powershell
git clone https://github.com/Agenticstiger/forge-cli.git
cd forge-cli

# Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install in editable mode with the local provider
pip install --upgrade pip wheel
pip install -e ".[local]"
```

**Command Prompt (cmd):**
```cmd
git clone https://github.com/Agenticstiger/forge-cli.git
cd forge-cli

python -m venv .venv
.venv\Scripts\activate.bat

pip install --upgrade pip wheel
pip install -e ".[local]"
```

#### 5. Verify the installation

```powershell
fluid --version
fluid validate examples\01-hello-world\contract.fluid.yaml
```

> **Tip:** You can also run the automated setup script:
> - **PowerShell:** `.\setup.ps1`
> - **Command Prompt:** `setup.bat`

</details>

> Adding a provider to a source checkout? Use the editable form, e.g. `pip install -e ".[gcp]"` / `".[snowflake]"` / `".[all]"`.

</details>

---

## 🧬 Anatomy of a FLUID Contract

Everything starts with `contract.fluid.yaml` — the **single source of truth** for your data product's entire lifecycle.

```yaml
fluidVersion: "0.7.5"
kind: DataProduct
id: example.customer_360
name: Customer 360
domain: analytics

metadata:
  layer: Gold              # medallion vocabulary
  productType: CDP         # Data Mesh vocabulary — Bronze↔SDP, Silver↔ADP, Gold↔CDP
  owner:
    team: data-platform
    email: platform@example.com

# 1. THE LOGIC — How is it built?
builds:
  - id: transform_customer
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        SELECT user_id, email, LTV
        FROM raw.users JOIN raw.orders USING (user_id)

# 2. THE INTERFACE — What does it output?
exposes:
  - exposeId: customer_profiles
    kind: table
    binding:
      platform: snowflake              # ← Change to 'gcp' or 'aws' instantly
      format: snowflake_table
      location:
        database: PROD
        schema: GOLD
        table: CUST_360
    contract:
      schema:
        - name: email
          type: string
          sensitivity: pii             # ← Triggers auto-masking/encryption

    # 3. THE SEMANTICS — What do the numbers mean?
    #    Compiles to MetricFlow semantic_models + metrics in the generated
    #    dbt project, and grounds the governed MCP `query` tool.
    semantics:
      entities:
        - name: customer
          type: primary
          expr: user_id
      measures:
        - name: lifetime_value
          agg: sum
          expr: LTV

    # 4. THE GOVERNANCE — Which agents can read it, and for what?
    policy:
      agentPolicy:                     # ← Agentic Era Governance
        allowedModels: ["gpt-4", "claude-3"]
        allowedUseCases: ["analysis", "summarization"]

# Who (or what) can access it?
accessPolicy:
  grants:
    - principal: "group:marketing@example.com"
      permissions: ["read"]
```

This exact contract validates as-is: `fluid validate contract.fluid.yaml` → `✅ Valid FLUID contract (schema v0.7.5)`.

---

## 🔌 Providers — Bring Your Own Cloud

Providers are the bridge between your declarative contract and your target execution environment.

| Provider | Target Ecosystem | Superpowers |
|----------|-----------------|-------------|
| 💻 **local** | DuckDB, Local FS | Zero-config. Runs anywhere. Perfect for dev/test. |
| ☁️ **gcp** | Google Cloud | BigQuery, GCS, Composer (Airflow), Dataform, IAM, BigLake Iceberg. |
| 🌩️ **aws** | Amazon Web Services | S3, Glue, Athena, Redshift, MWAA, IAM. |
| ❄️ **snowflake** | Snowflake | Databases, schemas, streams, tasks, RBAC, sharing, Iceberg tables (auto-provisioned external volumes). |
| 🔗 **confluent** | Confluent Cloud | Managed Kafka → Iceberg (Tableflow topics + Glue catalog integration). |

> Open-data-standard **export formats** (not cloud providers): **ODPS** (Open Data Product Standard — Bitol / LF-ODPI) via `fluid odps`, plus **odcs** and **datamesh-manager** integrations.

> **Streaming exposes** compile to Kafka Connect / Debezium sink configs — including managed Iceberg tables on Snowflake and BigQuery. `fluid apply` provisions the prerequisites dbt refuses to create (external volumes, GCS buckets), and `fluid validate` gates any Iceberg expose missing a required input before it can silently no-op. See [RFC-streaming-extension.md](RFC-streaming-extension.md).

---

## 💻 CLI Command Reference

FLUID Forge is designed to feel as natural as `git` or `terraform`.

### Core Lifecycle

```bash
fluid init                           # Scaffold a new Data Product contract
fluid validate contract.fluid.yaml   # Validate schema, dependencies, syntax
fluid plan contract.fluid.yaml       # Generate a deterministic execution plan
fluid apply contract.fluid.yaml      # Execute the plan against your target provider
fluid verify contract.fluid.yaml     # Post-deployment data quality & compliance checks
fluid ship contract.fluid.yaml       # One-shot macro: validate → bundle → plan → apply
```

### AI & Code Generation

```bash
fluid forge                                  # 🤖 Interactive, AI-powered project creation
fluid forge --offline                        # Same flow, no LLM / no network (air-gapped, CI)
fluid forge --watch                          # Re-forge automatically when the source data changes
fluid generate-airflow contract.fluid.yaml   # Compile contract → native Airflow DAG
fluid generate transformation contract.fluid.yaml  # Scaffold dbt/SQL transformation code
fluid generate-pipeline --provider github_actions  # CI/CD pipeline configs (7 systems)
fluid generate iac contract.fluid.yaml       # Emit a deterministic OpenTofu main.tf.json
fluid generate vector contract.fluid.yaml    # pgvector RAG target — embeddings table + ANN index
fluid stats                                  # Aggregate LLM cost across forge runs
fluid agents list                            # List / show / prune forge runs (.fluid/agents/)
fluid plugins list                           # Installed plugins (providers, exporters, commands)
```

Forge is extensible: third-party packages can register custom LLM providers and exporters via Python entry points — `pip install` them and they appear in `fluid plugins list` / `fluid exporters`, no core edit needed.

### Brownfield import & reconciliation

```bash
# dbt manifest → FLUID contract(s). --split-by folder|group emits one product
# per boundary, with cross-split ref()s becoming cross-product consumes[].
fluid import dbt ./my_dbt_project --split-by group --out ./products

fluid verify contract.fluid.yaml --reconcile-dbt      # Contract schema vs dbt schema.yml drift
fluid verify contract.fluid.yaml --reconcile-lineage  # Declared vs observed lineage (local-only)
```

Generated dbt projects close the loop the other way: enforced model contracts (`--model-contracts`), `sources.yml` freshness from the contract's SLOs, MetricFlow `semantic_models` + `metrics` YAML from the `semantics` block, and dbt Fusion (v2) compatibility out of the box.

### Missions — deep agents with verifiable goals

A mission is a YAML spec pairing a plain-language goal with **code-owned success criteria** — the criteria, not the model, decide when work is done.

```bash
fluid mission check mission.yaml contract.fluid.yaml  # Zero-LLM scorecard — drops straight into CI
fluid mission run mission.yaml                        # Autonomous loop with USD/wall-clock/iteration budgets
fluid mission trust mission.yaml                      # Pin a workspace spec's content hash (direnv-style)
fluid mission list                                    # Built-ins: gdpr-clean, quality-coverage
```

See [RFC-deep-agents.md](RFC-deep-agents.md) for the trust-pinning and gating model.

### Staged Forge Pipeline (v1.0)

```bash
# Forge a reviewable data-model contract from an intent or DDL files.
# OSI v0.1.1 semantics + Logical IR sidecar are emitted alongside the contract.
fluid forge data-model from-intent intent.yaml -o customer_orders.fluid.yaml
fluid forge data-model from-ddl --ddl legacy/*.sql -o customer_orders.fluid.yaml \
    --source-type snowflake --technique data-vault-2

# Validate, diff, dump-DDL companions
fluid forge data-model validate customer_orders.fluid.yaml
fluid forge data-model diff old.model.json new.model.json
fluid forge data-model dump-ddl --database <DATABASE> --schema <SCHEMA> -o /tmp/snapshot.sql

# Memory + roadmap
fluid memory show project|team|episodic|semantic
fluid memory search "<query>" --ns memory/semantic --mode hybrid
fluid roadmap

# MCP server (Claude Code, Cursor, …)
fluid mcp serve
```

Detailed usage: [`docs/forge-data-model.md`](docs/forge-data-model.md).
Provider matrix + setup: [`docs/PROVIDERS.md`](docs/PROVIDERS.md).
OSI semantics shape: [`docs/OSI.md`](docs/OSI.md).
Migration from pre-v1.0: [`docs/MIGRATION.md`](docs/MIGRATION.md).

### Governance & Compliance

```bash
fluid policy-compile contract.fluid.yaml   # Translate policies → native IAM
fluid contract-tests contract.fluid.yaml   # Run assertion suites
```

### MCP output port — serve data products to AI agents

Turn any FLUID expose into an agent-consumable MCP server with the
contract's `agentPolicy` enforced at every read.

```bash
# stdio (default — for Claude Desktop / Cursor / MCP Inspector)
fluid mcp output-port serve contract.fluid.yaml

# HTTP / SSE for network deployments (front with mTLS proxy)
FLUID_MCP_AUTH_TOKEN=$(openssl rand -hex 32) \
  fluid mcp output-port serve contract.fluid.yaml \
  --transport http --host 0.0.0.0 --port 8765

# CLI overrides for ops / incident response
fluid mcp output-port serve contract.fluid.yaml \
  --allow-models claude-haiku-4-5-20251001 \
  --deny-use-cases training,fine_tuning
```

Drivers in-tree: DuckDB · Postgres · Snowflake · BigQuery · AWS Athena.
See [`examples/mcp-output-port-docker/`](examples/mcp-output-port-docker/)
for a one-command Postgres demo + the [`proxy/`](examples/mcp-output-port-docker/proxy/)
templates (Caddy / nginx) for production mTLS.

### Visualization

```bash
fluid viz-graph contract.fluid.yaml    # Render the contract's internal DAG (SVG; --mesh for the cross-product mesh)
fluid docs --src ./products --out ./site   # Auto-generate a docs site from every contract under --src
```

### Diagnostics

```bash
fluid doctor   # Environment diagnostics — Python, providers, CLIs, creds
```

---

## 🎓 Templates

Don't start from scratch. `fluid init` ships with battle-tested enterprise patterns:

```bash
fluid init --template customer-360
```

| Template | What You Get |
|----------|-------------|
| `hello-world` | The basics — start here |
| `incremental-processing` | Append/Merge load patterns |
| `multi-source` | Complex DAG dependency orchestration |
| `customer-360` | Customer analytics quickstart |
| `data-quality-validation` | Quality rules and contract test patterns |

For advanced RBAC and AI-agent governance patterns, see [`examples/policy-examples/`](examples/policy-examples/).

---

## 🤝 Contributing

FLUID Forge is community-driven. We want your ideas, providers, and pull requests!

1. Fork the repo
2. Run `make setup` for a full dev environment
3. Check out [CONTRIBUTING.md](CONTRIBUTING.md) for style guides and architecture overview

Please read our [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

To report security vulnerabilities, see [SECURITY.md](SECURITY.md).

---

## License

[Apache License 2.0](LICENSE) · Copyright 2024–2026 Agentics Transformation Limited

---

<div align="center">

**Built for the future of data. Built for the Agentic Era.**

[Documentation](https://agenticstiger.github.io/forge_docs/) · [FLUID Specification](https://open-data-protocol.github.io/fluid/) · [The Book](https://a.co/d/04zTi7aQ) · [PyPI](https://pypi.org/project/data-product-forge/) · [Issues](https://github.com/Agenticstiger/forge-cli/issues)

---

🇿🇦 **Proudly developed by [dustlabs.co.za](https://dustlabs.co.za)**

</div>
