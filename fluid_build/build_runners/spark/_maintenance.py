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

A table no forge build writes (``iceberg.maintenance.runtime``: its streams run on its team's own
runner) gets its run from that block instead (:func:`_unwritten_run`): the EMR runtime, target
and role it names, its job config compiled from the expose's binding, and its artifacts in the
table's bucket under ``spark.names.maintenance_build``. The operator's role, target and Spark
setting lists apply to it as to a build's ``parameters.spark``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any, Dict, List, Mapping, Optional

from fluid_build.build_runners.spark._desired import (
    ACCOUNT_TOKEN,
    LOCAL_ROOT_TOKEN,
    DesiredRun,
    _placement,
    _sha,
    _stamp_fields,
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
        FluidUserError: the operator's policy refuses the writer build, or the runtime of a
            table no forge build writes.
    """
    from fluid_build.maintenance.job import compiled_actions, maintenance_config, table_name
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
        if tm.runtime is not None:
            out.append(_unwritten_run(contract, tm, env, policy, stamps, load))
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
        actions = compiled_actions(tm, table, load=load)
        job = SparkJob(
            maintenance_config(tm, build_config, [(a.name, a.statements) for a in actions])
        )
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
                maintenance_actions=tuple(a.plan_entry() for a in actions),
            )
        )
    return [run.bind(account) for run in out] if account else out


def _location(contract: Mapping[str, Any], expose_id: str) -> Mapping[str, Any]:
    for expose in contract.get("exposes") or []:
        if isinstance(expose, Mapping) and str(expose.get("exposeId")) == expose_id:
            return dict((expose.get("binding") or {}).get("location") or {})
    return {}


def _unwritten_run(
    contract: Mapping[str, Any],
    tm: Any,
    env: Mapping[str, str],
    policy: bool,
    stamps: Optional[Mapping[str, str]],
    load: Any,
) -> DesiredRun:
    """The maintenance run of a table no forge build writes, on the EMR runtime its
    ``runtime`` names: the job config holds the table as the expose binds it (a Glue-catalog
    table), the runner and the config sit in the table's bucket under the name the run goes by
    (``spark.names.maintenance_build``), and the role and target are the block's own, checked
    against the operator's lists when ``policy`` is on (apply and ``--mode maintain``).

    Raises:
        ValueError: the expose is not a table the runner can maintain.
        FluidUserError: the operator's policy refuses the role, the target or a Spark setting.
    """
    from fluid_build.iac.providers.aws_identity import refuse_unless_maintenance_runtime_allowed
    from fluid_build.maintenance.job import compiled_actions, maintenance_config, table_name
    from fluid_build.providers.aws.util.warehouse import normalize_location
    from fluid_build.spark import runner_source
    from fluid_build.spark.compiler import CONFIG_VERSION, SparkJob, maintenance_sink

    cfg: Dict[str, Any] = dict(tm.runtime)
    runtime = str(cfg["runtime"])
    where = f"exposes[{tm.expose_id}].iceberg.maintenance.runtime"
    if policy:
        refuse_unless_maintenance_runtime_allowed(tm.expose_id, cfg, env)
    product = str(contract.get("id"))
    bid = _names.maintenance_build(tm.expose_id)
    loc = _location(contract, tm.expose_id)
    region, role, target = _placement(cfg, runtime, product, bid, loc, where, where)
    base = {
        "configVersion": CONFIG_VERSION,
        "product": product,
        "build": bid,
        "sink": maintenance_sink(contract, tm.expose_id, account_ref=ACCOUNT_TOKEN),
    }
    actions = compiled_actions(tm, table_name(base), load=load)
    job = SparkJob(maintenance_config(tm, base, [(a.name, a.statements) for a in actions]))
    spark_conf = _submit_conf(runtime, job.config, cfg.get("sparkConf") or {}, where, env, policy)
    release = str(cfg.get("releaseLabel") or _names.default_release(runtime))
    runner_sha = _sha(runner_source())
    bucket, _ = normalize_location(loc, account_ref=ACCOUNT_TOKEN)
    root = f"s3://{bucket}"
    settings: Dict[str, Any] = {
        "runtime": runtime,
        "maintains": tm.expose_id,
        "target": dict(target),
        "role": role,
        "release": release,
        "glueVersion": None,
        "sparkConf": spark_conf,
    }
    return DesiredRun(
        product=product,
        build=bid,
        runtime=runtime,
        mode="maintenance",
        scheduled=True,
        region=region,
        config_hash=_sha({"job": job.digest, "runner": runner_sha, "settings": settings}),
        job_digest=job.digest,
        runner_sha256=runner_sha,
        job_uri=_names.maintenance_config_uri(root, product, bid, tm.expose_id),
        runner_uri=_names.runner_uri(root, product, bid, runner_sha),
        run_name=_names.maintenance_run_name(product, tm.expose_id),
        role_arn=role,
        target=target,
        release_label=release,
        glue_version=None,
        spark_conf=spark_conf,
        retry_attempts=None,
        restarts_per_hour=0,
        job_text=job.text,
        maintains=tm.expose_id,
        maintenance_runtime=True,
        maintenance_actions=tuple(a.plan_entry() for a in actions),
        **_stamp_fields(contract, {"outputs": [tm.expose_id]}, stamps),
    )


__all__ = ["maintenance_runs"]
