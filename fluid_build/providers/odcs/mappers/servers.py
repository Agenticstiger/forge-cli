# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""ODCS servers ↔ FLUID expects / exposes binding.

ODCS ``servers[]`` carry one entry per source/sink. FLUID models the same
information in two places: each ``expose.binding`` (where data is produced)
and each ``expect`` (where data is consumed). On export we read from both;
on import everything becomes ``expects[]`` entries that the caller can later
re-attach to exposes if desired.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)
from .types import provider_to_server_type, server_type_to_provider


# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    servers = ctx.odcs.get("servers") or []
    if not isinstance(servers, list):
        return
    # Verbatim pass-through so re-export reproduces the exact same servers list.
    metadata_passthrough(ctx.fluid)["servers"] = [
        dict(s) for s in servers if isinstance(s, Mapping)
    ]
    expects = ctx.fluid.setdefault("expects", [])
    for server in servers:
        if not isinstance(server, Mapping):
            continue
        expect = _server_to_expect(server)
        if expect:
            expects.append(expect)


def _server_to_expect(server: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    server_type = server.get("type")
    if not server_type:
        return None
    expect: Dict[str, Any] = {
        # ODCS canonical id lives at ``server`` (v3.1.0); ``name`` is accepted
        # for back-compat with legacy fixtures, then ``id``.
        "id": (
            server.get("server")
            or server.get("name")
            or server.get("id")
            or "dependency"
        ),
        "provider": server_type_to_provider(server_type),
    }
    location = _location_from_server(server)
    if location:
        expect["location"] = location
    return expect


def _location_from_server(server: Mapping[str, Any]) -> Dict[str, Any]:
    location: Dict[str, Any] = {}
    for key in (
        "project", "dataset", "table", "account", "database", "schema",
        "bucket", "path", "region", "host", "port", "format",
    ):
        if key in server:
            location[key] = server[key]
    return location


# ----- FLUID → ODCS --------------------------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    """Always emit ``servers`` (required by the strict ODCS v3.1.0 schema).

    Verbatim pass-through wins when the FLUID was produced by importing an
    ODCS contract — keeps the round-trip lossless.
    """
    pt = get_metadata_passthrough(ctx.fluid)
    raw = pt.get("servers")
    if isinstance(raw, list) and raw:
        ctx.odcs["servers"] = [dict(s) for s in raw if isinstance(s, Mapping)]
        return

    servers: List[Dict[str, Any]] = []
    for expose in ctx.fluid.get("exposes") or []:
        s = _expose_to_server(expose)
        if s:
            servers.append(s)
    for expect in ctx.fluid.get("expects") or []:
        s = _expect_to_server(expect)
        if s:
            servers.append(s)
    ctx.odcs["servers"] = servers


def _expose_to_server(expose: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(expose, Mapping):
        return None
    binding = expose.get("binding") if isinstance(expose.get("binding"), Mapping) else None
    provider = None
    location: Optional[Mapping[str, Any]] = None
    if binding:
        provider = binding.get("platform") or binding.get("provider")
        location = binding.get("location") if isinstance(binding.get("location"), Mapping) else None
    if not provider:
        provider = expose.get("provider")
    if not provider:
        return None

    expose_id = expose.get("exposeId") or expose.get("id") or "default"
    server: Dict[str, Any] = {
        "server": expose_id,
        "type": provider_to_server_type(provider),
    }
    if not isinstance(location, Mapping):
        loc = expose.get("location")
        location = loc if isinstance(loc, Mapping) else None
    if isinstance(location, Mapping):
        server.update(_server_details_from_location(location, provider))
    return server


def _expect_to_server(expect: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(expect, Mapping):
        return None
    binding = expect.get("binding") if isinstance(expect.get("binding"), Mapping) else None
    provider = None
    location: Optional[Mapping[str, Any]] = None
    if binding:
        provider = binding.get("platform") or binding.get("provider")
        location = binding.get("location") if isinstance(binding.get("location"), Mapping) else None
    if not provider:
        provider = expect.get("provider")
    if not provider:
        return None

    expect_id = expect.get("id") or "dependency"
    server: Dict[str, Any] = {
        "server": expect_id,
        "type": provider_to_server_type(provider),
    }
    if not isinstance(location, Mapping):
        loc = expect.get("location")
        location = loc if isinstance(loc, Mapping) else None
    if isinstance(location, Mapping):
        server.update(_server_details_from_location(location, provider))
    return server


def _server_details_from_location(
    location: Mapping[str, Any], provider: str
) -> Dict[str, Any]:
    """Map FLUID binding/location → ODCS server-type-specific allowed fields.

    ODCS v3.1.0 servers use ``unevaluatedProperties: false`` — emitting any
    field outside the allowed list for the server's type breaks validation.
    This table mirrors the ``$defs/ServerSource/<TypeServer>.properties``
    keys; FLUID fields with no ODCS counterpart are quietly dropped here and
    surface elsewhere via expose pass-through if needed.
    """
    p = provider.lower()
    details: Dict[str, Any] = {}

    if p in ("gcp", "bigquery"):
        for key in ("project", "dataset"):
            if key in location:
                details[key] = location[key]
    elif p == "snowflake":
        for key in ("host", "port", "account", "database", "schema", "warehouse"):
            if key in location:
                details[key] = location[key]
    elif p in ("aws", "s3"):
        # ODCS S3Server: location (URI), endpointUrl, format, delimiter
        loc_val = (
            location.get("location")
            or _build_s3_uri(location.get("bucket"), location.get("path") or location.get("key"))
        )
        if loc_val:
            details["location"] = loc_val
        for key in ("endpointUrl", "format", "delimiter"):
            if key in location:
                details[key] = location[key]
    elif p == "kafka":
        host = location.get("host") or location.get("account")
        if host:
            details["host"] = host
        if "format" in location:
            details["format"] = location["format"]
    elif p in ("postgres", "postgresql"):
        for key in ("host", "port", "database", "schema"):
            if key in location:
                details[key] = location[key]
    elif p == "mysql":
        for key in ("host", "port", "database"):
            if key in location:
                details[key] = location[key]
    elif p == "databricks":
        for key in ("host", "catalog", "schema"):
            if key in location:
                details[key] = location[key]
    elif p == "redshift":
        for key in ("host", "database", "schema", "region", "account"):
            if key in location:
                details[key] = location[key]
    elif p == "local":
        for key in ("path", "format"):
            if key in location:
                details[key] = location[key]
    elif p in ("athena",):
        for key in ("stagingDir", "schema", "catalog", "regionName"):
            if key in location:
                details[key] = location[key]
    else:
        # Custom / unknown provider → ODCS server.type=custom which accepts
        # the union of all server-type fields. Copy any that match the union.
        custom_keys = {
            "account", "catalog", "database", "dataset", "delimiter",
            "endpointUrl", "format", "host", "location", "path", "port",
            "project", "region", "regionName", "schema", "serviceName",
            "stagingDir", "warehouse", "stream",
        }
        for key, value in location.items():
            if key in custom_keys:
                details[key] = value
    return details


def _build_s3_uri(bucket: Optional[str], path: Optional[str]) -> Optional[str]:
    if not bucket:
        return None
    if path:
        return f"s3://{bucket.rstrip('/')}/{path.lstrip('/')}"
    return f"s3://{bucket}"
