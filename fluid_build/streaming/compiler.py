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

"""Pure compiler for the opt-in Flink Operator / Iceberg lane.

The runtime remains external. Compilation never contacts Kubernetes, a catalog,
or a registry, and unsupported capabilities fail before deployment.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import jsonschema

from fluid_build.providers._iceberg_catalog import (
    catalog_kind_info,
    resolve_iceberg_catalog,
)
from fluid_build.providers._iceberg_catalog_auth import FORBIDDEN_TEXT_PATTERN, parse_catalog_auth
from fluid_build.providers._sql_safety import quote_ansi_string_literal, validate_ident
from fluid_build.schema_manager import schema_validator
from fluid_build.streaming.mapping import SOURCE_TABLE, bind_by_name, compile_select, parse_stream
from fluid_build.streaming.types import LogicalType, columns_sql, parse_fields
from fluid_build.util.contract import is_externally_run_build

#: The schema the managed lane arrived in. On an older contract
#: ``parameters.flink`` stays what it was: free-form settings of a job that
#: runs elsewhere, which forge-cli does not read.
_MANAGED_SINCE = "0.7.7"
#: What the rendered FlinkDeployment fixes or defaults, whatever the contract says.
#: The digest covers the rendered spec, so changing one names a new ConfigMap.
_FLINK_VERSION = "v1_20"
_JAR_URI = "local:///opt/flink/usrlib/forge-sql-runner.jar"
_ENTRY_CLASS = "io.fluid.forge.ForgeSqlRunner"
_DEFAULT_PARALLELISM = 1
_DEFAULT_TASK_SLOTS = 2
_DEFAULT_IDLE_TIMEOUT_MS = 60000
_DEFAULT_CHECKPOINT_INTERVAL_MS = 10000
_DEFAULT_CHECKPOINT_TIMEOUT_MS = 120000
_DEFAULT_JOB_MANAGER_MEMORY = "1024m"
_DEFAULT_TASK_MANAGER_MEMORY = "2048m"
#: Stands for the ConfigMap's name inside the spec the digest is taken of: the name
#: is made from the digest, so it cannot be in what the digest covers.
_CONFIGMAP_PLACEHOLDER = "<configmap>"
#: Products whose own properties.sql the operator lets apply deploy
#: (streaming.policy.refuse_unless_contract_sql_allowed). Defined here, not in
#: streaming.policy, because validate reaches this module and must not import
#: build_runners.
CONTRACT_SQL_ALLOWLIST_ENV = "FLUID_FLINK_CONTRACT_SQL_ALLOWLIST"
_NAME = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_TEXT = {"type": "string", "minLength": 1}
_POSITIVE = {"type": "integer", "minimum": 1}
_CONFIG = {
    "type": "object",
    "additionalProperties": False,
    "required": ["management", "source", "deployment"],
    "properties": {
        "management": {"const": "operator"},
        "source": {
            "type": "object",
            "additionalProperties": False,
            "required": ["bootstrapServers", "groupId"],
            "properties": {
                "bootstrapServers": _TEXT,
                "groupId": _TEXT,
                "schemaRegistryUrl": _TEXT,
                "startupMode": {"enum": ["earliest-offset", "group-offsets"]},
                "eventTime": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["column", "delaySeconds"],
                    "properties": {
                        "column": _TEXT,
                        "delaySeconds": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
        "deployment": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "name",
                "namespace",
                "context",
                "image",
                "serviceAccount",
                "checkpointUri",
                "savepointUri",
                "haUri",
            ],
            "properties": {
                "name": {"type": "string", "pattern": _NAME},
                "namespace": {"type": "string", "pattern": _NAME},
                "serviceAccount": {"type": "string", "pattern": _NAME},
                "context": _TEXT,
                "image": {"type": "string", "pattern": r"^[^\s]+@sha256:[0-9a-f]{64}$"},
                "checkpointUri": _TEXT,
                "savepointUri": _TEXT,
                "haUri": _TEXT,
                "parallelism": _POSITIVE,
                "checkpointIntervalMs": {"type": "integer", "minimum": 1000},
                "checkpointTimeoutMs": {"type": "integer", "minimum": 1000},
                "idleTimeoutMs": _POSITIVE,
                "state": {"enum": ["running", "suspended"]},
                "secretRefs": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": _NAME},
                },
                "jobManagerMemory": {"type": "string", "pattern": r"^[1-9][0-9]*[mg]$"},
                "taskManagerMemory": {"type": "string", "pattern": r"^[1-9][0-9]*[mg]$"},
                "taskSlots": _POSITIVE,
            },
        },
        "catalogAuth": {"type": "object"},
        "catalogProperties": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "s3.endpoint": _TEXT,
                "s3.path-style-access": {"enum": ["true", "false"]},
                "client.region": _TEXT,
            },
        },
        "maxSnapshotAgeSeconds": _POSITIVE,
    },
}


@dataclass(frozen=True)
class FlinkArtifacts:
    build_id: str
    expose_id: str
    digest: str
    statements: tuple[str, ...]
    resources: tuple[dict[str, Any], ...]
    target_fields: tuple[tuple[str, LogicalType], ...]

    @property
    def sql(self) -> str:
        return ";\n\n".join(self.statements) + ";\n"

    def plan_action(self, step: int) -> dict[str, Any]:
        return {
            "step": step,
            "action_id": self.build_id,
            "provider": "flink-operator",
            "depends_on": [],
            "op": "flink.reconcile",
            "action_type": "flink.reconcile",
            "id": self.build_id,
            "payload": {
                "artifactDigest": self.digest,
                "resources": list(self.resources),
                "stateImpact": "savepoint upgrade; no automatic reset or table replacement",
            },
        }


def managed_config(build: Mapping[str, Any]) -> Mapping[str, Any] | None:
    props = build.get("properties")
    parameters = props.get("parameters") if isinstance(props, Mapping) else None
    if not isinstance(parameters, Mapping) or "flink" not in parameters:
        return None
    cfg = parameters["flink"]
    if not isinstance(cfg, Mapping):
        raise ValueError("parameters.flink must be an object")
    if not is_externally_run_build(build):
        raise ValueError("parameters.flink requires engine: flink or language: flink_sql")
    return cfg


def managed_lane_enabled(contract: Mapping[str, Any]) -> bool:
    """Whether the contract declares a bundled schema that has the managed lane.

    Only an exact bundled version counts. An unknown, prerelease or padded
    ``fluidVersion`` was never checked against a schema that has the lane,
    and ``fluid verify`` would otherwise reach the cluster for it.
    """
    # Local import: schema_manager imports this module lazily, so a module-level
    # import here would make the cycle real the day either side stops deferring.
    from fluid_build.schema_manager import FluidSchemaManager, SchemaVersion

    version = contract.get("fluidVersion")
    if not isinstance(version, str) or version not in FluidSchemaManager.BUNDLED_VERSIONS:
        return False
    return SchemaVersion.parse(_MANAGED_SINCE) <= SchemaVersion.parse(version)


def managed_builds(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not managed_lane_enabled(contract):
        return []
    return [
        build
        for build in contract.get("builds") or []
        if isinstance(build, Mapping) and managed_config(build) is not None
    ]


def _url(value: str, label: str, schemes: tuple[str, ...] = ("http", "https")) -> None:
    if "\\" in value:  # Python and Java split such a URL into different hosts
        raise ValueError(f"{label}: a backslash is not allowed in a URL")
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{label}: expected a {schemes} URI without inline credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label}: query/fragment is not supported")


def _options(options: Mapping[str, str]) -> str:
    return ",\n  ".join(
        f"{quote_ansi_string_literal(k)} = {quote_ansi_string_literal(v)}"
        for k, v in sorted(options.items())
    )


def _properties(statements: tuple[str, ...]) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")

    return f"count={len(statements)}\n" + "".join(
        f"statement.{i}={escape(statement)}\n" for i, statement in enumerate(statements)
    )


def compile_build(contract: Mapping[str, Any], build: Mapping[str, Any]) -> FlinkArtifacts:
    cfg = managed_config(build)
    if cfg is None:
        raise ValueError("This Flink build is externally managed; opt in with parameters.flink")
    source = cfg.get("source")
    moved = (
        sorted({"topic", "format", "schema"} & set(source)) if isinstance(source, Mapping) else []
    )
    if moved:
        raise ValueError(
            f"parameters.flink.source.{moved[0]}: the topic, format and schema of the records "
            "are declared in properties.stream; parameters.flink.source keeps the connection"
        )
    # schema_validator, so that a pattern ends at the end of the value, not
    # before a trailing newline as jsonschema's own re.search lets it.
    validator = schema_validator(_CONFIG, default=jsonschema.Draft202012Validator)
    errors = sorted(validator.iter_errors(cfg), key=lambda e: str(e.path))
    if errors:
        # Do not echo instance values: an invalid value might be a pasted credential.
        problem = errors[0]
        raise ValueError(
            f"parameters.flink.{'.'.join(map(str, problem.path))}: invalid configuration ({problem.validator})"
        )
    if not managed_lane_enabled(contract):
        raise ValueError(f"Managed Flink needs schema {_MANAGED_SINCE} or later")
    if contract.get("accessPolicy") or contract.get("governance"):
        raise ValueError("Managed Flink cannot yet enforce root accessPolicy/governance")
    bid = build.get("id")
    if not isinstance(bid, str) or not bid:
        raise ValueError("Managed Flink requires a build id")
    if (build.get("execution") or {}).get("trigger", {}).get("type") != "continuous":
        raise ValueError(f"Flink build {bid}: execution.trigger.type must be continuous")
    outputs = build.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 1:
        raise ValueError(f"Flink build {bid}: declare exactly one Iceberg output")
    targets = [
        (i, e)
        for i, e in enumerate(contract.get("exposes") or [])
        if e.get("exposeId") == outputs[0]
    ]
    if len(targets) != 1:
        raise ValueError(f"Flink build {bid}: output must identify one expose")
    index, target = targets[0]
    if (
        target.get("policy")
        or target.get("quality")
        or (target.get("contract") or {}).get("quality")
        or (target.get("contract") or {}).get("dq")
    ):
        raise ValueError(
            "Managed Flink does not yet compile output policy/quality rules; enforce them explicitly in SQL"
        )

    def reject_uncompiled_rules(node: Any) -> None:
        if isinstance(node, Mapping):
            if any(node.get(key) for key in ("quality", "validationRules", "policy")):
                raise ValueError("Managed Flink cannot yet enforce nested policy/quality rules")
            for key in ("fields", "element", "key", "value"):
                reject_uncompiled_rules(node.get(key))
        elif isinstance(node, list):
            for child in node:
                reject_uncompiled_rules(child)

    reject_uncompiled_rules((target.get("contract") or {}).get("schema"))
    binding = target.get("binding") or {}
    if binding.get("format") != "iceberg":
        raise ValueError(f"Flink build {bid}: output format must be iceberg")
    resolved = resolve_iceberg_catalog(binding)
    kind = catalog_kind_info(resolved.kind)
    if not kind.speaks_rest:
        raise ValueError("Managed Flink currently supports Iceberg REST-family catalogs only")
    if not resolved.uri or not resolved.warehouse:
        raise ValueError("Iceberg REST binding requires uri and warehouse")
    _url(resolved.uri, "catalog.uri")
    if kind.warehouse_is_name and "://" in resolved.warehouse:
        raise ValueError("This REST catalog requires a warehouse name, not a storage URI")
    if kind.warehouse_is_name:
        raise ValueError("Managed Flink currently requires an explicit S3 warehouse URI")
    _url(resolved.warehouse, "Iceberg warehouse", ("s3",))
    table_parts = resolved.fq_table.split(".")
    if len(table_parts) != 2 or "None" in table_parts:
        raise ValueError("Iceberg target requires one namespace/database and table")
    namespace, table = (validate_ident(x) for x in table_parts)
    fields = parse_fields(
        (target.get("contract") or {}).get("schema"), path=f"exposes[{index}].contract.schema"
    )
    props = build.get("properties") or {}
    if "stream" not in props:
        raise ValueError(
            f"Flink build {bid}: managed Flink reads the records properties.stream declares "
            "(kind, topic, format, schema)"
        )
    stream_spec = props["stream"]
    if isinstance(stream_spec, Mapping):
        reject_uncompiled_rules(stream_spec.get("schema"))
        key = stream_spec.get("key")
        if isinstance(key, Mapping):
            reject_uncompiled_rules(key.get("schema"))
    stream = parse_stream(stream_spec, f"Flink build {bid}: properties.stream")
    source = cfg["source"]
    registry = source.get("schemaRegistryUrl")
    if stream.uses_registry:
        if not registry:
            raise ValueError("An avro-confluent value or key requires source.schemaRegistryUrl")
        _url(registry, "source.schemaRegistryUrl")
    elif registry:
        raise ValueError("schemaRegistryUrl applies only to avro-confluent")
    source_options = {
        "connector": "kafka",
        "topic": stream.topic,
        "properties.bootstrap.servers": source["bootstrapServers"],
        "properties.group.id": source["groupId"],
        "scan.startup.mode": source.get("startupMode", "group-offsets"),
        "properties.auto.offset.reset": "earliest",
        **stream.format_options(registry),
    }
    deployment = cfg["deployment"]
    uris = [deployment[k].rstrip("/") for k in ("checkpointUri", "savepointUri", "haUri")]
    for uri in uris:
        _url(uri, "state storage", ("s3",))
        if not urlsplit(uri).path.strip("/"):
            raise ValueError("State storage requires a dedicated prefix, not a bucket root")
    if any(
        a == b or a.startswith(b + "/") or b.startswith(a + "/")
        for i, a in enumerate(uris)
        for b in uris[i + 1 :]
    ):
        raise ValueError("Checkpoint, savepoint and HA storage prefixes must not overlap")
    checkpoint_ms = deployment.get("checkpointIntervalMs", _DEFAULT_CHECKPOINT_INTERVAL_MS)
    if deployment.get("checkpointTimeoutMs", _DEFAULT_CHECKPOINT_TIMEOUT_MS) <= checkpoint_ms:
        raise ValueError("checkpointTimeoutMs must exceed checkpointIntervalMs")
    event_time = source.get("eventTime")
    watermark = ""
    if event_time:
        name = validate_ident(event_time["column"])
        dtype = stream.columns.get(name)
        if dtype is None or dtype.kind not in {"timestamp", "timestamp_ltz"}:
            raise ValueError("eventTime.column must identify a top-level timestamp")
        # Flink accepts a rowtime of precision 0 to 3 only, and the stream's default is 6.
        if (6 if dtype.precision is None else dtype.precision) > 3:
            raise ValueError(
                "eventTime.column must be a timestamp of precision 3 or less, such as "
                "timestamp_ltz(3): Flink's event time is milliseconds"
            )
        watermark = (
            f",\n  WATERMARK FOR `{name}` AS `{name}`"
            f" - INTERVAL '{event_time['delaySeconds']}' SECOND"
        )
    sql = props.get("sql")
    if sql is not None and props.get("mapping") is not None:
        raise ValueError(
            f"Flink build {bid}: declare properties.mapping or properties.sql, not both"
        )
    if sql is None:
        query = compile_select(
            stream,
            props.get("mapping"),
            fields,
            where=f"Flink build {bid}: properties.mapping",
            target_where=f"exposes[{index}].contract.schema",
        )
    else:
        # A contract's own SELECT, bound by name to the output columns.
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("Flink build properties.sql must be a SELECT query")
        # This gate is intentionally conservative. Flink's matching planner performs
        # authoritative semantic validation; Forge must not mistake DDL for a transform.
        if not re.match(r"(?is)^\s*SELECT\b", sql) or ";" in sql:
            raise ValueError(
                "Managed Flink properties.sql must be a single SELECT without semicolons"
            )
        # A /*+ OPTIONS(...) */ hint overrides the source's connector options at query
        # time (dynamic table options are on by default), so contract SQL could point
        # the Kafka source at another broker or wire a config provider that reads the
        # pod's secrets. Flink reads a hint only from the literal "/*+" token.
        if "/*+" in sql:
            raise ValueError("Managed Flink properties.sql must not contain SQL hints (/*+ ... */)")
        query = bind_by_name(sql, fields)
    iceberg = binding.get("icebergConfig") or {}
    if iceberg.get("writeVersion", 2) != 2:
        raise ValueError("Managed Flink requires Iceberg format version 2")
    if iceberg.get("sortOrder"):
        raise ValueError("Streaming sortOrder is not implemented; do not imply sorted files")
    table_properties = iceberg.get("properties") or {}
    allowed_table_properties = {"write.target-file-size-bytes", "write.parquet.compression-codec"}
    if set(table_properties) - allowed_table_properties:
        raise ValueError("Unsupported Iceberg table properties; this lane is append-only")
    partition = []
    for position, item in enumerate(iceberg.get("partitionSpec") or []):
        try:
            name = validate_ident(item["sourceColumn"])
        except ValueError as exc:
            raise ValueError(
                f"icebergConfig.partitionSpec[{position}].sourceColumn: {exc}"
            ) from exc
        if name not in dict(fields):
            raise ValueError(f"Partition source {name!r} is not a target column")
        transform = item["transform"]
        if transform == "identity":
            partition.append(f"`{name}`")
        elif transform in {"year", "month", "day", "hour", "bucket", "truncate"}:
            # Iceberg's transforms are hidden partitioning, which Flink SQL cannot declare:
            # Iceberg's Flink DDL partitions by plain columns only, and PARTITIONED BY
            # (days(col)) does not parse, so the job would fail after it deploys.
            raise ValueError(
                f"Partition transform {transform!r} on {name!r}: Flink's Iceberg DDL can "
                "partition only by a column (transform identity). Create the table with "
                "this partition spec before the job runs, and leave it out here"
            )
        else:
            raise ValueError(f"Unsupported partition transform {transform!r}")
    auth, auth_errors = parse_catalog_auth(cfg.get("catalogAuth"))
    if auth_errors:
        raise ValueError("; ".join(auth_errors))
    catalog_options = {
        "type": "iceberg",
        "catalog-type": "rest",
        "uri": resolved.uri,
        "warehouse": resolved.warehouse,
        **dict(resolved.extra_catalog_props),
        **dict(cfg.get("catalogProperties") or {}),
    }
    if resolved.io_impl:
        catalog_options["io-impl"] = resolved.io_impl
    if resolved.region:
        catalog_options.setdefault("client.region", resolved.region)
    # Iceberg's REST client replaces any catalog property that starts with env: by
    # that variable from the pod (RESTSessionCatalog.initialize ->
    # EnvironmentUtil.resolveAll), so contract text here could have the pod send any
    # of its variables: a region is signed into every S3 request. Only catalogAuth's
    # own reference, merged below and paired with its hosts by the operator, may.
    for key, value in catalog_options.items():
        if re.search(FORBIDDEN_TEXT_PATTERN, str(value)):
            raise ValueError(
                f"catalog property {key} must not start with env: or contain ${{ or a "
                "control character; only catalogAuth may reference a variable"
            )
    if "client.region" in catalog_options and not re.fullmatch(
        _NAME, str(catalog_options["client.region"])
    ):
        raise ValueError("client.region must be a region name such as eu-central-1")
    if auth:
        if urlsplit(resolved.uri).scheme != "https":
            raise ValueError("Authenticated Iceberg catalogs require HTTPS")
        if auth.type == "oauth2" and urlsplit(str(auth.oauth2_server_uri)).scheme != "https":
            raise ValueError("An OAuth2 token endpoint requires HTTPS: it receives the secret")
        catalog_options.update(auth.catalog_properties())
    if "s3.endpoint" in catalog_options:
        _url(catalog_options["s3.endpoint"], "s3.endpoint")
    target_name = f"`lake`.`{namespace}`.`{table}`"
    partition_sql = f"\nPARTITIONED BY ({', '.join(partition)})" if partition else ""
    statements = (
        f"CREATE CATALOG `lake` WITH (\n  {_options(catalog_options)}\n)",
        f"CREATE DATABASE IF NOT EXISTS `lake`.`{namespace}`",
        f"CREATE TABLE IF NOT EXISTS {target_name} (\n  {columns_sql(fields)}\n)"
        f"{partition_sql}\nWITH (\n  {_options({'format-version': '2', 'write.format.default': iceberg.get('fileFormat', 'parquet'), **table_properties})}\n)",
        f"CREATE TEMPORARY TABLE `{SOURCE_TABLE}` (\n  {stream.ddl()}{watermark}\n)"
        f" WITH (\n  {_options(source_options)}\n)",
        f"INSERT INTO {target_name}\n{query}",
    )
    config = {
        "execution.checkpointing.interval": f"{checkpoint_ms} ms",
        "execution.checkpointing.timeout": f"{deployment.get('checkpointTimeoutMs', _DEFAULT_CHECKPOINT_TIMEOUT_MS)} ms",
        "execution.checkpointing.mode": "EXACTLY_ONCE",
        "execution.checkpointing.externalized-checkpoint-retention": "RETAIN_ON_CANCELLATION",
        "state.checkpoints.dir": deployment["checkpointUri"],
        "state.savepoints.dir": deployment["savepointUri"],
        "high-availability.type": "kubernetes",
        "high-availability.storageDir": deployment["haUri"],
        "taskmanager.numberOfTaskSlots": str(deployment.get("taskSlots", _DEFAULT_TASK_SLOTS)),
        "table.exec.source.idle-timeout": f"{deployment.get('idleTimeoutMs', _DEFAULT_IDLE_TIMEOUT_MS)} ms",
        "kubernetes.operator.job.upgrade.last-state-fallback.enabled": "false",
    }
    storage_options = catalog_options
    if storage_options.get("s3.endpoint"):
        config["s3.endpoint"] = storage_options["s3.endpoint"]
    if storage_options.get("s3.path-style-access"):
        config["s3.path.style.access"] = storage_options["s3.path-style-access"]
    if storage_options.get("client.region"):
        config["s3.endpoint.region"] = storage_options["client.region"]
    spec: dict[str, Any] = {
        "image": deployment["image"],
        "flinkVersion": _FLINK_VERSION,
        "serviceAccount": deployment["serviceAccount"],
        "flinkConfiguration": config,
        "jobManager": {
            "resource": {
                "memory": deployment.get("jobManagerMemory", _DEFAULT_JOB_MANAGER_MEMORY),
                "cpu": 1,
            }
        },
        "taskManager": {
            "resource": {
                "memory": deployment.get("taskManagerMemory", _DEFAULT_TASK_MANAGER_MEMORY),
                "cpu": 1,
            }
        },
        "podTemplate": {
            "spec": {
                "containers": [
                    {
                        "name": "flink-main-container",
                        "envFrom": [
                            {"secretRef": {"name": s}} for s in deployment.get("secretRefs", [])
                        ],
                        "volumeMounts": [
                            {"name": "forge-sql", "mountPath": "/etc/forge", "readOnly": True}
                        ],
                    }
                ],
                "volumes": [{"name": "forge-sql", "configMap": {"name": _CONFIGMAP_PLACEHOLDER}}],
            }
        },
        "job": {
            "jarURI": _JAR_URI,
            "entryClass": _ENTRY_CLASS,
            "args": ["/etc/forge/job.properties"],
            "parallelism": deployment.get("parallelism", _DEFAULT_PARALLELISM),
            "upgradeMode": "savepoint",
            "allowNonRestoredState": False,
            "state": deployment.get("state", "running"),
        },
    }
    payload = {
        "artifactVersion": "flink-iceberg-v2",
        "product": contract.get("id"),
        "build": bid,
        "statements": statements,
        "deployment": deployment,
        # Everything the deployment renders, so a constant or a default that the
        # contract does not set still changes the digest and with it the ConfigMap's
        # name: the ConfigMap is immutable, and its deployment.json carries them.
        "spec": spec,
        "targetSchema": (target.get("contract") or {}).get("schema"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    name = deployment["name"]
    cm_name = f"{name[:40]}-{digest[:16]}"
    labels = {"app.kubernetes.io/managed-by": "fluid-forge"}
    metadata = {
        "name": name,
        "namespace": deployment["namespace"],
        "labels": labels,
        "annotations": {
            "fluid.build/digest": digest,
            "fluid.build/product": str(contract.get("id")),
            "fluid.build/build": bid,
        },
    }
    spec["podTemplate"]["spec"]["volumes"][0]["configMap"]["name"] = cm_name
    config_map: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {**metadata, "name": cm_name},
        "immutable": True,
        "data": {
            "job.properties": _properties(statements)
            + f"expected.target={target_name}\n"
            + "expected.type=ROW<"
            + columns_sql(fields).replace("\n", " ")
            + ">\n"
        },
    }
    manifest = {
        "apiVersion": "flink.apache.org/v1beta1",
        "kind": "FlinkDeployment",
        "metadata": metadata,
        "spec": spec,
    }
    config_map["data"]["deployment.json"] = json.dumps(manifest["spec"], sort_keys=True)
    if sum(len(value.encode("utf-8")) for value in config_map["data"].values()) > 1024 * 1024:
        raise ValueError("Generated ConfigMap data exceeds Kubernetes' 1 MiB limit")
    return FlinkArtifacts(bid, outputs[0], digest, statements, (config_map, manifest), fields)


def compile_contract(contract: Mapping[str, Any]) -> tuple[FlinkArtifacts, ...]:
    return tuple(artifact for _, artifact in compile_builds(contract))


def compile_builds(
    contract: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], FlinkArtifacts], ...]:
    """Each managed build with the artifacts compiled from it, paired by position.

    Whatever acts on an artifact checks the build it came from, never one found by
    id: two builds may share an id, and an operator's allowlists would then be
    judged on the wrong one.
    """
    builds = managed_builds(contract)
    artifacts = tuple(compile_build(contract, build) for build in builds)
    ids = [a.build_id for a in artifacts]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(f"Managed Flink build ids must be unique; {repeated[0]!r} is used twice")
    if builds and (
        len(builds) != len(contract.get("builds") or [])
        or {e.get("exposeId") for e in contract.get("exposes") or []}
        != {output for b in builds for output in b.get("outputs") or []}
    ):
        raise ValueError(
            "Managed streaming contracts must contain only managed Flink builds and their outputs; split batch/platform provisioning into a separate contract"
        )
    identities = [
        (a.resources[1]["metadata"]["namespace"], a.resources[1]["metadata"]["name"])
        for a in artifacts
    ]
    if len(set(identities)) != len(identities):
        raise ValueError("Managed Flink deployments must have unique namespace/name identities")
    if len({a.expose_id for a in artifacts}) != len(artifacts):
        raise ValueError("Only one managed Flink writer is allowed per expose")
    prefixes: list[str] = []
    for build in builds:
        config = managed_config(build)
        if config is not None:
            prefixes.extend(
                config["deployment"][key].rstrip("/")
                for key in ("checkpointUri", "savepointUri", "haUri")
            )
    if any(
        a == b or a.startswith(b + "/") or b.startswith(a + "/")
        for i, a in enumerate(prefixes)
        for b in prefixes[i + 1 :]
    ):
        raise ValueError("State storage prefixes must not overlap between managed builds")
    return tuple(zip(builds, artifacts, strict=True))


def validation_errors(contract: Mapping[str, Any]) -> list[str]:
    try:
        compile_contract(contract)
    except (ValueError, KeyError, TypeError) as exc:
        return [f"Managed Flink: {exc}"]
    return []


def validation_warnings(contract: Mapping[str, Any]) -> list[str]:
    """An older contract that asks for the managed lane still validates, as before.

    Its build stays run outside forge-cli, so say why nothing will deploy it. On the
    preview, a managed build that brings its own SQL is told that apply needs the
    operator to allow it.
    """
    if managed_lane_enabled(contract):
        try:
            builds = managed_builds(contract)
        except (ValueError, KeyError, TypeError):
            return []  # validation_errors reports it
        warnings = [
            f"Build {build.get('id')}: properties.sql is run only if the operator lists "
            f"{contract.get('id')} in {CONTRACT_SQL_ALLOWLIST_ENV}; properties.mapping is "
            "checked against the output before deploy and reads nothing but the stream"
            for build in builds
            if (build.get("properties") or {}).get("sql") is not None
        ]
        for build in contract.get("builds") or []:
            props = build.get("properties") if isinstance(build, Mapping) else None
            if isinstance(props, Mapping) and "mapping" in props and build not in builds:
                warnings.append(
                    f"Build {build.get('id')}: properties.mapping is compiled only by the "
                    "managed Flink lane (parameters.flink.management); nothing reads it here"
                )
        return warnings
    warnings = []
    for build in contract.get("builds") or []:
        props = build.get("properties") if isinstance(build, Mapping) else None
        parameters = props.get("parameters") if isinstance(props, Mapping) else None
        flink = parameters.get("flink") if isinstance(parameters, Mapping) else None
        if isinstance(flink, Mapping) and "management" in flink:
            warnings.append(
                f"Build {build.get('id')}: parameters.flink.management needs fluidVersion "
                f"{_MANAGED_SINCE} or later; on {contract.get('fluidVersion')} the job still "
                "runs outside forge-cli and fluid apply does not deploy it"
            )
    return warnings
