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

"""DataMesh Manager output-port + provider extraction helpers.

Lifted from ``providers/datamesh_manager/datamesh_manager.py`` (host
file was 2361 LOC). Five static helpers — pure transforms over the
contract section dict — that previously sat on
:class:`DataMeshManagerProvider` as ``@staticmethod`` definitions.

* :func:`build_server_object` — DPS ``server`` dict (account /
  database / schema / table / topic / location) per provider.
* :func:`extract_provider` — sniff the provider name from a section.
* :func:`resolve_location` — flatten a binding.location dict into the
  legacy stringified form for older DMM clients.
* :func:`extract_links` — pull the contract's ``links`` block.
* :func:`extract_custom` — pull the contract's ``customProperties`` block.

The host class re-binds each as a staticmethod so existing call
sites (``self._build_server_object(...)``) keep resolving.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping


def _build_server_object(section: Mapping[str, Any], provider: str) -> Dict[str, Any]:
    """Build a structured ``server`` object for an output port.

    The DPS schema expects keys like ``account``, ``database``,
    ``schema``, ``table``, ``topic``, ``location`` etc. inside a
    server object — NOT a flat location string.
    """
    server: Dict[str, Any] = {}
    provider_lower = provider.lower() if provider else ""

    # ---- FLUID 0.7.1: binding.location ----
    binding = section.get("binding", {})
    if isinstance(binding, dict):
        loc = binding.get("location", {})
        if isinstance(loc, dict) and loc:
            if provider_lower in ("gcp", "bigquery"):
                if loc.get("project"):
                    server["account"] = str(loc["project"])
                if loc.get("dataset"):
                    server["database"] = str(loc["dataset"])
                if loc.get("table"):
                    server["table"] = str(loc["table"])
                return server

            if provider_lower == "snowflake":
                for key in ("account", "database", "schema", "table"):
                    if loc.get(key):
                        server[key] = str(loc[key])
                return server

            if provider_lower in ("aws", "s3"):
                bucket = loc.get("bucket", "")
                path_val = loc.get("path", loc.get("prefix", loc.get("key", "")))
                if bucket:
                    loc_str = f"s3://{bucket}"
                    if path_val:
                        loc_str += "/{}".format(str(path_val).strip("/"))
                    server["location"] = loc_str
                fmt = binding.get("format")
                if fmt:
                    server["format"] = str(fmt)
                return server

            if provider_lower == "redshift":
                for key in ("database", "schema", "table"):
                    if loc.get(key):
                        server[key] = str(loc[key])
                return server

            if provider_lower == "kafka":
                if loc.get("topic"):
                    server["topic"] = str(loc["topic"])
                return server

            # Generic: copy all location fields (skip template vars)
            for k, v in loc.items():
                if v and not str(v).startswith("{{") and k != "region":
                    server[k] = v
            return server

    # ---- Legacy: flat provider keys ----
    cfg: Mapping[str, Any] = {}
    if provider_lower in ("gcp", "bigquery"):
        cfg = section.get("gcp", section.get("bigquery", {}))
        if isinstance(cfg, dict):
            if cfg.get("project"):
                server["account"] = str(cfg["project"])
            if cfg.get("dataset"):
                server["database"] = str(cfg["dataset"])
            if cfg.get("table"):
                server["table"] = str(cfg["table"])

    elif provider_lower == "snowflake":
        cfg = section.get("snowflake", {})
        if isinstance(cfg, dict):
            for key in ("account", "database", "schema", "table"):
                if cfg.get(key):
                    server[key] = str(cfg[key])

    elif provider_lower in ("aws", "s3"):
        cfg = section.get("aws", section.get("s3", {}))
        if isinstance(cfg, dict):
            bucket = cfg.get("bucket", "")
            prefix = cfg.get("prefix", cfg.get("key", ""))
            if bucket:
                loc_str = f"s3://{bucket}"
                if prefix:
                    loc_str += f"/{prefix}"
                server["location"] = loc_str

    elif provider_lower == "redshift":
        cfg = section.get("redshift", {})
        if isinstance(cfg, dict):
            for key in ("database", "schema", "table"):
                if cfg.get(key):
                    server[key] = str(cfg[key])

    elif provider_lower == "kafka":
        cfg = section.get("kafka", {})
        if isinstance(cfg, dict):
            if cfg.get("topic"):
                server["topic"] = str(cfg["topic"])

    # Fallback: location/connection string
    if not server:
        conn = section.get("location") or section.get("connection", "")
        if isinstance(conn, dict):
            uri = conn.get("uri", conn.get("endpoint", ""))
            if uri:
                server["location"] = str(uri)
        elif conn:
            server["location"] = str(conn)

    return server


# ---- location helpers -------------------------------------------------


def _extract_provider(section: Mapping[str, Any]) -> str:
    """Extract provider/platform name from an expose or expect block.

    Supports both legacy (``provider: gcp``) and FLUID 0.7.1
    (``binding.platform: gcp``) patterns.
    """
    # 0.7.1 pattern: binding.platform
    binding = section.get("binding", {})
    if isinstance(binding, dict):
        platform = binding.get("platform", "")
        if platform:
            return str(platform)
    # Legacy pattern
    return str(section.get("provider", ""))


def _resolve_location(section: Mapping[str, Any], provider: str) -> str:
    """Build a human-readable location string from provider config.

    Supports both legacy flat keys (``section.gcp``, ``section.snowflake``)
    and FLUID 0.7.1 ``binding.location`` pattern.
    """
    provider_lower = provider.lower() if provider else ""
    parts: List[str] = []

    # ---- FLUID 0.7.1: binding.location ----
    binding = section.get("binding", {})
    if isinstance(binding, dict):
        loc = binding.get("location", {})
        if isinstance(loc, dict) and loc:
            if provider_lower in ("gcp", "bigquery"):
                for key in ("project", "dataset", "table"):
                    if key in loc:
                        parts.append(str(loc[key]))
                if parts:
                    return ".".join(parts)

            elif provider_lower == "snowflake":
                for key in ("database", "schema", "table"):
                    if key in loc:
                        parts.append(str(loc[key]))
                if parts:
                    return ".".join(parts)

            elif provider_lower in ("aws", "s3"):
                bucket = loc.get("bucket", "")
                path_val = loc.get("path", loc.get("prefix", loc.get("key", "")))
                if bucket:
                    result = f"s3://{bucket}"
                    if path_val:
                        result += "/{}".format(str(path_val).strip("/"))
                    return result
                # Glue/Athena style
                for key in ("database", "table"):
                    if key in loc:
                        parts.append(str(loc[key]))
                if parts:
                    return ".".join(parts)

            elif provider_lower == "redshift":
                for key in ("database", "schema", "table"):
                    if key in loc:
                        parts.append(str(loc[key]))
                if parts:
                    return ".".join(parts)

            elif provider_lower == "kafka":
                topic = loc.get("topic", "")
                if topic:
                    return str(topic)

            # Generic fallback for unknown providers with binding.location
            if not parts:
                generic_parts = [
                    str(v)
                    for k, v in loc.items()
                    if k not in ("region",) and v and not str(v).startswith("{{")
                ]
                if generic_parts:
                    return ".".join(generic_parts)

    # ---- Legacy: flat provider keys (section.gcp, section.snowflake) ----
    if provider_lower in ("gcp", "bigquery"):
        cfg = section.get("gcp", section.get("bigquery", {}))
        if isinstance(cfg, dict):
            for key in ("project", "dataset", "table"):
                if key in cfg:
                    parts.append(str(cfg[key]))

    elif provider_lower == "snowflake":
        cfg = section.get("snowflake", {})
        if isinstance(cfg, dict):
            for key in ("database", "schema", "table"):
                if key in cfg:
                    parts.append(str(cfg[key]))

    elif provider_lower in ("aws", "s3"):
        cfg = section.get("aws", section.get("s3", {}))
        if isinstance(cfg, dict):
            bucket = cfg.get("bucket", "")
            prefix = cfg.get("prefix", cfg.get("key", ""))
            if bucket:
                loc_str = f"s3://{bucket}"
                if prefix:
                    loc_str += f"/{prefix}"
                return loc_str

    elif provider_lower == "redshift":
        cfg = section.get("redshift", {})
        if isinstance(cfg, dict):
            for key in ("database", "schema", "table"):
                if key in cfg:
                    parts.append(str(cfg[key]))

    elif provider_lower == "kafka":
        cfg = section.get("kafka", {})
        if isinstance(cfg, dict):
            topic = cfg.get("topic", "")
            if topic:
                return str(topic)

    # Fallback: explicit location / connection field
    if not parts:
        conn = section.get("location") or section.get("connection", "")
        if isinstance(conn, dict):
            return str(conn.get("uri", conn.get("endpoint", "")))
        return str(conn) if conn else ""

    return ".".join(parts)


# ---- links & custom ---------------------------------------------------


def _extract_links(fluid: Mapping[str, Any]) -> Dict[str, str]:
    links: Dict[str, str] = {}
    meta = fluid.get("metadata", {})
    if isinstance(meta, dict):
        for key in ("documentation", "repository", "catalog", "dataProduct"):
            val = meta.get(key)
            if val:
                links[key] = str(val)
    top = fluid.get("links", {})
    if isinstance(top, dict):
        links.update({k: str(v) for k, v in top.items()})
    return links


def _extract_custom(fluid: Mapping[str, Any]) -> Dict[str, Any]:
    custom: Dict[str, Any] = {}
    meta = fluid.get("metadata", {})
    if isinstance(meta, dict):
        for key in ("domain", "subdomain", "environment", "version", "layer", "sla"):
            val = meta.get(key)
            if val is not None:
                custom[key] = val
    explicit = fluid.get("custom", {})
    if not isinstance(explicit, dict):
        explicit = meta.get("custom", {}) if isinstance(meta, dict) else {}
    if isinstance(explicit, dict):
        custom.update(explicit)
    return custom
