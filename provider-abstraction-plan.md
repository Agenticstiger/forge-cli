# FLUID Provider Abstraction & Plugin Ecosystem Plan

> **Status:** Implementation in progress — Phases 0–4 complete, Phase 5 pending  
> **Date:** 2026-03-02 (amended 2026-03-02, implementation status updated 2026-03-02)  
> **Goal:** Design an extensible provider system that enables third-party adoption and a self-sustaining ecosystem around FLUID data products.  
> **Rating:** **7.5 / 10** — see [Plan Rating](#plan-rating) for details.
>
> ### Implementation Progress
>
> | Phase | Status | Tests | Key Artifacts |
> |---|---|---|---|
> | **Phase 0** — Unblock External Providers | **COMPLETE** | 31 tests | Entry-point discovery, registry consolidation, LocalProvider migration, `build_provider` normalization |
> | **Phase 1** — Provider SDK Extraction | **COMPLETE** | +10 (41 total) | `fluid-provider-sdk` v0.1.0 package, `BaseProvider` import shim, `ProviderMetadata`, `get_provider_info()` on all 4 built-in providers |
> | **Phase 2** — Contract Helpers + Action Schema | **COMPLETE** | +42 (73 total) | `ContractHelper`, `ProviderAction`, `validate_actions()`, LocalProvider planner migration, `--validate-actions` CLI flag |
> | **Phase 3** — Test Harness + Scaffolder | **COMPLETE** | +41 (114 total) | `ProviderTestHarness` (16 conformance tests), 4 fixture contracts, `fluid provider-init <name>` scaffolder |
> | **Phase 4** — Lifecycle Hooks + Capabilities v2 | **COMPLETE** | +54 (168 total) | `ProviderHookSpec`, `CostEstimate`, `invoke_hook`/`has_hook`, `ProviderCapabilities` v2, `--estimate-cost`/`--check-sovereignty` CLI flags, bugfixes H1-H3/M1-M7 |
> | **Phase 5** — Provider Index / Marketplace | NOT STARTED | — | Provider search/install/publish, JSON index, quality tiers |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Plan Rating](#plan-rating)
3. [How the Best Open-Source Projects Do It](#2-how-the-best-open-source-projects-do-it)
   - [Apache Airflow — Provider Packages](#21-apache-airflow--provider-packages)
   - [Terraform — Plugin Protocol + Registry](#22-terraform--plugin-protocol--registry)
   - [dbt — Adapter SDK + Hub](#23-dbt--adapter-sdk--hub)
   - [Pytest — Entry-Point Plugins](#24-pytest--entry-point-plugins)
   - [Stevedore (OpenStack) — Driver Pattern](#25-stevedore-openstack--driver-pattern)
   - [Key Lessons Across All Projects](#26-key-lessons-across-all-projects)
4. [Current State of FLUID Providers](#3-current-state-of-fluid-providers)
   - [Architecture Diagram](#31-architecture-diagram)
   - [What Works Well](#32-what-works-well)
   - [Critical Gaps](#33-critical-gaps)
   - [Code Evidence](#34-code-evidence)
5. [Breaking Changes Analysis](#breaking-changes-analysis)
6. [Options Analysis](#4-options-analysis)
   - [Option A: Entry-Point Activation (Quick Win)](#option-a-entry-point-activation-quick-win)
   - [Option B: Provider SDK Package](#option-b-provider-sdk-package)
   - [Option C: Full Plugin Protocol + Marketplace](#option-c-full-plugin-protocol--marketplace)
   - [Option D: Hybrid (Recommended)](#option-d-hybrid-recommended)
7. [Detailed Design — Option D (Hybrid)](#5-detailed-design--option-d-hybrid)
   - [Phase 0: Unblock External Providers](#phase-0-unblock-external-providers-week-1)
   - [Phase 1: Provider SDK Extraction](#phase-1-provider-sdk-extraction-weeks-2-4)
   - [Phase 2: Contract Helpers + Action Schema](#phase-2-contract-helpers--action-schema-weeks-3-5)
   - [Phase 3: Test Harness + Scaffolder](#phase-3-test-harness--scaffolder-weeks-5-7)
   - [Phase 4: Lifecycle Hooks + Capabilities v2](#phase-4-lifecycle-hooks--capabilities-v2-weeks-8-10)
   - [Phase 5: Provider Index / Marketplace](#phase-5-provider-index--marketplace-month-4)
8. [Third-Party Provider Developer Experience](#provider-developer-experience)
9. [Migration Strategy for Existing Providers](#6-migration-strategy-for-existing-providers)
10. [Versioning & Compatibility](#7-versioning--compatibility)
11. [Risk Assessment](#8-risk-assessment)
12. [Decision Log](#9-decision-log)
13. [Appendix: File-Level Refactoring Map](#10-appendix-file-level-refactoring-map)

---

## 1. Executive Summary

FLUID's provider system today is **functional but closed**. All four providers (AWS, GCP, Snowflake, Local) live inside the monolith. A third party literally cannot build an out-of-tree provider without forking the repo — despite entry points being declared in `pyproject.toml`, they are never consumed at runtime.

This document proposes a phased plan that draws from proven patterns in Airflow, Terraform, dbt, and pytest to evolve FLUID's providers into an open, extensible ecosystem. The plan is structured as four options (A through D) with a recommended hybrid path (D) that stages easily from quick wins to a full marketplace.

**The single most impactful change:** Activate `setuptools` entry-point discovery in `discover_providers()`. This is ~15 lines of code and immediately enables `pip install fluid-provider-X` to work.

---

## Plan Rating

> **Overall: 7.5 / 10** — Solid architecture, correct OSS pattern choices, good phasing strategy. Needs deeper treatment of breaking changes, contract parsing complexity, and the third-party DX. Amended below.

| Dimension | Score | Notes |
|---|---|---|
| **OSS Research Quality** | 9/10 | Correct patterns selected. Airflow, Terraform, dbt, pytest are the right comps. |
| **Gap Identification** | 8/10 | All 10 gaps are real and verified by code review. Originally understated G2 (we found 3 registries, not 2 — forge has its own) and G3 (contract divergence is worse than shown). |
| **Options Analysis** | 8/10 | Four options well-differentiated. Hybrid is the right call. |
| **Phase 0 Feasibility** | 7/10 | Entry-point activation is straightforward, but LocalProvider migration is harder than "just change the decorator" — `apply()` and `render()` signatures don't match BaseProvider. We've added detailed migration steps below. |
| **Phase 2 Feasibility** | 6/10 | `ContractHelper` design underestimates the deep structural disagreements (Snowflake's top-level binding, GCP's dual-format compat, Local's lack of binding entirely). The spec design needs amendment — done below. |
| **Breaking Changes Coverage** | 5/10 | Originally missing. This is the biggest gap. We've added a complete [Breaking Changes Analysis](#breaking-changes-analysis) section. |
| **Provider DX** | 6/10 | The scaffolder concept is good but the plan needs more attention to the end-to-end developer journey. We've added a [Provider Developer Experience](#provider-developer-experience) section. |
| **Risk Assessment** | 7/10 | Correct risks. Added new ones from deep review (name normalization, build_provider raw lookup, forge registry confusion). |

### Key Amendments Made (v2)

1. **Added [Breaking Changes Analysis](#breaking-changes-analysis)** — complete per-phase breaking change inventory with severity ratings and mitigations
2. **Revised G2** — there are actually **3 registries** (not 2): `providers/__init__.py`, `providers/base.py`, and `forge/__init__.py`
3. **Added G11** — `build_provider()` reads raw name from PROVIDERS dict, bypassing `get_provider()`'s normalization. `"GCP"` != `"gcp"`.
4. **Added G12** — `build_provider_instance()` exists in both registries but has zero callers — dead code
5. **Revised Phase 0** — detailed LocalProvider migration plan addressing `apply()` and `render()` signature mismatches
6. **Revised Phase 2** — deeper `ContractHelper` design that accounts for all 5 contract parsing patterns discovered
7. **Added [Provider Developer Experience](#provider-developer-experience)** — complete journey from "I want to build a provider" to "it's published"
8. **Updated Risk Assessment** with 3 new risks found in code review

---

### 2.1 Apache Airflow — Provider Packages

**Architecture:** Airflow decoupled providers from core in the Airflow 2.0 rewrite. Each provider is an independent PyPI package (e.g., `apache-airflow-providers-google`) with its own version, changelog, and release cycle.

**Key Design Decisions:**
| Aspect | How Airflow Does It |
|---|---|
| **Discovery** | `setuptools` entry points under the `apache_airflow_provider` group. Core scans `importlib.metadata.entry_points()` at startup. |
| **Interface** | Providers expose hooks, operators, sensors, and transfers. Each has a well-defined base class (`BaseHook`, `BaseOperator`, `BaseSensorOperator`). |
| **Packaging** | Each provider is a standalone PyPI package. `pip install apache-airflow-providers-google` — done. |
| **Metadata** | Each provider declares a `get_provider_info()` function returning name, description, versions, hook-class-names, connection-types, and extra-links. |
| **Testing** | Core ships a test harness. Provider packages must pass a compatibility matrix (provider version × core version). |
| **Extensions** | Providers can extend core beyond just operators: custom connections, CLI commands, logging backends, secret backends, notifications, and configuration sections. |
| **Versioning** | Independent semver per provider. Provider declares `apache-airflow>=2.x` as a dependency — the minimum core version it needs. |
| **Ecosystem** | 80+ community-maintained providers. Third-party providers have **identical capabilities** to community ones. |

**What we should steal:**
- Entry-point-based discovery (zero config for users)  
- `get_provider_info()` metadata function (enables `fluid providers` to show rich info)
- Independent packaging with semver
- Core extensions beyond just "plan/apply" (custom CLI commands, validators, codegen backends)

### 2.2 Terraform — Plugin Protocol + Registry

**Architecture:** Terraform takes the most extreme approach — plugins are **separate binaries** that communicate with core over gRPC. Each provider runs as its own OS process.

**Key Design Decisions:**
| Aspect | How Terraform Does It |
|---|---|
| **Discovery** | Terraform Registry (`registry.terraform.io`) + `terraform init` downloads provider binaries. Local overrides via `dev_overrides` in `.terraformrc`. |
| **Interface** | A formal gRPC Protocol (currently v6). Providers implement `GetSchema`, `ValidateResourceConfig`, `PlanResourceChange`, `ApplyResourceChange`, `ReadResource`, `ImportResourceState`. |
| **SDK** | `terraform-plugin-framework` (Go SDK) handles all the gRPC + protocol boilerplate. Provider authors implement typed resource schemas and CRUD methods. |
| **Naming** | Providers follow `hashicorp/aws`, `digitalocean/digitalocean` — namespaced by organization. |
| **Schema** | Providers declare their full schema (resource types, attributes, types, validation) to core. Core validates user configs **before** calling the provider. |
| **Versioning** | Providers semver independently. `required_providers` block in HCL pins versions. Core negotiates protocol version with the plugin. |
| **Ecosystem** | 4,000+ providers in the registry. Most created by the community/vendors. |

**What we should steal:**
- **Provider-declared schemas** — providers publish what resources/actions they support. Core validates contracts against provider capabilities *before* calling `plan()`.
- **SDK that hides boilerplate** — `terraform-plugin-framework` means providers only write business logic. Registration, protocol negotiation, and testing infrastructure are handled.
- **Namespaced providers** — `dustlabs/aws`, `community/databricks`. Prevents naming collisions as the ecosystem grows.
- **Protocol version negotiation** — future-proofs provider ↔ core compatibility.

**What we should NOT steal:**
- Separate-binary/gRPC architecture. Way too heavy for a Python CLI. Python entry points give us the same isolation benefit without the operational cost.

### 2.3 dbt — Adapter SDK + Hub

**Architecture:** dbt uses a Python class hierarchy for adapters. Each adapter (`dbt-snowflake`, `dbt-bigquery`, `dbt-redshift`) is a separate PyPI package that subclasses `dbt-adapters` base classes.

**Key Design Decisions:**
| Aspect | How dbt Does It |
|---|---|
| **Discovery** | Entry points + `profiles.yml` declares which adapter to use. `dbt-core` resolves the adapter by name. |
| **Interface** | Three base classes: `BaseAdapter` (high-level operations), `BaseConnectionManager` (connections), `BaseCredentials` (auth). Plus Jinja macros for SQL generation. |
| **SDK** | `dbt-adapters` is a standalone package (extracted from core). Contains base classes, test utilities, and the adapter protocol. |
| **Testing** | `dbt-tests-adapter` package provides a standard test suite. Adapter authors subclass and run it — conformance testing out of the box. |
| **Hub** | `hub.getdbt.com` indexes community packages with metadata, compatibility, and Fusion badges. |
| **Hierarchy** | Adapters can inherit from other adapters (e.g., `dbt-redshift` extends `dbt-postgres`). |
| **Ecosystem** | 30+ community adapters. Clear "Trusted" vs "Community" tier system. |

**What we should steal:**
- **Separate SDK package** (`fluid-provider-sdk`) containing only base classes + test utilities  
- **Conformance test suite** — a `ProviderTestSuite` that provider authors run against their implementation
- **Adapter inheritance** — e.g., a `DatabricksProvider` could extend `AwsProvider` to reuse S3 logic
- **Hub with quality tiers** — Verified / Community / Experimental

### 2.4 Pytest — Entry-Point Plugins

**Architecture:** The simplest and most Pythonic approach. Plugins are discovered via `setuptools` entry points under the `pytest11` group. No SDK, no separate binary — just a Python module that implements known hooks.

**Key Design Decisions:**
| Aspect | How pytest Does It |
|---|---|
| **Discovery** | `importlib.metadata.entry_points(group="pytest11")`. Plus `conftest.py` for local plugins and `-p` CLI flag for explicit loading. |
| **Interface** | Hook-based via `pluggy`. Plugins implement functions matching hook specifications (e.g., `pytest_collection_modifyitems`, `pytest_runtest_protocol`). |
| **Registration** | Automatic on import. The entry point module just needs to define functions with matching signatures. |
| **Ordering** | Hooks support `tryfirst`/`trylast` for ordering, and `hookimpl(wrapper=True)` for wrapping behavior. |
| **Ecosystem** | 1,500+ plugins on PyPI discovered by naming convention (`pytest-*`). |

**What we should steal:**
- **Zero-boilerplate discovery** — `pip install fluid-provider-X` → it works
- **Naming convention** (`fluid-provider-*`) for ecosystem discoverability on PyPI
- **pluggy-style hooks** for lifecycle events (pre-plan, post-apply, etc.) if we go beyond basic plan/apply
- **`-p` flag equivalent** — `fluid build --provider my_custom.provider` for development

### 2.5 Stevedore (OpenStack) — Driver Pattern

**Architecture:** Stevedore is a Python library built specifically for managing plugins via `setuptools` entry points. It's used by OpenStack, Cliff, and many other projects.

**Key Design Decisions:**
| Aspect | How Stevedore Does It |
|---|---|
| **Discovery** | `DriverManager`, `NamedExtensionManager`, `EnabledExtensionManager` — each wraps `importlib.metadata.entry_points()` with different selection strategies. |
| **Loading** | Lazy or eager. `DriverManager` loads one plugin by name. `ExtensionManager` loads all in a namespace. |
| **Error handling** | Configurable: `on_load_failure_callback` per-plugin, `propagate_map_exceptions`, `warn_on_missing_entrypoint`. |
| **Interface** | No opinion — plugins are whatever the host application expects. Stevedore is the discovery/loading layer only. |

**What we should steal:**
- **Using stevedore directly** (or its pattern) instead of hand-rolling entry-point scanning
- **`DriverManager` pattern** — load exactly one provider by name, fail fast with a clear error
- **Error handling callbacks** — graceful degradation when a provider fails to load (we partly have this already)

### 2.6 Key Lessons Across All Projects

| Lesson | Evidence | Applicability to FLUID |
|---|---|---|
| **Discovery via entry points is table stakes** | Airflow, dbt, pytest, Stevedore all use `importlib.metadata.entry_points()` | **Critical** — we declared them but don't read them |
| **Separate SDK package lowers the barrier** | dbt (`dbt-adapters`), Terraform (`terraform-plugin-framework`) | **High** — providers shouldn't need to install the full CLI |
| **Conformance tests create quality** | dbt (`dbt-tests-adapter`), Terraform (acceptance test framework) | **High** — we have zero provider tests today |
| **Schema declaration by providers enables validation** | Terraform's `GetSchema` RPC, Airflow's `get_provider_info()` | **Medium** — would eliminate contract-parsing duplication |
| **Independent versioning is essential** | All projects — provider and core version independently | **High** — enables ecosystem velocity |
| **Naming conventions drive discoverability** | `pytest-*`, `apache-airflow-providers-*`, `dbt-*` | **Quick win** — `fluid-provider-*` convention |
| **The scaffolder is the gateway drug** | `terraform provider scaffold`, dbt's adapter creation guide | **High** — `fluid provider init` gets people started in minutes |
| **You need a hub/index before you need a marketplace** | dbt Hub, Terraform Registry, even just a GitHub awesome-list | **Deferred** — useful once there are 5+ third-party providers |

---

## 3. Current State of FLUID Providers

### 3.1 Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  CLI Commands: fluid plan | fluid apply | fluid build            │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ _common.py                                                 │  │
│  │   resolve_provider_from_contract(contract)                 │  │
│  │   build_provider(name, project, region, logger)            │  │
│  └──────────────────────┬─────────────────────────────────────┘  │
│                         │                                        │
│  ┌──────────────────────▼─────────────────────────────────────┐  │
│  │ providers/__init__.py — Registry                           │  │
│  │   PROVIDERS: Dict[str, Any]      (registry #1)             │  │
│  │   discover_providers()           pkgutil scan               │  │
│  │   register_provider(name, cls)                              │  │
│  ├─────────────────────────────────────────────────────────────┤  │
│  │ providers/base.py — BaseProvider + Registry #2 (duplicate!) │  │
│  │   PROVIDERS: Dict[str, Type[BaseProvider]]  (registry #2)  │  │
│  │   register_provider(name, cls)  ← different signature!     │  │
│  │   cross-syncs with registry #1 via try/except import       │  │
│  └──────────────────────┬─────────────────────────────────────┘  │
│                         │                                        │
│  ┌──────────────────────▼─────────────────────────────────────┐  │
│  │ Built-in Providers (in-tree only)                          │  │
│  │                                                             │  │
│  │  aws/         gcp/         snowflake/      local/          │  │
│  │  ├─provider   ├─provider   ├─provider_enh  ├─local.py      │  │
│  │  ├─plan/      ├─plan/      ├─plan/         ├─planner.py    │  │
│  │  ├─actions/   ├─actions/   ├─actions/      └─(no actions)  │  │
│  │  ├─codegen/   ├─codegen/   ├─codegen/                      │  │
│  │  └─util/      └─util/      └─util/                         │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ ALSO: forge/core/interfaces.py — InfrastructureProvider     │  │
│  │   Completely separate ABC for `fluid forge` scaffolding     │  │
│  │   Different methods, different registry, different purpose  │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ ALSO: cli/plugins.py — PluginManager + ProviderPlugin       │  │
│  │   File-based plugin system (~/.fluid/plugins/)              │  │
│  │   ProviderPlugin ABC (different interface from BaseProvider)│  │
│  │   ⚠️ NOT wired into provider registry — dead code path     │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ pyproject.toml:                                              │  │
│  │   [project.entry-points."fluid_build.providers"]            │  │
│  │   local = "fluid_build.providers.local:LocalProvider"       │  │
│  │   gcp = "fluid_build.providers.gcp:GcpProvider"             │  │
│  │   ⚠️ DECLARED but NEVER CONSUMED at runtime                │  │
│  └─────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 What Works Well

- **Clean `BaseProvider` ABC** — only 2 abstract methods (`plan`, `apply`) + optional overrides. Low surface area.
- **Thread-safe registry** with 4 auto-registration strategies (explicit call, PROVIDERS dict, NAME+Provider, single-subclass detection).
- **Structured result types** — `ApplyResult`, `PlanAction`, `ProviderError` normalize outputs.
- **Entry points already declared** in `pyproject.toml` under `fluid_build.providers`.
- **`FLUID_PROVIDERS` env var** already constrains which providers get imported — useful for CI.
- **Graceful degradation** — GCP creates a stub provider if imports fail.

### 3.3 Critical Gaps

| # | Gap | Impact | OSS Precedent |
|---|---|---|---|
| **G1** | **Entry points not consumed** — `discover_providers()` only does `pkgutil.iter_modules` on the in-tree package | A `pip install fluid-provider-databricks` would be invisible to the CLI | Airflow, dbt, pytest all consume entry points |
| **G2** | **Triple registry** — `providers/__init__.py` (canonical, `Dict[str, Any]`), `providers/base.py` (strict, `Dict[str, Type[BaseProvider]]`), and `forge/__init__.py` (`ProviderRegistry` instance) each maintain separate provider registries. `base.py` has a one-way sync shim via try/except; the reverse doesn't exist. Name normalization differs: `__init__.py` normalizes (`-` → `_`, lowercase), `base.py` does not. | Registration race conditions, silent divergence, `datamesh-manager` stored as `datamesh_manager` in one registry and `datamesh-manager` in another | All OSS projects have one canonical registry |
| **G3** | **No shared contract parsing** — each provider re-implements extraction of `exposes[]`, `binding`, `consumes[]`, `build` from the raw dict | Duplicated effort, inconsistent behavior, high barrier for new providers | dbt base adapter provides shared schema access |
| **G4** | **No action schema** — action dicts from `plan()` have completely different key sets per provider (`op`, `id`, `phase`, `payload` — all inconsistent) | No generic dry-run, explain, or validation tooling. Third parties must reverse-engineer. | Terraform's `PlanResourceChange` returns a typed schema |
| **G5** | **`plan_actions()` signatures differ** — AWS(`account_id`), GCP(`project`), Snowflake(`account,warehouse,database,schema`), Local(`project?,region?`) | `build_provider()` can't uniformly construct providers | All OSS projects enforce a consistent constructor |
| **G6** | **Two competing provider ABCs** — `BaseProvider` (build) vs `InfrastructureProvider` (forge) with no bridge | Confusing for third-party authors — which one to implement? | dbt has one `BaseAdapter`. Terraform has one Provider protocol. |
| **G7** | **Plugin system (`cli/plugins.py`) is disconnected** — `ProviderPlugin` never feeds into the `PROVIDERS` registry or `build_provider()` | 859 lines of dead code for provider extensibility | N/A — should be unified or removed |
| **G8** | **No conformance tests** — zero runnable provider tests in `tests/providers/` | No way to verify a provider works. No quality bar. | dbt ships `dbt-tests-adapter`. Terraform ships acceptance tests. |
| **G9** | **No SDK package** — base classes live in the monolith. Provider authors must `pip install fluid-forge` with all deps. | Heavyweight dependency for provider development | dbt extracted `dbt-adapters`. Terraform has `terraform-plugin-framework`. |
| **G10** | **`LocalProvider` duck-types** instead of subclassing `BaseProvider`. It's a `@dataclass` with `apply(actions, plan, out, **kwargs)` and `render(plan, out, **kwargs)` — **different signatures** from `BaseProvider.apply(actions)` and `BaseProvider.render(src, *, out, fmt)`. The comment in `local/__init__.py` says "must have class LocalProvider(BaseProvider)" — aspirational, not reality. | Bad precedent. Migration requires sig changes, not just adding a base class. | All OSS projects enforce the base class. |
| **G11** | **`build_provider()` bypasses name normalization** — `cli/_common.py` does `registry.PROVIDERS.get(name)` with the raw user string, not via `get_provider()` which lower-cases and normalizes. `fluid plan --provider GCP` silently fails because `"GCP" != "gcp"`. | User-facing bug, provider not found on case mismatch | All OSS projects normalize names. |
| **G12** | **`build_provider_instance()` is dead code** — exists in both `providers/__init__.py` (L349) and `providers/base.py` (L326) but has **zero callers** anywhere in the codebase | Code rot, confusion for contributors | Should be removed or made canonical |

### 3.4 Code Evidence

**Contract parsing is duplicated 4 different ways:**

```python
# AWS (plan/planner.py) — reads binding.platform, binding.location
binding = exposure.get("binding") or {}
platform = binding.get("platform", "").lower()
location = binding.get("location") or {}
database = location.get("database") or binding.get("database")  # flat fallback

# GCP (plan/planner.py) — supports TWO contract formats (old + new)
if binding:
    format_type = binding.get("format")
    properties = binding.get("location", {})
else:
    format_type = location.get("format")
    properties = location.get("properties", {})

# Snowflake (plan/planner.py) — reads TOP-LEVEL binding, not per-exposure
binding = contract.get("binding", {})   # top-level!
location = binding.get("location", {})

# Local (planner.py) — imports util.contract helpers (only one that does!)
from fluid_build.util.contract import get_builds, get_consumes, get_exposes
```

**Action formats are incompatible:**

```python
# AWS action
{"op": "glue.ensure_database", "id": "database_mydb", "database": "mydb",
 "description": "...", "location": "s3://...", "tags": {...}}

# GCP action
{"op": "bq.ensure_dataset", "id": "dataset_myds", "project": "proj",
 "dataset": "myds", "location": "US", "labels": {...}}

# Snowflake action
{"id": "database_MYDB", "op": "sf.database.ensure", "phase": "infrastructure",
 "account": "acct", "database": "MYDB", "comment": "..."}

# Local action
{"op": "load_data", "resource_type": "table", "resource_id": "prices",
 "depends_on": [], "payload": {"path": "data.csv", ...}}
```

**The dual registry cross-syncs with a try/except:**

```python
# providers/base.py register_provider() — syncs with providers/__init__.py
try:
    import fluid_build.providers as _canonical
    if hasattr(_canonical, "PROVIDERS") and _canonical.PROVIDERS is not PROVIDERS:
        _canonical.PROVIDERS[name] = cls
except Exception:
    pass  # graceful — canonical registry may not be ready yet
```

---

## Breaking Changes Analysis

> Based on complete audit of every `register_provider` call, every `PROVIDERS` read, every `build_provider` call, every provider constructor, and all contract parsing paths.

### Phase 0 Breaking Changes

| Change | Severity | Who's Affected | Mitigation |
|---|---|---|---|
| **Consolidate triple registry → single PROVIDERS dict** | **HIGH** for internal contributors | Anyone importing `PROVIDERS` or `register_provider` from `providers/base.py` directly. Currently: `from fluid_build.providers.base import register_provider` has a different signature (no `override`/`source` params, requires `Type[BaseProvider]`). After: it becomes a re-export of `__init__.py`'s version. | Keep `base.py`'s `register_provider` as a thin wrapper that calls `__init__.py`'s version. Emit `DeprecationWarning` for direct use. Nobody in production code imports from `base.py` — only the docstring example does. |
| **`LocalProvider` drops `@dataclass`** | **MEDIUM** for anyone constructing LocalProvider with positional args | `LocalProvider("myproject", "us-east1")` → must use `LocalProvider(project="myproject", region="us-east1")`. Positional args break because `BaseProvider.__init__` uses keyword-only (`*`). | Survey all `LocalProvider(...)` call sites. `build_provider()` already uses keyword args. The `persist` field moves to `self.extra["persist"]` or stays as a class attribute. |
| **`LocalProvider.apply()` signature change** | **HIGH** for anyone calling `provider.apply(actions, plan, out)` | Current: `apply(actions=None, plan=None, out=None, **kwargs)`. BaseProvider requires: `apply(actions: Iterable[Mapping]) -> ApplyResult`. Must reconcile — widen BaseProvider or narrow Local. | **Recommended:** Keep `LocalProvider.apply()` accepting extra kwargs but make `plan`/`out` internal. Refactor callers in `cli/apply.py` that pass `plan=` and `out=` to LocalProvider. Local uses `ApplyResult` internally anyway so return type is fixable. |
| **`LocalProvider.render()` signature change** | **MEDIUM** | Current: `render(plan=None, out=None, **kwargs)`. BaseProvider: `render(src, *, out, fmt)`. | LocalProvider's `render()` just delegates to `apply()`. Can be renamed to `_render_internal()` with a proper `render(src, *, out, fmt)` wrapper. |
| **`LocalProvider.logger` behavior** | **LOW** | BaseProvider wraps logger via `_mk_logger()` — it's never `None`. Local's `_log_info/warn/error` all guard `if self.logger:`. | Guards become dead code (harmless). Logger will now default to a StreamHandler instead of silent skip. May produce unexpected output in tests. Add `NullHandler` for test environments. |
| **Entry-point provider loading order** | **LOW** | Entry points load before pkgutil discovery. If an external package declares `gcp` as an entry point, it would conflict with the built-in. | `register_provider(override=False)` is first-write-wins by default. Built-in pkgutil scan runs second, so built-ins still win. Document that overriding built-ins requires `fluid_build.providers.override` entry-point group or explicit `--provider-path`. |
| **`build_provider()` name normalization fix** | **LOW-positive** (bug fix) | Anyone passing `--provider GCP` (uppercase) today gets "provider not found". After: it works. | This is a bug fix, not a breaking change. Add `.lower().strip()` to `build_provider()` before dict lookup. |

### Phase 1 Breaking Changes

| Change | Severity | Who's Affected | Mitigation |
|---|---|---|---|
| **`BaseProvider` moves to `fluid-provider-sdk`** | **MEDIUM** for existing provider code | Any `from fluid_build.providers.base import BaseProvider` import path. | `providers/base.py` becomes a re-export shim: `from fluid_provider_sdk import BaseProvider`. Import paths continue to work unchanged. |
| **New SDK dependency** | **LOW** | `fluid-forge` gains a new dependency: `fluid-provider-sdk>=0.1.0`. | Zero external deps in SDK. Size impact: ~50KB. Pure Python, no native deps. |
| **`ProviderMetadata` required** | **NONE** (optional) | `get_provider_info()` returns a default stub. Providers can override when ready. | Default implementation returns `ProviderMetadata(name=cls.name, ...)`. |

### Phase 2 Breaking Changes

| Change | Severity | Who's Affected | Mitigation |
|---|---|---|---|
| **`ContractHelper` does NOT replace raw dicts** | **NONE** | `plan(contract: Mapping)` still receives the raw dict. `ContractHelper` is opt-in. | Providers adopt at their own pace. |
| **`ProviderAction` typed schema** | **NONE** | Actions can still be `Dict[str, Any]`. `ProviderAction.to_dict()` bridges to the old format. | Opt-in. `validate_actions()` is advisory. |

### Phase 3-5 Breaking Changes

| Change | Severity | Notes |
|---|---|---|
| **`cli/plugins.py` `ProviderPlugin` deprecation** | **LOW** | Nobody implements this interface externally. Will be removed in 1.0. |
| **Hook system** | **NONE** | Hooks have default no-op implementations. |
| **Marketplace** | **NONE** | Additive. |

### Summary: Total Breaking Changes

| Phase | Breaking Changes | Non-Breaking Changes | Risk Level |
|---|---|---|---|
| **Phase 0** | 4 (registry consolidation, LocalProvider migration, apply/render sigs, logger behavior) | 3 (entry-point activation, build_provider fix, docs) | **Medium** — all internal, no third-party API breaks |
| **Phase 1** | 1 (import path shim needed) | 3 (SDK package, metadata, shim) | **Low** — backward-compatible shims |
| **Phase 2** | 0 | 3 (ContractHelper, ProviderAction, validation) | **None** — fully additive |
| **Phase 3-5** | 1 (ProviderPlugin deprecation) | Everything else | **None** — additive |

**Bottom line:** All breaking changes are concentrated in Phase 0 and are internal-only (no external API surface exists today). If Phase 0 is done correctly with the mitigations above, all subsequent phases are non-breaking.

---

## 4. Options Analysis

### Option A: Entry-Point Activation (Quick Win)

**What:** Add ~15 lines to `discover_providers()` to scan `importlib.metadata.entry_points(group="fluid_build.providers")`.

**Scope:**
- Modify `providers/__init__.py` to consume entry points
- Fix the dual-registry problem (single source of truth)
- Document the entry-point convention for third parties
- Add PyPI naming convention: `fluid-provider-*`

**Effort:** 1-2 days

**Pros:**
- Immediately unblocks `pip install fluid-provider-X`
- Standard Python pattern (setuptools/importlib.metadata)
- Zero breaking changes to existing providers
- Already partially implemented (entry points declared in `pyproject.toml`)

**Cons:**
- Provider authors still depend on the full `fluid-forge` package for `BaseProvider`
- No shared contract parsing — still duplicated
- No conformance tests — quality is unverifiable
- No action schema — no interop between plan/apply

**Best for:** Getting to "something works" immediately while planning the deeper refactor.

---

### Option B: Provider SDK Package

**What:** Extract a `fluid-provider-sdk` package containing only the interfaces, types, contract helpers, and test utilities.

**Scope:**

```
fluid-provider-sdk/
  pyproject.toml
  src/fluid_provider_sdk/
    __init__.py
    base.py          # BaseProvider ABC (moved from providers/base.py)
    types.py         # ApplyResult, PlanAction, ProviderError
    contract.py      # ContractHelper — shared parsing (NEW)
    actions.py       # Typed action schema + validation (NEW)
    capabilities.py  # ProviderCapabilities dataclass (NEW)
    testing/
      __init__.py
      harness.py     # ProviderTestHarness (NEW)
      fixtures.py    # Sample contracts for testing (NEW)
    version.py       # SDK version + compat range
```

**What `ContractHelper` provides:**
```python
class ContractHelper:
    """Universal contract parser — so providers don't re-invent this."""
    def __init__(self, contract: dict): ...

    @property
    def fluid_version(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def domain(self) -> str: ...
    @property
    def metadata(self) -> MetadataSpec: ...

    def exposes(self) -> List[ExposeSpec]: ...
    def consumes(self) -> List[ConsumeSpec]: ...
    def build_config(self) -> Optional[BuildSpec]: ...
    def provider_actions(self) -> List[ProviderActionSpec]: ...
    def security(self) -> Optional[SecuritySpec]: ...
    def tags(self) -> Dict[str, str]: ...
```

**What `ProviderTestHarness` provides:**
```python
class ProviderTestHarness:
    """Conformance test suite for provider implementations."""
    def __init__(self, provider_class: Type[BaseProvider], **init_kwargs): ...

    def test_subclasses_base_provider(self): ...
    def test_name_is_valid(self): ...
    def test_capabilities_returns_mapping(self): ...
    def test_plan_returns_list_of_actions(self, contract: dict): ...
    def test_plan_actions_have_required_keys(self, contract: dict): ...
    def test_apply_returns_apply_result(self, actions: list): ...
    def test_apply_is_idempotent(self, actions: list): ...
    def test_registration_works(self): ...
    def run_all(self, contract: dict) -> TestReport: ...
```

**Effort:** 2-3 weeks

**Pros:**
- Provider authors install a lightweight package (~0 deps), not the full CLI
- Shared contract parsing eliminates duplication
- Conformance tests create a quality bar
- Clean separation of concerns

**Cons:**
- Two packages to maintain + version compatibility
- Existing providers need migration (can be gradual)
- Still no action schema enforcement without typed actions
- No marketplace / discoverability

---

### Option C: Full Plugin Protocol + Marketplace

**What:** Formalize the provider contract as a `Protocol` (structural typing), add lifecycle hooks, capability negotiation, and a provider index/marketplace.

**Scope:**

```python
@runtime_checkable
class FluidProvider(Protocol):
    """Structural protocol — no inheritance required."""
    name: str
    sdk_version: str

    def plan(self, contract: Mapping[str, Any]) -> List[ProviderAction]: ...
    def apply(self, actions: Sequence[ProviderAction]) -> ApplyResult: ...
    def capabilities(self) -> ProviderCapabilities: ...

class FluidProviderWithHooks(FluidProvider, Protocol):
    """Extended protocol with lifecycle hooks."""
    def pre_plan(self, contract: dict) -> dict: ...
    def post_plan(self, actions: list) -> list: ...
    def pre_apply(self, actions: list) -> list: ...
    def post_apply(self, result: ApplyResult) -> None: ...
    def on_error(self, error: Exception, context: dict) -> ErrorAction: ...

@dataclass
class ProviderCapabilities:
    planning: bool = True
    apply: bool = True
    dry_run: bool = False
    rollback: bool = False
    cost_estimation: bool = False
    schema_validation: bool = False
    streaming: bool = False
    lineage: bool = False
    codegen: bool = False
    # Minimum CLI version this provider requires
    min_cli_version: str = "0.7.0"
    # Provider-declared resource types it can manage
    supported_resource_types: List[str] = field(default_factory=list)
```

**Marketplace CLI:**
```
$ fluid provider search databricks
NAME                         VERSION  AUTHOR          DOWNLOADS
fluid-provider-databricks     1.2.0   databricks-inc  12,450
fluid-provider-unity-catalog  0.9.1   unitycatalog    3,200

$ fluid provider install fluid-provider-databricks
$ fluid provider info databricks
```

**Effort:** 2-3 months

**Pros:**
- Protocol-based — no inheritance coupling (duck-typing done right)
- Lifecycle hooks enable advanced integrations (cost estimation, lineage, governance)
- Marketplace creates discoverability and community incentive
- Capability negotiation future-proofs against feature additions

**Cons:**
- Significant ongoing maintenance
- Marketplace infrastructure cost
- Risk of over-engineering if adoption is still low
- Protocol approach makes conformance testing harder (no `isinstance` checks)

---

### Option D: Hybrid (Recommended)

**What:** Staged delivery that starts with the quick win and progressively adds SDK, contract helpers, testing, and marketplace — each phase delivers standalone value.

**Timeline:**

```
Week 1      Week 2-3    Week 4-5    Week 6-7   Week 8-10    Month 4+
┌─────┐    ┌────────┐  ┌────────┐  ┌────────┐  ┌─────────┐  ┌──────────┐
│Ph 0 │───►│ Ph 1   │─►│ Ph 2   │─►│ Ph 3   │─►│  Ph 4   │─►│  Ph 5    │
│Entry│    │  SDK   │  │Contract│  │  Test  │  │Lifecycle│  │Market-   │
│Point│    │Extract │  │Helpers │  │Harness │  │Hooks +  │  │place     │
│     │    │        │  │+Action │  │+Scaff  │  │Caps v2  │  │Index     │
└─────┘    └────────┘  └────────┘  └────────┘  └─────────┘  └──────────┘
  1d         1-2w        1-2w        1-2w         2w           ongoing
```

**Decision criteria for advancing phases:**
| Phase | Gate to proceed |
|---|---|
| 0 → 1 | At least 1 external provider attempt / interest signal |
| 1 → 2 | SDK published to PyPI; existing providers compiling against it |
| 2 → 3 | ContractHelper used by ≥2 providers; action schema validated |
| 3 → 4 | ≥1 third-party provider passes conformance suite |
| 4 → 5 | ≥5 providers exist (built-in + community) |

---

## 5. Detailed Design — Option D (Hybrid)

### Phase 0: Unblock External Providers (Week 1)

**Goal:** Make `pip install fluid-provider-X` work with zero changes to the external package beyond declaring an entry point.

**Changes:**

#### 5.0.1 — Activate entry-point discovery

Add to `providers/__init__.py` `discover_providers()`:

```python
def _discover_entrypoints(logger: Optional[logging.Logger] = None) -> None:
    """Discover providers registered via setuptools entry points."""
    try:
        if sys.version_info >= (3, 12):
            from importlib.metadata import entry_points
            eps = entry_points(group="fluid_build.providers")
        else:
            from importlib.metadata import entry_points
            all_eps = entry_points()
            eps = all_eps.get("fluid_build.providers", [])

        for ep in eps:
            try:
                provider_cls = ep.load()
                register_provider(
                    ep.name, provider_cls,
                    override=False, logger=logger,
                    source="entrypoint"
                )
            except Exception as exc:
                _add_discovery_error("entrypoint", ep.name, exc)
                _safe_log(logger, logging.WARNING,
                          "entrypoint_load_failed",
                          name=ep.name, error=str(exc))
    except Exception as exc:
        _safe_log(logger, logging.DEBUG,
                  "entrypoint_discovery_unavailable",
                  error=str(exc))
```

Wire it into `discover_providers()` as step 0 (before `pkgutil` scan).

#### 5.0.2 — Consolidate dual registry

Eliminate `providers/base.py`'s separate `PROVIDERS` dict. Make `providers/__init__.py` the single source of truth. The `base.py` `register_provider` becomes a re-export.

#### 5.0.3 — Document the convention

Add to README + docs:

```markdown
## Creating a Third-Party Provider

1. Create a Python package (e.g., `fluid-provider-databricks`)
2. Subclass `BaseProvider` from `fluid_build.providers.base`
3. Declare an entry point in your `pyproject.toml`:

   ```toml
   [project.entry-points."fluid_build.providers"]
   databricks = "fluid_provider_databricks:DatabricksProvider"
   ```

4. `pip install fluid-provider-databricks` — done. `fluid providers` will list it.
```

#### 5.0.4 — Fix `LocalProvider` to subclass `BaseProvider`

This is more complex than originally stated. Deep review reveals:

**Current state:**
```python
@dataclass
class LocalProvider:                      # No base class
    project: Optional[str] = None         # Positional OK
    region: Optional[str] = None
    logger: Optional[Any] = None          # Raw — may be None
    persist: bool = False                 # No equivalent in BaseProvider
```

**Signature mismatches:**
```python
# BaseProvider.apply() — what it should be
def apply(self, actions: Iterable[Mapping]) -> ApplyResult

# LocalProvider.apply() — what it is today
def apply(self, actions=None, plan=None, out=None, **kwargs) -> Dict

# BaseProvider.render()
def render(self, src, *, out, fmt) -> None

# LocalProvider.render()
def render(self, plan=None, out=None, **kwargs) -> None
```

**Step-by-step migration:**

| Step | Code Change | Risk |
|---|---|---|
| 1. Drop `@dataclass` | Convert to regular class with `__init__` | Medium — breaks positional construction |
| 2. Add `class LocalProvider(BaseProvider)` | Add base class | Low |
| 3. Write explicit `__init__` | `def __init__(self, *, project=None, region=None, logger=None, persist=False, **kwargs): super().__init__(project=project, region=region, logger=logger, **kwargs); self.persist = persist` | Low |
| 4. Set `name = "local"` | Class attribute | None |
| 5. Refactor `apply()` | Make `plan`/`out` internal state or accept via `**kwargs`. Ensure it returns `ApplyResult` not `Dict`. | High — need to check all callers |
| 6. Refactor `render()` | Wrap as `render(src=None, *, out=None, fmt=None, **kwargs)` → delegates to internal logic | Medium |
| 7. Remove `if self.logger:` guards | After BaseProvider wraps logger via `_mk_logger()`, it's never None | Low — guards become no-ops |
| 8. Verify all callers | `build_provider()` uses keyword args (safe). `cli/apply.py` passes `plan=`/`out=` to apply — needs update | Medium |

**The planner is already clean** — `planner.py` is a pure function module that doesn't reference LocalProvider at all. Zero risk there.

**Deliverables:**
- [x] Entry-point discovery in `discover_providers()`
- [x] Single canonical `PROVIDERS` registry (eliminate `base.py` and `forge/__init__.py` duplicates)
- [x] `LocalProvider` subclasses `BaseProvider` with proper signatures
- [x] `build_provider()` normalizes name case before lookup
- [x] Remove dead `build_provider_instance()` from both registries
- [x] Documentation for third-party providers
- [x] `fluid providers` shows source (builtin vs entrypoint)

---

### Phase 1: Provider SDK Extraction (Weeks 2-4)

**Goal:** Third-party provider authors install `fluid-provider-sdk` (~0 deps) instead of `fluid-forge` (40+ deps).

**Package structure:**

```
fluid-provider-sdk/
  pyproject.toml          # name = "fluid-provider-sdk", version = "0.1.0"
  src/fluid_provider_sdk/
    __init__.py            # re-exports: BaseProvider, ApplyResult, etc.
    base.py                # BaseProvider ABC
    types.py               # ApplyResult, PlanAction, ProviderError, ProviderInternalError
    capabilities.py        # ProviderCapabilities dataclass
    metadata.py            # ProviderMetadata — for `get_provider_info()`
    version.py             # SDK_VERSION, MIN_CLI_VERSION, MAX_CLI_VERSION
    py.typed               # PEP 561 marker
```

**Minimal `pyproject.toml`:**

```toml
[project]
name = "fluid-provider-sdk"
version = "0.1.0"
description = "SDK for building FLUID data product providers"
requires-python = ">=3.9"
dependencies = []  # Zero dependencies!

[project.urls]
Documentation = "https://agenticstiger.github.io/forge_docs/providers/"
Repository = "https://github.com/Agenticstiger/fluid-provider-sdk"
```

**Migration path for `fluid-forge`:**
```python
# fluid_build/providers/base.py becomes:
try:
    from fluid_provider_sdk import BaseProvider, ApplyResult, ProviderError, ...
except ImportError:
    # Fallback: keep local definitions for backward compatibility
    ...
```

**Provider metadata (Airflow-inspired):**

```python
@dataclass
class ProviderMetadata:
    """Metadata exposed to CLI and marketplace."""
    name: str                          # "databricks"
    display_name: str                  # "Databricks"
    description: str                   # One-line description
    version: str                       # Provider package version
    sdk_version: str                   # SDK version it was built with
    author: str                        # "Databricks, Inc."
    url: Optional[str] = None          # Homepage
    license: Optional[str] = None      # "Apache-2.0"
    supported_platforms: List[str] = field(default_factory=list)  # ["databricks", "unity_catalog"]
    tags: List[str] = field(default_factory=list)      # ["lakehouse", "spark", "delta"]

class BaseProvider(ABC):
    # ... existing methods ...

    @classmethod
    def get_provider_info(cls) -> ProviderMetadata:
        """Return provider metadata for registry/marketplace."""
        return ProviderMetadata(
            name=cls.name,
            display_name=cls.name.title(),
            description="",
            version="0.0.0",
            sdk_version=SDK_VERSION,
            author="Unknown",
        )
```

**Deliverables:**
- [x] `fluid-provider-sdk` package published (installed editable at `/fluid-provider-sdk/`)
- [x] `fluid-forge` imports from `fluid-provider-sdk` (with inline fallback)
- [x] Backward-compatible import paths preserved (`fluid_build.providers.base` re-exports)
- [x] `get_provider_info()` on BaseProvider + all 4 built-in providers
- [x] `fluid providers --debug` shows metadata from `get_provider_info()`

---

### Phase 2: Contract Helpers + Action Schema (Weeks 3-5)

**Goal:** Eliminate contract-parsing duplication. Standardize action format.

**Add to SDK:**

> **Amendment (v2):** Deep review revealed 5 fundamentally different contract parsing patterns. The `ContractHelper` must account for all of them:
>
> | Provider | Platform routing key | Binding source | Build key | Security key | Location nesting |
> |---|---|---|---|---|---|
> | **AWS** | `binding.platform` ("aws"/"s3"/"redshift") | expose-level `binding` only | `builds[]` (array) | `metadata.policies` | `binding.location.<key>` + flat `binding.<key>` fallback |
> | **GCP** | `binding.format` ("bigquery_table"/"gcs_bucket") | expose-level `binding` + old `location` | `builds[]` (array) | `metadata.policies` | `binding.location.<key>` only (new); `location.properties.<key>` (old) |
> | **Snowflake** | `binding.format` ("snowflake_table") | **top-level** `contract.binding` for infra + expose-level | singular `build` (object!) | `security.access_control.grants` | `binding.location.<key>` only |
> | **Local** | none (file-oriented) | no binding — uses `location.path` or string | both `builds`/`build` via adapter | none | `location` as dict or plain string |
> | **ODPS** | via `get_expose_binding()` adapter | expose-level via adapter | both `builds`/`build` | `accessPolicy` | via adapter |

```python
# fluid_provider_sdk/contract.py

@dataclass(frozen=True)
class ExposeSpec:
    """Parsed representation of a contract `exposes[]` entry."""
    id: str
    type: str                              # "table", "view", "topic", etc.
    platform: Optional[str]                # "gcp", "aws", "snowflake" (from binding.platform)
    format: Optional[str]                  # "bigquery_table", "snowflake_table" (from binding.format)
    database: Optional[str]
    schema_name: Optional[str]
    table: Optional[str]
    bucket: Optional[str]
    path: Optional[str]
    dataset: Optional[str]                 # GCP BigQuery
    project: Optional[str]                 # GCP project override
    region: Optional[str]                  # GCP/Snowflake region
    topic: Optional[str]                   # GCP Pub/Sub
    cluster: Optional[str]                 # AWS Redshift
    view: Optional[str]                    # AWS Athena / GCP BQ view
    query: Optional[str]                   # View SQL
    location: Dict[str, Any]              # raw binding.location dict
    columns: List[ColumnSpec]
    description: Optional[str]
    tags: Dict[str, str]
    labels: Dict[str, str]                 # GCP labels
    raw: Dict[str, Any]                   # original dict for provider-specific fields

@dataclass(frozen=True)
class ConsumeSpec:
    id: str
    ref: Optional[str]
    path: Optional[str]
    format: Optional[str]
    raw: Dict[str, Any]

@dataclass(frozen=True)
class BuildSpec:
    pattern: str                    # "declarative", "hybrid-reference", etc.
    engine: Optional[str]           # "dbt", "dataform", "spark"
    sql: Optional[str]
    sql_file: Optional[str]
    properties: Dict[str, Any]
    raw: Dict[str, Any]

class ContractHelper:
    """Universal contract parser. Handles all known contract format variants."""
    def __init__(self, contract: Mapping[str, Any]):
        self._raw = dict(contract)

    @property
    def fluid_version(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def domain(self) -> str: ...

    def exposes(self) -> List[ExposeSpec]: ...
    def consumes(self) -> List[ConsumeSpec]: ...
    def builds(self) -> List[BuildSpec]: ...
    def tags(self) -> Dict[str, str]: ...
    def security(self) -> Dict[str, Any]: ...
    def raw(self) -> Dict[str, Any]: ...
```

**Standardized action schema:**

```python
# fluid_provider_sdk/actions.py

@dataclass
class ProviderAction:
    """Standardized action — the common language between plan() and apply()."""
    op: str                               # "create_dataset", "grant_access", "execute_sql"
    resource_type: str                    # "dataset", "table", "bucket", "role"
    resource_id: str                      # unique within this plan
    params: Dict[str, Any] = field(default_factory=dict)  # provider-specific
    depends_on: List[str] = field(default_factory=list)    # resource_ids
    phase: str = "default"                # "infrastructure", "iam", "build", "test"
    idempotent: bool = True
    description: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]: ...

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProviderAction": ...

def validate_actions(actions: List[ProviderAction]) -> List[str]:
    """Return a list of validation errors (empty = valid)."""
    errors = []
    seen_ids = set()
    for a in actions:
        if not a.op:
            errors.append(f"Action missing 'op': {a}")
        if not a.resource_id:
            errors.append(f"Action missing 'resource_id': {a}")
        if a.resource_id in seen_ids:
            errors.append(f"Duplicate resource_id: {a.resource_id}")
        seen_ids.add(a.resource_id)
        for dep in a.depends_on:
            if dep not in seen_ids and dep not in {x.resource_id for x in actions}:
                errors.append(f"Unknown dependency '{dep}' in action '{a.resource_id}'")
    return errors
```

**Migration:** Existing providers can adopt `ContractHelper` and `ProviderAction` gradually. The raw-dict interface remains supported — `plan()` can still return `List[Dict]` and `apply()` still accepts `Iterable[Mapping]`. The typed classes add validation on top.

**Deliverables:**
- [x] `ContractHelper` handles all 4 current contract format variants (0.4.0/0.5.7/0.7.1)
- [x] `ProviderAction` dataclass with validation (`to_dict()`/`from_dict()`, dict-compat)
- [x] LocalProvider planner migrated to use `ContractHelper`
- [x] `fluid plan --validate-actions` flag checks action schema

---

### Phase 3: Test Harness + Scaffolder (Weeks 5-7)

**Goal:** A provider author can scaffold, implement, and test a new provider in under an hour.

**Test harness:**

```python
# fluid_provider_sdk/testing/harness.py

class ProviderTestHarness:
    """Conformance test suite. Subclass in your test file and run with pytest."""

    # The provider class to test — override in your subclass
    provider_class: Type[BaseProvider]
    # kwargs passed to __init__
    init_kwargs: Dict[str, Any] = {}
    # Sample contracts for plan() testing
    sample_contracts: List[Dict[str, Any]] = []

    def get_provider(self) -> BaseProvider:
        return self.provider_class(**self.init_kwargs)

    # --- Identity tests ---
    def test_subclasses_base_provider(self):
        assert issubclass(self.provider_class, BaseProvider)

    def test_name_is_valid(self):
        assert re.match(r"^[a-z0-9_]+$", self.provider_class.name)

    def test_name_not_reserved(self):
        assert self.provider_class.name not in {"unknown", "stub", "base", "test"}

    # --- Capabilities tests ---
    def test_capabilities_returns_mapping(self):
        prov = self.get_provider()
        caps = prov.capabilities()
        assert isinstance(caps, Mapping)
        assert "planning" in caps
        assert "apply" in caps

    # --- Plan tests ---
    def test_plan_returns_list(self):
        prov = self.get_provider()
        for contract in self.sample_contracts:
            result = prov.plan(contract)
            assert isinstance(result, list)

    def test_plan_actions_have_op(self):
        prov = self.get_provider()
        for contract in self.sample_contracts:
            actions = prov.plan(contract)
            for action in actions:
                assert "op" in action, f"Action missing 'op' key: {action}"

    # --- Apply tests ---
    def test_apply_returns_apply_result(self):
        """Subclass should provide mock actions if apply() talks to real infra."""
        pass  # Optional — providers override if they can test apply() locally

    # --- Metadata tests ---
    def test_get_provider_info(self):
        info = self.provider_class.get_provider_info()
        assert info.name == self.provider_class.name
        assert info.sdk_version

    # --- Registration tests ---
    def test_entry_point_registration(self):
        """Verify the package's entry point is declared and loadable."""
        from importlib.metadata import entry_points
        eps = entry_points(group="fluid_build.providers")
        names = [ep.name for ep in eps]
        assert self.provider_class.name in names, \
            f"Provider '{self.provider_class.name}' not found in entry points. Found: {names}"
```

**Scaffolder (`fluid provider init`):**

```
$ fluid provider init databricks --author "My Company" --description "Databricks Lakehouse"

Creating fluid-provider-databricks/
  ✓ pyproject.toml (entry points pre-configured)
  ✓ src/fluid_provider_databricks/__init__.py
  ✓ src/fluid_provider_databricks/provider.py (BaseProvider subclass)
  ✓ src/fluid_provider_databricks/planner.py (plan() skeleton)
  ✓ src/fluid_provider_databricks/executor.py (apply() skeleton)
  ✓ tests/test_provider.py (harness-based conformance tests)
  ✓ tests/fixtures/basic_contract.yaml
  ✓ README.md
  ✓ LICENSE

Next steps:
  cd fluid-provider-databricks
  pip install -e ".[dev]"
  # Implement plan() in planner.py
  # Implement apply() in executor.py
  pytest  # Run conformance tests
```

**Generated `provider.py` skeleton:**

```python
from fluid_provider_sdk import BaseProvider, ApplyResult, ProviderAction, ContractHelper

class DatabricksProvider(BaseProvider):
    name = "databricks"

    def plan(self, contract):
        helper = ContractHelper(contract)
        actions = []
        for expose in helper.exposes():
            actions.append(ProviderAction(
                op="databricks.ensure_table",
                resource_type="table",
                resource_id=expose.id,
                params={"database": expose.database, "table": expose.table},
                description=f"Ensure table {expose.id} exists",
            ).to_dict())
        return actions

    def apply(self, actions):
        applied, failed = 0, 0
        results = []
        for action in actions:
            try:
                # TODO: implement actual Databricks API calls
                self.info_kv(op=action["op"], status="applied")
                applied += 1
                results.append({"op": action["op"], "status": "success"})
            except Exception as e:
                failed += 1
                results.append({"op": action["op"], "status": "failed", "error": str(e)})
        return ApplyResult(
            provider=self.name, applied=applied, failed=failed,
            duration_sec=0.0, timestamp="", results=results
        )

    def capabilities(self):
        return {"planning": True, "apply": True, "render": False,
                "graph": False, "auth": True}

    @classmethod
    def get_provider_info(cls):
        from fluid_provider_sdk import ProviderMetadata, SDK_VERSION
        return ProviderMetadata(
            name="databricks", display_name="Databricks",
            description="Databricks Lakehouse provider for FLUID",
            version="0.1.0", sdk_version=SDK_VERSION,
            author="My Company",
            tags=["lakehouse", "spark", "delta", "unity-catalog"],
        )
```

**Deliverables:**
- [x] `ProviderTestHarness` in SDK with 16 conformance tests (identity, constructor, capabilities, plan, metadata, apply)
- [x] `fluid provider-init <name>` scaffolder command (registered in CLI bootstrap)
- [x] Generated project is immediately testable with `pytest`
- [x] Sample contracts as pytest fixtures in SDK (LOCAL, GCP, AWS, SNOWFLAKE)
- [ ] `fluid provider test` runs harness against installed provider (deferred to Phase 4+)

---

### Phase 4: Lifecycle Hooks + Capabilities v2 (Weeks 8-10)

**Goal:** Enable advanced integrations (cost estimation, governance, lineage) without breaking the simple plan/apply interface.

**Hook system (pluggy-inspired but simpler):**

```python
# fluid_provider_sdk/hooks.py

class ProviderHookSpec:
    """Optional hooks a provider can implement. All are no-ops by default."""

    def pre_plan(self, contract: dict) -> dict:
        """Modify contract before plan(). Return modified contract."""
        return contract

    def post_plan(self, actions: List[dict]) -> List[dict]:
        """Modify/filter actions after plan(). Return modified list."""
        return actions

    def pre_apply(self, actions: List[dict]) -> List[dict]:
        """Last chance to modify actions before apply(). Return modified list."""
        return actions

    def post_apply(self, result: ApplyResult) -> None:
        """Called after apply(). Use for notifications, lineage, audit."""
        pass

    def on_error(self, error: Exception, context: dict) -> None:
        """Called on plan() or apply() failure."""
        pass

    def estimate_cost(self, actions: List[dict]) -> Optional[CostEstimate]:
        """Optional: estimate cost of planned actions."""
        return None

    def validate_sovereignty(self, contract: dict) -> List[str]:
        """Optional: check data sovereignty / residency constraints."""
        return []
```

**Provider capabilities v2:**

```python
@dataclass
class ProviderCapabilities:
    # Core
    planning: bool = True
    apply: bool = True
    dry_run: bool = False

    # Advanced
    rollback: bool = False
    cost_estimation: bool = False
    schema_validation: bool = False
    lineage: bool = False
    streaming: bool = False
    codegen_airflow: bool = False
    codegen_dagster: bool = False
    codegen_prefect: bool = False

    # Compatibility
    min_cli_version: str = "0.7.0"
    max_cli_version: Optional[str] = None
    supported_contract_versions: List[str] = field(default_factory=lambda: ["0.7.1"])
    supported_resource_types: List[str] = field(default_factory=list)

    def is_compatible_with(self, cli_version: str) -> bool: ...
```

**CLI integration:**

```
$ fluid plan -c contract.yaml --provider databricks --estimate-cost
Plan: 5 actions
  1. [create] databricks.ensure_schema    → "analytics"
  2. [create] databricks.ensure_table     → "prices"
  3. [grant]  databricks.grant_access     → "analysts_role"
  4. [build]  databricks.run_notebook     → "transform_prices"
  5. [test]   databricks.validate_schema  → "prices"

Estimated cost: $0.42/month (Databricks DBUs: ~120/month)

$ fluid apply -c contract.yaml --provider databricks
  [pre_plan hook] Checking sovereignty constraints... OK
  [plan]          5 actions generated
  [post_plan hook] Adding audit metadata... OK
  [pre_apply hook] Validating credentials... OK
  [apply]         5/5 applied
  [post_apply hook] Recording lineage... OK
```

**Deliverables:**
- [x] `ProviderHookSpec` base class with default no-op implementations
- [x] CLI invokes hooks at appropriate lifecycle points
- [x] `ProviderCapabilities` v2 with compatibility checking
- [x] `fluid plan --estimate-cost` when provider supports it
- [x] `fluid plan --check-sovereignty` when provider supports it
- [x] `CostEstimate` dataclass and `invoke_hook`/`has_hook` helpers
- [x] 54 tests covering hooks, capabilities v2, and bugfix validation (168 total)

---

### Phase 5: Provider Index / Marketplace (Month 4+)

**Goal:** Discoverability. Users can find, compare, and install community providers.

**Tiered approach:**

1. **Tier 1: Awesome-list** (week 1 of phase)
   - `awesome-fluid-providers` GitHub repo with a curated README
   - Zero infrastructure cost

2. **Tier 2: JSON index** (week 2-3)
   - `providers.json` hosted on GitHub Pages / fluidhq.io
   - `fluid provider search <query>` reads the index
   - Community submits PRs to add their provider

3. **Tier 3: Full registry** (future, if volume warrants)
   - API service with search, download counts, quality badges
   - Similar to Terraform Registry or dbt Hub
   - Quality tiers: **Verified** (by Dust Labs), **Community** (passes conformance), **Experimental**

**JSON index format:**

```json
{
  "schema_version": "1",
  "providers": [
    {
      "name": "databricks",
      "package": "fluid-provider-databricks",
      "version": "1.2.0",
      "description": "Databricks Lakehouse provider",
      "author": "Databricks, Inc.",
      "url": "https://github.com/databricks/fluid-provider-databricks",
      "sdk_version": ">=0.1.0,<1.0.0",
      "cli_version": ">=0.7.0",
      "tier": "community",
      "tags": ["lakehouse", "spark", "delta"],
      "downloads": 12450
    }
  ]
}
```

**CLI commands:**
```
fluid provider search <query>       # search the index
fluid provider install <package>    # pip install + verify entry point
fluid provider info <name>          # show metadata + capabilities
fluid provider test <name>          # run conformance suite
fluid provider publish              # submit to index (opens PR or API call)
```

**Deliverables:**
- [ ] `awesome-fluid-providers` GitHub repo
- [ ] `providers.json` index with schema
- [ ] `fluid provider search/install/info/test/publish` commands
- [ ] Quality tier badges (Verified / Community / Experimental)
- [ ] Submission workflow (PR-based or API)

---

## Third-Party Provider Developer Experience

> **Goal:** A developer with no prior FLUID knowledge should be able to go from "I want to build a provider" to "it's installed and working" in under 60 minutes.

### The Complete Journey

```
┌────────────────────────────────────────────────────────────────────────────┐
│  STEP 1: Discover (5 min)                                                  │
│                                                                            │
│  $ pip install fluid-forge                                                 │
│  $ fluid providers                                                         │
│  NAME       VERSION  SOURCE     PLATFORMS                                  │
│  aws        0.7.1    builtin    glue, athena, s3, redshift                │
│  gcp        0.7.1    builtin    bigquery, gcs, pubsub                     │
│  snowflake  0.7.1    builtin    snowflake                                 │
│  local      0.7.1    builtin    duckdb                                    │
│                                                                            │
│  "I want to add Databricks support"                                        │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  STEP 2: Scaffold (2 min)                                                  │
│                                                                            │
│  $ fluid provider init databricks \                                        │
│      --author "My Company" \                                               │
│      --description "Databricks Lakehouse provider"                         │
│                                                                            │
│  Creating fluid-provider-databricks/                                       │
│    ✓ pyproject.toml          (entry points, deps, metadata)                │
│    ✓ src/fluid_provider_databricks/                                        │
│    │   ├── __init__.py       (exports + registration)                      │
│    │   ├── provider.py       (BaseProvider subclass skeleton)              │
│    │   ├── planner.py        (plan() logic skeleton)                       │
│    │   └── executor.py       (apply() logic skeleton)                      │
│    ✓ tests/                                                                │
│    │   ├── test_conformance.py  (ProviderTestHarness subclass)             │
│    │   └── fixtures/                                                       │
│    │       └── basic_contract.yaml                                         │
│    ✓ README.md               (getting started, API docs link)              │
│    ✓ LICENSE                                                               │
│                                                                            │
│  Next: cd fluid-provider-databricks && pip install -e ".[dev]"             │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  STEP 3: Implement (30-45 min)                                             │
│                                                                            │
│  The scaffolded provider.py gives you a working skeleton:                  │
│                                                                            │
│  from fluid_provider_sdk import (                                          │
│      BaseProvider, ApplyResult, ContractHelper, ProviderAction,            │
│      ProviderMetadata, SDK_VERSION                                         │
│  )                                                                         │
│                                                                            │
│  class DatabricksProvider(BaseProvider):                                    │
│      name = "databricks"                                                   │
│                                                                            │
│      def plan(self, contract):                                             │
│          helper = ContractHelper(contract)                                 │
│          actions = []                                                      │
│          for expose in helper.exposes():                                   │
│              # TODO: implement your planning logic                         │
│              actions.append({                                              │
│                  "op": f"databricks.ensure_{expose.type}",                 │
│                  "resource_type": expose.type,                             │
│                  "resource_id": expose.id,                                 │
│                  "params": {                                               │
│                      "database": expose.database,                          │
│                      "table": expose.table,                                │
│                      "catalog": expose.raw.get("catalog"),                 │
│                  },                                                        │
│              })                                                            │
│          return actions                                                    │
│                                                                            │
│      def apply(self, actions):                                             │
│          applied, failed = 0, 0                                            │
│          for action in actions:                                            │
│              try:                                                          │
│                  # TODO: call Databricks APIs                              │
│                  self.info_kv(op=action["op"], status="ok")                │
│                  applied += 1                                              │
│              except Exception as e:                                        │
│                  self.error_kv(op=action["op"], error=str(e))              │
│                  failed += 1                                               │
│          return ApplyResult(                                               │
│              provider=self.name,                                           │
│              applied=applied, failed=failed,                               │
│              duration_sec=0.0, timestamp=""                                │
│          )                                                                 │
│                                                                            │
│      def capabilities(self):                                               │
│          return {"planning": True, "apply": True, "render": False,         │
│                  "graph": False, "auth": True}                             │
│                                                                            │
│      @classmethod                                                          │
│      def get_provider_info(cls):                                           │
│          return ProviderMetadata(                                          │
│              name="databricks",                                            │
│              display_name="Databricks",                                    │
│              description="Databricks Lakehouse provider for FLUID",        │
│              version="0.1.0",                                              │
│              sdk_version=SDK_VERSION,                                      │
│              author="My Company",                                          │
│              tags=["lakehouse", "spark", "delta"],                         │
│          )                                                                 │
│                                                                            │
│  What you DON'T need to worry about:                                       │
│    ✗ Registry wiring (entry points handle it)                              │
│    ✗ Contract YAML parsing (ContractHelper does it)                        │
│    ✗ CLI integration (automatic once installed)                            │
│    ✗ Version compat (SDK handles negotiation)                              │
│    ✗ Logging setup (BaseProvider provides self.info_kv, self.warn_kv)      │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  STEP 4: Test (5 min)                                                      │
│                                                                            │
│  # tests/test_conformance.py (auto-generated by scaffolder)                │
│  from fluid_provider_sdk.testing import ProviderTestHarness                │
│  from fluid_provider_databricks import DatabricksProvider                  │
│                                                                            │
│  class TestDatabricksConformance(ProviderTestHarness):                     │
│      provider_class = DatabricksProvider                                   │
│      init_kwargs = {"project": "test", "region": "us-west-2"}             │
│      sample_contracts = [                                                  │
│          # loaded from tests/fixtures/basic_contract.yaml                  │
│      ]                                                                     │
│                                                                            │
│  $ pytest                                                                  │
│  tests/test_conformance.py::TestDatabricksConformance                      │
│    ✓ test_subclasses_base_provider                                         │
│    ✓ test_name_is_valid                                                    │
│    ✓ test_name_not_reserved                                                │
│    ✓ test_capabilities_returns_mapping                                     │
│    ✓ test_plan_returns_list                                                │
│    ✓ test_plan_actions_have_op                                             │
│    ✓ test_get_provider_info                                                │
│    ✓ test_entry_point_registration                                         │
│    8 passed in 0.3s                                                        │
│                                                                            │
│  OR: fluid provider test databricks                                        │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  STEP 5: Use Locally (2 min)                                               │
│                                                                            │
│  $ cd fluid-provider-databricks                                            │
│  $ pip install -e .                                                        │
│  $ fluid providers                                                         │
│  NAME         VERSION  SOURCE       PLATFORMS                              │
│  aws          0.7.1    builtin      glue, athena, s3, redshift            │
│  databricks   0.1.0    entrypoint   databricks                            │  ← new!
│  gcp          0.7.1    builtin      bigquery, gcs, pubsub                 │
│  ...                                                                       │
│                                                                            │
│  $ fluid plan -c my-contract.yaml --provider databricks                    │
│  Plan: 3 actions                                                           │
│    1. [create] databricks.ensure_schema → "analytics"                      │
│    2. [create] databricks.ensure_table  → "prices"                         │
│    3. [grant]  databricks.grant_access  → "analysts"                       │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  STEP 6: Publish (5 min)                                                   │
│                                                                            │
│  # pyproject.toml already has correct entry points:                        │
│  # [project.entry-points."fluid_build.providers"]                          │
│  # databricks = "fluid_provider_databricks:DatabricksProvider"             │
│                                                                            │
│  $ python -m build                                                         │
│  $ twine upload dist/*                                                     │
│                                                                            │
│  # Anyone can now:                                                         │
│  $ pip install fluid-provider-databricks                                   │
│  $ fluid providers   # databricks shows up automatically                   │
│                                                                            │
│  # Optional: submit to the FLUID provider index                            │
│  $ fluid provider publish   # opens PR to awesome-fluid-providers          │
└────────────────────────────────────────────────────────────────────────────┘
```

### What Makes This Easy?

| Friction Point | How We Remove It |
|---|---|
| "Where do I start?" | `fluid provider init` generates everything |
| "What do I subclass?" | Only `BaseProvider` — 2 abstract methods (`plan`, `apply`) |
| "How do I parse the contract YAML?" | `ContractHelper` handles all format variants and version compat |
| "How do I register my provider?" | Entry-point in `pyproject.toml` — scaffolder sets this up |
| "How do I test it?" | `ProviderTestHarness` — scaffolder generates conformance tests |
| "What's the minimum I need to implement?" | `plan()` + `apply()` + `name` class attribute. Everything else has defaults. |
| "Do I need all of fluid-forge as a dependency?" | No — `pip install fluid-provider-sdk` (~0 deps) is enough |
| "How do users install it?" | `pip install fluid-provider-databricks` — entries points auto-register |
| "How do I debug?" | `fluid plan --provider databricks -v` shows plan with verbose logging from `self.info_kv()` |
| "Can I extend beyond plan/apply?" | Yes — implement optional hooks (`pre_plan`, `post_apply`, `estimate_cost`, etc.) |

### Minimal Viable Provider (10 min)

For the absolute simplest case (no SDK, no scaffolder — just Phase 0):

```python
# fluid_provider_hello/__init__.py
from fluid_build.providers.base import BaseProvider, ApplyResult

class HelloProvider(BaseProvider):
    name = "hello"

    def plan(self, contract):
        return [{"op": "hello.greet", "resource_id": contract.get("id", "world")}]

    def apply(self, actions):
        for a in actions:
            print(f"Hello, {a['resource_id']}!")
        return ApplyResult(provider="hello", applied=len(list(actions)),
                           failed=0, duration_sec=0.0, timestamp="")
```

```toml
# pyproject.toml
[project]
name = "fluid-provider-hello"
dependencies = ["fluid-forge>=0.7.0"]

[project.entry-points."fluid_build.providers"]
hello = "fluid_provider_hello:HelloProvider"
```

```bash
pip install -e .
fluid providers          # hello appears
fluid plan -c any.yaml --provider hello
```

**That's it.** One file, one class, two methods.

---

## 6. Migration Strategy for Existing Providers

Existing built-in providers (AWS, GCP, Snowflake, Local) should migrate to the new SDK gradually. No big-bang rewrite.

### Migration order

| Order | Provider | Rationale |
|---|---|---|
| 1 | **Local** | Simplest. Fix `@dataclass` → `BaseProvider` subclass first. Good test bed. |
| 2 | **GCP** | Most mature planner. Clean extraction to `ContractHelper`. |
| 3 | **AWS** | Similar to GCP. Has env-template resolution that should become a shared helper. |
| 4 | **Snowflake** | Most divergent — unique `plan_actions()` signature, top-level binding read. Needs most adaptation. |

### Per-provider migration steps

1. **Subclass `BaseProvider`** (if not already). Fix constructor signature.
2. **Adopt `ContractHelper`** in `plan()`. Replace raw dict parsing.
3. **Return `ProviderAction` objects** from `plan()` (with `.to_dict()` for backward compat).
4. **Implement `get_provider_info()`**. Populate metadata.
5. **Normalize constructor** — accept `project`, `region`, `logger`, `**kwargs` uniformly.
6. **Add conformance tests** using `ProviderTestHarness`.
7. **Move provider-specific kwargs** (Snowflake's `account`, `warehouse`, etc.) into `**kwargs` / `extra`.

### Backward compatibility

The SDK `BaseProvider` will maintain the exact same method signatures:
```python
@abstractmethod
def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]: ...
@abstractmethod
def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult: ...
```

`ProviderAction.to_dict()` / `ProviderAction.from_dict()` bridges typed ↔ untyped worlds. Providers returning raw `List[Dict]` will continue to work.

---

## 7. Versioning & Compatibility

### Version scheme

```
fluid-forge        0.7.x → 0.8.x → 1.0.x
fluid-provider-sdk 0.1.x → 0.2.x → 1.0.x
fluid-provider-*   independent semver
```

### Compatibility matrix

| SDK Version | Min CLI | Max CLI | Notes |
|---|---|---|---|
| 0.1.x | 0.7.0 | 0.8.x | Initial SDK release |
| 0.2.x | 0.7.0 | 0.9.x | Adds ContractHelper |
| 1.0.x | 0.8.0 | — | Stable API — breaking changes bump major |

### Provider compatibility declaration

```toml
# In provider's pyproject.toml
[project]
dependencies = [
    "fluid-provider-sdk>=0.1.0,<1.0.0",
]
```

### Stability guarantees

| API | Stability |
|---|---|
| `BaseProvider.plan()` / `apply()` signatures | **Stable** — part of public SDK |
| `ApplyResult` fields | **Stable** — additive only |
| `ContractHelper` properties | **Stable** after 1.0 — additive only |
| `ProviderAction` fields | **Stable** after 1.0 — additive only |
| `ProviderHookSpec` methods | **Experimental** until 1.0 |
| Marketplace API | **Experimental** |

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **No third-party interest** — we build it and nobody comes | Medium | High | Phase 0 costs 1 day. Don't invest in Phase 5 until signals appear. |
| **SDK versioning hell** — breaking changes propagate painfully | Medium | High | Stability guarantees. Deprecation warnings before removal. Never break `plan()`/`apply()` signatures. |
| **Dual-package maintenance burden** | Medium | Medium | SDK is tiny (~300 lines initially). Auto-publish from monorepo CI. |
| **Existing providers resist migration** | Low | Medium | Migration is opt-in. Old raw-dict `plan()` continues to work. |
| **Action schema is too restrictive** | Medium | Medium | `params: Dict[str, Any]` is the escape hatch for provider-specific data. Core only validates common keys. |
| **Plugin system confusion** — three systems remain | Medium | Low | Phase 0 documents which is canonical. Phase 1 deprecates `cli/plugins.py` `ProviderPlugin`. |
| **ContractHelper can't handle all edge cases** | Low | Medium | `raw` field on every spec preserves access to original dict. |
| **NEW: Name normalization inconsistency during migration** | Medium | Medium | `__init__.py` normalizes (`-` → `_`, lowercase) but `base.py` doesn't. During migration, a provider registered as `datamesh-manager` via one path and `datamesh_manager` via another could create ghost entries. Fix: consolidate to single registry first (Phase 0, step 0.2). |
| **NEW: `build_provider()` raw name lookup** | High | Low | `build_provider()` does `PROVIDERS.get(name)` without normalization. A user typing `--provider GCP` gets "provider not found". Fix: add `.lower().strip()` before lookup. Already planned for Phase 0. Bug fix, not breaking change. |
| **NEW: Forge registry confusion** | Low | Low | `forge/__init__.py` has its own `register_provider()` and `ProviderRegistry` that is completely separate from the production system. Third-party devs may import from the wrong module. Fix: document clearly which import paths are the public API. Long-term: consider merging or renaming. |

---

## 9. Decision Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-03-02 | Start with Option D (Hybrid phased approach) | Balances immediate impact with long-term ecosystem play |
| | Entry points as primary discovery mechanism | Industry standard (Airflow, dbt, pytest, Stevedore) |
| | Separate SDK package | Follows dbt (`dbt-adapters`) and Terraform (`terraform-plugin-framework`) precedent |
| | `BaseProvider` ABC over `Protocol` | Easier conformance testing, clearer error messages, matches existing code. Protocol can be added later as an alternative. |
| | Keep `plan() -> List[Dict]` signature | Backward compatible. `ProviderAction` is an optional typed wrapper. |
| | Phase gates with adoption signals | Avoid over-engineering. Each phase delivers standalone value. |
| 2026-03-02 (v2) | Triple registry → single canonical registry | Deep review found 3 registries, not 2. `forge/__init__.py` has its own. All must consolidate. |
| | LocalProvider migration requires signature refactoring | `apply()` and `render()` don't match BaseProvider. Can't just add a base class. |
| | `build_provider()` must normalize names | Bug: `--provider GCP` fails because raw string used for dict lookup |
| | Remove dead `build_provider_instance()` | Zero callers in entire codebase — both copies are dead code |
| | `ContractHelper` must handle 5 parsing patterns | AWS (platform routing + flat fallback), GCP (dual-format), Snowflake (top-level binding), Local (no binding), ODPS (adapter-based) |
| | Breaking changes are Phase 0 only | All subsequent phases are additive. This is the key design constraint. |

---

## 10. Appendix: File-Level Refactoring Map

### Phase 0 changes — IMPLEMENTED

| File | Change | Status |
|---|---|---|
| `providers/__init__.py` | Add `_discover_entrypoints()`, wire into `discover_providers()` | **Done** |
| `providers/__init__.py` | Make this the **only** `PROVIDERS` dict | **Done** |
| `providers/__init__.py` | Remove dead `build_provider_instance()` (L349, zero callers) | **Done** |
| `providers/base.py` | Remove duplicate `PROVIDERS` dict. Re-export `register_provider` from `__init__`. Keep `BaseProvider`, `ApplyResult`, error classes. Remove dead `build_provider_instance()` (L326). | **Done** |
| `providers/local/local.py` | Drop `@dataclass`. Add `class LocalProvider(BaseProvider)`. Write explicit `__init__` with `persist` kwarg. Refactor `apply()` and `render()` signatures. Set `name = "local"`. | **Done** |
| `cli/_common.py` | Fix `build_provider()` to normalize name: `name = name.lower().replace("-", "_").strip()` before PROVIDERS lookup | **Done** |
| `cli/provider_cmds.py` | Show `source` (builtin/entrypoint) in `fluid providers` output, `--debug` flag | **Done** |
| `pyproject.toml` | Add `aws` entry point (currently missing). Verify all built-in providers are declared. | **Done** |
| `docs/CREATING_PROVIDERS.md` | Third-party provider development guide | **Done** |
| `tests/providers/test_registry.py` | 31 tests covering registry, entry-points, LocalProvider migration, SDK integration | **Done** |

### Phase 1 changes (new package) — IMPLEMENTED

| File | Change | Status |
|---|---|---|
| **New:** `fluid-provider-sdk/pyproject.toml` | Package config (zero dependencies) | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/__init__.py` | Public API re-exports | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/base.py` | `BaseProvider` ABC | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/types.py` | `ApplyResult`, `PlanAction`, `ProviderError`, `ProviderInternalError` | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/capabilities.py` | `ProviderCapabilities` dataclass | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/metadata.py` | `ProviderMetadata` + `get_provider_info()` | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/version.py` | `SDK_VERSION`, `MIN_CLI_VERSION`, `MAX_CLI_VERSION` | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/py.typed` | PEP 561 marker | **Done** |
| `fluid-forge/providers/base.py` | Import from SDK with fallback (`_HAS_SDK` flag) | **Done** |
| All 4 built-in providers | Added `get_provider_info()` returning `ProviderMetadata` | **Done** |

### Phase 2 changes — IMPLEMENTED

| File | Change | Status |
|---|---|---|
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/contract.py` | `ContractHelper`, `ExposeSpec`, `ConsumeSpec`, `BuildSpec`, `ColumnSpec` | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/actions.py` | `ProviderAction`, `validate_actions()` | **Done** |
| `providers/local/planner.py` | Migrated to `ContractHelper` (first adopter, with fallback) | **Done** |
| `cli/plan.py` | Added `--validate-actions` flag | **Done** |
| `tests/providers/test_phase2_contract_actions.py` | 42 tests for ContractHelper, ProviderAction, validate_actions | **Done** |

### Phase 3 changes — IMPLEMENTED

| File | Change | Status |
|---|---|---|
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/testing/__init__.py` | Testing subpackage init | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/testing/harness.py` | `ProviderTestHarness` (16 conformance test methods) | **Done** |
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/testing/fixtures.py` | `LOCAL_CONTRACT`, `GCP_CONTRACT`, `AWS_CONTRACT`, `SNOWFLAKE_CONTRACT` | **Done** |
| **New:** `fluid-forge/cli/provider_init.py` | `fluid provider-init <name>` scaffolder | **Done** |
| `cli/bootstrap.py` | Register `provider-init` subcommand | **Done** |
| `tests/providers/test_phase3_harness_scaffold.py` | 41 tests for harness, fixtures, scaffolder | **Done** |

### Phase 4 changes — COMPLETE

| File | Change | Status |
|---|---|---|
| **New:** `fluid-provider-sdk/src/fluid_provider_sdk/hooks.py` | `ProviderHookSpec`, `CostEstimate`, `invoke_hook`, `has_hook` | **Done** |
| `fluid-provider-sdk/src/fluid_provider_sdk/capabilities.py` | `ProviderCapabilities` v2: `dry_run`, `rollback`, `cost_estimation`, `schema_validation`, `lineage`, `streaming` fields | **Done** |
| `fluid-provider-sdk/src/fluid_provider_sdk/__init__.py` | Export new hook types | **Done** |
| `fluid-provider-sdk/src/fluid_provider_sdk/contract.py` | Fix M1: `ConsumeSpec.from_dict()` string location handling | **Done** |
| `fluid_build/providers/base.py` | Fix H1 (proxy dicts), H3 (capabilities), hook type fallbacks | **Done** |
| `fluid_build/providers/__init__.py` | Fix M7: `_check_sdk_compat()` + `_parse_version()` | **Done** |
| `fluid_build/cli/_common.py` | Fix H2 (targeted TypeError), M4 (top-level binding) | **Done** |
| **New:** `fluid_build/cli/hooks.py` | CLI hook wrappers: `run_pre_plan`, `run_post_plan`, `run_pre_apply`, `run_post_apply`, `run_on_error`, `run_estimate_cost`, `run_validate_sovereignty` | **Done** |
| `cli/plan.py` | Invoke `pre_plan`/`post_plan` hooks, `--estimate-cost`, `--check-sovereignty` flags | **Done** |
| `cli/apply.py` | Invoke `pre_apply`/`post_apply`/`on_error` hooks | **Done** |
| `fluid_build/providers/local/local.py` | Fix M2 (apply return type), M3 (render signature), M5 (utcnow) | **Done** |
| `fluid_build/providers/local/planner.py` | Fix M6: deterministic `sorted()` fallback | **Done** |
| `tests/providers/test_phase4_hooks_and_fixes.py` | 54 tests: hooks, capabilities v2, bugfix validation | **Done** |

### Phase 5 changes — NOT STARTED

| File | Change | Status |
|---|---|---|
| **New:** `fluid-forge/cli/provider_market.py` | `fluid provider search/install/info/publish` | Pending |
| **New:** `providers.json` (hosted) | Provider index | Pending |
| **New:** `awesome-fluid-providers/` (GitHub repo) | Curated list | Pending |

---

## What's Next

1. **~~Phase 0: Unblock External Providers~~ — COMPLETE** (31 tests)
2. **~~Phase 1: Provider SDK Extraction~~ — COMPLETE** (41 tests)
3. **~~Phase 2: Contract Helpers + Action Schema~~ — COMPLETE** (73 tests)
4. **~~Phase 3: Test Harness + Scaffolder~~ — COMPLETE** (114 tests)
5. **~~Phase 4: Lifecycle Hooks + Capabilities v2~~ — COMPLETE** (168 tests)
   - `ProviderHookSpec` with 7 hook methods (pre/post plan, pre/post apply, on_error, estimate_cost, validate_sovereignty)
   - `ProviderCapabilities` v2 with 6 new fields (dry_run, rollback, cost_estimation, schema_validation, lineage, streaming)
   - CLI integration: `--estimate-cost` and `--check-sovereignty` flags on `fluid plan`
   - Bugfixes: H1-H3, M1-M7 from architecture review resolved
6. **Phase 5: Provider Index / Marketplace** — Future
   - Create `awesome-fluid-providers` curated list
   - Build `providers.json` index
   - Implement `fluid provider search/install/info/test/publish` commands
7. **Identify first third-party provider candidate** — Databricks, Azure, or Kafka would drive real-world validation of the SDK

### Phase 0 Execution Checklist — COMPLETE

```
PR #1 — Registry consolidation + entry-point activation
  ✅ providers/__init__.py: add _discover_entrypoints()
  ✅ providers/__init__.py: remove dead build_provider_instance()
  ✅ providers/base.py: replace PROVIDERS with re-export from __init__
  ✅ providers/base.py: replace register_provider with thin wrapper
  ✅ providers/base.py: remove dead build_provider_instance()
  ✅ cli/_common.py: normalize name in build_provider()
  ✅ pyproject.toml: add aws entry point
  ✅ tests: verify all 6 providers register correctly

PR #2 — LocalProvider BaseProvider migration
  ✅ providers/local/local.py: drop @dataclass, add BaseProvider
  ✅ providers/local/local.py: refactor apply()/render() signatures
  ✅ cli/apply.py: update LocalProvider callers
  □ tests: conformance test for LocalProvider

PR #3 — Documentation + fluid providers enhancement
  □ cli/provider_cmds.py: show source column
  □ README: third-party provider guide
  □ docs: CREATING_PROVIDERS.md
```

---

*This document will evolve as we make decisions and learn from implementation. Update the Decision Log as choices are made.*
