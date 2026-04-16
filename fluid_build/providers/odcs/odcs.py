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

# fluid_build/providers/odcs/odcs.py
"""
ODCS (Open Data Contract Standard) Provider

Bidirectional conversion between FLUID and ODCS v3.1.0 (Bitol.io standard).
Handles data contract schema, quality, and SLA specifications.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fluid_build.providers.base import ApplyResult, BaseProvider, ProviderError


class OdcsProvider(BaseProvider):
    """
    ODCS (Open Data Contract Standard) provider.

    Supports:
    - Export: FLUID → ODCS v3.1.0
    - Import: ODCS v3.1.0 → FLUID
    - Validation: Against JSON Schema

    Specification: https://github.com/bitol-io/open-data-contract-standard
    """

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # ODCS version
        self.odcs_version = "v3.1.0"
        self.odcs_spec_url = "https://github.com/bitol-io/open-data-contract-standard"

        # Load JSON Schema for validation
        self.schema = self._load_schema()

        # Configuration
        # Quality checks now enabled by default with ODCS v3.1.0 compliant format
        # Disable with ODCS_INCLUDE_QUALITY=false if needed
        self.include_quality_checks = os.getenv("ODCS_INCLUDE_QUALITY", "true").lower() == "true"
        self.include_sla = os.getenv("ODCS_INCLUDE_SLA", "true").lower() == "true"

    @property
    def name(self) -> str:
        return "odcs"

    def capabilities(self) -> Mapping[str, bool]:
        """Signal provider capabilities."""
        caps = super().capabilities()
        caps = dict(caps)
        caps.update(
            {
                "planning": False,
                "apply": False,
                "render": True,  # Export capability
                "validate": True,  # Validation against ODCS schema
                "supports_batch": False,
            }
        )
        return caps

    def _load_schema(self) -> Optional[Dict[str, Any]]:
        """Load ODCS JSON Schema for validation."""
        schema_path = Path(__file__).parent / "odcs-schema-v3.1.0.json"

        if not schema_path.exists():
            self.logger.warning(f"ODCS schema not found: {schema_path}")
            return None

        try:
            with open(schema_path) as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load ODCS schema: {e}")
            return None

    def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Plan not supported for conversion provider."""
        raise ProviderError(
            "ODCS provider does not support plan(). Use render() for export or import() for conversion."
        )

    def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
        """Apply not supported - use render() or import."""
        raise ProviderError(
            "ODCS provider does not support apply(). Use render() for export or import for conversion."
        )

    def render(
        self,
        src: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
        *,
        out: Optional[Union[Path, str]] = None,
        fmt: Optional[str] = "yaml",
        expose_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Export FLUID contract to ODCS format.

        Args:
            src: FLUID contract dictionary
            out: Output file path (optional)
            fmt: Output format ('yaml' or 'json')
            expose_id: When set, export only this output port (exposeId).
                       The resulting ODCS contract id is scoped to
                       ``{product_id}.{expose_id}`` and ``status`` is
                       derived from that expose's ``lifecycle.state``.

        Returns:
            ODCS-compliant dictionary
        """
        if isinstance(src, list):
            raise ProviderError(
                "ODCS export does not support batch processing. "
                "Each contract should be exported separately."
            )

        self.logger.info("Converting FLUID contract to ODCS v3.1.0")

        # Scope to a single output port when requested
        fluid = self._filter_to_expose(src, expose_id) if expose_id else src

        # Convert to ODCS
        odcs_contract = self._fluid_to_odcs(fluid)

        # Validate if schema available (optional - can be disabled)
        if self.schema and os.getenv("ODCS_VALIDATE", "false").lower() == "true":
            self.validate_contract(odcs_contract)

        # Write output if path provided
        if out:
            self._write_output(odcs_contract, out, fmt)
            self.logger.info(f"Exported ODCS contract: {out}")

        return odcs_contract

    def render_all_ports(
        self,
        fluid: Mapping[str, Any],
        *,
        out_dir: Optional[Union[Path, str]] = None,
        fmt: Optional[str] = "yaml",
    ) -> List[tuple]:
        """
        Export one ODCS contract per output port.

        Args:
            fluid: FLUID contract dictionary
            out_dir: Directory to write files into.
                     Files are named ``product.odcs.<exposeId>.<fmt>``.
                     If *None*, files are not written.
            fmt: Output format ('yaml' or 'json')

        Returns:
            List of ``(expose_id, odcs_dict)`` tuples in expose order.
        """
        import copy

        results = []
        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            eid = expose.get("exposeId") or expose.get("id")
            if not eid:
                self.logger.warning("Expose missing exposeId — skipping")
                continue
            out_path = None
            if out_dir is not None:
                out_path = Path(out_dir) / f"product.odcs.{eid}.{fmt}"
            odcs = self.render(fluid, out=out_path, fmt=fmt, expose_id=eid)
            results.append((eid, odcs))
        return results

    def _filter_to_expose(self, fluid: Mapping[str, Any], expose_id: str) -> Dict[str, Any]:
        """
        Return a shallow copy of *fluid* filtered to a single output port.

        The returned dict carries two private sentinel keys used by
        ``_fluid_to_odcs``:

        * ``_scoped_id``     – ``{product_id}.{expose_id}``
        * ``_scoped_status`` – expose-level ``lifecycle.state`` (or ``active``)
        """
        import copy

        scoped = copy.deepcopy(dict(fluid))
        exposes = [
            e
            for e in scoped.get("exposes", [])
            if isinstance(e, dict) and (e.get("exposeId") == expose_id or e.get("id") == expose_id)
        ]
        if not exposes:
            raise ProviderError(
                f"Expose '{expose_id}' not found in contract. "
                f"Available exposeIds: "
                f"{[e.get('exposeId') or e.get('id') for e in fluid.get('exposes', [])]}"
            )
        scoped["exposes"] = exposes

        # Derive scoped contract id
        product_id = self._extract_contract_id(fluid)
        scoped["_scoped_id"] = f"{product_id}.{expose_id}"

        # Derive status from expose lifecycle
        lifecycle = exposes[0].get("lifecycle", {})
        scoped["_scoped_status"] = lifecycle.get("state", "active")

        return scoped

    def import_contract(self, odcs: Union[Mapping[str, Any], str, Path]) -> Dict[str, Any]:
        """
        Import ODCS contract to FLUID format.

        Args:
            odcs: ODCS contract (dict, JSON string, or file path)

        Returns:
            FLUID contract dictionary
        """
        # Parse input
        if isinstance(odcs, (str, Path)):
            odcs_data = self._read_input(odcs)
        else:
            odcs_data = dict(odcs)

        # Validate
        if self.schema:
            self.validate_contract(odcs_data)

        self.logger.info("Converting ODCS contract to FLUID")

        # Convert to FLUID
        fluid_contract = self._odcs_to_fluid(odcs_data)

        return fluid_contract

    def _fluid_to_odcs(self, fluid: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Convert FLUID contract to ODCS.

        Args:
            fluid: FLUID contract dictionary

        Returns:
            ODCS-compliant contract dictionary
        """
        metadata = fluid.get("metadata", {})
        fluid.get("contract", {})

        # When filtered to a single expose, _scoped_id / _scoped_status are set
        contract_id = fluid.get("_scoped_id") or self._extract_contract_id(fluid)
        raw_status = fluid.get("_scoped_status") or metadata.get("status", "active")

        # Required fields
        odcs_contract = {
            "version": metadata.get("version", "1.0.0"),
            "apiVersion": self.odcs_version,
            "kind": "DataContract",
            "id": contract_id,
            "status": self._map_status_to_odcs(raw_status),
        }

        # Optional but common fields
        name = metadata.get("name")
        if name:
            odcs_contract["name"] = name

        description = metadata.get("description")
        if description:
            # ODCS description is an object, not a string
            odcs_contract["description"] = {"purpose": description}

        # Team
        team = self._extract_team(fluid)
        if team:
            odcs_contract["team"] = team

        # Tags
        tags = metadata.get("tags", [])
        if tags:
            odcs_contract["tags"] = tags

        # Schema (from exposes) - always include
        schema = self._extract_schema(fluid)
        odcs_contract["schema"] = schema

        # Servers (from expects/exposes) - always include
        servers = self._extract_servers(fluid)
        odcs_contract["servers"] = servers

        # SLA Properties
        if self.include_sla:
            sla = self._extract_sla_properties(fluid)
            if sla:
                odcs_contract["slaProperties"] = sla

        # Quality (from schema fields)
        if self.include_quality_checks:
            quality = self._extract_quality(fluid)
            if quality:
                odcs_contract["quality"] = quality

        return odcs_contract

    def _odcs_to_fluid(self, odcs: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Convert ODCS contract to FLUID.

        Args:
            odcs: ODCS contract dictionary

        Returns:
            FLUID contract dictionary
        """
        # Build FLUID structure
        fluid_contract = {
            "metadata": {
                "version": odcs.get("version", "1.0.0"),
                "name": odcs.get("name", odcs.get("id")),
                "status": self._map_status_from_odcs(odcs.get("status", "active")),
            },
            "contract": {
                "id": odcs.get("id"),
            },
            "exposes": [],
            "expects": [],
        }

        # Description
        description = odcs.get("description")
        if description:
            fluid_contract["metadata"]["description"] = description

        # Tags
        tags = odcs.get("tags", [])
        if tags:
            fluid_contract["metadata"]["tags"] = tags

        # Owner/Team
        team = odcs.get("team")
        if team:
            fluid_contract["owner"] = self._odcs_team_to_fluid_owner(team)

        # Schema → Exposes
        schema = odcs.get("schema", [])
        if schema:
            expose = self._odcs_schema_to_expose(odcs)
            if expose:
                fluid_contract["exposes"].append(expose)

        # Servers → Expects
        servers = odcs.get("servers", [])
        for server in servers:
            expect = self._odcs_server_to_expect(server)
            if expect:
                fluid_contract["expects"].append(expect)

        return fluid_contract

    def _extract_contract_id(self, fluid: Mapping[str, Any]) -> str:
        """Extract contract ID from FLUID."""
        # FLUID 0.7.1 uses top-level 'id' field
        if "id" in fluid:
            return fluid["id"]

        # Try contract.id (older format)
        contract = fluid.get("contract")
        if isinstance(contract, dict):
            contract_id = contract.get("id")
            if contract_id:
                return contract_id

        # Fallback to metadata.id
        metadata = fluid.get("metadata")
        if isinstance(metadata, dict):
            contract_id = metadata.get("id")
            if contract_id:
                return contract_id

        raise ProviderError(
            "Contract missing required 'id' field. "
            "Expected one of: fluid['id'], fluid['contract']['id'], or fluid['metadata']['id']"
        )

    def _map_status_to_odcs(self, status: str) -> str:
        """
        Map FLUID status to ODCS status.

        Mappings:
        - draft → draft
        - active → active
        - deprecated → deprecated
        - retired → retired
        - development → draft
        """
        mapping = {
            "draft": "draft",
            "active": "active",
            "deprecated": "deprecated",
            "retired": "retired",
            "development": "draft",
        }

        return mapping.get(status, "active")

    def _map_status_from_odcs(self, status: str) -> str:
        """Map ODCS status to FLUID status."""
        # Reverse mapping
        mapping = {
            "draft": "draft",
            "active": "active",
            "deprecated": "deprecated",
            "retired": "retired",
        }

        return mapping.get(status, "active")

    def _extract_team(self, fluid: Mapping[str, Any]) -> Optional[str]:
        """
        Extract team information from FLUID owner.

        ODCS v3.1.0 team can be:
        - Team object with name and members array
        - Array of team members (deprecated)
        - String team name (not valid in v3.1.0, needs to be Team object)

        We'll create a proper Team object structure.
        """
        # FLUID 0.7.1: owner may live under metadata.owner
        owner = fluid.get("owner") or fluid.get("metadata", {}).get("owner", {})
        if not isinstance(owner, dict):
            owner = {}

        team_name = owner.get("team") or owner.get("name")
        if not team_name:
            return None

        # Build Team object structure (v3.1.0+)
        team_obj = {"name": team_name}

        # Add members if available
        members = []

        # Add owner as first member if has name or email
        if owner.get("name") or owner.get("email"):
            member = {}
            if owner.get("name"):
                member["name"] = owner["name"]
            if owner.get("email"):
                member["username"] = owner["email"]  # Use username field for email
            if owner.get("role"):
                member["role"] = owner["role"]
            members.append(member)

        # Add additional contacts as members
        if "contacts" in owner:
            for contact in owner["contacts"]:
                if isinstance(contact, dict):
                    member = {}
                    if contact.get("name"):
                        member["name"] = contact["name"]
                    if contact.get("email"):
                        member["username"] = contact["email"]  # Use username field for email
                    if contact.get("role"):
                        member["role"] = contact["role"]
                    members.append(member)

        if members:
            team_obj["members"] = members

        return team_obj

    def _extract_schema(self, fluid: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract ODCS schema from FLUID exposes.

        ODCS v3.1.0 requires schema to be an array of SchemaObjects (logicalType: "object")
        with properties arrays containing the actual fields.

        Supports both FLUID 0.5.7 and 0.7.1:
        - 0.5.7: exposes.schema.fields (object with fields array)
        - 0.7.1: exposes.contract.schema (array of fields)
        """
        odcs_schema = []

        exposes = fluid.get("exposes", [])
        if not isinstance(exposes, list):
            self.logger.warning(f"exposes is not a list: {type(exposes)}")
            return odcs_schema

        for expose in exposes:
            if not isinstance(expose, dict):
                self.logger.warning(f"Skipping non-dict expose: {type(expose)}")
                continue

            contract_schema = []

            # Try 0.7.1 format first: contract.schema (array)
            contract = expose.get("contract")
            if isinstance(contract, dict):
                schema = contract.get("schema", [])
                if isinstance(schema, list):
                    contract_schema = schema

            # Fall back to 0.5.7 format: schema.fields (object)
            if not contract_schema:
                schema_obj = expose.get("schema")
                if isinstance(schema_obj, dict):
                    fields = schema_obj.get("fields", [])
                    if isinstance(fields, list):
                        contract_schema = fields

            # Process fields and create SchemaObject
            if contract_schema:
                # Get expose metadata for naming
                expose_id = expose.get("exposeId") or expose.get("id", "dataset")

                # Determine physical type from binding
                binding = expose.get("binding")
                physical_type = "table"  # default
                if isinstance(binding, dict):
                    platform = binding.get("platform")
                    if platform == "kafka":
                        physical_type = "topic"
                    elif platform in ("bigquery", "snowflake"):
                        physical_type = "table"

                # Convert fields to properties
                properties = []
                for field in contract_schema:
                    if not isinstance(field, dict):
                        self.logger.warning(f"Skipping non-dict field: {type(field)}")
                        continue
                    try:
                        odcs_property = self._fluid_field_to_odcs_property(field, expose)
                        properties.append(odcs_property)
                    except Exception as e:
                        self.logger.error(
                            f"Error converting field {field.get('name', 'unknown')}: {e}"
                        )

                # Create SchemaObject wrapping the properties
                if properties:
                    schema_object = {
                        "name": expose_id,
                        "logicalType": "object",
                        "physicalType": physical_type,
                        "properties": properties,
                    }

                    # Add description if available
                    description = expose.get("description")
                    if description:
                        schema_object["description"] = description

                    odcs_schema.append(schema_object)

        return odcs_schema

    def _fluid_field_to_odcs_property(
        self, field: Mapping[str, Any], expose: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert FLUID field to ODCS schema property (inside SchemaObject.properties array).

        Args:
            field: FLUID field dictionary
            expose: Parent expose (for context)

        Returns:
            ODCS schema property
        """
        # Required fields
        schema_entry = {
            "name": field.get("name", "unknown"),
            "logicalType": self._map_type_to_logical(field.get("type", "string")),
        }

        # Physical type (provider-specific) - try binding.platform or direct provider
        binding = expose.get("binding")
        provider = None

        # Safely extract provider from binding (handle None case)
        if isinstance(binding, dict):
            provider = binding.get("platform") or binding.get("provider")

        # Fall back to direct provider field
        if not provider:
            provider = expose.get("provider")

        if provider:
            physical_type = self._map_type_to_physical(field.get("type", "string"), provider)
            if physical_type:
                schema_entry["physicalType"] = physical_type

        # Description
        description = field.get("description")
        if description:
            schema_entry["description"] = description

        # Required flag
        required = field.get("required", False)
        schema_entry["required"] = required

        # Classification
        classification = field.get("classification")
        if classification:
            schema_entry["classification"] = classification

        # Tags
        tags = field.get("tags", [])
        if tags:
            schema_entry["tags"] = tags

        # Quality checks
        if self.include_quality_checks:
            quality = self._extract_field_quality(field)
            if quality:
                schema_entry["quality"] = quality

        return schema_entry

    def _map_type_to_logical(self, fluid_type: str) -> str:
        """
        Map FLUID type to ODCS logicalType.

        ODCS v3.1.0 valid logicalTypes: string, date, timestamp, time, number, integer, object, array, boolean

        Args:
            fluid_type: FLUID field type

        Returns:
            ODCS logical type
        """
        mapping = {
            "string": "string",
            "text": "string",
            "varchar": "string",
            "char": "string",
            "int": "integer",
            "integer": "integer",
            "bigint": "integer",
            "long": "integer",
            "float": "number",
            "double": "number",
            "decimal": "number",
            "numeric": "number",
            "bool": "boolean",
            "boolean": "boolean",
            "date": "date",
            "datetime": "timestamp",
            "timestamp": "timestamp",
            "time": "time",
            "json": "object",
            "object": "object",
            "array": "array",
            "binary": "string",  # ODCS doesn't have binary type, use string
            "bytes": "string",  # ODCS doesn't have binary type, use string
        }

        return mapping.get(fluid_type.lower(), "string")

    def _map_type_to_physical(self, fluid_type: str, provider: Optional[str]) -> Optional[str]:
        """
        Map FLUID type to physical type for specific provider.

        Args:
            fluid_type: FLUID field type
            provider: Provider name (gcp, snowflake, etc.) or None

        Returns:
            Physical type string or None
        """
        if not provider:
            return self._map_type_to_logical(fluid_type)

        provider = provider.lower()

        # BigQuery types
        if provider == "gcp" or provider == "bigquery":
            mapping = {
                "string": "STRING",
                "text": "STRING",
                "int": "INT64",
                "integer": "INT64",
                "bigint": "INT64",
                "long": "INT64",
                "float": "FLOAT64",
                "double": "FLOAT64",
                "decimal": "NUMERIC",
                "numeric": "NUMERIC",
                "bool": "BOOL",
                "boolean": "BOOL",
                "date": "DATE",
                "datetime": "DATETIME",
                "timestamp": "TIMESTAMP",
                "time": "TIME",
                "json": "JSON",
                "object": "STRUCT",
                "array": "ARRAY",
                "binary": "BYTES",
                "bytes": "BYTES",
            }
            return mapping.get(fluid_type.lower())

        # Snowflake types
        elif provider == "snowflake":
            mapping = {
                "string": "VARCHAR",
                "text": "TEXT",
                "int": "NUMBER",
                "integer": "NUMBER",
                "bigint": "NUMBER",
                "long": "NUMBER",
                "float": "FLOAT",
                "double": "DOUBLE",
                "decimal": "DECIMAL",
                "numeric": "DECIMAL",
                "bool": "BOOLEAN",
                "boolean": "BOOLEAN",
                "date": "DATE",
                "datetime": "TIMESTAMP_NTZ",
                "timestamp": "TIMESTAMP_NTZ",
                "time": "TIME",
                "json": "VARIANT",
                "object": "OBJECT",
                "array": "ARRAY",
                "binary": "BINARY",
                "bytes": "BINARY",
            }
            return mapping.get(fluid_type.lower())

        # Generic fallback: use logical type for unknown providers
        return self._map_type_to_logical(fluid_type)

    def _extract_field_quality(self, field: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """
        Extract quality checks from FLUID field and convert to ODCS v3.1.0 format.

        ODCS v3.1.0 quality check structure:
        - type: "library", "text", "sql", or "custom"
        - For library type: metric (nullValues, missingValues, invalidValues, duplicateValues, rowCount)
          + ONE operator property (mustBe, mustNotBe, mustBeGreaterThan, etc.)
        - For text type: description (human-readable)

        Example:
        [
            {
                "type": "library",
                "metric": "nullValues",
                "mustBe": 0,
                "dimension": "completeness",
                "description": "Field must not contain null values"
            }
        ]
        """
        quality_checks = []

        # 1. Required field check (not null constraint)
        if field.get("required"):
            quality_checks.append(
                {
                    "type": "library",
                    "metric": "nullValues",
                    "mustBe": 0,
                    "dimension": "completeness",
                    "description": f"Field '{field.get('name', 'unknown')}' must not contain null values",
                }
            )

        # 2. Primary key check (uniqueness + not null)
        tags = field.get("tags", [])
        is_primary_key = "primary-key" in tags or "primaryKey" in tags

        if is_primary_key:
            # Primary keys must be unique
            quality_checks.append(
                {
                    "type": "library",
                    "metric": "duplicateValues",
                    "mustBe": 0,
                    "dimension": "uniqueness",
                    "description": f"Primary key field '{field.get('name', 'unknown')}' must contain only unique values",
                }
            )
            # Also ensure not null if not already added
            if not field.get("required"):
                quality_checks.append(
                    {
                        "type": "library",
                        "metric": "nullValues",
                        "mustBe": 0,
                        "dimension": "completeness",
                        "description": f"Primary key field '{field.get('name', 'unknown')}' must not contain null values",
                    }
                )

        # 3. Check for explicit validations (can be list or dict)
        validations = field.get("validations", [])

        # Handle both list and dict formats
        if isinstance(validations, dict):
            # Legacy dict format: {"pattern": "regex", "min_length": 5}
            validation_list = [{"type": k, "value": v} for k, v in validations.items()]
        elif isinstance(validations, list):
            validation_list = validations
        else:
            validation_list = []

        # Process validation list
        for validation in validation_list:
            if not isinstance(validation, dict):
                continue

            val_type = validation.get("type", "")
            val_value = validation.get("value")
            val_values = validation.get("values")
            field_name = field.get("name", "unknown")

            # Pattern/regex validation
            if val_type in ("pattern", "regex") and val_value:
                quality_checks.append(
                    {
                        "type": "text",
                        "description": f"Field '{field_name}' must match pattern: {val_value}",
                    }
                )

            # Min/max length constraints
            elif val_type == "min_length" and val_value is not None:
                quality_checks.append(
                    {
                        "type": "text",
                        "description": f"Field '{field_name}' must have minimum length of {val_value}",
                    }
                )

            elif val_type == "max_length" and val_value is not None:
                quality_checks.append(
                    {
                        "type": "text",
                        "description": f"Field '{field_name}' must have maximum length of {val_value}",
                    }
                )

            # Min/max value constraints (for numeric fields)
            elif val_type == "min_value" and val_value is not None:
                quality_checks.append(
                    {
                        "type": "text",
                        "description": f"Field '{field_name}' must be greater than or equal to {val_value}",
                    }
                )

            elif val_type == "max_value" and val_value is not None:
                quality_checks.append(
                    {
                        "type": "text",
                        "description": f"Field '{field_name}' must be less than or equal to {val_value}",
                    }
                )

            # Allowed values / enum constraints
            elif val_type in ("allowed_values", "enum") and val_values:
                values_str = ", ".join(str(v) for v in val_values[:5])
                if len(val_values) > 5:
                    values_str += f", ... ({len(val_values)} total)"
                quality_checks.append(
                    {
                        "type": "text",
                        "description": f"Field '{field_name}' must be one of: {values_str}",
                    }
                )

            # Not null constraint
            elif val_type == "not_null" and val_value:
                if not field.get("required"):  # Avoid duplicate if already added
                    quality_checks.append(
                        {
                            "type": "library",
                            "metric": "nullValues",
                            "mustBe": 0,
                            "dimension": "completeness",
                            "description": f"Field '{field_name}' must not contain null values",
                        }
                    )

            # Unique constraint
            elif val_type == "unique" and val_value:
                if not is_primary_key:  # Avoid duplicate if already added for PK
                    quality_checks.append(
                        {
                            "type": "library",
                            "metric": "duplicateValues",
                            "mustBe": 0,
                            "dimension": "uniqueness",
                            "description": f"Field '{field_name}' must contain only unique values",
                        }
                    )

        # 4. Custom quality checks from field metadata
        custom_quality = field.get("quality")
        if isinstance(custom_quality, list):
            for qc in custom_quality:
                if isinstance(qc, dict):
                    # If it's already in ODCS format, keep it
                    if "type" in qc and "metric" in qc:
                        quality_checks.append(qc)
                    elif "type" in qc and qc.get("type") == "text":
                        quality_checks.append(qc)
                    # Otherwise convert legacy format
                    elif "description" in qc:
                        quality_checks.append({"type": "text", "description": qc["description"]})

        return quality_checks if quality_checks else None

    def _extract_servers(self, fluid: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract ODCS servers from FLUID expects/exposes.

        Servers define where data is stored/accessed.
        """
        servers = []

        # From exposes (data source locations)
        exposes = fluid.get("exposes", [])
        if isinstance(exposes, list):
            for expose in exposes:
                try:
                    server = self._expose_to_server(expose)
                    if server and isinstance(server, dict):
                        servers.append(server)
                except Exception as e:
                    self.logger.error(f"Error extracting server from expose: {e}")

        # From expects (dependencies)
        expects = fluid.get("expects", [])
        if isinstance(expects, list):
            for expect in expects:
                try:
                    server = self._expect_to_server(expect)
                    if server and isinstance(server, dict):
                        servers.append(server)
                except Exception as e:
                    self.logger.error(f"Error extracting server from expect: {e}")

        return servers

    def _expose_to_server(self, expose: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert FLUID expose to ODCS server."""
        if not isinstance(expose, dict):
            self.logger.warning(f"Skipping non-dict expose: {type(expose)}")
            return None

        # Try 0.7.1 format: binding.platform
        binding = expose.get("binding")
        provider = None
        location = None

        # Safely extract from binding (handle None case)
        if isinstance(binding, dict):
            provider = binding.get("platform") or binding.get("provider")
            location = binding.get("location")

        # Fall back to 0.5.7 format: direct provider field
        if not provider:
            provider = expose.get("provider")

        if not provider:
            return None

        # Use exposeId (0.7.1) or id (0.5.7)
        expose_id = expose.get("exposeId") or expose.get("id", "default")

        server = {
            "id": expose_id,
            "server": expose_id,
            "type": self._map_provider_to_server_type(provider),
        }

        # Location/connection details - ensure it's a dict
        if not isinstance(location, dict):
            # Fall back to direct location (0.5.7)
            location = expose.get("location")

        if isinstance(location, dict) and location:
            try:
                server.update(self._extract_server_details(location, provider))
            except Exception as e:
                self.logger.error(f"Error extracting server details: {e}")

        return server

    def _expect_to_server(self, expect: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert FLUID expect to ODCS server."""
        if not isinstance(expect, dict):
            self.logger.warning(f"Skipping non-dict expect: {type(expect)}")
            return None

        # Try binding.platform (0.7.1) or direct provider (0.5.7)
        binding = expect.get("binding")
        provider = None
        location = None

        # Safely extract from binding (handle None case)
        if isinstance(binding, dict):
            provider = binding.get("platform") or binding.get("provider")
            location = binding.get("location")

        if not provider:
            provider = expect.get("provider")

        if not provider:
            return None

        expect_id = expect.get("id", "dependency")
        server = {
            "id": expect_id,
            "server": expect_id,
            "type": self._map_provider_to_server_type(provider),
        }

        # Location/connection details - ensure it's a dict
        if not isinstance(location, dict):
            location = expect.get("location")

        if isinstance(location, dict) and location:
            try:
                server.update(self._extract_server_details(location, provider))
            except Exception as e:
                self.logger.error(f"Error extracting server details: {e}")

        return server

    def _map_provider_to_server_type(self, provider: str) -> str:
        """
        Map FLUID provider to ODCS server type.

        ODCS supports 30+ server types.
        """
        mapping = {
            "gcp": "bigquery",
            "bigquery": "bigquery",
            "snowflake": "snowflake",
            "aws": "s3",
            "s3": "s3",
            "redshift": "redshift",
            "athena": "athena",
            "azure": "azure",
            "databricks": "databricks",
            "postgres": "postgres",
            "postgresql": "postgres",
            "mysql": "mysql",
            "kafka": "kafka",
            "mongodb": "mongodb",
            "elasticsearch": "elasticsearch",
            "local": "local",
        }

        return mapping.get(provider.lower(), "custom")

    def _extract_server_details(self, location: Mapping[str, Any], provider: str) -> Dict[str, Any]:
        """
        Extract server connection details from location.

        Returns provider-specific fields for ODCS server.
        """
        details = {}

        provider = provider.lower()

        # BigQuery
        if provider in ("gcp", "bigquery"):
            if "project" in location:
                details["project"] = location["project"]
            if "dataset" in location:
                details["dataset"] = location["dataset"]

        # Snowflake
        elif provider == "snowflake":
            if "account" in location:
                details["account"] = location["account"]
            if "database" in location:
                details["database"] = location["database"]
            if "schema" in location:
                details["schema"] = location["schema"]
            if "table" in location:
                details["table"] = location["table"]

        # S3
        elif provider in ("aws", "s3"):
            if "bucket" in location:
                details["bucket"] = location["bucket"]
            if "path" in location or "key" in location:
                details["path"] = location.get("path") or location.get("key")
            if "region" in location:
                details["region"] = location["region"]

        # Kafka
        elif provider == "kafka":
            # ODCS Kafka servers require 'host' property
            # Extract host from topic name or location
            if "host" in location:
                details["host"] = location["host"]
            elif "account" in location:
                # Use account as host for streaming platform
                details["host"] = location["account"]

            # Format is optional
            if "format" in location:
                details["format"] = location["format"]

        # Generic fields for other providers
        else:
            # Copy all location fields
            details.update(location)

        return details

    def _extract_sla_properties(self, fluid: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """
        Extract SLA properties from FLUID contract.

        ODCS SLA structure:
        {
            "interval": "daily",
            "sla": "00:00",
            "completenessKpi": 0.95,
            ...
        }

        Sources (in priority order):
        1. expose-level ``qos`` block  (FLUID 0.7.1)
        2. ``metadata.update_frequency`` / ``metadata.availability`` (legacy)
        """
        sla_values: Dict[str, Any] = {}

        # --- 1. Expose-level qos block (FLUID 0.7.1) -----------------------
        # When scoped to a single expose via render(expose_id=...) there will
        # be exactly one expose; otherwise we merge qos from all exposes.
        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            qos = expose.get("qos", {})
            if not isinstance(qos, dict):
                continue

            # availability: "99.5%" → strip % and convert to float
            avail = qos.get("availability")
            if avail is not None and "availability" not in sla_values:
                try:
                    avail_str = str(avail).strip()
                    if avail_str.endswith("%"):
                        sla_values["availability"] = float(avail_str.rstrip("%")) / 100
                    else:
                        parsed = float(avail_str)
                        sla_values["availability"] = parsed / 100 if parsed > 1 else parsed
                except (ValueError, TypeError):
                    sla_values["availability"] = str(avail)

            # freshnessSLO: ISO-8601 duration string (e.g. "PT5M") → interval
            freshness = qos.get("freshnessSLO") or qos.get("freshness_slo")
            if freshness and "interval" not in sla_values:
                sla_values["interval"] = str(freshness)

            # SLA labels as custom properties
            qos_labels = qos.get("labels", {})
            if isinstance(qos_labels, dict) and qos_labels:
                for key, value in qos_labels.items():
                    sla_values.setdefault(f"label:{key}", value)

        # --- 2. Metadata-level fallbacks (legacy) ---------------------------
        metadata = fluid.get("metadata", {})

        # Update frequency → interval (if not already set from qos)
        update_frequency = metadata.get("update_frequency")
        if update_frequency and "interval" not in sla_values:
            sla_values["interval"] = update_frequency

        # Availability/uptime (if not already set from qos)
        availability = metadata.get("availability")
        if availability and "availability" not in sla_values:
            try:
                availability_f = float(availability)
                sla_values["availability"] = (
                    availability_f / 100 if availability_f > 1 else availability_f
                )
            except (ValueError, TypeError):
                pass

        # Quality thresholds
        quality = metadata.get("quality_threshold")
        if quality:
            try:
                sla_values["completenessKpi"] = float(quality)
            except (ValueError, TypeError):
                pass

        if not sla_values:
            return None

        return [
            {"property": property_name, "value": value}
            for property_name, value in sla_values.items()
            if value is not None
        ]

    def validate_contract(self, odcs: Mapping[str, Any]) -> None:
        """Validate an ODCS contract payload against the configured schema."""
        self._validate_odcs(odcs)

    def _extract_quality(self, fluid: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract quality properties from FLUID contract.

        ODCS quality structure:
        {
            "type": "SodaCL",
            "specification": "..."
        }
        """
        # Check for quality definitions
        quality_spec = fluid.get("quality", {})

        if not quality_spec:
            return None

        quality_type = quality_spec.get("type", "custom")
        specification = quality_spec.get("specification", "")

        return {"type": quality_type, "specification": specification}

    def _odcs_team_to_fluid_owner(self, team: str) -> Dict[str, Any]:
        """Convert ODCS team (string) to FLUID owner."""
        return {
            "team": team,
            "name": team,
        }

    def _odcs_schema_to_expose(self, odcs: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Convert ODCS schema to FLUID expose.

        Groups all schema fields into one expose.
        """
        schema = odcs.get("schema", [])
        if not schema:
            return None

        expose = {
            "id": odcs.get("id", "default"),
            "version": odcs.get("version", "1.0.0"),
            "description": odcs.get("description", ""),
            "schema": {"fields": []},
        }

        # Convert each schema entry to field
        for schema_entry in schema:
            field = self._odcs_schema_to_field(schema_entry)
            expose["schema"]["fields"].append(field)

        return expose

    def _odcs_schema_to_field(self, schema_entry: Mapping[str, Any]) -> Dict[str, Any]:
        """Convert ODCS schema entry to FLUID field."""
        field = {
            "name": schema_entry.get("name", "unknown"),
            "type": self._map_logical_type_to_fluid(schema_entry.get("logicalType", "string")),
        }

        # Description
        description = schema_entry.get("description")
        if description:
            field["description"] = description

        # Required (inverse of isNullable)
        is_nullable = schema_entry.get("isNullable", True)
        field["required"] = not is_nullable

        # Classification
        classification = schema_entry.get("classification")
        if classification:
            field["classification"] = classification

        # Tags
        tags = schema_entry.get("tags", [])
        if tags:
            field["tags"] = tags

        return field

    def _map_logical_type_to_fluid(self, logical_type: str) -> str:
        """Map ODCS logical type to FLUID type."""
        mapping = {
            "string": "string",
            "integer": "int",
            "long": "bigint",
            "float": "float",
            "double": "double",
            "decimal": "decimal",
            "boolean": "bool",
            "date": "date",
            "timestamp": "timestamp",
            "time": "time",
            "object": "object",
            "array": "array",
            "binary": "binary",
        }

        return mapping.get(logical_type.lower(), "string")

    def _odcs_server_to_expect(self, server: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert ODCS server to FLUID expect."""
        server_type = server.get("type")
        if not server_type:
            return None

        expect = {
            "id": server.get("name", "dependency"),
            "provider": self._map_server_type_to_provider(server_type),
        }

        # Extract location details
        location = self._extract_location_from_server(server)
        if location:
            expect["location"] = location

        return expect

    def _map_server_type_to_provider(self, server_type: str) -> str:
        """Map ODCS server type to FLUID provider."""
        mapping = {
            "bigquery": "gcp",
            "snowflake": "snowflake",
            "s3": "aws",
            "redshift": "aws",
            "athena": "aws",
            "azure": "azure",
            "databricks": "databricks",
            "postgres": "postgres",
            "mysql": "mysql",
            "kafka": "kafka",
            "mongodb": "mongodb",
            "elasticsearch": "elasticsearch",
            "local": "local",
        }

        return mapping.get(server_type.lower(), "custom")

    def _extract_location_from_server(self, server: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract location details from ODCS server."""
        location = {}

        # Copy relevant fields
        for key in [
            "project",
            "dataset",
            "table",
            "account",
            "database",
            "schema",
            "bucket",
            "path",
            "region",
            "host",
            "port",
        ]:
            if key in server:
                location[key] = server[key]

        return location

    def _validate_odcs(self, odcs: Mapping[str, Any]) -> None:
        """
        Validate ODCS contract against JSON Schema.

        Args:
            odcs: ODCS contract to validate

        Raises:
            ProviderError: If validation fails
        """
        if not self.schema:
            self.logger.warning("ODCS schema not available, skipping validation")
            return

        try:
            import jsonschema

            jsonschema.validate(instance=odcs, schema=self.schema)
            self.logger.info("ODCS contract validated successfully")
        except ImportError:
            self.logger.warning("jsonschema not installed, skipping validation")
        except jsonschema.ValidationError as e:
            raise ProviderError(f"ODCS validation failed: {e.message}")

    def _read_input(self, path: Union[str, Path]) -> Dict[str, Any]:
        """Read ODCS contract from file."""
        input_path = Path(path) if not isinstance(path, Path) else path

        with open(input_path) as f:
            if input_path.suffix in (".yaml", ".yml"):
                import yaml

                return yaml.safe_load(f)
            else:  # JSON
                return json.load(f)

    def _write_output(self, data: Dict[str, Any], path: Union[Path, str], fmt: str) -> None:
        """Write ODCS contract to file."""
        output_path = Path(path) if not isinstance(path, Path) else path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as f:
            if fmt == "yaml":
                import yaml

                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            else:  # json
                json.dump(data, f, indent=2)
