# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The forge Spark runner: runs one job config that forge-cli generated (configVersion 1).

    spark-submit forge_spark_runner.py --config s3://bucket/path/job.json

The same file runs on EMR Serverless, EMR on EC2, EMR on EKS and AWS Glue 5 (Spark
3.5). It holds no contract logic: the config (``fluid_build.spark.compiler``) carries
the queries, already checked against the contract. forge-cli never imports this file,
so forge has no pyspark dependency; it depends on pyspark and the standard library
only (plus boto3, which every AWS Spark runtime has, for an ``aws-sm://`` registry
credential).

- streaming: Kafka -> decode (JSON, or Confluent Avro through ABRiS) -> the source
  query -> watermark -> the select -> append to the Iceberg table, with the config's
  checkpoint and trigger.
- batch: the same read of what the topic holds now, or the contract's own SELECT over
  catalog tables, -> overwrite the partitions it computes.

The submitter puts on the classpath what the job uses: the Iceberg Spark runtime and
its AWS bundle (EMR: the release's Iceberg; Glue: ``--datalake-formats iceberg``),
``spark-sql-kafka-0-10``, ``aws-msk-iam-auth`` for MSK IAM, and for Avro ``spark-avro``
with ``za.co.absa:abris_2.12:6.4.0`` (Confluent's Maven repository). A catalog the
submitter declares (``spark.sql.catalog.<catalog>``) is used as declared; otherwise the
runner derives the Glue catalog from the config, and any single setting the submitter
passes (``spark.sql.catalog.<catalog>.glue.endpoint``, say) wins.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

CONFIG_VERSION = 1
SOURCE_TABLE = "source_events"
RECORDS_TABLE = "kafka_records"
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
    return config


def resolve_secret(ref: str) -> str:
    """A registry credential: ``env://NAME`` or ``aws-sm://<name-or-arn>[#key]``."""
    scheme, _, locator = ref.partition("://")
    locator, _, field = locator.partition("#")
    if scheme == "env":
        value = os.environ.get(locator)
        if not value:
            raise JobConfigError(f"the registry credential's variable {locator} is not set")
        return value
    if scheme == "aws-sm":
        import boto3  # every AWS Spark runtime has it; imported only when used

        name, _, version = locator.partition("?version=")
        kwargs = {"SecretId": name, **({"VersionId": version} if version else {})}
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
    with urllib.request.urlopen(request, timeout=_REGISTRY_TIMEOUT_S) as response:  # nosec B310
        return json.loads(response.read())["schema"]


def registry_conf(registry: dict) -> dict:
    conf = {"schema.registry.url": registry["url"]}
    if registry.get("auth"):
        conf["basic.auth.credentials.source"] = "USER_INFO"
        conf["basic.auth.user.info"] = resolve_secret(registry["auth"]["secretRef"])
    return conf


def kafka_options(source: dict, mode: str) -> dict:
    options = {
        "kafka.bootstrap.servers": source["bootstrapServers"],
        "subscribe": source["topic"],
        "startingOffsets": source["startingOffsets"],
        "includeHeaders": "true" if source.get("includeHeaders") else "false",
    }
    if mode == "batch":
        options["endingOffsets"] = "latest"
    if source["auth"] == "msk-iam":
        options.update(_MSK_IAM)
    elif source["auth"] != "none":
        raise JobConfigError(f"unknown Kafka auth {source['auth']!r}")
    return options


def configure_session(spark, config: dict) -> None:
    for name, value in SESSION_CONF.items():
        spark.conf.set(name, value)
    sink = config["sink"]
    prefix = f"spark.sql.catalog.{sink['catalog']}"
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
    spark.conf.set("spark.sql.defaultCatalog", sink["catalog"])
    # forge checks nullability against the contract statically, and the source query
    # fails on a required field that arrives null; Spark's own check would refuse every
    # CASE expression as nullable.
    spark.conf.set("spark.sql.iceberg.check-nullability", "false")


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


def check_decoded(frame, column: str, ddl: str) -> None:
    """Refuse a decoded record whose types differ from the declared ones.

    A Spark cast that loses data returns null instead of failing, so the source query's
    casts are safe only over the types the contract declares.
    """
    from pyspark.sql.types import StructType

    declared = StructType.fromDDL(ddl)
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


def read_records(spark, config: dict):
    """``kafka_records``: the Kafka source's columns, ``value`` and ``key`` decoded."""
    source = config["source"]
    if source.get("kind") != "kafka":
        raise JobConfigError("the source must be kafka")
    streaming = config["mode"] == "streaming"
    reader = spark.readStream if streaming else spark.read
    raw = reader.format("kafka").options(**kafka_options(source, config["mode"])).load()
    columns = [
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
        if spec is not None and spec["format"] != "raw":
            check_decoded(records, column, spec["schema"])
    return records


def build_output(spark, config: dict):
    """The output rows: the generated queries, run over the decoded records."""
    queries = config["sql"]
    if config.get("source"):
        read_records(spark, config).createOrReplaceTempView(RECORDS_TABLE)
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
    output = build_output(spark, config)
    if config["mode"] == "batch":
        writer = output.writeTo(table).option("check-nullability", "false")
        if sink["write"] == "overwrite":
            writer.overwritePartitions()
        else:
            writer.append()
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
    query = writer.toTable(table)
    log("streaming", table=table, id=str(query.id))
    query.awaitTermination()
    log("done", table=table)


def read_text(spark, path: str) -> str:
    """The config file's text, from any file system Spark reads (s3://, file://, a path)."""
    if "://" not in path or path.startswith("file://"):
        with open(path.removeprefix("file://"), encoding="utf-8") as handle:
            return handle.read()
    return spark.read.text(path, wholetext=True).first()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one forge Spark job config.")
    parser.add_argument("--config", required=True, help="the job.json, a path or s3:// URI")
    # Glue passes its own --JOB_NAME and friends; they are not this runner's.
    args, _ = parser.parse_known_args(argv)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    run(spark, parse_config(read_text(spark, args.config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
