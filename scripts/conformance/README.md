# Multi-language MCP client conformance

The Fluid MCP output port is spec-conformant for the
[Model Context Protocol](https://modelcontextprotocol.io/). Any
SDK that speaks MCP correctly should connect, list tools, and
respect the `agentPolicy` gate.

This directory carries 3 conformance harnesses — one per non-Python
SDK that customers ask about — plus the matching CI matrix wiring.

## What each harness exercises

| Harness | SDK | Lifecycle | Allow scenario | Deny scenario |
|---|---|---|---|---|
| [`conformance_test.ts`](conformance_test.ts) | `@modelcontextprotocol/sdk` (TypeScript) | initialize → tools/list → tools/call | gpt-4o-mini in `--allow-models` returns rows | claude-3-opus returns `AgentPolicyDenied` |
| [`conformance_test.go`](conformance_test.go) | `github.com/mark3labs/mcp-go` (Go) | same | same | same |
| [`conformance_test.rs`](conformance_test.rs) | `rmcp` (Rust) | same | same | same |

The Python SDK has full integration tests in-tree
([`tests/output_ports/test_extensibility_and_runtime.py`](../../tests/output_ports/test_extensibility_and_runtime.py));
this directory is for the spec compatibility of the OTHER SDKs.

## CI integration

The `multi-lang-mcp-conformance` job in
[`.github/workflows/integration.yml`](../../.github/workflows/integration.yml)
runs all three harnesses on every nightly + workflow-dispatch
trigger. Each language gets its own job step that:

1. Installs the SDK + language toolchain.
2. Stands up the gateway against
   `examples/mcp-output-port-docker/contract.fluid.yaml`.
3. Runs the conformance script.
4. Fails the job on non-zero exit.

Per-step Docker layers are cached so the recurring cost is just
the SDK install (a few MB) per run, not the full toolchain
installation.

## Running locally

```bash
# Python (always present)
.venv/bin/python -m pytest tests/output_ports/test_extensibility_and_runtime.py -v

# TypeScript
npm install --no-save @modelcontextprotocol/sdk ts-node typescript
node --loader ts-node/esm scripts/conformance/conformance_test.ts \
  examples/mcp-output-port-docker/contract.fluid.yaml

# Go
cd /tmp && mkdir -p mcp-conformance && cd mcp-conformance
cp /path/to/forge-cli/scripts/conformance/conformance_test.go .
go mod init mcpconformance && go mod tidy
go run conformance_test.go /path/to/forge-cli/examples/mcp-output-port-docker/contract.fluid.yaml

# Rust
cd /tmp && cargo new --bin mcp-conformance && cd mcp-conformance
cp /path/to/forge-cli/scripts/conformance/conformance_test.rs src/main.rs
cargo add rmcp tokio anyhow serde_json --features tokio/full
cargo run -- /path/to/forge-cli/examples/mcp-output-port-docker/contract.fluid.yaml
```

## Adding a new SDK

1. Drop a `conformance_test.<lang>` script that implements the same
   3-step contract (initialize → tools/list → call with allow + deny).
2. Add a step to the `multi-lang-mcp-conformance` job in
   `integration.yml`.
3. Open a PR — we'll review the SDK's spec conformance and add it
   to the table above.
