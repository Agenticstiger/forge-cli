<div align="center">

<img src="https://raw.githubusercontent.com/Agenticstiger/forge-cli/main/assets/fluid-forge-logo.png" alt="FLUID Forge" width="480">

<br><br>

### Stop writing boilerplate. Start declaring Data Products.

**The declarative control plane for data engineering in the Agentic Era.**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://github.com/Agenticstiger/forge-cli/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/data-product-forge.svg)](https://pypi.org/project/data-product-forge/)
[![CI](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml)

[Documentation](https://agenticstiger.github.io/forge_docs/) · [Getting Started](https://agenticstiger.github.io/forge_docs/getting-started/) · [FLUID Specification](https://open-data-protocol.github.io/fluid/) · [The Book](https://a.co/d/04zTi7aQ) · [Community](https://github.com/Agenticstiger/forge-cli/discussions)

</div>


---

## 🌊 What is FLUID?

**FLUID** (Federated Layered Universal Instructional Declaration) is an open-source declarative framework for building, validating, and deploying data products across any cloud. You write a single YAML contract describing *what* your data product is — its schema, quality rules, access policies, and scheduling — and FLUID Forge compiles that into provider-specific infrastructure (BigQuery, Snowflake, AWS Glue, Athena, or local DuckDB) with full governance baked in.

Think of it as **Terraform for data products**: one contract, many clouds, zero boilerplate.

```
contract.fluid.yaml  →  fluid validate  →  fluid plan  →  fluid apply
       (declare)          (check)           (preview)       (deploy)
```

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
# 1. Install the CLI
pip install data-product-forge

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

---

## 🖥️ First-Time Setup from Source

Choose your platform below for step-by-step instructions.

### Prerequisites

| Requirement | Version |
|-------------|---------|
| **Python** | 3.9 or higher |
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
python3 --version   # Should print Python 3.9+
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
python3 --version   # Should print Python 3.9+
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

Download and install Python 3.9+ from [python.org](https://www.python.org/downloads/).

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
python --version   # Should print Python 3.9+
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

---

### Installing additional providers

Once the base install is working, add cloud providers as needed:

```bash
pip install -e ".[gcp]"         # + Google Cloud (BigQuery, GCS, Composer)
pip install -e ".[snowflake]"   # + Snowflake
pip install -e ".[all]"         # Everything (all providers + dev tools)
```



## 🧬 Anatomy of a FLUID Contract

Everything starts with `contract.fluid.yaml` — the **single source of truth** for your data product's entire lifecycle.

```yaml
fluidVersion: "0.7.2"
kind: DataProduct
id: example.customer_360
name: Customer 360
domain: analytics

metadata:
  layer: Gold
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

# 3. THE GOVERNANCE — Who (or what) can access it?
accessPolicy:
  grants:
    - principal: "group:marketing@example.com"
      permissions: ["read"]

agentPolicy:                           # ← Agentic Era Governance
  allowedModels: ["gpt-4", "claude-3"]
  allowedUseCases: ["analysis", "summarization"]
```

---

## 🔌 Providers — Bring Your Own Cloud

Providers are the bridge between your declarative contract and your target execution environment.

| Provider | Target Ecosystem | Superpowers |
|----------|-----------------|-------------|
| 💻 **local** | DuckDB, Local FS | Zero-config. Runs anywhere. Perfect for dev/test. |
| ☁️ **gcp** | Google Cloud | BigQuery, GCS, Composer (Airflow), Dataform, IAM. |
| 🌩️ **aws** | Amazon Web Services | S3, Glue, Athena, Redshift, MWAA, IAM. |
| ❄️ **snowflake** | Snowflake | Databases, schemas, streams, tasks, RBAC, sharing. |

> Export-only providers for open data standards: **odps**, **odcs**, **datamesh-manager**.

---

## 🛠️ Installation

FLUID Forge is modular. Install only what you need.

```bash
pip install data-product-forge                # Minimal — CLI + Local/DuckDB provider
pip install "data-product-forge[gcp]"         # + Google Cloud
pip install "data-product-forge[aws]"         # + AWS
pip install "data-product-forge[snowflake]"   # + Snowflake
pip install "data-product-forge[all]"         # Everything
```

> 💡 **Tip:** We recommend [pipx](https://pipx.pypa.io/) for an isolated global install:
> `pipx install "data-product-forge[all]"`

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
```

### AI & Code Generation

```bash
fluid forge                                  # 🤖 Interactive, AI-powered project creation
fluid generate-airflow contract.fluid.yaml   # Compile contract → native Airflow DAG
fluid generate-pipeline contract.fluid.yaml  # Scaffold transformation code
```

### Governance & Compliance

```bash
fluid policy-compile contract.fluid.yaml   # Translate policies → native IAM
fluid contract-tests contract.fluid.yaml   # Run assertion suites
```

### Visualization

```bash
fluid graph contract.fluid.yaml   # Graphviz DAG of internal lineage
fluid docs contract.fluid.yaml    # Auto-generate documentation from contract
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
| `policy-examples` | Advanced RBAC and AI agent governance |

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

[Apache License 2.0](LICENSE) · Copyright 2024–2026 Agentics Transformation Pty Ltd

---

<div align="center">

**Built for the future of data. Built for the Agentic Era.**

[Documentation](https://agenticstiger.github.io/forge_docs/) · [FLUID Specification](https://open-data-protocol.github.io/fluid/) · [The Book](https://a.co/d/04zTi7aQ) · [PyPI](https://pypi.org/project/data-product-forge/) · [Issues](https://github.com/Agenticstiger/forge-cli/issues)

---

🇿🇦 **Proudly developed by [dustlabs.co.za](https://dustlabs.co.za)**

</div>
