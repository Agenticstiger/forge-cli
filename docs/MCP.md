# MCP Server — `fluid mcp serve`

forge-cli ships an [MCP](https://modelcontextprotocol.io/) stdio
server that exposes the staged forge pipeline as tools for any
MCP-compatible client (Claude Code, Cursor, Continue, etc.). The
server speaks the standard MCP wire format over stdin/stdout — no
HTTP, no auth dance, no extra ports.

## What's exposed

| Tool | Read/Write | What it does |
|---|---|---|
| `read_logical_model` | read | Load a `<name>.fluid.yaml.model.json` sidecar and return the typed Logical IR (DV2 or Dimensional). |
| `validate_contract` | read | Run the FluidContractValidator against a contract path and return the structured `ValidationReport`. |
| `diff_models` | read | Structural diff between two `.model.json` sidecars (added/removed/renamed entities). |
| `search_semantic_memory` | read | Vector-search across `memory/semantic` for similar prior forged models. |
| `update_entity` | **write** | Patch a single entity (hub / link / satellite / fact / dimension) in a logical sidecar. |
| `add_relationship` | **write** | Add a foreign-key relationship between two entities. |
| `regenerate_physical` | **write** | Re-run the physical fan-out (Builder ∥ Readme ∥ Transformation) against a logical draft. |

Path-based read tools are scoped to the server's readable roots. Write
tools are gated by the policy model below.

## Access control

Every `tools/call` is checked against:

1. **`--read-only` flag.** When set, any tool that needs to mutate
   the contract / sidecar / store is rejected with a typed error.
   Default: write tools are visible, but the user has to opt in.
2. **Readable-paths whitelist.** `--readable-paths` accepts a
   comma-separated list of paths that path-based read tools may
   inspect. Default: current working directory.
3. **Writable-paths whitelist.** `--writable-paths` accepts a
   comma-separated list of paths the MCP server is allowed to
   mutate. Any write tool whose target falls outside the list is
   rejected. Defends against an upstream LLM that asks the server
   to overwrite `~/.fluid/ai_config.json` or similar.
4. **Tool-name allow/deny list.** `--allow-tools` / `--deny-tools`
   accept comma-separated tool names. `--deny-tools` wins over
   `--allow-tools` when both name the same tool.

The policy is built once at startup and applied to every
`tools/call`. The 41-test suite at
`tests/test_mcp_permission_policy.py` pins every branch of the
matrix.

## Sample Claude Code config

Drop this into Claude Code's MCP settings (typically
`~/.config/claude-code/mcp_servers.json` or the IDE's MCP panel):

```json
{
  "mcpServers": {
    "fluid-forge": {
      "command": "fluid",
      "args": [
        "mcp",
        "serve",
        "--read-only",
        "--readable-paths",
        "${WORKSPACE}/forge-output",
        "--writable-paths",
        "${WORKSPACE}/forge-output"
      ],
      "env": {
        "FLUID_LLM_PROVIDER": "anthropic",
        "FLUID_QUIET": "1"
      }
    }
  }
}
```

* `--read-only` blocks every write tool by default; remove the flag
  if you want Claude Code to actually patch your contracts.
* `--readable-paths` scopes model/contract reads to a single
  directory under your workspace.
* `--writable-paths` scopes any allowed writes to a single
  directory under your workspace — defence-in-depth even when the
  read-only flag is dropped.
* `FLUID_QUIET=1` silences the v2-preview banner so it doesn't
  pollute the MCP wire format.

## Sample Cursor config

```json
{
  "mcp.servers": {
    "fluid-forge": {
      "command": "fluid",
      "args": ["mcp", "serve"],
      "env": { "FLUID_LLM_PROVIDER": "openai", "FLUID_QUIET": "1" }
    }
  }
}
```

Cursor reads the same config shape; the only difference is the
top-level key (`mcp.servers` vs `mcpServers`).

## Client certification

Before cutting a release, run the real-client certification script:

```bash
PYTHONPATH=. python scripts/mcp_client_certify.py
```

It performs three checks:

1. Direct JSON-RPC lifecycle smoke (`initialize`, `tools/list`,
   `tools/call`).
2. MCP Inspector CLI `tools/list` and `tools/call` against the stdio
   server, when `npx` is installed.
3. Claude Code project-config health check via `claude mcp get`, when
   the `claude` CLI is installed.

Use `--json` for machine-readable CI output. Optional clients are
reported as `skip`, not `fail`; protocol failures from installed
clients fail the script.

## Versioning + audit trail

Every write tool that mutates an artefact bumps the artefact's
`meta.version` and stores the previous version under
`~/.fluid/store/history/<contract_hash>/<n>.json`. This is the
file-based audit trail the plan promises — no DB, no separate
service, just lazy versioned snapshots. List with:

```bash
fluid memory show history --contract customer_orders
```

The retention policy is `keep last N` (default `N=10`); override
via `FLUID_HISTORY_KEEP=N` or per-call.

## Tool schemas

Every tool's input schema is the corresponding Pydantic model from
`fluid_build/copilot/schemas/`. So `update_entity`'s schema is
literally `EntityPatch.model_json_schema()` — the MCP client gets
the same validation guarantees as the staged pipeline. A future
v2.x bump will rename the Pydantic models with backward-compat
shims so existing MCP clients keep working.

## Wire-format quirks

* **stdio only.** No HTTP transport in v1.0. SSE/HTTP comes in
  v1.6+ when we have a clear use case (today's MCP clients all
  speak stdio).
* **Logging goes to stderr.** stdout is reserved for MCP frames; a
  stray `print()` in any imported module would corrupt the wire.
  All logging in `cli/mcp.py` and the tool-call paths uses
  `logging.getLogger(...)` configured at warning level by default.
* **Tools are idempotent.** Calling `update_entity` twice with the
  same patch produces the same final state. The repair-loop logic
  inside the staged pipeline already handles this — we don't add a
  second idempotency layer at the MCP boundary.

## Disabling the server

Don't run `fluid mcp serve` and the server doesn't exist. There's
no daemon, no socket, no startup hook. The server is a foreground
process that exits when stdin closes — kill the parent process
(your IDE) and the server vanishes with it.
