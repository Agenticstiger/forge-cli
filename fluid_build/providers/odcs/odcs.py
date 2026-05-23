# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Back-compat shim.

The ODCS provider was split into :mod:`.provider`, :mod:`.mappers`,
:mod:`.validation`, and :mod:`.io` in the modular refactor. This module
re-exports :class:`OdcsProvider` plus a few thin delegating methods that
older tests still call as private members of the class (``_map_status_to_odcs``,
``_extract_team``, ``_extract_field_quality``, ``_fluid_field_to_odcs_property``,
``_map_type_to_logical``, ``_map_type_to_physical``).

New code should import from :mod:`fluid_build.providers.odcs` directly:

    from fluid_build.providers.odcs import OdcsProvider
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from .mappers import quality as _quality_mapper
from .mappers import schema as _schema_mapper
from .mappers import servers as _servers_mapper
from .mappers import sla as _sla_mapper
from .mappers import team as _team_mapper
from .mappers.base import ExportCtx, get_field_passthrough
from .mappers.types import (
    _FLUID_TYPE_TO_ODCS_LOGICAL,
    fluid_to_logical,
    fluid_to_odcs_status,
    fluid_to_physical,
    logical_to_fluid,
    odcs_to_fluid_status,
    provider_to_server_type,
    server_type_to_provider,
)
from .provider import OdcsProvider as _OdcsProviderImpl
from .validation import validate as _validate


class OdcsProvider(_OdcsProviderImpl):
    """OdcsProvider with legacy private-method shims for back-compat tests."""

    # --- status mapping -------------------------------------------------
    def _map_status_to_odcs(self, status: str) -> str:
        return fluid_to_odcs_status(status)

    def _map_status_from_odcs(self, status: str) -> str:
        return odcs_to_fluid_status(status)

    # --- type mapping ---------------------------------------------------
    def _map_type_to_logical(self, fluid_type: str) -> str:
        return fluid_to_logical(fluid_type)

    def _map_type_to_physical(self, fluid_type: str, provider: Optional[str]) -> Optional[str]:
        return fluid_to_physical(fluid_type, provider)

    # --- team / owner ---------------------------------------------------
    def _extract_team(self, fluid: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        ctx = ExportCtx(fluid=fluid, odcs={}, logger=self.logger)
        _team_mapper.to_odcs(ctx)
        return ctx.odcs.get("team")

    def _odcs_team_to_fluid_owner(self, team: Any) -> Dict[str, Any]:
        return _team_mapper._team_to_owner(team) or {}

    # --- field quality --------------------------------------------------
    def _extract_field_quality(
        self, field: Mapping[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        if not getattr(self, "include_quality_checks", True):
            return None
        return _quality_mapper.to_odcs_property(field)

    # --- field property (export) ---------------------------------------
    def _fluid_field_to_odcs_property(
        self, field: Mapping[str, Any], expose: Mapping[str, Any]
    ) -> Dict[str, Any]:
        provider = None
        binding = expose.get("binding") if isinstance(expose, Mapping) else None
        if isinstance(binding, Mapping):
            provider = binding.get("platform") or binding.get("provider")
        if not provider and isinstance(expose, Mapping):
            provider = expose.get("provider")
        prop = _schema_mapper._field_to_property(field, provider)
        prop.pop("odcs_passthrough", None)
        if get_field_passthrough(field):
            pass  # _field_to_property already merged pass-through
        return prop

    # --- SLA ------------------------------------------------------------
    def _extract_sla_properties(
        self, fluid: Mapping[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        if not getattr(self, "include_sla", True):
            return None
        ctx = ExportCtx(
            fluid=fluid,
            odcs={},
            logger=self.logger,
            options={"include_sla": True},
        )
        _sla_mapper.to_odcs(ctx)
        return ctx.odcs.get("slaProperties")

    # --- contract-level quality ----------------------------------------
    def _extract_quality(self, fluid: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        ctx = ExportCtx(
            fluid=fluid,
            odcs={},
            logger=self.logger,
            options={"include_quality_checks": True},
        )
        _quality_mapper.to_odcs(ctx)
        return ctx.odcs.get("quality")

    # --- schema (import) -----------------------------------------------
    def _odcs_schema_to_expose(
        self, odcs: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """DEPRECATED. Legacy helper that flattens every SchemaObject in an
        ODCS contract into a single FLUID expose. Kept only for back-compat
        with one specific test in ``tests/test_odcs_mappings.py``; will be
        removed in the next major release.

        New code must call :meth:`import_contract` instead, which correctly
        emits **one FLUID expose per SchemaObject** — preserving the multi-
        port shape that the new modular mapper pipeline produces.
        """
        import warnings

        warnings.warn(
            "OdcsProvider._odcs_schema_to_expose is deprecated; use "
            "OdcsProvider.import_contract() instead. This helper collapses "
            "multi-SchemaObject contracts into a single expose and will be "
            "removed in the next major release.",
            DeprecationWarning,
            stacklevel=2,
        )
        schema = odcs.get("schema") or []
        if not isinstance(schema, list) or not schema:
            return None
        expose: Dict[str, Any] = {
            "id": odcs.get("id", "default"),
            "version": odcs.get("version", "1.0.0"),
            "description": odcs.get("description", ""),
            "schema": {"fields": []},
        }
        for schema_object in schema:
            if not isinstance(schema_object, Mapping):
                continue
            for prop in schema_object.get("properties") or [schema_object]:
                if not isinstance(prop, Mapping):
                    continue
                expose["schema"]["fields"].append(self._odcs_schema_to_field(prop))
        return expose

    def _odcs_schema_to_field(self, schema_entry: Mapping[str, Any]) -> Dict[str, Any]:
        return _schema_mapper._property_to_field(schema_entry)

    def _map_logical_type_to_fluid(self, logical_type: str) -> str:
        return logical_to_fluid(logical_type)

    # --- servers (import / export) -------------------------------------
    def _odcs_server_to_expect(
        self, server: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        return _servers_mapper._server_to_expect(server)

    def _map_provider_to_server_type(self, provider: str) -> str:
        return provider_to_server_type(provider)

    def _map_server_type_to_provider(self, server_type: str) -> str:
        return server_type_to_provider(server_type)

    def _extract_location_from_server(
        self, server: Mapping[str, Any]
    ) -> Dict[str, Any]:
        return _servers_mapper._location_from_server(server)

    # --- expose scoping (legacy name) ---------------------------------
    def _filter_to_expose(
        self, fluid: Mapping[str, Any], expose_id: str
    ) -> Dict[str, Any]:
        return self._scope_to_expose(fluid, expose_id)

    # --- validation -----------------------------------------------------
    def _validate_odcs(self, odcs: Mapping[str, Any]) -> None:
        if not getattr(self, "schema", None):
            self.logger.warning("ODCS schema not available, skipping validation")
            return
        _validate(odcs, self.schema)


__all__ = ["OdcsProvider", "_FLUID_TYPE_TO_ODCS_LOGICAL"]
