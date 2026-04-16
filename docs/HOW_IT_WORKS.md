# How Fluid Forge Works: The SDK + CLI Architecture

> A one-page guide for anyone confused about what lives where and why.

---

## The Two Packages

Fluid Forge is split into **two separate Python packages** that work together:

| Package | PyPI Name | Dependencies | Who Uses It |
|---------|-----------|-------------|-------------|
| **Provider SDK** | `fluid-provider-sdk` | **Zero** (stdlib only) | Provider authors (you, if building a new cloud adapter) |
| **CLI** | `fluid-forge` | ~40 deps (click, rich, pydantic, httpx, etc.) | Data engineers running `fluid validate / plan / apply` |

**Why the split?** If you're building a Databricks provider, you shouldn't need to install BigQuery, Snowflake, Rich, and 40 other packages just to subclass `BaseProvider`. The SDK gives you the contract (ABC + types) with zero baggage.

---

## What the SDK Defines (the "Contract")

The SDK (`fluid-provider-sdk`) is a **pure interface package**. It defines exactly what a provider must look like:

```
┌─────────────────────────────────────────────────────┐
│  fluid-provider-sdk                                 │
│                                                     │
│  BaseProvider (ABC)          ← subclass this        │
│    .plan(contract) → [actions]                      │
│    .apply(actions) → ApplyResult                    │
│    .capabilities() → ProviderCapabilities           │
│    .render(src) → artifact  (optional)              │
│                                                     │
│  ProviderHookSpec            ← mix in for hooks     │
│    .pre_plan() / .post_plan()                       │
│    .pre_apply() / .post_apply()                     │
│    .on_error() / .estimate_cost()                   │
│                                                     │
│  Types (no external deps):                          │
│    ApplyResult, ProviderAction, ProviderMetadata,   │
│    ProviderCapabilities, CostEstimate,              │
│    ProviderError, ProviderInternalError             │
│                                                     │
│  ContractHelper              ← parses YAML contracts│
│    .exposes() → [ExposeSpec]                        │
│    .builds()  → [BuildSpec]                         │
│    .consumes() → [ConsumeSpec]                      │
│                                                     │
│  Testing Harness             ← free conformance tests│
│    ProviderTestHarness + sample fixtures             │
└─────────────────────────────────────────────────────┘
```

---

## What the CLI Does (the "Engine")

The CLI (`fluid-forge`) is the **orchestration engine**. It loads your YAML contract, finds the right provider, and drives the plan/apply lifecycle:

```
 You run:  fluid apply contract.fluid.yaml
           │
           ▼
 ┌─────────────────────┐
 │  1. LOAD CONTRACT   │  loader.py reads YAML, merges env overlays
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  2. VALIDATE        │  schema.py + schema_manager.py check structure
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  3. FIND PROVIDER   │  Reads "platform:" from contract → looks up
 │                     │  provider registry → instantiates GcpProvider,
 │                     │  LocalProvider, AwsProvider, etc.
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  4. HOOKS: pre_plan │  If provider implements ProviderHookSpec
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  5. PLAN            │  provider.plan(contract) → list of actions
 │                     │  e.g. [create_dataset, create_table, grant_access]
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  6. HOOKS: post_plan│  Provider can filter/reorder actions
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  7. POLICY CHECK    │  policy/compiler.py + guardrails.py
 │                     │  Validates IAM bindings, sovereignty rules
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  8. APPLY           │  provider.apply(actions) → ApplyResult
 │                     │  Actually creates cloud resources
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  9. HOOKS: post_apply│  Notifications, lineage, audit
 └────────┬────────────┘
          ▼
 ┌─────────────────────┐
 │  10. REPORT         │  Observability → Command Center (optional)
 └─────────────────────┘
```

---

## How They Connect at Runtime

The CLI's `providers/base.py` is the **bridge**. It does this:

```python
try:
    from fluid_provider_sdk import BaseProvider, ApplyResult, ...  # Use the SDK
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False
    # Define identical fallback classes inline (self-contained mode)
```

This means:

- **If `fluid-provider-sdk` is installed** → the CLI uses the SDK's classes directly. Both the CLI and third-party providers share the exact same `BaseProvider` ABC.
- **If the SDK is NOT installed** → the CLI has an identical inline copy, so it still works. The built-in providers (GCP, AWS, Snowflake, Local) will function either way.

The built-in providers (e.g., `fluid_build/providers/gcp/provider.py`) import from `fluid_build.providers.base`, which re-exports whichever source was resolved above.

---

## How Providers Are Discovered

```
                    Provider Registry
                    (fluid_build/providers/__init__.py)
                    ┌──────────────────────────────────┐
                    │                                  │
 Built-in ─────────►│  1. Import fluid_build.providers.*│
 (gcp, aws,        │     modules and auto-register    │
  local, snowflake)│                                  │
                    │  2. Check pyproject.toml entry   │
 Third-party ──────►│     points under                 │
 (pip-installed)   │     "fluid_build.providers"      │
                    │                                  │
                    │  3. Thread-safe, idempotent      │
                    │     discovery with RLock         │
                    └──────────────────────────────────┘
```

A third-party provider just needs:

```toml
# In their pyproject.toml
[project.entry-points."fluid_build.providers"]
databricks = "my_databricks_provider:DatabricksProvider"
```

Then `pip install my-databricks-provider` and `fluid plan --provider databricks` works.

---

## The Mental Model (TL;DR)

Think of it like a **compiler toolchain**:

| Concept | Fluid Forge Equivalent |
|---------|----------------------|
| **Source code** | Your `contract.fluid.yaml` — declares what you want |
| **Compiler frontend** | The CLI — parses, validates, plans |
| **Compiler backend** | The Provider — knows how to talk to GCP/AWS/Snowflake |
| **Backend interface** | The SDK — the contract between frontend and backend |
| **Object code** | The ApplyResult — what actually got created in the cloud |

The SDK exists so backend authors don't need the frontend, and the frontend doesn't need to know about every possible backend.

---

## File Map (Where to Look)

| When you need to... | Look at... |
|---------------------|------------|
| Understand the provider contract | `fluid-provider-sdk/src/fluid_provider_sdk/base.py` |
| Parse a YAML contract in a provider | `fluid-provider-sdk/src/fluid_provider_sdk/contract.py` → `ContractHelper` |
| See how the CLI calls providers | `forge-cli/fluid_build/cli/apply.py` + `plan.py` + `hooks.py` |
| See how providers are found | `forge-cli/fluid_build/providers/__init__.py` |
| See the SDK ↔ CLI bridge | `forge-cli/fluid_build/providers/base.py` (the try/except import) |
| Build a new provider | `fluid-provider-sdk/src/fluid_provider_sdk/testing/harness.py` |
| See a working provider | `forge-cli/fluid_build/providers/local/local.py` (simplest) |
