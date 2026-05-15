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

"""Single source-of-truth v0.7.3 contract builder for forge templates.

Replaces the ~100-line duplicated ``generate_contract`` across the
five forge templates (analytics / etl_pipeline / ml_pipeline /
starter / streaming) with a small spec-driven builder. Each template
declares only what's *unique* about it (engine, pattern, columns,
exposeId); this helper handles every shape detail the v0.7.3 schema
checks for.

This builder is the canonical source of v0.7.3-shaped contracts for
the five built-in templates — they emit schema-valid output directly,
with no coercion layer needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


@dataclass
class TemplateSpec:
    """What a template tells the builder.

    Only the unique-to-the-template bits live here; everything else
    (canonical metadata pair, owner email, default exposeId, etc.) is
    derived from the spec + the user's project config.
    """

    template_name: str
    """Identifier used for human-facing logs (e.g. 'analytics')."""

    product_type: str = "SDP"
    """Canonical Data Mesh code — ``SDP`` / ``ADP`` / ``CDP``."""

    pattern: str = "embedded-logic"
    """Build pattern — ``embedded-logic`` / ``hybrid-reference`` /
    ``acquisition`` / ``logical-mapping`` / ``declarative``."""

    engine: str = "sql"
    """Build engine, must be in the schema enum (dbt / sql / python /
    spark / custom / duckdb / airbyte / meltano / dlt / kafka-connect /
    debezium)."""

    properties: Dict[str, Any] = field(default_factory=dict)
    """Build-pattern-specific properties; the builder validates required
    keys (e.g. embedded-logic.sql, hybrid-reference.model)."""

    columns: List[Dict[str, Any]] = field(default_factory=list)
    """Schema columns for the single ``exposes[0]`` block."""

    expose_id: str = "main_output"
    """The exposeId on ``exposes[0]``."""

    expose_kind: str = "table"
    """The exposes[0].kind value — table / view / api / topic / file."""

    binding_format: str = "csv"
    """Binding format. Must be one of the v0.7.3 binding format enums."""

    location_template: Optional[Dict[str, Any]] = None
    """Optional binding.location dict; defaults to a path-based location."""

    consumes: List[Dict[str, str]] = field(default_factory=list)
    """consumes[] entries; each row is ``{productId, exposeId}``."""

    cron: str = "0 6 * * *"
    """Default cron schedule for the build."""

    description_suffix: str = ""
    """Appended to the contract description for clarity."""


_LAYER_BY_PT = {"SDP": "Bronze", "ADP": "Silver", "CDP": "Gold"}


def _resolve_id(project_name: str, template_name: str) -> str:
    """Build a contract id matching the schema's ``^[a-zA-Z0-9_.-]+$`` pattern.

    Reuses the canonical sanitiser from
    :mod:`fluid_build.forge.product_types` so the templates and the
    Phase 1 ``shape_contract`` builder stay byte-equivalent on the same
    project name (invariant **I2**). Local duplicate deleted.
    """
    from fluid_build.forge.product_types import _sanitize_id_segment

    base = _sanitize_id_segment(project_name) or template_name
    return f"{base}.{template_name}"


def _default_location(provider: str, expose_id: str, fmt: str) -> Dict[str, Any]:
    """Default binding.location shape per provider."""
    if provider == "snowflake":
        return {
            "database": "${SNOWFLAKE_DATABASE}",
            "schema": "ANALYTICS",
            "table": expose_id.upper(),
        }
    if provider == "gcp":
        return {
            "project": "${FLUID_GCP_PROJECT}",
            "dataset": "analytics",
            "table": expose_id,
        }
    if provider == "aws":
        return {
            "bucket": "${FLUID_AWS_BUCKET}",
            "key": f"runtime/out/{expose_id}.{('parquet' if fmt == 's3_file' else 'csv')}",
            "region": "${AWS_REGION}",
        }
    return {"path": f"runtime/out/{expose_id}.{('csv' if fmt == 'csv' else 'parquet')}"}


def _binding_format_for(provider: str, default_fmt: str) -> str:
    """Map provider → schema-valid binding.format."""
    by_provider = {
        "snowflake": "snowflake_table",
        "gcp": "bigquery_table",
        "aws": "s3_file",
    }
    return by_provider.get(provider, default_fmt or "csv")


def _normalise_properties(spec: TemplateSpec) -> Dict[str, Any]:
    """Project the spec's properties into a schema-valid build properties block.

    Defends against templates that ship empty / wrong-shape properties
    so the resulting contract round-trips through ``fluid validate``
    directly.
    """
    p = dict(spec.properties)
    if spec.pattern == "embedded-logic":
        if spec.engine == "python":
            # Python builds run as embedded-logic with ``language: python``
            # — the JSON schema's embeddedLogicPattern requires ``sql`` so
            # we ship a placeholder query that flags "see model" rather
            # than emitting an empty schema-violating contract.
            return {
                "sql": p.get("sql") or "-- python entrypoint defined in builds[0].repository",
                "language": "python",
            }
        # SQL embedded-logic: only ``sql`` + optional ``language`` allowed
        return {"sql": p.get("sql") or "SELECT 1 AS id"}
    if spec.pattern == "hybrid-reference":
        return {
            "model": p.get("model") or "main_model",
            **({"vars": p["vars"]} if isinstance(p.get("vars"), dict) else {}),
        }
    if spec.pattern == "acquisition":
        # acquisition.source requires kind + mode
        source = p.get("source") or {}
        source.setdefault("kind", "filesystem")
        source.setdefault("mode", "full_refresh")
        return {"source": source}
    if spec.pattern == "declarative":
        return {k: v for k, v in p.items() if k in {"from", "joins", "filters", "select"}}
    if spec.pattern == "logical-mapping":
        return {
            "sources": p.get("sources") or [],
            "steps": p.get("steps") or [],
        }
    return p


def build_contract(
    *,
    spec: TemplateSpec,
    project_config: Mapping[str, Any],
    fluid_version: str = "0.7.3",
) -> Dict[str, Any]:
    """Build a v0.7.3-canonical contract for *spec* + *project_config*.

    Output passes ``fluid validate`` against fluid-schema-0.7.3 with
    no coercion needed. Equivalence axiom enforced: layer + productType
    are populated as a canonical pair.
    """
    project_name = project_config.get("name") or f"{spec.template_name}-product"
    domain = project_config.get("domain") or "analytics"
    owner = project_config.get("owner") or f"{spec.template_name}-team"
    provider = project_config.get("provider") or "local"
    description = project_config.get("description", f"FLUID {spec.template_name} data product")
    if spec.description_suffix:
        description = f"{description} — {spec.description_suffix}"

    layer = _LAYER_BY_PT.get(spec.product_type, "Bronze")
    columns = spec.columns or [
        {"name": "id", "type": "string", "required": True},
        {"name": "created_at", "type": "timestamp", "required": True},
    ]

    binding_format = _binding_format_for(provider, spec.binding_format)
    location = spec.location_template or _default_location(provider, spec.expose_id, binding_format)

    contract: Dict[str, Any] = {
        "fluidVersion": fluid_version,
        "kind": "DataProduct",
        "id": _resolve_id(str(project_name), spec.template_name),
        "name": str(project_name),
        "description": description,
        "domain": domain,
        "metadata": {
            "layer": layer,
            "productType": spec.product_type,
            "owner": {
                "team": owner,
                "email": f"{owner}@company.com",
            },
        },
        "consumes": list(spec.consumes),
        "builds": [
            {
                "id": "main_build",
                "pattern": spec.pattern,
                "engine": spec.engine,
                "properties": _normalise_properties(spec),
                "execution": {
                    "trigger": {"type": "schedule", "cron": spec.cron},
                    "runtime": {
                        "platform": provider,
                        "resources": {"cpu": "1", "memory": "2Gi"},
                    },
                },
            }
        ],
        "exposes": [
            {
                "exposeId": spec.expose_id,
                "kind": spec.expose_kind,
                "binding": {
                    "platform": provider,
                    "format": binding_format,
                    "location": location,
                },
                "contract": {"schema": columns},
            }
        ],
    }

    # Python-engine builds need ``repository`` next to properties.model.
    if spec.engine == "python" and spec.pattern == "embedded-logic":
        contract["builds"][0]["repository"] = "src/main.py"

    return contract


__all__ = ["TemplateSpec", "build_contract"]
