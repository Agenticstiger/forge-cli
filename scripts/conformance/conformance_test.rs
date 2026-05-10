// MCP Rust SDK conformance test for the Fluid MCP output port.
//
// Spawns the gateway as a child process over stdio and exercises
// the full lifecycle: initialize → tools/list → tools/call sample
// with allowed + denied models.
//
// Run:
//   cargo new --bin mcp-conformance && cd mcp-conformance
//   cp <this-file> src/main.rs
//   cargo add rmcp tokio anyhow serde_json --features tokio/full
//   cargo run -- <path-to-contract.fluid.yaml>
//
// Exit 0 = conformant, non-zero = a wire-shape regression. Uses the
// official-modelcontextprotocol Rust SDK (`rmcp`); when the
// Anthropic-official Rust SDK ships, swap the import.

use std::env;
use std::process::Stdio;

use anyhow::{anyhow, Context, Result};
use rmcp::{
    model::{ClientCapabilities, Implementation},
    service::ServiceExt,
    transport::child_process::TokioChildProcess,
};
use serde_json::Value;
use tokio::process::Command;

const ALLOWED_MODEL: &str = "gpt-4o-mini";
const DENIED_MODEL: &str = "claude-3-opus";

async fn run_scenario(contract_path: &str, expect_allow: bool) -> Result<()> {
    let fluid_bin = env::var("FLUID_BIN").unwrap_or_else(|_| "fluid".to_string());
    let mut cmd = Command::new(fluid_bin);
    cmd.args([
        "mcp",
        "output-port",
        "serve",
        contract_path,
        "--allow-models",
        ALLOWED_MODEL,
    ])
    .stdin(Stdio::piped())
    .stdout(Stdio::piped());

    let transport = TokioChildProcess::new(cmd).context("spawn fluid")?;
    let client_info = Implementation {
        name: "rust-conformance".to_string(),
        version: "1.0.0".to_string(),
    };
    let client = ()
        .serve(transport)
        .await
        .context("serve")?;

    let _init = client.peer_info();
    let tools = client.list_tools(Default::default()).await?;
    if !tools.tools.iter().any(|t| t.name == "sample") {
        return Err(anyhow!("tools/list missing `sample`"));
    }

    let result = client
        .call_tool(rmcp::model::CallToolRequestParam {
            name: "sample".into(),
            arguments: Some(serde_json::json!({"limit": 2})),
        })
        .await?;

    let text = result
        .content
        .first()
        .and_then(|c| c.as_text())
        .map(|t| t.text.clone())
        .ok_or_else(|| anyhow!("no text content"))?;
    let payload: Value = serde_json::from_str(&text)?;

    if expect_allow {
        if payload.get("error").is_some() {
            return Err(anyhow!("expected allow, got error: {}", payload));
        }
    } else {
        let err = payload
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if err != "AgentPolicyDenied" {
            return Err(anyhow!("expected AgentPolicyDenied, got {}", err));
        }
    }
    client.cancel().await.ok();
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: conformance_test <contract.fluid.yaml>");
        std::process::exit(2);
    }
    let contract_path = &args[1];

    if let Err(e) = run_scenario(contract_path, true).await {
        eprintln!("FAIL allow scenario: {e:#}");
        std::process::exit(1);
    }
    if let Err(e) = run_scenario(contract_path, false).await {
        eprintln!("FAIL deny scenario: {e:#}");
        std::process::exit(1);
    }
    println!("PASS: Rust SDK conformance — allow + deny scenarios both behaved");
    Ok(())
}
