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

"""DataMesh Manager data-contract builders (ODCS + DCS).

Lifted from ``providers/datamesh_manager/datamesh_manager.py``.
~340 LOC of pure dict-shape builders. Two top-level builders +
one logical-type mapper:

* :func:`_build_data_contract_odcs` — emits Bitol ODCS payload.
* :func:`_build_data_contract_dcs` — emits DataMesh DCS payload.
* :func:`_odcs_logical_type` — FLUID logicalType → ODCS string.

The host class wraps each builder in a thin instance method that
passes ``self._derive_team_id`` and ``self._extract_provider`` as
callables, preserving the original ``self``-method API while
keeping the builder itself decoupled from the provider class.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping

_STATUS_MAP: Dict[str, str] = {
    "draft": "draft",
    "development": "draft",
    "active": "active",
    "production": "active",
    "deprecated": "deprecated",
    "retired": "retired",
}

# Provider name → DCS Server type mapping. Lifted from the host
# module so the extracted builders are self-contained.
_PROVIDER_TYPE_MAP: Dict[str, str] = {
    "gcp": "BigQuery",
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "aws": "S3",
    "redshift": "Redshift",
    "kafka": "Kafka",
    "s3": "S3",
    "azure": "Azure",
    "postgres": "Postgres",
    "mysql": "MySQL",
    "local": "Local",
}


def _build_data_contract_odcs(
    fluid: Mapping[str, Any],
    product_id: str,
    *,
    derive_team_id_fn: Callable[[Mapping[str, Any]], str],
    extract_provider_fn: Callable[[Mapping[str, Any]], str],
) -> Dict[str, Any]:
    """Build an Open Data Contract Standard v3.1.0 payload.

    Reference: https://bitol-io.github.io/open-data-contract-standard/
    API example format::

        {
          "apiVersion": "v3.1.0",
          "kind": "DataContract",
          "id": "...",
          "name": "...",
          "version": "1.0.0",
          "domain": "...",
          "status": "active",
          "description": { "purpose": "..." },
          "schema": [ { "name": "...", "physicalType": "table", "properties": [...] } ],
          "team": { "name": "team-id" }
        }
    """
    meta = fluid.get("metadata", {})
    contract_id = f"{product_id}-contract"

    dc: Dict[str, Any] = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": contract_id,
        "name": meta.get("name") or fluid.get("name") or product_id,
        "version": meta.get("version", "1.0.0"),
        "status": _STATUS_MAP.get(str(meta.get("status", "active")).lower(), "active"),
        "dataProduct": product_id,
        "team": {
            "name": derive_team_id_fn(fluid),
        },
    }

    # Domain
    domain = fluid.get("domain") or meta.get("domain")
    if domain:
        dc["domain"] = str(domain).lower().replace(" ", "-")

    # Description — ODCS uses { purpose, usage, limitations }
    desc_text = meta.get("description") or fluid.get("description", "")
    if desc_text:
        dc["description"] = {"purpose": str(desc_text).strip()}

    # Schema — ODCS uses a top-level array of schema objects
    schema_array: List[Dict[str, Any]] = []
    servers: List[Dict[str, Any]] = []

    for expose in fluid.get("exposes", []):
        model_id = expose.get("id", expose.get("exposeId", "default"))

        # Extract field definitions
        raw_schema = expose.get("schema", {})
        if not raw_schema:
            contract_block = expose.get("contract", {})
            if isinstance(contract_block, dict):
                raw_schema = contract_block.get("schema", {})

        fields_in = (
            raw_schema
            if isinstance(raw_schema, list)
            else (raw_schema.get("fields", []) if isinstance(raw_schema, dict) else [])
        )

        properties: List[Dict[str, Any]] = []
        for f in fields_in:
            if not isinstance(f, dict):
                continue
            prop: Dict[str, Any] = {
                "name": f.get("name", f.get("id", "unnamed")),
                "logicalType": _odcs_logical_type(f.get("type", "string")),
            }
            if f.get("description"):
                prop["description"] = f["description"]
            if f.get("required") is not None:
                prop["required"] = bool(f["required"])
            if f.get("primaryKey") or f.get("primary_key"):
                prop["primaryKey"] = True
            if f.get("sensitivity"):
                prop["classification"] = f["sensitivity"]
            properties.append(prop)

        if properties:
            schema_entry: Dict[str, Any] = {
                "name": model_id,
                "physicalType": expose.get("kind") or expose.get("type", "table"),
                "properties": properties,
            }
            schema_array.append(schema_entry)

        # Server definitions for ODCS
        provider = extract_provider_fn(expose)
        binding = expose.get("binding", {})
        if isinstance(binding, dict) and binding:
            srv: Dict[str, Any] = {}
            if provider:
                srv["type"] = _PROVIDER_TYPE_MAP.get(provider.lower(), provider.title()).lower()
            location = binding.get("location", {})
            if isinstance(location, dict):
                for k, v in location.items():
                    if v and not str(v).startswith("{{"):
                        srv[k] = v
            fmt_val = binding.get("format")
            if fmt_val:
                srv["format"] = str(fmt_val)
            if srv:
                servers.append(srv)

    if schema_array:
        dc["schema"] = schema_array
    if servers:
        dc["servers"] = servers

    # Service-level objectives (quality)
    sla = fluid.get("sla", meta.get("sla", {}))
    if isinstance(sla, dict) and sla:
        slo: Dict[str, Any] = {}
        if "freshness" in sla:
            slo["freshness"] = sla["freshness"]
        if "availability" in sla:
            slo["availability"] = sla["availability"]
        if "completeness" in sla:
            slo["completeness"] = sla["completeness"]
        if slo:
            dc["serviceLevelObjectives"] = slo

    # Tags
    tags: List[str] = []
    top_tags = fluid.get("tags", [])
    if isinstance(top_tags, list):
        tags.extend(top_tags)
    meta_tags = meta.get("tags", [])
    if isinstance(meta_tags, list):
        for tag in meta_tags:
            if tag not in tags:
                tags.append(tag)
    if tags:
        dc["tags"] = tags

    # Custom properties — ODCS uses a list of {property, value} dicts
    custom_props: List[Dict[str, Any]] = []
    labels = fluid.get("labels", {})
    if isinstance(labels, dict):
        for k, v in labels.items():
            custom_props.append({"property": k, "value": v})
    builds = fluid.get("builds", {})
    if isinstance(builds, (dict, list)) and builds:
        custom_props.append({"property": "builds", "value": builds})
    if custom_props:
        dc["customProperties"] = custom_props

    return dc


def _odcs_logical_type(fluid_type: str) -> str:
    """Map FLUID/SQL types to ODCS logical types."""
    t = fluid_type.strip().lower()
    mapping = {
        "string": "string",
        "varchar": "string",
        "text": "string",
        "char": "string",
        "integer": "integer",
        "int": "integer",
        "int64": "integer",
        "bigint": "integer",
        "smallint": "integer",
        "float": "number",
        "float64": "number",
        "double": "number",
        "decimal": "number",
        "numeric": "number",
        "boolean": "boolean",
        "bool": "boolean",
        "date": "date",
        "datetime": "timestamp",
        "timestamp": "timestamp",
        "timestamp_ntz": "timestamp",
        "time": "string",
        "json": "object",
        "struct": "object",
        "array": "array",
        "binary": "binary",
        "bytes": "binary",
    }
    return mapping.get(t, "string")


# ---- DCS 0.9.3 (opt-in via ``--contract-format dcs``) -----------


def _build_data_contract_dcs(
    fluid: Mapping[str, Any],
    product_id: str,
    *,
    derive_team_id_fn: Callable[[Mapping[str, Any]], str],
    extract_provider_fn: Callable[[Mapping[str, Any]], str],
) -> Dict[str, Any]:
    """Build a Data Contract Specification 0.9.3 payload.

    Opt-in via ``--contract-format dcs`` for Entropy Data
    deployments still on the DCS path. Default is ODCS v3.1
    (the canonical format). Both formats are produced from the
    same FLUID contract — DCS will be removed upstream after
    2026-12-31; until then, both shapes are first-class.
    """
    meta = fluid.get("metadata", {})
    contract_id = f"{product_id}-contract"

    dc: Dict[str, Any] = {
        "dataContractSpecification": "0.9.3",
        "id": contract_id,
        "info": {
            "title": meta.get("name") or fluid.get("name") or product_id,
            "version": meta.get("version", "1.0.0"),
            "description": meta.get("description") or fluid.get("description", ""),
            "owner": derive_team_id_fn(fluid),
        },
    }

    # Domain
    domain = fluid.get("domain") or meta.get("domain")
    if domain:
        dc["info"]["domain"] = str(domain)

    # Map exposes -> models + servers
    models: Dict[str, Any] = {}
    servers: Dict[str, Any] = {}
    all_dq_rules: List[Dict[str, Any]] = []

    for expose in fluid.get("exposes", []):
        model_id = expose.get("id", expose.get("exposeId", "default"))

        # Schema fields — support both flat and nested contract.schema
        schema = expose.get("schema", {})
        if not schema:
            contract_block = expose.get("contract", {})
            if isinstance(contract_block, dict):
                schema = contract_block.get("schema", {})

        fields_in = (
            schema
            if isinstance(schema, list)
            else (schema.get("fields", []) if isinstance(schema, dict) else [])
        )
        fields_out: Dict[str, Any] = {}
        for f in fields_in:
            if not isinstance(f, dict):
                continue
            fname = f.get("name", f.get("id", "unnamed"))
            fdef: Dict[str, Any] = {"type": f.get("type", "string")}
            if f.get("description"):
                fdef["description"] = f["description"]
            if f.get("required") is not None:
                fdef["required"] = bool(f["required"])
            if f.get("sensitivity"):
                fdef["classification"] = f["sensitivity"]
            fields_out[fname] = fdef
        if fields_out:
            models[model_id] = {
                "type": expose.get("kind") or expose.get("type", "table"),
                "fields": fields_out,
            }

        # Server definition from binding
        binding = expose.get("binding", {})
        if isinstance(binding, dict) and binding:
            provider = extract_provider_fn(expose)
            server_entry: Dict[str, Any] = {}

            if provider:
                server_entry["type"] = _PROVIDER_TYPE_MAP.get(provider.lower(), provider.title())

            location = binding.get("location", {})
            if isinstance(location, dict):
                # Copy location fields, skip template vars
                for k, v in location.items():
                    if v and not str(v).startswith("{{"):
                        server_entry[k] = v

            fmt_val = binding.get("format")
            if fmt_val:
                server_entry["format"] = str(fmt_val)

            if server_entry:
                servers[model_id] = server_entry

        # Collect DQ rules from policy.dq.rules
        policy = expose.get("policy", {})
        dq = policy.get("dq", {}) if isinstance(policy, dict) else {}
        rules = dq.get("rules", []) if isinstance(dq, dict) else []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            dq_entry: Dict[str, Any] = {
                "type": rule.get("type", "custom"),
                "description": rule.get("description", ""),
            }
            if rule.get("id"):
                dq_entry["id"] = rule["id"]
            if rule.get("severity"):
                dq_entry["severity"] = rule["severity"]
            if rule.get("selector"):
                dq_entry["field"] = rule["selector"]
            if rule.get("threshold") is not None:
                dq_entry["threshold"] = rule["threshold"]
            if rule.get("window"):
                dq_entry["window"] = rule["window"]
            all_dq_rules.append(dq_entry)

    if models:
        dc["models"] = models
    if servers:
        dc["servers"] = servers

    # Quality section: SLA + DQ rules
    quality: Dict[str, Any] = {}
    sla = fluid.get("sla", meta.get("sla", {}))
    if isinstance(sla, dict) and sla:
        if "freshness" in sla:
            quality["freshness"] = sla["freshness"]
        if "availability" in sla:
            quality["availability"] = sla["availability"]
        if "completeness" in sla:
            quality["completeness"] = sla["completeness"]
    if all_dq_rules:
        quality["checks"] = all_dq_rules
    if quality:
        dc["quality"] = quality

    # Builds metadata (if present)
    builds = fluid.get("builds", {})
    if isinstance(builds, dict) and builds:
        dc["custom"] = dc.get("custom", {})
        dc["custom"]["builds"] = builds

    # Governance tags & labels as custom metadata
    tags = fluid.get("tags", [])
    if not tags:
        tags = meta.get("tags", [])
    labels = fluid.get("labels", {})
    if tags or labels:
        dc["custom"] = dc.get("custom", {})
        if tags:
            dc["custom"]["tags"] = tags
        if isinstance(labels, dict) and labels:
            dc["custom"]["labels"] = labels

    return dc


# ---- team management --------------------------------------------------
