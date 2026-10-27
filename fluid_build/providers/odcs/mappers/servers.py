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

"""ODCS servers ↔ FLUID expects / exposes binding.

ODCS ``servers[]`` carry one entry per source/sink. FLUID models the same
information in two places: each ``expose.binding`` (where data is produced)
and each ``expect`` (where data is consumed). On export we read from both;
on import everything becomes ``expects[]`` entries that the caller can later
re-attach to exposes if desired.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)
from .types import provider_to_server_type, server_type_to_provider

LOG = logging.getLogger(__name__)

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
        "id": (server.get("server") or server.get("name") or server.get("id") or "dependency"),
        "provider": server_type_to_provider(server_type),
    }
    location = _location_from_server(server)
    if location:
        expect["location"] = location
    return expect


def _location_from_server(server: Mapping[str, Any]) -> Dict[str, Any]:
    location: Dict[str, Any] = {}
    for key in (
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
        "format",
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
        merged = _merge_binding_into_location(location, binding)
        server.update(_server_details_from_location(merged, provider))
    return server


def _merge_binding_into_location(
    location: Mapping[str, Any], binding: Optional[Mapping[str, Any]]
) -> Mapping[str, Any]:
    """FLUID exposes carry server-shaped fields like ``format`` at the
    *binding* level (sibling of ``location``). ODCS's LocalServer (and
    several other server types) requires ``format`` on the server
    record itself. Merge any such binding-level field into the
    location dict so :func:`_server_details_from_location` sees them
    in one place. Location wins over binding when both are set.
    """
    if not isinstance(binding, Mapping):
        return location
    # Fields that ODCS expects on the server record but FLUID puts at
    # the binding sibling level (e.g. binding.format alongside
    # binding.location). Extend as new server types surface them.
    # ``serialization`` is not copied onto the server as-is: the Kafka branch
    # of :func:`_server_details_from_location` reads it to decide the ODCS
    # message ``format``, and no other server type looks at it.
    _BINDING_LIFTED = ("format", "delimiter", "endpointUrl", "serialization")
    merged: Dict[str, Any] = dict(location)
    for key in _BINDING_LIFTED:
        if key not in merged and key in binding:
            merged[key] = binding[key]
    return merged


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
        merged = _merge_binding_into_location(location, binding)
        server.update(_server_details_from_location(merged, provider))
    return server


def _server_details_from_location(location: Mapping[str, Any], provider: str) -> Dict[str, Any]:
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
        # ODCS BigQueryServer requires {project, dataset}.
        for required_key, env_var in (
            ("project", "${GCP_PROJECT}"),
            ("dataset", "${BIGQUERY_DATASET}"),
        ):
            details.setdefault(required_key, env_var)
    elif p == "snowflake":
        for key in ("host", "port", "account", "database", "schema", "warehouse"):
            if key in location:
                details[key] = location[key]
        # ODCS v3.1.0 SnowflakeServer requires {account, database, schema}.
        # Real forge-AI runs against Gemini Flash routinely emit a binding
        # like ``{database: <name>, schema: <name>}`` without ``account``,
        # which fails vendored-schema validation. Synthesize env-var
        # placeholders for the required-but-missing fields so the
        # generated contract is valid out of the box; deploy-time
        # substitution fills them in. Users / LLMs that DID provide the
        # field win — we only fill gaps.
        for required_key, env_var in (
            ("account", "${SNOWFLAKE_ACCOUNT}"),
            ("database", "${SNOWFLAKE_DATABASE}"),
            ("schema", "${SNOWFLAKE_SCHEMA}"),
        ):
            details.setdefault(required_key, env_var)
    elif p in ("aws", "s3"):
        # ODCS S3Server: location (URI), endpointUrl, format, delimiter
        loc_val = location.get("location") or _build_s3_uri(
            location.get("bucket"), location.get("path") or location.get("key")
        )
        if loc_val:
            details["location"] = loc_val
        for key in ("endpointUrl", "format", "delimiter"):
            if key in location:
                details[key] = location[key]
        # ODCS S3Server requires {location}.
        details.setdefault("location", "${S3_LOCATION}")
    elif p == "kafka":
        # ``host`` is the declared home of the bootstrap servers since 0.7.6.
        # ``account`` stays as a fallback because until then the schema
        # rejected ``host`` and accepted ``account``, so a Kafka contract that
        # named its brokers at all named them there; dropping the fallback
        # would turn each of those exports back into the placeholder below.
        host = location.get("host") or location.get("account")
        if host:
            details["host"] = host
        message_format = _kafka_message_format(location.get("serialization"))
        if message_format:
            details["format"] = message_format
        elif "format" in location:
            # No declared message format, so keep what this mapper has always
            # written for it: ``binding.format`` (the storage kind, usually
            # the legacy token ``kafka_topic``) rather than guessing. Falling
            # back to ODCS's own ``json`` default would swap a meaningless
            # token for a confident claim that is wrong for every Avro topic,
            # and would change the export of every contract that has not
            # adopted ``binding.serialization``. The token goes away when the
            # contract says what its messages are.
            details["format"] = location["format"]
        # ODCS KafkaServer requires {host}; ``format`` is optional and
        # defaults to json. Neither fallback is a claim about the topic: the
        # placeholder is substituted at deploy time like the other server
        # types', and an absent host is the documented, tested behaviour.
        details.setdefault("host", "${KAFKA_BOOTSTRAP_SERVERS}")
        details.setdefault("format", "json")
    elif p in ("postgres", "postgresql"):
        for key in ("host", "port", "database", "schema"):
            if key in location:
                details[key] = location[key]
        # ODCS PostgreSQLServer requires {host, database}.
        for required_key, env_var in (
            ("host", "${POSTGRES_HOST}"),
            ("database", "${POSTGRES_DATABASE}"),
        ):
            details.setdefault(required_key, env_var)
    elif p == "mysql":
        for key in ("host", "port", "database"):
            if key in location:
                details[key] = location[key]
        # ODCS MySQLServer requires {host, database}.
        for required_key, env_var in (
            ("host", "${MYSQL_HOST}"),
            ("database", "${MYSQL_DATABASE}"),
        ):
            details.setdefault(required_key, env_var)
    elif p == "databricks":
        for key in ("host", "catalog", "schema"):
            if key in location:
                details[key] = location[key]
        # ODCS DatabricksServer requires {host, catalog, schema}.
        for required_key, env_var in (
            ("host", "${DATABRICKS_HOST}"),
            ("catalog", "${DATABRICKS_CATALOG}"),
            ("schema", "${DATABRICKS_SCHEMA}"),
        ):
            details.setdefault(required_key, env_var)
    elif p == "redshift":
        for key in ("host", "database", "schema", "region", "account"):
            if key in location:
                details[key] = location[key]
        # ODCS RedshiftServer requires {host, database}.
        for required_key, env_var in (
            ("host", "${REDSHIFT_HOST}"),
            ("database", "${REDSHIFT_DATABASE}"),
        ):
            details.setdefault(required_key, env_var)
    elif p == "local":
        for key in ("path", "format"):
            if key in location:
                details[key] = location[key]
        # ODCS LocalServer requires {path, format}.
        details.setdefault("path", "./data")
        details.setdefault("format", "csv")
    elif p in ("athena",):
        for key in ("stagingDir", "schema", "catalog", "regionName"):
            if key in location:
                details[key] = location[key]
        # ODCS AthenaServer requires {stagingDir, schema}.
        for required_key, env_var in (
            ("stagingDir", "${ATHENA_STAGING_DIR}"),
            ("schema", "${ATHENA_SCHEMA}"),
        ):
            details.setdefault(required_key, env_var)
    else:
        # Custom / unknown provider → ODCS server.type=custom which accepts
        # the union of all server-type fields. Copy any that match the union.
        custom_keys = {
            "account",
            "catalog",
            "database",
            "dataset",
            "delimiter",
            "endpointUrl",
            "format",
            "host",
            "location",
            "path",
            "port",
            "project",
            "region",
            "regionName",
            "schema",
            "serviceName",
            "stagingDir",
            "warehouse",
            "stream",
        }
        for key, value in location.items():
            if key in custom_keys:
                details[key] = value
    return details


# FLUID ``binding.serialization.value`` -> the ODCS v3.1.0 Kafka server
# ``format`` ("The format of the messages", documented as json, avro,
# protobuf or xml). The two ``-confluent`` values are those same wire formats
# carried through a Confluent schema registry, which is what the ODCS field
# means by the format of the messages, so they collapse onto the plain name.
# Whether a registry is involved is not lost: ``binding.serialization`` rides
# along verbatim in the ``fluidExtras`` custom property (extras.py) unless
# ``ODCS_FLUID_EXTRAS=false`` opts out of that blob, and ODCS has no server
# field for it that this mapper could fill honestly.
_KAFKA_MESSAGE_FORMAT = {
    "json": "json",
    "avro": "avro",
    "protobuf": "protobuf",
    "xml": "xml",
    "avro-confluent": "avro",
    "json-confluent": "json",
}


def _kafka_message_format(serialization: Any) -> Optional[str]:
    """ODCS Kafka server ``format`` for a binding's ``serialization``, or None.

    Only ``serialization.value`` describes the messages. ``serialization.key``
    is deliberately not exported: ODCS's Kafka server has one ``format`` field
    and it means the message value, so putting the key's format there would
    make the field say something else.

    None means "the contract did not say", and the caller keeps its previous
    behaviour. A value outside the schema's enum is treated the same way, with
    a warning, because the exporter does not run ``fluid validate`` first and
    must not crash on, or repeat, a format ODCS does not document.
    """
    if not isinstance(serialization, Mapping):
        return None
    value = serialization.get("value")
    if value is None:
        return None
    message_format = _KAFKA_MESSAGE_FORMAT.get(value) if isinstance(value, str) else None
    if message_format is None:
        LOG.warning(
            "binding.serialization.value %r has no ODCS Kafka message format; the server "
            "format is left as it would be without serialization. Use one of: %s.",
            value,
            ", ".join(_KAFKA_MESSAGE_FORMAT),
        )
    return message_format


def _build_s3_uri(bucket: Optional[str], path: Optional[str]) -> Optional[str]:
    if not bucket:
        return None
    if path:
        return f"s3://{bucket.rstrip('/')}/{path.lstrip('/')}"
    return f"s3://{bucket}"
