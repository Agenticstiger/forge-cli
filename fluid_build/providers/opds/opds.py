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

# fluid_build/providers/opds/opds.py
"""
OPDS v4.1 (Open Data Product Specification) Provider — Linux Foundation / ODPI.

**OPDS** is fluid's name for the LF/ODPI **Open Data Product Specification**
v4.1. Upstream abbreviates the spec *ODPS*; fluid uses **OPDS** so it stays
unambiguous against Bitol's Open Data Product *Standard* (which also abbreviates
to ODPS). It is hosted by the Open Data Product **Initiative** (ODPI), which is
the *organisation*, not the spec. Identifiers / URLs that retain the upstream
``ODPS`` / ``ODPI`` spelling (class name, ``.name``, ``ODPS_*`` env vars) are
kept as-is; they name the upstream spec, not Bitol's.

**Distinct from Bitol ODPS-Bitol v1.0.0.** Despite both abbreviating to "ODPS",
the Bitol Open Data Product Standard (v1.0.0, ``providers/odps_standard/``,
class :class:`BitolOdpsProvider`) is a separate specification with its own
schema, JSON format, and ``contractId``-based linkage to ODCS. This module
implements the LF/ODPI OPDS v4.1 spec — single-JSON-document export, no
import support.

The three data-product standards this repo supports, kept unambiguous:
  - **OPDS** — LF/ODPI Open Data Product Specification v4.1 (this module)
  - **ODPS-Bitol** — Bitol Open Data Product Standard v1.0.0 (``odps_standard/``)
  - **ODCS** — Bitol Open Data Contract Standard v3.1.0 (``odcs/``)

Selected via ``fluid odps export --spec odps-4.1`` (legacy ``--spec odpi-4.1``
remains accepted with a deprecation warning); the Bitol ODPS v1.0.0 path is
the default (``--spec bitol-1.0.0``).

Official Specification: https://github.com/Open-Data-Product-Initiative/v4.1
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fluid_build.providers.base import ApplyResult, BaseProvider, ProviderError
from fluid_build.util.contract import (
    consumes_to_canonical_ports,
    get_expose_binding,
    get_expose_contract,
    get_expose_id,
    get_expose_kind,
    get_expose_location,
)

# Import optional validator module
try:
    from .validator import validate_opds_structure

    VALIDATOR_AVAILABLE = True
except ImportError:
    VALIDATOR_AVAILABLE = False


def _now_iso() -> str:
    """Generate ISO 8601 timestamp in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_date() -> str:
    """Generate date string (YYYY-MM-DD) in UTC for ODPS v4.1 compliance."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _to_date(value: str) -> str:
    """Truncate an ISO datetime or date string to date-only (YYYY-MM-DD)."""
    if value and len(value) >= 10:
        return value[:10]
    return value or _now_date()


def _safe_get(obj: Any, *keys: str, default: Any = None) -> Any:
    """Safely navigate nested dictionary keys."""
    current = obj
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


# LF/ODPI ODPS v4.1 enum vocabularies. The schema rejects values outside
# these closed sets; the helpers below map common FLUID values to the
# closest LF enum equivalent so emitted docs validate against the vendored
# odps-schema-v4.1.json. Unknown values fall back to ``_DEFAULT_*`` rather
# than failing emission — the validator will surface any mismatch.
_LF_STATUS_ENUM = (
    "announcement",
    "draft",
    "development",
    "testing",
    "acceptance",
    "production",
    "sunset",
    "retired",
)
_LF_FORMAT_ENUM = ("TOON", "JSON", "XML", "CSV", "Excel", "zip", "plain text", "GraphQL", "MCP")

# FLUID status vocabulary → LF status enum. Anything unmapped lands on
# ``_LF_DEFAULT_STATUS`` (``production`` — the LF v4.1 default for shipped
# products) rather than carrying an out-of-enum value through to the
# emitted doc.
_LF_STATUS_MAP = {
    "draft": "draft",
    "active": "production",
    "live": "production",
    "production": "production",
    "released": "production",
    "shipped": "production",
    "announcement": "announcement",
    "announced": "announcement",
    "planned": "announcement",
    "dev": "development",
    "development": "development",
    "developing": "development",
    "wip": "development",
    "test": "testing",
    "testing": "testing",
    "qa": "testing",
    "uat": "acceptance",
    "stage": "acceptance",
    "staged": "acceptance",
    "staging": "acceptance",
    "acceptance": "acceptance",
    "preprod": "acceptance",
    "deprecated": "sunset",
    "deprecating": "sunset",
    "sunset": "sunset",
    "retiring": "sunset",
    "archived": "retired",
    "retired": "retired",
    "dead": "retired",
}
_LF_DEFAULT_STATUS = "production"

# FLUID binding.format (and platform-derived formats) → LF format enum.
# SQL is mapped to JSON since LF's format enum is about *data representation*
# at the data-access boundary; SQL query results are most commonly conveyed
# as JSON-shaped tuples and the LF enum has no `SQL` entry.
_LF_FORMAT_MAP = {
    "json": "JSON",
    "xml": "XML",
    "csv": "CSV",
    "tsv": "CSV",
    "excel": "Excel",
    "xlsx": "Excel",
    "xls": "Excel",
    "zip": "zip",
    "text": "plain text",
    "plain": "plain text",
    "txt": "plain text",
    "graphql": "GraphQL",
    "mcp": "MCP",
    "toon": "TOON",
    # SQL / Parquet / Avro / ORC have no native LF equivalent; map to JSON
    # as the closest interchange representation.
    "sql": "JSON",
    "parquet": "JSON",
    "avro": "JSON",
    "orc": "JSON",
}
_LF_DEFAULT_FORMAT = "JSON"


def _map_lf_status(value: Optional[str]) -> str:
    """Map a free-form FLUID status to the LF v4.1 status enum."""
    if not value:
        return _LF_DEFAULT_STATUS
    return _LF_STATUS_MAP.get(str(value).strip().lower(), _LF_DEFAULT_STATUS)


def _map_lf_format(value: Optional[str]) -> str:
    """Map a free-form FLUID/binding format to the LF v4.1 format enum."""
    if not value:
        return _LF_DEFAULT_FORMAT
    key = str(value).strip().lower()
    # Honour values already inside the enum (case-insensitive).
    for canonical in _LF_FORMAT_ENUM:
        if canonical.lower() == key:
            return canonical
    return _LF_FORMAT_MAP.get(key, _LF_DEFAULT_FORMAT)


def _env_legacy_opds(canonical_var: str, legacy_var: str, default: str) -> str:
    """Read an env var that has both a canonical ``ODPS_*`` and a legacy
    ``OPDS_*`` form. Prefer the canonical; warn once if only the legacy
    form is set."""
    val = os.getenv(canonical_var)
    if val is not None:
        return val
    val = os.getenv(legacy_var)
    if val is not None:
        logging.getLogger(__name__).warning(
            "%s is deprecated; rename to %s "
            "(OPDS is a letter-swap of ODPS — the canonical spec acronym)",
            legacy_var,
            canonical_var,
        )
        return val
    return default


class OdpsProvider(BaseProvider):
    """
    ODPS v4.1 (Open Data Product Specification) exporter — LF / ODPI.

    This provider converts FLUID contracts into ODPS v4.1 (LF/ODPI) JSON
    format, enabling integration with data catalogs, governance platforms,
    and ecosystem tools that support the ODPS standard.

    NOTE: Despite the class name, ``self.name`` returns ``"opds"`` for
    historical reasons — the provider registry (entry-point ``odps`` in
    pyproject.toml) accepts both spellings via the heuristic at
    ``bootstrap.py``. Renaming the ``.name`` property would shift catalog
    metadata keys, so the legacy string is preserved.

    Features:
    - Full ODPS v4.1 compliance
    - Rich metadata extraction from FLUID contracts
    - Support for batch processing of multiple contracts
    - Comprehensive governance and lineage information
    - SLA and quality metrics extraction
    - Extensible metadata preservation under x-fluid namespace
    """

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Configuration from environment. Each setting accepts both the
        # canonical ``ODPS_*`` form and the legacy ``OPDS_*`` form (with
        # a one-time deprecation warning) so existing operator scripts
        # keep working through the rename.
        self.include_build_info = (
            _env_legacy_opds("ODPS_INCLUDE_BUILD_INFO", "OPDS_INCLUDE_BUILD_INFO", "true").lower()
            == "true"
        )
        self.include_execution_details = (
            _env_legacy_opds(
                "ODPS_INCLUDE_EXECUTION_DETAILS", "OPDS_INCLUDE_EXECUTION_DETAILS", "false"
            ).lower()
            == "true"
        )
        self.target_platform = _env_legacy_opds(
            "ODPS_TARGET_PLATFORM", "OPDS_TARGET_PLATFORM", "generic"
        )
        self.validate_output = (
            _env_legacy_opds("ODPS_VALIDATE_OUTPUT", "OPDS_VALIDATE_OUTPUT", "true").lower()
            == "true"
        )

        # ODPS version support (can be overridden by CLI). The attribute is
        # named ``opds_version`` for back-compat with external callers /
        # tests / providers that pin it; ``odps_version`` is exposed as an
        # alias property below.
        self.opds_version = _env_legacy_opds("ODPS_VERSION", "OPDS_VERSION", "4.1")
        self.opds_spec_url = "https://github.com/Open-Data-Product-Initiative/v4.1"
        self.opds_schema_url = (
            "https://github.com/Open-Data-Product-Initiative/v4.1/blob/main/source/schema/odps.json"
        )

    @property
    def odps_version(self) -> str:
        """Canonical accessor for the ODPS spec version (aliases ``opds_version``)."""
        return self.opds_version

    @odps_version.setter
    def odps_version(self, value: str) -> None:
        self.opds_version = value

    @property
    def name(self) -> str:
        """Canonical provider name.

        Returns ``"odps"`` — the canonical spec acronym. The pre-2026-05
        value was the letter-swap ``"opds"``; downstream consumers that
        keyed on ``provider.name == "opds"`` should switch to ``"odps"``.
        The registry registers both spellings (see
        ``providers/opds/__init__.py``) so ``--provider opds`` and
        ``--provider odps`` both resolve to this class.
        """
        return "odps"

    def capabilities(self) -> Mapping[str, bool]:
        """Signal exporter capabilities with enhanced feature set."""
        caps = super().capabilities()
        caps = dict(caps)
        caps.update(
            {
                "planning": True,  # Minimal planning for compatibility
                "apply": True,  # Apply returns summary but prefers render()
                "render": True,  # Primary path for export functionality
                "auth": False,  # No authentication required for export
                "graph": True,  # Can generate dependency graphs
                "validation": True,  # Can validate OPDS output
                "batch": True,  # Supports batch processing
            }
        )
        return caps

    # ---------------- Planning / Apply ----------------

    def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Generate minimal export plan.

        Args:
            contract: FLUID contract to export

        Returns:
            List containing single export action
        """
        return [
            {
                "op": "export",
                "format": "opds",
                "source": "contract",
                "target": self.target_platform,
                "id": contract.get("id", "unknown"),
                "validation": self.validate_output,
            }
        ]

    def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
        """
        Process export actions.

        Note: CLI should prefer render() for exporters. This method
        provides compatibility but doesn't know the output path.

        Args:
            actions: Iterable of export actions

        Returns:
            ApplyResult with summary of processed actions
        """
        results: List[Dict[str, Any]] = []
        count = 0

        for i, action in enumerate(actions):
            try:
                result = {
                    "index": i,
                    "status": "acknowledged",
                    "operation": action.get("op", "export"),
                    "format": action.get("format", "opds"),
                    "target": action.get("target", self.target_platform),
                    "message": "Export action acknowledged. Use render() for actual export.",
                }

                # Validate action structure
                if action.get("op") != "export":
                    result["warning"] = f"Unexpected operation: {action.get('op')}"

                results.append(result)
                count += 1

            except Exception as e:
                results.append(
                    {
                        "index": i,
                        "status": "error",
                        "error": str(e),
                        "operation": action.get("op", "unknown"),
                    }
                )

        return ApplyResult(
            provider=self.name,
            applied=count,
            failed=len(results) - count,
            duration_sec=0.0,  # Minimal operation
            timestamp=_now_iso(),
            results=results,
        )

    # ---------------- Render (Primary Export Method) ----------------

    def render(
        self,
        src: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
        *,
        out: Optional[Union[Path, str]] = None,
        fmt: Optional[str] = "opds",
    ) -> Dict[str, Any]:
        """
        Convert FLUID contract(s) to OPDS JSON format.

        Args:
            src: Single contract or sequence of contracts to export
            out: Output path ('-' for stdout, None for return only)
            fmt: Export format (only 'opds' supported)

        Returns:
            OPDS-compliant JSON structure or file operation result

        Raises:
            ProviderError: If format unsupported or validation fails
        """
        # Validate input parameters
        if fmt not in (None, "opds"):
            raise ProviderError(f"Unsupported export format: {fmt!r}. Only 'opds' is supported.")

        self.logger.debug(
            "Starting OPDS export",
            extra={
                "format": fmt,
                "target_platform": self.target_platform,
                "include_build_info": self.include_build_info,
                "output_path": str(out) if out else "return",
            },
        )

        # Normalize input to list of contracts
        contracts: List[Mapping[str, Any]]
        if isinstance(src, Mapping):
            contracts = [src]
        else:
            contracts = list(src)

        # Process each contract
        opds_artifacts: List[Dict[str, Any]] = []
        processing_errors: List[Dict[str, Any]] = []

        for i, contract in enumerate(contracts):
            try:
                opds_artifact = self._contract_to_opds(contract)

                # Validate if enabled
                if self.validate_output:
                    validation_result = self._validate_opds_artifact(opds_artifact)
                    if not validation_result["valid"]:
                        self.logger.warning(
                            "OPDS validation failed",
                            extra={
                                "contract_id": contract.get("id", f"contract_{i}"),
                                "errors": validation_result["errors"],
                            },
                        )

                opds_artifacts.append(opds_artifact)

            except ProviderError:
                raise  # Don't swallow validation/provider errors
            except Exception as e:
                error_info = {
                    "contract_index": i,
                    "contract_id": contract.get("id", f"contract_{i}"),
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
                processing_errors.append(error_info)
                self.logger.error("Failed to process contract", extra=error_info)

        # Fail loudly when NOTHING could be mapped. The pre-2026-06 behaviour
        # collected the swallowed exceptions, built a payload whose
        # ``artifacts`` was an empty list, and let the CLI write the literal
        # string ``[]`` to disk while exiting 0 — a silent corruption that
        # looked like success. A render that produced zero artifacts is a hard
        # failure: re-raise the real reason(s) so the caller gets a non-zero
        # exit and an actionable message instead of garbage output.
        if not opds_artifacts:
            detail = "; ".join(
                f"{e['contract_id']}: {e['error_type']}: {e['error']}" for e in processing_errors
            )
            raise ProviderError(
                "ODPS v4.1 export produced no document — "
                f"all {len(contracts)} contract(s) failed to map. {detail}".strip()
            )

        # Build final payload
        payload = self._build_opds_payload(opds_artifacts, processing_errors)

        # Handle output
        if out and out != "-":
            return self._write_opds_file(payload, Path(out))
        else:
            return payload

    # ---------------- FLUID → OPDS Conversion ----------------

    def _contract_to_opds(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        """Convert a single FLUID contract to OPDS format.

        Generates OPDS v4.1-compliant structure with nested
        ``product.details`` AND mirrors the same fields at the top
        level — the dual shape is part of the OPDS v4.1 spec for
        round-trip compatibility with v4.0 readers, not a legacy
        back-compat artefact of fluid.

        Args:
            contract: FLUID contract dictionary

        Returns:
            OPDS-compliant artifact dictionary
        """
        contract_id = contract.get("id")
        if not contract_id:
            raise ProviderError("Contract missing required 'id' field")

        # Extract metadata helper
        metadata = contract.get("metadata", {})

        # Determine language code (default to 'en')
        lang_code = metadata.get("language", "en")
        if len(lang_code) != 2:
            lang_code = "en"

        # Build OPDS v4.1 compliant structure
        opds_artifact = {
            # OPDS v4.1 required fields
            "schema": self.opds_schema_url,  # 'schema' not '$schema' per v4.1
            "version": self.opds_version,  # OPDS version
            # Product section with language-specific details (OPDS v4.1 structure)
            "product": {
                "details": {
                    lang_code: {
                        "name": contract.get("name", contract_id),
                        "productID": contract_id,
                        "visibility": metadata.get("visibility", "private"),
                        # Map FLUID status to the LF v4.1 enum so the
                        # emitted doc validates against odps-schema-v4.1.json.
                        "status": _map_lf_status(metadata.get("status", "draft")),
                        "type": self._map_fluid_kind_to_opds_type(
                            contract.get("kind", "DataProduct")
                        ),
                        "created": _to_date(_safe_get(metadata, "created_at", default=_now_date())),
                        "updated": _to_date(_safe_get(metadata, "updated_at", default=_now_date())),
                        "description": contract.get("description", ""),
                        "valueProposition": metadata.get("value_proposition", ""),
                        "productVersion": self._extract_version_info(contract),
                        "categories": metadata.get("categories", []),
                        "tags": metadata.get("tags", []),
                    }
                },
                # Data Access methods
                "dataAccess": self._extract_data_access_methods(contract.get("exposes", [])),
            },
        }

        # Add optional OPDS sections
        sla_info = self._extract_sla_info(contract)
        if sla_info:
            opds_artifact["product"]["SLA"] = sla_info

        dq_info = self._extract_data_quality_info(contract)
        if dq_info:
            opds_artifact["product"]["dataQuality"] = dq_info

        # Add DataHolder information if available
        owner_info = metadata.get("owner", {})
        if owner_info:
            opds_artifact["product"]["dataHolder"] = self._extract_data_holder_info(owner_info)

        # Legacy flat fields and FLUID extensions live under product
        # to avoid root-level additionalProperties violation (OPDS v4.1
        # only allows schema, version, product at root).
        opds_artifact["product"]["_legacy"] = {
            "dataProductId": contract_id,
            "dataProductName": contract.get("name", contract_id),
            "dataProductDescription": contract.get("description", ""),
            "dataProductOwner": self._extract_owner_info(metadata.get("owner", {})),
            "dataProductType": contract.get("kind", "DataProduct"),
            "domain": contract.get("domain"),
            "tags": metadata.get("tags", []),
            "layer": metadata.get("layer"),
            "status": metadata.get("status", "Draft"),
            "outputPorts": self._extract_output_ports(contract.get("exposes", [])),
            "inputPorts": self._extract_input_ports(contract),
        }

        # Preserve FLUID-specific metadata under product namespace
        opds_artifact["product"]["x-fluid"] = self._extract_fluid_extensions(contract)

        # Remove None values for cleaner output
        return {k: v for k, v in opds_artifact.items() if v is not None}

    @staticmethod
    def _coerce_owner(owner: Any) -> Dict[str, Any]:
        """Normalise a contract ``metadata.owner`` to a dict.

        FLUID contracts in the wild declare ``owner`` either as a structured
        mapping (``{team, email, ...}``) or as a bare string (the team / person
        name). The extractors below index it with ``.get()``; a bare-string
        owner therefore used to raise ``AttributeError`` deep inside
        ``_contract_to_opds`` — which the render loop swallowed into an empty
        ``[]`` document. Coercing here keeps both shapes working.
        """
        if isinstance(owner, Mapping):
            return dict(owner)
        if isinstance(owner, str) and owner.strip():
            return {"team": owner.strip()}
        return {}

    def _extract_owner_info(self, owner: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract and format owner information."""
        owner = self._coerce_owner(owner)
        return {
            "name": owner.get("team", owner.get("name")),
            "email": owner.get("email"),
            "contact": owner.get("contact"),
            "organization": owner.get("organization", owner.get("org")),
        }

    def _extract_output_ports(self, exposes: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Extract output port information from exposes section."""
        ports = []

        for expose in exposes:
            if not isinstance(expose, dict):
                continue
            # Use canonical 0.7.x field accessors
            expose_id = get_expose_id(expose)
            expose_kind = get_expose_kind(expose)
            binding = get_expose_binding(expose)
            location = get_expose_location(expose)

            # Get schema from contract section or direct schema field
            contract_section = get_expose_contract(expose)
            if contract_section and "schema" in contract_section:
                schema = contract_section["schema"]
            else:
                schema = expose.get("schema", [])

            # Handle v0.7.x format where schema is {"fields": [...]}
            if isinstance(schema, dict) and "fields" in schema:
                schema = schema["fields"]

            port = {
                "id": expose_id,
                "name": expose.get("name", expose_id),
                "description": expose.get("description", ""),
                "type": expose_kind,
                "format": binding.get("format") if binding else None,
                "location": location if location else {},
                "schema": self._extract_schema_info(schema),
            }

            # Add quality information if available from contract.dq
            if contract_section and "dq" in contract_section:
                port["quality"] = self._extract_quality_info(contract_section["dq"])
            elif "quality" in expose:
                port["quality"] = self._extract_quality_info(expose["quality"])

            ports.append(port)

        return ports

    def _extract_input_ports(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Extract input port information from the contract's ``consumes[]``.

        Delegates to the shared :func:`consumes_to_canonical_ports` helper so
        that the 0.7.x field adapters live in exactly one place. The
        output shape here is the OPDS v4.1 legacy ``inputPort`` dict, which
        differs from the ODPS-Bitol shape emitted by ``OdpsStandardProvider``.
        """
        canonical = consumes_to_canonical_ports(contract, logger=self.logger)

        ports: List[Dict[str, Any]] = []
        for entry in canonical:
            port: Dict[str, Any] = {
                "id": entry["id"],
                "name": entry["name"] or entry["id"],
                "description": entry["description"],
                "reference": entry["reference"],
                "kind": entry["kind"] or "data",
                "required": bool(entry["required"]) if entry["required"] is not None else True,
            }
            if entry["constraints"] is not None:
                port["constraints"] = entry["constraints"]
            ports.append(port)

        return ports

    def _extract_schema_info(self, schema: List[Mapping[str, Any]]) -> Dict[str, Any]:
        """Extract and format schema information."""
        if not schema:
            return {"fields": []}

        fields = []
        for field in schema:
            # Handle 0.7.x (required field)
            if "required" in field:
                nullable = not field["required"]
            elif "nullable" in field:
                nullable = field["nullable"]
            else:
                nullable = True

            field_info = {
                "name": field.get("name"),
                "type": field.get("type"),
                "nullable": nullable,
                "description": field.get("description", ""),
            }

            # Add constraints if available
            if "constraints" in field:
                field_info["constraints"] = field["constraints"]

            # Add format information if available
            if "format" in field:
                field_info["format"] = field["format"]

            fields.append(field_info)

        return {
            "fields": fields,
            "format": "json-schema",  # Default format
        }

    def _extract_governance_info(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract governance and compliance information."""
        governance = {}

        # Access policies
        if "accessPolicy" in contract:
            governance["accessPolicy"] = self._extract_access_policy(contract["accessPolicy"])

        # Data classification
        metadata = contract.get("metadata", {})
        if "classification" in metadata:
            governance["classification"] = metadata["classification"]

        # Compliance information
        if "compliance" in metadata:
            governance["compliance"] = metadata["compliance"]

        # Privacy information
        if "privacy" in metadata:
            governance["privacy"] = metadata["privacy"]

        # Retention policies
        if "retention" in metadata:
            governance["retention"] = metadata["retention"]

        return governance

    def _extract_access_policy(self, access_policy: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract and format access policy information."""
        policy = {}

        if "grants" in access_policy:
            grants = []
            for grant in access_policy["grants"]:
                grant_info = {
                    "principal": grant.get("principal"),
                    "permissions": grant.get("permissions", []),
                    "conditions": grant.get("conditions", {}),
                }
                grants.append(grant_info)
            policy["grants"] = grants

        if "restrictions" in access_policy:
            policy["restrictions"] = access_policy["restrictions"]

        return policy

    def _extract_sla_info(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract SLA and quality information."""
        sla = {}

        # Check v0.7.x qos at expose level (most specific)
        exposes = contract.get("exposes", [])
        if exposes and len(exposes) > 0:
            first_expose = exposes[0]
            if "qos" in first_expose:
                qos = first_expose["qos"]
                if "availability" in qos:
                    sla["availability"] = {
                        "target": qos["availability"],
                        "measurementWindow": "monthly",
                    }
                if "freshnessSLO" in qos:
                    sla["freshness"] = {"slo": qos["freshnessSLO"], "format": "ISO8601"}

        # Extract from builds array (v0.7.x) or build object (legacy, dropped)
        builds = contract.get("builds", [])
        if builds and len(builds) > 0:
            build = builds[0]
        else:
            build = contract.get("build", {})

        if build:
            execution = build.get("execution", {})
            if "trigger" in execution:
                trigger = execution["trigger"]
                if trigger.get("type") == "schedule":
                    sla["freshness"] = sla.get("freshness", {})
                    sla["freshness"]["schedule"] = trigger.get("cron")
                    sla["freshness"]["maxAgeHours"] = self._cron_to_max_age_hours(
                        trigger.get("cron")
                    )

            if "sla" in execution:
                sla.update(execution["sla"])

        # Extract from metadata
        metadata = contract.get("metadata", {})
        if "sla" in metadata:
            sla.update(metadata["sla"])

        # Default availability if not specified
        if "availability" not in sla:
            sla["availability"] = {"target": "99.9%", "measurementWindow": "monthly"}

        return sla

    def _extract_lineage_info(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract data lineage information."""
        lineage = {
            "upstream": [],
            "transformation": None,
        }

        # Extract upstream dependencies - v0.7.x productId only
        consumes = contract.get("consumes", [])
        for consume in consumes:
            ref = consume.get("productId") or consume.get("ref")
            if ref:
                lineage["upstream"].append(ref)

        # Extract transformation information from builds array (v0.7.x) or build object (legacy, dropped)
        builds = contract.get("builds", [])
        if builds and len(builds) > 0:
            build = builds[0]
        else:
            build = contract.get("build", {})

        if build:
            transformation = build.get("transformation", {})
            lineage["transformation"] = {
                "pattern": transformation.get("pattern"),
                "engine": transformation.get("engine"),
                "language": transformation.get("language", "sql"),
            }

        return lineage

    def _extract_version_info(self, contract: Mapping[str, Any]) -> str:
        """Extract version information from contract."""
        # Try various version fields
        version = (
            _safe_get(contract, "metadata", "version")
            or _safe_get(contract, "version")
            or self._extract_version_from_id(contract.get("id", ""))
        )
        return version or "1.0.0"

    def _extract_version_from_id(self, contract_id: str) -> Optional[str]:
        """Extract version from contract ID if it follows versioning patterns."""
        import re

        # Look for patterns like _v1, _v2.1, etc.
        match = re.search(r"_v(\d+(?:\.\d+)*)", contract_id)
        return match.group(1) if match else None

    def _extract_build_info(self, build: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract build configuration information."""
        build_info = {}

        if "transformation" in build:
            build_info["transformation"] = build["transformation"]

        if "validation" in build:
            build_info["validation"] = build["validation"]

        if "testing" in build:
            build_info["testing"] = build["testing"]

        return build_info

    def _extract_execution_info(self, execution: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract execution configuration information."""
        execution_info = {}

        if "trigger" in execution:
            execution_info["trigger"] = execution["trigger"]

        if "runtime" in execution:
            execution_info["runtime"] = execution["runtime"]

        if "retries" in execution:
            execution_info["retries"] = execution["retries"]

        return execution_info

    def _extract_quality_info(self, quality: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract data quality information."""
        return {
            "rules": quality.get("rules", []),
            "metrics": quality.get("metrics", {}),
            "monitoring": quality.get("monitoring", {}),
        }

    def _extract_fluid_extensions(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract FLUID-specific extensions for preservation."""
        extensions = {
            "fluidVersion": contract.get("fluidVersion"),
            "originalId": contract.get("id"),
        }

        # Preserve builds[] section (v0.7.x canonical)
        if "builds" in contract:
            extensions["builds"] = contract["builds"]
        elif "build" in contract:
            extensions["build"] = contract["build"]

        # Preserve validation rules
        if "validation" in contract:
            extensions["validation"] = contract["validation"]

        # Preserve any custom fields not covered by OPDS
        custom_fields = {}
        opds_standard_fields = {
            "id",
            "name",
            "description",
            "domain",
            "kind",
            "metadata",
            "consumes",
            "exposes",
            "build",
            "builds",
            "accessPolicy",
            "validation",
        }

        for key, value in contract.items():
            if key not in opds_standard_fields:
                custom_fields[key] = value

        if custom_fields:
            extensions["customFields"] = custom_fields

        return {k: v for k, v in extensions.items() if v is not None}

    def _map_fluid_kind_to_opds_type(self, kind: str) -> str:
        """
        Map FLUID contract kind to OPDS product type.

        OPDS v4.1 types: raw data, derived data, dataset, reports, analytic view,
        3D visualisation, algorithm, decision support, automated decision-making,
        data-enhanced product, data-driven service, data-enabled performance, bi-directional
        """
        kind_lower = kind.lower()

        type_mapping = {
            "dataproduct": "dataset",
            "dataset": "dataset",
            "table": "dataset",
            "view": "analytic view",
            "analytics": "analytic view",
            "dashboard": "reports",
            "report": "reports",
            "ml": "algorithm",
            "model": "algorithm",
            "pipeline": "derived data",
            "etl": "derived data",
            "raw": "raw data",
            "api": "data-driven service",
            "service": "data-driven service",
        }

        return type_mapping.get(kind_lower, "dataset")

    def _extract_data_access_methods(
        self, exposes: List[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extract data access methods from exposes for OPDS v4.1."""
        access_methods = []

        for expose in exposes:
            if not isinstance(expose, dict):
                continue
            binding = get_expose_binding(expose)
            location = get_expose_location(expose)

            if not binding:
                continue

            platform = binding.get("platform", "").lower()

            # Determine output port type based on platform/binding
            output_type = "API"
            data_format = "JSON"

            if platform in ("bigquery", "snowflake", "redshift", "postgres"):
                output_type = "SQL"
                # LF v4.1 format enum has no `SQL` entry; SQL query results
                # are conveyed as JSON-shaped tuples by every spec consumer
                # that supports `outputPortType: SQL` (per the LF examples).
                data_format = "JSON"
            elif platform == "gcs":
                output_type = "file"
                data_format = _safe_get(expose, "format", "CSV")
            elif platform in ("rest", "http"):
                output_type = "API"
                data_format = "JSON"

            access_method = {
                "name": {"en": get_expose_id(expose) or "data_access"},
                "description": {"en": expose.get("description", "Data access endpoint")},
                "outputPortType": output_type,
                # Map binding/derived format to the LF v4.1 enum.
                "format": _map_lf_format(data_format),
            }

            # Add location-based access URL if available
            if location:
                if isinstance(location, dict):
                    # Construct access URL from location components
                    if platform == "bigquery" and all(
                        k in location for k in ["project", "dataset", "table"]
                    ):
                        access_method["accessURL"] = (
                            f"bigquery://{location['project']}.{location['dataset']}.{location['table']}"
                        )
                    elif "url" in location:
                        access_method["accessURL"] = location["url"]
                    elif "path" in location:
                        access_method["accessURL"] = location["path"]
                elif isinstance(location, str):
                    access_method["accessURL"] = location

            access_methods.append(access_method)

        return (
            access_methods
            if access_methods
            else [{"name": {"en": "default"}, "outputPortType": "API", "format": "JSON"}]
        )

    def _extract_data_holder_info(self, owner: Mapping[str, Any]) -> Dict[str, Any]:
        """Extract DataHolder information for OPDS v4.1."""
        owner = self._coerce_owner(owner)
        return {
            "legalName": owner.get("organization", owner.get("org", owner.get("team", ""))),
            "contactName": owner.get("name", ""),
            "email": owner.get("email", ""),
            "description": owner.get("description", ""),
        }

    def _extract_data_quality_info(self, contract: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract data quality information for OPDS v4.1."""
        # Look for quality info in exposes
        exposes = contract.get("exposes", [])
        if not exposes:
            return None

        dq_dimensions = []

        for expose in exposes:
            if not isinstance(expose, dict):
                continue
            # Check for quality rules in contract section
            contract_section = get_expose_contract(expose)
            if contract_section:
                dq_rules = _safe_get(contract_section, "dq", "rules", default=[])
                if not dq_rules:
                    dq_rules = _safe_get(contract_section, "quality", "rules", default=[])

                for rule in dq_rules:
                    dimension_type = rule.get("type", "validity").lower()
                    dq_dimensions.append(
                        {
                            "dimension": dimension_type,
                            "objective": (
                                int(rule.get("threshold", 100) * 100)
                                if isinstance(rule.get("threshold"), float)
                                else 100
                            ),
                            "unit": "percentage",
                            "description": rule.get("description", f"{dimension_type} check"),
                        }
                    )

        if dq_dimensions:
            return {"declarative": dq_dimensions}

        return None

    # ---------------- Utility Methods ----------------

    def _cron_to_max_age_hours(self, cron_expr: Optional[str]) -> int:
        """Convert cron expression to maximum age hours estimate."""
        if not cron_expr:
            return 24

        # Simple heuristic: daily schedule = 24 hours, hourly = 1 hour, etc.
        parts = cron_expr.split()
        if len(parts) >= 5:
            minute, hour, day, month, weekday = parts[:5]

            # Daily at specific time
            if day == "*" and month == "*" and weekday == "*":
                return 24
            # Weekly
            elif weekday != "*":
                return 168  # 7 days
            # Monthly
            elif day != "*":
                return 744  # ~31 days

        return 24  # Default to daily

    def _validate_opds_artifact(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate OPDS artifact against requirements.

        Uses full JSON schema validation if available, falls back to basic validation.
        """
        if VALIDATOR_AVAILABLE and self.validate_output:
            try:
                # Use comprehensive validator with full JSON schema
                result = validate_opds_structure(
                    artifact,
                    version=self.opds_version,
                    use_full_schema=True,
                )
                return result
            except Exception as e:
                self.logger.warning("full_validation_failed_using_basic", extra={"error": str(e)})
                # Fall through to basic validation

        # Basic validation (fallback or when validator not available)
        errors = []
        warnings = []

        # Required fields
        required_fields = ["dataProductId", "dataProductName", "dataProductOwner"]
        for field in required_fields:
            if not artifact.get(field):
                errors.append(f"Missing required field: {field}")

        # Validate owner structure
        owner = artifact.get("dataProductOwner", {})
        if owner and not (owner.get("name") or owner.get("email")):
            warnings.append("Owner should have either name or email")

        # Validate output ports
        output_ports = artifact.get("outputPorts", [])
        for i, port in enumerate(output_ports):
            if not port.get("id"):
                errors.append(f"Output port {i} missing id")

        # Check for schema reference
        if "$schema" not in artifact and "schema" not in artifact:
            warnings.append("Schema reference ($schema) recommended for validation")

        return {
            "valid": len(errors) == 0,
            "errors": errors if errors else None,
            "warnings": warnings if warnings else None,
            "validation_type": "basic",
        }

    def _build_opds_payload(
        self, artifacts: List[Dict[str, Any]], errors: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Build final OPDS payload with metadata."""
        payload = {
            "opds_version": "1.0",
            # ``generator`` identifies the emitter on the wire. Canonical
            # spelling is ``fluid-forge-odps-provider`` (ODPS, matching the
            # spec acronym). Pre-2026-05 emitted the letter-swap
            # ``fluid-forge-opds-provider``; downstream consumers that key
            # on this string should accept both spellings.
            "generator": "fluid-forge-odps-provider",
            "generated_at": _now_iso(),
            "target_platform": self.target_platform,
            "count": len(artifacts),
        }

        # Add artifacts
        if len(artifacts) == 1:
            payload["artifacts"] = artifacts[0]
        else:
            payload["artifacts"] = artifacts

        # Add processing errors if any
        if errors:
            payload["processing_errors"] = errors
            payload["status"] = "partial_success"
        else:
            payload["status"] = "success"

        # Add configuration metadata
        payload["export_config"] = {
            "include_build_info": self.include_build_info,
            "include_execution_details": self.include_execution_details,
            "validation_enabled": self.validate_output,
        }

        return payload

    def _write_opds_file(self, payload: Dict[str, Any], output_path: Path) -> Dict[str, Any]:
        """Write OPDS payload to file."""
        try:
            # Ensure output directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # Write JSON with pretty formatting
            with output_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            file_size = output_path.stat().st_size

            self.logger.debug(
                "OPDS export completed",
                extra={
                    "output_path": str(output_path),
                    "file_size_bytes": file_size,
                    "artifact_count": payload.get("count", 0),
                    "status": payload.get("status", "unknown"),
                },
            )

            return {
                "status": "success",
                "path": str(output_path),
                "bytes": file_size,
                "artifacts_exported": payload.get("count", 0),
                "export_timestamp": payload.get("generated_at"),
            }

        except Exception as e:
            error_msg = f"Failed to write OPDS file to {output_path}: {e}"
            self.logger.error(error_msg)
            raise ProviderError(error_msg) from e
