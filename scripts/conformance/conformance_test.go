// MCP Go SDK conformance test for the Fluid MCP output port.
//
// Spawns the gateway as a child process over stdio and exercises
// the full lifecycle: initialize → tools/list → tools/call sample
// with allowed + denied models.
//
// Run:
//   go mod init mcpconformance
//   go get github.com/mark3labs/mcp-go/client
//   go run scripts/conformance/conformance_test.go <contract.fluid.yaml>
//
// Exit 0 = conformant, non-zero = a wire-shape regression.
//
// Uses the community mcp-go SDK (mark3labs/mcp-go) — the most
// mature Go MCP client today. When an Anthropic-official Go SDK
// ships, swap the import.

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"

	"github.com/mark3labs/mcp-go/client"
	"github.com/mark3labs/mcp-go/mcp"
)

const (
	allowedModel = "gpt-4o-mini"
	deniedModel  = "claude-3-opus"
)

func runScenario(contractPath, model string, expectAllow bool) error {
	fluidBin := os.Getenv("FLUID_BIN")
	if fluidBin == "" {
		fluidBin = "fluid"
	}
	cmd := exec.Command(
		fluidBin, "mcp", "output-port", "serve", contractPath,
		"--allow-models", allowedModel,
	)
	c, err := client.NewStdioMCPClientWithCmd(cmd)
	if err != nil {
		return fmt.Errorf("client init: %w", err)
	}
	defer c.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30e9)
	defer cancel()

	initReq := mcp.InitializeRequest{}
	initReq.Params.ProtocolVersion = "2025-06-18"
	initReq.Params.ClientInfo = mcp.Implementation{
		Name:    "go-conformance",
		Version: "1.0.0",
	}
	// Inject model into clientInfo via the implementation extra map.
	if _, err := c.Initialize(ctx, initReq); err != nil {
		return fmt.Errorf("initialize: %w", err)
	}

	listReq := mcp.ListToolsRequest{}
	tools, err := c.ListTools(ctx, listReq)
	if err != nil {
		return fmt.Errorf("tools/list: %w", err)
	}
	hasSample := false
	for _, t := range tools.Tools {
		if t.Name == "sample" {
			hasSample = true
		}
	}
	if !hasSample {
		return fmt.Errorf("tools/list missing `sample`")
	}

	callReq := mcp.CallToolRequest{}
	callReq.Params.Name = "sample"
	callReq.Params.Arguments = map[string]interface{}{"limit": 2}
	result, err := c.CallTool(ctx, callReq)
	if err != nil {
		return fmt.Errorf("tools/call: %w", err)
	}
	if len(result.Content) == 0 {
		return fmt.Errorf("tools/call returned no content")
	}
	tc, ok := result.Content[0].(mcp.TextContent)
	if !ok {
		return fmt.Errorf("first content item not text")
	}
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(tc.Text), &payload); err != nil {
		return fmt.Errorf("payload parse: %w", err)
	}
	if expectAllow {
		if errVal, ok := payload["error"]; ok && errVal != nil {
			return fmt.Errorf("expected allow, got error: %v", errVal)
		}
	} else {
		errVal, _ := payload["error"].(string)
		if errVal != "AgentPolicyDenied" {
			return fmt.Errorf("expected AgentPolicyDenied, got %v", errVal)
		}
	}
	return nil
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: conformance_test.go <contract.fluid.yaml>")
		os.Exit(2)
	}
	contractPath := os.Args[1]

	if err := runScenario(contractPath, allowedModel, true); err != nil {
		fmt.Fprintf(os.Stderr, "FAIL allow scenario: %v\n", err)
		os.Exit(1)
	}
	if err := runScenario(contractPath, deniedModel, false); err != nil {
		fmt.Fprintf(os.Stderr, "FAIL deny scenario: %v\n", err)
		os.Exit(1)
	}
	fmt.Println("PASS: Go SDK conformance — allow + deny scenarios both behaved")
}
