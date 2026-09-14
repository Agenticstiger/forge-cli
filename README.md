<div align="center">

<img src="https://raw.githubusercontent.com/Agenticstiger/forge-cli/main/assets/fluid-forge-logo.png" alt="FLUID Forge" width="420">

</div>

**One YAML contract describes a data product — its schema, its quality rules, where its data is
allowed to live, and which AI agents may read it. `fluid` compiles that contract into cloud
infrastructure, and refuses to deploy anything the contract forbids.**

[![PyPI](https://img.shields.io/pypi/v/data-product-forge.svg)](https://pypi.org/project/data-product-forge/)
[![Downloads](https://img.shields.io/pypi/dm/data-product-forge.svg)](https://pypi.org/project/data-product-forge/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://github.com/Agenticstiger/forge-cli/blob/main/LICENSE)
[![CI](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/Agenticstiger/forge-cli?style=social)](https://github.com/Agenticstiger/forge-cli)

[Documentation](https://agenticstiger.github.io/forge_docs/) ·
[Getting started](https://agenticstiger.github.io/forge_docs/getting-started/) ·
[Demos](https://agenticstiger.github.io/forge_docs/demos/) ·
[FLUID Spec](https://open-data-protocol.github.io/fluid/) ·
[Changelog](https://github.com/Agenticstiger/forge-cli/blob/main/CHANGELOG.md)

<div align="center">

<img src="https://raw.githubusercontent.com/Agenticstiger/forge_docs/main/docs/.vuepress/public/demos/local-quickstart.svg" alt="Terminal recording: pip install, fluid init, then validate, plan and apply against the local DuckDB provider" width="800">

<sub><i>Install through deploy against the local DuckDB provider. More casts — the same contract on a
different cloud, agentPolicy enforcement, the AI copilot — in the
<a href="https://agenticstiger.github.io/forge_docs/demos/">demos library</a>.</i></sub>

</div>

## Install and run it

Requires Python 3.10 or newer. The `[local]` extra adds the DuckDB provider, so the first run needs
no cloud account and no credentials.

```bash
pip install "data-product-forge[local]"

fluid init demo --template hello-world
cd demo
fluid validate contract.fluid.yaml     # ✅ Valid FLUID contract (schema v0.7.2)
fluid plan contract.fluid.yaml         # 2 actions: provision a dataset, schedule a task
fluid apply contract.fluid.yaml --yes  # ✅ Data product deployed successfully — 1.35s
```

`apply` writes `output/hello_message.parquet` and an HTML run report under `runtime/`. Nothing leaves
the machine.

Cloud targets are separate extras: `[gcp]`, `[aws]`, `[snowflake]`, or `[all]`. For an isolated
global install, [pipx](https://pipx.pypa.io/) works too:
`pipx install "data-product-forge[all]"`.

## What makes it different

Four properties, each with the command that demonstrates it. Everything below was run against
`data-product-forge` 0.15.0 installed from PyPI, offline, with no cloud credentials configured.

### 1. The platform lives in one block, and only that block

[`examples/sovereignty-platform-swap`](https://github.com/Agenticstiger/forge-cli/tree/main/examples/sovereignty-platform-swap)
carries the same data product bound to AWS, Google Cloud and Snowflake. Strip the `binding:` block
out of all three files and what remains is byte-identical:

```bash
git clone --depth 1 --branch v0.15.0 https://github.com/Agenticstiger/forge-cli.git
cd forge-cli/examples/sovereignty-platform-swap

for c in contract.fluid.yaml contract.gcp.yaml contract.snowflake.yaml; do
  awk '/^    binding:/{skip=1; next} /^    contract:/{skip=0} !skip' "$c" | shasum
done
# three identical hashes: 886dfddc003055d2699008e05e4c40ceb133c39a
```

Measured against the AWS contract, the GCP version changes 6 lines and the Snowflake version 7:
platform, format, and the location keys each warehouse actually needs. Every other line of the
54-line file is untouched — the five-column schema, the completeness rule on `event_id`, the one-hour
freshness SLO, the owning team. Run `diff contract.fluid.yaml contract.gcp.yaml` and the entire diff
sits between lines 19 and 26.

That separation survives compilation. `fluid generate iac` emits a deterministic OpenTofu
`main.tf.json` from each contract, offline:

```bash
for c in contract.fluid.yaml contract.gcp.yaml contract.snowflake.yaml; do
  fluid generate iac "$c" -o "iac-$c"
done
```

| Same contract, bound to | Resources emitted |
|---|---|
| `platform: aws` | `aws_s3_bucket`, `aws_glue_catalog_database`, `aws_glue_catalog_table` |
| `platform: gcp` | `google_bigquery_dataset`, `google_bigquery_table` |
| `platform: snowflake` | `snowflake_database`, `snowflake_schema`, `snowflake_table` |

Apply is then delegated to the `tofu` binary, so state, drift detection and idempotency belong to
OpenTofu rather than to a hand-rolled implementation per cloud.

### 2. Plans are deterministic, so a plan can be reviewed

`fluid plan` writes `plan.json` with a `planDigest` computed over the plan's contents. Run it twice
against an unchanged contract and the two files differ in exactly one line, the wall-clock
`generated_at` stamp. The digest is identical:

```bash
fluid plan contract.fluid.yaml && cp plan.json first.json
fluid plan contract.fluid.yaml && diff first.json plan.json
# 233c233
# <   "generated_at": 1789418017.904394,
# ---
# >   "generated_at": 1789418019.5870879,
```

Each of the three platform variants yields a different digest, which is what makes the digest usable
as a gate. Bundle the contract, plan against the bundle, and the plan records the bundle it was
computed from. Apply it against a bundle whose contents have moved on and it refuses, exit 1:

```bash
fluid bundle contract.fluid.yaml --format tgz -o bundle.tgz   # content-addressed, merkle root
fluid plan bundle.tgz --out plan.json                          # plan.json records that digest
# ...someone edits the contract and re-bundles...
fluid apply plan.json --bundle bundle2.tgz --dry-run
# ❌ apply_plan_digest_bundle_mismatch  [ERR_APPLY_PLAN_DIGEST_BUNDLE_MISMATCH]
#   plan.json was computed against bundle 'sha256:de7a84e5…' but bundle2.tgz has
#   digest 'sha256:9f006e73…'. Re-run `fluid plan` against the current bundle before applying.
```

Against the bundle it was planned from, the same command exits 0. The check is on by default;
`--no-verify-plan-binding` exists as a documented escape hatch for disaster recovery.

### 3. Data residency is declared in the contract and enforced by the engine

A contract can pin a jurisdiction. As of 0.15.0 the engine enforces it by default, with no flag to
turn on:

```yaml
sovereignty:
  jurisdiction: EU
  regulatoryFramework: [GDPR]
```

With the binding's `region: eu-central-1`, `fluid validate` exits 0. Change that one value to
`us-east-1` and it exits 1:

```
❌ Invalid FLUID contract (1 error(s)) (schema v0.7.5)
 1. ❌  Region 'us-east-1' (jurisdiction: US) does not match required jurisdiction: EU
   💡 Consider using regions in EU jurisdiction
```

The region table is specific rather than approximate: `eu-west-2` is London, and under
`jurisdiction: EU` it fails the same way, naming `jurisdiction: UK`. Contracts that pin no
jurisdiction, or that pin `Global` or `Multi-Region`, are unaffected and validate into any region.

### 4. Agents read through a policy gate that fails closed

`fluid mcp output-port serve` turns an expose into an MCP server for Claude Desktop, Cursor or any
other MCP client, with the contract's `agentPolicy` — allowed models, allowed use cases, caller
jurisdiction — checked on every tool call.

Fail-closed means it refuses to start when the deployment could not possibly satisfy the contract. A
jurisdiction-pinned contract served over stdio exits 2 before accepting a connection, because stdio
carries no headers and therefore no verified caller jurisdiction:

```
fluid mcp output-port: refusing to serve over 'stdio'.
  This contract pins sovereignty.jurisdiction to EU, so every tool call needs a
  cryptographically verified caller jurisdiction.
  'stdio' carries no headers, so no credential can supply one and every call would be denied.
  Serve it over HTTP with auth instead:
    FLUID_MCP_AUTH_MODE=jwt fluid mcp output-port serve --transport http
```

A jurisdiction claim binds only when a JWT or mTLS identity was verified, never from self-attested
client metadata. The decision function has a closed vocabulary of 11 outcomes and ships its test
vectors inside the wheel, so the gate's behaviour is inspectable without reading the source:

```bash
python -c "import json, importlib.resources as r; \
d = json.loads(r.files('fluid_build').joinpath('policy/data/vectors/agent-policy-vectors.json').read_text()); \
print(len(d['vectors']), 'vectors,', len(d['reasonCodes']), 'reason codes')"
# 35 vectors, 11 reason codes
```

Query drivers in tree: DuckDB, Postgres, Snowflake, BigQuery, AWS Athena. A one-command Postgres
demo, with Caddy and nginx mTLS templates for production, is in
[`examples/mcp-output-port-docker/`](https://github.com/Agenticstiger/forge-cli/tree/main/examples/mcp-output-port-docker).

## What a contract looks like

One file is the source of truth for the whole lifecycle. These blocks are the questions a data
product has to answer.

```yaml
fluidVersion: "0.7.5"
kind: DataProduct
id: analytics.eu.customer_events_v1
name: EU Customer Events
domain: Customer

metadata:
  layer: Silver            # medallion vocabulary
  productType: ADP         # Data Mesh vocabulary — Bronze↔SDP, Silver↔ADP, Gold↔CDP
  owner:
    team: customer-platform
    email: customer-platform@example.com

# WHERE MAY THE DATA LIVE?  Checked at validate time, before any cloud call.
sovereignty:
  jurisdiction: EU
  regulatoryFramework: [GDPR]

# HOW IS IT BUILT?
builds:
  - id: transform_events
    pattern: embedded-logic
    engine: sql
    properties:
      sql: SELECT event_id, event_time, customer_id FROM raw.events

exposes:
  - exposeId: customer_events
    kind: table

    # WHERE DOES IT LAND?  The only block that changes between clouds.
    binding:
      platform: aws
      format: parquet
      location:
        database: customer_analytics
        table: customer_events
        bucket: acme-eu-lake
        path: curated/customer_analytics/customer_events/
        region: eu-central-1

    # WHAT IS THE AGREEMENT?
    contract:
      schema:
        - name: event_id
          type: string
          required: true
        - name: customer_id
          type: string
          required: true
          sensitivity: pseudonymized   # drives masking and encryption
      dq:
        rules:
          - id: event_id_not_null
            type: completeness
            selector: event_id
            threshold: 1.0
            operator: ">="
            severity: error

    # WHO, AND WHICH AGENTS, MAY READ IT?
    policy:
      agentPolicy:
        allowedModels: ["claude-sonnet-4-5"]
        allowedUseCases: ["analysis"]
        deniedUseCases: ["training"]

    qos:
      availability: "99.5%"
      freshnessSLO: "PT1H"
```

Save that block and `fluid validate` it: it passes as written against 0.15.0, comments and all.
`fluid version` prints the schema versions a given release accepts — 0.15.0 validates FLUID 0.7.1
through 0.7.6, defaults to 0.7.5, and treats 0.7.6 as preview.

## Where it deploys

Three clouds, plus a local path that needs no account. These are the values
`fluid generate iac --provider` accepts, which is the honest test of whether
something is a deployment target:

| Target | What its OpenTofu emitter can provision |
|---|---|
| `local` | DuckDB and the local filesystem. No credentials, no cloud account. Selected by the contract's `binding.platform`, not by `--provider`. |
| `gcp` | BigQuery datasets and tables with IAM members, GCS buckets and objects, Pub/Sub topics and subscriptions, Cloud Run services, Cloud Scheduler jobs. |
| `aws` | S3 buckets with policies and notifications, Glue catalog databases, tables and jobs, Lake Formation permissions and LF-tags, Lambda, Kinesis streams, Step Functions, EventBridge rules. |
| `snowflake` | Databases, schemas, tables, views, warehouses, tasks, streams, masking and row-access policies, RBAC grants, external volumes and the Glue catalog integration that Iceberg tables need. |

`--provider` also accepts `confluent`, for Kafka-side resources, and `auto`,
which is the default and reads the target out of the contract's binding.

**`fluid providers` prints a longer list, and it is a different thing.** It is
the registry of everything that registers as a provider plugin, which includes
publishers and a partly-landed emitter:

- `datamesh_manager` is a **publish** target, reached by
  [`fluid publish`](https://agenticstiger.github.io/forge_docs/cli/datamesh-manager.html).
  It receives a contract's metadata; it never provisions infrastructure.
- `redshift` has an emitter in the tree but is **not selectable with
  `--provider`** — `fluid generate iac --provider redshift` exits with
  `invalid choice`. Treat it as unfinished rather than as a fourth cloud.

Neither belongs in a list of places a data product can be deployed, and this
README previously listed both as though they were.

That column is the set each emitter is capable of. Which resources a particular contract actually
produces is visible before anything is applied: `fluid generate iac <contract>` writes the
`main.tf.json` and you can read it.

Per-provider setup and credential handling are in the
[providers documentation](https://agenticstiger.github.io/forge_docs/providers/).

Contracts also export to open standards rather than staying in a private format. `fluid exporters`
lists what the installed release emits: Bitol Open Data Contract Standard v3.1.0 (`--format odcs`),
Bitol Open Data Product Standard v1.0.0 (`--format odps`, the default), and the LF/ODPI Open Data
Product Specification v4.1 (`--format odps-v4.1`). Bitol ODPS is bidirectional — a single ODPS
document, a directory bundle, or a lone ODCS file all converge on one validated FLUID contract. The
[ODPS guide](https://agenticstiger.github.io/forge_docs/cli/odps.html) covers both directions.

## Finding your way around the CLI

Everyday work is three commands: `validate`, `plan`, `apply`. `fluid --help` groups the rest the same
way as the table below, and every command takes `-h`.

| Group | Commands | What it is for |
|---|---|---|
| Core | `init` `forge` `validate` `plan` `apply` | Scaffold a product, check it, preview the change, deploy it. |
| Generate | `generate transformation` `generate schedule` `generate ci` `generate iac` `generate standard` | dbt and SQL; Airflow, Dagster or Prefect DAGs; CI pipelines for seven systems; OpenTofu; open-standard exports. |
| Governance | `policy-check` `policy-compile` `test` `verify` | Lint the contract, compile policy to native IAM, test against live data, check a deployment after the fact. |
| Agents | `mcp serve` `mcp output-port serve` `mission` | Serve the tooling to an MCP client; serve data through the policy gate; run missions whose success criteria are verified deterministically rather than judged by a model. |
| Brownfield | `import` `split` `publish` `market` | Bring in an existing dbt, Terraform or SQL project; split a contract into fragments; publish to a catalog; search catalogs for products that already exist. |
| Safety | `bundle` `diff` `rollback` `verify-signature` | Snapshot and restore around an apply; verify a bundle's cosign signature and SLSA attestation. |
| Utilities | `config` `auth` `doctor` `providers` `exporters` `version` | Credentials, health checks, and what the installed release supports. |

Production runs a longer sequence than three commands, and `fluid generate ci` emits it rather than
asking anyone to wire it by hand:

```
bundle → validate → generate-artifacts → validate-artifacts → diff →
plan → apply → policy-apply → verify → publish → schedule-sync
```

Stages 6 and 7 are joined by the digest check shown above, so plan and apply cannot drift apart
inside a pipeline any more than they can by hand. `fluid generate ci --system` takes `jenkins`,
`github`, `gitlab`, `azure-devops`, `bitbucket`, `circleci` or `tekton`; the Jenkins template
parameterises every stage, the apply mode and the publish targets so an operator picks them from a
dialog instead of editing Groovy. The
[pipeline walkthrough](https://agenticstiger.github.io/forge_docs/walkthrough/11-stage-pipeline.html)
covers every stage and the six apply modes.

Where to go next in the documentation, depending on what you are trying to do:

- [Getting started](https://agenticstiger.github.io/forge_docs/getting-started/) — the local path, end to end.
- [Why FLUID](https://agenticstiger.github.io/forge_docs/why.html) and [versus the alternatives](https://agenticstiger.github.io/forge_docs/concepts/vs-alternatives.html) — where this sits next to dbt, Terraform and a catalog.
- [Governance and sovereignty](https://agenticstiger.github.io/forge_docs/advanced/governance.html) — the `sovereignty` block, and how policy compiles to native IAM.
- [agentPolicy](https://agenticstiger.github.io/forge_docs/concepts/agent-policy.html) and the [MCP output port walkthrough](https://agenticstiger.github.io/forge_docs/walkthrough/mcp-output-port.html) — serving data to agents.
- [`examples/`](https://github.com/Agenticstiger/forge-cli/tree/main/examples) — 29 runnable contracts, from `01-hello-world` to multi-cloud lakehouses.

`fluid init --list-templates` shows the 14 bundled starting points. `hello-world` is the one the
quickstart above uses; `customer-360`, `incremental-processing`, `multi-source` and
`data-quality-validation` cover the common patterns.

## Contributing

Fork the repository, run `make setup` for a full development environment, and read
[CONTRIBUTING.md](https://github.com/Agenticstiger/forge-cli/blob/main/CONTRIBUTING.md) for the style
guide and architecture overview. The
[Code of Conduct](https://github.com/Agenticstiger/forge-cli/blob/main/CODE_OF_CONDUCT.md) applies
everywhere in the project.

Architecture notes for contributors are in
[AGENTS.md](https://github.com/Agenticstiger/forge-cli/blob/main/AGENTS.md); the coverage matrix of
what is tested against a unit, an emulator or live cloud is in
[HONESTLY_TESTED.md](https://github.com/Agenticstiger/forge-cli/blob/main/HONESTLY_TESTED.md); the
extension points — custom providers, exporters and LLM backends registered through Python entry
points — are in the
[SDK and plugins guide](https://agenticstiger.github.io/forge_docs/sdk-and-plugins/).

To report a security vulnerability, follow
[SECURITY.md](https://github.com/Agenticstiger/forge-cli/blob/main/SECURITY.md). Please do not open a
public issue for one.

## License

[Apache License 2.0](https://github.com/Agenticstiger/forge-cli/blob/main/LICENSE) · Copyright
2024–2026 Agentics Transformation Limited

[Documentation](https://agenticstiger.github.io/forge_docs/) ·
[PyPI](https://pypi.org/project/data-product-forge/) ·
[Issues](https://github.com/Agenticstiger/forge-cli/issues) ·
[Discussions](https://github.com/Agenticstiger/forge-cli/discussions) ·
[The book](https://www.amazon.com/dp/B0G4BBP65K)
