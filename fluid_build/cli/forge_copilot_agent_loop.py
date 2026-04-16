# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Multi-turn agent loop for forge copilot (slice UX-K).

Instead of cramming everything into a single prompt, the agent loop
lets the LLM call tools (``discover_workspace``, ``read_sample_schema``,
``list_templates``, ``propose_contract``, ``validate_contract``) on
demand across multiple turns.  This reduces input-token cost (~30%),
eliminates most repair retries (the LLM can call ``validate_contract``
itself), and unlocks parallel tool dispatch for read-only tools.

The loop is opt-in via ``fluid forge --agent-loop`` or
``FLUID_COPILOT_AGENT_LOOP=1``.  The default single-shot flow in
``generate_copilot_artifacts`` is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Mapping, Optional

import httpx

from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    LlmConfig,
    LlmProvider,
    get_llm_provider,
)
from fluid_build.cli.forge_copilot_tools import (
    dispatch_tool_call,
    get_tool_definitions,
)
from fluid_build.schema_manager import FluidSchemaManager

LOG = logging.getLogger("fluid.cli.forge_copilot.agent_loop")

# Read-only tools that can safely run in parallel.
_PARALLELIZABLE_TOOLS = frozenset({
    "discover_workspace",
    "read_sample_schema",
    "list_templates",
})

# Maximum number of LLM round-trips before we give up.
MAX_AGENT_ITERATIONS = 12

# After this many iterations, compact old tool results to stay within
# context window limits.  Configurable via FLUID_AGENT_COMPACT_AFTER.
_COMPACT_AFTER = int(os.environ.get("FLUID_AGENT_COMPACT_AFTER", "6"))
_COMPACT_KEEP_TAIL = 4  # Keep last N messages intact.
_COMPACT_MAX_CHARS = 500  # Truncate old tool results to this length.


def _compact_message_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Truncate old tool result messages to prevent context window overflow.

    Keeps the first message (original user context) and the last
    ``_COMPACT_KEEP_TAIL`` messages intact.  Messages in between have
    their content truncated to ``_COMPACT_MAX_CHARS`` characters.
    """
    if len(messages) <= _COMPACT_KEEP_TAIL + 1:
        return messages

    head = messages[:1]
    tail = messages[-_COMPACT_KEEP_TAIL:]
    middle = messages[1:-_COMPACT_KEEP_TAIL]

    compacted_middle: List[Dict[str, Any]] = []
    for msg in middle:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > _COMPACT_MAX_CHARS:
            msg = dict(msg)
            msg["content"] = content[:_COMPACT_MAX_CHARS] + f" [truncated — {len(content)} chars total]"
        elif isinstance(content, list):
            # Anthropic-style content blocks — truncate text blocks.
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if len(text) > _COMPACT_MAX_CHARS:
                        block = dict(block)
                        block["text"] = text[:_COMPACT_MAX_CHARS] + f" [truncated — {len(text)} chars total]"
                new_blocks.append(block)
            msg = dict(msg)
            msg["content"] = new_blocks
        compacted_middle.append(msg)

    before = sum(len(json.dumps(m.get("content", ""))) for m in messages)
    result = head + compacted_middle + tail
    after = sum(len(json.dumps(m.get("content", ""))) for m in result)
    LOG.debug(
        "Compacted message history: %d messages, %d→%d chars",
        len(result), before, after,
    )
    return result


# System prompt for the agent loop — much shorter than the single-shot
# prompt because the LLM discovers information via tools instead of
# receiving it up front.


def _build_agent_system_prompt() -> str:
    fv = FluidSchemaManager.latest_bundled_version()
    return (
        "You are FLUID Forge Copilot, running in agent mode.\n"
        "Use the available tools to understand the user's workspace, choose "
        "the right template and provider, build a contract, and validate it.\n\n"
        "PLANNING: Before calling any tools, state a brief plan (2-4 sentences):\n"
        "- What you already know from the user's context\n"
        "- What information is missing and needs discovery\n"
        "- Which tools you will call and in what order\n"
        "- Your strategy for building the contract\n"
        "You may emit your plan text alongside your first tool calls in the same response.\n\n"
        "REASONING: Before each major decision, briefly state your reasoning.\n"
        "When returning the final contract, include a 'reasoning' key explaining your choices.\n\n"
        "Workflow:\n"
        "1. Call discover_workspace to scan for data files and existing contracts.\n"
        "2. Call list_templates to see available templates and providers.\n"
        "3. Optionally call read_sample_schema on interesting data files.\n"
        "4. Call propose_contract with the user's context to get a seed.\n"
        "5. Refine the seed based on discovery results.\n"
        "6. Call validate_contract to check for errors.\n"
        "7. If there are validation errors, fix the contract and re-validate.\n"
        "8. When the contract is valid, return your final response as a JSON "
        "object with keys: recommended_template, recommended_provider, "
        "recommended_patterns, architecture_suggestions, best_practices, "
        "technology_stack, description, domain, owner, readme_markdown, "
        "contract, additional_files.\n\n"
        f"CRITICAL: The contract must be a valid FLUID {fv} DataProduct contract.\n"
        f"Use fluidVersion '{fv}'. Only use providers and templates from list_templates.\n"
        "Never include secrets or raw sample data in your response.\n"
        "When you're ready to deliver, stop calling tools and return the final JSON directly."
    )


# Keep backward-compatible module-level name for any external references.
AGENT_SYSTEM_PROMPT = _build_agent_system_prompt()


def run_copilot_agent_loop(
    *,
    context: Mapping[str, Any],
    llm_config: LlmConfig,
    discovery_report: Any = None,
    project_memory: Any = None,
    capability_matrix: Optional[Mapping[str, Any]] = None,
    max_iterations: int = MAX_AGENT_ITERATIONS,
    console: Any = None,
    perf_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the multi-turn agent loop and return the final result dict.

    The return value has the same shape as
    ``CopilotGenerationResult`` field sources so callers can build
    the dataclass from it.

    Raises :class:`CopilotGenerationError` if the loop exhausts
    iterations without producing a valid contract.
    """
    provider_adapter = get_llm_provider(llm_config.provider)
    tools = get_tool_definitions()

    # Build the initial user message from the context.
    user_content = _build_initial_user_message(context, project_memory)
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": user_content},
    ]

    total_tool_calls = 0
    for iteration in range(max_iterations):
        LOG.debug("Agent loop iteration %d/%d", iteration + 1, max_iterations)

        # Compact old messages to stay within context window limits.
        if iteration >= _COMPACT_AFTER:
            messages = _compact_message_history(messages)

        # Call the LLM with the tool definitions.
        response_json = _call_llm_with_tools(
            provider_adapter, llm_config, _build_agent_system_prompt(), messages, tools
        )

        # Check for tool calls.
        tool_calls = provider_adapter.extract_tool_calls(response_json)

        if not tool_calls:
            # Slice UX-L: show the final-response indicator.
            if console:
                try:
                    console.print(
                        f"  [bold green]Round {iteration + 1}[/bold green]  Delivering contract"
                    )
                except Exception:  # noqa: BLE001
                    pass
            # No tool calls — the model is emitting its final response.
            text = provider_adapter.extract_text_from_tool_response(response_json)
            if text:
                try:
                    from fluid_build.cli.forge_copilot_runtime import extract_json_object
                    payload = extract_json_object(text)
                    # Slice UX-L: record final stats.
                    if perf_stats is not None:
                        perf_stats["agent_loop_rounds"] = iteration + 1
                        perf_stats["agent_loop_tool_calls"] = total_tool_calls
                    return payload
                except ValueError:
                    # The model returned text that isn't valid JSON.
                    # Ask it to try again.
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your response was not valid JSON. Please return "
                            "the final response as a strict JSON object with "
                            "the required keys."
                        ),
                    })
                    continue
            # Empty response — unusual but recoverable.
            messages.append({
                "role": "user",
                "content": "I didn't receive a response. Please continue.",
            })
            continue

        # Slice UX-L: show which tools are being called.
        total_tool_calls += len(tool_calls)
        tool_list = " → ".join(tc["name"] for tc in tool_calls)
        if console:
            try:
                console.print(
                    f"  [bold cyan]Round {iteration + 1}[/bold cyan]  {tool_list}"
                )
            except Exception:  # noqa: BLE001
                pass

        # Dispatch tool calls (parallel for read-only tools).
        results = _dispatch_tools(tool_calls)

        # Feed tool results back to the LLM.
        result_msgs = provider_adapter.build_tool_result_messages(
            tool_calls, results
        )
        messages.extend(result_msgs)

    # Slice UX-L: update perf stats even on failure.
    if perf_stats is not None:
        perf_stats["agent_loop_rounds"] = max_iterations
        perf_stats["agent_loop_tool_calls"] = total_tool_calls

    raise CopilotGenerationError(
        "copilot_agent_loop_exhausted",
        f"Agent loop did not produce a valid contract after {max_iterations} iterations.",
        suggestions=[
            "The model may be stuck in a tool-call loop",
            "Try with a different model or use the default single-shot flow",
            "Set FLUID_COPILOT_AGENT_LOOP=0 to disable agent mode",
        ],
    )


def _build_initial_user_message(
    context: Mapping[str, Any],
    project_memory: Any = None,
) -> str:
    """Build the first user message from the interview context."""
    parts = []
    if context.get("project_goal"):
        parts.append(f"Project goal: {context['project_goal']}")
    if context.get("data_sources"):
        parts.append(f"Data sources: {context['data_sources']}")
    if context.get("use_case"):
        parts.append(f"Use case: {context['use_case']}")
    if context.get("domain"):
        parts.append(f"Domain: {context['domain']}")
    if context.get("owner_team"):
        parts.append(f"Owner team: {context['owner_team']}")
    if context.get("provider"):
        parts.append(f"Preferred provider: {context['provider']}")

    if project_memory:
        try:
            mem_payload = project_memory.to_prompt_payload()
            parts.append(f"Project memory: {json.dumps(mem_payload, default=str)}")
        except Exception:  # noqa: BLE001
            pass

    if not parts:
        parts.append("Please help me create a FLUID data product contract.")

    parts.append(
        "\nPlease use tools to discover my workspace, choose the right "
        "template, build and validate a contract, then return the final result."
    )
    return "\n".join(parts)


def _call_llm_with_tools(
    provider: LlmProvider,
    config: LlmConfig,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Make one LLM call with tool definitions and return the raw response."""
    url, headers, payload = provider.build_tool_request(
        config, system_prompt, messages, tools
    )
    try:
        with httpx.Client(timeout=config.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise CopilotGenerationError(
            "copilot_agent_loop_request_failed",
            f"Agent loop LLM request failed ({exc.response.status_code}) "
            f"for {config.provider} model '{config.model}'.",
            suggestions=[
                "Check the model supports tool use",
                "Try --llm-model gpt-4o / claude-3-5-sonnet-latest / gemini-1.5-pro",
            ],
        ) from exc
    except httpx.HTTPError as exc:
        raise CopilotGenerationError(
            "copilot_agent_loop_network_error",
            f"Agent loop network error: {exc}",
        ) from exc


def _dispatch_tools(
    tool_calls: List[Dict[str, Any]],
) -> List[Any]:
    """Dispatch tool calls, running read-only tools in parallel."""
    if len(tool_calls) == 1:
        tc = tool_calls[0]
        return [dispatch_tool_call(tc["name"], tc["arguments"])]

    # Check if ALL calls are parallelizable.
    all_parallel = all(
        tc["name"] in _PARALLELIZABLE_TOOLS for tc in tool_calls
    )

    if all_parallel and len(tool_calls) > 1:
        # Submit all futures in parallel but collect results in input
        # order so that logging and downstream processing are
        # deterministic across runs.
        with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as pool:
            futures = [
                pool.submit(dispatch_tool_call, tc["name"], tc["arguments"])
                for tc in tool_calls
            ]
            results = [f.result() for f in futures]
        return results

    # Sequential fallback for mixed read/write calls.
    return [
        dispatch_tool_call(tc["name"], tc["arguments"])
        for tc in tool_calls
    ]
