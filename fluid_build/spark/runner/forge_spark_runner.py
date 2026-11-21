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
- a prebuilt package (``package``): the team's own PySpark, by digest. The runner imports
  the pinned bytes, calls the entry point, checks the DataFrame it returns, and writes it
  as a batch run does; the package computes rows and never writes (:func:`run_package`).

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
import functools
import hashlib
import hmac
import importlib
import inspect
import json
import os
import re
import sys
import tempfile
import time
import types
import unicodedata
import urllib.parse
import urllib.request
import zipfile

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
    if config.get("package") is not None:
        _check_package(config)
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


def run(spark, config: dict) -> None:
    configure_session(spark, config)
    sink = config["sink"]
    table = f"{sink['catalog']}.{sink['table']}"
    log(
        "start",
        product=config["product"],
        build=config["build"],
        mode=config["mode"],
        table=table,
        masked_columns=config.get("maskedColumns") or [],
    )
    if config.get("maskedColumns"):
        mask = config.get("maskKey") or {}
        if not mask.get("secretArn"):
            raise JobConfigError("maskAtIngest columns need maskKey.secretArn")
        register_mask(spark, load_mask_key(mask["secretArn"]))
    create_sink_table(spark, config)
    output = build_output(spark, config)
    if config["mode"] == "batch":
        # A batch run replaces the partitions it computes; a stream appends what it reads.
        output.writeTo(table).option("check-nullability", "false").overwritePartitions()
        log("done", table=table)
        return
    writer = (
        output.writeStream.format("iceberg")
        .outputMode("append")
        .option("checkpointLocation", config["checkpoint"])
        .option("check-nullability", "false")
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
        queries.append(start_dead_letters(spark, config, trigger, seed))
    await_queries(spark, queries)
    log("done", table=table)


def await_queries(spark, queries) -> None:
    """Wait for every query; when one fails, stop the others and raise its error (an
    availableNow run ends when all have ended)."""
    try:
        while any(q.isActive for q in queries):
            spark.streams.awaitAnyTermination()
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

    def __init__(self, spark, config: dict) -> None:
        self.spark, self.config = spark, config
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
        (
            batch.writeTo(table)
            .option(f"snapshot-property.{_DL_QUERY_PROPERTY}", query)
            .option(f"snapshot-property.{_DL_BATCH_PROPERTY}", str(int(batch_id)))
            .option("check-nullability", "false")
            .append()
        )

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


def start_dead_letters(spark, config: dict, trigger: dict, starting_offsets: str | None):
    """The dead letters' own query: the records ``sql.check`` refuses, read by a source of
    its own (from ``starting_offsets`` when its checkpoint is new), with a checkpoint
    inside the stream's and the stream's trigger."""
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
        .writeStream.foreachBatch(DeadLetters(spark, config))
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
    # Glue passes its own --JOB_NAME and friends; they are not this runner's.
    args, _ = parser.parse_known_args(argv)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    text = checked_text(read_text(spark, args.config), args.fluid_job_sha256, args.config)
    config = parse_config(text)
    if config.get("package"):
        # The package's copy sits beside the job config: found from where it was read.
        run_package(spark, config, args.config)
    else:
        run(spark, config)
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


# --- prebuilt packages ------------------------------------------------------------------
#
# A build with ``package`` runs the team's own PySpark, by digest. fluid apply puts the
# package beside the job config, keyed by its sha256 (fluid_build/spark/names.py::
# package_key). The runner reads that copy, refuses any bytes but the pinned ones, imports
# the entry point from its own copy of them, calls it as fn(spark, context), and writes the
# DataFrame it returns: the columns and types must be the contract's, a row whose required
# value is null goes to the dead letters (``sql.check`` says why), and the rest replace
# the partitions they hold. While the function runs, PySpark's writes are refused and
# spark.sql runs reads only (WriteGuard); a commit to the output or its dead letters that
# the runner did not make fails the run (table_marker). Code in the same process can
# reach the JVM itself, so these stop a write the package makes through PySpark and catch
# one it makes around it; the job's role is what keeps it from other tables.

#: Where fluid apply puts a package beside the job config (``names.PACKAGE_DIR`` and
#: ``names.PACKAGE_FILE``): a ``.zip`` name, as PySpark puts only a ``.zip`` on the path.
PACKAGE_DIR, PACKAGE_FILE = "package", "package.zip"
#: The largest package the runner reads (``fluid_build/spark/package.py``).
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
#: The view the package's output is checked under: ``sql.check`` reads it.
PACKAGE_VIEW = "package_output"
#: Snapshot summary keys: the package whose rows a commit holds, and the run whose dead
#: letters a dead-letter commit holds, so a re-attempt of the run lands them once.
PACKAGE_SHA_PROPERTY = "fluid.package.sha256"
_PACKAGE_RUN_PROPERTY = "fluid.dead-letter.run"
#: Compiled extension modules, which Python never imports from a zip.
_EXTENSION_SUFFIXES = (".so", ".pyd", ".dylib")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ENTRY_POINT = re.compile(rf"{_NAME}(?:\.{_NAME})*:{_NAME}(?:\.{_NAME})*")
_CHUNK = 1024 * 1024


class PackageError(RuntimeError):
    """The package, or what its entry point did or returned, is refused: the runner writes
    nothing to the output."""


def _check_package(config: dict) -> None:
    package = config["package"]
    if not isinstance(package, dict):
        raise JobConfigError("package must be an object")
    if config["mode"] != "batch" or config.get("source"):
        raise JobConfigError("a package runs in a batch job, which reads no topic")
    if config.get("deadLetter") is not None:
        raise JobConfigError("a package build's dead letters are package.deadLetter")
    if not isinstance((config["sql"] or {}).get("check"), str):
        raise JobConfigError("a package build carries sql.check, the query its rows are checked by")
    _check_package_fields(package)
    if package.get("deadLetter") is not None:
        _check_package_dead_letters(package["deadLetter"])


def _check_package_fields(package: dict) -> None:
    if not (isinstance(package.get("sha256"), str) and _SHA256.fullmatch(package["sha256"])):
        raise JobConfigError("package.sha256 must be 64 lowercase hex characters")
    entry = package.get("entryPoint")
    if not (isinstance(entry, str) and _ENTRY_POINT.fullmatch(entry)):
        raise JobConfigError("package.entryPoint must be <module>:<attribute>")
    arguments = package.get("arguments")
    if not isinstance(arguments, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in arguments.items()
    ):
        raise JobConfigError("package.arguments must map names to strings")
    if not (isinstance(package.get("schema"), str) and package["schema"]):
        raise JobConfigError("package.schema must hold the output's declared columns")


def _check_package_dead_letters(dead) -> None:
    cap = dead.get("maxRecordsBeforeAbort") if isinstance(dead, dict) else None
    if (
        not isinstance(dead, dict)
        or not (isinstance(dead.get("table"), str) and dead["table"])
        or isinstance(cap, bool)
        or not isinstance(cap, int)
        or cap < 0
    ):
        raise JobConfigError(
            "package.deadLetter names its table and a whole maxRecordsBeforeAbort from 0"
        )
    _check_create(dead.get("create"), "package.deadLetter")


def package_location(config_uri: str, sha256: str) -> str:
    """The package's copy beside the job config read from ``config_uri`` (an ``s3://`` or
    ``file://`` URI, or a path), as fluid apply puts it."""
    base, sep, _ = config_uri.rpartition("/")
    return f"{base if sep else '.'}/{PACKAGE_DIR}/{sha256[:16]}/{PACKAGE_FILE}"


def fetch_package(spark, location: str, sha256: str) -> str:
    """A copy of the package at ``location`` of the runner's own (owner-only, in a new
    directory nobody else can enter), once its bytes are the pinned ones and it is a zip
    Python can import from; its path."""
    target = os.path.join(tempfile.mkdtemp(prefix="fluid-package-"), f"{sha256[:16]}.zip")
    if "://" not in location or location.startswith("file://"):
        _copy_local(location.removeprefix("file://"), target)
    else:
        _copy_remote(spark, location, target)
    found = _file_sha256(target)
    if found != sha256:
        os.unlink(target)
        raise PackageError(
            f"the package at {location} is not the one the job config pins (its sha256 is "
            f"{found}, the pin {sha256}); nothing was imported. Run fluid apply again: it "
            "copies the pinned package"
        )
    _check_archive(target, location)
    log("package_verified", sha256=sha256, bytes=os.path.getsize(target))
    return target


def _refuse_oversized(size: int, where: str) -> None:
    if size > MAX_PACKAGE_BYTES:
        raise PackageError(
            f"the package at {where} is larger than {MAX_PACKAGE_BYTES} bytes; keep "
            "dependencies in the runtime's image or environment"
        )


def _copy_local(source: str, target: str) -> None:
    try:
        handle = open(source, "rb")
    except OSError as exc:
        raise PackageError(
            f"the package at {source} cannot be read ({exc.strerror}); run fluid apply, which "
            "puts it beside the job config"
        ) from None
    with handle:
        _refuse_oversized(os.fstat(handle.fileno()).st_size, source)
        # Owner-only: the package is the job's alone (and O_EXCL: a name nobody placed).
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags, 0o600), "wb") as out:
            copied = 0
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                _refuse_oversized(copied, source)
                out.write(chunk)


def _copy_remote(spark, uri: str, target: str) -> None:
    """Through Hadoop's file system, with the job's own credentials (EMRFS or S3A)."""
    jvm = spark.sparkContext._jvm
    path = jvm.org.apache.hadoop.fs.Path(uri)
    fs = path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration())
    try:
        size = int(fs.getFileStatus(path).getLen())
    except Exception as exc:  # noqa: BLE001 - Py4J's error, a missing object among them
        raise PackageError(
            f"the package at {uri} cannot be read ({type(exc).__name__}); run fluid apply, "
            "which copies it beside the job config"
        ) from None
    _refuse_oversized(size, uri)
    # useRawLocalFileSystem: no .crc file beside the copy.
    fs.copyToLocalFile(False, path, jvm.org.apache.hadoop.fs.Path(target), True)
    _refuse_oversized(os.path.getsize(target), uri)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_archive(path: str, where: str) -> None:
    if not zipfile.is_zipfile(path):
        raise PackageError(f"the package at {where} is not a wheel or a zip")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    compiled = sorted(
        name
        for name in names
        if name.lower().endswith(_EXTENSION_SUFFIXES) or ".so." in name.rsplit("/", 1)[-1].lower()
    )
    if compiled:
        shown = ", ".join(ascii(name) for name in compiled[:3])
        raise PackageError(
            f"the package holds compiled extension modules ({shown}"
            f"{', ...' if len(compiled) > 3 else ''}), which Python does not import from a "
            "zip; put the dependency that has them in the runtime's image or environment, and "
            "keep the package pure Python"
        )


def _origin(module) -> str | None:
    spec = getattr(module, "__spec__", None)
    return getattr(spec, "origin", None) or getattr(module, "__file__", None)


def _inside(origin: str | None, zips) -> bool:
    """Whether a module's origin is a file inside one of ``zips`` (zipimport names it
    ``<zip>/<path in the zip>``)."""
    if not origin:
        return False
    roots = {os.path.realpath(z) for z in zips} | {os.path.abspath(z) for z in zips}
    return any(origin.startswith(root + os.sep) for root in roots)


def import_entry_point(entry_point: str, zips):
    """The function ``entry_point`` names, imported from the package: from one of ``zips``
    (the runner's copy and Spark's, which addPyFile put on the path), never from a module of
    that name already loaded or earlier on the path. Resolved as the packaging spec's
    entry-point object reference (``import_module``, then each attribute)."""
    module_name, _, qualname = entry_point.partition(":")
    parts = module_name.split(".")
    chain = [".".join(parts[: i + 1]) for i in range(len(parts))]
    _refuse_loaded_elsewhere(chain, zips)
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - the package's own import, whatever it raised
        raise PackageError(
            f"importing {module_name} from the package failed: {type(exc).__name__}: {exc}"
        ) from exc
    _refuse_imported_elsewhere(chain, zips)
    function = module
    for attribute in qualname.split("."):
        try:
            function = getattr(function, attribute)
        except AttributeError:
            raise PackageError(f"{module_name} has no {qualname}") from None
    _check_callable(function, entry_point)
    return function


def _refuse_loaded_elsewhere(chain, zips) -> None:
    """Before the import: a module of the entry point's name loaded from anywhere else."""
    for name in chain:
        loaded = sys.modules.get(name)
        if loaded is not None and not _inside(_origin(loaded), zips):
            where = _origin(loaded)
            raise PackageError(
                f"{name} was imported before the package was{f', from {where}' if where else ''}"
                "; a module of that name is on the runtime's Python path: take it out of the "
                "image, or rename the package's"
            )


def _refuse_imported_elsewhere(chain, zips) -> None:
    """After it: the entry module, and each parent with a file of its own, from the package
    (a parent without one is a namespace package, which holds no code)."""
    for name in chain:
        origin = _origin(sys.modules.get(name))
        if (origin is not None or name == chain[-1]) and not _inside(origin, zips):
            raise PackageError(
                f"{name} was imported from {origin}, not from the package: a module of that "
                "name comes first on the runtime's Python path; take it out of the image, or "
                "rename the package's"
            )


def _check_callable(function, entry_point: str) -> None:
    if not callable(function):
        raise PackageError(f"{entry_point} is not a function")
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return  # nothing to read the parameters from: the call says
    try:
        signature.bind(None, None)
    except TypeError:
        raise PackageError(
            f"{entry_point} takes {signature}; a package's entry point takes (spark, context)"
        ) from None


class PackageContext:
    """What a package's entry point gets beside the session, read-only: ``arguments`` (the
    contract's strings), ``product``, ``build``, ``output`` (the table the runner writes,
    which the function may read), ``run_id`` (Spark's application id) and
    ``package_sha256``."""

    __slots__ = ("arguments", "product", "build", "output", "run_id", "package_sha256")

    def __init__(self, **values) -> None:
        for name in self.__slots__:
            object.__setattr__(self, name, values[name])

    def __setattr__(self, name, value) -> None:
        raise AttributeError("the package context is read-only")


#: The PySpark calls that write, create a table or put more code on the cluster, refused
#: while a package's entry point runs: ``(module, class, methods)``.
_WRITE_CALLS = (
    (
        "pyspark.sql.readwriter",
        "DataFrameWriter",
        ("save", "saveAsTable", "insertInto", "json", "parquet", "orc", "text", "csv", "jdbc"),
    ),
    (
        "pyspark.sql.readwriter",
        "DataFrameWriterV2",
        ("create", "replace", "createOrReplace", "append", "overwrite", "overwritePartitions"),
    ),
    ("pyspark.sql.streaming.readwriter", "DataStreamWriter", ("start", "toTable")),
    ("pyspark.sql.dataframe", "DataFrame", ("foreach", "foreachPartition")),
    (
        "pyspark.rdd",
        "RDD",
        (
            "foreach",
            "foreachPartition",
            "saveAsTextFile",
            "saveAsPickleFile",
            "saveAsSequenceFile",
            "saveAsHadoopFile",
            "saveAsNewAPIHadoopFile",
            "saveAsHadoopDataset",
            "saveAsNewAPIHadoopDataset",
        ),
    ),
    ("pyspark.sql.catalog", "Catalog", ("createTable", "createExternalTable", "recoverPartitions")),
    ("pyspark.context", "SparkContext", ("addFile", "addPyFile", "addArchive", "addJar")),
    ("pyspark.sql.session", "SparkSession", ("addArtifact", "addArtifacts")),
)


class WriteGuard:
    """While a package's entry point runs, PySpark's writes are refused and ``spark.sql``
    runs reads only (:func:`sql_write`). What it refused stays in ``refused``: the run
    fails on it even when the package caught the error."""

    def __init__(self) -> None:
        self.refused: list = []
        self._patches: list = []

    def __enter__(self):
        missing = []
        for module_name, class_name, methods in _WRITE_CALLS:
            try:
                cls = getattr(importlib.import_module(module_name), class_name)
            except (ImportError, AttributeError):
                missing.append(f"{module_name}.{class_name}")
                continue
            for name in methods:
                if name in vars(cls):
                    self._patch(cls, name, self._refusal(f"{class_name}.{name}"))
        session = importlib.import_module("pyspark.sql.session").SparkSession
        self._patch(session, "sql", self._sql(vars(session)["sql"]))
        if missing:
            log("package_guard_partial", missing=missing)
        return self

    def __exit__(self, *exc) -> bool:
        while self._patches:
            cls, name, original = self._patches.pop()
            setattr(cls, name, original)
        return False

    def _patch(self, cls, name: str, replacement) -> None:
        self._patches.append((cls, name, vars(cls)[name]))
        setattr(cls, name, replacement)

    def _refusal(self, what: str):
        def refused(*args, **kwargs):
            self.refused.append(what)
            raise PackageError(
                f"{what} is refused while the package runs: a package returns its DataFrame, "
                "and the runner writes it"
            )

        return refused

    def _sql(self, original):
        @functools.wraps(original)
        def sql(session, sqlQuery, *args, **kwargs):
            verb = sql_write(sqlQuery)
            if verb is not None:
                self.refused.append(f"spark.sql {verb}")
                raise PackageError(
                    f"a {verb} statement is refused while the package runs: through the "
                    "session a package reads and sets up, and the runner writes the output"
                )
            return original(session, sqlQuery, *args, **kwargs)

        return sql


#: Statements that only read or set up the session.
_SQL_READS = frozenset(
    {
        "SELECT",
        "VALUES",
        "TABLE",
        "SHOW",
        "DESCRIBE",
        "DESC",
        "EXPLAIN",
        "SET",
        "RESET",
        "USE",
        "CACHE",
        "UNCACHE",
        "CLEAR",
        "REFRESH",
        "LIST",
    }
)
#: A read with a prefix (``WITH ... AS (...)``, ``FROM t``, a parenthesis) that a write can
#: follow, as in ``WITH a AS (...) INSERT INTO ...``: refused when one does, at the top level.
_SQL_PREFIXES = frozenset({"WITH", "FROM", "("})
_SQL_PREFIXED_WRITES = frozenset({"INSERT", "MERGE", "UPDATE", "DELETE"})


def sql_write(text) -> str | None:
    """None when ``text`` only reads or sets up the session (a temporary view among it), else
    what makes it a write: its verb, or why the runner cannot read it. Comments, quoted
    strings and quoted names are skipped as Spark's parser skips them; a statement that does
    not start as a read is refused, so a verb nobody listed is refused too."""
    if not isinstance(text, str):
        return None  # spark.sql refuses it itself
    try:
        tokens = _sql_tokens(text)
    except ValueError:
        return "SQL the runner cannot read (a quote or comment that does not end)"
    if not tokens:
        return "no statement"
    head = tokens[0]
    if head in _SQL_READS:
        return None
    if head == "CREATE":
        return None if _temporary_view(tokens) else "CREATE"
    if head in _SQL_PREFIXES:
        return _top_level_write(tokens)
    return head


def _temporary_view(tokens: list) -> bool:
    """``CREATE [OR REPLACE] [GLOBAL] TEMP|TEMPORARY VIEW``: a view of this session only."""
    rest = [t for t in tokens[1:6] if t not in "()"]
    at = 2 if rest[:2] == ["OR", "REPLACE"] else 0
    at += 1 if rest[at : at + 1] == ["GLOBAL"] else 0
    return rest[at : at + 1] in (["TEMP"], ["TEMPORARY"]) and rest[at + 1 : at + 2] == ["VIEW"]


def _top_level_write(tokens: list) -> str | None:
    """The write verb a read's prefix leads to, at the top level (outside parentheses)."""
    depth = 0
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and token in _SQL_PREFIXED_WRITES:
            return token
    return None


def _sql_tokens(text: str) -> list:
    """``text``'s words (upper case) and parentheses, without comments, strings and quoted
    names. Raises ValueError for a string, a quoted name or a comment that does not end."""
    tokens: list = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith(("--", "/*"), i):
            i = _end_of_comment(text, i)
        elif c in "'\"":
            i = _end_of_string(text, i, raw=False)
        elif c == "`":
            i = _end_of_name(text, i)
        elif c in "()":
            tokens.append(c)
            i += 1
        elif c.isascii() and (c.isalpha() or c == "_"):
            start = i
            while i < n and text[i].isascii() and (text[i].isalnum() or text[i] == "_"):
                i += 1
            word = text[start:i].upper()
            if word == "R" and i < n and text[i] in "'\"":
                i = _end_of_string(text, i, raw=True)  # r'...': a backslash is just itself
            else:
                tokens.append(word)
        else:
            i += 1
    return tokens


def _end_of_comment(text: str, start: int) -> int:
    """Past a ``--`` line comment, or a ``/* */`` one, which may nest as Spark's do."""
    if text.startswith("--", start):
        end = text.find("\n", start)
        return len(text) if end < 0 else end + 1
    depth, i = 1, start + 2
    while depth:
        if i >= len(text):
            raise ValueError("comment")
        if text.startswith("/*", i):
            depth, i = depth + 1, i + 2
        elif text.startswith("*/", i):
            depth, i = depth - 1, i + 2
        else:
            i += 1
    return i


def _end_of_name(text: str, start: int) -> int:
    """Past a backquoted name, where a doubled backquote is one of its own characters."""
    end = start + 1
    while True:
        end = text.find("`", end)
        if end < 0:
            raise ValueError("name")
        if not text.startswith("``", end):
            return end + 1
        end += 2


def _end_of_string(text: str, start: int, *, raw: bool) -> int:
    quote, i = text[start], start + 1
    while i < len(text):
        if text[i] == "\\" and not raw:
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    raise ValueError("string")


def _type_text(kind) -> str:
    """A ``DataType.jsonValue()`` as Spark's simpleString writes it."""
    if not isinstance(kind, dict):
        return str(kind)
    if kind.get("type") == "struct":
        fields = ",".join(f"{f['name']}:{_type_text(f['type'])}" for f in kind.get("fields", []))
        return f"struct<{fields}>"
    if kind.get("type") == "array":
        return f"array<{_type_text(kind.get('elementType'))}>"
    if kind.get("type") == "map":
        return f"map<{_type_text(kind.get('keyType'))},{_type_text(kind.get('valueType'))}>"
    return json.dumps(kind, sort_keys=True)


def _same_json_type(actual, declared) -> bool:
    """Whether ``actual`` is ``declared`` exactly (both ``DataType.jsonValue()``): structs by
    field name, arrays and maps by their element types; nullability is the row checks'."""
    if not (isinstance(actual, dict) and isinstance(declared, dict)):
        return actual == declared
    kind = declared.get("type")
    if actual.get("type") != kind:
        return False
    if kind == "struct":
        mine = [(f["name"], f["type"]) for f in actual.get("fields", [])]
        theirs = {f["name"]: f["type"] for f in declared.get("fields", [])}
        return (
            len(mine) == len(theirs)
            and {name for name, _ in mine} == set(theirs)
            and all(_same_json_type(t, theirs[name]) for name, t in mine)
        )
    if kind == "array":
        return _same_json_type(actual.get("elementType"), declared.get("elementType"))
    if kind == "map":
        return _same_json_type(actual.get("keyType"), declared.get("keyType")) and _same_json_type(
            actual.get("valueType"), declared.get("valueType")
        )
    return actual == declared


def _shown(name: str) -> str:
    return name if name.isprintable() and name.isascii() else ascii(name)


def conformance_problems(actual: dict, declared: dict) -> list:
    """What keeps a DataFrame of schema ``actual`` from being the output ``declared`` (both
    ``StructType.jsonValue()``): the same column names, exactly, in any order, each of the
    declared type; empty when it is the output."""
    names = [f["name"] for f in actual.get("fields", [])]
    have = {f["name"]: f["type"] for f in actual.get("fields", [])}
    wanted = [(f["name"], f["type"]) for f in declared.get("fields", [])]
    folded = {name.casefold() for name, _ in wanted}
    problems = [
        f"{_shown(name)} appears {names.count(name)} times"
        for name in sorted({n for n in names if names.count(n) > 1})
    ]
    for name, kind in wanted:
        if name not in have:
            close = [n for n in names if n.casefold() == name.casefold()]
            problems.append(
                f"{name} is missing"
                + (
                    f" (the package returns {_shown(close[0])}; names are case-sensitive)"
                    if close
                    else ""
                )
            )
        elif not _same_json_type(have[name], kind):
            problems.append(f"{name} is {_type_text(have[name])}, declared {_type_text(kind)}")
    problems += [
        f"{_shown(name)} is not declared"
        for name in dict.fromkeys(names)
        if name.casefold() not in folded
    ]
    return problems


def package_outcome(failed: int, dead) -> tuple:
    """``(land the dead letters, write the output)`` for a run whose output has ``failed``
    rows that fail the checks: without dead letters any such row stops the run; past
    ``maxRecordsBeforeAbort`` they land and the output is not written."""
    if not failed:
        return False, True
    if dead is None:
        return False, False
    return True, failed <= dead["maxRecordsBeforeAbort"]


def table_marker(spark, table: str) -> tuple:
    """What a commit to ``table`` changes: how many snapshots it has, the newest one's
    time, and the snapshot its main branch is at (a rollback moves only that)."""
    from pyspark.sql import functions as F

    # The catalog keeps loaded tables a while; REFRESH drops its copy, so a commit made
    # through another catalog object is seen too. The name is the compiled sink's.
    statement = f"REFRESH TABLE {table}"
    spark.sql(statement)
    snapshots = spark.table(f"{table}.snapshots")
    count, last = snapshots.agg(F.count(F.lit(1)), F.max("committed_at")).first()
    refs = spark.table(f"{table}.refs").where(F.col("name") == "main")
    main = refs.select("snapshot_id").first()
    return int(count), str(last), None if main is None else int(main[0])


def _add_package(spark, location: str, sha: str) -> list:
    """The verified bytes on the driver's Python path and handed to every executor by
    addPyFile (from the driver: no executor reads the store); the copies the entry point
    may be imported from."""
    from pyspark import SparkFiles

    path = fetch_package(spark, location, sha)
    spark.sparkContext.addPyFile(path)
    # addPyFile copies the file into Spark's own directory and puts that copy on the path.
    copy = os.path.join(SparkFiles.getRootDirectory(), os.path.basename(path))
    if os.path.exists(copy) and _file_sha256(copy) != sha:
        raise PackageError(
            "Spark's copy of the package is not the pinned one; nothing was imported"
        )
    return [path, copy]


def _conformed_columns(spark, output, ddl: str) -> list:
    """The declared columns, in the contract's order, once ``output`` is the output."""
    from pyspark.sql import DataFrame
    from pyspark.sql.functions import col

    if not isinstance(output, DataFrame):
        raise PackageError(
            f"the entry point returned {type(output).__name__}, not a pyspark DataFrame"
        )
    if output.isStreaming:
        raise PackageError(
            "the entry point returned a streaming DataFrame; a package build is batch"
        )
    # PySpark has no StructType.fromDDL; an empty frame of the DDL is its public parser.
    declared = spark.createDataFrame([], ddl).schema
    problems = conformance_problems(output.schema.jsonValue(), declared.jsonValue())
    if problems:
        raise PackageError(
            "the package's output is not the output the contract declares: "
            + "; ".join(problems)
            + ". Nothing was written"
        )
    return [col(f"`{field.name}`") for field in declared.fields]


def _reasons(failures: dict) -> str:
    return "; ".join(f"{reason}: {count}" for reason, count in sorted(failures.items()))


def write_package_dead_letters(spark, checked, table: str, columns, run_id: str, sha: str) -> None:
    """Append the rows the checks refused, once per run: a re-attempt of the run finds its
    own snapshot, which carries the run's id, as a stream's dead letters keep their batch."""
    from pyspark.sql.functions import col, current_timestamp, lit, struct, to_json

    done = (
        spark.table(f"{table}.snapshots")
        .where(col("summary")[_PACKAGE_RUN_PROPERTY] == run_id)
        .limit(1)
        .collect()
    )
    if done:
        return
    (
        checked.where(f"`{ERROR_COLUMN}` IS NOT NULL")
        .select(
            col(f"`{ERROR_COLUMN}`").alias("error"),
            to_json(struct(*columns)).alias("row_json"),
            lit(run_id).alias("run_id"),
            lit(sha).alias("package_sha256"),
            current_timestamp().alias("dead_lettered_at"),
        )
        .writeTo(table)
        .option(f"snapshot-property.{_PACKAGE_RUN_PROPERTY}", run_id)
        .option(f"snapshot-property.{PACKAGE_SHA_PROPERTY}", sha)
        .option("check-nullability", "false")
        .append()
    )


def write_package_output(checked, table: str, columns, sha: str) -> None:
    """Replace the partitions the passing rows hold, as a batch run does, stamped with the
    package's digest."""
    (
        checked.where(f"`{ERROR_COLUMN}` IS NULL")
        .select(*columns)
        .writeTo(table)
        .option("check-nullability", "false")
        .option(f"snapshot-property.{PACKAGE_SHA_PROPERTY}", sha)
        .overwritePartitions()
    )


def _not_written(failed: int, failures: dict, dead) -> str:
    if dead is None:
        return (
            f"{failed} rows of the package's output fail the contract's checks "
            f"({_reasons(failures)}) and the build has no dead letters: nothing was written. "
            "Fix the package, or add properties.package.deadLetter"
        )
    return (
        f"{failed} rows went to the dead letters ({_reasons(failures)}), past "
        f"maxRecordsBeforeAbort {dead['maxRecordsBeforeAbort']}: the output was not written"
    )


def _call_package(spark, package: dict, zips: list, context) -> object:
    """Import the entry point and call it, with writes refused throughout: what the
    package's modules run as they are imported counts as the package's too."""
    guard = WriteGuard()
    with guard:
        function = import_entry_point(package["entryPoint"], zips)
        log("package_loaded", entry_point=package["entryPoint"], sha256=package["sha256"])
        output = function(spark, context)
    if guard.refused:
        raise PackageError(
            f"the package tried to write while it ran ({', '.join(guard.refused)}); a package "
            "returns its DataFrame, and the runner writes it. Nothing was written"
        )
    return output


def _land(spark, config: dict, checked, columns, run_id: str, before: dict) -> None:
    """Count what the checks refused, refuse a table changed behind the runner's back, then
    land the dead letters and the output as :func:`package_outcome` says."""
    package, sink = config["package"], config["sink"]
    table, dead = f"{sink['catalog']}.{sink['table']}", package.get("deadLetter")
    counts = {row[0]: int(row[1]) for row in checked.groupBy(ERROR_COLUMN).count().collect()}
    failures = {reason: n for reason, n in counts.items() if reason is not None}
    failed = sum(failures.values())
    changed = [t for t in before if table_marker(spark, t) != before[t]]
    if changed:
        raise PackageError(
            f"{', '.join(changed)} changed while the package ran: a commit the runner did not "
            "make. The runner wrote nothing; read the table's snapshots and roll back what the "
            "package wrote"
        )
    land, write = package_outcome(failed, dead)
    if land:
        dead_table = f"{sink['catalog']}.{dead['table']}"
        write_package_dead_letters(spark, checked, dead_table, columns, run_id, package["sha256"])
        log("dead_letters", table=dead_table, records=failed, reasons=failures)
    if not write:
        raise PackageError(_not_written(failed, failures, dead))
    write_package_output(checked, table, columns, package["sha256"])
    log("done", table=table, rows=sum(counts.values()) - failed, dead_letters=failed)


def run_package(spark, config: dict, config_uri: str) -> None:
    """Run a prebuilt package's entry point by digest and write what it returns (see this
    section's comment). Raises PackageError when the runner writes no output."""
    from pyspark import StorageLevel

    configure_session(spark, config)
    package, sink = config["package"], config["sink"]
    table = f"{sink['catalog']}.{sink['table']}"
    dead = package.get("deadLetter")
    log(
        "start",
        product=config["product"],
        build=config["build"],
        mode="batch",
        table=table,
        package_sha256=package["sha256"],
        entry_point=package["entryPoint"],
    )
    create_sink_table(spark, config)
    for statement in (dead or {}).get("create") or []:
        spark.sql(statement)
    zips = _add_package(spark, package_location(config_uri, package["sha256"]), package["sha256"])
    run_id = str(spark.sparkContext.applicationId)
    # Before the package's code runs at all, its import included.
    watched = [table] + ([f"{sink['catalog']}.{dead['table']}"] if dead else [])
    before = {t: table_marker(spark, t) for t in watched}
    context = PackageContext(
        arguments=types.MappingProxyType(dict(package["arguments"])),
        product=config["product"],
        build=config["build"],
        output=table,
        run_id=run_id,
        package_sha256=package["sha256"],
    )
    output = _call_package(spark, package, zips, context)
    columns = _conformed_columns(spark, output, package["schema"])
    output.select(*columns).createOrReplaceTempView(PACKAGE_VIEW)
    queries = config["sql"]
    checked = spark.sql(queries["check"])
    # Computed once for the count and both writes: Spark recomputes a lost block from the
    # package's plan, so a package's output should be deterministic.
    checked = checked.persist(StorageLevel.MEMORY_AND_DISK)
    try:
        _land(spark, config, checked, columns, run_id, before)
    finally:
        checked.unpersist()


if __name__ == "__main__":
    sys.exit(main())
