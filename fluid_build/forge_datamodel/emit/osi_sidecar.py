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

"""Standalone OSI (Apache Ossie) interchange-document emission.

This module is the only place allowed to serialize the internal OSI IR
(:class:`fluid_build.copilot.schemas.osi.OSISemanticModel`) for external
consumers. The IR is deliberately looser and richer than the interchange
spec, so emission is a normalization pass, not a plain dump:

* **Root wrapper.** The spec's root document is
  ``{version, semantic_model: [...]}`` — a bare semantic model is not a
  valid Ossie document. ``version`` comes from
  :data:`~fluid_build.copilot.schemas.osi.OSI_SPEC_VERSION` (the released
  spec revision dbt Core's native OSI reader accepts).
* **IR-field relocation.** ``OSIField.data_type``,
  ``OSIDimension.grain``, and ``OSIRelationship.description`` are
  fluid-only IR enrichments; the upstream schema is
  ``additionalProperties: false`` everywhere, so they move into
  ``custom_extensions`` entries under the ``FLUID`` vendor instead of
  being emitted inline (no information loss, strict validators stay
  green).
* **Required-key guarantees.** ``dataset.source`` defaults to the
  dataset name; a field without an expression gets the spec-idiomatic
  plain column reference in ``ANSI_SQL``.
* **Pruning.** Optional keys that dumped as ``None`` / empty are dropped
  so the document matches the reference serializer's omit-when-absent
  style.

``validate_osi_document`` checks a built document against the vendored
upstream JSON Schema (``fluid_build/schemas/ossie-osi-schema.json``,
verbatim from apache/ossie ``core-spec/osi-schema.json``) — used by the
conformance agent at runtime (advisory) and by the test suite as a hard
CI gate.

The JSON emission form exists because dbt Core (v1.12+) natively reads
OSI ``.json`` documents from a project's ``OSI/`` directory (or
``osi-paths`` config) — drop the emitted file there and the semantic
model is queryable through dbt with no conversion step.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.copilot.schemas.osi import FLUID_VENDOR_NAME, OSI_SPEC_VERSION
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft

_OSSIE_SCHEMA_RESOURCE = "ossie-osi-schema.json"


def emit_osi_yaml(logical: LogicalDraft) -> str:
    """Render the spec-conformant Ossie document as YAML."""
    return yaml.safe_dump(build_osi_document(logical), sort_keys=False, allow_unicode=True)


def emit_osi_json(logical: LogicalDraft) -> str:
    """Render the spec-conformant Ossie document as JSON.

    This is the shape dbt Core's native OSI reader consumes (``OSI/``
    dir / ``osi-paths``).
    """
    return json.dumps(build_osi_document(logical), indent=2, ensure_ascii=False) + "\n"


def build_osi_document(logical: LogicalDraft) -> Dict[str, Any]:
    """Build the spec-conformant root document from the draft's OSI IR."""
    raw = logical.osi.model_dump(mode="json", by_alias=True)
    return {
        "version": OSI_SPEC_VERSION,
        "semantic_model": [_spec_semantic_model(raw)],
    }


def validate_osi_document(document: Dict[str, Any]) -> List[str]:
    """Validate ``document`` against the vendored upstream JSON Schema.

    Returns a sorted list of human-readable issue strings (empty when
    conformant). ``jsonschema`` is imported lazily — this module sits on
    the forge pipeline path, not the CLI cold path, but there is no
    reason to tax importers that never validate.
    """
    import jsonschema

    schema = _load_ossie_schema()
    validator_cls = jsonschema.validators.validator_for(schema)
    errors = sorted(
        validator_cls(schema).iter_errors(document),
        key=lambda e: ([str(p) for p in e.absolute_path], e.message),
    )
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]


@lru_cache(maxsize=1)
def _load_ossie_schema() -> Dict[str, Any]:
    """Load the vendored Ossie schema (verbatim upstream copy, one tweak).

    The upstream schema pins ``version`` to its own in-development const
    (``0.2.0.dev0``); fluid stamps :data:`OSI_SPEC_VERSION` (the released
    revision downstream readers accept), so the const is relaxed to a
    plain string here. Callers must treat the returned dict as read-only
    — it is cached and shared.
    """
    from importlib import resources

    text = (
        resources.files("fluid_build.schemas")
        .joinpath(_OSSIE_SCHEMA_RESOURCE)
        .read_text(encoding="utf-8")
    )
    schema = json.loads(text)
    schema.get("properties", {}).get("version", {}).pop("const", None)
    return schema


# --- document normalization ------------------------------------------------


def _spec_semantic_model(model: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": model.get("name") or "semantic_model"}
    _set_if(out, "description", model.get("description"))
    _set_if(out, "ai_context", _pruned_ai_context(model.get("ai_context")))
    # ``datasets`` is spec-required (minItems 1) — always emitted; an
    # empty list on a stub IR surfaces as a conformance finding rather
    # than being silently papered over.
    out["datasets"] = [_spec_dataset(d) for d in model.get("datasets") or []]
    _set_if(
        out,
        "relationships",
        [_spec_relationship(r) for r in model.get("relationships") or []] or None,
    )
    _set_if(out, "metrics", [_spec_metric(m) for m in model.get("metrics") or []] or None)
    _set_if(out, "custom_extensions", model.get("custom_extensions") or None)
    return out


def _spec_dataset(ds: Dict[str, Any]) -> Dict[str, Any]:
    name = ds.get("name") or "dataset"
    out: Dict[str, Any] = {
        "name": name,
        # Spec-required; the IR leaves it unset on incremental drafts.
        # The dataset name is the correct default — deterministic
        # builders populate ``source`` with the physical table name,
        # which equals the dataset name for source-aligned models.
        "source": ds.get("source") or name,
    }
    _set_if(out, "description", ds.get("description"))
    _set_if(out, "primary_key", ds.get("primary_key") or None)
    _set_if(out, "unique_keys", ds.get("unique_keys") or None)
    _set_if(out, "fields", [_spec_field(f) for f in ds.get("fields") or []] or None)
    _set_if(out, "ai_context", _pruned_ai_context(ds.get("ai_context")))
    _set_if(out, "custom_extensions", ds.get("custom_extensions") or None)
    return out


def _spec_field(f: Dict[str, Any]) -> Dict[str, Any]:
    name = f.get("name") or "field"
    fluid_ext: Dict[str, Any] = {}
    if f.get("data_type"):
        fluid_ext["data_type"] = f["data_type"]

    dimension = f.get("dimension") or {}
    if dimension.get("grain"):
        fluid_ext["grain"] = dimension["grain"]

    expression = f.get("expression")
    if not expression or not expression.get("dialects"):
        # Spec-required. A bare column reference is the spec's own
        # idiom for simple fields, and ANSI_SQL is the portable label.
        expression = {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]}

    out: Dict[str, Any] = {"name": name, "expression": expression}
    _set_if(out, "description", f.get("description"))
    _set_if(out, "label", f.get("label"))
    if dimension.get("is_time"):
        out["dimension"] = {"is_time": True}
    _set_if(out, "ai_context", _pruned_ai_context(f.get("ai_context")))
    extensions = list(f.get("custom_extensions") or [])
    if fluid_ext:
        extensions.append(_fluid_extension(fluid_ext))
    _set_if(out, "custom_extensions", extensions or None)
    return out


def _spec_relationship(rel: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "name": rel.get("name") or "relationship",
        "from": rel.get("from"),
        "to": rel.get("to"),
        "from_columns": rel.get("from_columns") or [],
        "to_columns": rel.get("to_columns") or [],
    }
    _set_if(out, "ai_context", _pruned_ai_context(rel.get("ai_context")))
    extensions = list(rel.get("custom_extensions") or [])
    if rel.get("description"):
        # The spec's Relationship has no ``description`` — relocate.
        extensions.append(_fluid_extension({"description": rel["description"]}))
    _set_if(out, "custom_extensions", extensions or None)
    return out


def _spec_metric(m: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "name": m.get("name") or "metric",
        # Spec-required; an empty dialect list is caught upstream by the
        # semantic-quality lint (error) and again by the document check.
        "expression": m.get("expression") or {"dialects": []},
    }
    _set_if(out, "description", m.get("description"))
    _set_if(out, "ai_context", _pruned_ai_context(m.get("ai_context")))
    _set_if(out, "custom_extensions", m.get("custom_extensions") or None)
    return out


def _fluid_extension(payload: Dict[str, Any]) -> Dict[str, str]:
    return {"vendor_name": FLUID_VENDOR_NAME, "data": json.dumps(payload, sort_keys=True)}


def _pruned_ai_context(ctx: Any) -> Optional[Any]:
    """Drop empty ai_context values; keep spec-extra keys (the spec's
    structured form is ``additionalProperties: true``)."""
    if not ctx:
        return None
    if isinstance(ctx, str):
        return ctx
    pruned = {k: v for k, v in ctx.items() if v not in ("", [], None)}
    return pruned or None


def _set_if(target: Dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value
