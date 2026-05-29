# MCP Output Port — minimal demo

Three-step demo of `fluid mcp output-port serve` against a local CSV.
No cloud credentials required.

## 1. List the exposes

```bash
fluid mcp output-port list examples/mcp-output-port/contract.fluid.yaml
```

You should see a single expose `customer_segments` with engine
`local/csv`, semantics, and an `expose.mcp` overrides block.

## 2. Preflight the binding

```bash
fluid mcp output-port doctor examples/mcp-output-port/contract.fluid.yaml
```

The doctor loads the DuckDB driver and runs a `SELECT 1` health
check. A green check on every line means the server will start
cleanly.

## 3. Serve over MCP stdio

```bash
fluid mcp output-port serve examples/mcp-output-port/contract.fluid.yaml
```

`--expose-id` is omitted because there is exactly one expose in
the contract; the server logs `auto-selected expose
'customer_segments'` on startup.

In another terminal, drive it with the MCP Inspector CLI:

```bash
npx -y @modelcontextprotocol/inspector --cli --transport stdio \
  --method tools/list \
  -- fluid mcp output-port serve examples/mcp-output-port/contract.fluid.yaml
```

You should see four tools advertised: `describe`, `sample`, `query`
(predeclared semantic), and `query_sql` (only with `--allow-sql`).

## 4. See governance in action (the point)

`email` is marked `sensitivity: pii` in the contract, so the gateway
redacts its **values** on every result while keeping the column
visible. Call `sample`:

```bash
npx -y @modelcontextprotocol/inspector --cli --transport stdio \
  --method tools/call --tool-name sample --tool-arg limit=2 \
  -- fluid mcp output-port serve examples/mcp-output-port/contract.fluid.yaml
```

```jsonc
{
  "columns": ["customer_id", "email", "segment", "signup_date", "lifetime_value_usd"],
  "rows": [
    {"customer_id": "C-0001", "email": "[REDACTED-PII]", "segment": "enterprise", ...},
    {"customer_id": "C-0002", "email": "[REDACTED-PII]", "segment": "smb", ...}
  ]
}
```

The agent learns the field exists but never sees a real address — and
the same masking applies to `query` / `query_sql` results, so it can't
be aliased away (`SELECT email AS x` is rejected at compile time). No
flag, no proxy, no code: governance comes from the contract. This is
the whole value proposition in one call.

## Wire to Claude Code

Drop this into `~/.config/claude-code/mcp_servers.json`:

```json
{
  "mcpServers": {
    "customer-segments-demo": {
      "command": "fluid",
      "args": [
        "mcp", "output-port", "serve",
        "/abs/path/to/forge-cli/examples/mcp-output-port/contract.fluid.yaml"
      ],
      "env": { "FLUID_QUIET": "1" }
    }
  }
}
```

Ask Claude: *"Sample the customer_segments table and show ltv_total
grouped by segment."*
