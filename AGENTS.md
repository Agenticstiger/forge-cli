# AGENTS.md — AI Agent Integration Guide

> How AI agents, LLMs, and copilots should interact with FLUID Forge and the data products it governs.

---

## What Is FLUID Forge?

FLUID Forge is a **contract-driven build system for declarative data products**. You write a single `contract.fluid.yaml` that declares your data product — transformations, schemas, quality rules, access policies, observability, and AI governance — and the CLI compiles it into validated, planned, executable deployments across any supported cloud.

```
fluid validate → fluid plan → fluid apply
```

One contract. Any provider. Full governance. Zero boilerplate.

---

## For AI Coding Agents (Copilot, Cursor, Cline, etc.)

### Project Structure

```
forge-cli/
├── fluid_build/              # Python package — the CLI
│   ├── cli/                  # Command implementations (Click-based)
│   ├── providers/            # Provider plugins: local, gcp, aws, snowflake, odps, odcs
│   ├── forge/                # AI-assisted project creation engine
│   ├── policy/               # Policy compiler, agent policy, sovereignty
│   ├── blueprints/           # Enterprise blueprint registry
│   ├── credentials/          # Credential resolution (keyring, dotenv, encrypted)
│   ├── schemas/              # FLUID JSON Schema versions (0.5.7, 0.7.1, 0.7.2)
│   ├── templates/            # Init templates (hello-world, customer-360, etc.)
│   └── tools/                # Diagnostic utilities
├── tests/                    # Pytest suite — unit, integration, provider-specific
├── examples/                 # Progressive learning examples (01-hello-world → customer360)
├── docs/                     # Documentation site source
├── pyproject.toml            # Package metadata, dependencies, tool configs
├── Makefile                  # Developer ergonomics — `make setup`, `make test`, `make build`
└── AGENTS.md                 # This file
```

### Key Entry Points

| What | Where |
|------|-------|
| CLI entrypoint | `fluid_build/cli/__init__.py` → `main()` |
| Command implementations | `fluid_build/cli/*.py` (validate, plan, apply, forge, export, etc.) |
| Provider plugins | `fluid_build/providers/{local,gcp,aws,snowflake}/` |
| Contract schemas | `fluid_build/schemas/*.json` |
| Policy engine | `fluid_build/policy/` (compiler, agent_policy, sovereignty, guardrails) |
| Forge (AI creation) | `fluid_build/forge/` (templates, generators, extensions) |
| Test suite | `tests/` — mirrors `fluid_build/` structure |

### Development Commands

```bash
make setup          # One-command setup: venv + deps + doctor
make test           # Run pytest suite
make lint           # Ruff + Black check
make fmt            # Auto-format
make build          # Build wheel
make doctor         # Run system diagnostics
make demo           # validate → plan → apply on example contract
```

### When Modifying Code

- **Adding a CLI command**: Create `fluid_build/cli/<command>.py`, register in `fluid_build/cli/__init__.py`
- **Adding a provider**: Create `fluid_build/providers/<name>/`, implement `BaseProvider` interface
- **Adding a template**: Create `fluid_build/templates/<name>.j2` or add to `fluid_build/forge/templates/`
- **Adding a policy rule**: Extend validators in `fluid_build/policy/`
- **Modifying the contract schema**: Update `fluid_build/schemas/` and bump `fluidVersion`

### Code Conventions

- Python 3.9+ target
- Line length: 100 (Ruff + Black)
- Type hints encouraged but not enforced (`mypy` runs with `ignore_missing_imports`)
- Logging via `fluid_build/structured_logging.py` (structured JSON logs)
- Rich console output for user-facing messages
- Test markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.gcp`, etc.

---

## For AI Agents Consuming Data Products

FLUID contracts include first-class **agent policies** that govern how AI/LLM systems may access, process, and store data. This is the `agentPolicy` block in `contract.fluid.yaml`.

### Agent Policy Schema

```yaml
exposes:
  - exposeId: sales_metrics
    kind: table
    policy:
      agentPolicy:
        # Which models may access this data
        allowedModels:
          - gpt-4
          - claude-3-opus
        deniedModels:
          - llama-3-70b          # No open-source models for this data

        # What the AI may do with the data
        allowedUseCases:
          - inference            # Read-only analysis
          - summarization        # Report generation
          - analysis             # Trend analysis
        deniedUseCases:
          - training             # Never train on this data
          - fine_tuning          # Never fine-tune on this data
          - embedding            # No vector embeddings

        # Operational limits
        maxTokensPerRequest: 8192
        maxTokensPerDay: 1000000

        # Storage and reasoning
        canStore: false          # May the agent persist this data?
        canReason: true          # May the agent do multi-step reasoning?

        # Retention
        retentionPolicy:
          maxRetentionDays: 90
          requireDeletion: true

        # Audit
        auditRequired: true
        purposeLimitation: "Sales reporting only — no customer profiling"
```

### Policy Enforcement Levels

| Level | `allowedModels` | `allowedUseCases` | `canStore` | Example |
|-------|-----------------|-------------------|------------|---------|
| **Blocked** | `[]` (empty) | All denied | `false` | Raw PII, financial transactions |
| **Restricted** | Named models only | Named use cases only | `false` | Internal analytics, HR data |
| **Moderate** | Named models | Broad use cases | `true` with retention | Aggregated metrics, dashboards |
| **Open** | Not specified | Not specified | `true` | Public datasets, market data |

### How Agents Should Respect Policies

1. **Before accessing a data product**, read its `agentPolicy` from the contract
2. **Check your model identity** against `allowedModels` / `deniedModels`
3. **Check your use case** against `allowedUseCases` / `deniedUseCases`
4. **Respect token limits** — honour `maxTokensPerRequest` and `maxTokensPerDay`
5. **Respect storage rules** — if `canStore: false`, do not persist any data beyond the session
6. **Respect retention** — if `retentionPolicy.requireDeletion: true`, delete data after `maxRetentionDays`
7. **Log access** if `auditRequired: true`

### Validating Agent Policies

```bash
# Validate all policies in a contract
fluid policy-check contract.fluid.yaml

# Compile policies to provider-native IAM
fluid policy-compile contract.fluid.yaml

# Run the full agent policy validator
fluid contract-validation contract.fluid.yaml
```

The `AgentPolicyValidator` in `fluid_build/policy/agent_policy.py` checks for:
- Unknown models (warns if not in the known model registry)
- Conflicting allow/deny lists
- Missing required fields for restricted data
- Use case validity against the FLUID schema
- Token limit sanity checks

---

## For AI-Assisted Project Creation (Forge)

FLUID Forge includes AI-powered project creation via specialized domain agents:

```bash
fluid forge                          # Interactive — picks the best agent
fluid forge --mode template          # Template-guided creation
fluid forge --mode copilot           # AI copilot assistance
fluid forge --mode blueprint         # Enterprise blueprint deployment
```

### Domain Agents

The `fluid_build/cli/forge_agents.py` module implements domain-specific AI agents:

| Agent | Domain | What It Creates |
|-------|--------|----------------|
| Analytics Agent | Business analytics | Dashboards, KPI tracking, trend analysis |
| ML Pipeline Agent | Machine learning | Feature stores, model serving, experiment tracking |
| ETL Agent | Data engineering | Ingestion pipelines, transformations, quality gates |
| Streaming Agent | Real-time data | Event processing, windowing, alerting |
| Compliance Agent | Governance | Policy-first contracts with full access control |

Each agent:
1. Asks domain-specific questions about your requirements
2. Analyzes requirements and recommends templates, providers, and patterns
3. Generates a complete FLUID project with contract, tests, and CI config
4. Validates the generated contract against the FLUID schema

### Extending with Custom Agents

```python
from fluid_build.cli.forge_agents import AIAgentBase

class MyDomainAgent(AIAgentBase):
    def __init__(self):
        super().__init__(
            name="my-domain",
            description="Custom agent for my domain",
            domain="my-domain"
        )

    def get_questions(self):
        return [
            {"id": "goal", "text": "What is your data product goal?", "type": "text"},
            {"id": "provider", "text": "Target cloud?", "type": "choice",
             "options": ["local", "gcp", "aws", "snowflake"]},
        ]

    def analyze_requirements(self, context):
        return {
            "recommended_template": "analytics",
            "recommended_provider": context.get("provider", "local"),
            "suggested_quality_rules": ["not_null", "uniqueness"],
        }
```

---

## Data Sovereignty & Guardrails

FLUID contracts support data sovereignty constraints that agents must respect:

```yaml
sovereignty:
  jurisdiction: EU
  residency: strict
  allowedRegions:
    - europe-west1
    - europe-west3
```

The CLI enforces these at plan time — any deployment to a non-allowed region is rejected. AI agents operating on governed data should similarly restrict data movement to allowed jurisdictions.

---

## MCP / Tool Use Integration

FLUID CLI commands are designed to be composable and machine-readable:

```bash
# All commands support --out for structured JSON output
fluid validate contract.fluid.yaml --out validation.json
fluid plan contract.fluid.yaml --out plan.json
fluid apply contract.fluid.yaml --out apply.json

# Graph output for dependency analysis
fluid graph contract.fluid.yaml --format dot
fluid graph contract.fluid.yaml --format json

# Policy reports
fluid policy-check contract.fluid.yaml --out policy-report.json
```

For MCP (Model Context Protocol) tool servers, these JSON outputs can be piped directly into agent tool responses. The structured output includes:
- Validation results with line-level error locations
- Execution plans with resource diffs
- Policy compliance reports with violation severity

---

## Links

- **Documentation**: [https://agenticstiger.github.io/forge_docs/](https://agenticstiger.github.io/forge_docs/)
- **PyPI**: [https://pypi.org/project/fluid-forge](https://pypi.org/project/fluid-forge/)
- **Repository**: [https://github.com/Agenticstiger/forge-cli](https://github.com/Agenticstiger/forge-cli)
- **Agent Policy Examples**: `examples/0.7.1/ai-restricted-data.yaml`
- **Policy Validator Source**: `fluid_build/policy/agent_policy.py`
- **Forge Agents Source**: `fluid_build/cli/forge_agents.py`

---

*FLUID Forge — Declarative Data Products for the Agentic Era*
*Copyright 2024–2026 Agentics Transformation Pty Ltd — [fluidhq.io](https://fluidhq.io)*
