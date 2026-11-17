# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The jars a Glue Spark job adds to Glue 5's own classpath, pinned by sha256.

Glue documents no Maven route (no ``--packages``), so a forge Spark build on
``runtime: glue`` takes its missing jars from an S3 prefix the platform fills
(:data:`JARS_URL_ENV`), through ``--extra-jars``. OpenTofu reads each object with
``checksum_mode = "ENABLED"`` and the Glue job's ``lifecycle.precondition`` compares its
``checksum_sha256`` with the pin below, so ``tofu plan`` and ``apply`` fail before a job
can load a jar that was swapped. The EMR runtimes resolve pinned Maven coordinates at run
time instead (``build_runners/spark``).

What Glue 5 already ships, measured with a Glue 5.0 ``gluestreaming`` job in
ca-central-1 (2026-11-17; Spark 3.5.4-amzn-0, Scala 2.12.18), by loading each class:

- ``spark-sql-kafka-0-10_2.12`` 3.5.4-amzn-0 and its token provider
  (``/usr/lib/spark/connector/lib``), ``kafka-clients`` 3.4.1, ``commons-pool2`` 2.11.1
  and ``aws-msk-iam-auth`` 2.2.0 (``/usr/share/aws/glue-streaming/lib``);
- ``spark-avro_2.12`` 3.5.4-amzn-0, Avro 1.11.2, and Iceberg 1.7.1 (``--datalake-formats
  iceberg``).

A JSON or MSK IAM build therefore adds nothing: pinning a second Kafka connector would
only risk a clash. ABRiS is missing, so a Confluent Avro build adds ABRiS 6.4.0 (the
version the EMR runtimes pin) and the Confluent 6.2.1 clients it is built against, and
nothing Glue already has. Glue 5 also carries a Confluent ``CachedSchemaRegistryClient``
inside its Hudi utilities bundle, so these jars go first (``--user-jars-first``).

Each sha256 was computed from the artifact downloaded from Maven Central or
packages.confluent.io, whose published sha1 matched. ``checksum_sha256`` is what S3
reports for a single-part upload made with ``--checksum-algorithm SHA256``: the base64 of
the digest.

What the check covers: ``tofu plan`` and ``apply`` read each object and refuse a digest
that is not the pin. A Glue run reads the jars when it starts, which no plan sees, so the
platform keeps the prefix write-once: only its own principal may ``s3:PutObject`` there,
with S3 Object Lock (or a bucket policy that denies overwriting) on the prefix.

Filling the prefix (the platform, once per Glue release): download each jar below from
Maven Central or packages.confluent.io, check its sha256 against this table, and upload it
single-part with ``aws s3 cp <jar> s3://<bucket>/<prefix>/<file> --checksum-algorithm
SHA256`` (:func:`upload_commands`). A recent AWS CLI defaults to CRC64NVME, which leaves no
SHA-256 for the check. The applier needs ``s3:GetObject`` (and ``kms:Decrypt`` for SSE-KMS)
on those keys; the job role gets exactly that.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

#: Operator-only: where the platform keeps the pinned jars, ``s3://<bucket>/<prefix>/``.
#: The ``_URL`` suffix makes it a protected setting (a project ``.env`` cannot set it).
JARS_URL_ENV = "FLUID_SPARK_GLUE_JARS_URL"

_URL = re.compile(
    r"s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(?P<prefix>(?:[A-Za-z0-9._=-]+/)*)"
)


@dataclass(frozen=True)
class GlueJar:
    coordinate: str  # group:artifact:version
    file: str
    size: int
    sha256: str  # hex
    sha256_b64: str  # as S3 reports checksum_sha256


# verified on Glue 5.0 (2026-11-17), ca-central-1, two gluestreaming probe runs.
#: What Glue 5.0 ships, by loading each class (probe 1, no extra jars). A later Glue
#: release that drops or changes one of these needs this table and the lock re-checked.
GLUE5_BUNDLED: Mapping[str, str] = {
    "spark": "3.5.4-amzn-0 (Scala 2.12.18)",
    "spark-sql-kafka-0-10_2.12": "3.5.4-amzn-0, /usr/lib/spark/connector/lib",
    "spark-token-provider-kafka-0-10_2.12": "3.5.4-amzn-0, /usr/lib/spark/connector/lib",
    "kafka-clients": "3.4.1, /usr/share/aws/glue-streaming/lib",
    "commons-pool2": "2.11.1, /usr/share/aws/glue-streaming/lib",
    "aws-msk-iam-auth": "2.2.0, /usr/share/aws/glue-streaming/lib",
    "spark-avro_2.12": "3.5.4-amzn-0",
    "iceberg-spark-runtime-3.5_2.12": "1.7.1-amzn-1 (--datalake-formats iceberg)",
    "abris": "absent",
    "confluent schema-registry client": "only inside hudi-utilities-bundle_2.12-0.15.0-amzn-5",
}
#: Probe 2 (these jars, --user-jars-first): ABRiS from_avro decoded a Confluent-framed
#: record from a registry the driver served; CachedSchemaRegistryClient and RestService
#: loaded from kafka-schema-registry-client-6.2.1.jar, ABRiS from abris_2.12-6.4.0.jar, and
#: AvroDataToCatalyst from Glue's spark-avro. The set is used as proven: none is dropped.
#: What a Confluent Avro (``avro-confluent``) build adds on Glue 5.
AVRO_JARS: Tuple[GlueJar, ...] = (
    GlueJar(
        "za.co.absa:abris_2.12:6.4.0",
        "abris_2.12-6.4.0.jar",
        204298,
        "4939173289337bd98d6b5c404ea0cda197ff711ef8ececc810a58c0759220650",  # pragma: allowlist secret - a published jar digest
        "STkXMokze9mNa1xATqDNoZf/cR747OzIEKWMB1kiBlA=",  # pragma: allowlist secret - a published jar digest
    ),
    GlueJar(
        "za.co.absa.commons:commons_2.12:1.0.0",
        "commons_2.12-1.0.0.jar",
        279643,
        "8b5ef2c3fede529ea172d5f9054337b2112dd41cec5afacc2ee19398f85cf0d9",  # pragma: allowlist secret - a published jar digest
        "i17yw/7eUp6hctX5BUM3shEt1BzsWvrMLuGTmPhc8Nk=",  # pragma: allowlist secret - a published jar digest
    ),
    GlueJar(
        "io.confluent:kafka-avro-serializer:6.2.1",
        "kafka-avro-serializer-6.2.1.jar",
        30184,
        "c52f3d0e68a7b8d8f2e5b462bd600c4b998ad8015fd5f21fb7107404350dda05",  # pragma: allowlist secret - a published jar digest
        "xS89DminuNjy5bRivWAMS5mK2AFf1fIftxB0BDUN2gU=",  # pragma: allowlist secret - a published jar digest
    ),
    GlueJar(
        "io.confluent:kafka-schema-serializer:6.2.1",
        "kafka-schema-serializer-6.2.1.jar",
        29606,
        "ccdfef5fbfc8ccef893d94b8b7fba12b7b783dc269cb335a7dddfb4f7bee1fff",  # pragma: allowlist secret - a published jar digest
        "zN/vX7/IzO+JPZS4t/uhK3t4PcJpyzNafd37T3vuH/8=",  # pragma: allowlist secret - a published jar digest
    ),
    GlueJar(
        "io.confluent:kafka-schema-registry-client:6.2.1",
        "kafka-schema-registry-client-6.2.1.jar",
        118151,
        "7b43ed00ae0ca5090dc62a5ee5e794d437e76ad9657dba315c1728f501268cf3",  # pragma: allowlist secret - a published jar digest
        "e0PtAK4MpQkNxipe5eeU1DfnatllfboxXBco9QEmjPM=",  # pragma: allowlist secret - a published jar digest
    ),
    GlueJar(
        "io.confluent:common-config:6.2.1",
        "common-config-6.2.1.jar",
        23669,
        "e02201027fe17b04c1b17914e6089a3b5f724d5b292ad912f208e4df64cd4e36",  # pragma: allowlist secret - a published jar digest
        "4CIBAn/hewTBsXkU5giaO19yTVspKtkS8gjk32TNTjY=",  # pragma: allowlist secret - a published jar digest
    ),
    GlueJar(
        "io.confluent:common-utils:6.2.1",
        "common-utils-6.2.1.jar",
        17266,
        "e76389fa2eb2620fea0ccb9f4ac3cf4b3a4a0fa589facb508ec930f9cb313c2d",  # pragma: allowlist secret - a published jar digest
        "52OJ+i6yYg/qDMufSsPPSzpKD6WJ+stQjskw+csxPC0=",  # pragma: allowlist secret - a published jar digest
    ),
)


def needed_jars(config: Mapping[str, object]) -> Tuple[GlueJar, ...]:
    """The jars a build's job config needs on Glue 5: the Avro set for a Confluent Avro
    value or key, else none (Kafka and MSK IAM ship with Glue)."""
    source = config.get("source") if isinstance(config, Mapping) else None
    if not isinstance(source, Mapping):
        return ()
    formats = {
        (part.get("format") if isinstance(part, Mapping) else None)
        for part in (source.get("value"), source.get("key"))
    }
    return AVRO_JARS if "avro-confluent" in formats else ()


@dataclass(frozen=True)
class JarsLocation:
    bucket: str
    prefix: str

    def key(self, jar: GlueJar) -> str:
        return f"{self.prefix}{jar.file}"


def jars_location(environ: Optional[Mapping[str, str]] = None) -> Optional[JarsLocation]:
    """The operator's jar prefix, or None when unset. Raises ValueError when malformed."""
    env = os.environ if environ is None else environ
    raw = (env.get(JARS_URL_ENV) or "").strip()
    if not raw:
        return None
    url = raw if raw.endswith("/") else raw + "/"
    match = _URL.fullmatch(url)
    if match is None or ".." in url.split("/"):
        raise ValueError(f"{JARS_URL_ENV} must be s3://<bucket>/<prefix>/, not {raw!r}")
    return JarsLocation(match["bucket"], match["prefix"])


def upload_commands(location: JarsLocation) -> Tuple[str, ...]:
    """How the platform fills the prefix: single-part uploads with a SHA-256 checksum."""
    return tuple(
        f"aws s3 cp {jar.file} s3://{location.bucket}/{location.key(jar)} "
        "--checksum-algorithm SHA256"
        for jar in AVRO_JARS
    )
