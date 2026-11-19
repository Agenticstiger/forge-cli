# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Where a Spark build's infrastructure lives, from the contract alone.

``fluid apply`` provisions it with OpenTofu (``iac/providers/aws_ingest.py``): the job
config and the runner in S3, the EMR Serverless application, the job's role and the Glue
job. Runs (``build_runners/spark``) find them by these names, never by reading OpenTofu
state, so starting a run needs the contract alone. Both lanes import this one module, so the
names cannot drift apart.

Product and build ids hold hyphens, so ``fluid-a-b-c`` alone could be product ``a-b``
build ``c`` or product ``a`` build ``b-c``: every per-build name ends in 8 hex characters
of ``<product>/<build>``. Pure functions, no imports beyond the standard library.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, Optional

#: The S3 prefix under the output's bucket that holds forge's Spark artifacts.
ARTIFACT_PREFIX = "_fluid/spark"
#: The runner's file name in S3, as package data names it.
RUNNER_FILE = "forge_spark_runner.py"
#: The job config's file name in S3.
CONFIG_FILE = "job.json"
#: Tag keys on every Spark resource (the same keys as the Databricks jobs).
PRODUCT_TAG = "fluid_product"
BUILD_TAG = "fluid_build"


def _bounded(name: str, limit: int) -> str:
    """``name`` cut to ``limit`` characters, keeping a hash of all of it so cut names differ."""
    if len(name) <= limit:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: limit - 9]}-{digest}"


def build_key(product: str, build: str) -> str:
    """8 hex characters of ``product/build``: what tells ``a-b``/``c`` from ``a``/``b-c``."""
    return hashlib.sha256(f"{product}/{build}".encode("utf-8")).hexdigest()[:8]


def _named(product: str, build: str, suffix: str, limit: int) -> str:
    tail = f"-{build_key(product, build)}{suffix}"
    return _bounded(f"fluid-{product}-{build}", limit - len(tail)) + tail


#: An EMR release label as each EMR runtime takes it. EMR Serverless and an EMR on EC2
#: cluster name the release (``emr-7.12.0``); EMR on EKS names an image of it, its newest
#: (``emr-7.12.0-latest``) or a dated one (``emr-7.12.0-20251111``), after an optional
#: variant (``emr-7.12.0-java11-latest``), and refuses the plain form.
RELEASE_LABELS = {
    "emr-serverless": re.compile(r"emr-[0-9]+\.[0-9]+\.[0-9]+"),
    "emr-ec2": re.compile(r"emr-[0-9]+\.[0-9]+\.[0-9]+"),
    "emr-eks": re.compile(r"emr-[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9]+)*-(?:latest|[0-9]{8})"),
}
DEFAULT_RELEASE = "emr-7.12.0"
#: EMR on EKS's label of :data:`DEFAULT_RELEASE`: its newest image of that release.
DEFAULT_EKS_RELEASE = "emr-7.12.0-latest"


def default_release(runtime: str) -> Optional[str]:
    """The release label a build of ``runtime`` runs on when it names none; Glue none."""
    if runtime not in RELEASE_LABELS:
        return None
    return DEFAULT_EKS_RELEASE if runtime == "emr-eks" else DEFAULT_RELEASE


def release_label_form(runtime: str) -> str:
    """How ``runtime`` writes a release label, for a message."""
    if runtime == "emr-eks":
        return f"emr-X.Y.Z-latest or emr-X.Y.Z-YYYYMMDD (e.g. {DEFAULT_EKS_RELEASE})"
    return f"emr-X.Y.Z (e.g. {DEFAULT_RELEASE})"


def artifact_prefix(product: str, build: str) -> str:
    """The build's own artifact prefix in the output's bucket, ending in ``/``."""
    return f"{ARTIFACT_PREFIX}/{product}/{build}/"


def job_config_key(product: str, build: str) -> str:
    return artifact_prefix(product, build) + CONFIG_FILE


def job_config_uri(bucket: str, product: str, build: str) -> str:
    return f"s3://{bucket}/{job_config_key(product, build)}"


def runner_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def runner_key(product: str, build: str, sha256: str) -> str:
    """Per build and content-addressed: a new runner never overwrites the one a running job
    reads, and no two products' states own one object (bucketless products share the
    ``<account>-fluid-data`` bucket, so a shared key would go with either one's destroy)."""
    return f"{artifact_prefix(product, build)}runner/{sha256[:16]}/{RUNNER_FILE}"


def runner_uri(bucket: str, product: str, build: str, sha256: str) -> str:
    return f"s3://{bucket}/{runner_key(product, build, sha256)}"


def run_name(product: str, build: str) -> str:
    """A run's name on every runtime (EMR on EKS allows 64 characters, ``[.-_/#A-Za-z0-9]``)."""
    return _named(product, build, "", 64)


def application_name(product: str) -> str:
    """The product's own EMR Serverless application (``packaging.containers.emrApplication:
    isolated``). A domain's shared application is the platform's and is named by
    ``parameters.spark.applicationId``."""
    return _bounded(f"fluid-{product}-spark", 64)


def execution_role_name(product: str, build: str) -> str:
    return _named(product, build, "-spark", 64)


def glue_job_name(product: str, build: str) -> str:
    return _named(product, build, "", 255)


def log_group_name(product: str, build: str) -> str:
    return f"/aws/fluid/spark/{product}/{build}"


def registration_role_name(product: str, expose: str) -> str:
    """The role Lake Formation registers a Spark-written location with."""
    tail = f"-{build_key(product, expose)}-lf"
    return _bounded(f"fluid-{product}-{expose}", 64 - len(tail)) + tail


def caller_policy_name(product: str) -> str:
    """The managed policy a scheduler attaches to start and watch the product's runs."""
    return _bounded(f"fluid-{product}-spark-caller", 128)


def tags(product: str, build: str) -> Dict[str, str]:
    return {PRODUCT_TAG: product, BUILD_TAG: build}


def partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"
