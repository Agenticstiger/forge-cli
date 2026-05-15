# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
from pathlib import Path
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
_PARALLELIZABLE_TOOLS = frozenset(
    {
        "discover_workspace",
        "read_sample_schema",
        "list_templates",
    }
)

# Maximum number of LLM round-trips before we give up.
MAX_AGENT_ITERATIONS = 12
# Security: cap the tool-call batch per round. A poisoned or looping LLM
# response can otherwise request hundreds of parallel tool calls in one
# round — each result feeds back into the next LLM call, inflating cost,
# I/O, and workspace scans. With MAX_AGENT_ITERATIONS this bounds the
# worst case to a small, predictable number of tool invocations.
MAX_TOOL_CALLS_PER_ITERATION = 8


def _int_env(name: str, default: int) -> int:
    """Read an integer from the environment, falling back to ``default``.

    A malformed value (e.g. ``FLUID_AGENT_COMPACT_AFTER=foo``) used to crash
    the CLI at import time. We now log a warning and fall back to the
    default so a stray env var never breaks ``fluid forge --agent-loop``.
    See SECURITY_REVIEW S-014.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.warning("Invalid %s=%r; falling back to default of %d", name, raw, default)
        return default


# After this many iterations, compact old tool results to stay within
# context window limits.  Configurable via FLUID_AGENT_COMPACT_AFTER.
_COMPACT_AFTER = _int_env("FLUID_AGENT_COMPACT_AFTER", 6)
_COMPACT_KEEP_TAIL = 4  # Keep last N messages intact.
_COMPACT_MAX_CHARS = 500  # Truncate old tool results to this length.


def _compact_message_history(
    messages: List[Dict[str, Any]],
    *,
    capability_matrix: Optional[Dict[str, Any]] = None,
    summarizer: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Compact tool-heavy message history to prevent context overflow.

    Delegates to :mod:`fluid_build.cli.forge_copilot_compaction` so
    the strategy is pluggable: ``truncate`` (default, char-aware),
    ``summarize`` (LLM-backed), or ``hybrid``. Pick via
    ``capability_matrix["compaction_strategy"]`` or
    ``FLUID_COMPACTION_STRATEGY`` env. The legacy
    ``_COMPACT_KEEP_TAIL`` / ``_COMPACT_MAX_CHARS`` knobs are
    preserved as the truncate-strategy defaults so existing operators
    see the same behaviour unless they explicitly opt into the
    summarize / hybrid strategies.
    """
    from fluid_build.cli.forge_copilot_compaction import compact_messages

    before = sum(len(json.dumps(m.get("content", ""))) for m in messages)
    result = compact_messages(
        messages,
        capability_matrix=capability_matrix,
        summarizer=summarizer,
        keep_tail=_COMPACT_KEEP_TAIL,
        truncate_chars=_COMPACT_MAX_CHARS,
    )
    after = sum(len(json.dumps(m.get("content", ""))) for m in result)
    LOG.debug(
        "Compacted message history: %d messages, %d→%d chars",
        len(result),
        before,
        after,
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
        "PLANNING: Before calling any tools, state an intent plan (2-4 sentences):\n"
        "- What you already know from the user's context\n"
        "- What information is missing and needs discovery\n"
        "- Which tools you will call and in what order\n"
        "- Your strategy for building the contract\n"
        "You may emit your plan text alongside your first tool calls in the same response.\n\n"
        "REASONING: Before each major decision, intently state your reasoning.\n"
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
    workspace_root: Optional[Path] = None,
    preview_panel: Any = None,
    show_work: bool = False,
) -> Dict[str, Any]:
    """Run the multi-turn agent loop and return the final result dict.

    The return value has the same shape as
    ``CopilotGenerationResult`` field sources so callers can build
    the dataclass from it.

    Raises :class:`CopilotGenerationError` if the loop exhausts
    iterations without producing a valid contract.

    ``workspace_root`` (SECURITY_REVIEW S-003/S-004): the directory the
    copilot's path-accepting tools (``read_sample_schema``,
    ``discover_workspace``) are confined to. Defaults to ``Path.cwd()``
    when callers haven't plumbed it through — the CLI entry points
    should pass this explicitly from the user's ``--workspace`` or
    current directory.

    ``preview_panel`` (Phase 0.4, invariant **I1**): when supplied, every
    iteration appends a transcript event (tools called, response text,
    iteration index) and persists the artifact stack under
    ``.fluid/agents/<run-id>/``. A Ctrl-C anywhere in the loop leaves
    a recoverable record on disk — the next ``fluid forge --refine``
    can pick up where this run left off.

    ``show_work`` (Phase 0.4): when True, stream the agent's reasoning
    and tool-call decisions to the console as they happen. Independent
    of ``preview_panel`` — they can be combined or used separately.
    """
    provider_adapter = get_llm_provider(llm_config.provider)
    tools = get_tool_definitions()
    # Resolve the workspace root ONCE, at loop entry, so every tool
    # call within this loop sees the same canonical root.
    ws_root: Path = (workspace_root or Path.cwd()).resolve()

    # Build the default summarizer ONCE per loop. When the user opts
    # into ``FLUID_COMPACTION_STRATEGY=summarize`` (or ``hybrid``),
    # the compaction layer will invoke this on the middle-message
    # blob to compress prior turns into a single summary message
    # using the user's provider's fast tier (Haiku / 4.1-nano /
    # gemini-flash). Built lazily — the closure makes no LLM call
    # until compaction actually triggers.
    from fluid_build.cli.forge_copilot_default_summarizer import (
        build_default_summarizer,
    )

    default_summarizer = build_default_summarizer(llm_config)

    # Build the initial user message from the context.
    user_content = _build_initial_user_message(context, project_memory)
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": user_content},
    ]

    total_tool_calls = 0

    def _record_iteration(
        *,
        iteration_idx: int,
        kind: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        text: str = "",
    ) -> None:
        """Persist one agent loop event to the preview panel (I1).

        Called from inside the loop after every meaningful step so a
        Ctrl-C doesn't lose the trace. Best-effort — failures are
        logged at debug, never raised.
        """
        if preview_panel is None:
            return
        try:
            preview_panel.append_transcript(
                {
                    "kind": kind,
                    "iteration": iteration_idx + 1,
                    "tool_calls": [
                        {"name": tc.get("name"), "input": tc.get("input")}
                        for tc in (tool_calls or [])
                    ],
                    "text_excerpt": (text or "")[:1024],
                }
            )
            if text:
                preview_panel.append_reasoning(text)
            for tc in tool_calls or []:
                if tc.get("name"):
                    preview_panel.add_tool_call(str(tc["name"]))
            preview_panel.persist_artifacts()
        except Exception as exc:  # noqa: BLE001 — never crash the loop on telemetry
            LOG.debug("preview_panel_record_failed: %s", exc)

    for iteration in range(max_iterations):
        LOG.debug("Agent loop iteration %d/%d", iteration + 1, max_iterations)

        # Phase 0.5 / Gap #3 — honour ``:override`` from the interview
        # slash-command dispatcher. The interview sets
        # ``preview_panel.override_action`` (when supplied); we stop
        # the loop cleanly so the runtime can react instead of
        # running to the iteration cap.
        override = (
            getattr(preview_panel, "override_action", None) if preview_panel is not None else None
        )
        if override and override != "cancel":
            LOG.info("Agent loop honouring override: %s", override)
            if console:
                try:
                    console.print(
                        f"[yellow]Override received: {override} — "
                        "exporting current state and exiting the agent loop.[/yellow]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                preview_panel.append_transcript(
                    {
                        "kind": "override",
                        "iteration": iteration + 1,
                        "action": override,
                    }
                )
                preview_panel.persist_artifacts()
            except Exception:  # noqa: BLE001
                pass
            raise CopilotGenerationError(
                "copilot_agent_loop_overridden",
                f"Agent loop stopped by user override: {override}.",
                suggestions=[
                    "Re-run forge with the desired engine via --transform-engine",
                    "Use --refine to iterate on the partial contract under .fluid/agents/",
                ],
            )

        # Compact old messages to stay within context window limits.
        if iteration >= _COMPACT_AFTER:
            messages = _compact_message_history(
                messages,
                capability_matrix=dict(capability_matrix or {}),
                summarizer=default_summarizer,
            )

        # Call the LLM with the tool definitions. AGENT_SYSTEM_PROMPT is
        # cached at module-import time; FluidSchemaManager.latest_bundled_version()
        # doesn't change over the lifetime of a CLI invocation, so rebuilding
        # the prompt on every iteration just wastes work.
        response_json = _call_llm_with_tools(
            provider_adapter, llm_config, AGENT_SYSTEM_PROMPT, messages, tools
        )

        # Check for tool calls.
        tool_calls = provider_adapter.extract_tool_calls(response_json)

        # Security: bound the tool-call batch. A poisoned/looping LLM
        # response can otherwise dispatch an unbounded number of parallel
        # tool calls in a single round.
        if tool_calls and len(tool_calls) > MAX_TOOL_CALLS_PER_ITERATION:
            LOG.warning(
                "LLM requested %d tool calls in one round; truncating to %d",
                len(tool_calls),
                MAX_TOOL_CALLS_PER_ITERATION,
            )
            tool_calls = tool_calls[:MAX_TOOL_CALLS_PER_ITERATION]

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
            _record_iteration(iteration_idx=iteration, kind="final_response", text=text)
            if show_work and console and text:
                try:
                    console.print(f"[dim]  {text[:200]}{'…' if len(text) > 200 else ''}[/dim]")
                except Exception:  # noqa: BLE001
                    pass
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
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your response was not valid JSON. Please return "
                                "the final response as a strict JSON object with "
                                "the required keys."
                            ),
                        }
                    )
                    continue
            # Empty response — unusual but recoverable.
            messages.append(
                {
                    "role": "user",
                    "content": "I didn't receive a response. Please continue.",
                }
            )
            continue

        # Slice UX-L: show which tools are being called.
        total_tool_calls += len(tool_calls)
        tool_list = " → ".join(tc["name"] for tc in tool_calls)
        if console:
            try:
                console.print(f"  [bold cyan]Round {iteration + 1}[/bold cyan]  {tool_list}")
            except Exception:  # noqa: BLE001
                pass

        # Phase 0.4 (I1): record the planned tool call set BEFORE
        # dispatch so a Ctrl-C mid-tool leaves the intent on disk.
        _record_iteration(
            iteration_idx=iteration, kind="tool_calls_dispatched", tool_calls=tool_calls
        )
        if show_work and console:
            try:
                for tc in tool_calls:
                    name = tc.get("name", "?")
                    inp = tc.get("input") or {}
                    inp_summary = (
                        ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:3])
                        if isinstance(inp, dict)
                        else ""
                    )
                    console.print(f"[dim]  → {name}({inp_summary})[/dim]")
            except Exception:  # noqa: BLE001
                pass

        # Dispatch tool calls (parallel for read-only tools). Thread the
        # loop's workspace_root through so path-accepting tools can
        # confine the LLM's path argument (S-003 / S-004).
        results = _dispatch_tools(tool_calls, workspace_root=ws_root)

        # Feed tool results back to the LLM.
        result_msgs = provider_adapter.build_tool_result_messages(tool_calls, results)
        messages.extend(result_msgs)

        # World-class agent layer: structured corrective feedback for
        # any failed tool call. The legacy loop returned the typed
        # error dict to the LLM but never told the model *what to do
        # differently* — the LLM frequently retried the same broken
        # call until the iteration cap fired. The corrective messages
        # are deterministic per error class so behaviour is
        # reproducible and security-safe (no server-side state
        # quoted back to the model).
        from fluid_build.cli.forge_copilot_corrective_feedback import (
            build_corrective_messages,
        )

        messages.extend(build_corrective_messages(tool_calls, results))

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
    url, headers, payload = provider.build_tool_request(config, system_prompt, messages, tools)
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
                "Try --llm-model gpt-4.1-mini / claude-sonnet-4-6 / gemini-2.5-flash",
            ],
        ) from exc
    except httpx.HTTPError as exc:
        raise CopilotGenerationError(
            "copilot_agent_loop_network_error",
            f"Agent loop network error: {exc}",
        ) from exc


def _dispatch_tools(
    tool_calls: List[Dict[str, Any]],
    *,
    workspace_root: Optional[Path] = None,
) -> List[Any]:
    """Dispatch tool calls, running read-only tools in parallel.

    ``workspace_root`` is forwarded to every ``dispatch_tool_call`` so
    path-accepting tools can confine the LLM's path argument.
    """
    if len(tool_calls) == 1:
        tc = tool_calls[0]
        return [dispatch_tool_call(tc["name"], tc["arguments"], workspace_root=workspace_root)]

    # Check if ALL calls are parallelizable.
    all_parallel = all(tc["name"] in _PARALLELIZABLE_TOOLS for tc in tool_calls)

    if all_parallel and len(tool_calls) > 1:
        # Submit all futures in parallel but collect results in input
        # order so that logging and downstream processing are
        # deterministic across runs.
        with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as pool:
            futures = [
                pool.submit(
                    dispatch_tool_call,
                    tc["name"],
                    tc["arguments"],
                    workspace_root=workspace_root,
                )
                for tc in tool_calls
            ]
            results = [f.result() for f in futures]
        return results

    # Sequential fallback for mixed read/write calls.
    return [
        dispatch_tool_call(tc["name"], tc["arguments"], workspace_root=workspace_root)
        for tc in tool_calls
    ]
