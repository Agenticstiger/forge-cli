# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""JSON Schema for the LLM response envelope (slice UX-I).

This module centralises the shape that the forge copilot expects
back from the LLM.  Before slice UX-I, the generation prompt pleaded
"Return strict JSON only. Do not wrap the response in markdown
fences." in natural language and then leaned on ``extract_json_object``
to recover from malformed output.  That cost ~50% of real-world
runs a repair retry.

Slice UX-I wires this schema into each provider's native structured
output feature:

- **OpenAI**: ``response_format={"type": "json_schema", ...}`` with
  ``strict: true``.  The model literally cannot return anything else.
- **Anthropic**: ``tools=[{name: "emit_forge_contract", input_schema:
  ...}]`` + ``tool_choice={type: "tool", name: "emit_forge_contract"}``.
  The model is forced to call the single emit tool and its structured
  input is the final payload.
- **Gemini**: ``generationConfig={"responseMimeType":
  "application/json", "responseSchema": ...}``.
- **Ollama**: model-dependent; the adapter falls through to plain
  JSON mode when the model isn't in the known-good list.

The schema deliberately mirrors the field set that
``normalize_generation_payload`` in ``forge_copilot_contract_helpers``
already expects, so there is no drift between what the LLM is told to
emit and what the validator accepts.  The ``contract`` sub-schema is
intentionally permissive (``additionalProperties: True``) because the
FLUID contract itself is validated downstream by
``FluidSchemaManager``, not by this provider-level schema.

The schema is deliberately kept compatible with OpenAI's strict-mode
JSON-Schema subset: no ``patternProperties``, no ``$ref``, every
object has an explicit ``properties`` block, and strict-mode objects
list every property in ``required``.
"""

from __future__ import annotations

__all__ = [
    "FORGE_RESPONSE_SCHEMA",
    "FORGE_RESPONSE_REQUIRED_KEYS",
    "anthropic_tool_definition",
    "openai_response_format",
    "gemini_response_schema_config",
    "ollama_supports_structured_output",
]


from typing import Any, Dict, Tuple


# The field set below mirrors
# ``forge_copilot_contract_helpers.normalize_generation_payload`` so
# that the LLM, provider, and normalizer all agree on the same shape.
FORGE_RESPONSE_REQUIRED_KEYS: Tuple[str, ...] = (
    "recommended_template",
    "recommended_provider",
    "contract",
)


# Permissive but well-typed JSON Schema.  OpenAI's strict mode allows
# ``additionalProperties: false`` on top-level objects; for nested
# free-form objects (contract, additional_files) we keep it permissive
# and rely on downstream semantic validation.
FORGE_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "recommended_template": {
            "type": "string",
            "description": (
                "The template id chosen from the capability matrix "
                "(e.g. 'starter', 'analytics', 'etl_pipeline')."
            ),
        },
        "recommended_provider": {
            "type": "string",
            "description": (
                "The provider id chosen from the capability matrix "
                "(e.g. 'local', 'gcp', 'aws', 'snowflake')."
            ),
        },
        "recommended_patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "High-level architectural patterns relevant to the contract.",
        },
        "architecture_suggestions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "best_practices": {
            "type": "array",
            "items": {"type": "string"},
        },
        "technology_stack": {
            "type": "array",
            "items": {"type": "string"},
        },
        "description": {
            "type": "string",
            "description": "Short human-readable project description.",
        },
        "domain": {
            "type": "string",
        },
        "owner": {
            "type": "string",
            "description": "Team slug (e.g. 'data-team').",
        },
        "readme_markdown": {
            "type": "string",
            "description": "Markdown README generated for the product.",
        },
        "contract": {
            "type": "object",
            "description": (
                "A FLUID DataProduct contract (latest schema version).  "
                "The provider-level schema is deliberately permissive; "
                "downstream semantic validation enforces the FLUID contract rules."
            ),
            "additionalProperties": True,
            "properties": {},
        },
        "additional_files": {
            "type": "object",
            "description": (
                "Optional extra files to write alongside the contract. "
                "Keys are relative paths, values are file contents."
            ),
            "additionalProperties": True,
            "properties": {},
        },
    },
    "required": [
        "recommended_template",
        "recommended_provider",
        "recommended_patterns",
        "architecture_suggestions",
        "best_practices",
        "technology_stack",
        "description",
        "domain",
        "owner",
        "readme_markdown",
        "contract",
        "additional_files",
    ],
}


# ---------------------------------------------------------------------------
# Provider-specific helpers
# ---------------------------------------------------------------------------


def openai_response_format(model: str) -> Dict[str, Any]:
    """Build the OpenAI ``response_format`` directive for *model*.

    Uses strict ``json_schema`` mode for models the catalog marks as
    ``structured_output``-capable and falls back to the weaker
    ``json_object`` mode for older or unknown models.
    """
    from fluid_build.cli.forge_copilot_llm_providers import model_supports_structured_output

    if model_supports_structured_output("openai", model):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "ForgeContract",
                "description": "FLUID Forge copilot response envelope.",
                "schema": FORGE_RESPONSE_SCHEMA,
                "strict": True,
            },
        }
    return {"type": "json_object"}


# ---------------------------------------------------------------------------
# Anthropic: single forced tool call
# ---------------------------------------------------------------------------


def anthropic_tool_definition() -> Dict[str, Any]:
    """Return the ``emit_forge_contract`` tool definition for Anthropic.

    The model is forced to call this tool via
    ``tool_choice={"type": "tool", "name": "emit_forge_contract"}``.
    Its structured ``input`` is the final response payload.
    """
    return {
        "name": "emit_forge_contract",
        "description": (
            "Emit the final FLUID Forge copilot response envelope. "
            "This is the only way to return data to the user."
        ),
        "input_schema": FORGE_RESPONSE_SCHEMA,
    }


# ---------------------------------------------------------------------------
# Gemini: responseSchema with a stripped-down JSON Schema
# ---------------------------------------------------------------------------


def _strip_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini's ``responseSchema`` only accepts a subset of JSON Schema.

    Strip out ``additionalProperties``, descriptions, and other keys
    that the Gemini API rejects.  This is a best-effort transform —
    if Gemini rejects the schema we fall back to plain JSON mode.
    """
    if not isinstance(schema, dict):
        return schema

    allowed = {
        "type",
        "properties",
        "items",
        "required",
        "enum",
        "format",
        "nullable",
    }
    stripped: Dict[str, Any] = {}
    for key, value in schema.items():
        if key not in allowed:
            continue
        if key == "properties" and isinstance(value, dict):
            stripped[key] = {k: _strip_for_gemini(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            stripped[key] = _strip_for_gemini(value)
        else:
            stripped[key] = value
    return stripped


def gemini_response_schema_config() -> Dict[str, Any]:
    """Build Gemini's ``generationConfig`` fragment for structured output."""
    return {
        "responseMimeType": "application/json",
        "responseSchema": _strip_for_gemini(FORGE_RESPONSE_SCHEMA),
    }


# ---------------------------------------------------------------------------
# Ollama: model allowlist
# ---------------------------------------------------------------------------


def ollama_supports_structured_output(model: str) -> bool:
    """Return True if *model* is in the catalog with ``structured_output`` capability.

    Falls back to False for unknown models — the prompt-level JSON
    nudge still works as a safety net.
    """
    from fluid_build.cli.forge_copilot_llm_providers import model_supports_structured_output

    return model_supports_structured_output("ollama", model)
