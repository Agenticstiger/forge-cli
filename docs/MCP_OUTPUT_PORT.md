# MCP Output Port — `fluid mcp output-port serve`

> **First 5 minutes:** clone this repo, then run
> `fluid mcp output-port doctor examples/mcp-output-port/contract.fluid.yaml`.
> If it prints ✅, run `fluid mcp output-port serve
> examples/mcp-output-port/contract.fluid.yaml` and pipe stdin/stdout
> to any MCP client. The example uses a 8-row CSV — no cloud
> credentials needed.

forge-cli ships a second [MCP](https://modelcontextprotocol.io/)
stdio server that turns a single FLUID `expose` block into an
agent-consumable endpoint. Where `fluid mcp serve` (see
[MCP.md](MCP.md)) gives MCP clients the **forge authoring** tool
surface (read/diff/regenerate contracts), `fluid mcp output-port
serve` gives them the **data product consumption** surface — describe
the schema, sample rows, run a predeclared semantic query.

## Subcommands

| Command | What it does |
|---|---|
| `fluid mcp output-port list <contract>` | Print every expose in the contract — id, kind, engine, table reference. Use this to find the right `--expose-id`. Add `--json` for machine output. |
| `fluid mcp output-port doctor <contract> [--expose-id <id>]` | Preflight: load the driver, run a cheap engine health check, print the resolved binding. Run before wiring `serve` to a client. |
| `fluid mcp output-port serve <contract> [--expose-id <id>]` | Start the stdio MCP server bound to one expose. `--expose-id` is optional when the contract has exactly one expose; the server auto-picks and logs the choice. |

```
┌──────────────────────────────────┐         ┌──────────────────────────────────┐
│ fluid mcp serve                  │         │ fluid mcp output-port serve      │
│                                  │         │                                  │
│ Authoring surface                │         │ Consumption surface              │
│ ─────────────────                │         │ ─────────────────                │
│ • read_logical_model             │         │ • describe                       │
│ • update_entity                  │         │ • sample                         │
│ • regenerate_physical            │         │ • query (semantic, predeclared)  │
│ • validate_contract              │         │ • query_sql (gated)              │
│ • forge_from_source              │         │ • resources/list                 │
│                                  │         │                                  │
│ Threat model: filesystem writes  │         │ Threat model: production data    │
│ Identity: developer              │         │ Identity: agent-end-user         │
└──────────────────────────────────┘         └──────────────────────────────────┘
```

## What's exposed

For one `expose` block at a time, the server advertises:

| Tool | Description |
|---|---|
| `describe` | Returns the bound expose's contract — schema, semantic model, QoS, classification, lineage hints, binding location. No engine round-trip. |
| `sample` | Returns up to `--max-sample-rows` rows. Column-level restrictions from `expose.policy.authz.columnRestrictions` are enforced; restricted columns are dropped. |
| `query` | Predeclared semantic query — pick a metric or measure from `expose.semantics`, group by zero or more dimensions, optionally filter on dimension keys. Compiled to parameterised SQL; the LLM never authors raw SQL. The measure column is aliased to the **metric** name when you ask for a metric, so two metrics over one measure stay distinguishable. A grouped result is ordered by the measure descending (then by each grouping key) and reports `truncated: true` when the `LIMIT` clipped it — so "revenue by day" gives you a reproducible top-N, not an arbitrary slice labelled complete. |
| `query_sql` | **Gated** — only advertised when `--allow-sql` is set. Caller-supplied SQL is checked through the SQL-safety allowlist before execution. |

The server also speaks MCP `resources/list` + `resources/read`, so an
agent can browse the contract YAML, the JSON expose, and the
semantic model as MCP resources without spending a `tools/call`.

## Engine drivers

The server picks a driver based on `expose.binding.platform` /
`expose.binding.format`:

| `(platform, format)` | Driver | Extra |
|---|---|---|
| `(local, csv)` / `(local, parquet)` / `(local, json)` | DuckDB | `pip install 'data-product-forge[local]'` |
| `(gcp, bigquery_table)` | BigQuery | `pip install 'data-product-forge[gcp]'` |
| `(snowflake, snowflake_table)` | Snowflake | `pip install 'data-product-forge[snowflake]'` |

Out-of-tree drivers register themselves at runtime with
`fluid_build.output_ports.mcp.register_driver(("databricks",
"delta_table"), DatabricksDriver)`.

## Quick start (DuckDB, no cloud creds)

Stand up an example expose:

```bash
cat > /tmp/demo-contract.fluid.yaml <<'EOF'
fluidVersion: "0.7.3"
kind: DataProduct
id: gold.demo.customers_v1
name: Demo customers
metadata:
  layer: Gold
  owner: { team: demo, email: demo@example.com }
  businessContext: { domain: Demo }
exposes:
  - exposeId: customer_profiles
    kind: table
    contract:
      schema:
        - { name: customer_id, type: STRING, required: true }
        - { name: email, type: STRING }
        - { name: signup_date, type: DATE }
    binding:
      platform: local
      format: csv
      location: { path: /tmp/customers.csv, table: customer_profiles }
    semantics:
      name: customer_profiles
      measures:
        - { name: customer_count, agg: count_distinct, expr: customer_id }
      dimensions:
        - { name: signup_date, type: time }
      metrics:
        - { name: active_customers, type: simple, measure: customer_count }
EOF
cat > /tmp/customers.csv <<'EOF'
customer_id,email,signup_date
C0001,alice@example.com,2024-01-15
C0002,bob@example.com,2024-02-10
C0003,carol@example.com,2024-03-05
EOF

# Start the server (stdio):
fluid mcp output-port serve /tmp/demo-contract.fluid.yaml --expose-id customer_profiles
```

Drive it with the MCP Inspector CLI in another terminal:

```bash
npx -y @modelcontextprotocol/inspector-cli@latest -- \
  fluid mcp output-port serve /tmp/demo-contract.fluid.yaml --expose-id customer_profiles
```

## Sample Claude Code config

`~/.config/claude-code/mcp_servers.json`:

```json
{
  "mcpServers": {
    "customer-profiles": {
      "command": "fluid",
      "args": [
        "mcp",
        "output-port",
        "serve",
        "${WORKSPACE}/contracts/customer_360.fluid.yaml",
        "--expose-id", "customer_profiles",
        "--max-sample-rows", "50"
      ],
      "env": {
        "FLUID_QUIET": "1"
      }
    }
  }
}
```

* `--max-sample-rows` defends against agents that ask for a
  full-table dump.
* No `--allow-sql` — Claude calls `query` with a metric name and
  the server compiles parameterised SQL on its behalf.
* `FLUID_QUIET=1` silences the v2-preview banner so it doesn't
  pollute the JSON-RPC wire.

## Sample Cursor config

```json
{
  "mcp.servers": {
    "customer-profiles": {
      "command": "fluid",
      "args": [
        "mcp", "output-port", "serve",
        "${WORKSPACE}/contracts/customer_360.fluid.yaml",
        "--expose-id", "customer_profiles"
      ],
      "env": { "FLUID_QUIET": "1" }
    }
  }
}
```

## Access control

Every `tools/call` is checked against:

1. **Tool allow/deny list.** `--allow-tools` / `--deny-tools`
   accept comma-separated tool names. Denial wins. Denied tools
   are also hidden from `tools/list` so upstream agents don't
   advertise them.
2. **Free-form-SQL gate.** `--allow-sql` is required to advertise
   the `query_sql` tool; without the flag, calls return a typed
   permission error.
3. **Sample row cap.** `--max-sample-rows N` (default 100)
   bounds every `sample` call. Asking for more than the cap
   silently returns the cap.
4. **Column-level masking.** Columns listed in
   `expose.policy.authz.columnRestrictions` with `access: deny`
   are dropped from `sample` projections, and **rejected outright**
   on `query` / `query_sql`. Rejecting rather than dropping is
   deliberate: the semantic layer aliases its projection, so a
   measure `{name: avg_balance, agg: avg, expr: account_balance}`
   would otherwise serve statistics over a denied column under a
   name the drop step never recognises, and `filters:
   {account_balance: 9999.99}` is an inference oracle over the
   column's values even though it never appears in the projection.
   The denial applies to a measure's `expr`, a dimension's `expr`,
   a metric's `filter`, and any caller filter key.
5. **Privacy masking.** Columns listed in
   `expose.policy.privacy.masking` are dropped today (Phase 1);
   Phase 2 emits the engine-specific masking expression so
   masked values flow back hashed / tokenised / encrypted.
6. **PII / PHI redaction.** Columns marked `sensitivity: pii`
   (or `phi` / `sensitive`) in `expose.contract.schema` stay
   VISIBLE but their values are replaced with `[REDACTED-PII]`, so
   an agent can still aggregate over them (`COUNT(DISTINCT
   customer_email)`) without ever seeing an address. Redaction
   keys off the underlying **column expression**, not the output
   name: a dimension `{name: seg_alias, expr: market_segment}` is
   redacted exactly like `{name: market_segment}`. A
   value-revealing aggregate (`min` / `max` / `median` /
   `percentile`) over a PII column returns a real cell value, so
   it is redacted too; `count` / `count_distinct` / `sum` / `avg`
   are not.
7. **Model + use-case gate (`agentPolicy`).** When an expose
   declares `policy.agentPolicy.{allowedModels, deniedModels,
   allowedUseCases, deniedUseCases}` — or an operator passes
   `--allow-models` / `--deny-models` / `--allow-use-cases` /
   `--deny-use-cases` — every `tools/call` is evaluated against
   the caller's declared identity. **Caller identity is not part
   of the MCP spec**: the `initialize` request's `Implementation`
   object carries `{name, version}`, so the gateway reads the
   model from a non-standard `model` field (and the use case from
   `useCase`) inside `clientInfo`, or from cryptographically
   verified `fluid_auth_attrs` when the HTTP transport's auth
   middleware is enabled. A client that sends neither is denied
   with `missing-model-identity` — fail-closed, including on the
   denylist-only case, because otherwise a denied model would slip
   the gate by omitting the field. **When no model policy is
   declared at all the gate is inert**, so an off-the-shelf client
   (Claude Code, Cursor, the MCP Inspector) works out of the box.

The 0.7.3 schema also lets each contract author pin MCP-specific
overrides via a new `expose.mcp` block:

```yaml
exposes:
  - exposeId: customer_profiles
    mcp:
      sampling: { maxRows: 50, redactPII: true }
      classification: { dataClass: confidential }
      allowFreeFormSql: false
      auth: { mode: oidc, issuer: "https://auth.example.com/", audience: "mcp.customer.profiles" }
```

The server reads `expose.mcp` at startup and applies the overrides
on top of the CLI flags. CLI overrides take precedence so an
operator can temporarily widen or narrow the surface for incident
response without editing the contract.

## Audit trail

Every `tools/call` writes a JSON document to
`~/.fluid/store/audit/` via
`fluid_build.copilot.store.audit_trail.write_audit_event`. The
document records the tool name, expose id, contract path,
`elapsedMs`, and a sanitised argument summary (long SQL bodies are
truncated to 256 characters). Forensic operators consume the
trail with the existing `AuditReportGenerator`.

## Client certification

Before cutting a release, run the cert script:

```bash
PYTHONPATH=. python scripts/mcp_output_port_certify.py
```

The script:

1. Drives the stdio server with a six-message JSON-RPC sequence
   (initialize → tools/list → describe → sample → query →
   resources/list).
2. Runs the MCP Inspector CLI's `tools/list` against the server
   when `npx` is on PATH.
3. Calls `claude mcp list` when the Claude Code CLI is installed.

Use `--json` for machine-readable output. Optional clients are
reported as `skipped` rather than failing so the same script runs
in dev sandboxes and release CI.

## Error reporting

Every refusal and every failure comes back as a normal `tools/call`
result carrying `isError: true` plus a JSON envelope with `error`,
`tool` and `message` — the shape the MCP spec prescribes, so agent
loops can branch on it and the model can see what went wrong.
`AgentPolicyDenied`, `RateLimitExceeded`, `CircuitOpen`,
`TokenBudgetExceeded`, `ToolNotAllowed`, `UnknownTool`,
`QueryValidationError` and engine failures all use it.

`QueryValidationError` messages are surfaced VERBATIM — they name
only contract-declared measures / metrics / dimensions / columns
the caller can already enumerate via `describe`, so an agent can
self-correct its next call. Engine and binding failures are
sanitised behind "see server audit trail" so the binding's
database / schema / table never reaches the model.

## Troubleshooting

**`exposeId 'X' not found in contract; available: [...]`** — typo or
the expose has been renamed. Run `fluid mcp output-port list <contract>`
to see the canonical ids.

**`Contract has N exposes; pass --expose-id to pick one. Available: [...]`** —
the auto-pick only triggers for single-expose contracts. Pick one
from the listed ids.

**`Object 'X' does not exist or not authorized` (Snowflake/BigQuery)** —
the bound table is missing or the connection's role can't see it.
The error envelope includes a `Hint:` line pointing at the relevant
binding fields. Verify `binding.location.{database,schema,table}`
(Snowflake) or `binding.location.{project,dataset,table}` (BigQuery)
and confirm the role/service-account has SELECT on the bound table.

**`duckdb is not installed; install via the 'local' extra`** —
`pip install 'data-product-forge[local]'`. Same shape for
`[gcp]` and `[snowflake]` extras when the corresponding driver
is the binding target.

**`Refusing to execute statement with injection marker`** /
**`Refusing to execute statement: rendered SQL body contains a
banned keyword`** — the driver's defence-in-depth guard fired
because the compiler emitted SQL with a forbidden marker
(`;`, `--`, `/*`, `*/`) or a banned body keyword (UNION /
DROP / etc.). This indicates a regression in
`compile_free_form_sql`; report at
https://github.com/Agenticstiger/forge-cli/issues with the
`compiledSql` from the JSON-RPC response.

**`sql references column 'X' which is restricted by …`** — the
caller's `query_sql` references a column denied by
`expose.policy.authz.columnRestrictions` or covered by
`expose.policy.privacy.masking`. Aliasing (`SELECT email AS x`)
does NOT bypass the rule by design.

**Server appears to hang on startup** — the server is waiting on
stdin for JSON-RPC messages. When stderr is a TTY the CLI prints
`fluid mcp output-port: serving expose=… ready for MCP client on
stdio.` as a readiness cue. Pipe an MCP client (Claude Code,
Cursor, MCP Inspector) at it.

## What's next

Phase 2 lands multi-expose support (one server, all exposes from
one contract; tool names namespaced as `<exposeId>.<tool>`),
lineage / quality / metadata tools, and full audit-trail
integration. Phase 3 lands streamable HTTP transport and OAuth 2.1
PKCE; see `~/.claude/plans/i-want-to-pick-jaunty-cook.md` for the
phased roadmap.
