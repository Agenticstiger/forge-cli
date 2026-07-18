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

"""Stage output models and provider-specific structured-output adapters."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .data_model import DimensionalModel, DV2Model, TechniqueLiteral
from .intent import BusinessIntent
from .osi import OSIAIContext, OSIAIContextValue, OSISemanticModel

# ---------------------------------------------------------------------------
# Gemini schema translation helpers (module-private).
#
# Gemini's ``responseSchema`` is an OpenAPI 3.0 subset; Pydantic emits full
# JSON Schema with ``$ref``/``$defs``/``anyOf``. Walking the Pydantic
# output into something Gemini understands splits into three passes so
# each concern is small and independently testable.
# ---------------------------------------------------------------------------

_GEMINI_ALLOWED_KEYS = {
    "type",
    "properties",
    "items",
    "required",
    "enum",
    "nullable",
    "format",
    "description",
}


def _slug(text: Any) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    return value or "model"


def _repair_dv2_payload(value: Dict[str, Any]) -> Dict[str, Any]:
    """Patch provider JSON omissions that would fail before repair loops run."""

    dv2 = dict(value)
    hubs = dv2.get("hubs") if isinstance(dv2.get("hubs"), list) else []
    hub_by_table: Dict[str, str] = {}
    entity_to_hub: Dict[str, str] = {}
    for hub in hubs:
        if not isinstance(hub, dict):
            continue
        entity = str(hub.get("entity_name") or "").strip()
        table = str(hub.get("hub_table_name") or f"hub_{_slug(entity)}").strip()
        if table:
            hub_by_table[table] = table
        if entity and table:
            entity_to_hub[_slug(entity)] = table

    first_hub = next(iter(hub_by_table.values()), None)
    repaired_sats = []
    for raw_sat in dv2.get("satellites") or []:
        if not isinstance(raw_sat, dict):
            repaired_sats.append(raw_sat)
            continue
        sat = dict(raw_sat)
        parent = str(sat.get("parent_hub") or "").strip()
        if not parent:
            entity = _slug(sat.get("entity_name"))
            parent = entity_to_hub.get(entity)
            if parent is None:
                sat_name = _slug(sat.get("satellite_table_name"))
                for candidate_entity, candidate_hub in sorted(entity_to_hub.items()):
                    if candidate_entity and candidate_entity in sat_name:
                        parent = candidate_hub
                        break
            parent = parent or first_hub or f"hub_{entity}"
            sat["parent_hub"] = parent
        repaired_sats.append(sat)
    dv2["satellites"] = repaired_sats
    return dv2


def _inline_refs(schema: Any, defs: Dict[str, Any], _seen: Optional[set] = None) -> Any:
    """Resolve ``$ref`` pointers by inlining their ``$defs`` target.

    ``_seen`` guards against recursive schemas — if we re-enter the same
    definition we stop and emit an empty object rather than looping.
    None of our current schemas are self-recursive, but the guard makes
    the function safe to reuse.
    """
    if _seen is None:
        _seen = set()
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"]
            # Refs look like ``#/$defs/HubDefinition``.
            key = ref.rsplit("/", 1)[-1]
            if key in _seen or key not in defs:
                # Recursive or dangling — emit an empty object so
                # Gemini still sees *some* nested-object hint.
                return {"type": "object"}
            _seen = _seen | {key}
            return _inline_refs(defs[key], defs, _seen)
        return {k: _inline_refs(v, defs, _seen) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_inline_refs(item, defs, _seen) for item in schema]
    return schema


def _flatten_nullable_anyof(schema: Any) -> Any:
    """Collapse ``anyOf: [{...}, {type: "null"}]`` → ``{..., nullable: true}``.

    Pydantic uses this shape for every ``Optional[T]`` field. Gemini
    doesn't support ``anyOf`` in ``responseSchema``; without the
    collapse the field turns into an empty object and the model makes
    up strings.
    """
    if isinstance(schema, dict):
        any_of = schema.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2:
            null_branches = [b for b in any_of if isinstance(b, dict) and b.get("type") == "null"]
            non_null = [b for b in any_of if not (isinstance(b, dict) and b.get("type") == "null")]
            if len(null_branches) == 1 and len(non_null) == 1:
                flattened = dict(_flatten_nullable_anyof(non_null[0]))
                # Preserve sibling keys like ``description``/``default``
                # that Pydantic places next to the ``anyOf``.
                for k, v in schema.items():
                    if k in {"anyOf", "default"}:
                        continue
                    flattened.setdefault(k, _flatten_nullable_anyof(v))
                flattened["nullable"] = True
                return flattened
        return {k: _flatten_nullable_anyof(v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_flatten_nullable_anyof(item) for item in schema]
    return schema


def _strip_gemini(schema: Any) -> Any:
    """Drop keys outside the Gemini-accepted OpenAPI subset."""
    if isinstance(schema, dict):
        result: Dict[str, Any] = {}
        for key, value in schema.items():
            if key not in _GEMINI_ALLOWED_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                result[key] = {name: _strip_gemini(item) for name, item in value.items()}
            elif key == "items":
                result[key] = _strip_gemini(value)
            elif isinstance(value, dict):
                result[key] = _strip_gemini(value)
            elif isinstance(value, list):
                # ``required`` + ``enum`` are lists of scalars — leave them intact.
                result[key] = value
            else:
                result[key] = value
        return result
    return schema


def _strict_openai_schema(schema: Any) -> Any:
    """Normalize Pydantic JSON Schema for OpenAI strict structured outputs.

    OpenAI's strict ``response_format`` subset is narrower than the JSON
    Schema Pydantic emits by default: object schemas must explicitly set
    ``additionalProperties: false`` and defaults are rejected. We keep
    ``$defs``/``$ref`` intact because OpenAI accepts references, but we
    normalize every referenced object definition too.
    """
    if isinstance(schema, dict):
        result: Dict[str, Any] = {}
        for key, value in schema.items():
            if key == "default":
                continue
            result[key] = _strict_openai_schema(value)

        properties = result.get("properties")
        if result.get("type") == "object" or isinstance(properties, dict):
            result.setdefault("type", "object")
            result["additionalProperties"] = False
            if not isinstance(properties, dict):
                properties = {}
                result["properties"] = properties
            result["required"] = list(properties.keys())
        return result
    if isinstance(schema, list):
        return [_strict_openai_schema(item) for item in schema]
    return schema


class StructuredOutputModel(BaseModel):
    """Base model with helper adapters for provider-native structured output."""

    @classmethod
    def to_openai_json_schema(cls) -> Dict[str, Any]:
        schema = cls.model_json_schema()
        schema = _inline_refs(schema, schema.get("$defs", {}))
        if isinstance(schema, dict):
            schema.pop("$defs", None)
        return {
            "type": "json_schema",
            "json_schema": {
                "name": cls.__name__,
                "schema": _strict_openai_schema(schema),
                "strict": True,
            },
        }

    @classmethod
    def to_anthropic_tool(cls) -> Dict[str, Any]:
        return {
            "name": cls.__name__,
            "description": f"Emit a structured {cls.__name__} payload.",
            "input_schema": cls.model_json_schema(),
        }

    @classmethod
    def to_gemini_config(cls) -> Dict[str, Any]:
        """Translate the Pydantic JSON schema into Gemini's OpenAPI subset.

        Gemini's ``responseSchema`` is an OpenAPI 3.0 subset — no
        ``$ref``, no ``$defs``, no unrestricted ``anyOf``. Pydantic's
        default emission uses all three. Without translation, nested
        model references collapse to empty schemas and Gemini responds
        with bare strings (e.g. ``"HUB_SERVICE"`` instead of
        ``{"hub_table_name": "hub_service", ...}``).

        The translation pipeline is:

        1. **Inline refs.** Walk the schema, resolve every ``$ref`` by
           looking it up in ``$defs`` (then drop ``$defs``).
        2. **Flatten Optional.** Pydantic emits
           ``{"anyOf": [{"$ref": ...}, {"type": "null"}], ...}`` for
           ``Optional[T]`` fields. Collapse the two-branch
           nullable-anyOf to the non-null branch plus ``nullable: true``.
        3. **Strip unknown keys.** Keep the OpenAPI-legal surface only:
           ``type``, ``properties``, ``items``, ``required``, ``enum``,
           ``nullable``, ``format``, ``description``.
        """
        raw = cls.model_json_schema()
        defs = raw.pop("$defs", {})
        inlined = _inline_refs(raw, defs)
        flattened = _flatten_nullable_anyof(inlined)
        stripped = _strip_gemini(flattened)
        return {
            "responseMimeType": "application/json",
            "responseSchema": stripped,
        }

    @classmethod
    def to_ollama_format(cls) -> str:
        return "json"

    @classmethod
    def to_provider_spec(cls, provider_name: str) -> Dict[str, Any]:
        if provider_name in {"openai", "azure-openai"}:
            return cls.to_openai_json_schema()
        if provider_name in {"anthropic", "claude"}:
            return {
                "tools": [cls.to_anthropic_tool()],
                "tool_choice": {"type": "tool", "name": cls.__name__},
            }
        if provider_name == "gemini":
            return cls.to_gemini_config()
        if provider_name == "ollama":
            return {"format": cls.to_ollama_format()}
        return {"schema": cls.model_json_schema()}


class ConceptualEntity(BaseModel):
    name: str
    description: str = ""
    source_names: List[str] = Field(default_factory=list)


class ConceptualRelationship(BaseModel):
    source_entity: str
    target_entity: str
    description: str = ""
    cardinality: Optional[str] = None


class ConceptualDraft(StructuredOutputModel):
    name: str
    description: str = ""
    # OSIAIContextValue coerces the spec's plain-string ai_context form
    # (and the occasional LLM string emission) into the structured object.
    ai_context: OSIAIContextValue = Field(default_factory=OSIAIContext)
    entities: List[ConceptualEntity] = Field(default_factory=list)
    relationships: List[ConceptualRelationship] = Field(default_factory=list)


class LogicalDraft(StructuredOutputModel):
    name: str
    description: str = ""
    technique: TechniqueLiteral
    conceptual: Optional[ConceptualDraft] = None
    dv2: Optional[DV2Model] = None
    dimensional: Optional[DimensionalModel] = None
    osi: OSISemanticModel
    source_summary: Dict[str, Any] = Field(default_factory=dict)
    review_notes: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_unused_branch(cls, data: Any) -> Any:
        """Normalize away empty sibling-branch shells before strict validation.

        Gemini's OpenAPI-3.0 ``responseSchema`` subset cannot express "this
        object is optional AND nullable" cleanly, so the model frequently
        returns ``{"technique": "dimensional", "dv2": {}, "dimensional": {...}}``
        instead of ``"dv2": null``. An empty dict coerces to a default-valued
        ``DV2Model`` which then trips the shape validator below.

        This pre-validator strips the unused branch when it's empty-shaped
        (no hubs/links/sats for DV2, no facts/dimensions for dimensional) so
        real content is preserved and the strict ``mode="after"`` check
        still flags genuinely ambiguous drafts.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("source_summary") is None:
            data["source_summary"] = {}
        if data.get("osi") is None:
            data["osi"] = {
                "name": data.get("name") or "semantic_model",
                "description": data.get("description") or "",
                "ai_context": {},
            }
        elif isinstance(data.get("osi"), dict):
            osi = dict(data["osi"])
            osi.setdefault("name", data.get("name") or "semantic_model")
            if osi.get("ai_context") is None:
                osi["ai_context"] = {}
            data["osi"] = osi
        if isinstance(data.get("conceptual"), dict):
            conceptual = dict(data["conceptual"])
            if conceptual.get("ai_context") is None:
                conceptual["ai_context"] = {}
            data["conceptual"] = conceptual
        technique = data.get("technique")
        if technique == "dimensional":
            dv2 = data.get("dv2")
            if isinstance(dv2, dict) and not any(
                dv2.get(key) for key in ("hubs", "links", "satellites", "pits", "bridges")
            ):
                data = {**data, "dv2": None}
        elif technique == "data_vault_2":
            dim = data.get("dimensional")
            if isinstance(dim, dict) and not any(
                dim.get(key) for key in ("facts", "dimensions", "conformed_dimensions", "bridges")
            ):
                data = {**data, "dimensional": None}
            if isinstance(data.get("dv2"), dict):
                data["dv2"] = _repair_dv2_payload(data["dv2"])
        return data

    @model_validator(mode="after")
    def _validate_technique_shape(self) -> "LogicalDraft":
        # Registry-driven (issue #248): each technique declares which branch it
        # fills. data_vault_2 -> dv2 only; dimensional -> dimensional only; a
        # source-aligned ``flat`` (or any branch=None technique) -> neither
        # branch (the OSI semantic model carries the 1:1 shape); ``custom``
        # (bring-your-own) -> accept whatever shape the user supplied verbatim.
        from fluid_build.copilot.modeling_techniques import get_modeling_technique

        spec = get_modeling_technique(self.technique)
        if spec is not None and spec.requires_logical_model:
            return self  # custom — user-supplied model used verbatim
        branch = spec.branch if spec is not None else None
        if branch == "dv2":
            if self.dv2 is None or self.dimensional is not None:
                raise ValueError("data_vault_2 logical drafts must populate dv2 only")
        elif branch == "dimensional":
            if self.dimensional is None or self.dv2 is not None:
                raise ValueError("dimensional logical drafts must populate dimensional only")
        else:
            if self.dv2 is not None or self.dimensional is not None:
                raise ValueError(
                    f"{self.technique!r} logical drafts must not populate dv2/dimensional "
                    "(source-aligned techniques carry their shape in osi)"
                )
        return self


class BuildSpec(BaseModel):
    id: str
    name: str
    engine: str
    layer: str = ""
    sql: str = ""
    depends_on: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TransformPlan(StructuredOutputModel):
    builds: List[BuildSpec] = Field(default_factory=list)
    additional_files: Dict[str, str] = Field(default_factory=dict)


class ReadmeDraft(StructuredOutputModel):
    readme_markdown: str
    description: str = ""


class ValidationFinding(BaseModel):
    message: str
    severity: Literal["error", "warning", "info"] = "error"
    field: Optional[str] = None


class ValidationReport(StructuredOutputModel):
    score: int = 0
    issues: List[ValidationFinding] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    passes_schema: bool = False


class PhysicalDraft(StructuredOutputModel):
    contract: Dict[str, Any]
    logical: LogicalDraft
    transform_plan: TransformPlan
    readme: Optional[ReadmeDraft] = None
    validation: Optional[ValidationReport] = None
    additional_files: Dict[str, str] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "BuildSpec",
    "BusinessIntent",
    "ConceptualDraft",
    "LogicalDraft",
    "PhysicalDraft",
    "ReadmeDraft",
    "StructuredOutputModel",
    "TransformPlan",
    "ValidationReport",
]
