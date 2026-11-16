# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The forge Spark runner and the job config forge-cli generates for it.

:mod:`fluid_build.spark.compiler` turns an ``engine: spark`` build into a
deterministic ``job.json``. ``runner/forge_spark_runner.py`` is the one PySpark
program that reads it, shipped as package data: forge-cli never imports it, so core
has no pyspark dependency. :func:`runner_source` returns its text for an uploader.
"""

from __future__ import annotations

from importlib.resources import files

#: The runner's path inside this package, as package data.
RUNNER = "runner/forge_spark_runner.py"


def runner_source() -> str:
    """The runner's source, to upload next to the job config."""
    return files(__name__).joinpath(RUNNER).read_text(encoding="utf-8")
