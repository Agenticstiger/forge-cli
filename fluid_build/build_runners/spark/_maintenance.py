# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runs of the contract's backend-spark Iceberg table maintenance (``fluid_build/maintenance``).

One :class:`~._desired.DesiredRun` per table, made from the run of the build that writes it
(:func:`~._desired.desired_runs`): the same runtime, target, role, release and region, so a
maintenance run reads and writes what the build may and nothing more. Its job config
(:func:`fluid_build.maintenance.job.maintenance_config`), run name, run history and plan action
(``spark.maintain``) are its own, so the build's reconcile never takes a maintenance run for one of
its runs, nor the other way round. It always ends on its own (``scheduled``), and starts no
service retry: the next scheduled run is the retry.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any, Dict, List, Mapping, Optional

from fluid_build.build_runners.spark._desired import (
    LOCAL_ROOT_TOKEN,
    DesiredRun,
    _sha,
    _submit_conf,
    desired_runs,
)
from fluid_build.spark import names as _names


def _root(run: DesiredRun) -> str:
    """The artifact root ``run``'s job config sits under (``s3://<bucket>`` or the local
    root's token)."""
    key = "/" + _names.job_config_key(run.product, run.build)
    if not run.job_uri.endswith(key):
        raise ValueError(f"Spark build {run.build}: its job config is not where forge puts it")
    return run.job_uri[: -len(key)]


def maintenance_runs(
    contract: Mapping[str, Any],
    *,
    expose_id: Optional[str] = None,
    account: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    policy: bool = True,
    stamps: Optional[Mapping[str, str]] = None,
    load: Any = None,
) -> List[DesiredRun]:
    """Each backend-spark table maintenance's run, in contract order (``expose_id``: that one).

    ``load`` stands in for the plugin loader (tests). The other arguments are
    :func:`~._desired.desired_runs`'s.

    Raises:
        ValueError: the maintenance or its writer build does not compile.
        MaintenanceActionError: a plugin action cannot be used (a ``ValueError``).
        FluidUserError: the operator's policy refuses the writer build.
    """
    from fluid_build.maintenance.job import action_statements, maintenance_config, table_name
    from fluid_build.maintenance.spec import read_maintenance
    from fluid_build.spark.compiler import SparkJob, spark_builds, spark_parameters

    tables, errors = read_maintenance(contract)
    if errors:
        raise ValueError(errors[0])
    env = os.environ if environ is None else environ
    builds = {str(b.get("id")): b for b in spark_builds(contract)}
    out: List[DesiredRun] = []
    for tm in tables:
        if tm.backend != "spark" or (expose_id is not None and tm.expose_id != expose_id):
            continue
        writers = desired_runs(
            contract, build_id=tm.build_id, environ=env, policy=policy, stamps=stamps
        )
        if len(writers) != 1 or tm.build_id is None:
            raise ValueError(
                f"exposes[{tm.expose_id}].iceberg.maintenance: build {tm.build_id} is not a "
                "forge Spark build"
            )
        writer = writers[0]
        build_config = json.loads(writer.job_text)
        table = table_name(build_config)
        statements = action_statements(tm, table, load=load)
        job = SparkJob(maintenance_config(tm, build_config, statements))
        cfg = spark_parameters(builds[tm.build_id]) or {}
        where = f"exposes[{tm.expose_id}].iceberg.maintenance (build {tm.build_id})"
        spark_conf = _submit_conf(
            writer.runtime, job.config, cfg.get("sparkConf") or {}, where, env, policy
        )
        settings: Dict[str, Any] = {
            "runtime": writer.runtime,
            "maintains": tm.expose_id,
            "target": dict(writer.target),
            "role": writer.role_arn,
            "release": writer.release_label,
            "glueVersion": writer.glue_version,
            "sparkConf": spark_conf,
        }
        root = LOCAL_ROOT_TOKEN if writer.local else _root(writer)
        out.append(
            dataclasses.replace(
                writer,
                mode="maintenance",
                scheduled=True,
                config_hash=_sha(
                    {"job": job.digest, "runner": writer.runner_sha256, "settings": settings}
                ),
                job_digest=job.digest,
                job_uri=_names.maintenance_config_uri(
                    root, writer.product, writer.build, tm.expose_id
                ),
                run_name=_names.maintenance_run_name(writer.product, tm.expose_id),
                spark_conf=spark_conf,
                retry_attempts=None,
                source_id=None,
                checkpoint_id=None,
                checkpoint=None,
                topic=None,
                restarts_per_hour=0,
                job_text=job.text,
                job_sha256=None,
                package_source=None,
                package_sha256=None,
                package_entry_point=None,
                package_uri=None,
                maintains=tm.expose_id,
            )
        )
    return [run.bind(account) for run in out] if account else out


__all__ = ["maintenance_runs"]
