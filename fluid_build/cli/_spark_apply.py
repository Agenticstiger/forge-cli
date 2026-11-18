# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The hooks ``fluid plan`` and ``fluid apply`` call for Spark builds.

``fluid plan`` lists a ``spark.reconcile`` action per Spark build (:func:`planned_actions`),
which ``planDigest`` covers. ``fluid apply`` compiles the runs before ``tofu apply``
(:func:`prepare`), refusing a bound plan whose Spark actions differ from them and a missing
boto3, so nothing is provisioned that could not then be started; after a ``tofu apply`` that
succeeded it reconciles the streaming jobs (:func:`reconcile_after_apply`).

A build of ``runtime: local`` runs on the machine ``fluid apply`` runs on, with nothing for
OpenTofu to provision: the native engine (outputs on ``platform: local``) reconciles it the
same way, and :func:`refuse_without_opentofu` refuses only the AWS runtimes there.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fluid_build.build_runners.spark._desired import ACTION, DesiredRun


def _cli_error(event: str, context: Dict[str, Any]) -> Exception:
    from fluid_build.cli._common import CLIError

    return CLIError(1, event, context)


def _desired(contract: Mapping[str, Any], event: str, **kwargs: Any) -> List[DesiredRun]:
    """The runs, or ``CLIError`` ``<event>_refused`` (policy) / ``<event>_invalid``."""
    import copy

    from fluid_build._errors import FluidUserError
    from fluid_build.build_runners.spark._desired import desired_runs
    from fluid_build.cli._common import resolve_env_templates_in_contract

    try:
        # As the OpenTofu engine reads it: ``{{ env.* }}`` resolved, the same at plan and apply.
        resolved = resolve_env_templates_in_contract(copy.deepcopy(dict(contract)))
        return desired_runs(resolved, **kwargs)
    except FluidUserError as exc:
        raise _cli_error(
            f"{event}_refused", {"error": f"{exc.what}. {exc.why}", "hint": exc.fix}
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise _cli_error(f"{event}_invalid", {"error": str(exc)}) from exc


def _has_spark(contract: Mapping[str, Any]) -> bool:
    from fluid_build.build_runners.spark._desired import spark_builds

    try:
        return bool(spark_builds(contract))
    except ValueError:
        return True  # let the compile report it


def planned_actions(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """``fluid plan``'s ``spark.reconcile`` actions (steps renumbered by the caller).

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
    return [dict(_comparable(run.plan_action(0))) for run in runs]


def _comparable(action: Mapping[str, Any]) -> Mapping[str, Any]:
    return {k: v for k, v in action.items() if k not in ("step", "depends_on")}


def prepare(
    contract: Mapping[str, Any],
    *,
    build_id: Optional[str],
    verified_plan: Optional[Mapping[str, Any]],
    dry_run: bool,
    workspace: Optional[Path] = None,
) -> List[DesiredRun]:
    """The runs ``fluid apply`` reconciles, checked before ``tofu apply``. A local run's
    settings are checked against ``workspace``, the contract's directory, which holds it.

    Raises:
        CLIError: ``apply_spark_invalid`` / ``apply_spark_refused`` (the build does not
            compile, or the operator's policy refuses it), ``apply_spark_artifact_mismatch``
            (the bound plan lists other Spark runs), ``apply_aws_sdk_missing``.
    """
    if not _has_spark(contract):
        if verified_plan and any(
            a.get("op") == ACTION
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
    runs = _desired(contract, "apply_spark")
    if verified_plan is not None:
        planned = [
            _comparable(a)
            for a in verified_plan.get("actions") or []
            if isinstance(a, Mapping) and a.get("op") == ACTION
        ]
        if planned != [_comparable(r.plan_action(0)) for r in runs]:
            raise _cli_error(
                "apply_spark_artifact_mismatch",
                {
                    "error": "the Spark runs compiled now differ from the bound plan's "
                    "spark.reconcile actions (job config, runner or run settings, which "
                    "include FLUID_SPARK_MAVEN_REPOSITORY and FLUID_SPARK_GLUE_JARS_URL); "
                    "regenerate it with the same environment as apply"
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
    return 1 if any(o.error for o in outcomes) else 0


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
