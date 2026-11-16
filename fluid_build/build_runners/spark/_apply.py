# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The hooks ``fluid plan`` and ``fluid apply`` call for Spark builds.

``fluid plan`` lists a ``spark.reconcile`` action per Spark build (:func:`planned_actions`),
which ``planDigest`` covers. ``fluid apply`` compiles the runs before ``tofu apply``
(:func:`prepare`), refusing a bound plan whose Spark actions differ from them and a missing
boto3, so nothing is provisioned that could not then be started; after a ``tofu apply`` that
succeeded it reconciles the streaming jobs (:func:`reconcile_after_apply`).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

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
    return [dict(_comparable(run.plan_action(0))) for run in _desired(contract, "plan_spark")]


def _comparable(action: Mapping[str, Any]) -> Mapping[str, Any]:
    return {k: v for k, v in action.items() if k not in ("step", "depends_on")}


def prepare(
    contract: Mapping[str, Any],
    *,
    build_id: Optional[str],
    verified_plan: Optional[Mapping[str, Any]],
    dry_run: bool,
) -> List[DesiredRun]:
    """The runs ``fluid apply`` reconciles, checked before ``tofu apply``.

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
                    "spark.reconcile actions (job config, runner or run settings); regenerate it"
                },
            )
    selected = [r for r in runs if build_id is None or r.build == build_id]
    if not dry_run and any(r.long_running for r in selected):
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
    return selected


def reconcile_after_apply(
    runs: List[DesiredRun], logger: logging.Logger, *, dry_run: bool, clients: Any = None
) -> int:
    """Reconcile the streaming runs after ``tofu apply``; 1 when any build failed."""
    if not runs:
        return 0
    from fluid_build.build_runners.spark._reconcile import reconcile
    from fluid_build.cli.console import cprint

    if dry_run:  # a dry run reads nothing from AWS: the application may not exist yet
        for run in runs:
            what = (
                "started by a scheduler (fluid spark run)"
                if run.scheduled
                else f"would be reconciled to config {run.config_hash[:12]}"
            )
            cprint(f"Spark {run.build} ({run.runtime}): {what}", markup=False)
        return 0
    outcomes = reconcile(runs, dry_run=dry_run, clients=clients)
    for out in outcomes:
        cprint(out.line(), markup=False)
        logger.info("spark_reconcile", extra={"spark": out.as_dict()})
    return 1 if any(o.error for o in outcomes) else 0
