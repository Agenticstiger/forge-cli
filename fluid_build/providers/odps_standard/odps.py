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

# fluid_build/providers/odps_standard/odps.py
"""
ODPS-Bitol (Bitol.io Data Product Standard) Provider

Converts FLUID contracts to ODPS-Bitol v1.0.0 format.
Used for data marketplace integration with Entropy Data.

Note: This is Bitol.io's proprietary ODPS variant, distinct from the
official ODPS v4.1 (Linux Foundation / Open Data Product Initiative).

Specification: https://github.com/bitol-io/open-data-product-standard
Marketplace: https://entropy-data.com
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fluid_build.providers.base import ApplyResult, BaseProvider, ProviderError
from fluid_build.util.contract import (
    consumes_to_canonical_ports,
    get_owner,
)


class OdpsStandardProvider(BaseProvider):
    """
    ODPS-Bitol (Bitol.io Data Product Standard) exporter.

    Converts FLUID contracts into ODPS-Bitol v1.0.0 format for data marketplace
    integration, particularly with Entropy Data.

    This is Bitol.io's variant. For the official ODPS v4.1 (Linux Foundation),
    use the 'odps' provider (accessed via 'fluid opds' command).

    Specification: https://github.com/bitol-io/open-data-product-standard
    """

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # ODPS version
        self.odps_version = "v1.0.0"
        self.odps_spec_url = "https://github.com/bitol-io/open-data-product-standard"

        # Configuration
        self.include_custom_properties = os.getenv("ODPS_INCLUDE_CUSTOM", "true").lower() == "true"
        self.default_port_version = os.getenv("ODPS_DEFAULT_PORT_VERSION", "1.0.0")

    @property
    def name(self) -> str:
        return "odps-standard"

    def capabilities(self) -> Mapping[str, bool]:
        """Signal exporter capabilities."""
        caps = super().capabilities()
        caps = dict(caps)
        caps.update(
            {
                "planning": False,
                "apply": False,
                "render": True,  # Primary capability
                "validate": False,
                "supports_batch": False,  # ODPS is per-product
            }
        )
        return caps

    def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Plan not supported for export-only provider."""
        raise ProviderError("ODPS provider does not support plan(). Use render() for export.")

    def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
        """Apply not supported - use render() instead."""
        raise ProviderError("ODPS provider does not support apply(). Use render() for export.")

    def render(
        self,
        src: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
        *,
        out: Optional[Union[Path, str]] = None,
        fmt: Optional[str] = "yaml",
    ) -> Dict[str, Any]:
        """
        Export FLUID contract to ODPS format.

        Args:
            src: FLUID contract dictionary
            out: Output file path (optional)
            fmt: Output format ('yaml' or 'json')

        Returns:
            ODPS-compliant dictionary
        """
        if isinstance(src, list):
            raise ProviderError(
                "ODPS export does not support batch processing. "
                "Each data product should be exported separately."
            )

        self.logger.info("Converting FLUID contract to ODPS v1.0.0")

        # Convert to ODPS
        odps_product = self._fluid_to_odps(src)

        # Write output if path provided
        if out:
            self._write_output(odps_product, out, fmt)
            self.logger.info(f"Exported ODPS data product: {out}")

        return odps_product

    def _fluid_to_odps(self, fluid: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Convert FLUID contract to ODPS data product.

        Args:
            fluid: FLUID contract dictionary

        Returns:
            ODPS-compliant data product dictionary
        """
        metadata = fluid.get("metadata", {})
        fluid.get("contract", {})

        # Build ODPS structure
        odps_product = {
            "apiVersion": self.odps_version,
            "kind": "DataProduct",
            "id": self._extract_id(fluid),
            "name": self._extract_name(fluid),
            "status": self._extract_status(metadata),
        }

        # Optional fields
        description = self._extract_description(fluid)
        if description:
            odps_product["description"] = description

        team = self._extract_team(fluid)
        if team:
            odps_product["team"] = team

        tags = metadata.get("tags") or fluid.get("tags") or []
        if tags:
            odps_product["tags"] = tags

        input_ports = self._extract_input_ports(fluid)
        if input_ports:
            odps_product["inputPorts"] = input_ports

        # Output ports (from exposes) - always include
        output_ports = self._extract_output_ports(fluid)
        odps_product["outputPorts"] = output_ports

        # Custom properties
        if self.include_custom_properties:
            custom_props = self._extract_custom_properties(fluid)
            if custom_props:
                odps_product["customProperties"] = custom_props

        return odps_product

    def _extract_id(self, fluid: Mapping[str, Any]) -> str:
        """Extract data product ID."""
        # FLUID 0.7.1 uses top-level 'id' field
        if "id" in fluid:
            return fluid["id"]

        # Try contract.id (older format)
        contract_id = fluid.get("contract", {}).get("id")
        if contract_id:
            return contract_id

        # Fall back to metadata.id
        metadata_id = fluid.get("metadata", {}).get("id")
        if metadata_id:
            return metadata_id

        raise ProviderError("Contract missing required 'id' field")

    def _extract_name(self, fluid: Mapping[str, Any]) -> str:
        """Extract data product name.

        Checks metadata.name first (explicit override), then falls back to
        the top-level 'name' field which is the standard FLUID contract location.
        """
        name = fluid.get("metadata", {}).get("name") or fluid.get("name")
        if not name:
            raise ProviderError(
                "Contract missing required name field. "
                "Set 'name:' at the contract root (or 'metadata.name' to override)."
            )
        return name

    def _extract_status(self, metadata: Mapping[str, Any]) -> str:
        """
        Extract and map status.

        FLUID -> ODPS mapping:
        - draft -> draft
        - active -> active
        - deprecated -> deprecated
        - retired -> retired
        - development -> draft
        """
        status = metadata.get("status", "draft")

        mapping = {
            "draft": "draft",
            "active": "active",
            "deprecated": "deprecated",
            "retired": "retired",
            "development": "draft",
        }

        return mapping.get(status.lower(), "draft")

    def _extract_description(self, fluid: Mapping[str, Any]) -> Optional[Dict[str, str]]:
        """
        Extract description as ODPS structure.

        Returns:
            {"purpose": "..."} or None
        """
        metadata = fluid.get("metadata", {})
        description = metadata.get("description") or fluid.get("description")
        if not description:
            return None

        return {"purpose": description}

    def _extract_team(self, fluid: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract team information as ODPS-Bitol v1.0.0-conformant shape.

        The v1.0.0 ``Team`` schema (``additionalProperties: false``) permits
        only: ``name, description, members, tags, customProperties,
        authoritativeDefinitions``. It does NOT permit a ``contacts`` key.

        FLUID's ``metadata.owner`` structure carries name+email+team+contacts
        in one dict. We map that to v1.0.0 as follows:

        * ``owner.team`` / ``owner.name`` → ``team.name``
        * ``owner.name`` + ``owner.email`` → one ``members[]`` entry with
          ``{username: email, name}`` (TeamMember requires ``username``)
        * ``owner.contacts[]`` → additional ``members[]`` entries with
          ``{username: email or name, name: name}``
        """
        owner = get_owner(fluid)
        if not owner:
            return None

        team: Dict[str, Any] = {}

        # Team name
        team_name = owner.get("team") or owner.get("name")
        if team_name:
            team["name"] = team_name

        # Members list (replaces the forbidden 'contacts' key). ODPS-Bitol
        # TeamMember requires 'username'; use email when available, fall
        # back to name so we always produce a valid member.
        members: list = []

        if owner.get("name") or owner.get("email"):
            member: Dict[str, Any] = {}
            username = owner.get("email") or owner.get("name") or ""
            if username:
                member["username"] = str(username)
            if owner.get("name"):
                member["name"] = str(owner["name"])
            if owner.get("role"):
                member["role"] = str(owner["role"])
            if member.get("username"):
                members.append(member)

        if "contacts" in owner and isinstance(owner["contacts"], list):
            for c in owner["contacts"]:
                if not isinstance(c, dict):
                    continue
                username = c.get("username") or c.get("email") or c.get("name")
                if not username:
                    continue
                entry: Dict[str, Any] = {"username": str(username)}
                if c.get("name"):
                    entry["name"] = str(c["name"])
                if c.get("role"):
                    entry["role"] = str(c["role"])
                members.append(entry)

        if members:
            team["members"] = members

        return team if team else None

    def _extract_input_ports(self, fluid: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Map FLUID ``consumes[]`` to ODPS-Bitol v1.0.0 input ports.

        ODPS-Bitol v1.0.0 ``InputPort`` (``additionalProperties: false``) permits
        only: ``name, version, contractId, tags, customProperties,
        authoritativeDefinitions``. Required: ``name, version, contractId``.

        Two divergences from the previous shape:

        * ``id``, ``description``, ``reference``, ``required``, ``sourceSystemId``
          are NOT permitted on ``InputPort`` under v1.0.0. Stripped. If the
          host repo uses them downstream (e.g. for DMM provider overlay),
          that extension should happen after this provider returns — not
          be baked into the v1.0.0 artifact.
        * ``contractId`` is REQUIRED. We synthesize when the FLUID consume
          didn't carry one explicitly, in order of preference:
            1. Canonical ``contract_id`` field (FLUID's explicit field)
            2. Canonical ``reference`` field (source's fully-qualified name
               — stable identifier for the contract's upstream source)
            3. Canonical ``name`` (last-resort stable identifier)

        Uses :func:`consumes_to_canonical_ports` for v0.7.x
        compatibility.
        """
        canonical_ports = consumes_to_canonical_ports(
            fluid,
            default_version=self.default_port_version,
            logger=self.logger,
        )

        input_ports: List[Dict[str, Any]] = []
        for canonical in canonical_ports:
            # Synthesize contractId via fallback chain so the emitted port
            # always carries the v1.0.0-required field.
            contract_id = (
                canonical.get("contract_id") or canonical.get("reference") or canonical.get("name")
            )
            port: Dict[str, Any] = {
                "name": canonical["name"],
                "version": canonical["version"],
                "contractId": str(contract_id) if contract_id else canonical["name"],
            }
            input_ports.append(port)

        return input_ports

    def _extract_output_ports(self, fluid: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract output ports from exposes section.

        Each expose becomes an output port.
        """
        output_ports = []

        for expose in fluid.get("exposes", []):
            port = self._expose_to_output_port(expose, fluid)
            output_ports.append(port)

        return output_ports

    def _expose_to_output_port(
        self, expose: Mapping[str, Any], fluid: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert FLUID expose to ODPS output port.

        Args:
            expose: FLUID expose dictionary
            fluid: Full FLUID contract (for context)

        Returns:
            ODPS output port dictionary
        """
        expose_id = self._extract_expose_id(expose)
        metadata = fluid.get("metadata") if isinstance(fluid.get("metadata"), Mapping) else {}
        port_version = expose.get("version") or metadata.get("version") or self.default_port_version
        # ODPS-Bitol v1.0.0 OutputPort (``additionalProperties: false``) does
        # NOT permit an ``id`` field. The expose identifier travels via
        # ``name`` instead. Keep ``id`` OUT of the emitted port dict.
        port = {
            "name": expose_id,
            "version": str(port_version),
            "description": expose.get("description", ""),
        }

        # Map provider to type
        provider = self._extract_expose_provider(expose)
        if provider:
            port["type"] = self._map_provider_to_type(provider)

        # Contract ID (reference to ODCS contract) — only when explicitly set.
        # The DMM provider layer overlays a deterministic contractId when
        # ``publish_contract=True``; we deliberately do not fabricate one
        # here so that standalone renders don't leak dangling references.
        contract_id = expose.get("contract_id") or expose.get("contractId")
        if contract_id:
            port["contractId"] = contract_id

        # Custom properties (server details, etc.)
        custom_props = self._extract_port_custom_properties(expose, fluid)
        if custom_props:
            port["customProperties"] = custom_props

        return port

    def _extract_expose_id(self, expose: Mapping[str, Any]) -> str:
        expose_id = expose.get("exposeId") or expose.get("id")
        if not expose_id:
            raise ProviderError("Expose missing required id/exposeId field")
        return str(expose_id)

    def _extract_expose_provider(self, expose: Mapping[str, Any]) -> Optional[str]:
        provider = expose.get("provider")
        if provider:
            return str(provider)

        binding = expose.get("binding")
        if isinstance(binding, Mapping):
            platform = binding.get("platform")
            if platform:
                return str(platform)

        return None

    def _extract_expose_location(self, expose: Mapping[str, Any]) -> Any:
        binding = expose.get("binding")
        if isinstance(binding, Mapping) and "location" in binding:
            return binding.get("location")
        return expose.get("location")

    def _extract_expose_schema(self, expose: Mapping[str, Any]) -> Any:
        contract = expose.get("contract")
        if isinstance(contract, Mapping) and "schema" in contract:
            return contract.get("schema")
        return expose.get("schema", {})

    def _map_provider_to_type(self, provider: str) -> str:
        """
        Map FLUID provider to ODPS type.

        Args:
            provider: FLUID provider name

        Returns:
            ODPS type string
        """
        mapping = {
            "gcp": "bigquery",
            "snowflake": "snowflake",
            "aws": "s3",
            "azure": "azure",
            "databricks": "databricks",
            "postgres": "postgres",
            "postgresql": "postgres",
            "mysql": "mysql",
            "kafka": "kafka",
            "local": "local",
        }

        # Default to "custom" for unknown providers
        return mapping.get(provider.lower(), "custom")

    def _extract_port_custom_properties(
        self, expose: Mapping[str, Any], fluid: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Extract custom properties for output port.

        Includes server details, PII flags, etc.
        """
        custom_props = []

        # Server/location details
        location = self._extract_expose_location(expose)
        if location and isinstance(location, dict):
            custom_props.append({"property": "server", "value": location})

        # Environment
        environment = fluid.get("metadata", {}).get("environment")
        if environment:
            custom_props.append({"property": "environment", "value": environment})

        # PII flag (from schema or metadata)
        contains_pii = self._check_contains_pii(expose)
        custom_props.append({"property": "containsPii", "value": contains_pii})

        # Auto-approve flag (from access control)
        auto_approve = self._check_auto_approve(expose, fluid)
        custom_props.append({"property": "autoApprove", "value": auto_approve})

        # Status
        status = expose.get("status") or fluid.get("metadata", {}).get("status", "active")
        custom_props.append({"property": "status", "value": status})

        return custom_props

    def _check_contains_pii(self, expose: Mapping[str, Any]) -> bool:
        """Check if expose contains PII data."""
        # Check schema fields for PII classification
        schema = self._extract_expose_schema(expose)
        if isinstance(schema, Mapping):
            fields = schema.get("fields", [])
        elif isinstance(schema, list):
            fields = schema
        else:
            fields = []

        for field in fields:
            classification = field.get("classification", "").lower()
            if "pii" in classification or "sensitive" in classification:
                return True

        # Check expose-level classification
        if "pii" in expose.get("classification", "").lower():
            return True

        return False

    def _check_auto_approve(self, expose: Mapping[str, Any], fluid: Mapping[str, Any]) -> bool:
        """Check if access should be auto-approved."""
        # Check access control settings
        access_control = fluid.get("access_control", {})

        # If PII, don't auto-approve
        if self._check_contains_pii(expose):
            return False

        # Check explicit auto-approve setting
        auto_approve = access_control.get("auto_approve")
        if auto_approve is not None:
            return bool(auto_approve)

        # Default: no auto-approve for safety
        return False

    def _extract_custom_properties(self, fluid: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract product-level custom properties.

        Preserves FLUID-specific metadata.
        """
        custom_props = []

        metadata = fluid.get("metadata", {})

        # Product type (if specified)
        product_type = (
            metadata.get("product_type")
            or metadata.get("type")
            or fluid.get("product_type")
            or fluid.get("type")
        )
        if product_type:
            custom_props.append({"property": "type", "value": product_type})

        # Domain
        domain = metadata.get("domain") or fluid.get("domain")
        if domain:
            custom_props.append({"property": "domain", "value": domain})

        # FLUID version
        fluid_version = metadata.get("version") or fluid.get("fluidVersion")
        if fluid_version:
            custom_props.append({"property": "fluidVersion", "value": fluid_version})

        return custom_props

    def _write_output(self, data: Dict[str, Any], path: Union[Path, str], fmt: str) -> None:
        """Write ODPS data to file."""
        output_path = Path(path) if not isinstance(path, Path) else path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as f:
            if fmt == "yaml":
                import yaml

                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            else:  # json
                json.dump(data, f, indent=2)
