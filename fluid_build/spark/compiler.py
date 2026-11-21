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
- the trigger, watermark and checkpoint of a streaming job, and where the records it cannot
  land go (``properties.stream.deadLetter``, :mod:`fluid_build.spark.dead_letter`);
- the sink: one Iceberg table in the Glue catalog, at the warehouse
  :func:`fluid_build.providers.aws.util.warehouse.get_iceberg_warehouse` derives; or, for
  ``runtime: local``, one table in an Iceberg REST catalog the operator's Spark
  configuration declares, with the statements that create it when nothing has (on AWS,
  OpenTofu creates the table before the job runs).

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
    FAMILY_REST,
    catalog_kind_info,
    resolve_iceberg_catalog,
)
from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.aws.util.warehouse import bucket_uses_fallback, normalize_location
from fluid_build.spark import dead_letter
from fluid_build.streaming.mapping import (
    bind_by_name,
    compile_select,
    parse_stream,
    spark_schema,
    spark_source_select,
    spark_type,
)
from fluid_build.streaming.types import parse_fields

#: The version of the job config's shape. The runner refuses any other.
CONFIG_VERSION = 1
#: Operator-only: the builds whose own ``properties.sql`` the job may run, as in
#: :mod:`fluid_build.spark.policy`. Defined here so naming it in a message needs no
#: module-level policy import: the policy reaches build_runners (through
#: streaming.policy), which a tier-0 leaf importing this module must not.
CONTRACT_SQL_ALLOWLIST_ENV = "FLUID_SPARK_CONTRACT_SQL_ALLOWLIST"
#: Operator-only: the existing runtimes and MSK clusters a contract may name, as
#: ``<runtime>:<id>`` or ``msk:<cluster ARN>``, and the topics a stream's dead letters may
#: be written to, as ``msk-write:<cluster ARN>/<topic>`` or, for a cluster named by
#: ``bootstrapServers``, ``kafka-write:<host:port>/<topic>`` for each broker. A job reads
#: whichever cluster its build names, with a role that may read it, so a contract naming
#: another team's cluster would read that team's topics, and write the one its dead
#: letters go to. Unset allows none. A write needs its entry on every runtime, a broker
#: at a private address included. The IaC applies the same list
#: (``iac/providers/aws_identity.py``).
TARGET_ALLOWLIST_ENV = "FLUID_SPARK_TARGET_ALLOWLIST"
#: Operator-only: where a local run (``runtime: local``) may create or write tables in the
#: operator's Iceberg REST catalog, as ``<warehouse>/<namespace prefix>``, comma-separated.
#: ``<warehouse>`` is the binding's ``location.warehouse``, or ``fluid`` (the Spark catalog
#: the operator's properties file declares) when it names none; a prefix matches whole
#: namespace levels (``sales`` covers ``sales`` and ``sales.eu``, never ``salesforce``).
#: The job creates its tables ``IF NOT EXISTS``, so without the list a contract could name
#: another team's table and append to it. Unset allows none. On AWS the IaC owns the table.
LOCAL_NAMESPACE_ALLOWLIST_ENV = "FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST"
#: The Spark catalog the runner registers for the Glue catalog, and the default one,
#: so contract SQL names a table as ``<database>.<table>``.
CATALOG = "fluid"
KAFKA_AUTH = ("msk-iam", "none")
#: The runtime that runs the job with spark-submit on the machine ``fluid apply`` runs on.
LOCAL_RUNTIME = "local"
AWS_RUNTIMES = ("emr-serverless", "emr-ec2", "emr-eks", "glue")
RUNTIMES = (*AWS_RUNTIMES, LOCAL_RUNTIME)
MODES = ("streaming", "batch")
#: The ``properties.stream`` keys only an engine reads; the rest describe the records.
_ENGINE_KEYS = (
    "trigger",
    "watermark",
    "checkpoint",
    "startingOffsets",
    "maxOffsetsPerTrigger",
    "registry",
    "deadLetter",
)
#: Spark reads maxOffsetsPerTrigger as a long.
_MAX_OFFSETS = 2**63 - 1
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


def runs_to_completion(build: Mapping[str, Any]) -> bool:
    """Whether a Spark build's run ends on its own, so a scheduler starts it: a batch job, or
    a stream triggered ``availableNow``. Any other stream keeps running, and ``fluid apply``
    keeps it so. The mode is :func:`compile_job_config`'s rule: ``parameters.spark.mode``,
    else streaming when ``properties.stream`` is declared, else batch. Reads the build only;
    a malformed one is the compiler's to refuse."""
    props = build.get("properties")
    if not isinstance(props, Mapping):
        return True
    parameters = props.get("parameters")
    cfg = parameters.get("spark") if isinstance(parameters, Mapping) else None
    stream = props.get("stream")
    mode = (cfg.get("mode") if isinstance(cfg, Mapping) else None) or (
        "streaming" if stream is not None else "batch"
    )
    if mode == "batch":
        return True
    trigger = stream.get("trigger") if isinstance(stream, Mapping) else None
    return isinstance(trigger, Mapping) and trigger.get("availableNow") is True


def run_timeout_seconds(build: Mapping[str, Any]) -> Optional[int]:
    """``execution.runtime.timeout``: how long a scheduled run may take before it is cancelled,
    in seconds; ``None`` when the build does not say.

    Raises:
        ValueError: not an ISO-8601 duration of days, hours, minutes and seconds of at least
            one second (``PT2H``). A wait it cannot read is refused, never guessed.
    """
    execution = build.get("execution")
    runtime = execution.get("runtime") if isinstance(execution, Mapping) else None
    raw = runtime.get("timeout") if isinstance(runtime, Mapping) else None
    if raw is None:
        return None
    from fluid_build.build_runners._late_arrival import parse_iso_duration

    try:
        parsed = parse_iso_duration(raw) if isinstance(raw, str) else None
    except OverflowError:
        parsed = None
    if parsed is None or parsed.total_seconds() < 1:
        raise ValueError(
            f"execution.runtime.timeout {raw!r} is not an ISO-8601 duration of at least one "
            "second (days, hours, minutes and seconds, as PT2H)"
        )
    return int(parsed.total_seconds())


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


def cluster_listed(arn: str, environ: Any = None) -> bool:
    """Whether the operator lists the MSK cluster ``arn`` (``msk:<arn>``, exact)."""
    env = os.environ if environ is None else environ
    entries = {e.strip() for e in (env.get(TARGET_ALLOWLIST_ENV) or "").split(",")}
    return f"msk:{arn}" in entries


def _target_entries(environ: Any) -> list[str]:
    env = os.environ if environ is None else environ
    return [e.strip() for e in (env.get(TARGET_ALLOWLIST_ENV) or "").split(",") if e.strip()]


def dead_letter_write_entries(kafka: Mapping[str, Any], topic: str) -> list[str]:
    """The operator entries a stream's dead-letter topic needs: ``msk-write:<ARN>/<topic>``
    for a cluster named by ARN, else ``kafka-write:<host:port>/<topic>`` for each broker
    the build lists (any of them may take the write)."""
    if kafka.get("mskClusterArn"):
        return [f"msk-write:{kafka['mskClusterArn']}/{topic}"]
    servers = str(kafka.get("bootstrapServers") or "").split(",")
    return list(dict.fromkeys(f"kafka-write:{s.strip()}/{topic}" for s in servers if s.strip()))


def unlisted_dead_letter_writes(
    kafka: Mapping[str, Any], topic: str, environ: Any = None
) -> list[str]:
    """The entries of :func:`dead_letter_write_entries` the operator has not listed."""
    listed = set(_target_entries(environ))
    return [e for e in dead_letter_write_entries(kafka, topic) if e not in listed]


def _refuse_unless_dead_letter_topic_listed(
    bid: Any, kafka: Mapping[str, Any], topic: str, environ: Any
) -> None:
    """A stream writes its dead letters to the topic its contract names, on the cluster it
    reads: without the operator's entry, a contract could write into any topic there."""
    from fluid_build._errors import doc_url
    from fluid_build.spark.policy import SparkPolicyError

    missing = unlisted_dead_letter_writes(kafka, topic, environ)
    if not missing:
        return
    listed = ",".join([*_target_entries(environ), *missing])
    raise SparkPolicyError(
        what=f"Spark build {bid!r} writes dead letters to a topic the operator has not listed",
        why=(
            f"properties.stream.deadLetter.topic {topic} is not in {TARGET_ALLOWLIST_ENV} "
            f"(as {', '.join(missing)}). The job writes the topic its contract names, on the "
            "cluster it reads, so a contract could write into another team's topic there. "
            "A write needs its entry on every runtime, a broker at a private address "
            "included; an unset list allows none."
        ),
        fix=(
            "If this topic is the product's, have the operator list it in the OPERATOR "
            f'environment (not a project .env): export {TARGET_ALLOWLIST_ENV}="{listed}"'
        ),
        doc=doc_url(),
    )


def local_namespace_scope(resolved: Any) -> str:
    """The warehouse part of a local output's namespace entry (:data:`LOCAL_NAMESPACE_ALLOWLIST_ENV`)."""
    return str(resolved.warehouse or CATALOG)


def local_namespace_listed(scope: str, namespace: str, environ: Any = None) -> bool:
    """Whether the operator lets a local run write tables in ``scope``'s ``namespace``."""
    env = os.environ if environ is None else environ
    levels = namespace.split(".")
    for entry in (env.get(LOCAL_NAMESPACE_ALLOWLIST_ENV) or "").split(","):
        # The last "/": a warehouse may hold one (s3://b/wh, team/wh), a namespace never.
        where, _, prefix = entry.strip().rpartition("/")
        wanted = prefix.split(".") if prefix else []
        if where == scope and wanted and all(wanted) and levels[: len(wanted)] == wanted:
            return True
    return False


def _refuse_unless_namespace_listed(
    bid: Any, scope: str, namespace: str, tables: list[str], environ: Any
) -> None:
    """A local run creates its tables ``IF NOT EXISTS`` in the operator's catalog, so a
    table that exists is adopted and appended to: the namespace must be the operator's
    choice, never the contract's (a product-id table property would be the contract's)."""
    from fluid_build._errors import doc_url
    from fluid_build.spark.policy import SparkPolicyError

    if local_namespace_listed(scope, namespace, environ):
        return
    env = os.environ if environ is None else environ
    listed = ",".join(
        [
            *(e.strip() for e in (env.get(LOCAL_NAMESPACE_ALLOWLIST_ENV) or "").split(",")),
            f"{scope}/{namespace}",
        ]
    ).lstrip(",")
    raise SparkPolicyError(
        what=f"Spark build {bid!r} writes a namespace the operator has not listed",
        why=(
            f"A local run writes {', '.join(tables)} in namespace {namespace} of "
            f"{scope}, which {LOCAL_NAMESPACE_ALLOWLIST_ENV} does not list (as "
            f"{scope}/{namespace}). It creates its tables IF NOT EXISTS, so a table that "
            "exists, another team's included, would be adopted and appended to. An unset "
            "list allows none."
        ),
        fix=(
            "If this namespace is the product's, have the operator list it in the OPERATOR "
            f'environment (not a project .env): export {LOCAL_NAMESPACE_ALLOWLIST_ENV}="{listed}"'
        ),
        doc=doc_url(),
    )


def _refuse_unless_cluster_listed(product: Any, bid: Any, arn: str, environ: Any) -> None:
    """The MSK cluster an ARN source names needs the operator's entry: its brokers come
    from AWS, so the host allowlist never sees them (``fluid validate`` warns instead)."""
    from fluid_build._errors import doc_url
    from fluid_build.spark.policy import SparkPolicyError

    if cluster_listed(arn, environ):
        return
    raise SparkPolicyError(
        what=f"Spark build {bid!r} reads an MSK cluster the operator has not listed",
        why=(
            f"parameters.spark.source.mskClusterArn {arn} is not in {TARGET_ALLOWLIST_ENV} "
            f"(as msk:{arn}). The job reads the cluster its contract names, with a role that "
            "may read it, so a contract could read another team's topics. An unset list "
            "allows none."
        ),
        fix=(
            "If this cluster is the product's, have the operator list it in the OPERATOR "
            f'environment (not a project .env): export {TARGET_ALLOWLIST_ENV}="msk:{arn}" '
            "(comma-separated with any other entries)."
        ),
        doc=doc_url(),
    )


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


def _decoders(
    stream: Any, registry: Optional[Mapping[str, Any]], *, dead_letters: bool = False
) -> dict[str, Any]:
    """How the runner decodes ``value`` and ``key``, by the stream's formats. With dead
    letters, a JSON record that does not decode is kept, its text in Spark's corrupt-record
    column for the check query."""
    from fluid_build.spark.dead_letter import CORRUPT_COLUMN

    def decoder(fmt: str, fields: Any, subject: Optional[str]) -> dict[str, Any]:
        if fmt == "json":
            if not dead_letters:
                return {"format": "json", "schema": spark_schema(fields)}
            return {
                "format": "json",
                "schema": f"{spark_schema(fields)}, `{CORRUPT_COLUMN}` STRING",
                "corruptRecord": CORRUPT_COLUMN,
            }
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


def _max_offsets(spec: Any, where: str) -> dict[str, int]:
    """``source.maxOffsetsPerTrigger``, absent unless set: a build without it keeps its job
    config and its run."""
    if spec is None:
        return {}
    if isinstance(spec, bool) or not isinstance(spec, int) or not 1 <= spec <= _MAX_OFFSETS:
        raise ValueError(f"{where}.maxOffsetsPerTrigger: expected a whole number, at least 1")
    return {"maxOffsetsPerTrigger": spec}


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


def _check_local_source(local: bool, kafka: Mapping[str, Any], where: str) -> None:
    if local and kafka["mskClusterArn"] is not None:
        raise ValueError(
            f"{where}: parameters.spark.source: a local run reads bootstrapServers; "
            "nothing resolves an MSK cluster's brokers for it (keep mskClusterArn in the "
            "AWS environment's overlay)"
        )


def _checkpoint(declared: Any, bucket: Optional[str], product: str, bid: str, where: str) -> str:
    """The stream's checkpoint, ending in ``/``. The default is built from the warehouse
    bucket, which may be the IaC emitter's account token; only a declared checkpoint is
    the contract's text to check."""
    if declared is None:
        if bucket is None:
            raise ValueError(
                f"{where}.checkpoint: a local stream keeps its checkpoint in the S3 store its "
                "output names; set the output's location.bucket or properties.stream.checkpoint"
            )
        return f"s3://{bucket}/_checkpoints/{product}/{bid}/"
    if not isinstance(declared, str) or not _CHECKPOINT.fullmatch(declared):
        raise ValueError(f"{where}.checkpoint: expected an s3:// URI with a prefix")
    return declared.rstrip("/") + "/"


def _check_local_checkpoint(
    checkpoint: str, bucket: Optional[str], product: str, where: str
) -> None:
    """A local stream's declared checkpoint is the product's: under
    ``_checkpoints/<product>/``, in the output's bucket when it names one, as the IaC
    scopes the role forge creates on AWS (``iac/providers/aws_ingest._s3_grants``). The
    run writes it with the operator's S3 credentials, so a checkpoint elsewhere would be
    written wherever the contract pointed it."""
    ck_bucket, _, prefix = checkpoint[len("s3://") :].partition("/")
    home = f"_checkpoints/{product}/"
    segments = prefix.rstrip("/").split("/")
    if (
        (bucket is not None and ck_bucket != bucket)
        or not prefix.startswith(home)
        or any(part in ("", ".", "..") for part in segments)
    ):
        shown = bucket or ck_bucket
        raise ValueError(
            f"{where}.checkpoint: a local stream keeps its checkpoint under the product's "
            f"checkpoints, s3://{shown}/{home}; leave it out for "
            f"s3://{shown}/{home}<build>/, or put it under that prefix"
        )


def _check_catalog(local: bool, binding: Mapping[str, Any], family: str, where: str) -> None:
    """Refuse an output the runtime cannot write: runtime local writes a table in an Iceberg
    REST catalog on platform local, an AWS runtime a Glue-catalog table."""
    if local and (binding.get("platform") != "local" or family != FAMILY_REST):
        raise ValueError(
            f"{where}: runtime local writes a table in an Iceberg REST catalog on platform "
            "local (binding.platform: local, location.catalog lakekeeper, rest or polaris); "
            "a Glue table runs on an AWS runtime"
        )
    if not local and family != FAMILY_GLUE:
        raise ValueError(
            f"{where}: the forge Spark runner writes Glue-catalog Iceberg tables on an AWS "
            "runtime; runtime local writes a table in an Iceberg REST catalog"
        )


def _sink(local: bool, resolved: Any, database: str, table: str, fields: Any) -> dict[str, Any]:
    if local:
        return _rest_sink(resolved, database, table, fields)
    return {
        "catalog": CATALOG,
        "type": "glue",
        "warehouse": resolved.warehouse,
        "region": resolved.region,
        "table": f"{database}.{table}",
    }


def _rest_sink(resolved: Any, database: str, table: str, fields: Any) -> dict[str, Any]:
    """A local run's sink: one table in the Iceberg REST catalog the submitter declares.

    The catalog's connection (its URI, warehouse and credential) is the operator's, in the
    Spark configuration the job is submitted with: derived from the contract, a catalog
    credential the operator holds would go wherever the contract pointed it. A URI or
    warehouse the binding names is kept, and the runner refuses a submitter catalog that
    names others.

    Nothing creates the table before a local job runs, as OpenTofu does on AWS, so the sink
    carries the statements that create it: the contract's columns in an Iceberg v2 table,
    unpartitioned, as the Glue table OpenTofu emits. ``IF NOT EXISTS``: a table that exists
    is left as it is. The DDL follows the managed Flink lane's
    (``fluid_build/streaming/compiler.py``) in Spark's syntax.
    """
    name = f"`{CATALOG}`.`{database}`"
    columns = ",\n  ".join(
        f"`{column}` {spark_type(dtype)}{' NOT NULL' if dtype.required else ''}"
        for column, dtype in fields
    )
    sink: dict[str, Any] = {
        "catalog": CATALOG,
        "type": "rest",
        "kind": resolved.kind,
        "table": f"{database}.{table}",
        "create": [
            f"CREATE NAMESPACE IF NOT EXISTS {name}",
            f"CREATE TABLE IF NOT EXISTS {name}.`{table}` (\n  {columns}\n) USING iceberg"
            " TBLPROPERTIES ('format-version' = '2')",
        ],
    }
    if resolved.uri:
        sink["uri"] = str(resolved.uri)
    if resolved.warehouse:
        sink["warehouse"] = str(resolved.warehouse)
    return sink


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
    package_spec = props.get("package")
    mode = cfg.get("mode") or ("streaming" if stream_spec is not None else "batch")
    if mode not in MODES:
        raise ValueError(f"{where}: parameters.spark.mode: expected one of {list(MODES)}")
    if sql is not None and mapping is not None:
        raise ValueError(f"{where}: declare properties.mapping or properties.sql, not both")
    if package_spec is not None:
        _check_package_build(props, mode, where)
    elif mode == "streaming" and stream_spec is None:
        raise ValueError(f"{where}: a streaming job reads the records properties.stream declares")
    if mode == "batch" and package_spec is None and (stream_spec is not None or sql is None):
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
    local = cfg.get("runtime") == LOCAL_RUNTIME
    if not local and bucket_uses_fallback(loc) and not account_ref:
        raise ValueError(
            f"{where}: the output names no bucket, so its warehouse is the account's "
            "<account>-fluid-data bucket; pass the account"
        )
    resolved = resolve_iceberg_catalog(binding, account_ref=account_ref)
    _check_catalog(local, binding, catalog_kind_info(resolved.kind).family, where)
    parts = resolved.fq_table.split(".")
    if len(parts) != 2 or "None" in parts:
        raise ValueError(f"{where}: the output needs one database and table")
    database, table = (validate_ident(p) for p in parts)
    fields = parse_fields(
        _object(expose.get("contract"), f"exposes[{index}].contract").get("schema"),
        path=f"exposes[{index}].contract.schema",
    )
    # A local output names its bucket (the S3 store its checkpoint lives in) or none.
    bucket: Optional[str] = (
        None
        if local and bucket_uses_fallback(loc)
        else normalize_location(loc, account_ref=account_ref)[0]
    )

    source: Optional[dict[str, Any]] = None
    source_sql: Optional[str] = None
    dead_letters: Optional[dict[str, Any]] = None
    streaming: dict[str, Any] = {"trigger": None, "watermark": None, "checkpoint": None}
    masked: list[str] = []
    if stream_spec is not None:
        if not isinstance(stream_spec, Mapping):
            raise ValueError(f"{where}: properties.stream: expected an object")
        swhere = f"{where}: properties.stream"
        records = {k: v for k, v in stream_spec.items() if k not in _ENGINE_KEYS}
        stream = parse_stream(records, swhere)
        kafka = _kafka(cfg, where)
        _check_local_source(local, kafka, where)
        registry = _registry(
            stream_spec.get("registry"), stream, swhere, environ, granted=host_gate
        )
        _refuse_unless_hosts_allowed(
            _source_hosts(kafka, registry and registry["url"]), environ, listed=host_gate
        )
        if kafka["mskClusterArn"] and host_gate:
            _refuse_unless_cluster_listed(product, bid, kafka["mskClusterArn"], environ)
        offsets = stream_spec.get("startingOffsets", "latest")
        if offsets not in ("earliest", "latest"):
            raise ValueError(f"{swhere}.startingOffsets: expected earliest or latest")
        rate_limit = _max_offsets(stream_spec.get("maxOffsetsPerTrigger"), swhere)
        dead = _parsed_dead_letters(stream_spec, stream, swhere)
        if dead is not None and dead["sink"] == "topic" and host_gate:
            _refuse_unless_dead_letter_topic_listed(bid, kafka, dead["topic"], environ)
        decoders = _decoders(stream, registry, dead_letters=dead is not None)
        source = {
            "kind": "kafka",
            **kafka,
            "topic": stream.topic,
            "startingOffsets": offsets,
            **rate_limit,
            "includeHeaders": any(src == "headers" for _, src in stream.metadata),
            "registry": (
                None if registry is None else {"url": registry["url"], "auth": registry["auth"]}
            ),
            **decoders,
        }
        source_sql = spark_source_select(stream, swhere)
        streaming = {
            "trigger": _trigger(stream_spec.get("trigger"), expose, swhere),
            "watermark": _watermark(stream_spec.get("watermark"), stream, swhere),
            "checkpoint": _checkpoint(stream_spec.get("checkpoint"), bucket, product, bid, swhere),
        }
        if local and stream_spec.get("checkpoint") is not None:
            _check_local_checkpoint(streaming["checkpoint"], bucket, product, swhere)
        if dead is not None:
            dead_letters = _dead_letter_block(
                dead, database, table, streaming["checkpoint"], local, swhere
            )
            dead_letters["check"] = dead_letter.check_select(stream, decoders)
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
    queries: dict[str, Any]
    package_block: Optional[dict[str, Any]] = None
    if package_spec is not None:
        # The code the contract brings, as its own SQL is: the operator's to approve.
        package_block = _package_block(
            package_spec,
            cfg["runtime"],
            fields,
            (database, table, local),
            where,
            gate=(product, bid, environ) if contract_sql_gate else None,
        )
        queries = {"check": package_block.pop("check")}
    else:
        if sql is not None:
            if contract_sql_gate:
                refuse_unless_contract_sql_allowed(product, build, environ)
            select = bind_by_name(_contract_sql(sql, bid), fields)
        queries = {"source": source_sql, "select": select}

    config = {
        "configVersion": CONFIG_VERSION,
        "product": product,
        "build": bid,
        "mode": mode,
        "source": source,
        "sql": queries,
        **streaming,
        "sink": _sink(local, resolved, database, table, fields),
        "maskedColumns": masked,
        # The secret's ARN, never its value: the runner reads the key with the job's role.
        "maskKey": {"secretArn": mask_key} if masked else None,
    }
    if package_block is not None:
        config["package"] = package_block
    if dead_letters is not None:
        _add_dead_letters(config, dead_letters, masked=bool(masked))
    if local and host_gate:
        tables = [f"{database}.{table}", *_dead_letter_tables(config)]
        _refuse_unless_namespace_listed(
            bid, local_namespace_scope(resolved), database, tables, environ
        )
    return SparkJob(config)


def _dead_letter_tables(config: Mapping[str, Any]) -> list[str]:
    """The dead-letter tables a job writes beside its output: a stream's (``deadLetter``) or
    a package build's (``package.deadLetter``)."""
    blocks = (config.get("deadLetter"), (config.get("package") or {}).get("deadLetter"))
    return [str(b["table"]) for b in blocks if isinstance(b, Mapping) and b.get("table")]


def _parsed_dead_letters(stream_spec: Mapping[str, Any], stream: Any, where: str) -> Any:
    """``properties.stream.deadLetter``, checked against the stream it routes; None when
    the stream has none."""
    dead = dead_letter.parse(stream_spec.get("deadLetter"), where)
    if dead is None:
        return None
    _refuse_corrupt_column_name(stream, where)
    if "avro-confluent" in (stream.format, stream.key_format):
        raise ValueError(
            f"{where}.deadLetter: an Avro stream's dead letters are not supported yet: the "
            "decoder's permissive mode would also turn a schema registry outage into dead "
            "letters"
        )
    if dead["topic"] is not None and dead["topic"] == stream_spec.get("topic"):
        raise ValueError(
            f"{where}.deadLetter.topic: the stream's own topic; the stream would read its dead "
            "letters again"
        )
    return dead


def _add_dead_letters(config: dict[str, Any], block: dict[str, Any], *, masked: bool) -> None:
    """The ``deadLetter`` block and its ``sql.check``, only for a stream that has them: one
    without keeps the job config, and the hash, it had."""
    # A build that masks fields at ingest sends no raw bytes: they hold those in clear.
    block["raw"] = not masked
    config["sql"]["check"] = block.pop("check")
    config["deadLetter"] = block


def _refuse_corrupt_column_name(stream: Any, where: str) -> None:
    from fluid_build.spark.dead_letter import CORRUPT_COLUMN

    names = {name.casefold() for name, _ in stream.value}
    names |= {name[len(stream.key_prefix) :].casefold() for name, _ in stream.key}
    if CORRUPT_COLUMN in names:
        raise ValueError(
            f"{where}.deadLetter: the stream declares a field named {CORRUPT_COLUMN}, which "
            "the runner keeps a record's undecoded text in; rename it"
        )


def _dead_letter_block(
    dead: Mapping[str, Any],
    database: str,
    table: str,
    checkpoint: str,
    local: bool,
    where: str,
) -> dict[str, Any]:
    """The job config's ``deadLetter``: the sink, the abort threshold, the alerts, and the
    checkpoint of the dead letters' own query, inside the stream's."""
    block: dict[str, Any] = {
        "sink": dead["sink"],
        "checkpoint": f"{checkpoint}dead-letter/",
        "maxRecordsBeforeAbort": dead["maxRecordsBeforeAbort"],
        "alertOn": dead["alertOn"],
    }
    if dead["sink"] == "topic":
        block["topic"] = dead["topic"]
        return block
    name = dead["table"] or dead_letter.table_name(table)
    if name.casefold() == table.casefold():
        raise ValueError(f"{where}.deadLetter.table: the dead letters need a table of their own")
    block["table"] = f"{database}.{validate_ident(name)}"
    if local:
        block["create"] = dead_letter.create_statements(CATALOG, database, name)
    return block


def _check_package_build(props: Mapping[str, Any], mode: str, where: str) -> None:
    """A build with ``properties.package``: a batch PySpark job, computed by the package
    alone (:mod:`fluid_build.spark.package`)."""
    given = [k for k in ("sql", "mapping", "stream") if props.get(k) is not None]
    if given:
        raise ValueError(
            f"{where}: properties.package runs in place of properties.sql, properties.mapping "
            f"and properties.stream; this build also declares {', '.join(given)}"
        )
    if mode != "batch":
        raise ValueError(
            f"{where}: a package build is a batch job (parameters.spark.mode: batch); a "
            "streaming package is not supported yet"
        )
    if props.get("language") not in (None, "pyspark"):
        raise ValueError(f"{where}: properties.language: a package build runs pyspark")


def _package_block(
    spec: Any,
    runtime: Any,
    fields: Any,
    output: tuple[str, str, bool],
    where: str,
    *,
    gate: Optional[tuple[Any, Any, Optional[Mapping[str, str]]]],
) -> dict[str, Any]:
    """The job config's ``package``: the digest the runner checks the copy beside the job
    config against, the entry point and its arguments, the output's declared columns and
    types, and the dead letters' table; plus ``check``, the query that gives each row its
    dead-letter reason, which goes into ``sql``. Where the team published the package is not
    in it: the runner reads forge's copy only.

    ``output`` is ``(database, table, local)``; ``gate``, ``(product, build, environ)`` to
    apply the operator's package list (``spark/policy.py``), or None to leave it to the
    caller."""
    from fluid_build.spark import package as _package

    database, table, local = output
    parsed = _package.parse_package(spec, runtime, where)
    if gate is not None:
        from fluid_build.spark.policy import refuse_unless_package_allowed

        refuse_unless_package_allowed(gate[0], gate[1], parsed.uri, gate[2])
    if any(name.casefold() == dead_letter.ERROR_COLUMN for name, _ in fields):
        raise ValueError(
            f"{where}: the output declares a column named {dead_letter.ERROR_COLUMN}, which the "
            "runner keeps each row's dead-letter reason in; rename it"
        )
    dead: Optional[dict[str, Any]] = None
    if parsed.dead_letter is not None:
        name = parsed.dead_letter["table"] or dead_letter.table_name(table)
        if name.casefold() == table.casefold():
            raise ValueError(
                f"{where}: properties.package.deadLetter.table: the output's own table; the "
                "dead letters need a table of their own"
            )
        dead = {
            "table": f"{database}.{validate_ident(name)}",
            "maxRecordsBeforeAbort": parsed.dead_letter["maxRecordsBeforeAbort"],
        }
        if local:
            dead["create"] = dead_letter.create_statements(
                CATALOG, database, name, dead_letter.BATCH_COLUMNS
            )
    return {
        "sha256": parsed.sha256,
        "entryPoint": parsed.entry_point,
        "arguments": dict(parsed.arguments),
        "schema": spark_schema(fields),
        "deadLetter": dead,
        "check": dead_letter.output_check_select(fields),
    }


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
            job = compile_job_config(
                contract,
                build,
                account_ref="000000000000",
                contract_sql_gate=False,
                host_gate=False,
            )
            # Apply refuses it before upload (iac/providers/aws_ingest.py,
            # spark-config-interpolation); validate says so first.
            if "${" in job.text.replace(MSK_BROKERS_PLACEHOLDER, ""):
                errors.append(
                    f"Spark build {build.get('id')}: the job config would hold '${{', which "
                    "the runner refuses as an unresolved interpolation; take it out of the "
                    "contract (properties.sql included)"
                )
        except FluidUserError as exc:
            errors.append(f"Spark build {build.get('id')}: {exc.what}. {exc.why}")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"Spark: {exc}")
    return errors


def _local_namespace(contract: Mapping[str, Any], build: Mapping[str, Any]) -> Optional[tuple]:
    """``(scope, namespace)`` a ``runtime: local`` build writes, or None (another runtime,
    or an output :func:`validation_errors` reports)."""
    if (spark_parameters(build) or {}).get("runtime") != LOCAL_RUNTIME:
        return None
    outputs = build.get("outputs")
    targets = [
        e
        for e in contract.get("exposes") or []
        if isinstance(e, Mapping) and isinstance(outputs, list) and e.get("exposeId") in outputs
    ]
    if len(targets) != 1 or not isinstance(targets[0].get("binding"), Mapping):
        return None
    try:
        resolved = resolve_iceberg_catalog(targets[0]["binding"])
    except (ValueError, KeyError, TypeError):
        return None
    parts = str(resolved.fq_table).split(".")
    if len(parts) != 2:
        return None
    return local_namespace_scope(resolved), parts[0]


def validation_warnings(contract: Mapping[str, Any]) -> list[str]:
    """The operator entries a Spark build lacks, which ``fluid validate`` warns about.

    A registry that receives a credential is left to the contract-wide checks, which name
    its host entry (``schema_registry/policy.py``) and the credential's grant
    (``secrets/policy.py``); repeating them here would print each twice.
    """
    from fluid_build.streaming.compiler import managed_lane_enabled
    from fluid_build.streaming.contract_sql import contract_sql_warning
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
        # Apply's rule (spark/policy.py) under this environment: a listed build is quiet.
        sql_warning = contract_sql_warning(CONTRACT_SQL_ALLOWLIST_ENV, contract, build)
        if sql_warning:
            warnings.append(sql_warning)
        package = props.get("package")
        if isinstance(package, Mapping) and isinstance(package.get("uri"), str):
            from fluid_build.spark.package import PACKAGE_ALLOWLIST_ENV, listed

            if not listed(package["uri"]):
                warnings.append(
                    f"Build {build.get('id')}: not in {PACKAGE_ALLOWLIST_ENV}: "
                    f"{package['uri'].rsplit('/', 1)[0]}/; fluid apply refuses the build until "
                    "the operator lists where its package comes from"
                )
        namespace = _local_namespace(contract, build)
        if namespace is not None and not local_namespace_listed(*namespace):
            warnings.append(
                f"Build {build.get('id')}: not in {LOCAL_NAMESPACE_ALLOWLIST_ENV}: "
                f"{namespace[0]}/{namespace[1]}; the local run is refused until the operator "
                "lists it"
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
