# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tool registry for the forge copilot agent loop (slice UX-K).

Each tool is a thin wrapper over an existing function in the forge
copilot codebase.  The registry exposes them with JSON Schema input
definitions so the LLM can call them via the provider's native
tool-use protocol (OpenAI ``tools``, Anthropic ``tools``,
Gemini ``functionDeclarations``).

Adding a new tool is intentional: define the schema, write a thin
``_dispatch_<name>`` function, and register it in ``TOOL_REGISTRY``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fluid_build.schema_manager import FluidSchemaManager

LOG = logging.getLogger("fluid.cli.forge_copilot.tools")

_FV = FluidSchemaManager.latest_bundled_version()

# ---------------------------------------------------------------------------
# Tool definitions — {name, description, input_schema, impl}
# ---------------------------------------------------------------------------

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def _register(
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    impl: Callable[..., Any],
) -> None:
    TOOL_REGISTRY[name] = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "impl": impl,
    }


# ---- discover_workspace --------------------------------------------------


def _dispatch_discover_workspace(*, workspace_path: str = ".", **_kw: Any) -> Dict[str, Any]:
    """Scan the workspace and return a metadata-only discovery report."""
    from fluid_build.cli.forge_copilot_discovery import discover_local_context

    report = discover_local_context(
        discovery_path=None,
        discover=True,
        workspace_root=Path(workspace_path).resolve(),
    )
    return report.to_prompt_payload()


_register(
    name="discover_workspace",
    description=(
        "Scan the user's workspace for data files, SQL, dbt projects, "
        "existing contracts, and infer provider hints.  Returns a "
        "metadata-only report (no raw file contents or credentials)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workspace_path": {
                "type": "string",
                "description": "Path to the workspace root (default: current directory).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    impl=_dispatch_discover_workspace,
)


# ---- read_sample_schema --------------------------------------------------


def _dispatch_read_sample_schema(*, path: str, **_kw: Any) -> Dict[str, Any]:
    """Infer the schema of a single sample file (CSV, JSON, Parquet, Avro)."""
    from fluid_build.cli.forge_copilot_schema_inference import summarize_sample_file

    return summarize_sample_file(Path(path))


_register(
    name="read_sample_schema",
    description=(
        "Read a single data file and return its inferred schema "
        "(column names, types, row count).  Supports CSV, JSON, "
        "JSONL, Parquet, and Avro."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the sample file.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    impl=_dispatch_read_sample_schema,
)


# ---- list_templates -------------------------------------------------------


def _dispatch_list_templates(*, use_case: str = "", domain: str = "", **_kw: Any) -> Dict[str, Any]:
    """Return the capability matrix (available templates, providers, engines)."""
    from fluid_build.cli.forge_copilot_runtime import build_capability_matrix

    matrix = build_capability_matrix()
    # Optionally filter templates by use_case / domain if the LLM asks.
    # For now return the full matrix — the LLM can decide which templates fit.
    return matrix


_register(
    name="list_templates",
    description=(
        "List the locally available templates, providers, and build "
        "engines.  Returns the full capability matrix.  Use the "
        "use_case and domain hints to narrow your template choice."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "use_case": {
                "type": "string",
                "description": "Optional use-case hint (analytics, etl_pipeline, streaming, ml_pipeline).",
            },
            "domain": {
                "type": "string",
                "description": "Optional domain hint (finance, healthcare, retail, telco).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    impl=_dispatch_list_templates,
)


# ---- propose_contract -----------------------------------------------------


def _dispatch_propose_contract(
    *,
    context: Dict[str, Any],
    template: str = "starter",
    provider: str = "local",
    **_kw: Any,
) -> Dict[str, Any]:
    """Build a seed contract scaffold from the given context."""
    from fluid_build.cli.forge_copilot_discovery import DiscoveryReport
    from fluid_build.cli.forge_copilot_runtime import build_seed_contract

    # Build a minimal discovery report if one isn't available.
    discovery = DiscoveryReport(workspace_roots=["."])
    return build_seed_contract(
        context=context,
        discovery_report=discovery,
        template_name=template,
        provider_name=provider,
    )


_register(
    name="propose_contract",
    description=(
        f"Generate a seed FLUID {_FV} contract scaffold for the given "
        "context, template, and provider.  The seed is a starting "
        "point — refine it based on the discovery report and user "
        "requirements before returning it as your final contract."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "context": {
                "type": "object",
                "description": "User context with project_goal, data_sources, use_case, etc.",
                "additionalProperties": True,
                "properties": {},
            },
            "template": {
                "type": "string",
                "description": "Template id from the capability matrix (e.g. 'starter', 'analytics').",
            },
            "provider": {
                "type": "string",
                "description": "Provider id (e.g. 'local', 'gcp', 'aws', 'snowflake').",
            },
        },
        "required": ["context"],
        "additionalProperties": False,
    },
    impl=_dispatch_propose_contract,
)


# ---- validate_contract ----------------------------------------------------


def _dispatch_validate_contract(*, contract: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
    """Validate a candidate FLUID contract and return errors + warnings."""
    from fluid_build.cli.forge_copilot_runtime import (
        build_capability_matrix,
        validate_generated_result,
    )

    capabilities = build_capability_matrix()
    # Wrap the contract in the envelope shape normalize expects.
    normalized = {
        "suggestions": {},
        "contract": contract,
        "readme_markdown": "",
        "additional_files": {},
    }
    errors, warnings = validate_generated_result(normalized, capabilities=capabilities)
    return {"errors": errors, "warnings": warnings}


_register(
    name="validate_contract",
    description=(
        f"Validate a candidate FLUID {_FV} contract against the local "
        "schema and capability matrix.  Returns {errors, warnings}.  "
        "If errors is empty, the contract is valid."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "contract": {
                "type": "object",
                "description": "The FLUID contract to validate.",
                "additionalProperties": True,
                "properties": {},
            },
        },
        "required": ["contract"],
        "additionalProperties": False,
    },
    impl=_dispatch_validate_contract,
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


# ---- list_schedulers --------------------------------------------------------

_TRIGGER_TYPES = ["cron", "event", "manual", "streaming"]
_cached_schedulers: Optional[Dict[str, Any]] = None


def _dispatch_list_schedulers(**_kw: Any) -> Dict[str, Any]:
    """Return available scheduler engines and their supported platforms."""
    global _cached_schedulers
    if _cached_schedulers is not None:
        return _cached_schedulers
    try:
        from fluid_build.schedulers import get_scheduler, list_schedulers

        result = []
        for name in list_schedulers():
            sched = get_scheduler(name)
            result.append(
                {
                    "name": name,
                    "platforms": getattr(sched, "supported_platforms", None) or "all",
                }
            )
        _cached_schedulers = {"schedulers": result, "trigger_types": _TRIGGER_TYPES}
        return _cached_schedulers
    except ImportError:
        return {"schedulers": [], "trigger_types": _TRIGGER_TYPES}


_register(
    name="list_schedulers",
    description=(
        "List available schedule/orchestration engines (e.g., Airflow, Dagster, Prefect) "
        "and supported trigger types.  Use this when the user asks about scheduling, "
        "DAGs, pipelines, or orchestration."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    impl=_dispatch_list_schedulers,
)


def get_tool_definitions() -> List[Dict[str, Any]]:
    """Return the tool definitions in the shape providers expect."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["input_schema"],
        }
        for t in TOOL_REGISTRY.values()
    ]


def dispatch_tool_call(name: str, arguments: Dict[str, Any]) -> Any:
    """Execute a tool by name and return its result.

    Returns a plain dict or string that can be JSON-serialized and
    sent back to the LLM as a tool result.  Unknown tool names
    return an error dict rather than raising so the agent loop can
    continue.
    """
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        LOG.warning("Unknown tool call: %s", name)
        return {"error": f"Unknown tool: {name}"}
    try:
        return tool["impl"](**arguments)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Tool %s failed: %s", name, exc)
        return {"error": f"Tool {name} failed: {exc}"}
