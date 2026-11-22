# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The forge Spark runner: runs one job config that forge-cli generated (configVersion 1).

    spark-submit forge_spark_runner.py --config s3://bucket/path/job.json

The same file runs on EMR Serverless, EMR on EC2, EMR on EKS, AWS Glue 5 and a local
spark-submit (Spark 3.5). It holds no contract logic: the config (``fluid_build.spark.compiler``) carries
the queries, already checked against the contract. forge-cli never imports this file,
so forge has no pyspark dependency; it depends on pyspark and the standard library
only (plus boto3, which every AWS Spark runtime has, for an ``aws-sm://`` registry
credential).

- streaming: Kafka -> decode (JSON, or Confluent Avro through ABRiS) -> the source
  query -> watermark -> the select -> append to the Iceberg table, with the config's
  checkpoint and trigger. With ``deadLetter``, a record that does not decode or lacks a
  required field (the config's ``sql.check``) leaves the stream for a dead-letter table or
  topic, in a second query over the same decoded records.
- batch: the contract's own SELECT over catalog tables -> overwrite the partitions it
  computes. A topic is landed by a stream (``availableNow`` for a scheduled run), never
  by a batch read, which sees only what Kafka still retains.
- a release: ``fluid apply`` asks a stream it replaces to stop between two batches
  (``--fluid-stop-key``, :func:`stop_request_uri`), and a stream refuses a checkpoint that
  holds another topic's position before it reads (:func:`check_checkpoint_fits`).

Every commit the runner makes (a batch's overwrite, the stream's append, a dead-letter table's
append) carries ``fluid.*`` entries in its Iceberg snapshot summary: the product and build, the
digest of the job config as it ran, and what ``fluid apply`` passes: the product version, the
contract digest, the digest of the bundle its plan binds, the release id, the run id, and the job
and run of the scheduler that started it (``--fluid-product-version``, ``--fluid-contract-digest``,
``--fluid-bundle-digest``, ``--fluid-release-id``, ``--fluid-run-id``, ``--fluid-scheduler-job``,
``--fluid-scheduler-run-id``). The job's Spark conf holds the same as ``spark.fluid.*``. Any of
them missing is left out: a plain spark-submit runs the job too.

The submitter puts on the classpath what the job uses: the Iceberg Spark runtime and
its AWS bundle (EMR: the release's Iceberg; Glue: ``--datalake-formats iceberg``),
``spark-sql-kafka-0-10``, ``aws-msk-iam-auth`` for MSK IAM, and for Avro ``spark-avro``
with ``za.co.absa:abris_2.12:6.4.0`` (Confluent's Maven repository). A catalog the
submitter declares (``spark.sql.catalog.<catalog>``) is used as declared; otherwise the
runner derives the Glue catalog from the config, and any single setting the submitter
passes (``spark.sql.catalog.<catalog>.glue.endpoint``, say) wins. A REST-catalog sink (a
local run) is never derived: the submitter declares that catalog, credential included, and
the job creates its table with the config's statements when nothing has.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

CONFIG_VERSION = 1
SOURCE_TABLE = "source_events"
RECORDS_TABLE = "kafka_records"
#: The decoded records with their raw bytes, which ``sql.check`` reads (dead letters).
DECODED_TABLE = "kafka_records_decoded"
RAW_VALUE, RAW_KEY, ERROR_COLUMN = "fluid_raw_value", "fluid_raw_key", "fluid_error"
#: A dead letter's row, from the checked records.
DEAD_LETTER_SELECT = (
    f"`{ERROR_COLUMN}` AS `error`",
    f"`{RAW_VALUE}` AS `raw_value`",
    f"`{RAW_KEY}` AS `raw_key`",
    "`topic` AS `kafka_topic`",
    "`partition` AS `kafka_partition`",
    "`offset` AS `kafka_offset`",
    "`timestamp` AS `kafka_timestamp`",
    "current_timestamp() AS `dead_lettered_at`",
)
#: The header a topic dead letter keeps the record's own key in (its key says where it came from).
RAW_KEY_HEADER = "fluid.dead-letter.raw-key"
#: Snapshot summary keys that make a dead-letter table append happen once per batch.
_DL_QUERY_PROPERTY = "fluid.dead-letter.query"
_DL_BATCH_PROPERTY = "fluid.dead-letter.batch"
#: The flags that carry a run's identity, and the snapshot summary key each becomes on every commit
#: the runner makes. Iceberg keeps a ``snapshot-property.<key>`` write option as ``<key>`` in the
#: commit's summary, for a batch write and for a stream's, and Spark lower-cases a stream's option
#: keys, so the keys are lower case. They sit beside the ``spark.app.id`` Iceberg writes itself and
#: never take the name of one of its own (the streaming sink's query and epoch ids).
STAMP_FLAGS = (
    ("--fluid-product-version", "fluid.product-version"),
    ("--fluid-contract-digest", "fluid.contract-digest"),
    ("--fluid-bundle-digest", "fluid.bundle-digest"),
    ("--fluid-release-id", "fluid.release-id"),
    ("--fluid-run-id", "fluid.run-id"),
    # the scheduler's job and run (Airflow's DAG id and run id), beside forge's own run id
    ("--fluid-scheduler-job", "fluid.scheduler-job"),
    ("--fluid-scheduler-run-id", "fluid.scheduler-run-id"),
)
#: What a stamp may hold: an identifier, a version, a digest, or the run id a scheduler names
#: (``scheduled__2026-11-21T00:00:00+00:00``); never a space or a control character, so a value
#: cannot change what the log line or the snapshot summary shows. A value ``fluid apply`` passes
#: starts with a letter or a digit, so it is never read as an option of the command line.
_STAMP_IDENT = re.compile(r"[A-Za-z0-9._:+@/=~-]{1,256}")
_STAMP_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+@/=~-]{0,255}")
#: What ``--fluid-stop-key`` may hold: it names a file (``stop/<key>``), so never a ``/`` or a dot.
_STOP_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
#: How often a stream looks for its stop request (one request to the store each time), and how
#: often it looks at its queries once one has come.
STOP_CHECK_S = 10.0
_BETWEEN_BATCHES_S = 0.1
GLUE_CATALOG_IMPL = "org.apache.iceberg.aws.glue.GlueCatalog"
S3_FILE_IO = "org.apache.iceberg.aws.s3.S3FileIO"
#: Session settings the generated SQL relies on.
SESSION_CONF = {
    # spark.sql() would otherwise expand ${env:..} and ${spark:..} in the query text.
    "spark.sql.variable.substitute": "false",
    # Kafka headers repeat keys; the source query keeps the last, as Flink's map does.
    "spark.sql.mapKeyDedupPolicy": "LAST_WIN",
    "spark.sql.session.timeZone": "UTC",
}
_MSK_IAM = {
    "kafka.security.protocol": "SASL_SSL",
    "kafka.sasl.mechanism": "AWS_MSK_IAM",
    "kafka.sasl.jaas.config": "software.amazon.msk.auth.iam.IAMLoginModule required;",
    "kafka.sasl.client.callback.handler.class": (
        "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
    ),
}
_REGISTRY_TIMEOUT_S = 30
#: What a sink's own statements may do: create its namespace and table if missing.
_CREATE_PREFIXES = ("CREATE NAMESPACE IF NOT EXISTS ", "CREATE TABLE IF NOT EXISTS ")


class JobConfigError(ValueError):
    """The config is not one this runner runs."""


def log(event: str, **fields: object) -> None:
    """One JSON line on stdout: never a credential, never a record."""
    sys.stdout.write(json.dumps({"forge_spark_runner": event, **fields}, sort_keys=True) + "\n")
    sys.stdout.flush()


def _check_stamp(key: str, value: object, pattern: re.Pattern) -> None:
    if not pattern.fullmatch(str(value)):
        raise JobConfigError(
            f"{key}: {str(value)[:60]!r} is not an identifier, a version, a digest or a run id "
            "(letters, digits and . _ : + @ / = ~ -, at most 256 characters; a value passed on "
            "the command line starts with a letter or a digit); the job was not run"
        )


def stamp_flags(args) -> dict:
    """The ``--fluid-*`` values the command line carries, as stamps, each only when given (the
    runner also runs under a plain spark-submit). Each is checked here, before Spark starts: a
    value a stamp may not hold runs nothing.

    Raises:
        JobConfigError: a value holds a character a stamp may not.
    """
    given = {}
    for flag, key in STAMP_FLAGS:
        value = getattr(args, flag[2:].replace("-", "_"), None)
        if value:
            _check_stamp(key, value, _STAMP_VALUE)
            given[key] = value
    return given


def release_stamps(config: dict, text: str, given: dict | None = None) -> dict:
    """What identifies this run on every commit it makes: the product and build the config names,
    the digest of the job config as it ran, and the version, contract digest, release and run
    ``fluid apply`` handed over (``given``: :func:`stamp_flags`).

    Raises:
        JobConfigError: the config's product or build holds a character a stamp may not.
    """
    stamps = {
        "fluid.product": config["product"],
        "fluid.build": config["build"],
        "fluid.job-sha256": config_digest(text),
    }
    for key, value in stamps.items():
        _check_stamp(key, value, _STAMP_IDENT)
    stamps.update(given or {})
    return stamps


def stamp_conf(spark, stamps: dict) -> None:
    """The job's own conf holds the stamps as ``spark.<key>``, which the Spark UI and a listener
    read. A conf Spark refuses is logged, not fatal: the commits carry the stamps."""
    for key, value in stamps.items():
        try:
            spark.conf.set(f"spark.{key}", value)
        except Exception as exc:  # noqa: BLE001 - Py4J's error: the stamp stays on the commits
            log("stamp_conf_not_set", key=key, error=type(exc).__name__)


def stamped(writer, stamps: dict | None):
    """``writer`` (a DataFrameWriterV2 or a DataStreamWriter) with each stamp as the
    ``snapshot-property.`` write option that puts it in the commit's snapshot summary."""
    for key, value in (stamps or {}).items():
        writer = writer.option(f"snapshot-property.{key}", value)
    return writer


def parse_config(text: str) -> dict:
    """The job config, checked for what this runner needs before Spark does anything."""
    # A ${...} left in the text is an OpenTofu interpolation nobody resolved: the job
    # would connect to a broker literally named so.
    if "${" in text:
        raise JobConfigError("the job config holds an unresolved ${...} interpolation")
    config = json.loads(text)
    if not isinstance(config, dict) or config.get("configVersion") != CONFIG_VERSION:
        raise JobConfigError(f"this runner reads configVersion {CONFIG_VERSION} only")
    missing = [k for k in ("product", "build", "mode", "sql", "sink") if not config.get(k)]
    if missing:
        raise JobConfigError(f"the job config lacks {missing}")
    if config["mode"] not in ("streaming", "batch"):
        raise JobConfigError("mode must be streaming or batch")
    if config["mode"] == "streaming" and not (config.get("source") and config.get("checkpoint")):
        raise JobConfigError("a streaming job reads a source and keeps a checkpoint")
    if config["mode"] == "batch" and config.get("source"):
        raise JobConfigError("a batch job runs SQL over catalog tables and reads no topic")
    per_batch = (config.get("source") or {}).get("maxOffsetsPerTrigger")
    if per_batch is not None and (
        isinstance(per_batch, bool) or not isinstance(per_batch, int) or per_batch < 1
    ):
        raise JobConfigError("source.maxOffsetsPerTrigger must be a whole number, at least 1")
    _check_sink(config["sink"])
    if config.get("deadLetter") is not None:
        _check_dead_letters(config, config["deadLetter"])
    return config


def _check_sink(sink) -> None:
    if not isinstance(sink, dict) or sink.get("type") not in ("glue", "rest"):
        raise JobConfigError("the sink must be a glue or rest catalog table")
    _check_create(sink.get("create"), "sink")


def _check_dead_letters(config: dict, dead) -> None:
    if not isinstance(dead, dict):
        raise JobConfigError("deadLetter must be an object")
    if config["mode"] != "streaming" or not (config["sql"] or {}).get("check"):
        raise JobConfigError("deadLetter is a stream's, with the sql.check query")
    if dead.get("sink") == "table" and not dead.get("table"):
        raise JobConfigError("a deadLetter table sink names its table")
    if dead.get("sink") == "topic" and not dead.get("topic"):
        raise JobConfigError("a deadLetter topic sink names its topic")
    if dead.get("sink") not in ("table", "topic") or not dead.get("checkpoint"):
        raise JobConfigError("deadLetter needs a table or topic sink and a checkpoint")
    if isinstance(dead.get("maxRecordsBeforeAbort"), bool) or not isinstance(
        dead.get("maxRecordsBeforeAbort"), int
    ):
        raise JobConfigError("deadLetter.maxRecordsBeforeAbort must be a whole number")
    _check_create(dead.get("create"), "deadLetter")


def _check_create(create, where: str) -> None:
    create = create or []
    if not isinstance(create, list) or not all(
        isinstance(s, str) and s.startswith(_CREATE_PREFIXES) for s in create
    ):
        raise JobConfigError(f"{where}.create holds CREATE ... IF NOT EXISTS statements only")


def resolve_secret(ref: str) -> str:
    """A registry credential: ``env://NAME`` or ``aws-sm://<name-or-arn>[#key]``."""
    scheme, _, rest = ref.partition("://")
    # The canonical form: <scheme>://<locator>[#<field>][?version=<v>].
    rest, _, version = rest.partition("?version=")
    locator, _, field = rest.partition("#")
    if scheme == "env":
        value = os.environ.get(locator)
        if not value:
            raise JobConfigError(f"the registry credential's variable {locator} is not set")
        return value
    if scheme == "aws-sm":
        import boto3  # every AWS Spark runtime has it; imported only when used

        kwargs = {"SecretId": locator, **({"VersionId": version} if version else {})}
        value = boto3.client("secretsmanager").get_secret_value(**kwargs)["SecretString"]
        return str(json.loads(value)[field]) if field else value
    raise JobConfigError(f"this runner does not read {scheme} secret references")


def fetch_reader_schema(registry: dict, subject: str, version: object) -> str:
    """The Avro schema the registry holds for ``subject`` at ``version``, as JSON text."""
    url = (
        f"{registry['url'].rstrip('/')}/subjects/{urllib.parse.quote(subject, safe='')}"
        f"/versions/{version}"
    )
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if registry.get("auth"):
        token = base64.b64encode(resolve_secret(registry["auth"]["secretRef"]).encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    # No redirects: urllib would send the Authorization header on to the new location,
    # a host the operator never allowed the credential to reach.
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=_REGISTRY_TIMEOUT_S) as response:  # nosec B310
        return json.loads(response.read())["schema"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JobConfigError(f"the schema registry answered {code} with a redirect; refused")


def registry_conf(registry: dict) -> dict:
    conf = {"schema.registry.url": registry["url"]}
    if registry.get("auth"):
        conf["basic.auth.credentials.source"] = "USER_INFO"
        conf["basic.auth.user.info"] = resolve_secret(registry["auth"]["secretRef"])
    return conf


def kafka_options(source: dict) -> dict:
    options = {
        "kafka.bootstrap.servers": source["bootstrapServers"],
        "subscribe": source["topic"],
        "startingOffsets": source["startingOffsets"],
        "includeHeaders": "true" if source.get("includeHeaders") else "false",
        # Offsets the topic no longer holds (retention passed them while the stream was
        # stopped, or the topic was recreated) fail the stream instead of being skipped.
        # Spark's default, pinned so the runner never depends on it.
        "failOnDataLoss": "true",
    }
    if source.get("maxOffsetsPerTrigger"):
        options["maxOffsetsPerTrigger"] = str(int(source["maxOffsetsPerTrigger"]))
    if source.get("groupIdPrefix"):
        options["groupIdPrefix"] = source["groupIdPrefix"]
    if source["auth"] == "msk-iam":
        options.update(_MSK_IAM)
    elif source["auth"] != "none":
        raise JobConfigError(f"unknown Kafka auth {source['auth']!r}")
    elif source.get("tls"):
        options["kafka.security.protocol"] = "SSL"
    return options


def mask_digest(key: bytes, value) -> str | None:
    """``fluid_mask``: HMAC-SHA256 of the value's bytes under the product's key, lowercase hex."""
    if value is None:
        return None
    return hmac.new(key, bytes(value), hashlib.sha256).hexdigest()


def load_mask_key(secret_arn: str) -> bytes:
    """The product's masking key, from AWS Secrets Manager with the job's role.

    Held in memory only: never logged, printed, put in Spark conf or in an error.
    """
    import boto3  # every AWS Spark runtime has it; imported only when used

    secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    key = secret.get("SecretBinary") or str(secret.get("SecretString") or "").encode("utf-8")
    if not key:
        raise JobConfigError("the maskAtIngest key secret is empty")
    return bytes(key)


def register_mask(spark, key: bytes) -> None:
    """Register ``fluid_mask`` (binary -> string) for the generated SQL.

    The key travels to the executors inside the function, as Spark ships any UDF; it is
    in no conf, plan text or log. A pandas UDF when pyarrow is there (EMR and Glue have
    it), a row UDF otherwise.
    """
    try:
        import pandas as pd  # noqa: F401  (pandas_udf needs it)
        import pyarrow  # noqa: F401
        from pyspark.sql.functions import pandas_udf

        def digests(values):
            return values.map(lambda v: mask_digest(key, v))

        function = pandas_udf(digests, "string")
    except ImportError:
        from pyspark.sql.functions import udf

        function = udf(lambda v: mask_digest(key, v), "string")
    spark.udf.register("fluid_mask", function)


def configure_session(spark, config: dict) -> None:
    for name, value in SESSION_CONF.items():
        spark.conf.set(name, value)
    sink = config["sink"]
    prefix = f"spark.sql.catalog.{sink['catalog']}"
    if sink["type"] == "rest":
        _check_declared_catalog(spark, sink, prefix)
    else:
        _derive_glue_catalog(spark, sink, prefix)
    spark.conf.set("spark.sql.defaultCatalog", sink["catalog"])
    # forge checks nullability against the contract statically, and the source query
    # fails on a required field that arrives null; Spark's own check would refuse every
    # CASE expression as nullable.
    spark.conf.set("spark.sql.iceberg.check-nullability", "false")


def _check_declared_catalog(spark, sink: dict, prefix: str) -> None:
    """A REST-catalog sink's catalog is the submitter's, whole: its URI, warehouse and
    credential come from the Spark configuration the job is submitted with, never from the
    config, so no contract can send the operator's catalog credential elsewhere. A URI or
    warehouse the contract names must be the declared catalog's."""
    if spark.conf.get(prefix, None) is None:
        raise JobConfigError(
            f"the output's {sink.get('kind') or 'REST'} catalog is declared by the "
            f"submitter: set {prefix} (and its uri and warehouse) in the Spark configuration "
            "the job is submitted with"
        )
    for key in ("uri", "warehouse"):
        wanted = sink.get(key)
        declared = spark.conf.get(f"{prefix}.{key}", None)
        if wanted and declared and str(declared).rstrip("/") != str(wanted).rstrip("/"):
            # The submitter's value is not shown: a URI there may carry a user and password.
            raise JobConfigError(
                f"the contract's output is in the catalog whose {key} is {wanted}, and the "
                f"submitter's {prefix}.{key} names another; refused before writing"
            )


def _derive_glue_catalog(spark, sink: dict, prefix: str) -> None:
    derived = {
        prefix: "org.apache.iceberg.spark.SparkCatalog",
        f"{prefix}.catalog-impl": GLUE_CATALOG_IMPL,
        f"{prefix}.io-impl": S3_FILE_IO,
        f"{prefix}.warehouse": sink["warehouse"],
    }
    if sink.get("region"):
        derived[f"{prefix}.client.region"] = sink["region"]
    # A catalog the submitter declared is the submitter's, whole. Otherwise its single
    # settings win over the derived ones: an endpoint, a Lake Formation client, a region.
    if spark.conf.get(prefix, None) is None:
        for name, value in derived.items():
            if spark.conf.get(name, None) is None:
                spark.conf.set(name, value)


def create_sink_table(spark, config: dict) -> None:
    """Run the sink's own statements (``CREATE ... IF NOT EXISTS``, compiled by forge-cli)
    before the job writes: on a local run nothing else creates the table, nor the dead
    letters' one."""
    for statement in config["sink"].get("create") or []:
        spark.sql(statement)
    for statement in (config.get("deadLetter") or {}).get("create") or []:
        spark.sql(statement)


def _same_type(decoded, declared) -> bool:
    """Whether ``decoded`` holds ``declared`` exactly, structs by field name."""
    from pyspark.sql.types import ArrayType, MapType, StructType

    if isinstance(declared, StructType):
        if not isinstance(decoded, StructType):
            return False
        fields = {f.name: f.dataType for f in decoded.fields}
        return all(
            f.name in fields and _same_type(fields[f.name], f.dataType) for f in declared.fields
        )
    if isinstance(declared, ArrayType):
        return isinstance(decoded, ArrayType) and _same_type(
            decoded.elementType, declared.elementType
        )
    if isinstance(declared, MapType):
        return (
            isinstance(decoded, MapType)
            and _same_type(decoded.keyType, declared.keyType)
            and _same_type(decoded.valueType, declared.valueType)
        )
    return decoded == declared


def check_decoded(spark, frame, column: str, ddl: str) -> None:
    """Refuse a decoded record whose types differ from the declared ones.

    A Spark cast that loses data returns null instead of failing, so the source query's
    casts are safe only over the types the contract declares.
    """
    # PySpark has no StructType.fromDDL; an empty frame of the DDL is its public parser.
    declared = spark.createDataFrame([], ddl).schema
    decoded = frame.schema[column].dataType
    if not _same_type(decoded, declared):
        raise JobConfigError(
            f"the decoded Kafka {column} does not hold the declared fields and types: "
            f"declared {declared.simpleString()}, decoded {decoded.simpleString()}"
        )


def decode(spark, source: dict, column: str):
    """The record's ``value`` or ``key``, decoded as the config says."""
    from pyspark.sql.column import Column, _to_java_column
    from pyspark.sql.functions import col, from_json

    spec = source[column]
    if spec is None or spec["format"] == "raw":
        return col(column)
    if spec["format"] == "json":
        if spec.get("corruptRecord"):
            # Dead letters: a record that is not JSON, or holds a field of another type,
            # decodes to nulls with its text in the corrupt-record field, for sql.check.
            options = {"mode": "PERMISSIVE", "columnNameOfCorruptRecord": spec["corruptRecord"]}
            return from_json(col(column).cast("string"), spec["schema"], options)
        # FAILFAST: a record that is not JSON, or holds a field of another type, fails
        # the batch instead of landing as nulls.
        return from_json(col(column).cast("string"), spec["schema"], {"mode": "FAILFAST"})
    if spec["format"] != "avro-confluent":
        raise JobConfigError(f"unknown {column} format {spec['format']!r}")
    registry = source["registry"]
    jvm = spark.sparkContext._jvm
    abris = (
        jvm.za.co.absa.abris.config.AbrisConfig.fromConfluentAvro()
        .provideReaderSchema(fetch_reader_schema(registry, spec["subject"], spec["version"]))
        .usingSchemaRegistry(jvm.PythonUtils.toScalaMap(registry_conf(registry)))
    )
    return Column(
        jvm.za.co.absa.abris.avro.functions.from_avro(_to_java_column(col(column)), abris)
    )


def read_records(spark, config: dict, starting_offsets: str | None = None):
    """``kafka_records``: the Kafka source's columns, ``value`` and ``key`` decoded, read
    from ``starting_offsets`` (Spark's JSON form) when given, else from the config's."""
    from pyspark.sql.functions import col

    source = config["source"]
    if source.get("kind") != "kafka":
        raise JobConfigError("the source must be kafka")
    options = kafka_options(source)
    if starting_offsets is not None:
        options["startingOffsets"] = starting_offsets
    raw = spark.readStream.format("kafka").options(**options).load()
    # With dead letters, the bytes travel beside the decoded value and key.
    kept = (
        [col("value").alias(RAW_VALUE), col("key").alias(RAW_KEY)]
        if config.get("deadLetter")
        else []
    )
    columns = [
        *kept,
        decode(spark, source, "value").alias("value"),
        decode(spark, source, "key").alias("key"),
        "topic",
        "partition",
        "offset",
        "timestamp",
        "timestampType",
    ]
    if source.get("includeHeaders"):
        columns.append("headers")
    records = raw.select(*columns)
    for column in ("value", "key"):
        spec = source[column]
        # from_json returns the declared schema itself, so only Avro (the writer's schema
        # read by field name) can decode to other types.
        if spec is not None and spec["format"] == "avro-confluent":
            check_decoded(spark, records, column, spec["schema"])
    return records


def build_output(spark, config: dict):
    """The output rows: the generated queries, run over the decoded records; with dead
    letters, over those ``sql.check`` passes (:func:`dead_letters` takes the rest)."""
    queries = config["sql"]
    if config.get("source"):
        records = read_records(spark, config)
        if config.get("deadLetter"):
            records.createOrReplaceTempView(DECODED_TABLE)
            checked = spark.sql(queries["check"])
            records = checked.where(f"`{ERROR_COLUMN}` IS NULL").drop(
                ERROR_COLUMN, RAW_VALUE, RAW_KEY
            )
        records.createOrReplaceTempView(RECORDS_TABLE)
        events = spark.sql(queries["source"])
        watermark = config.get("watermark")
        if watermark:
            events = events.withWatermark(watermark["column"], watermark["delay"])
        events.createOrReplaceTempView(SOURCE_TABLE)
    # No kwargs and no args: PySpark would format the text, and map keys may hold { }.
    return spark.sql(queries["select"])


def run(spark, config: dict, stamps: dict | None = None, stop=None) -> None:
    """Run the job; ``stop`` (a :class:`StopWatch`) is a stream's stop request, if it has one."""
    configure_session(spark, config)
    stamps = stamps or {}
    stamp_conf(spark, stamps)
    sink = config["sink"]
    table = f"{sink['catalog']}.{sink['table']}"
    log(
        "start",
        product=config["product"],
        build=config["build"],
        mode=config["mode"],
        table=table,
        masked_columns=config.get("maskedColumns") or [],
        stamps=stamps,
    )
    if config.get("maskedColumns"):
        mask = config.get("maskKey") or {}
        if not mask.get("secretArn"):
            raise JobConfigError("maskAtIngest columns need maskKey.secretArn")
        register_mask(spark, load_mask_key(mask["secretArn"]))
    if config["mode"] != "batch":
        check_checkpoint_fits(spark, config)
    create_sink_table(spark, config)
    output = build_output(spark, config)
    if config["mode"] == "batch":
        # A batch run replaces the partitions it computes; a stream appends what it reads.
        stamped(
            output.writeTo(table).option("check-nullability", "false"), stamps
        ).overwritePartitions()
        log("done", table=table)
        return
    writer = stamped(
        output.writeStream.format("iceberg")
        .outputMode("append")
        .option("checkpointLocation", config["checkpoint"])
        .option("check-nullability", "false"),
        stamps,
    )
    trigger = config.get("trigger") or {}
    if trigger.get("availableNow"):
        writer = writer.trigger(availableNow=True)
    elif trigger.get("processingTime"):
        writer = writer.trigger(processingTime=trigger["processingTime"])
    dead = config.get("deadLetter")
    # Where the output resumes, read before it starts: a new dead-letter query starts there.
    seed = dead_letter_start(spark, config) if dead else None
    query = writer.toTable(table)
    log("streaming", table=table, id=str(query.id))
    queries = [query]
    if dead:
        if seed is None and not _names(spark, f"{dead['checkpoint']}offsets"):
            seed = await_initial_offsets(spark, config["checkpoint"], query)
        queries.append(start_dead_letters(spark, config, trigger, seed, stamps))
    await_queries(spark, queries, stop)
    log("done", table=table)


def await_queries(spark, queries, stop=None) -> None:
    """Wait for every query; when one fails, stop the others and raise its error (an
    availableNow run ends when all have ended). With ``stop`` (a :class:`StopWatch`), a stop
    request ends the run once each query is between batches (:func:`stop_between_batches`)."""
    try:
        while any(q.isActive for q in queries):
            if stop is not None and stop.requested():
                stop_between_batches(queries)
                break
            if stop is None:
                spark.streams.awaitAnyTermination()
            elif not spark.streams.awaitAnyTermination(stop.every):
                continue  # nothing ended: look for the request again
            spark.streams.resetTerminated()
        for q in queries:
            q.awaitTermination()  # raises the error of one that failed after the reset
    except BaseException:
        for q in queries:
            if q.isActive:
                q.stop()
        raise


class DeadLetterAbort(RuntimeError):
    """More dead letters than ``maxRecordsBeforeAbort`` since the stream started."""


class DeadLetters:
    """The dead letters' ``foreachBatch``: land each micro-batch's in its sink, count them,
    and fail the stream past the threshold, once that batch has landed."""

    def __init__(self, spark, config: dict, stamps: dict | None = None) -> None:
        self.spark, self.config, self.stamps = spark, config, stamps
        self.spec = config["deadLetter"]
        self.total = 0
        self._query_id: str | None = None

    def query_id(self) -> str:
        """This query's id, from its checkpoint (Spark writes ``metadata`` when the query
        starts): the same across restarts, a new one when the checkpoint is replaced, so
        a batch id is only ever matched with this checkpoint's own batches."""
        if self._query_id is None:
            text = _read_small(self.spark, f"{self.spec['checkpoint']}metadata")
            self._query_id = str(json.loads(text)["id"])
        return self._query_id

    def __call__(self, batch, batch_id: int) -> None:
        batch = batch.persist()
        try:
            count = batch.count()
            if count:
                if self.spec["sink"] == "table":
                    self.to_table(batch, batch_id)
                else:
                    self.to_topic(batch)
                self.total += count
                log("dead_letters", batch=batch_id, records=count, total=self.total)
                if "schema_violation" in (self.spec.get("alertOn") or []):
                    log("dead_letter_alert", reason="schema_violation", records=count)
        finally:
            batch.unpersist()
        cap = self.spec["maxRecordsBeforeAbort"]
        if self.total > cap:
            raise DeadLetterAbort(
                f"{self.total} records went to the dead letters since the stream started, "
                f"past maxRecordsBeforeAbort {cap}; the stream is stopped"
            )

    def to_table(self, batch, batch_id: int) -> None:
        """Append the batch once: a batch replayed after a failure finds its own snapshot,
        which carries this query's id and the batch id (as Iceberg's streaming sink keeps
        the query id and epoch of what it committed)."""
        from pyspark.sql.functions import col

        table = f"{self.config['sink']['catalog']}.{self.spec['table']}"
        summary = col("summary")
        query = self.query_id()
        done = (
            self.spark.table(f"{table}.snapshots")
            .where(
                (summary[_DL_QUERY_PROPERTY] == query)
                & (summary[_DL_BATCH_PROPERTY] == str(int(batch_id)))
            )
            .limit(1)
            .collect()
        )
        if done:
            return
        stamped(
            batch.writeTo(table)
            .option(f"snapshot-property.{_DL_QUERY_PROPERTY}", query)
            .option(f"snapshot-property.{_DL_BATCH_PROPERTY}", str(int(batch_id)))
            .option("check-nullability", "false"),
            self.stamps,
        ).append()

    def to_topic(self, batch) -> None:
        """Send the batch, at least once, with Kafka Connect's error-context headers.

        Each dead letter is keyed by where it came from, ``<topic>-<partition>@<offset>``, and
        keeps its own key in the ``fluid.dead-letter.raw-key`` header: a batch sent again
        after a failure (the Kafka sink is not transactional) carries the same keys, so a
        consumer drops repeats by key, and a compacted topic keeps one of each."""
        from pyspark.sql.functions import array, col, format_string, lit, struct, when

        def header(name, value):
            return struct(
                lit(name).alias("key"), value.cast("string").cast("binary").alias("value")
            )

        stage = (
            when(col("error").startswith("the value"), lit("VALUE_CONVERTER"))
            .when(col("error").startswith("the key"), lit("KEY_CONVERTER"))
            .otherwise(lit("TRANSFORMATION"))
        )
        connector = f"{self.config['product']}/{self.config['build']}"
        headers = array(
            header("__connect.errors.topic", col("kafka_topic")),
            header("__connect.errors.partition", col("kafka_partition")),
            header("__connect.errors.offset", col("kafka_offset")),
            header("__connect.errors.connector.name", lit(connector)),
            header("__connect.errors.stage", stage),
            header("__connect.errors.exception.message", col("error")),
            struct(lit(RAW_KEY_HEADER).alias("key"), col("raw_key").alias("value")),
        )
        source = format_string(
            "%s-%d@%d", col("kafka_topic"), col("kafka_partition"), col("kafka_offset")
        )
        options = {
            k: v for k, v in kafka_options(self.config["source"]).items() if k.startswith("kafka.")
        }
        (
            batch.select(
                source.cast("binary").alias("key"),
                col("raw_value").alias("value"),
                headers.alias("headers"),
            )
            .write.format("kafka")
            .options(**options)
            .option("topic", self.spec["topic"])
            .save()
        )


def start_dead_letters(
    spark, config: dict, trigger: dict, starting_offsets: str | None, stamps: dict | None = None
):
    """The dead letters' own query: the records ``sql.check`` refuses, read by a source of
    its own (from ``starting_offsets`` when its checkpoint is new), with a checkpoint
    inside the stream's and the stream's trigger; a table's commits carry the run's ``stamps``."""
    spec, queries = config["deadLetter"], config["sql"]
    read_records(spark, config, starting_offsets).createOrReplaceTempView(DECODED_TABLE)
    checked = spark.sql(queries["check"])
    rows = checked.where(f"`{ERROR_COLUMN}` IS NOT NULL")
    select = list(DEAD_LETTER_SELECT)
    if spec.get("raw") is False:
        # The build masks fields at ingest: a raw record would hold them in clear.
        select[1:3] = ["CAST(NULL AS BINARY) AS `raw_value`", "CAST(NULL AS BINARY) AS `raw_key`"]
    writer = (
        rows.selectExpr(*select)
        .writeStream.foreachBatch(DeadLetters(spark, config, stamps))
        .option("checkpointLocation", spec["checkpoint"])
    )
    if trigger.get("availableNow"):
        writer = writer.trigger(availableNow=True)
    elif trigger.get("processingTime"):
        writer = writer.trigger(processingTime=trigger["processingTime"])
    query = writer.start()
    target = spec.get("table") or spec.get("topic")
    log(
        "dead_letters_streaming",
        sink=spec["sink"],
        to=target,
        id=str(query.id),
        startingOffsets=starting_offsets or "its checkpoint, or the config's",
    )
    return query


# --- where a new dead-letter query starts --------------------------------------------
#
# The output query resumes from its own checkpoint. A dead-letter query whose checkpoint
# is new (dead letters added to a stream that runs, perhaps one stopped by a record it
# could not read) must start where the output does, or a record the output now refuses
# and its dead letters never read is lost. Spark keeps a query's offsets in its
# checkpoint: ``offsets/<n>`` (``v1``, the batch's metadata, then one line per source:
# the offsets the batch ended at) for each batch, ``commits/<n>`` once it is done, and
# ``sources/0/0`` (a NUL byte, ``v1``, the offsets the source started at).


def _names(spark, directory: str) -> list:
    """The names in ``directory``, on any file system Spark reads; none when it is missing."""
    jvm = spark.sparkContext._jvm
    path = jvm.org.apache.hadoop.fs.Path(directory)
    fs = path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration())
    if not fs.exists(path):
        return []
    return [status.getPath().getName() for status in fs.listStatus(path)]


def _read_small(spark, path: str) -> str:
    jvm = spark.sparkContext._jvm
    hadoop_path = jvm.org.apache.hadoop.fs.Path(path)
    stream = hadoop_path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration()).open(
        hadoop_path
    )
    try:
        return bytes(jvm.org.apache.commons.io.IOUtils.toByteArray(stream)).decode("utf-8")
    finally:
        stream.close()


def source_offsets(text: str, line: int) -> str | None:
    """The Kafka offsets on ``line`` of a checkpoint file, as ``startingOffsets`` takes them
    (``{"<topic>": {"<partition>": <offset>}}``), or None."""
    lines = text.lstrip("\x00").splitlines()
    try:
        offsets = json.loads(lines[line])
    except (IndexError, ValueError):
        return None
    if not isinstance(offsets, dict) or not offsets:
        return None
    if not all(
        isinstance(parts, dict) and all(isinstance(o, int) for o in parts.values())
        for parts in offsets.values()
    ):
        return None
    return json.dumps(offsets, sort_keys=True)


def dead_letter_start(spark, config: dict) -> str | None:
    """Where the output query starts, read before it does: its last committed batch's end,
    or where its source started when no batch is committed. None when the dead letters'
    checkpoint exists (Spark resumes it) or the output has never run."""
    if _names(spark, f"{config['deadLetter']['checkpoint']}offsets"):
        return None
    checkpoint = config["checkpoint"]
    done = [int(n) for n in _names(spark, f"{checkpoint}commits") if n.isdigit()]
    if done:
        found = source_offsets(_read_small(spark, f"{checkpoint}offsets/{max(done)}"), 2)
    elif "0" in _names(spark, f"{checkpoint}sources/0"):
        found = source_offsets(_read_small(spark, f"{checkpoint}sources/0/0"), 1)
    else:
        return None
    return with_every_partition(found, topic_partitions(spark, config)) if found else None


def with_every_partition(offsets: str, partitions: list | None) -> str:
    """``offsets`` with each partition of the topic it lacks (one added since) at its start
    (``-2``), as the output query reads a partition it discovers: Spark refuses specific
    starting offsets that do not name every partition. Unchanged when they are unknown."""
    if partitions is None:
        return offsets
    parsed = json.loads(offsets)
    for topic, parts in parsed.items():
        for partition in partitions:
            parts.setdefault(str(partition), -2)
    return json.dumps(parsed, sort_keys=True)


def topic_partitions(spark, config: dict) -> list | None:
    """The source topic's partitions, from Kafka with the job's own connection settings;
    None when they cannot be read (Spark then reports what it lacks)."""
    try:
        jvm = spark.sparkContext._jvm
        props = jvm.java.util.Properties()
        for key, value in kafka_options(config["source"]).items():
            if key.startswith("kafka."):
                props.setProperty(key[len("kafka.") :], str(value))
        deserializer = "org.apache.kafka.common.serialization.ByteArrayDeserializer"
        props.setProperty("key.deserializer", deserializer)
        props.setProperty("value.deserializer", deserializer)
        consumer = jvm.org.apache.kafka.clients.consumer.KafkaConsumer(props)
        try:
            found = consumer.partitionsFor(config["source"]["topic"])
            return sorted(int(info.partition()) for info in found) if found else None
        finally:
            consumer.close()
    except Exception as exc:  # noqa: BLE001 - Py4J's error: logged, the seed stays as read
        log("dead_letters_partitions_unknown", error=type(exc).__name__)
        return None


def await_initial_offsets(spark, checkpoint: str, query, timeout: float = 300.0) -> str | None:
    """A new output query's starting offsets, once its source has written them (as it plans
    its first batch). None if it ends or the time passes first: the dead letters then
    start from the config's startingOffsets."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "0" in _names(spark, f"{checkpoint}sources/0"):
            return source_offsets(_read_small(spark, f"{checkpoint}sources/0/0"), 1)
        if not query.isActive:
            break
        time.sleep(0.5)
    log("dead_letters_start_unknown", checkpoint=checkpoint)
    return None


# --- stream releases -----------------------------------------------------------------
#
# fluid apply replaces a running stream whose config changed: it asks the old run to stop
# between two batches, then starts the new one on the same checkpoint. The request is an
# object at ``stop/<key>`` beside the job config the run read, ``<key>`` being the one
# ``--fluid-stop-key`` gave this run when it was started, so a request for one run never
# stops another (a later run of the same config included). Spark's ``query.stop()``
# interrupts a batch that is running (it cancels the query's jobs) and Spark has no stop
# that waits for one, so the runner waits for the gap between batches itself, as Apache
# Flink's stop-with-savepoint ends a job at a consistent point. A batch interrupted anyway
# (one that started as the last committed) is planned again from the offset log by the next
# run, as after any stop. A run that never answers is cancelled by fluid apply.


def stop_key_flag(args) -> str | None:
    """``--fluid-stop-key``, checked before Spark starts; None when it is not given.

    Raises:
        JobConfigError: the key could name a file outside ``stop/``."""
    key = getattr(args, "fluid_stop_key", None)
    if not key:
        return None
    if not _STOP_KEY.fullmatch(key):
        raise JobConfigError(
            f"--fluid-stop-key: {key[:60]!r} is not a stop key (a letter or a digit, then at "
            "most 63 letters, digits, _ or -); the job was not run"
        )
    return key


def stop_request_uri(config_uri: str, key: str) -> str:
    """Where fluid apply asks the run started with ``key`` to stop: ``stop/<key>`` beside the
    job config it read. ``fluid_build/build_runners/spark/_adapters.py`` has the same function."""
    return f"{config_uri.rsplit('/', 1)[0]}/stop/{key}"


def _exists(spark, uri: str) -> bool:
    """Whether ``uri`` names a file: on this machine (a path, ``file://``), or on any file
    system Spark reads."""
    if "://" not in uri or uri.startswith("file://"):
        return os.path.isfile(uri.removeprefix("file://"))
    jvm = spark.sparkContext._jvm
    path = jvm.org.apache.hadoop.fs.Path(uri)
    return bool(path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration()).exists(path))


class StopWatch:
    """Whether fluid apply has asked this run to stop (:func:`stop_request_uri`), looked for
    at most every ``every`` seconds: each look is a request to the store. A look that fails
    (a role that may not list the job's prefix) is logged once, and the stream goes on."""

    def __init__(self, spark, uri: str, every: float = STOP_CHECK_S, now=time.monotonic) -> None:
        self.spark, self.uri, self.every, self._now = spark, uri, every, now
        self._next: float | None = None
        self._failed = False

    def requested(self) -> bool:
        now = self._now()
        if self._next is not None and now < self._next:
            return False
        self._next = now + self.every
        try:
            return _exists(self.spark, self.uri)
        except Exception as exc:  # noqa: BLE001 - Py4J's error: the stream keeps going
            if not self._failed:
                self._failed = True
                log("stop_check_failed", uri=self.uri, error=type(exc).__name__)
            return False


def _last_batch(query):
    """The batch id of the query's last progress report, or None before its first."""
    progress = query.lastProgress
    return progress.get("batchId") if progress else None


def stop_between_batches(queries, sleep=time.sleep) -> dict:
    """Stop each of ``queries`` once it is between batches: no trigger is running in it, or a
    batch has committed since the request came (with no trigger interval the next batch
    starts as the last one commits, so it is stopped as it starts). Each query's last
    reported batch id, by query id, logged and returned."""
    started = {str(q.id): _last_batch(q) for q in queries}
    going = [q for q in queries if q.isActive]
    while going:
        for query in list(going):
            if (
                query.isActive
                and query.status.get("isTriggerActive")
                and _last_batch(query) == started[str(query.id)]
            ):
                continue
            query.stop()
            going.remove(query)
        if going:
            sleep(_BETWEEN_BATCHES_S)
    last = {str(q.id): _last_batch(q) for q in queries}
    log("stopped_on_request", lastBatch=last)
    return last


def checkpoint_topics(spark, checkpoint: str) -> set | None:
    """The topics the newest batch in ``checkpoint``'s offset log read; None for a new
    checkpoint, or an entry this runner cannot read (Spark has the last word on it)."""
    batches = [int(n) for n in _names(spark, f"{checkpoint}offsets") if n.isdigit()]
    if not batches:
        return None
    found = source_offsets(_read_small(spark, f"{checkpoint}offsets/{max(batches)}"), 2)
    return set(json.loads(found)) if found else None


def check_checkpoint_fits(spark, config: dict) -> None:
    """Refuse, before the stream reads anything, a checkpoint that holds another topic's
    position: Spark would resume this job at offsets that belong to that topic. The stream's
    checkpoint and its dead letters' are checked; a new checkpoint fits any job.

    fluid apply refuses a new source on the old checkpoint before it stops the run reading
    it, from the runs' history; this check also covers a checkpoint no known run of the build
    used (a prefix copied from another stream, a new EMR cluster with no step history).

    Raises:
        JobConfigError: a checkpoint holds another topic's position."""
    topic = config["source"]["topic"]
    places = [config["checkpoint"]]
    if config.get("deadLetter"):
        places.append(config["deadLetter"]["checkpoint"])
    for checkpoint in places:
        topics = checkpoint_topics(spark, checkpoint)
        if topics is not None and topics != {topic}:
            raise JobConfigError(
                f"the checkpoint at {checkpoint} holds the position of topic "
                f"{', '.join(sorted(topics))}, and this job reads {topic}; nothing was read. "
                "To read the new topic, set properties.stream.checkpoint to a new prefix: the "
                "job then starts from properties.stream.startingOffsets"
            )


def read_text(spark, path: str) -> str:
    """The config file's text, from any file system Spark reads (s3://, file://, a path)."""
    if "://" not in path or path.startswith("file://"):
        with open(path.removeprefix("file://"), encoding="utf-8") as handle:
            return handle.read()
    return spark.read.text(path, wholetext=True).first()[0]


def main(argv: list[str] | None = None) -> int:
    # allow_abbrev=False: Glue's own --conf would otherwise be read as --config.
    parser = argparse.ArgumentParser(
        description="Run one forge Spark job config.", allow_abbrev=False
    )
    parser.add_argument("--config", required=True, help="the job.json, a path or s3:// URI")
    parser.add_argument(
        "--fluid-job-sha256", help="the sha256 of the job config fluid apply started the run on"
    )
    for flag, key in STAMP_FLAGS:
        parser.add_argument(flag, help=f"stamped on every commit as {key}")
    parser.add_argument(
        "--fluid-stop-key", help="a stream's key: fluid apply asks it to stop at stop/<key>"
    )
    # Glue passes its own --JOB_NAME and friends; they are not this runner's.
    args, _ = parser.parse_known_args(argv)
    flags = stamp_flags(args)  # before Spark starts: a value a stamp may not hold runs nothing
    key = stop_key_flag(args)  # likewise
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    text = checked_text(read_text(spark, args.config), args.fluid_job_sha256, args.config)
    config = parse_config(text)
    stop = StopWatch(spark, stop_request_uri(args.config, key)) if key else None
    run(spark, config, release_stamps(config, text, flags), stop)
    return 0


def checked_text(text: str, expected: str | None, where: str) -> str:
    """The job config's text, refused before the job reads anything unless it is the one
    ``fluid apply`` started the run on (``--fluid-job-sha256``, which every runtime passes;
    :func:`config_digest`): a config another apply put there since would run under this
    run's hash."""
    if expected is None:
        return text
    try:
        found = config_digest(text)
    except ValueError:
        raise JobConfigError(f"the job config at {where} is not a JSON object") from None
    if found != expected.lower():
        raise JobConfigError(
            f"the job config at {where} is not the one fluid apply started this run on (its "
            f"digest is {found}; the run's is {expected}); nothing was read. Run fluid apply "
            "again: it starts a run on the config it uploads"
        )
    return text


def config_digest(text: str) -> str:
    """The sha256 a run's job config is started with (``--fluid-job-sha256``) and checked
    by: its JSON in one canonical form (keys sorted, strings in NFC, as OpenTofu writes
    them), the MSK brokers OpenTofu fills in for an ``mskClusterArn`` source left out, so a
    broker list MSK changes does not stop a stream's retries. The same function is in
    ``fluid_build/build_runners/spark/_desired.py``.

    Raises:
        ValueError: the text is not a JSON object.
    """
    config = _nfc(json.loads(text))
    if not isinstance(config, dict):
        raise ValueError("the job config is not a JSON object")
    source = config.get("source")
    if isinstance(source, dict) and source.get("mskClusterArn"):
        source["bootstrapServers"] = None
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _nfc(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    return value


if __name__ == "__main__":
    sys.exit(main())
