# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Spark build, compiled into the job config the forge Spark runner reads.

forge runs one versioned PySpark runner (``runner/forge_spark_runner.py``, shipped
as package data and never imported here) with a generated ``job.json``. The config
says everything the job does, so the runner holds no contract logic:

- the Kafka source (``parameters.spark.source``: brokers or an MSK cluster ARN, MSK IAM
  or no auth; the topic and where a new job starts), how
  values and keys are decoded (JSON with a Spark schema, or Confluent Avro through
  ABRiS with the registry's subject and version);
- the two queries: ``source`` turns the decoded Kafka records into ``source_events``
  (:func:`fluid_build.streaming.mapping.spark_source_select`), and ``select`` makes
  the output rows from it, compiled from ``properties.mapping`` in the Spark dialect,
  or the contract's own ``properties.sql`` when the operator lists the build in
  :data:`CONTRACT_SQL_ALLOWLIST_ENV`;
- the trigger, watermark and checkpoint of a streaming job;
- the sink: one Iceberg table in the Glue catalog, at the warehouse
  :func:`fluid_build.providers.aws.util.warehouse.get_iceberg_warehouse` derives.

The text is canonical JSON (sorted keys, one trailing newline), so the same contract
always gives the same bytes and digest. It holds no credential: a registry password
is a secret reference the runner resolves, the ``maskAtIngest`` key is a Secrets
Manager ARN, and the job's AWS identity is its role. The operator's policy is applied
while it compiles: the broker and registry hosts, the registry credential's secrets
policy, and the contract-SQL allowlist.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from fluid_build.providers._iceberg_catalog import (
    FAMILY_GLUE,
    catalog_kind_info,
    resolve_iceberg_catalog,
)
from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.aws.util.warehouse import bucket_uses_fallback, normalize_location
from fluid_build.streaming.mapping import (
    bind_by_name,
    compile_select,
    parse_stream,
    spark_schema,
    spark_source_select,
)
from fluid_build.streaming.types import parse_fields

#: The version of the job config's shape. The runner refuses any other.
CONFIG_VERSION = 1
#: Operator-only: the builds whose own ``properties.sql`` the job may run, as in
#: :mod:`fluid_build.spark.policy`. Defined here so naming it in a message needs no
#: module-level policy import: the policy reaches build_runners (through
#: streaming.policy), which a tier-0 leaf importing this module must not.
CONTRACT_SQL_ALLOWLIST_ENV = "FLUID_SPARK_CONTRACT_SQL_ALLOWLIST"
#: The Spark catalog the runner registers for the Glue catalog, and the default one,
#: so contract SQL names a table as ``<database>.<table>``.
CATALOG = "fluid"
KAFKA_AUTH = ("msk-iam", "none")
RUNTIMES = ("emr-serverless", "emr-ec2", "emr-eks", "glue")
MODES = ("streaming", "batch")
#: The ``properties.stream`` keys only an engine reads; the rest describe the records.
_ENGINE_KEYS = ("trigger", "watermark", "checkpoint", "startingOffsets", "registry")
_PRODUCT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_BUILD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_BROKER = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?:[0-9]{1,5}"
_BROKERS = re.compile(rf"{_BROKER}(?:,{_BROKER})*")
#: The brokers of an MSK cluster named by ARN: the spark-infra uploader fills this in when
#: it renders the job config, from the cluster's broker string for the source's ``auth``
#: (``bootstrap_brokers_sasl_iam`` for msk-iam, ``bootstrap_brokers_tls`` for none); the
#: runner refuses a config that still holds it.
MSK_BROKERS_PLACEHOLDER = "${fluid_msk_bootstrap_brokers}"
_MSK_ARN = re.compile(r"arn:aws[a-z-]*:kafka:[a-z0-9-]+:[0-9]{12}:cluster/[A-Za-z0-9_./-]{1,256}")
_SECRET_ARN = re.compile(
    r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}"
)
_GROUP_PREFIX = re.compile(r"[A-Za-z0-9._-]{1,200}")
_CHECKPOINT = re.compile(r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[A-Za-z0-9._/=-]*")
_REGISTRY_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")
_SUBJECT = re.compile(r"[A-Za-z0-9._-]{1,255}")
#: Secret stores the runner reads a registry credential from: its environment, or
#: AWS Secrets Manager with the job's role.
_REGISTRY_SECRET_SCHEMES = ("env", "aws-sm")


@dataclass(frozen=True)
class SparkJob:
    """One build's job config, as the runner reads it."""

    config: Mapping[str, Any]

    @property
    def text(self) -> str:
        return json.dumps(self.config, sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def spark_parameters(build: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """``properties.parameters.spark``, or None when the build does not declare it."""
    props = build.get("properties")
    parameters = props.get("parameters") if isinstance(props, Mapping) else None
    if not isinstance(parameters, Mapping) or "spark" not in parameters:
        return None
    cfg = parameters["spark"]
    if not isinstance(cfg, Mapping):
        raise ValueError("parameters.spark must be an object")
    return cfg


def spark_builds(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The builds the forge Spark runner runs: ``engine: spark`` with parameters.spark."""
    return [
        build
        for build in contract.get("builds") or []
        if isinstance(build, Mapping)
        and build.get("engine") == "spark"
        and spark_parameters(build) is not None
    ]


def _seconds(text: Any, where: str) -> float:
    # The repo's ISO-8601 duration parser; days, hours, minutes and seconds only, as a
    # job has no calendar to place a month in.
    from fluid_build.build_runners._late_arrival import parse_iso_duration

    duration = parse_iso_duration(text) if isinstance(text, str) else None
    if duration is None or duration.total_seconds() <= 0:
        raise ValueError(
            f"{where}: expected a positive ISO-8601 duration of days, hours, minutes and "
            "seconds, such as PT1M (a month or a year has no fixed length)"
        )
    return duration.total_seconds()


def _interval(seconds: float) -> str:
    """A Spark interval string, in whole milliseconds."""
    return f"{round(seconds * 1000)} milliseconds"


def _kafka(cfg: Mapping[str, Any], where: str) -> dict[str, Any]:
    """``parameters.spark.source``: the cluster the job reads, as the runner connects."""
    spec = cfg.get("source")
    where = f"{where}: parameters.spark.source"
    if not isinstance(spec, Mapping):
        raise ValueError(f"{where}: a streaming build names its Kafka cluster")
    unknown = sorted(set(spec) - {"bootstrapServers", "mskClusterArn", "auth", "groupIdPrefix"})
    if unknown:
        raise ValueError(f"{where}: unsupported field(s) {unknown}")
    servers, arn = spec.get("bootstrapServers"), spec.get("mskClusterArn")
    if (servers is None) == (arn is None):
        raise ValueError(f"{where}: name the cluster by bootstrapServers or mskClusterArn")
    if servers is not None and not (isinstance(servers, str) and _BROKERS.fullmatch(servers)):
        raise ValueError(f"{where}.bootstrapServers: expected host:port[,host:port...]")
    if arn is not None and not (isinstance(arn, str) and _MSK_ARN.fullmatch(arn)):
        raise ValueError(f"{where}.mskClusterArn: expected an MSK cluster ARN")
    auth = spec.get("auth", "msk-iam" if arn is not None else "none")
    if auth not in KAFKA_AUTH:
        raise ValueError(f"{where}.auth: expected one of {list(KAFKA_AUTH)}")
    prefix = spec.get("groupIdPrefix")
    if prefix is not None and not (isinstance(prefix, str) and _GROUP_PREFIX.fullmatch(prefix)):
        raise ValueError(f"{where}.groupIdPrefix: expected letters, digits, ., _ and -")
    return {
        "bootstrapServers": MSK_BROKERS_PLACEHOLDER if servers is None else servers,
        "mskClusterArn": arn,
        "auth": auth,
        # MSK's listener without IAM is TLS-only by default, so an unauthenticated read of
        # a cluster named by ARN speaks SSL; msk-iam is SASL_SSL either way.
        "tls": arn is not None and auth == "none",
        "groupIdPrefix": prefix,
    }


def _source_hosts(kafka: Mapping[str, Any], registry_url: Optional[str]) -> dict[str, Any]:
    source: dict[str, Any] = {}
    if kafka["mskClusterArn"] is None:
        source["bootstrapServers"] = kafka["bootstrapServers"]
    if registry_url is not None:
        source["schemaRegistryUrl"] = registry_url
    return source


def _needs_entry(host: str) -> bool:
    """Whether the host rule wants an operator entry for ``host``: every host name (the job
    resolves it itself), and a public address. A private address needs none, and a
    link-local one is refused even with one."""
    import ipaddress

    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_global
    except ValueError:
        return True  # a host name


def _unlisted_names(source: Mapping[str, Any], environ: Any) -> list[str]:
    """The broker and registry hosts that need an operator entry the allowlist lacks."""
    from fluid_build.secrets.policy import is_loopback
    from fluid_build.streaming.policy import is_listed

    hosts = [e.rpartition(":")[0] for e in str(source.get("bootstrapServers") or "").split(",")]
    if source.get("schemaRegistryUrl"):
        hosts.append(urlsplit(source["schemaRegistryUrl"]).hostname or "")
    unlisted = []
    for host in filter(None, hosts):
        if not _needs_entry(host) or is_loopback(host) or is_listed(host, environ):
            continue
        if host not in unlisted:
            unlisted.append(host)
    return unlisted


def _refuse_unless_hosts_allowed(source: Mapping[str, Any], environ: Any, *, listed: bool) -> None:
    """The managed Flink lane's broker and registry host rule, unchanged: the job resolves
    each name itself, so a hostname needs the operator's entry.

    ``listed=False`` (``fluid validate``) takes the names the allowlist lacks as listed,
    so only what fails at every stage (a link-local or unreadable host) is refused;
    :func:`validation_warnings` reports the missing entries (policy #818: a missing
    entry warns at validate and refuses at run time).
    """
    from fluid_build.spark.policy import SparkPolicyError
    from fluid_build.streaming.policy import (
        HOST_ALLOWLIST_ENV,
        FlinkPolicyError,
        refuse_unless_source_hosts_allowed,
    )

    env = dict(os.environ if environ is None else environ)
    if not listed:
        names = _unlisted_names(source, env)
        if names:
            env[HOST_ALLOWLIST_ENV] = ",".join(filter(None, [env.get(HOST_ALLOWLIST_ENV), *names]))
    try:
        refuse_unless_source_hosts_allowed({"source": source}, environ=env)
    except FlinkPolicyError as exc:
        # The rule is the managed Flink lane's; the refusal is this Spark job's.
        raise SparkPolicyError(
            what=exc.what.replace("the managed Flink lane", "a forge Spark job"),
            why=exc.why,
            fix=exc.fix,
            doc=exc.doc,
        ) from None


def _refuse_unless_mask_key_allowed(arn: str, environ: Any) -> None:
    """The secrets policy for the key the job hashes with, read as ``aws-sm://<arn>``.

    The contract names the secret and the job's role reads it, so without the operator's
    locator prefixes a product could hash with another product's key, and join its own
    digests of chosen values against that product's masked table. The value never
    leaves the job, so no destination applies.
    """
    from fluid_build.secrets.policy import check_prefix, check_scheme
    from fluid_build.secrets.ref import parse_secret_ref

    ref = parse_secret_ref(f"aws-sm://{arn}")
    check_scheme(ref, environ)
    check_prefix(ref, environ)


def _refuse_unless_credential_allowed(
    ref: Any, url: str, where: str, environ: Any, *, granted: bool = True
) -> None:
    """The secrets policy for a credential the job sends to the registry.

    The contract names both the secret and the host, so without this a product could
    have the job's role read another product's secret and send it wherever it chose.
    The job sends from the cluster, so a loopback host is no exemption here.

    ``granted=False`` (``fluid validate``) leaves out only the operator's destination
    grant, which :func:`validation_warnings` names instead (policy #818: a missing
    operator entry warns at validate and refuses at compile and run).
    """
    from fluid_build.secrets.policy import (
        check_prefix,
        check_scheme,
        is_loopback,
        refuse_unless_allowed,
    )

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"{where}.registry.url: a registry credential is sent over https only")
    if is_loopback(parts.hostname):
        raise ValueError(
            f"{where}.registry.url: a registry credential goes to a named registry host; "
            "loopback on the cluster is the cluster itself"
        )
    check_scheme(ref, environ)
    check_prefix(ref, environ)
    if granted:
        refuse_unless_allowed(ref, host=parts.hostname, environ=environ)


def _registry(
    spec: Any, stream: Any, where: str, environ: Any, *, granted: bool = True
) -> Optional[dict[str, Any]]:
    if spec is None:
        if stream.uses_registry:
            raise ValueError(f"{where}.registry: avro-confluent records need their registry")
        return None
    if not stream.uses_registry:
        raise ValueError(f"{where}.registry: a registry applies only to avro-confluent")
    if not isinstance(spec, Mapping):
        raise ValueError(f"{where}.registry: expected an object")
    if spec.get("kind") != "confluent":
        raise ValueError(
            f"{where}.registry.kind: the forge Spark runner reads the Confluent Schema "
            "Registry (kind: confluent); the Glue Schema Registry is not supported yet"
        )
    url = spec.get("url")
    if not isinstance(url, str) or not _REGISTRY_URL.fullmatch(url):
        raise ValueError(f"{where}.registry.url: expected an http(s) URL with no credentials")
    subject = spec.get("subject", f"{stream.topic}-value")
    if not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
        raise ValueError(f"{where}.registry.subject: expected a subject name")
    version = spec.get("version", "latest")
    if version != "latest" and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        raise ValueError(f"{where}.registry.version: expected latest or a version from 1")
    result: dict[str, Any] = {"url": url, "subject": subject, "version": version, "auth": None}
    auth = spec.get("auth")
    if auth is not None:
        from fluid_build.secrets.ref import parse_secret_ref

        raw = auth.get("secretRef") if isinstance(auth, Mapping) else None
        ref = parse_secret_ref(raw)
        if ref.scheme not in _REGISTRY_SECRET_SCHEMES:
            raise ValueError(
                f"{where}.registry.auth.secretRef: the Spark runner reads a registry "
                f"credential from {' or '.join(_REGISTRY_SECRET_SCHEMES)} references, "
                f"not {ref.scheme}"
            )
        _refuse_unless_credential_allowed(ref, url, where, environ, granted=granted)
        result["auth"] = {"secretRef": ref.canonical}
    return result


def _decoders(stream: Any, registry: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """How the runner decodes ``value`` and ``key``, by the stream's formats."""

    def decoder(fmt: str, fields: Any, subject: Optional[str]) -> dict[str, Any]:
        if fmt == "json":
            return {"format": "json", "schema": spark_schema(fields)}
        if fmt == "avro-confluent":
            version = registry["version"] if subject == registry["subject"] else "latest"
            return {
                "format": "avro-confluent",
                "subject": subject,
                "version": version,
                # The runner checks the decoded record against the declared fields.
                "schema": spark_schema(fields),
            }
        return {"format": "raw"}

    key = None
    if stream.key_format is not None:
        prefix = len(stream.key_prefix)
        fields = tuple((name[prefix:], dtype) for name, dtype in stream.key)
        key = decoder(stream.key_format, fields, f"{stream.topic}-key")
    value_subject = registry["subject"] if registry else None
    return {"value": decoder(stream.format, stream.value, value_subject), "key": key}


def _trigger(spec: Any, expose: Mapping[str, Any], where: str) -> Optional[dict[str, Any]]:
    if spec is None:
        slo = (expose.get("qos") or {}).get("freshnessSLO")
        if slo is None:
            return None
        # A micro-batch every half SLO lands a record within it, the batch included.
        return {"processingTime": _interval(_seconds(slo, "qos.freshnessSLO") / 2)}
    if not isinstance(spec, Mapping):
        raise ValueError(f"{where}.trigger: expected an object")
    if spec.get("availableNow") is True and len(spec) == 1:
        return {"availableNow": True}
    if set(spec) == {"processingTime"}:
        return {"processingTime": _interval(_seconds(spec["processingTime"], f"{where}.trigger"))}
    raise ValueError(f"{where}.trigger: expected processingTime or availableNow: true")


def _watermark(spec: Any, stream: Any, where: str) -> Optional[dict[str, Any]]:
    if spec is None:
        return None
    if not isinstance(spec, Mapping) or set(spec) != {"column", "delay"}:
        raise ValueError(f"{where}.watermark: expected column and delay")
    column = spec["column"]
    dtype = stream.columns.get(column) if isinstance(column, str) else None
    # Spark's withWatermark reads a top-level column of the source query, and an instant
    # only: it refuses TIMESTAMP_NTZ (EVENT_TIME_IS_NOT_ON_TIMESTAMP_TYPE, Spark 3.5.7).
    if dtype is None or dtype.kind not in {"timestamp", "timestamp_ltz"}:
        raise ValueError(f"{where}.watermark.column: expected a top-level timestamp column")
    if dtype.kind != "timestamp_ltz":
        raise ValueError(
            f"{where}.watermark.column: {column} is a timestamp (wall-clock time); Spark keeps "
            "a watermark on an instant only, so declare it timestamp_ltz"
        )
    return {"column": column, "delay": _interval(_seconds(spec["delay"], f"{where}.watermark"))}


def _object(value: Any, where: str) -> Mapping[str, Any]:
    """An optional object of the contract: {} when absent, refused when not an object."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: expected an object")
    return value


def _contract_sql(sql: Any, bid: str) -> str:
    if not isinstance(sql, str) or not re.match(r"(?is)^\s*SELECT\b", sql) or ";" in sql:
        raise ValueError(f"Spark build {bid}: properties.sql must be a single SELECT without ;")
    return sql


def compile_job_config(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    *,
    account_ref: str = "",
    environ: Optional[Mapping[str, str]] = None,
    contract_sql_gate: bool = True,
    host_gate: bool = True,
) -> SparkJob:
    """The job config of one Spark build.

    ``account_ref`` fills the ``<account>-fluid-data`` bucket of a binding without one,
    as the IaC emitter's interpolation token or an account id. ``environ`` holds the
    operator's policy (:data:`CONTRACT_SQL_ALLOWLIST_ENV`, the broker and registry host
    allowlist, the secrets policy). ``contract_sql_gate=False`` and ``host_gate=False``
    leave the contract-SQL allowlist, the host allowlist's entries and the registry
    credential's destination grant to the caller (``fluid validate`` warns about them).
    """
    from fluid_build.spark.policy import refuse_unless_contract_sql_allowed

    cfg = spark_parameters(build)
    bid = build.get("id")
    if cfg is None or build.get("engine") != "spark":
        raise ValueError("A forge Spark build declares engine: spark and parameters.spark")
    if not isinstance(bid, str) or not _BUILD.fullmatch(bid):
        raise ValueError("A forge Spark build needs an id of letters, digits, _ and -")
    product = contract.get("id")
    if not isinstance(product, str) or not _PRODUCT.fullmatch(product) or ".." in product:
        raise ValueError("A forge Spark build needs a product id of letters, digits, ., _ and -")
    where = f"Spark build {bid}"
    if cfg.get("runtime") not in RUNTIMES:
        raise ValueError(f"{where}: parameters.spark.runtime: expected one of {list(RUNTIMES)}")
    props = build.get("properties") or {}
    stream_spec, mapping, sql = props.get("stream"), props.get("mapping"), props.get("sql")
    mode = cfg.get("mode") or ("streaming" if stream_spec is not None else "batch")
    if mode not in MODES:
        raise ValueError(f"{where}: parameters.spark.mode: expected one of {list(MODES)}")
    if sql is not None and mapping is not None:
        raise ValueError(f"{where}: declare properties.mapping or properties.sql, not both")
    if mode == "streaming" and stream_spec is None:
        raise ValueError(f"{where}: a streaming job reads the records properties.stream declares")
    if mode == "batch" and (stream_spec is not None or sql is None):
        # A batch read of a topic sees only what Kafka still retains, so overwriting the
        # table with it would drop every older row; a stream keeps its position instead.
        raise ValueError(
            f"{where}: a batch job runs properties.sql over catalog tables; to land a topic "
            "on a schedule, set parameters.spark.mode: streaming with "
            "properties.stream.trigger.availableNow: true"
        )

    outputs = build.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 1:
        raise ValueError(f"{where}: declare exactly one Iceberg output")
    targets = [
        (i, e)
        for i, e in enumerate(contract.get("exposes") or [])
        if isinstance(e, Mapping) and e.get("exposeId") == outputs[0]
    ]
    if len(targets) != 1:
        raise ValueError(f"{where}: output must identify one expose")
    index, expose = targets[0]
    binding = _object(expose.get("binding"), f"exposes[{index}].binding")
    if binding.get("format") != "iceberg":
        raise ValueError(f"{where}: the output's binding.format must be iceberg")
    loc = _object(binding.get("location"), f"exposes[{index}].binding.location")
    if bucket_uses_fallback(loc) and not account_ref:
        raise ValueError(
            f"{where}: the output names no bucket, so its warehouse is the account's "
            "<account>-fluid-data bucket; pass the account"
        )
    resolved = resolve_iceberg_catalog(binding, account_ref=account_ref)
    if catalog_kind_info(resolved.kind).family != FAMILY_GLUE:
        raise ValueError(f"{where}: the forge Spark runner writes Glue-catalog Iceberg tables")
    parts = resolved.fq_table.split(".")
    if len(parts) != 2 or "None" in parts:
        raise ValueError(f"{where}: the output needs one database and table")
    database, table = (validate_ident(p) for p in parts)
    fields = parse_fields(
        _object(expose.get("contract"), f"exposes[{index}].contract").get("schema"),
        path=f"exposes[{index}].contract.schema",
    )
    bucket, _ = normalize_location(loc, account_ref=account_ref)

    source: Optional[dict[str, Any]] = None
    source_sql: Optional[str] = None
    streaming: dict[str, Any] = {"trigger": None, "watermark": None, "checkpoint": None}
    masked: list[str] = []
    if stream_spec is not None:
        if not isinstance(stream_spec, Mapping):
            raise ValueError(f"{where}: properties.stream: expected an object")
        swhere = f"{where}: properties.stream"
        records = {k: v for k, v in stream_spec.items() if k not in _ENGINE_KEYS}
        stream = parse_stream(records, swhere)
        kafka = _kafka(cfg, where)
        registry = _registry(
            stream_spec.get("registry"), stream, swhere, environ, granted=host_gate
        )
        _refuse_unless_hosts_allowed(
            _source_hosts(kafka, registry and registry["url"]), environ, listed=host_gate
        )
        offsets = stream_spec.get("startingOffsets", "latest")
        if offsets not in ("earliest", "latest"):
            raise ValueError(f"{swhere}.startingOffsets: expected earliest or latest")
        source = {
            "kind": "kafka",
            **kafka,
            "topic": stream.topic,
            "startingOffsets": offsets,
            "includeHeaders": any(src == "headers" for _, src in stream.metadata),
            "registry": (
                None if registry is None else {"url": registry["url"], "auth": registry["auth"]}
            ),
            **_decoders(stream, registry),
        }
        source_sql = spark_source_select(stream, swhere)
        # The default is built from the warehouse bucket, which may be the IaC emitter's
        # account token; only a declared checkpoint is the contract's text to check.
        checkpoint = stream_spec.get("checkpoint")
        if checkpoint is None:
            checkpoint = f"s3://{bucket}/_checkpoints/{product}/{bid}/"
        elif not isinstance(checkpoint, str) or not _CHECKPOINT.fullmatch(checkpoint):
            raise ValueError(f"{swhere}.checkpoint: expected an s3:// URI with a prefix")
        streaming = {
            "trigger": _trigger(stream_spec.get("trigger"), expose, swhere),
            "watermark": _watermark(stream_spec.get("watermark"), stream, swhere),
            "checkpoint": checkpoint.rstrip("/") + "/",
        }
        if sql is None:
            select = compile_select(
                stream,
                mapping,
                fields,
                where=f"{where}: properties.mapping",
                target_where=f"exposes[{index}].contract.schema",
                dialect="spark",
            )
            masked = sorted(
                str(c.get("name"))
                for c in (mapping or {}).get("columns") or []
                if isinstance(c, Mapping) and c.get("maskAtIngest")
            )
    mask_key = cfg.get("maskKeySecretArn")
    if masked and mask_key is None:
        raise ValueError(
            f"{where}: maskAtIngest hashes with the product's key; set "
            "parameters.spark.maskKeySecretArn to the Secrets Manager secret that holds it"
        )
    if mask_key is not None:
        if not (isinstance(mask_key, str) and _SECRET_ARN.fullmatch(mask_key)):
            raise ValueError(f"{where}: parameters.spark.maskKeySecretArn: expected a secret ARN")
        _refuse_unless_mask_key_allowed(mask_key, environ)
    if sql is not None:
        if contract_sql_gate:
            refuse_unless_contract_sql_allowed(product, build, environ)
        select = bind_by_name(_contract_sql(sql, bid), fields)

    config = {
        "configVersion": CONFIG_VERSION,
        "product": product,
        "build": bid,
        "mode": mode,
        "source": source,
        "sql": {"source": source_sql, "select": select},
        **streaming,
        "sink": {
            "catalog": CATALOG,
            "type": "glue",
            "warehouse": resolved.warehouse,
            "region": resolved.region,
            "table": f"{database}.{table}",
        },
        "maskedColumns": masked,
        # The secret's ARN, never its value: the runner reads the key with the job's role.
        "maskKey": {"secretArn": mask_key} if masked else None,
    }
    job = SparkJob(config)
    # The uploader fills in the broker placeholder and an IaC account token; any other
    # ${...} came from the contract, and the runner refuses it as an interpolation nobody
    # resolved.
    rendered = job.text.replace(MSK_BROKERS_PLACEHOLDER, "")
    if account_ref:
        rendered = rendered.replace(account_ref, "")
    if "${" in rendered:
        raise ValueError(
            f"{where}: the job config would hold '${{', which the runner refuses as an "
            "unresolved interpolation; take it out of the contract (properties.sql included)"
        )
    return job


def validation_errors(contract: Mapping[str, Any]) -> list[str]:
    """What ``fluid validate`` reports for the contract's Spark builds, on 0.7.7 or later.

    The contract-SQL allowlist is a warning here (:func:`validation_warnings`), as on
    the managed Flink lane; the host and secrets policies are errors.
    """
    from fluid_build._errors import FluidUserError
    from fluid_build.streaming.compiler import managed_lane_enabled

    if not managed_lane_enabled(contract):
        return []
    errors = []
    try:
        builds = spark_builds(contract)
    except ValueError as exc:
        return [f"Spark: {exc}"]
    for build in builds:
        try:
            # A stand-in account: a bucketless binding validates; apply passes the real one.
            compile_job_config(
                contract,
                build,
                account_ref="000000000000",
                contract_sql_gate=False,
                host_gate=False,
            )
        except FluidUserError as exc:
            errors.append(f"Spark build {build.get('id')}: {exc.what}. {exc.why}")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"Spark: {exc}")
    return errors


def validation_warnings(contract: Mapping[str, Any]) -> list[str]:
    """The operator entries a Spark build lacks, which ``fluid validate`` warns about.

    A registry that receives a credential is left to the contract-wide checks, which name
    its host entry (``schema_registry/policy.py``) and the credential's grant
    (``secrets/policy.py``); repeating them here would print each twice.
    """
    from fluid_build.streaming.compiler import managed_lane_enabled
    from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

    if not managed_lane_enabled(contract):
        return []
    warnings = []
    try:
        builds = spark_builds(contract)
    except ValueError:
        return []  # validation_errors reports it
    for build in builds:
        props = build.get("properties") or {}
        if props.get("sql") is not None:
            warnings.append(
                f"Build {build.get('id')}: properties.sql is run only if the operator lists "
                f"{contract.get('id')} in {CONTRACT_SQL_ALLOWLIST_ENV}; properties.mapping is "
                "checked against the output before deploy and reads nothing but the stream"
            )
        stream = props.get("stream")
        if not isinstance(stream, Mapping):
            continue
        try:
            kafka = _kafka(spark_parameters(build) or {}, f"Spark build {build.get('id')}")
            registry = stream.get("registry")
            url = registry.get("url") if isinstance(registry, Mapping) else None
            if isinstance(registry, Mapping) and registry.get("auth") is not None:
                url = None  # the secret check names this host
            source = _source_hosts(kafka, url if isinstance(url, str) else None)
            names = _unlisted_names(source, None)
        except ValueError:
            continue  # validation_errors reports it
        if names:
            warnings.append(
                f"Build {build.get('id')}: not in {HOST_ALLOWLIST_ENV}: {', '.join(names)}; "
                "the job is refused at compile and run time until the operator lists them"
            )
    return warnings
