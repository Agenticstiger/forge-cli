# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Where a Spark build's infrastructure lives, from the contract alone.

One module for both sides: the spark-infra emit names what it provisions with these
functions, and ``build_runners/spark`` finds it with them.

``fluid apply`` provisions it with OpenTofu (the spark-infra emit): the job config and
the runner in S3, the EMR Serverless application, the job's role and the Glue job. Runs
find them by these names, never by reading OpenTofu state, because Airflow starts a
scheduled build with ``fluid spark run`` and has no state to read.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

#: The tags on what forge provisions and starts for a Spark build.
PRODUCT_TAG = "fluid_product"
BUILD_TAG = "fluid_build"
#: The S3 prefix under the warehouse bucket that holds forge's Spark artifacts.
ARTIFACT_PREFIX = "_fluid/spark"
#: The runner's file name in S3, as package data names it.
RUNNER_FILE = "forge_spark_runner.py"


def _bounded(name: str, limit: int) -> str:
    """``name`` cut to ``limit`` characters, keeping a hash of all of it so cut names differ."""
    if len(name) <= limit:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: limit - 9]}-{digest}"


def job_config_uri(bucket: str, product: str, build: str) -> str:
    return f"s3://{bucket}/{ARTIFACT_PREFIX}/{product}/{build}/job.json"


def runner_uri(bucket: str, product: str, build: str, runner_sha256: str) -> str:
    """Per build, so one state owns it (bucketless products share ``<account>-fluid-data``,
    and a shared key would be deleted by one destroy for all), and content-addressed, so a
    new runner never overwrites the one a running job reads."""
    return (
        f"s3://{bucket}/{ARTIFACT_PREFIX}/{product}/{build}/runner/"
        f"{runner_sha256[:16]}/{RUNNER_FILE}"
    )


def _key(product: str, build: str) -> str:
    """8 hex characters of ``product/build``: ids hold hyphens, so ``fluid-a-b-c`` alone
    could be product ``a-b`` build ``c`` or product ``a`` build ``b-c``."""
    return hashlib.sha256(f"{product}/{build}".encode("utf-8")).hexdigest()[:8]


def _named(product: str, build: str, suffix: str, limit: int) -> str:
    tail = f"-{_key(product, build)}{suffix}"
    return _bounded(f"fluid-{product}-{build}", limit - len(tail)) + tail


def run_name(product: str, build: str) -> str:
    """A run's name on every runtime (EMR on EKS allows 64 characters, ``[.-_/#A-Za-z0-9]``)."""
    return _named(product, build, "", 64)


def application_name(contract: Mapping[str, Any]) -> str:
    """The product's own EMR Serverless application (packaging ``emrApplication:
    isolated``), when the build names no ``applicationId``. The domain's shared
    application is the platform's, and a build on it names its id."""
    return _bounded(f"fluid-{contract.get('id')}-spark", 64)


def execution_role_name(product: str, build: str) -> str:
    return _named(product, build, "-spark", 64)


def glue_job_name(product: str, build: str) -> str:
    return _named(product, build, "", 255)


def partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"
