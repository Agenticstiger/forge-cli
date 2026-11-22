# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The hooks ``fluid plan`` and ``fluid apply`` call for Spark builds.

``fluid plan`` lists a ``spark.reconcile`` action per Spark build (:func:`planned_actions`),
which ``planDigest`` covers. ``fluid apply`` compiles the runs before ``tofu apply``
(:func:`prepare`), refusing a bound plan whose Spark actions differ from them and a missing
boto3, so nothing is provisioned that could not then be started; after a ``tofu apply`` that
succeeded it reconciles the streaming jobs (:func:`reconcile_after_apply`). Both warn about
an EMR on EC2 cluster that runs a build's driver or application master where Spot can take
it (:func:`plan_posture`, :func:`warn_cluster_posture`).

``fluid apply --mode reconcile`` reconciles the streams alone (:func:`reconcile_only`): no
``tofu``, no plan, and a stream starts again only with the config the last apply deployed.

A backend-spark Iceberg table maintenance (``exposes[].iceberg.maintenance``) is planned as a
``spark.maintain`` action (:func:`planned_actions`), its job config deployed beside its writer
build's, and run by ``fluid apply --mode maintain`` (:func:`maintain_only`): no ``tofu``, no
plan, one run on the writer build's runtime with the config the last apply deployed.

A build of ``runtime: local`` runs on the machine ``fluid apply`` runs on, with nothing for
OpenTofu to provision: the native engine (outputs on ``platform: local``) reconciles it the
same way, and :func:`refuse_without_opentofu` refuses only the AWS runtimes there.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fluid_build.build_runners.spark._desired import ACTION, MAINTAIN_ACTION, DesiredRun

#: The plan actions this module lists and checks: a build's run and a table's maintenance.
_SPARK_OPS = (ACTION, MAINTAIN_ACTION)


def _cli_error(event: str, context: Dict[str, Any]) -> Exception:
    from fluid_build.cli._common import CLIError

    return CLIError(1, event, context)


def _desired(
    contract: Mapping[str, Any], event: str, *, maintenance: bool = False, **kwargs: Any
) -> List[DesiredRun]:
    """The runs (``maintenance``: the tables' maintenance runs), or ``CLIError``
    ``<event>_refused`` (policy) / ``<event>_invalid``."""
    import copy

    from fluid_build._errors import FluidUserError
    from fluid_build.build_runners.spark._desired import desired_runs
    from fluid_build.build_runners.spark._maintenance import maintenance_runs
    from fluid_build.cli._common import resolve_env_templates_in_contract

    try:
        # As the OpenTofu engine reads it: ``{{ env.* }}`` resolved, the same at plan and apply.
        resolved = resolve_env_templates_in_contract(copy.deepcopy(dict(contract)))
        if maintenance:
            return maintenance_runs(resolved, **kwargs)
        return desired_runs(resolved, **kwargs)
    except FluidUserError as exc:
        raise _cli_error(
            f"{event}_refused", {"error": f"{exc.what}. {exc.why}", "hint": exc.fix}
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise _cli_error(f"{event}_invalid", {"error": str(exc)}) from exc


def _stamps(
    contract: Mapping[str, Any],
    run_id: Optional[str],
    *,
    refuse_release: bool = True,
    verified_plan: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """What the runner stamps on each commit besides the job config's own: the contract's
    digest, the bundle ``verified_plan`` binds (its ``bundleDigest``, checked before this runs),
    the pipeline's ``run_id``, ``FLUID_RELEASE_ID`` and the scheduler's run
    (``_desired.run_stamps``). A deploy refuses a release a stamp may not hold;
    ``--mode reconcile`` (``refuse_release`` off) leaves it off with a warning, so a mis-set
    variable cannot stop the keepalive from restarting a dead stream.

    Raises:
        CLIError: ``apply_spark_invalid`` (``FLUID_RELEASE_ID`` holds a character a stamp may not).
    """
    from fluid_build.build_runners.spark._desired import run_stamps

    bundle = verified_plan.get("bundleDigest") if verified_plan is not None else None
    try:
        return run_stamps(
            contract, run_id=run_id, refuse_release=refuse_release, bundle_digest=bundle
        )
    except ValueError as exc:
        raise _cli_error("apply_spark_invalid", {"error": f"{exc}; nothing was started"}) from exc


def _has_spark(contract: Mapping[str, Any]) -> bool:
    from fluid_build.build_runners.spark._desired import spark_builds

    try:
        return bool(spark_builds(contract))
    except ValueError:
        return True  # let the compile report it


def planned_actions(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """``fluid plan``'s ``spark.reconcile`` actions, then its ``spark.maintain`` ones (steps
    renumbered by the caller).

    Raises:
        CLIError: ``plan_spark_invalid`` / ``plan_spark_refused``.
    """
    if not _has_spark(contract):
        return []
    # The operator's gates are apply's (fluid validate warns about them); they refuse a
    # build, never change what it compiles to. FLUID_SPARK_MAVEN_REPOSITORY and, on Glue,
    # FLUID_SPARK_GLUE_JARS_URL do change it (where the run loads code from), so the plan
    # binds them: apply refuses a plan made with other values.
    runs = _desired(contract, "plan_spark", policy=False)
    if _has_spark_maintenance(contract):
        runs += _desired(contract, "plan_spark", maintenance=True, policy=False)
    return [dict(_comparable(run.plan_action(0))) for run in runs]


def _has_spark_maintenance(contract: Mapping[str, Any]) -> bool:
    from fluid_build.maintenance.spec import declares_maintenance

    return declares_maintenance(contract)


def _comparable(action: Mapping[str, Any]) -> Mapping[str, Any]:
    return {k: v for k, v in action.items() if k not in ("step", "depends_on")}


def prepare(
    contract: Mapping[str, Any],
    *,
    build_id: Optional[str],
    verified_plan: Optional[Mapping[str, Any]],
    dry_run: bool,
    workspace: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> List[DesiredRun]:
    """The runs ``fluid apply`` reconciles, checked before ``tofu apply``. A local run's
    settings are checked against ``workspace``, the contract's directory, which holds it.
    ``run_id`` is the pipeline's, which the runner stamps on each commit of a run started here.

    Raises:
        CLIError: ``apply_spark_invalid`` / ``apply_spark_refused`` (the build does not
            compile, or the operator's policy refuses it), ``apply_spark_artifact_mismatch``
            (the bound plan lists other Spark runs), ``apply_aws_sdk_missing``.
    """
    if not _has_spark(contract):
        if verified_plan and any(
            a.get("op") in _SPARK_OPS
            for a in verified_plan.get("actions") or []
            if isinstance(a, Mapping)
        ):
            raise _cli_error(
                "apply_spark_artifact_mismatch",
                {
                    "error": "the bound plan starts Spark runs the contract no longer declares; "
                    "regenerate it"
                },
            )
        return []
    stamps = _stamps(contract, run_id, verified_plan=verified_plan)
    runs = _desired(contract, "apply_spark", stamps=stamps)
    # The tables' maintenance runs: this apply deploys their job configs (OpenTofu uploads
    # them beside the builds'), and fluid apply --mode maintain runs them.
    maintained = (
        _desired(contract, "apply_spark", maintenance=True, stamps=stamps)
        if _has_spark_maintenance(contract)
        else []
    )
    if verified_plan is not None:
        planned = [
            _comparable(a)
            for a in verified_plan.get("actions") or []
            if isinstance(a, Mapping) and a.get("op") in _SPARK_OPS
        ]
        if planned != [_comparable(r.plan_action(0)) for r in [*runs, *maintained]]:
            raise _cli_error(
                "apply_spark_artifact_mismatch",
                {
                    "error": "the Spark runs compiled now differ from the bound plan's "
                    "spark.reconcile and spark.maintain actions (job config, runner, a "
                    "maintenance action's statements, a prebuilt package's source or run "
                    "settings, which include FLUID_SPARK_MAVEN_REPOSITORY and "
                    "FLUID_SPARK_GLUE_JARS_URL); regenerate it with the same environment as "
                    "apply"
                },
            )
    selected = [r for r in runs if build_id is None or r.build == build_id]
    if not dry_run and any(r.local for r in selected):
        _check_local(selected, workspace)
    if not dry_run and any(r.long_running and not r.local for r in selected):
        _require_boto3()
    return selected


def _require_boto3() -> None:
    import importlib.util

    try:
        found = importlib.util.find_spec("boto3") is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        raise _cli_error(
            "apply_aws_sdk_missing",
            {
                "error": "a streaming Spark build is started after tofu apply through boto3, "
                "which is not installed; nothing was applied",
                "hint": "pip install 'data-product-forge[aws]'",
            },
        )


def _check_local(runs: List[DesiredRun], workspace: Optional[Path] = None) -> None:
    """Refuse local runs this machine cannot start, before anything is applied: no POSIX
    lock, or an operator setting (spark-submit, its properties file, the artifact root)
    that is unusable.

    Raises:
        CLIError: ``apply_spark_local_unavailable``.
    """
    from fluid_build.build_runners.spark._adapters import SparkRunError
    from fluid_build.build_runners.spark._local import default_host, require_posix, submitter

    try:
        require_posix()
        submitter(default_host(workspace))
    except SparkRunError as exc:
        builds = ", ".join(r.build for r in runs if r.local)
        raise _cli_error(
            "apply_spark_local_unavailable",
            {
                "error": f"Spark build(s) {builds} run on this machine (runtime local), and "
                f"{exc}; nothing was applied",
                "hint": "Set the operator's FLUID_SPARK_SUBMIT_EXECUTABLE, "
                "FLUID_SPARK_SUBMIT_PROPERTIES_FILE and FLUID_SPARK_LOCAL_ROOT in the real "
                "environment (a project .env cannot set them).",
            },
        ) from exc


def refuse_without_opentofu(contract: Mapping[str, Any], engine: str) -> None:
    """Refuse a contract with Spark builds on an AWS runtime on an engine that neither
    provisions nor runs them (the native one, chosen when the contract names several
    providers, or none on AWS): its plan lists a ``spark.reconcile`` that would otherwise
    silently never happen. A build of ``runtime: local`` needs nothing provisioned and runs
    on any engine.

    Raises:
        CLIError: ``apply_spark_needs_opentofu``.
    """
    if not _has_spark(contract):
        return
    from fluid_build.build_runners.spark._desired import spark_builds
    from fluid_build.spark.compiler import LOCAL_RUNTIME, spark_parameters

    try:
        remote = [
            str(b.get("id"))
            for b in spark_builds(contract)
            if (spark_parameters(b) or {}).get("runtime") != LOCAL_RUNTIME
        ]
    except ValueError:
        remote = ["?"]  # a build that does not parse: refused here as before
    if remote:
        raise _cli_error(
            "apply_spark_needs_opentofu",
            {
                "error": f"the {engine} apply engine was chosen, and only the OpenTofu engine "
                f"provisions and runs a Spark build on AWS ({', '.join(remote)}); nothing was "
                "applied",
                "hint": "keep the AWS Spark builds in a contract whose exposes are all on AWS, "
                "or run them with runtime: local on platform: local outputs",
            },
        )


def warn_cluster_posture(
    runs: List[DesiredRun], logger: logging.Logger, *, dry_run: bool, clients: Any = None
) -> None:
    """Warn about each EMR on EC2 cluster that can lose a build's driver or application
    master to Spot (``build_runners/spark/_posture.py``), with the fix. Never refuses and
    never raises; a dry run reads nothing, and a cluster that cannot be read gets a debug line.
    """
    if not any(r.runtime == "emr-ec2" for r in runs):
        return
    if dry_run:
        logger.debug("spark_cluster_posture_skipped reason=dry-run")
        return
    from rich.markup import escape

    from fluid_build.build_runners.spark._posture import cluster_findings
    from fluid_build.cli.console import warning

    try:
        for finding in cluster_findings(runs, clients=clients):
            warning(escape(finding.line()))
            logger.info("spark_cluster_posture", extra={"spark": finding.as_dict()})
    except Exception as exc:  # noqa: BLE001 - a warning never fails plan or apply
        logger.debug("spark_cluster_posture_failed error=%s", type(exc).__name__)


def plan_posture(contract: Mapping[str, Any], logger: logging.Logger, clients: Any = None) -> None:
    """``fluid plan``'s :func:`warn_cluster_posture`, for the runs the plan lists. The
    warnings are not in plan.json: the cluster is its owner's, not the contract's."""
    try:
        runs = _desired(contract, "plan_spark", policy=False) if _has_spark(contract) else []
    except Exception:  # noqa: BLE001 - planned_actions reports a build that does not compile
        logger.debug("spark_cluster_posture_skipped reason=uncompiled")
        return
    warn_cluster_posture(runs, logger, dry_run=False, clients=clients)


def reconcile_after_apply(
    runs: List[DesiredRun],
    logger: logging.Logger,
    *,
    dry_run: bool,
    clients: Any = None,
    workspace: Optional[Path] = None,
    contract: Optional[Mapping[str, Any]] = None,
    build_id: Optional[str] = None,
) -> int:
    """Reconcile the streaming runs after ``tofu apply`` (or the native apply, for local
    runs); 1 when any build failed.

    ``workspace`` is the contract's directory, which holds local runs. With ``contract``
    and no ``build_id`` (the whole contract), this machine's runs of the product's builds
    the contract no longer runs locally are stopped too, even when it has no Spark build
    left."""
    from fluid_build.build_runners.spark._local import LocalHost

    host = LocalHost(Path(workspace)) if workspace is not None else None
    stop_removed_of = _removed_of(host, runs, contract, build_id)
    if not runs and stop_removed_of is None:
        return 0
    if dry_run:
        _say_planned(runs, host, stop_removed_of)
        return 0
    from fluid_build.build_runners.spark._reconcile import reconcile
    from fluid_build.cli.console import cprint

    outcomes = reconcile(
        runs, dry_run=False, clients=clients, local_host=host, stop_removed_of=stop_removed_of
    )
    for out in outcomes:
        cprint(out.line(), markup=False)
        logger.info("spark_reconcile", extra={"spark": out.as_dict()})
        if out.action == "rolled-back":
            logger.error("spark_release_rolled_back", extra={"spark": out.as_dict()})
    return _result(outcomes)


def _result(outcomes: List[Any]) -> int:
    """The apply's exit code once every outcome is said: 1 when any build failed. An outcome
    with an error-catalog event (a release that was not rolled back, or a config that is not
    started again) raises it as ``CLIError``, so its guidance and docs link are printed too;
    one a Ctrl-C cut short stops the apply as an interrupt does."""
    if any(o.interrupted for o in outcomes):
        raise KeyboardInterrupt
    coded = next((o for o in outcomes if o.code), None)
    if coded is not None:
        raise _cli_error(
            str(coded.code),
            {"build": coded.build, "runtime": coded.runtime, "run": coded.run_id or "-"},
        )
    return 1 if any(o.error for o in outcomes) else 0


def reconcile_only(
    contract: Mapping[str, Any],
    logger: logging.Logger,
    *,
    build_id: Optional[str],
    dry_run: bool,
    workspace: Optional[Path] = None,
    confirm: Any = None,
    clients: Any = None,
    run_id: Optional[str] = None,
) -> int:
    """``fluid apply --mode reconcile``: keep the always-on Spark streams running (with
    ``build_id``, that one stream); 1 when any build is an error.

    A stream that died is started again on its checkpoint, within its restart budget, when
    what the last apply deployed is what the contract compiles to; anything else (drift, a
    spent budget, nothing deployed) starts nothing and is an error naming what to run. It
    runs no ``tofu``, cancels only a second copy of a running config, and never stops a
    removed build (a full ``fluid apply`` does). ``dry_run`` reads the same state and
    starts nothing.

    Raises:
        CLIError: ``apply_reconcile_not_a_stream`` (``build_id`` is not an always-on forge
            Spark stream), ``apply_spark_invalid`` / ``apply_spark_refused``,
            ``apply_spark_local_unavailable``, ``apply_aws_sdk_missing``.
    """
    from fluid_build.build_runners.spark._local import LocalHost
    from fluid_build.build_runners.spark._reconcile import reconcile
    from fluid_build.cli.console import cprint

    runs: List[DesiredRun] = []
    if _has_spark(contract):
        stamps = _stamps(contract, run_id, refuse_release=False)
        runs = _desired(contract, "apply_spark", build_id=build_id, stamps=stamps)
    if build_id and not runs:
        raise _cli_error(
            "apply_reconcile_not_a_stream",
            {
                "build_id": build_id,
                "error": f"build {build_id} is not a forge Spark build (engine: spark with "
                "properties.parameters.spark, fluidVersion 0.7.7 or later), and --mode "
                "reconcile keeps those streams running; nothing was started",
                "hint": "Name an always-on Spark stream, or run fluid apply for the rest.",
            },
        )
    if build_id and runs[0].scheduled:
        raise _cli_error(
            "apply_reconcile_not_a_stream",
            {
                "build_id": build_id,
                "error": f"Spark build {build_id} runs to completion (a batch, or a stream "
                "triggered availableNow): its scheduler runs it with fluid apply --mode "
                f"amend-and-build --build-id {build_id}, and --mode reconcile keeps only "
                "always-on streams running; nothing was started",
                "hint": f"Run it with fluid apply --mode amend-and-build --build-id {build_id}, "
                "as its schedule does.",
            },
        )
    runs = [r for r in runs if not r.scheduled]
    if not runs:
        cprint("No always-on Spark stream in the contract: nothing to reconcile.", markup=False)
        return 0
    if any(r.local for r in runs) and not dry_run:
        _check_local(runs, workspace)
    if any(not r.local for r in runs):
        _require_boto3()  # a dry run reads the runs and the deployed job config too
    host = LocalHost(Path(workspace)) if workspace is not None else None
    outcomes = reconcile(
        runs, dry_run=dry_run, only=True, confirm=confirm, clients=clients, local_host=host
    )
    for out in outcomes:
        cprint(out.line(), markup=False)
        logger.info("spark_reconcile", extra={"spark": out.as_dict()})
    return _result(outcomes)


def maintain_only(
    contract: Mapping[str, Any],
    logger: logging.Logger,
    *,
    expose_id: Optional[str],
    dry_run: bool,
    workspace: Optional[Path] = None,
    clients: Any = None,
    run_id: Optional[str] = None,
) -> int:
    """``fluid apply --mode maintain``: one run of each backend-spark table maintenance (with
    ``expose_id``, that table's), started on its writer build's runtime and waited for; 1 when
    any run did not succeed.

    It runs no ``tofu``: the job config it starts is the one the last ``fluid apply`` deployed,
    refused unless it is the one the contract compiles to now (``_adapters.checked_run``). A
    retried task finds its run going and waits for it. A dry run says what it would start and
    reads nothing from AWS. A backend-glue table has nothing to start: AWS runs its optimizers.

    Raises:
        CLIError: ``apply_maintain_not_declared`` (``expose_id`` declares no maintenance),
            ``apply_maintain_invalid``, ``apply_spark_invalid`` / ``apply_spark_refused``,
            ``apply_spark_local_unavailable``, ``apply_aws_sdk_missing``.
        KeyboardInterrupt: Ctrl-C, SIGTERM or SIGHUP while a run was waited on; the run was
            cancelled.
    """
    from fluid_build.build_runners.spark._local import LocalHost
    from fluid_build.build_runners.spark._reconcile import DEFAULT_RUN_WAIT_S, run_once
    from fluid_build.cli.console import cprint
    from fluid_build.maintenance.spec import read_maintenance

    def say(line: str) -> None:
        cprint(line, markup=False)

    tables, errors = read_maintenance(contract)
    if errors:
        raise _cli_error("apply_maintain_invalid", {"error": f"{errors[0]}; nothing was started"})
    chosen = [t for t in tables if expose_id is None or t.expose_id == expose_id]
    if expose_id is not None and not chosen:
        raise _cli_error(
            "apply_maintain_not_declared",
            {
                "expose_id": expose_id,
                "error": f"expose {expose_id} declares no iceberg.maintenance; nothing was started",
                "hint": "Name an expose that declares it, or leave --expose-id out to run the "
                "maintenance of every table that does.",
            },
        )
    for table in chosen:
        if table.backend == "glue":
            say(
                f"Iceberg {table.expose_id}: the Glue Data Catalog's table optimizers maintain "
                "it (fluid apply provisions them, AWS runs them); nothing to start"
            )
    if not any(t.backend == "spark" for t in chosen):
        if not chosen:
            say("No exposes[].iceberg.maintenance in the contract: nothing to run.")
        return 0
    stamps = _stamps(contract, run_id, refuse_release=False)
    runs = _desired(contract, "apply_spark", maintenance=True, expose_id=expose_id, stamps=stamps)
    if any(r.local for r in runs) and not dry_run:
        _check_local(runs, workspace)
    if any(not r.local for r in runs) and not dry_run:
        _require_boto3()
    host = LocalHost(Path(workspace)) if workspace is not None else None
    failed = False
    for run in runs:
        head = f"Iceberg {run.maintains} maintenance (build {run.build}, {run.runtime})"
        if dry_run:
            say(
                f"{head}: would start one run and wait up to {int(DEFAULT_RUN_WAIT_S)} s for it "
                "to end"
            )
            continue
        say(f"{head}:")
        outcome = run_once(
            run, clients=clients, local_host=host, timeout=DEFAULT_RUN_WAIT_S, say=say
        )
        say(outcome.line())
        logger.info(
            "spark_maintain", extra={"spark": {**outcome.as_dict(), "expose": run.maintains}}
        )
        if outcome.action == "cancelled":
            raise KeyboardInterrupt  # the run was cancelled; stop the apply, as Ctrl-C does
        failed = failed or bool(outcome.error)
    return 1 if failed else 0


def _removed_of(
    host: Any, runs: List[DesiredRun], contract: Optional[Mapping[str, Any]], build_id: Any
) -> Optional[Tuple[str, set]]:
    """``(product, builds it runs here)``: this machine's runs of the product's other builds
    are stopped. Only for the whole contract (no ``build_id``), and only when the contract
    runs builds here or this machine has runs of the product."""
    if host is None or contract is None or build_id or not contract.get("id"):
        return None
    from fluid_build.build_runners.spark._local import builds_with_runs

    product = str(contract.get("id"))
    if not runs and not builds_with_runs(host, product):
        return None
    return product, {r.build for r in runs if r.local}


def _say_planned(
    runs: List[DesiredRun], host: Any, stop_removed_of: Optional[Tuple[str, set]]
) -> None:
    """A dry run's lines. It reads nothing from AWS (the application may not exist yet),
    only this machine's run files."""
    from fluid_build.build_runners.spark._reconcile import stop_removed
    from fluid_build.cli.console import cprint

    for run in runs:
        what = (
            "started by a scheduler (fluid apply --mode amend-and-build)"
            if run.scheduled
            else f"would be reconciled to config {run.config_hash[:12]}"
        )
        cprint(f"Spark {run.build} ({run.runtime}): {what}", markup=False)
    if stop_removed_of is not None:
        for out in stop_removed(*stop_removed_of, host, dry_run=True):
            cprint(out.line(), markup=False)
