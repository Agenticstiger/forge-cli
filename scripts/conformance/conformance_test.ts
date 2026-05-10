// MCP TypeScript SDK conformance test for the Fluid MCP output port.
//
// Spawns the gateway as a child process over stdio (matching what
// Claude Desktop and Cursor do) and exercises the full lifecycle:
// initialize → tools/list → tools/call sample with allowed model →
// tools/call sample with denied model.
//
// Run:
//   npm install --no-save @modelcontextprotocol/sdk
//   node --loader ts-node/esm scripts/conformance/conformance_test.ts \
//     <path-to-contract.fluid.yaml>
//
// Exit 0 = conformant, non-zero = a wire-shape regression. CI runs
// this against the docker example contract.

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const contractPath = process.argv[2];
if (!contractPath) {
  console.error("usage: conformance_test.ts <contract.fluid.yaml>");
  process.exit(2);
}

async function main(): Promise<number> {
  const ALLOWED_MODEL = "gpt-4o-mini";
  const DENIED_MODEL = "claude-3-opus";

  // Scenario 1: allowed model → sample succeeds
  const allowedClient = new Client(
    { name: "ts-conformance", version: "1.0.0", model: ALLOWED_MODEL },
    { capabilities: {} },
  );
  const transport1 = new StdioClientTransport({
    command: process.env.FLUID_BIN || "fluid",
    args: ["mcp", "output-port", "serve", contractPath, "--allow-models", ALLOWED_MODEL],
    env: process.env as Record<string, string>,
  });
  await allowedClient.connect(transport1);
  const allowedTools = await allowedClient.listTools();
  if (!allowedTools.tools.find(t => t.name === "sample")) {
    console.error("FAIL: tools/list did not advertise `sample`");
    return 1;
  }
  const allowedResult = await allowedClient.callTool({
    name: "sample",
    arguments: { limit: 2 },
  });
  const allowedPayload = JSON.parse((allowedResult.content[0] as any).text);
  if (allowedPayload.error) {
    console.error(`FAIL: allowed model got error: ${allowedPayload.error}`);
    return 1;
  }
  if (!allowedPayload.rowCount || allowedPayload.rowCount < 1) {
    console.error("FAIL: allowed model received zero rows");
    return 1;
  }
  await allowedClient.close();

  // Scenario 2: denied model → AgentPolicyDenied envelope
  const deniedClient = new Client(
    { name: "ts-conformance-denied", version: "1.0.0", model: DENIED_MODEL },
    { capabilities: {} },
  );
  const transport2 = new StdioClientTransport({
    command: process.env.FLUID_BIN || "fluid",
    args: ["mcp", "output-port", "serve", contractPath, "--allow-models", ALLOWED_MODEL],
    env: process.env as Record<string, string>,
  });
  await deniedClient.connect(transport2);
  const deniedResult = await deniedClient.callTool({
    name: "sample",
    arguments: { limit: 2 },
  });
  const deniedPayload = JSON.parse((deniedResult.content[0] as any).text);
  if (deniedPayload.error !== "AgentPolicyDenied") {
    console.error(
      `FAIL: denied model expected AgentPolicyDenied, got: ${deniedPayload.error}`,
    );
    return 1;
  }
  if (deniedPayload.reason !== "not-in-allowedModels") {
    console.error(`FAIL: denied reason wrong: ${deniedPayload.reason}`);
    return 1;
  }
  await deniedClient.close();

  console.log("PASS: TypeScript SDK conformance — allow + deny scenarios both behaved");
  return 0;
}

main()
  .then(code => process.exit(code))
  .catch(err => {
    console.error("CONFORMANCE CRASH:", err);
    process.exit(1);
  });
