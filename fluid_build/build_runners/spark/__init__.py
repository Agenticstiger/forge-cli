# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Spark job runs of ``engine: spark`` builds, on EMR Serverless, Glue, EMR on EC2 and EKS.

OpenTofu provisions a Spark build (the job config and runner in S3, the application, the
role, the Glue job); there is no OpenTofu resource for a job run, so runs are started here:

- :mod:`._desired` — each build's :class:`~._desired.DesiredRun` and config hash, and the
  ``spark.reconcile`` actions ``fluid plan`` lists;
- :mod:`._adapters` — one adapter per runtime (start, find, read, cancel);
- :mod:`._reconcile` — ``fluid apply`` keeps streaming jobs on the declared config, and
  its build phase (``--mode amend-and-build``, what a scheduler runs) starts a scheduled
  build and waits for it;
- ``fluid_build.cli._spark_apply`` — the two hooks ``fluid plan`` and ``fluid apply`` call
  (in ``cli``: build_runners never depends on it, an import-linter contract).

Nothing here is imported on the ``fluid --help`` cold path, and boto3 (the ``[aws]`` extra)
only when a run is started or read.
"""
