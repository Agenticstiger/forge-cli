# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The drift pass over the apply's own OpenTofu state, for ``fluid diff`` and
``fluid verify --state-drift``.

``fluid apply`` already refreshes every resource it manages when it plans;
this runs the same refresh without applying anything. For a contract whose
provider is applied through OpenTofu, and whose apply state is reachable
(the workdir and backend ``fluid apply`` resolves, ``FLUID_STATE_BACKEND``
included, through :func:`~fluid_build.cli._apply_opentofu_engine.resolve_state_target`),
it emits the same module the apply would, runs ``tofu plan
-detailed-exitcode -out`` and ``tofu show -json`` on the saved plan, and
classifies the result with :mod:`fluid_build.iac.drift`. The user runs
``fluid``; ``tofu`` stays behind it.

What the pass leaves as it found it: the apply's ``main.tf.json`` is put
back, the saved plan is deleted (it holds every attribute value, secrets
included), and nothing is imported or written to state. ``tofu init``
refreshes ``.terraform/`` exactly as the apply's own init does.

When no state is reachable (no apply has run from here, a local contract,
no ``tofu`` on PATH) the pass reports ``not_checked`` with the reason and
the caller keeps its SDK comparison as the only answer, which is what it
did before this pass existed.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fluid_build.iac import drift as state_drift
from fluid_build.iac.drift import StateDriftReport

from ._common import CLIError, resolve_env_templates_in_contract
from ._logging import info, warn

#: The saved plan's file name inside the workdir; not the apply's ``tfplan``,
#: so a drift check never replaces a plan an apply is about to use.
PLAN_FILE = "fluid-drift.tfplan"

#: Error text lands in the report file and the CI log.
_MAX_DETAIL_CHARS = 500

#: Printed under every checked report: what a state read cannot see.
NOT_IN_STATE = (
    "      Not in the apply's state, so not checked here: what the build writes "
    "into these containers (object contents, object-level encryption of files "
    "written outside the apply), and shared containers this contract only "
    "references{referenced}. The live checks above read targets through the "
    "cloud SDKs where forge has an inspector."
)


#: ``_resolve_provider`` refusals that mean "apply provisions nothing through
#: OpenTofu for this contract", said in one line.
_NO_CLOUD_REASONS = {
    "generate_iac_local_target": "the contract runs on the local engine, which keeps no OpenTofu state",
    "generate_iac_no_provider": "the contract names no cloud that fluid apply provisions",
}


def _not_checked(detail: str, state: Optional[str] = None) -> StateDriftReport:
    return StateDriftReport(status=state_drift.NOT_CHECKED, state=state, detail=detail)


def _error(detail: str, state: Optional[str] = None) -> StateDriftReport:
    return StateDriftReport(status=state_drift.ERROR, state=state, detail=_safe(detail))


def _safe(text: str) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    text = redact_secret_text(" ".join(str(text or "").split()))
    if len(text) > _MAX_DETAIL_CHARS:
        text = "..." + text[-(_MAX_DETAIL_CHARS - 3) :]
    return text


def _cli_error_text(exc: CLIError) -> str:
    context = getattr(exc, "context", None) or {}
    return str(context.get("error") or context.get("detail") or exc.event)


def _local_state_present(workdir: Path) -> bool:
    """Has an apply left local state in ``workdir``?

    ``terraform.tfstate``, or ``terraform.tfstate.d/`` for a ``TF_WORKSPACE``
    other than ``default``. Checked before ``tofu`` runs so a directory no
    apply ever ran in costs nothing and is not created.
    """
    from ._apply_opentofu_engine import LOCAL_STATE_FILE

    return (workdir / LOCAL_STATE_FILE).is_file() or (workdir / "terraform.tfstate.d").is_dir()


def check_state_drift(
    contract: Mapping[str, Any], args: Any, logger: logging.Logger
) -> StateDriftReport:
    """Compare the contract with the apply's refreshed OpenTofu state.

    ``args`` carries what ``fluid apply`` resolves its state from:
    ``provider`` (``--provider`` / ``FLUID_PROVIDER``), ``state_backend``
    (``--state-backend``, default ``FLUID_STATE_BACKEND``) and
    ``workspace_dir``. Never raises: the report's ``status`` says whether it
    compared, and anything unexpected is an ``error`` (could not look), so
    the caller's gate can never read a crash here as drift or as a pass.
    """
    try:
        return _check(contract, args, logger)
    except Exception as exc:  # noqa: BLE001 - reported as "could not compare"
        logger.debug("state drift pass failed", exc_info=True)
        return _error(f"{type(exc).__name__}: {exc}")


def _check(contract: Mapping[str, Any], args: Any, logger: logging.Logger) -> StateDriftReport:
    from fluid_build.iac import get_iac_plugin, resolve_engine, runner

    from ._apply_opentofu_engine import resolve_state_target
    from .generate_iac import _resolve_provider

    resolved = resolve_env_templates_in_contract(copy.deepcopy(dict(contract)))
    try:
        provider = _resolve_provider(resolved, getattr(args, "provider", None) or "auto")
    except CLIError as exc:
        reason = _NO_CLOUD_REASONS.get(exc.event) or _cli_error_text(exc)
        return _not_checked(reason)
    if resolve_engine(None, provider) != "opentofu":
        return _not_checked(f"'{provider}' is applied natively; there is no OpenTofu state")
    plugin = get_iac_plugin(provider)
    if plugin is None:
        return _not_checked(f"no OpenTofu plugin for provider '{provider}'")

    try:
        target = resolve_state_target(args, resolved, provider)
    except CLIError as exc:
        return _error(f"the state backend is not usable: {_cli_error_text(exc)}")
    if target.backend is None and not _local_state_present(target.workdir):
        return _not_checked(
            f"no apply state for this contract at {target.workdir}: run from the directory "
            "`fluid apply` ran in (or pass --workspace-dir), or name the remote state with "
            "--state-backend / FLUID_STATE_BACKEND",
            state=target.location,
        )
    if runner.tofu_path() is None and getattr(args, "ensure_opentofu", False):
        # Same provisioning ``fluid apply --ensure-opentofu`` does: a pinned,
        # SHA-256-verified build, prepended to PATH for this process.
        from fluid_build.iac.opentofu_install import OpenTofuInstallError, ensure_opentofu

        try:
            ensure_opentofu(logger=logger)
        except OpenTofuInstallError as exc:
            return _not_checked(f"OpenTofu could not be provisioned: {exc}", state=target.location)
    if runner.tofu_path() is None:
        return _not_checked(
            "OpenTofu is not installed, so the apply's state was not read "
            "(--ensure-opentofu provisions a pinned build)",
            state=target.location,
        )
    try:
        runner.require_tofu_version()
    except runner.TofuVersionError as exc:
        return _not_checked(str(exc), state=target.location)
    return _plan_and_classify(plugin, resolved, target, logger)


def _plan_and_classify(
    plugin: Any, contract: Mapping[str, Any], target: Any, logger: logging.Logger
) -> StateDriftReport:
    from fluid_build.iac import runner
    from fluid_build.iac.credentials import build_tofu_env

    from ._apply_opentofu_engine import (
        _guard_region_move,
        _reconcile_with_state,
        _tail,
        emit_module,
    )

    workdir: Path = target.workdir
    location = target.location
    module_path = workdir / "main.tf.json"
    previous = module_path.read_bytes() if module_path.is_file() else None
    try:
        try:
            module, _actions = emit_module(plugin, contract, target, logger)
        except CLIError as exc:
            return _error(f"the module could not be emitted: {_cli_error_text(exc)}", location)
        workdir.mkdir(parents=True, exist_ok=True)
        module_path.write_text(module, encoding="utf-8")

        env = build_tofu_env()
        env.update(plugin.credential_env(env))
        init = runner.tofu_init(str(workdir), backend=target.backend is not None, env=env)
        if not init.ok:
            return _error(
                "OpenTofu could not initialise the apply's workdir: "
                + _tail(init.stderr or init.stdout, _MAX_DETAIL_CHARS),
                location,
            )
        # The apply refuses to run when state holds the contract's resources in
        # another region; the refresh would find them gone and this pass would
        # call that "deleted outside the apply".
        try:
            _guard_region_move(plugin, contract, str(workdir), env)
            # The apply's one-time revocation of an older access list's stale
            # grants, so this plan shows what the apply will do.
            _reconcile_with_state(plugin, module_path, str(workdir), env, logger, announce=False)
        except CLIError as exc:
            return _error(_cli_error_text(exc), location)

        plan = runner.tofu_plan_detailed(str(workdir), out_file=PLAN_FILE, env=env)
        if plan.returncode not in (0, runner.PLAN_HAS_CHANGES):
            return _error(
                "the refresh plan failed: " + _tail(plan.stderr or plan.stdout, _MAX_DETAIL_CHARS),
                location,
            )
        doc = runner.tofu_show_plan(str(workdir), plan_file=PLAN_FILE, env=env)
        if doc is None:
            return _error("the saved plan could not be read back", location)
        return state_drift.report_from_plan(doc, state=location, plan_exit_code=plan.returncode)
    finally:
        (workdir / PLAN_FILE).unlink(missing_ok=True)
        if previous is not None:
            module_path.write_bytes(previous)
        elif module_path.exists():
            module_path.unlink()


def is_clean(report: Optional[StateDriftReport]) -> bool:
    """No state pass, one that did not run, or one that found nothing."""
    return report is None or not (report.has_errors or report.has_drift)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def render(report: StateDriftReport, say: Callable[[str], None]) -> None:
    """Print the state pass; ``say`` prints one line as plain text."""
    if report.status == state_drift.NOT_CHECKED:
        say(
            f"State drift check: not run ({report.detail}). Drift comes from the live checks alone."
        )
        return
    if report.status == state_drift.ERROR:
        say(f"State drift check: FAILED ({report.state or 'state unknown'})")
        say(f"      {report.detail}")
        return
    say(
        f"State drift check: {len(report.resources)} resource(s) in the apply's state ({report.state})"
    )
    for resource in report.resources:
        if resource.status != state_drift.MATCH:
            say(f"  {resource.address}  {resource.human()}")
    matched = report.counts()[state_drift.MATCH]
    if matched:
        say(f"  {matched} resource(s) match")
    for resource in report.resources:
        if resource.outside_contract:
            say(
                f"  {resource.address}  also changed outside the apply in attributes the "
                "contract does not declare (apply leaves them, not drift): "
                + state_drift.shown(resource.outside_contract)
            )
    referenced = f" ({', '.join(report.referenced)})" if report.referenced else ""
    say(NOT_IN_STATE.format(referenced=referenced))


def log(report: StateDriftReport, logger: logging.Logger, *, out: Optional[str] = None) -> None:
    """The structured events the run record carries for the state pass."""
    if report.status == state_drift.NOT_CHECKED:
        info(logger, "state_drift_not_checked", detail=report.detail, state=report.state)
        return
    if report.status == state_drift.ERROR:
        warn(logger, "state_drift_check_failed", detail=report.detail, state=report.state)
        return
    pending = report.with_status(state_drift.PENDING)
    if pending:
        info(
            logger,
            "state_drift_changes_pending",
            resources=[r.address for r in pending],
            state=report.state,
        )
    drifted = report.with_status(state_drift.DRIFT)
    if drifted:
        warn(
            logger,
            "state_drift_detected",
            resources=[r.address for r in drifted],
            state=report.state,
            out=out,
        )
    else:
        info(logger, "state_drift_none", resources=len(report.resources), state=report.state)
