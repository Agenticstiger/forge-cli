# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The OpenTofu apply engine behind ``fluid apply``.

Compiles the contract to a ``.tf.json`` module and delegates to ``tofu``
(init → plan → apply). Engine selection is automatic and per-provider —
the cloud providers route here, ``local`` keeps its native apply; there
is no user-facing engine switch.

Flow: load contract → emit ``.tf.json`` (+ optional remote backend) →
``tofu init`` → ``tofu plan`` (the review point; ``--mode dry-run`` stops
here) → data-loss gate → ``tofu apply``.

The proof gate splits that in two. ``fluid plan --tofu`` runs everything up
to ``tofu plan -out`` and keeps the saved plan next to ``plan.json``, whose
``planDigest`` covers its sha256 (:func:`plan_with_opentofu`). ``fluid apply``
of that ``plan.json`` checks the digest and runs ``tofu apply`` of exactly that
file, with no plan of its own (:func:`_apply_bound_plan`); OpenTofu refuses it
if the state changed since.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.cli.console import cprint, cprint_literal
from fluid_build.iac import build_module, get_iac_plugin, runner, target_identity
from fluid_build.iac.backend import (
    STATE_BACKEND_ENV,
    backend_location,
    legacy_default_backend,
    parse_backend,
    resolve_state_backend_spec,
)
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.credentials import backend_clouds, build_tofu_env, credential_report
from fluid_build.iac.encryption import (
    StateEncryption,
    StateEncryptionError,
    configured_state_encryption,
    encryption_block,
    encryption_env,
    encryption_variables,
    explain_state_failure,
    require_supported_tofu,
)
from fluid_build.iac.module import write_dependency_lock
from fluid_build.iac.naming import safe_ident
from fluid_build.iac.source import contract_source, source_contract_file
from fluid_build.iac.state_migration import (
    PENDING,
    StateMigrationError,
    StateReconciliation,
    other_clouds,
    read_state,
    records_backend,
)
from fluid_build.iac.state_migration import reconcile_state_key as _reconcile_state
from fluid_build.observability.secret_redactor import redact_secret_text

from . import _apply_table_adoption as _table_adoption
from . import _apply_table_hold as _table_hold
from ._apply_cc_report import current_report
from ._common import CLIError, load_contract_with_overlay, resolve_env_templates_in_contract
from ._logging import info, warn
from .generate_iac import _resolve_provider, native_actions


def apply_via_opentofu(
    args,
    logger: logging.Logger,
    contract: Optional[Mapping[str, Any]] = None,
    *,
    applied_state: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Run ``fluid apply`` through the OpenTofu engine. Returns an exit code.

    ``contract`` is the copy ``fluid apply`` already loaded and schema-gated.
    When given it is what ``tofu`` applies (its ``{{ env.* }}`` placeholders
    resolved here) and the file is not re-read, so a source changed after
    the gate cannot reach ``tofu`` unvalidated. Without it the contract is
    loaded from ``args.contract``.

    ``applied_state``, when given, receives the resources of the state a
    successful apply leaves (``runner.tofu_state_resources``, read once and not
    refreshed): the build phase starts the jobs it provisioned by the ids there.
    A dry run applies nothing and leaves it empty.

    Every compile of the run sees the contract's file (``iac/source.py``), so a
    plugin that ships a build's local files finds them as the runners do.
    """
    with contract_source(source_contract_file(getattr(args, "contract", None))):
        return _apply_via_opentofu(args, logger, contract, applied_state)


@dataclass
class _Engine:
    """One run's OpenTofu working state, from :func:`_open_engine` on."""

    contract: Dict[str, Any]
    provider: str
    plugin: Any
    target: "StateTarget"
    module_path: Path
    module: str
    actions: Any
    env: Dict[str, str] = field(repr=False)
    report: Any

    @property
    def workdir(self) -> str:
        return str(self.target.workdir)

    def emit(self, logger: logging.Logger) -> None:
        """Compile the contract for :attr:`target` and write the module."""
        self.module, self.actions = emit_module(self.plugin, self.contract, self.target, logger)
        self.module_path.write_text(self.module, encoding="utf-8")


def _apply_via_opentofu(
    args,
    logger: logging.Logger,
    contract: Optional[Mapping[str, Any]],
    applied_state: Optional[List[Dict[str, Any]]] = None,
) -> int:
    # Plan-binding verification — must run BEFORE any tofu apply so a
    # tampered plan.json is rejected before infra changes. The native
    # apply path (cli/apply.py::_verify_plan_digests) had this gate; the
    # OpenTofu cutover initially bypassed it, which silently disabled
    # plan-binding for every cloud provider that cut over (aws / gcp /
    # snowflake). This is the same gate, replicated here so the
    # OpenTofu path matches the native path's stage-7 guarantee.
    plan_data = _verify_plan_binding_for_opentofu(args, logger)
    # A plan.json from ``fluid plan --tofu`` binds a saved OpenTofu plan: read
    # once and checked here, before init, and those bytes are what is applied.
    bound = _bound_tofu_plan(args, plan_data, logger)

    eng = _open_engine(args, logger, contract, bound=bound[0] if bound else None)
    # Consumed subjects whose registry needs a credential: plan leaves them to apply,
    # which resolves it. Before any tofu apply, saved plan or not (schema_registry/verify.py).
    _verify_registry_at_apply(
        eng.contract, eng.provider, dry_run=bool(getattr(args, "dry_run", False))
    )
    if bound is not None:
        return _apply_bound_plan(eng, args, logger, *bound, applied_state=applied_state)

    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        # A dry-run is plan only and writes no state, so it never moves any
        # either: while the move is pending it plans against the old key, the
        # read-only path ``fluid diff`` takes (``read_target``). The copy is
        # left to the first real apply, which a plan-only CI role with
        # read-only state access never runs.
        read = read_target(eng.target, eng.provider, eng.env, logger)
        if read is not eng.target:
            eng.target = read
            eng.emit(logger)
            init = runner.tofu_init(eng.workdir, backend=True, env=eng.env, reconfigure=True)
            if not init.ok:
                raise CLIError(
                    1, "opentofu_init_failed", {"error": _tail(init.stderr or init.stdout)}
                )
    else:
        # State a previous release kept at the key without the provider moves
        # to this provider's key (OpenTofu's own ``init -migrate-state``), so
        # the first apply after the upgrade does not plan every resource as
        # new (see ``iac.state_migration``). Before anything reads the state.
        reconcile_state_key(eng.target, eng.provider, eng.env, logger, migrate=True)

    _guard_before_plan(eng, args, logger)
    _table_adoption.before_lookup(eng.plugin, eng.provider, eng.contract, eng.actions, args)

    # What this run imports stays in state only once its ``tofu apply`` starts
    # (:func:`_imports_kept_only_by_an_apply`).
    with _imports_kept_only_by_an_apply(eng.workdir, eng.env, logger) as imported:
        # A table's look-up is strict under --adopt-tables (see _adopt_existing).
        adopted = _adopt_existing(
            eng.plugin,
            eng.contract,
            eng.actions,
            eng.workdir,
            eng.env,
            logger,
            imported.addresses,
            strict_tables=_table_adoption.asked(args),
        )
        tables = _tables_through_import_blocks(eng, args, adopted, imported)

        plan = runner.tofu_plan(eng.workdir, env=eng.env)
        if not plan.ok:
            raise _plan_failure(eng.plugin, eng.contract, plan, eng.workdir, eng.env)
        changes = runner.change_summary(plan)
        cprint(f"\n  tofu plan: +{changes['add']} ~{changes['change']} -{changes['remove']}")
        shown = _shown_for_review(eng, changes, tables=tables)
        # A live Iceberg table the plan would update in place is left as it is, unless
        # --allow-iceberg-update (``_apply_table_hold``), whatever brought it in.
        held = _table_hold.to_hold(args, _table_hold.planned_updates(eng.plugin, eng.module, shown))
        # Before the data-loss gate: a table that differs is refused naming what differs.
        adoption = _table_adoption.check_plan(
            eng.plugin, eng.module, tables, _read(shown), logger, held=held
        )
        plan, changes, shown = _plan_holding_tables(
            eng, logger, held, adoption, plan, changes, shown
        )
        _record_planned(eng, changes, dry_run)
        destroyed, updated = _review_plan(
            eng, args, logger, changes, runner.planned_removals(plan), shown=shown
        )
        if dry_run:
            _preview_outside_opentofu(eng, logger, destroyed, changes)
            return 0
        # Access enforcement the cloud keeps outside OpenTofu (Unity Catalog column masks
        # and row filters) goes onto the tables that already exist BEFORE the apply, so a
        # grant the apply adds lands on a protected table. A failure stops the run here:
        # nothing has been applied.
        _enforce_access(eng.plugin, "pre_apply", eng.contract)
        _prepare_destroys(eng.plugin, eng.contract, destroyed)
        imported.keep()

    _table_adoption.audit_take_over(logger, adoption.reviews)
    _table_hold.audit_updated(logger, updated)
    return _finish_apply(eng, logger, changes, destroyed, applied_state)


def _tables_through_import_blocks(
    eng: _Engine, args, adopted: List[Any], imported: Any
) -> List[Any]:
    """The existing tables forge did not make that ``fluid apply`` found, moved from state
    into ``import`` blocks.

    The brownfield look-up (:func:`_adopt_existing`) imports a table to learn that it
    exists. Without ``--adopt-tables`` the run stops here (``_apply_table_adoption``);
    with it the table goes back out of state and into the module as an ``import`` block,
    so the plan shows its import and the check reads it from the saved plan. The other
    imports, a table forge made among them, stay in state for the apply, as before.
    """
    found = _table_adoption.foreign_tables(eng.plugin, adopted, eng.workdir, eng.env, eng.contract)
    _table_adoption.refuse_unless_asked(args, found, eng.contract)
    tables = found.tables
    if not tables:
        return []
    addresses = [block.to for block in tables]
    released = runner.tofu_state_rm(eng.workdir, addresses, env=eng.env)
    if not released.ok:
        # Still held, so the run's own hand-back tries again as it stops.
        raise CLIError(
            1,
            "plan_tofu_adoption_not_released",
            {
                "error": "the tables to adopt could not be released from state for their "
                "import blocks: " + _tail(released.stderr or released.stdout, 200),
                "remediation": [
                    f"tofu -chdir={shlex.quote(eng.workdir)} state rm " + " ".join(addresses),
                    "then re-run",
                ],
            },
        )
    for address in addresses:
        imported.addresses.remove(address)
    _table_adoption.write_import_blocks(eng.module_path, tables)
    return tables


def _plan_holding_tables(
    eng: _Engine,
    logger: logging.Logger,
    held: Mapping[str, Any],
    adoption: Any,
    plan: Any,
    changes: Dict[str, int],
    shown: Any,
) -> Tuple[Any, Dict[str, int], Any]:
    """``(plan, changes, shown)`` the run goes on with: the same plan, or, when it would
    update in place a table the plugin keeps as it is (a live Iceberg table), the plan made
    again with the held attributes in that table's ``ignore_changes``, checked to leave it
    with no change at all (``_apply_table_hold``). ``shown`` is the plan the run goes on
    with as ``tofu show -json`` prints it (:func:`_shown_for_review`)."""
    if not _table_hold.hold_still(eng.module_path, held):
        return plan, changes, shown
    again = runner.tofu_plan(eng.workdir, out_file=_WORKDIR_PLAN, env=eng.env)
    if not again.ok:
        raise _plan_failure(eng.plugin, eng.contract, again, eng.workdir, eng.env)
    changes = runner.change_summary(again)
    cprint(
        f"  tofu plan: +{changes['add']} ~{changes['change']} -{changes['remove']} "
        "(again: live Iceberg tables left as they are)"
    )
    shown = runner.tofu_show_plan(eng.workdir, plan_file=_WORKDIR_PLAN, env=eng.env)
    _table_hold.check_held(shown, held, adopted=adoption.addresses)
    _table_hold.say_held(logger, held)
    return again, changes, shown


#: A plan no review has read back yet (``None`` is one ``tofu show -json`` could not read).
_UNREAD: Any = object()


def _shown_for_review(
    eng: _Engine, changes: Mapping[str, int], shown: Any = _UNREAD, *, tables: Sequence[Any] = ()
) -> Any:
    """The saved plan as ``tofu show -json`` prints it, read once for the reviews that need
    it: the tables to adopt, and an in-place update the plugin may hold
    (``_apply_table_hold.may_hold``). ``shown`` as it is when a review read it already, and
    :data:`_UNREAD` when none needs it."""
    if shown is not _UNREAD or not (tables or _table_hold.may_hold(eng.plugin, changes)):
        return shown
    return runner.tofu_show_plan(eng.workdir, plan_file=_WORKDIR_PLAN, env=eng.env)


def _read(shown: Any) -> Optional[Dict[str, Any]]:
    """``shown`` for a reader that takes a plan or ``None``."""
    return None if shown is _UNREAD else shown


def _verify_registry_at_apply(
    contract: Optional[Mapping[str, Any]], provider: str, *, dry_run: bool
) -> None:
    """``schema_registry.verify.verify_at_apply``; function-local: it reaches httpx."""
    from fluid_build.schema_registry.verify import verify_at_apply

    verify_at_apply(contract or {}, dry_run=dry_run, provider=provider)


def _open_engine(
    args,
    logger: logging.Logger,
    contract: Optional[Mapping[str, Any]],
    *,
    bound: Optional[Mapping[str, Any]] = None,
) -> _Engine:
    """Everything up to and including ``tofu init``, shared by ``fluid apply`` and
    ``fluid plan --tofu``: the contract, the plugin, a usable ``tofu``, the state
    target, the module and the provider credentials.

    ``bound`` is the ``opentofuPlan`` record of a saved plan being applied: one
    made for another provider or state is refused before anything is initialised.
    """
    contract = _load_contract(args, logger, contract)
    provider = _resolve_provider(contract, getattr(args, "provider", None) or "auto")
    report = current_report()
    if report is not None:
        # Known from here on, so a refusal before the run is registered (a
        # sovereignty refusal in the emitter, an init that fails) still
        # reaches the Command Center with its product, version and platform.
        # A run refused earlier is described from the base contract instead
        # (``ApplyRunReport.finish``).
        report.identify(contract=contract, provider=provider, environment=_applied_env(args))

    plugin = get_iac_plugin(provider)
    if plugin is None:
        from fluid_build.iac.registry import iac_plugin_unavailable_reason

        why = iac_plugin_unavailable_reason(provider)
        raise CLIError(
            1,
            "opentofu_engine_unsupported_provider",
            {
                "error": f"no OpenTofu plugin for provider {provider!r}"
                + (f": {why}" if why else "")
            },
        )
    if runner.tofu_path() is None:
        # `tofu` isn't on PATH. When the operator opted in (--ensure-opentofu,
        # which `fluid generate ci` bakes into the apply stage), provision a
        # pinned, SHA-256-verified build with no root/gpg needed; otherwise
        # fail loud and point at the flag.
        if getattr(args, "ensure_opentofu", False):
            from fluid_build.iac.opentofu_install import OpenTofuInstallError, ensure_opentofu

            try:
                ensure_opentofu(logger=logger)
            except OpenTofuInstallError as exc:
                raise CLIError(1, "opentofu_engine_install_failed", {"error": str(exc)})
        if runner.tofu_path() is None:
            raise CLIError(
                1,
                "opentofu_engine_no_tofu",
                {
                    "error": "the `tofu` binary is required to provision cloud "
                    "infrastructure — re-run with `--ensure-opentofu` to auto-"
                    "provision a pinned, verified build, or install it from "
                    "https://opentofu.org/docs/intro/install/"
                },
            )
    # Fail loud if `tofu` is older than the supported floor — a stale
    # binary would otherwise be discovered only mid-apply, after partial
    # state has been mutated. See ``runner.require_tofu_version`` for the
    # version floor.
    try:
        runner.require_tofu_version()
    except runner.TofuVersionError as exc:
        raise CLIError(
            1,
            "opentofu_engine_unsupported_tofu_version",
            {"error": str(exc)},
        )

    target = resolve_state_target(args, contract, provider)
    if bound is not None:
        _check_plan_target(bound, provider, target)
    backend, backend_origin, workdir = target.backend, target.origin, target.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    eng = _Engine(
        contract=contract,
        provider=provider,
        plugin=plugin,
        target=target,
        module_path=workdir / "main.tf.json",
        module="",
        actions=(),
        env={},
        report=report,
    )
    eng.emit(logger)
    # The provider version and checksums `tofu init` must accept, for a plugin
    # that ships a committed lock (iac/module.py::dependency_lock).
    write_dependency_lock(plugin, workdir)
    if bound is not None:
        # A saved plan installs the providers it was made with, not today's newest.
        _install_plan_lock(args, bound, workdir)

    env = _tofu_env(plugin, target)
    eng.env = env
    present, _absent = credential_report(plugin, env)

    cprint(f"\nOpenTofu engine — provider: {provider}")
    cprint(f"  module:      {eng.module_path}")
    # The resolved object, not just the backend type: the same bucket-only
    # spec keys state differently from the flag and from FLUID_STATE_BACKEND,
    # and a run that lands on an empty state re-plans every resource as new.
    state_line = ("remote: " + backend_location(backend)) if backend else "local"
    if backend_origin != "default":
        state_line += f" (from {backend_origin})"
    cprint(f"  state:       {state_line}")
    if target.encryption is not None:
        cprint(f"  encryption:  {target.encryption.describe()}")
    cprint(f"  credentials: {', '.join(present) if present else 'none detected in environment'}")

    # The Command Center hears about the run now that the product, the
    # provider and the environment are known (best effort; see
    # cli/_apply_cc_report.py). Addresses and counts only, never tofu output.
    if report is not None:
        report.begin(
            contract=contract,
            provider=provider,
            environment=_applied_env(args),
            state=target.location,
        )

    init = runner.tofu_init(
        str(workdir),
        backend=backend is not None,
        env=env,
        reconfigure=recorded_legacy_backend(target),
    )
    if not init.ok:
        raise _encryption_failure(init.stderr or init.stdout) or CLIError(
            1, "opentofu_init_failed", {"error": _tail(init.stderr or init.stdout)}
        )
    return eng


def _guard_before_plan(eng: _Engine, args, logger: logging.Logger) -> None:
    """The read-only checks against the state that run before every ``tofu plan``."""
    # One state, two clouds: a key that does not name the provider (the
    # shared ``fluid/terraform.tfstate`` a bucket-only --state-backend gives a
    # contract without packaging, or an explicit key used for both) can hold
    # the other cloud's resources, which this plan would destroy.
    guard_state_shared_with_another_cloud(eng.target, eng.provider, eng.env)

    # Pre-plan region guard. A module now pins the region its bindings name,
    # and moving a contract's resources to it would not show as a destroy.
    _guard_region_move(eng.plugin, eng.contract, eng.workdir, eng.env)

    # A dataset an older forge-cli applied with an authoritative access list has
    # its stale grants revoked once, before its grants become member resources.
    _reconcile_with_state(eng.plugin, eng.module_path, eng.workdir, eng.env, logger)

    # Pre-plan ownership-transition guard (RFC-packaging-modes.md file 10).
    # Runs BEFORE _adopt_existing — brownfield adoption is precisely the
    # mechanism that would re-own a shared pool — and before `tofu plan`,
    # which is where an ownership flip would otherwise first surface as a
    # destroy.
    _guard_packaging_transitions(
        eng.contract, eng.workdir, eng.env, args, logger, plugin=eng.plugin, actions=eng.actions
    )

    # Pre-plan Iceberg catalog-move guard: what an older forge-cli created for
    # an Iceberg table that lives in Lakekeeper / a REST catalog (Glue resources
    # on AWS, an EXTERNAL VOLUME on Snowflake) would otherwise be planned for
    # DESTROY (iac/catalog_moves.py).
    _guard_catalog_moves(
        eng.plugin, eng.contract, eng.provider, eng.workdir, eng.env, logger, actions=eng.actions
    )


def _record_planned(eng: _Engine, changes: Mapping[str, int], dry_run: bool) -> None:
    if eng.report is not None:
        eng.report.record_infra(
            planned=changes,
            applied=None,
            resources=_module_addresses(eng.module),
            dry_run=dry_run,
        )


def _review_plan(
    eng: _Engine,
    args,
    logger: logging.Logger,
    changes: Mapping[str, int],
    removals: List[Tuple[str, str]],
    *,
    enforce: bool = True,
    shown: Any = _UNREAD,
    recorded: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """What the saved plan in the workdir does beyond its counts, then the data-loss gate.

    Returns the plan's deletes for the plugin's ``prepare_destroys``, and the in-place
    updates of live Iceberg tables the run may make (``_apply_table_hold.review``;
    ``recorded``: those a saved plan's record names). With ``enforce=False`` (``fluid plan
    --tofu``) the gates only say what the apply will refuse: the flags belong to the apply.
    """
    # Revoking a grant destroys a member resource and loses no data: only the
    # removals of data-bearing resources reach the data-loss gate.
    data_changes, revoked = _data_bearing_changes(changes, removals, eng.plugin)
    if revoked:
        cprint(
            f"  {len(revoked)} access grant(s), policy tag(s) or tag assignment(s) removed "
            "(access or metadata only, no data lost; not gated): " + ", ".join(revoked)
        )
    retired = _retired_definitions(changes, removals)
    if retired:
        cprint(
            f"  {len(retired)} job(s), workspace file(s), folder(s) or data quality "
            "monitor(s) removed (no table data lost; not gated; a job's run history and "
            "what a monitor has learned go with them): " + ", ".join(retired)
        )
    built = _build_definitions(eng.plugin, changes, removals)
    if built:
        cprint(
            f"  {len(built)} resource(s) a removed build ran with (its artifacts, role, log "
            "group and job; no table data lost; not gated; its logs go with them): "
            + ", ".join(built)
        )
    _report_plan_notices(eng.plugin, eng.contract, logger)
    shown = _shown_for_review(eng, changes, shown)
    # In-place updates that delete data — a dropped column — which the destroy
    # count above never sees (``~`` in the summary, not ``-``).
    lost_in_place = _in_place_changes(eng.plugin, changes, eng.workdir, eng.env, shown)

    # Report what the plan's ``lifecycle.ignore_changes`` deliberately hides.
    # Without this, a contract whose column types no longer match the live
    # table plans clean and applies with exit 0 — including under the
    # destructive ``--mode replace``, where the operator reasonably reads
    # "apply complete" as "the table now matches the contract".
    _report_suppressed_drift(eng.plugin, eng.contract, eng.workdir, eng.env, args, logger)
    # The backstop of the Iceberg hold: a live Iceberg table the plan still updates in place.
    updated = _table_hold.review(
        eng.plugin, eng.module, shown, args, changes, recorded=recorded, enforce=enforce
    )
    if not enforce:
        _say_data_loss_ahead(data_changes, lost_in_place)
        return [], updated
    _enforce_data_loss_gate(args, logger, eng.provider, changes, data_changes, lost_in_place)
    return _planned_destroys(eng.plugin, changes, eng.workdir, eng.env), updated


def _say_data_loss_ahead(data_changes: Mapping[str, int], lost_in_place: List[str]) -> None:
    """``fluid plan --tofu``: say whether the apply will need ``--allow-data-loss``."""
    if _data_loss_blocked(data_changes, False) or lost_in_place:
        cprint(
            "  ⚠️  this plan destroys or changes existing data: `fluid apply` will refuse "
            "it unless given --allow-data-loss"
        )


def _preview_outside_opentofu(
    eng: _Engine,
    logger: logging.Logger,
    destroyed: List[Dict[str, Any]],
    changes: Mapping[str, int],
) -> None:
    """A dry run: print what the apply would do outside OpenTofu, and stop."""
    _enforce_access(eng.plugin, "pre_apply", eng.contract, dry_run=True)
    _prepare_destroys(eng.plugin, eng.contract, destroyed, dry_run=True)
    _converge_output_ports(eng.plugin, eng.contract, dry_run=True)
    cprint("\ndry-run: plan only — not applying.")
    info(logger, "opentofu_apply_dry_run", provider=eng.provider, **changes)


def _finish_apply(
    eng: _Engine,
    logger: logging.Logger,
    changes: Mapping[str, int],
    destroyed: List[Dict[str, Any]],
    applied_state: Optional[List[Dict[str, Any]]],
    *,
    bound: bool = False,
) -> int:
    """``tofu apply`` of the saved plan in the workdir, and what follows it."""
    plugin, contract, workdir, env = eng.plugin, eng.contract, eng.workdir, eng.env
    # ``fluid apply`` is the headline state-changing command, so a receiver
    # configured with the standard OPENLINEAGE_URL must see it. Emission was
    # previously wired only into the acquisition runners, so a real Snowflake
    # apply produced zero events. Opt-in and zero-cost when unconfigured
    # (``resolve_lineage_emitter`` returns the null emitter), and every
    # emit call soft-fails — lineage never breaks an apply.
    with _apply_lineage(contract, provider=eng.provider, planned=changes) as record:
        apply_result, waited = _apply_saved_plan(
            plugin, contract, destroyed, workdir, env, resume=not bound
        )
        if not apply_result.ok:
            if bound and _saved_plan_stale(apply_result):
                # OpenTofu checked the state against the plan's snapshot and refused:
                # nothing was applied, so there is nothing to bind or enforce.
                raise _stale_plan_error(apply_result)
            # A table the failed apply created, its grants and the job that fills it can all
            # exist with no mask or filter: bind what exists (never drop) before failing.
            _enforce_after_failed_apply(plugin, contract)
            logger.debug(
                "opentofu apply raw output tail: %s",
                _tail(apply_result.stderr or apply_result.stdout),
            )
            raise _apply_failure(apply_result, workdir, plugin)
        applied = _applied(changes, apply_result, waited)
        record(applied)

    cprint(f"\n  tofu apply complete: +{applied['add']} ~{applied['change']} -{applied['remove']}")
    if eng.report is not None:
        eng.report.record_infra(
            planned=changes, applied=applied, resources=_module_addresses(eng.module), dry_run=False
        )

    # The tables this apply created exist now, empty, and get their masks and row filter here.
    # A dbt job that writes one was created paused in this same apply (the plugin's ``emit``
    # reads the live bindings), and the next apply starts it once they are in place, so no
    # build fills a table first. A table that cannot be bound is closed to the principals it
    # restricts (a deny-all row filter, else revoked), and the run fails.
    _enforce_access(plugin, "post_apply", contract)
    # Output ports the cloud keeps outside OpenTofu (a Databricks Genie space) come last, on
    # tables that exist and are protected.
    _converge_output_ports(plugin, contract)

    if applied_state is not None:
        # After the bindings: a job the build phase starts writes tables already protected.
        applied_state.extend(runner.tofu_state_resources(workdir, env=env))
        if not applied_state:  # an apply that provisioned a job leaves a state with it
            cprint(
                f"  ⚠️  could not read the applied state back (tofu show -json in {workdir}): "
                "the build phase cannot find the jobs this apply provisioned."
            )

    info(logger, "opentofu_apply_ok", provider=eng.provider, **applied)
    return 0


# ---------------------------------------------------------------------------
# The saved OpenTofu plan: ``fluid plan --tofu`` writes it, ``fluid apply`` runs it
# ---------------------------------------------------------------------------

#: The workdir file ``tofu plan -out`` writes and ``tofu apply`` reads.
_WORKDIR_PLAN = "tfplan"


def plan_with_opentofu(
    args, logger: logging.Logger, contract: Mapping[str, Any], out: str
) -> Dict[str, Any]:
    """``fluid plan --tofu``: run ``tofu plan -out`` as ``fluid apply`` would, and keep it.

    The module, the state target and every check before the plan are the
    apply's own (:func:`_open_engine`, :func:`_guard_before_plan`). Next to the
    ``plan.json`` at ``out`` it writes, owner-only (0600):

    - ``<stem>.tfplan``: the saved plan ``fluid apply <out>`` applies, unchanged;
    - ``<stem>.tfplan.json``: that plan as ``tofu show -json`` prints it;
    - ``<stem>.gitlab-tofu.json``: the GitLab ``artifacts:reports:terraform``
      summary (:func:`runner.gitlab_plan_report`).

    Returns the ``opentofuPlan`` record the caller puts into the plan before its
    digests, so ``planDigest`` covers the saved plan's sha256.

    Brownfield adoption cannot write state here: a saved plan carries the state it
    was made from, and OpenTofu refuses it once the state has changed. So what
    :func:`_adopt_existing` imports is handed back at once, and adopted through
    ``import`` blocks in the plan instead: the reviewer sees it, the apply does it.
    """
    with contract_source(source_contract_file(getattr(args, "contract", None))):
        return _plan_with_opentofu(args, logger, contract, Path(out))


def _plan_with_opentofu(
    args, logger: logging.Logger, contract: Mapping[str, Any], out: Path
) -> Dict[str, Any]:
    from fluid_build.forge.core.plan_digest import sha256_bytes
    from fluid_build.iac.importer import ImportBlock

    # Before anything reads or writes state: the files go next to plan.json.
    out.parent.mkdir(parents=True, exist_ok=True)
    eng = _open_engine(args, logger, contract)
    reconcile_state_key(eng.target, eng.provider, eng.env, logger, migrate=True)
    _guard_before_plan(eng, args, logger)
    _table_adoption.before_lookup(eng.plugin, eng.provider, eng.contract, eng.actions, args)

    with _imports_kept_only_by_an_apply(eng.workdir, eng.env, logger) as imported:
        # A table's look-up is strict under --adopt-tables (see _adopt_existing).
        adopted: List[ImportBlock] = _adopt_existing(
            eng.plugin,
            eng.contract,
            eng.actions,
            eng.workdir,
            eng.env,
            logger,
            imported.addresses,
            strict_tables=_table_adoption.asked(args),
        )
        # Read while the look-up's imports are in state: a table forge did not make stops
        # the plan unless it was asked to adopt it; the hand-back runs first, as it stops.
        found = _table_adoption.foreign_tables(
            eng.plugin, adopted, eng.workdir, eng.env, eng.contract
        )
        _table_adoption.refuse_unless_asked(args, found, eng.contract)
        tables = found.tables
    _refuse_unreleased_imports(eng, adopted)
    if adopted:
        # Escaped as the module is: an import id carries contract text.
        _table_adoption.write_import_blocks(eng.module_path, adopted)
        cprint(f"  brownfield:  the saved plan adopts {len(adopted)} pre-existing resource(s)")

    plan = runner.tofu_plan(eng.workdir, out_file=_WORKDIR_PLAN, env=eng.env)
    if not plan.ok:
        raise _plan_failure(eng.plugin, eng.contract, plan, eng.workdir, eng.env)
    changes = runner.change_summary(plan)
    cprint(f"\n  tofu plan: +{changes['add']} ~{changes['change']} -{changes['remove']}")
    shown = runner.tofu_show_plan(eng.workdir, plan_file=_WORKDIR_PLAN, env=eng.env)
    if shown is None:
        raise CLIError(
            1,
            "plan_tofu_show_failed",
            {"error": f"`tofu show -json {_WORKDIR_PLAN}` failed in {eng.workdir}"},
        )
    held = _table_hold.to_hold(args, _table_hold.planned_updates(eng.plugin, eng.module, shown))
    # Before the data-loss reading, and before any file is written next to plan.json.
    adoption = _table_adoption.check_plan(eng.plugin, eng.module, tables, shown, logger, held=held)
    plan, changes, shown = _plan_holding_tables(eng, logger, held, adoption, plan, changes, shown)
    _, updated = _review_plan(
        eng, args, logger, changes, runner.planned_removals(plan), enforce=False, shown=shown
    )

    saved = (Path(eng.workdir) / _WORKDIR_PLAN).read_bytes()
    os.chmod(Path(eng.workdir) / _WORKDIR_PLAN, 0o600)
    target = _target_record(eng)
    cprint_literal(f"  target:      {target_identity.describe(eng.provider, target['identity'])}")
    for field_name, why in sorted(target["unread"].items()):
        cprint_literal(f"               {field_name} not bound: {why}")
    _say_provider_unbound(eng, logger, "plan_tofu_target_provider_unbound")
    stem = out.name[: -len(".json")] if out.name.endswith(".json") else out.name
    names = {
        "planFile": f"{stem}.tfplan",
        "showJson": f"{stem}.tfplan.json",
        "gitlabReport": f"{stem}.gitlab-tofu.json",
    }
    _write_private(out.parent / names["planFile"], saved)
    # ``tofu show -json`` holds every sensitive value in plain text (the json-format
    # docs mark them in ``*_sensitive``, they do not mask them): owner-only, like the
    # temporary file gitlab-tofu.sh keeps it in.
    _write_private(out.parent / names["showJson"], (json.dumps(shown) + "\n").encode())
    _write_private(out.parent / names["gitlabReport"], runner.gitlab_plan_report(shown).encode())
    # File names, not the directory: a long path wrapped mid-name in a terminal.
    # The provider versions and checksums this plan was made with travel with it: the
    # apply's ``tofu init`` installs exactly these (OpenTofu honours a lock it finds),
    # so another runner, days later, does not pick a newer provider release.
    lock = _read_lock(eng.workdir)
    if lock is not None:
        names["lockFile"] = f"{stem}{_LOCK_FILE}"
        _write_private(out.parent / names["lockFile"], lock)
    cprint_literal(f"  saved plan:  {', '.join(names.values())} (owner-only, next to {out.name})")
    return {
        **names,
        **({"lockSha256": sha256_bytes(lock)} if lock is not None else {}),
        "sha256": sha256_bytes(saved),
        "provider": eng.provider,
        "state": eng.target.location,
        **_state_version(eng),
        "changes": changes,
        "imports": sorted(block.to for block in adopted),
        # Under planDigest: an approval of plan.json is an approval of these take-overs.
        **(
            {"adoptedTables": sorted(review.table for review in adoption.reviews)}
            if adoption.reviews
            else {}
        ),
        # Under planDigest too: the live Iceberg tables it updates in place, as asked.
        **_table_hold.record_field(updated),
        "target": target,
    }


#: Where ``tofu init`` writes the providers it selected, in the workdir.
_LOCK_FILE = ".terraform.lock.hcl"

#: ``provider "<source>" {`` then, inside the block, ``version = "<version>"``.
_LOCKED_PROVIDER_RE = re.compile(r'provider\s+"([^"]+)"\s*\{[^}]*?\bversion\s*=\s*"([^"]+)"', re.S)


def _read_lock(workdir: str) -> Optional[bytes]:
    """The workdir's dependency lock, or ``None`` when ``tofu init`` wrote none (a module
    using only built-in resources)."""
    try:
        return (Path(workdir) / _LOCK_FILE).read_bytes()
    except OSError:
        return None


def _locked_providers(workdir: str) -> Dict[str, str]:
    """``{source: version}`` from the workdir's dependency lock; ``{}`` when there is none."""
    lock = _read_lock(workdir)
    if lock is None:
        return {}
    return dict(sorted(_LOCKED_PROVIDER_RE.findall(lock.decode("utf-8", "replace"))))


def _install_plan_lock(args, record: Mapping[str, Any], workdir: Path) -> None:
    """Put the saved plan's dependency lock into the workdir before ``tofu init``.

    The lock is read from next to ``plan.json`` and must be the bytes the plan's record
    names (``lockSha256``, under ``planDigest``). A plan made without one installs none.
    ``--no-verify-plan-binding`` skips the digest, not the file: without it there is no
    reviewed provider set to install."""
    from fluid_build.forge.core.plan_digest import sha256_bytes

    name = record.get("lockFile")
    if name is None:
        return
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or Path(name).name != name
        or "\\" in name
        or "\x00" in name
    ):
        raise _target_error(
            "tofu-plan-invalid",
            f"the saved plan's lockFile {name!r} is not a file name next to plan.json",
            "Re-run `fluid plan --tofu`.",
        )
    try:
        lock = (Path(str(args.contract)).parent / name).read_bytes()
    except OSError:
        raise _target_error(
            "tofu-plan-lock-missing",
            f"the saved plan's dependency lock {name} is not next to plan.json, so the "
            "provider versions it was reviewed with cannot be installed",
            f"Hand {name} over with plan.json and the saved plan, or re-run `fluid plan --tofu`.",
        )
    if not getattr(args, "no_verify_plan_binding", False) and sha256_bytes(lock) != record.get(
        "lockSha256"
    ):
        raise _target_error(
            "tofu-plan-lock-mismatch",
            f"{name} is not the dependency lock the plan was made with (its sha256 differs)",
            "Hand over the lock written with the plan, or re-run `fluid plan --tofu`.",
        )
    _write_private(workdir / _LOCK_FILE, lock)


def _target_record(eng: _Engine) -> Dict[str, Any]:
    """What a saved plan is bound to besides its state: the cloud target as the provider
    reads it (:mod:`fluid_build.iac.target_identity`), the backend block, the ``tofu``
    version and the provider versions ``tofu init`` locked. Read after ``tofu init``,
    on the plan's runner and again on the apply's, with the run's own environment."""
    from fluid_build.forge.core.plan_digest import sha256_bytes

    try:
        module = json.loads(eng.module)  # the emitted module: its provider block is static
    except ValueError:
        module = {}
    identity, unread = target_identity.target_identity(eng.provider, module, eng.env)
    version = runner.tofu_version()
    backend = eng.target.backend
    return {
        "identity": identity,
        "unread": unread,
        "backend": (
            sha256_bytes(json.dumps(backend, sort_keys=True, separators=(",", ":")).encode())
            if backend
            else ""
        ),
        "tofu": ".".join(str(part) for part in version) if version else "",
        "providers": _locked_providers(eng.workdir),
    }


def _refuse_another_target(
    eng: _Engine, args, logger: logging.Logger, record: Mapping[str, Any]
) -> None:
    """Refuse a saved plan applied to another target than it was made for, before anything
    runs: the cloud account, project, region or host, the backend, ``tofu`` or a provider.

    OpenTofu applies a saved plan on whatever account the apply's credentials belong to;
    this reads the target again here and compares it with the plan's. A field the plan
    recorded and this run cannot read fails closed; one the plan could not read is not
    compared, and this says so. ``--no-verify-plan-binding`` skips it, logged."""
    if getattr(args, "no_verify_plan_binding", False):
        warn(
            logger,
            "apply_tofu_plan_target_unverified_by_flag",
            message="--no-verify-plan-binding: the saved plan's target (account, region, "
            "backend, tofu and provider versions) was NOT compared with this apply's",
        )
        return
    recorded = record.get("target")
    if not isinstance(recorded, Mapping):
        raise _target_error(
            "tofu-plan-target-missing",
            "the saved plan records no target (account, region, backend, tofu and provider "
            "versions), so the target it was reviewed for is unknown",
            "Re-run `fluid plan --tofu` and apply that plan.",
        )
    now = _target_record(eng)
    toolchain: List[str] = []
    if recorded.get("tofu") != now["tofu"]:
        toolchain.append(f"tofu {recorded.get('tofu') or '?'} -> {now['tofu'] or '?'}")
    planned = recorded.get("providers") if isinstance(recorded.get("providers"), Mapping) else {}
    for source in sorted(set(planned) | set(now["providers"])):
        if planned.get(source) != now["providers"].get(source):
            toolchain.append(
                f"{source} {planned.get(source, 'none')} -> {now['providers'].get(source, 'none')}"
            )
    if toolchain:
        raise _target_error(
            "tofu-plan-toolchain",
            "the saved plan was made with another OpenTofu or provider version: "
            + "; ".join(toolchain),
            "Apply with the versions the plan was made with (the plan's dependency lock "
            "and tofu), or re-run `fluid plan --tofu` here.",
        )
    if recorded.get("backend") != now["backend"]:
        raise _target_error(
            "tofu-plan-target",
            "the saved plan was made for another state backend configuration than this " "apply's",
            "Apply with the --state-backend (or FLUID_STATE_BACKEND) the plan was made "
            "with, or re-run `fluid plan --tofu`.",
        )
    bound = recorded.get("identity")
    if not isinstance(bound, Mapping):
        raise _target_error(
            "tofu-plan-target-missing",
            "the saved plan's target record has no identity",
            "Re-run `fluid plan --tofu` and apply that plan.",
        )
    changed, unverified = target_identity.compare(bound, now["identity"])
    if changed:
        raise _target_error(
            "tofu-plan-target-mismatch",
            f"the saved plan was made for {target_identity.describe(eng.provider, bound)}, "
            f"but this apply's credentials and settings reach "
            f"{target_identity.describe(eng.provider, now['identity'])} ("
            + ", ".join(changed)
            + " differ). OpenTofu would apply it there.",
            "Apply with the credentials and region the plan was made for, or re-run "
            "`fluid plan --tofu` for this target and review that plan.",
        )
    if unverified:
        raise _target_error(
            "tofu-plan-target-unverified",
            "the saved plan is bound to "
            + ", ".join(f"{k} {bound[k]}" for k in unverified)
            + ", which this apply cannot read: "
            + "; ".join(f"{k}: {now['unread'].get(k, 'not read')}" for k in unverified),
            "Give this run what the plan's runner had (credentials, the SDK named above), "
            "or pass --no-verify-plan-binding for an emergency apply (logged).",
        )
    _say_provider_unbound(eng, logger, "apply_tofu_plan_target_provider_unbound")
    planned_unread = recorded.get("unread") if isinstance(recorded.get("unread"), Mapping) else {}
    for field_name in sorted(set(planned_unread) | (set(now["identity"]) - set(bound))):
        why = str(planned_unread.get(field_name, "not read when the plan was made"))
        cprint_literal(f"  ⚠️  target {field_name} not bound by the plan, not compared: {why}")
        warn(
            logger,
            "apply_tofu_plan_target_field_unbound",
            provider=eng.provider,
            field=str(field_name),
            reason=why,
        )


def _say_provider_unbound(eng: _Engine, logger: logging.Logger, event: str) -> None:
    """Name a provider whose target forge cannot read: its plan binds no account,
    project or host, so nothing stops it being applied to another one."""
    if target_identity.has_reader(eng.provider):
        return
    cprint_literal(
        f"  ⚠️  target: forge reads no account, project or host for {eng.provider}, "
        "so the saved plan is not bound to one"
    )
    warn(logger, event, provider=eng.provider)


def _target_error(kind: str, error: str, remediation: str) -> CLIError:
    return CLIError(
        1,
        "apply_plan_digest_" + kind.replace("-", "_"),
        {"kind": kind, "error": error, "remediation": [remediation]},
    )


def _state_version(eng: _Engine) -> Dict[str, Any]:
    """``{"stateLineage", "stateSerial"}`` of the state now, as ``tofu state pull`` reads it,
    or ``{}`` when it cannot be read. A saved plan records the state it was made from, and
    OpenTofu refuses it once lineage or serial moved on (``internal/backend/local``); recording
    them lets ``fluid apply`` refuse it too, before anything runs outside OpenTofu."""
    try:
        doc = read_state(Path(eng.workdir), eng.env)
    except Exception:  # noqa: BLE001 — OpenTofu's own check at apply time still stands
        return {}
    return {"stateLineage": doc.lineage, "stateSerial": doc.serial}


def _refuse_unreleased_imports(eng: _Engine, adopted: List[Any]) -> None:
    """Stop ``fluid plan --tofu`` when an adoption could not be handed back: the state would
    hold an object no apply took over, and its ``import`` block would plan it twice."""
    if not adopted:
        return
    held = sorted(
        {block.to for block in adopted} & set(runner.tofu_state_list(eng.workdir, env=eng.env))
    )
    if held:
        raise CLIError(
            1,
            "plan_tofu_adoption_not_released",
            {
                "error": "brownfield adoption imported resources the plan could not release "
                "from state again: " + ", ".join(held),
                "remediation": [
                    f"tofu -chdir={shlex.quote(eng.workdir)} state rm " + " ".join(held),
                    "then re-run `fluid plan --tofu`",
                ],
            },
        )


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` owner-only (0600), atomically: a new file is made
    with ``mkstemp`` (0600, never through an existing link) and renamed over."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _bound_tofu_plan(
    args, plan_data: Optional[Mapping[str, Any]], logger: logging.Logger
) -> Optional[Tuple[Dict[str, Any], bytes]]:
    """``(record, bytes)`` of the saved OpenTofu plan ``plan.json`` binds, or ``None``.

    The file is read once, here; the bytes that matched the digest are the bytes
    applied, so a file swapped after this check is never run. Under
    ``--no-verify-plan-binding`` the digest is not compared (logged by the caller),
    but the plan still has to be there: without it there is nothing reviewed to run.
    """
    if plan_data is None:
        return None
    from ..forge.core.plan_digest import PlanBindingError, tofu_plan_record, verify_tofu_plan

    skip = bool(getattr(args, "no_verify_plan_binding", False))
    try:
        record = tofu_plan_record(plan_data)
        if record is None:
            return None
        path = Path(str(args.contract)).parent / record["planFile"]
        try:
            data: Optional[bytes] = path.read_bytes()
        except OSError:
            data = None
        if data is None or not skip:
            verify_tofu_plan(record, data)
    except PlanBindingError as exc:
        raise CLIError(
            1,
            f"apply_plan_digest_{exc.kind.replace('-', '_')}",
            {"kind": exc.kind, "error": str(exc)},
        )
    assert data is not None
    return record, data


def _apply_bound_plan(
    eng: _Engine,
    args,
    logger: logging.Logger,
    record: Mapping[str, Any],
    saved: bytes,
    *,
    applied_state: Optional[List[Dict[str, Any]]],
) -> int:
    """``fluid apply plan.json`` for a plan from ``fluid plan --tofu``: ``tofu apply`` of
    the saved plan it binds, with no plan of its own.

    The checks before a plan ran when it was made, and its imports are in it. What
    runs here reads the saved plan: the data-loss gate (the apply's
    ``--allow-data-loss`` decides), the plugin's notices, and what the cloud keeps
    outside OpenTofu. OpenTofu itself refuses a plan whose state has changed since.
    """
    dry_run = bool(getattr(args, "dry_run", False))
    # First: nothing (not even the state-key migration) runs against another target.
    _refuse_another_target(eng, args, logger, record)
    if not dry_run:
        reconcile_state_key(eng.target, eng.provider, eng.env, logger, migrate=True)
    plan_path = Path(eng.workdir) / _WORKDIR_PLAN
    _write_private(plan_path, saved)
    shown = runner.tofu_show_plan(eng.workdir, plan_file=_WORKDIR_PLAN, env=eng.env)
    if shown is None:
        raise _unreadable_saved_plan(record)
    # Before the hooks: a stale plan is refused before anything outside OpenTofu runs.
    _refuse_stale_before_hooks(eng, record)
    changes = runner.saved_plan_changes(shown)
    cprint(
        f"\n  saved plan {record['planFile']} ({str(record['sha256'])[:19]}…): "
        f"+{changes['add']} ~{changes['change']} -{changes['remove']}"
    )
    _table_adoption.say_bound(record)
    # None under --no-verify-plan-binding: the record is unbound, and the apply's own flag decides.
    recorded = _table_hold.say_bound(args, record, logger)
    _record_planned(eng, changes, dry_run)
    removals = runner.saved_plan_removals(shown)
    destroyed, updated = _review_plan(
        eng, args, logger, changes, removals, shown=shown, recorded=recorded
    )
    if dry_run:
        _preview_outside_opentofu(eng, logger, destroyed, changes)
        return 0
    _enforce_access(eng.plugin, "pre_apply", eng.contract)
    _prepare_destroys(eng.plugin, eng.contract, destroyed)
    adopted_tables = record.get("adoptedTables")
    _table_adoption.audit_take_over(
        logger, adopted_tables if isinstance(adopted_tables, list) else []
    )
    _table_hold.audit_updated(logger, updated)
    return _finish_apply(eng, logger, changes, destroyed, applied_state, bound=True)


def _unreadable_saved_plan(record: Mapping[str, Any]) -> CLIError:
    """``fluid apply plan.json``: ``tofu show -json`` could not read the saved plan."""
    return CLIError(
        1,
        "apply_tofu_plan_unreadable",
        {
            "error": f"`tofu show -json` could not read the saved plan {record['planFile']}",
            "remediation": [
                "The plan needs the OpenTofu version and providers it was made with; "
                + "re-run `fluid plan --tofu` where you apply.",
                "A plan made with FLUID_STATE_ENCRYPTION set is read only with the same "
                + "setting, and one made without it only without it.",
            ],
        },
    )


def _check_plan_target(record: Mapping[str, Any], provider: str, target: "StateTarget") -> None:
    """Refuse a saved plan made for another provider or another state than this apply's."""
    if record.get("provider") == provider and record.get("state") == target.location:
        return
    raise CLIError(
        1,
        "apply_plan_digest_tofu_plan_target",
        {
            "kind": "tofu-plan-target",
            "error": f"the saved plan was made for {record.get('provider')!r} state "
            f"{record.get('state')!r}, but this apply resolves {provider!r} state "
            f"{target.location!r}",
            "remediation": [
                "Apply with the --state-backend (or FLUID_STATE_BACKEND) and working "
                "directory the plan was made with, or re-run `fluid plan --tofu`."
            ],
        },
    )


#: How OpenTofu refuses a saved plan whose state moved on (internal/backend/local/
#: backend_local.go): the state's serial changed, or it is another state altogether.
_STALE_PLAN_MARKERS = ("Saved plan is stale", "different state lineage")


def _refuse_stale_before_hooks(eng: _Engine, record: Mapping[str, Any]) -> None:
    """Refuse a saved plan whose state moved on, read before the plugin's hooks run (a
    Databricks ``prepare_destroys`` drops objects outside OpenTofu): ``tofu apply`` would
    refuse it only after them. A record without the state version, or a state that cannot
    be read, leaves the check to OpenTofu."""
    if "stateSerial" not in record:
        return
    now = _state_version(eng)
    if record.get("stateLineage") and now and not now["stateLineage"]:
        raise _state_missing_error(eng, record)
    if now and (now["stateLineage"], now["stateSerial"]) != (
        record.get("stateLineage"),
        record.get("stateSerial"),
    ):
        raise _stale_plan_error(None)


def _state_missing_error(eng: _Engine, record: Mapping[str, Any]) -> CLIError:
    """The saved plan was made against a state this run does not have at all."""
    local = not eng.target.backend
    err = CLIError(
        1,
        "apply_plan_digest_tofu_plan_state_missing",
        {
            "kind": "tofu-plan-state-missing",
            "error": f"the saved plan was made against existing state ({record.get('state')}), "
            "and this run finds no state there"
            + (
                ": local state stays in the workspace that holds it, so a plan made on one "
                "runner does not apply on another"
                if local
                else ""
            )
            + ". OpenTofu applied nothing.",
        },
    )
    err.suggestions = [
        (
            "Apply in the workspace the plan was made in, or keep state in a backend both "
            "runners reach (--state-backend s3://... or gcs://...)."
            if local
            else "Check the backend's credentials and key, or re-run `fluid plan --tofu`."
        )
    ]
    return err


def _saved_plan_stale(result: Any) -> bool:
    output = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    return any(marker in output for marker in _STALE_PLAN_MARKERS)


def _stale_plan_error(result: Any) -> CLIError:
    err = CLIError(
        1,
        "apply_plan_digest_tofu_plan_stale",
        {
            "kind": "tofu-plan-stale",
            "error": "the saved plan is stale: the state changed after it was made (another "
            "apply ran in between), so it no longer shows what applying it would do. OpenTofu "
            "applied nothing.",
        },
    )
    err.suggestions = ["Re-run `fluid plan --tofu`, review the new plan, then apply that one."]
    return err


def _converge_output_ports(
    plugin: Any, contract: Mapping[str, Any], *, dry_run: bool = False
) -> None:
    """Run a plugin's ``converge_output_ports`` hook, if it has one.

    Optional plugin capability, discovered like ``post_apply``: the Databricks plugin creates,
    updates or trashes the product's Genie space through the Genie API, since the provider has
    no resource for one (``iac/providers/databricks_genie.py``). It runs after the access
    enforcement, so a space only ever opens on protected tables. A failure is a typed error
    and the run exits non-zero; the tables the apply made stay, and the next apply converges
    the space. With ``dry_run`` the plugin prints what it would do and writes nothing.
    """
    # Its lines quote titles (anyone's, from the Genie list) and the ``[fluid-genie:...]``
    # marker, which Rich would read as markup.
    _run_hook(
        plugin, "converge_output_ports", "opentofu_output_port_failed", contract, dry_run=dry_run
    )


def _run_hook(plugin: Any, hook: str, event: str, *args: Any, **kwargs: Any) -> None:
    """Run the plugin's optional ``hook``, if it has one, with what it prints going out
    literally: it quotes the contract and the server (statements, names, a server's error text
    with its ``[ERROR_CLASS]``), data, not markup, so a "[word]" in it neither vanishes nor
    raises. A typed refusal is the run's error (:func:`_binding_error`, under ``event``)."""
    run = getattr(plugin, hook, None)
    if not callable(run):
        return
    try:
        run(*args, say=cprint_literal, **kwargs)
    except UnsupportedBindingError as exc:
        raise _binding_error(exc, event) from exc


def _enforce_access(
    plugin: Any, hook: str, contract: Mapping[str, Any], *, dry_run: bool = False
) -> None:
    """Run a plugin's ``pre_apply`` / ``post_apply`` access-enforcement hook, if it has one.

    Optional plugin capability, discovered like ``plan_notices``: the Databricks plugin
    binds Unity Catalog column masks and row filters, which the provider cannot manage
    (``iac/providers/databricks_enforce.py``). Unlike the advisory hooks this one gates: a
    refusal or failure is a typed error and the run exits non-zero. With ``dry_run`` the
    plugin prints the statements and runs none. What a hook prints is data (table names,
    statements, a server's error text with its ``[ERROR_CLASS]``), so it goes out literally.
    """
    # ``post_apply`` takes no ``dry_run``: it never runs in one.
    flags = {"dry_run": True} if dry_run else {}
    _run_hook(plugin, hook, "opentofu_access_enforcement_failed", contract, **flags)


def _planned_destroys(
    plugin: Any, changes: Mapping[str, int], workdir: str, env: Mapping[str, str]
) -> List[Dict[str, Any]]:
    """What the saved plan only deletes (``runner.planned_deletes``), for the plugin's
    ``prepare_destroys`` and ``unblock_destroys``. ``[]`` for a plugin without
    ``prepare_destroys`` or a plan that removes nothing; a plan that cannot be read back is
    said, and nothing is cleared before the apply."""
    if not callable(getattr(plugin, "prepare_destroys", None)) or not changes.get("remove"):
        return []
    shown = runner.tofu_show_plan(workdir, plan_file="tfplan", env=env)
    if shown is None:
        cprint(
            "  note: the saved plan could not be read back, so what the provider plugin made "
            "outside OpenTofu in the resources it destroys is not cleared first."
        )
        return []
    return runner.planned_deletes(shown)


def _prepare_destroys(
    plugin: Any,
    contract: Mapping[str, Any],
    destroyed: List[Dict[str, Any]],
    *,
    dry_run: bool = False,
) -> None:
    """Run the plugin's ``prepare_destroys`` hook, if it has one, before ``tofu apply``.

    Optional plugin capability, discovered like ``pre_apply``: the Databricks plugin drops what
    it made outside OpenTofu (metric views, column mask and row filter functions) in the
    schemas the plan destroys, which Unity Catalog would otherwise refuse to delete
    (``iac/providers/databricks_vacated.py``). It gets the plan's deletes
    (:func:`_planned_destroys`). With ``dry_run`` it prints the statements and runs none. What
    it prints quotes live objects, so it goes out literally; a refusal stops the run before the
    apply.
    """
    if destroyed:
        _run_hook(
            plugin,
            "prepare_destroys",
            "opentofu_destroy_preparation_failed",
            contract,
            destroyed,
            dry_run=dry_run,
        )


def _apply_saved_plan(
    plugin: Any,
    contract: Mapping[str, Any],
    destroyed: List[Dict[str, Any]],
    workdir: str,
    env: Mapping[str, str],
    *,
    resume: bool = True,
) -> Tuple[Any, int]:
    """``tofu apply`` of the saved plan, with the destroys it could not make yet resumed
    (:func:`_resume_blocked_destroys`): the result that stands, and how many destroys waited
    for a second apply (0 when none did). ``resume=False`` (a plan from ``fluid plan --tofu``)
    never resumes: that takes a second plan, which nobody reviewed."""
    result = runner.tofu_apply(workdir, env=env)
    if result.ok or not resume:
        return result, 0
    return _resume_blocked_destroys(plugin, contract, result, destroyed, workdir, env) or (
        result,
        0,
    )


def _applied(changes: Mapping[str, int], result: Any, waited: int) -> Dict[str, int]:
    """What the apply made: its summary, or after a resume the plan's own changes (the first
    apply made all but the destroys that waited, the second plan held nothing else), with the
    second apply's destroys in place of those that waited."""
    applied = runner.change_summary(result)
    if not waited:
        return applied
    return {**changes, "remove": int(changes["remove"]) - waited + applied["remove"]}


def _resume_blocked_destroys(
    plugin: Any,
    contract: Mapping[str, Any],
    result: Any,
    destroyed: List[Dict[str, Any]],
    workdir: str,
    env: Mapping[str, str],
) -> Optional[Tuple[Any, int]]:
    """Finish the destroys a ``tofu apply`` could not make yet: ``(the second apply's result,
    how many waited)``, or ``None`` and the apply's own failure stands.

    OpenTofu destroys a resource and then what holds it, inside one apply. Unity Catalog keeps
    the functions of a table's column masks and row filter until the table is gone, and refuses
    to delete a schema that holds them, so a schema the contract stopped using fails to destroy
    in the very apply that destroyed its tables. Optional plugin capability
    ``unblock_destroys(contract, blocked, say)``: the addresses of the destroys it cleared (the
    Databricks plugin drops those functions once no table uses them, and says what it dropped,
    ``iac/providers/databricks_vacated.py``). Fails closed: it resumes only when every
    resource the apply failed on is a delete of the saved plan that the plugin cleared, no
    error is left unaccounted for, and the plan made after it holds nothing but some of those
    deletes, which the data-loss gate already passed.
    """
    unblock = getattr(plugin, "unblock_destroys", None)
    errored = runner.errored_resources(result)
    planned = {entry["address"]: entry for entry in destroyed}
    if not callable(unblock) or not errored or len(_diagnostics(result.events)) != len(errored):
        return None  # nothing to resume, or an error no failed resource accounts for
    if any(action != "delete" or address not in planned for address, action in errored):
        return None
    waited = sorted({address for address, _action in errored})
    try:
        blocked = [planned[address] for address in waited]
        cleared = set(unblock(contract, blocked, say=cprint_literal) or ())
    except Exception as exc:  # noqa: BLE001 — the apply's own failure is what the run raises
        cprint_literal(
            f"  what holds {', '.join(waited)} could not be cleared: {type(exc).__name__}"
        )
        return None
    if cleared != set(waited):
        return None  # a failure the plugin did not clear: the apply's own error stands
    plan = runner.tofu_plan(workdir, env=env)
    removals = {address for address, _kind in runner.planned_removals(plan)}
    only_those = {"add": 0, "change": 0, "remove": len(removals)}
    if not (plan.ok and removals and removals <= set(waited)) or (
        runner.change_summary(plan) != only_those
    ):
        cprint_literal(
            "  not destroyed again: the plan made after the apply holds more than "
            + ", ".join(waited)
        )
        return None
    cprint(f"  tofu plan (the destroys that waited): +0 ~0 -{len(removals)}")
    return runner.tofu_apply(workdir, env=env), len(waited)


def build_after_apply(
    args, logger: logging.Logger, contract: Mapping[str, Any], build_kwargs: Mapping[str, Any]
) -> int:
    """The build phase of a build-augmented mode, after a ``tofu apply`` that succeeded, then
    the plugin's ``post_build`` hook (:func:`after_builds`), whatever the builds returned: one
    that failed part way may already have rebuilt a table. A dry run changes nothing, so it
    runs no hook (its plan already printed what the apply would declare)."""
    from fluid_build.build_runners import run_builds_from_args

    rc = run_builds_from_args(args, logger, force_run=True, **build_kwargs)
    if not getattr(args, "dry_run", False):
        after_builds(args, logger, contract)
    return rc


def after_builds(args, logger: logging.Logger, contract: Mapping[str, Any]) -> None:
    """The plugin's ``post_build`` hook, if it has one, after the build phase.

    Optional plugin capability, discovered like ``plan_notices``, and advisory: a dbt build run
    here may have rebuilt a table with CREATE OR REPLACE, which drops what the apply declared on
    it that dbt does not (the Databricks plugin's Unity Catalog keys), and the plugin declares
    it again. It gets the contract as the apply did, its ``{{ env.* }}`` templates resolved
    (:func:`_load_contract`), and never changes the run's exit code.
    """
    try:
        resolved = _load_contract(args, logger, contract)
        plugin = get_iac_plugin(
            _resolve_provider(resolved, getattr(args, "provider", None) or "auto")
        )
    except Exception:  # noqa: BLE001 — the apply resolved it already; nothing to add here
        return
    run = getattr(plugin, "post_build", None)
    if not callable(run):
        return
    try:
        with contract_source(source_contract_file(getattr(args, "contract", None))):
            run(resolved, say=cprint_literal)
    except Exception as exc:  # noqa: BLE001 — advisory: the build's own result stands
        cprint_literal(f"  after the build: {type(exc).__name__}: {exc}")


def _enforce_after_failed_apply(plugin: Any, contract: Mapping[str, Any]) -> None:
    """Best effort, bind-only ``post_apply`` for a ``tofu apply`` that failed part way.

    A failure here is printed, never raised: the error the run exits with is the apply's own
    (``opentofu_apply_failed``). The plugin binds what exists, revokes ``SELECT`` where it
    cannot, skips tables that do not exist and drops nothing (``after_failed_apply``).
    """
    run = getattr(plugin, "post_apply", None)
    if not callable(run):
        return
    cprint(
        "\n  tofu apply failed part way: binding the masks and row filter on the tables that "
        "exist before the run stops (no mask or filter is dropped)."
    )
    try:
        run(contract, say=cprint_literal, after_failed_apply=True)
    except UnsupportedBindingError as exc:
        cprint_literal(
            f"  access enforcement after the failed apply: {exc} {' '.join(exc.remediation)}"
        )
    except Exception as exc:  # noqa: BLE001 — the apply's own error is what the run raises
        cprint(f"  access enforcement after the failed apply could not run: {type(exc).__name__}")


def _applied_env(args) -> Optional[str]:
    """The overlay env this apply is for: ``--env``, else the bundle's own."""
    env = getattr(args, "env", None)
    if env:
        return str(env)
    bundle = getattr(args, "bundle", None)
    if not bundle:
        return None
    try:
        from fluid_build.forge.core.bundle import read_bundle_source

        source = read_bundle_source(Path(bundle)) or {}
    except Exception:  # noqa: BLE001 - the env is a report field, not a gate
        return None
    return str(source["env"]) if source.get("env") else None


def _module_addresses(module_text: str) -> List[str]:
    """``type.name`` of every resource the emitted module declares."""
    try:
        doc = json.loads(module_text)
    except ValueError:
        return []
    out: List[str] = []
    for rtype, by_name in sorted((doc.get("resource") or {}).items()):
        if isinstance(by_name, dict):
            out.extend(f"{rtype}.{name}" for name in sorted(by_name))
    return out


@dataclass(frozen=True)
class StateTarget:
    """Where ``fluid apply`` keeps one contract's OpenTofu workdir and state."""

    workdir: Path
    #: The ``terraform.backend`` block, or ``None`` for local state in ``workdir``.
    backend: Optional[Dict[str, Any]]
    #: ``--state-backend``, ``FLUID_STATE_BACKEND`` or ``default``.
    origin: str
    #: Where a release before the provider-keyed default kept this state, when
    #: that differs from ``backend`` (``iac.backend.legacy_default_backend``).
    legacy_backend: Optional[Dict[str, Any]] = None
    #: The key ``FLUID_STATE_ENCRYPTION`` names, or ``None``: the state is not encrypted.
    encryption: Optional[StateEncryption] = None
    #: ``--migrate-state-encryption``: this run reads plaintext state, writes it encrypted.
    migrate_encryption: bool = False

    @property
    def location(self) -> str:
        """The state's location, as the apply prints it."""
        if self.backend:
            return "remote: " + backend_location(self.backend)
        return f"local: {self.workdir / LOCAL_STATE_FILE}"


#: Where the local backend keeps state inside the workdir (``tofu`` default).
LOCAL_STATE_FILE = "terraform.tfstate"


def resolve_state_target(args, contract: Mapping[str, Any], provider: str) -> StateTarget:
    """The workdir and backend ``fluid apply`` uses for ``contract``.

    One resolution for every command that reads the apply's state (``fluid
    diff`` and ``fluid verify --state-drift`` too), so a reader can never look
    at a different state from the one the apply wrote. Raises the apply's own
    ``apply_state_backend_invalid`` for a spec it cannot use.
    """
    # ``contract`` selects the default state key: packaging-bearing contracts
    # get a per-contract key so two products sharing one state bucket cannot
    # clobber each other; legacy contracts keep the shared key (RFC file 7).
    #
    # ``--state-backend`` defaults from ``FLUID_STATE_BACKEND`` (flag wins;
    # an empty flag forces local state), so a CI job can keep state in a
    # bucket instead of the workspace it wipes after every run. That job
    # applies every product with the one value, so a bucket-only value keys
    # state per contract for all of them, packaging block or not.
    #
    # The provider is part of every per-contract default key, so one contract
    # applied to aws and to gcp (``--env`` overlays) keeps two states: with
    # one, each cloud's plan read the other's resources as orphans to destroy.
    backend_spec, backend_origin = resolve_state_backend_spec(getattr(args, "state_backend", None))
    per_contract = backend_origin == STATE_BACKEND_ENV
    try:
        backend = parse_backend(
            backend_spec,
            contract,
            per_contract_default=per_contract,
            provider=provider,
        )
        legacy = legacy_default_backend(
            backend_spec, contract, per_contract_default=per_contract, provider=provider
        )
    except ValueError as exc:
        raise CLIError(
            1,
            "apply_state_backend_invalid",
            {"source": backend_origin, "error": str(exc)},
        )
    # Per-contract workdir + state: each contract owns an isolated ``tofu``
    # state, so applying contract B never plans to destroy contract A's
    # resources (they share the provider but not the state).
    workdir = (
        Path(getattr(args, "workspace_dir", None) or ".")
        / ".fluid"
        / "iac"
        / provider
        / safe_ident(contract.get("id") or "contract")
    )
    encryption, migrate = _state_encryption(args)
    return StateTarget(
        workdir=workdir,
        backend=backend,
        origin=backend_origin,
        legacy_backend=legacy,
        encryption=encryption,
        migrate_encryption=migrate,
    )


def _state_encryption(args) -> Tuple[Optional[StateEncryption], bool]:
    """``FLUID_STATE_ENCRYPTION``'s key and whether this run migrates plaintext state."""
    try:
        encryption = configured_state_encryption()
    except StateEncryptionError as exc:
        raise _encryption_error(exc)
    migrate = bool(getattr(args, "migrate_state_encryption", False))
    if migrate and encryption is None:
        raise _encryption_error(
            StateEncryptionError(
                "opentofu_state_encryption_invalid",
                "migrate-without-key",
                "--migrate-state-encryption encrypts the state with the key "
                "FLUID_STATE_ENCRYPTION names, and it is not set.",
                ("Set FLUID_STATE_ENCRYPTION, then re-run",),
            )
        )
    return encryption, migrate


def recorded_legacy_backend(target: StateTarget) -> bool:
    """True when the workdir's ``.terraform/`` recorded the pre-provider-key backend.

    A plain ``tofu init`` stops there with "Backend configuration changed";
    the caller inits with ``-reconfigure`` instead, and :func:`reconcile_state_key`
    moves the state, so nothing is left behind.
    """
    return target.legacy_backend is not None and records_backend(
        target.workdir, target.legacy_backend
    )


def reconcile_state_key(
    target: StateTarget,
    provider: str,
    env: Mapping[str, str],
    logger: logging.Logger,
    *,
    migrate: bool,
) -> Optional[StateReconciliation]:
    """Bring state a previous release kept without the provider in its key along.

    Runs after the workdir's ``tofu init`` on ``target.backend``.
    ``migrate=True`` (``fluid apply``) copies it with ``tofu init
    -migrate-state``; ``migrate=False`` (the read-only drift pass) only
    reports it, and :func:`read_target` then points the read at the old key.
    ``None`` when there is no old key to look at.
    """
    if target.backend is None or target.legacy_backend is None:
        return None
    try:
        outcome = _reconcile_state(
            workdir=target.workdir,
            current=target.backend,
            legacy=target.legacy_backend,
            provider=provider,
            env=env,
            migrate=migrate,
            logger=logger,
        )
    except StateMigrationError as exc:
        raise _encryption_failure(str(exc)) or CLIError(
            1,
            exc.code,
            {
                "error": str(exc),
                "state": backend_location(target.backend),
                "legacy_state": backend_location(target.legacy_backend),
            },
        )
    line = outcome.summary()
    if line:
        cprint(f"  state move:  {line}")
        info(
            logger,
            "opentofu_state_key_reconciled",
            outcome=outcome.outcome,
            state=backend_location(target.backend),
            legacy_state=backend_location(target.legacy_backend),
            resources=outcome.resources,
        )
    return outcome


def _key_names_provider(backend: Mapping[str, Any], provider: str) -> bool:
    """True when the backend's key (or GCS prefix) has ``provider`` as a path segment."""
    if "s3" in backend:
        path = str((backend.get("s3") or {}).get("key") or "")
    elif "gcs" in backend:
        path = str((backend.get("gcs") or {}).get("prefix") or "")
    else:
        return False
    return provider in path.split("/")


def guard_state_shared_with_another_cloud(
    target: StateTarget, provider: str, env: Mapping[str, str]
) -> None:
    """Refuse a remote state whose key names no provider and holds another cloud's resources.

    The per-provider default keys (``fluid/<id>/<provider>/...``) cannot be
    shared by two clouds, and local state lives in a per-provider workdir, so
    only a remote key that does not name the provider is read (one ``tofu
    state pull``). Whose resources they are is the migration's own rule
    (``state_migration.other_clouds``). Without this, the gcp plan on a key
    the aws apply wrote reads the aws resources as orphans, and
    ``--allow-data-loss`` destroys them.
    """
    if target.backend is None or _key_names_provider(target.backend, provider):
        return
    location = backend_location(target.backend)
    try:
        doc = read_state(target.workdir, env)
    except StateMigrationError as exc:
        raise _encryption_failure(str(exc)) or CLIError(
            1, exc.code, {"error": str(exc), "state": location}
        )
    others = sorted(other_clouds(doc.resources, provider))
    if not others:
        return
    raise CLIError(
        1,
        "state_shared_with_another_provider",
        {
            "error": (
                f"{location} holds resources of the {', '.join(others)} provider, and this is "
                f"the {provider} apply: its plan would read them as orphans to destroy. Give "
                "each provider its own state, with a key that names the provider in "
                "--state-backend (for example fluid/<id>/<provider>/terraform.tfstate) or a "
                f"bucket-only {STATE_BACKEND_ENV}, which keys state by contract and provider"
            ),
            "state": location,
            "providers": others,
        },
    )


def read_target(
    target: StateTarget, provider: str, env: Mapping[str, str], logger: logging.Logger
) -> StateTarget:
    """The target a read-only caller reads: the old key while its move is pending.

    Call after the workdir's init on ``target.backend``. When this returns a
    different target, the caller re-emits the module on its backend and
    re-inits with ``-reconfigure``.
    """
    outcome = reconcile_state_key(target, provider, env, logger, migrate=False)
    if outcome is None or outcome.outcome != PENDING:
        return target
    return replace(target, backend=dict(outcome.legacy))


def emit_module(
    plugin: Any, contract: Mapping[str, Any], target: StateTarget, logger: logging.Logger
) -> Tuple[str, List[Any]]:
    """``(main.tf.json text, native actions)`` the apply emits for ``contract``."""
    actions = native_actions(contract, logger)
    try:
        module = build_module(
            plugin, contract, actions=actions, backend=target.backend, **_encryption_args(target)
        )
    except UnsupportedBindingError as exc:
        # The emitter refused to substitute a different resource kind for the
        # declared binding. Surface it as a typed CLI error rather than a
        # traceback — and, critically, before anything reaches the warehouse.
        raise _binding_error(exc)
    return module, actions


def _binding_error(exc: UnsupportedBindingError, event: str = "unsupported_binding") -> CLIError:
    """A typed refusal as the run's error, under ``event``, or the refusal's own when its class
    names one (a Genie space's ``opentofu_output_port_failed``)."""
    return CLIError(
        1,
        getattr(exc, "event", None) or event,
        {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
    )


def credential_overlay(plugin: Any, env: Mapping[str, str]) -> Dict[str, str]:
    """``plugin.credential_env(env)``; a refusal (no host, two credentials, a
    profile for another workspace) is a typed CLI error before ``tofu`` starts."""
    try:
        return plugin.credential_env(env)
    except UnsupportedBindingError as exc:
        raise _binding_error(exc)


def _tofu_env(plugin: Any, target: StateTarget) -> Dict[str, str]:
    """``tofu``'s environment for one run of ``plugin`` on ``target``'s state.

    The allowlist (``iac/credentials.py``) for the plugin, the state backend's
    cloud and the encryption key's; then the plugin's ``credential_env`` overlay,
    derived from the full environment as before; then what the encryption key
    needs (``iac/encryption.py``), fetched now for this run. Every ``tofu`` call
    of the run, ``init`` to ``apply``, takes this one mapping: an encrypted saved
    plan is read back with the same key.
    """
    full = dict(os.environ)
    encryption = getattr(target, "encryption", None)
    clouds = backend_clouds(target.backend) + (encryption.clouds if encryption else ())
    env = build_tofu_env(plugin, clouds=clouds, base=full)
    env.update(credential_overlay(plugin, full))
    if encryption is not None:
        try:
            require_supported_tofu(runner.tofu_version())
            env.update(encryption_env(encryption))
        except StateEncryptionError as exc:
            raise _encryption_error(exc)
        if getattr(target, "migrate_encryption", False):
            warn(
                logging.getLogger(__name__),
                "opentofu_state_encryption_migration",
                message="--migrate-state-encryption: this run reads unencrypted state and "
                "writes it back encrypted; the state is not enforced until it ends",
                key=encryption.describe(),
            )
    return env


def _encryption_args(target: StateTarget) -> Dict[str, Any]:
    """``build_module``'s encryption block and variables for ``target``, or nothing."""
    encryption = getattr(target, "encryption", None)
    if encryption is None:
        return {}
    migrate = bool(getattr(target, "migrate_encryption", False))
    return {
        "encryption": encryption_block(encryption, migrate=migrate),
        "variables": encryption_variables(encryption),
    }


def _encryption_failure(output: str) -> Optional[CLIError]:
    """The fix-it for a ``tofu`` run that failed on state encryption, else ``None``."""
    refusal = explain_state_failure(output)
    return _encryption_error(refusal) if refusal is not None else None


def _encryption_error(exc: StateEncryptionError) -> CLIError:
    """A state-encryption refusal as the run's error: what happened, then the fix as
    the CLI's suggestions (a bullet list, as a plugin's plan fix-it prints)."""
    err = CLIError(1, exc.event, {"error": str(exc)})
    err.suggestions = list(exc.remediation)
    return err


@contextmanager
def _apply_lineage(contract: Mapping[str, Any], *, provider: str, planned: Mapping[str, Any]):
    """Emit START then COMPLETE / FAIL around one ``tofu apply``.

    Yields a ``record(applied)`` callback so the terminal event carries the
    real applied change counts rather than the planned ones. Any exception
    escaping the block (including the ``opentofu_apply_failed`` CLIError)
    produces a FAIL event and is re-raised untouched.
    """
    from fluid_build.api.lineage import RunEventType
    from fluid_build.build_runners._acquisition_common import generate_run_id, utc_now_iso
    from fluid_build.build_runners._lineage import emit_apply_event, resolve_lineage_emitter

    emitter = resolve_lineage_emitter()
    run_id = generate_run_id()
    started_at = utc_now_iso()
    applied: Dict[str, Any] = {}

    def record(counts: Mapping[str, Any]) -> None:
        applied.update(counts)

    emit_apply_event(
        emitter,
        contract,
        event_type=RunEventType.START,
        event_time=started_at,
        run_id=run_id,
        run_started_at=started_at,
        provider=provider,
        facets={"planned_changes": dict(planned)},
    )
    try:
        yield record
    except BaseException:
        emit_apply_event(
            emitter,
            contract,
            event_type=RunEventType.FAIL,
            event_time=utc_now_iso(),
            run_id=run_id,
            run_started_at=started_at,
            provider=provider,
            facets={"planned_changes": dict(planned)},
        )
        raise
    emit_apply_event(
        emitter,
        contract,
        event_type=RunEventType.COMPLETE,
        event_time=utc_now_iso(),
        run_id=run_id,
        run_started_at=started_at,
        provider=provider,
        facets={"planned_changes": dict(planned), "applied_changes": dict(applied)},
    )


def resolve_apply_engine(
    args, logger: logging.Logger, contract: Optional[Mapping[str, Any]] = None
) -> str:
    """Resolve the apply engine for ``fluid apply`` — automatic, per-provider.

    There is no user-facing engine switch: the cloud providers (all cut
    over) compile the contract to ``.tf.json`` and run ``tofu``; ``local``
    keeps its native apply. The per-provider mapping lives in
    ``iac.cutover``. Any failure to classify the contract falls back to
    ``native`` — the safe default.

    ``contract``, when given, is the copy ``fluid apply`` loaded: the engine
    is chosen from it rather than from a second read of ``args.contract``.
    """
    from fluid_build.iac.cutover import resolve_engine

    try:
        contract = _load_contract(args, logger, contract)
        provider = _resolve_provider(contract, getattr(args, "provider", None) or "auto")
    except CLIError as exc:
        # A `--provider` that contradicts the contract's binding is an
        # operator error, not a classification failure. Falling through to
        # the broad handler would swallow it and route to the *native*
        # engine — trading one silent wrong-target apply for another. Let it
        # out so `fluid apply` fails with the same message
        # `fluid generate iac` gives.
        if getattr(exc, "event", None) == "generate_iac_provider_mismatch":
            raise
        return "native"
    except Exception:  # noqa: BLE001 — cannot classify the contract → safe default
        return "native"
    return resolve_engine(None, provider)


def _verify_plan_binding_for_opentofu(args, logger: logging.Logger) -> Optional[Dict[str, Any]]:
    """Stage-7 plan-binding gate, OpenTofu-engine edition.

    Mirrors :func:`fluid_build.cli.apply._verify_plan_digests` — same
    semantics, replicated here to avoid a circular import (this module
    is imported by ``cli/apply.py``). When the apply input is a
    ``plan.json``, recompute ``planDigest`` over the plan body and
    re-verify the ``bundleDigest`` if present.

    Honours ``--no-verify-plan-binding`` (logged at WARNING). Plans
    without a ``planDigest`` are treated as tamper signals.

    Returns the plan as read (``None`` for a contract input), so the saved
    OpenTofu plan it may bind is looked up in the same copy
    (:func:`_bound_tofu_plan`).
    """
    src = str(getattr(args, "contract", "") or "")
    if not src.endswith(".json"):
        return None  # raw contract input — no plan to verify
    skip = bool(getattr(args, "no_verify_plan_binding", False))
    if skip:
        logger.warning(
            "--no-verify-plan-binding: plan-binding verification was SKIPPED. "
            "This is an emergency escape hatch; the apply may be running "
            "against a tampered or stale plan. Make sure this is recorded "
            "in the change log."
        )
    try:
        with open(src, encoding="utf-8") as handle:
            plan_data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(
            1,
            "opentofu_engine_plan_unreadable",
            {"error": f"{src}: {exc}"},
        )
    if not isinstance(plan_data, dict):
        raise CLIError(
            1, "opentofu_engine_plan_unreadable", {"error": f"{src}: root is not an object"}
        )
    if skip:
        return plan_data
    # Local import — keeps the plan_digest import + tarfile dependency
    # off the hot path for raw-contract applies.
    from ..forge.core.plan_digest import PlanBindingError, verify_plan_binding

    # Find an adjacent .tgz bundle if present (no --bundle arg on the
    # CLI today). The plan_digest module's verify_plan_binding accepts
    # bundle_path=None and only enforces bundle verification when the
    # plan carries a non-empty bundleDigest AND a bundle is available.
    bundle_path = None
    bundle_arg = getattr(args, "bundle", None)
    if bundle_arg and Path(bundle_arg).exists():
        bundle_path = Path(bundle_arg)
    else:
        candidate = Path(src).with_suffix(".tgz")
        if candidate.exists():
            bundle_path = candidate
    try:
        verify_plan_binding(plan_data, bundle_path=bundle_path)
    except PlanBindingError as exc:
        raise CLIError(
            1,
            f"apply_plan_digest_{exc.kind.replace('-', '_')}",
            {"kind": exc.kind, "error": str(exc)},
        )
    return plan_data


def _load_contract(
    args, logger: logging.Logger, loaded: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Load the contract dict from a ``.fluid.yaml`` file or a ``.json`` plan.

    ``loaded`` is a contract the caller already holds; it is used instead of
    reading ``args.contract`` (templates are still resolved, on a copy).

    ``{{ env.* }}`` placeholders are resolved before the contract reaches the
    emitter — the OpenTofu data-plane is emitted straight from the contract's
    ``exposes[]``, so an unresolved template would otherwise land verbatim in
    the ``.tf.json``.
    """
    if loaded is not None:
        resolved: Dict[str, Any] = resolve_env_templates_in_contract(dict(loaded))
        return resolved
    src = str(args.contract)
    if src.endswith(".json"):
        with open(src, encoding="utf-8") as handle:
            plan = json.load(handle)
        contract = plan.get("contract") if isinstance(plan, dict) else None
        if not contract:
            raise CLIError(
                1,
                "opentofu_engine_no_contract",
                {"error": f"{src}: plan has no embedded contract"},
            )
    else:
        contract = load_contract_with_overlay(src, getattr(args, "env", None), logger)
    return resolve_env_templates_in_contract(contract)


def _adopt_existing(
    plugin: Any,
    contract: Mapping[str, Any],
    actions: Any,
    workdir: str,
    env: Mapping[str, str],
    logger: logging.Logger,
    held: Optional[List[str]] = None,
    *,
    strict_tables: bool = False,
) -> List[Any]:
    """Brownfield adoption — ``tofu import`` each declared resource that
    already exists in the cloud, so ``tofu apply`` reconciles it instead of
    failing "already exists" against pre-provisioned infrastructure.

    A table's import (``ImportBlock.table``) is only a look-up, which the caller refuses or
    turns into an ``import`` block (``cli/_apply_table_adoption.py``); ``strict_tables``
    (``--adopt-tables``) makes it strict, as ``strict_adoption`` makes every import.

    Best-effort: a candidate already in state is skipped; one that does not
    exist in the cloud fails to import and is left for ``tofu apply`` to
    create. Genuine apply-time errors still surface from ``tofu apply``.

    Except for a plugin with ``strict_adoption``, whose provider would REPLACE
    an existing object it creates over (Databricks' CREATE OR REPLACE TABLE,
    databricks/terraform-provider-databricks#6140): there an import that fails
    for any reason but "no such object" stops the apply, since the object may
    exist and the plan would then show its replacement as a plain create.

    Each import is appended to ``held``, which the caller hands back when the run
    does not apply (:func:`_imports_kept_only_by_an_apply`). Returns what it imported,
    each an ``ImportBlock`` with its id resolved, for ``fluid plan --tofu`` to render as
    ``import`` blocks (``iac.importer.import_section``).
    """
    candidates = plugin.discover_imports(contract, actions)
    if not candidates:
        return []
    strict = bool(getattr(plugin, "strict_adoption", False))
    in_state = set(runner.tofu_state_list(workdir, env=env))
    adopted: List[Any] = []
    for block in candidates:
        if block.to in in_state:
            continue
        import_id = _import_id(block, workdir, env)
        if import_id is None:
            continue
        result = runner.tofu_import(workdir, block.to, import_id, env=env)
        if result.ok:
            adopted.append(replace(block, id=import_id, id_from=None))
            if held is not None:
                held.append(block.to)
        elif (strict or (strict_tables and getattr(block, "table", None))) and not (
            block.best_effort
        ):
            _refuse_unadopted(plugin, contract, block, result, workdir, env)
        else:
            logger.debug(
                "opentofu: import skipped %s (id=%s): %s",
                block.to,
                block.id,
                _tail(result.stderr or result.stdout, 200),
            )
    if adopted:
        cprint(f"  brownfield:  adopted {len(adopted)} pre-existing resource(s) into state")
    return adopted


def _import_id(block: Any, workdir: str, env: Mapping[str, str]) -> Optional[str]:
    """``block.id``, or for a block whose id the cloud assigns (``ImportBlock.id_from``) that id
    completed from state, which the imports before it have just filled. ``None`` when the
    resource it derives from is not in state: then neither exists yet, and there is nothing
    to adopt."""
    if getattr(block, "id_from", None) is None:
        return block.id
    address, attribute = block.id_from
    for resource in runner.tofu_state_resources(workdir, env=env):
        if resource.get("address") == address:
            value = (resource.get("values") or {}).get(attribute)
            return block.id.replace("{}", value) if isinstance(value, str) and value else None
    return None


@dataclass
class _Imports:
    """What one run's brownfield adoption imported (:func:`_adopt_existing`)."""

    addresses: List[str] = field(default_factory=list)
    kept: bool = False

    def keep(self) -> None:
        """The apply starts: what this run imported is the apply's now."""
        self.kept = True


@contextmanager
def _imports_kept_only_by_an_apply(workdir: str, env: Mapping[str, str], logger: logging.Logger):
    """Hand back what a run imported when it ends before ``tofu apply`` starts.

    ``tofu import`` writes state at once. A run that then stops (a dry-run, a
    refusal, the data-loss gate, a failed plan) would leave objects in state that
    no apply of the contract ever took over, and one the contract then leaves out
    plans as a destroy: of an object forge-cli never created. ``tofu state rm``
    forgets them again (the objects stay as they are), so a run that applies
    nothing leaves the state as it found it, and the next apply adopts again.
    """
    imports = _Imports()
    try:
        yield imports
    except BaseException:
        _hand_back(imports, workdir, env, logger)
        raise
    if not imports.kept:
        _hand_back(imports, workdir, env, logger)


def _hand_back(
    imports: _Imports, workdir: str, env: Mapping[str, str], logger: logging.Logger
) -> None:
    if not imports.addresses:
        return
    count = len(imports.addresses)
    result = runner.tofu_state_rm(workdir, imports.addresses, env=env)
    if result.ok:
        cprint(f"  brownfield:  released the {count} adopted resource(s); nothing was applied")
        return
    warn(
        logger,
        "opentofu_adoption_release_failed",
        resources=imports.addresses,
        error=_tail(result.stderr or result.stdout, 200),
    )
    cprint(
        f"  ⚠️  {count} adopted resource(s) stay in state, though nothing was applied: "
        + ", ".join(imports.addresses)
        + f". To leave them unmanaged: tofu -chdir={workdir} state rm "
        + " ".join(imports.addresses)
    )


#: How ``tofu import`` reports an id with no object behind it (OpenTofu's
#: ``import`` command): the one failure adoption treats as "create it".
_IMPORT_ABSENT = "Cannot import non-existent remote object"


def _refuse_unadopted(
    plugin: Any,
    contract: Mapping[str, Any],
    block: Any,
    result: Any,
    workdir: str,
    env: Mapping[str, str],
) -> None:
    """Under ``strict_adoption``, stop on an import that failed for any reason but
    "no such object" (that one is left for ``tofu apply`` to create).

    ``tofu import`` reads the module's data sources first, so a failure the plugin
    knows (a catalog that does not exist) surfaces here, before the plan, and gets
    the fix-it the plan would give.
    """
    output = (result.stderr or "") + (result.stdout or "")
    if _IMPORT_ABSENT in output:
        return
    explained = _explained_failure(plugin, contract, output, workdir, env)
    if explained is not None:
        raise explained
    why = (
        "the provider replaces an existing object it creates over; "
        "applying now could replace it, with the plan showing a plain create"
    )
    if getattr(block, "table", None) and not getattr(plugin, "strict_adoption", False):
        why = (
            "--adopt-tables adopts a table only once it is known to exist; planned as new, "
            "an existing table would fail the apply after the resources before it"
        )
    raise CLIError(
        1,
        "opentofu_adoption_failed",
        {
            "error": f"could not tell whether {block.id} already exists, so it was not "
            f"adopted into {block.to}: {_first_error(output)}",
            "why": why,
        },
    )


def _in_place_changes(
    plugin: Any,
    changes: Mapping[str, int],
    workdir: str,
    env: Mapping[str, str],
    shown: Any = _UNREAD,
) -> List[str]:
    """What the plan's in-place updates do to existing data, one line per change.

    Optional plugin capability ``plan_data_changes(plan)`` over the saved plan
    as ``tofu show -json`` prints it: a resource updated in place can still
    delete data (Databricks drops a removed column with ``ALTER TABLE ... DROP
    COLUMN``) or keep it under another name (a positional rename), and the
    change summary counts that as ``~``, never as ``-``. Fails closed: a plan
    with updates that cannot be read back counts. A plan the provider cannot
    apply at all (a Databricks column reorder) the plugin refuses, and no
    ``--allow-data-loss`` lets it through.
    """
    review = getattr(plugin, "plan_data_changes", None)
    if not callable(review) or not int(changes.get("change", 0)):
        return []
    if shown is _UNREAD:
        shown = runner.tofu_show_plan(workdir, plan_file="tfplan", env=env)
    if shown is None:
        return ["the saved plan could not be read back, so a dropped column cannot be ruled out"]
    try:
        return list(review(shown))
    except UnsupportedBindingError as exc:
        raise CLIError(
            1,
            "unsupported_binding",
            {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
        )


def _enforce_data_loss_gate(
    args,
    logger: logging.Logger,
    provider: str,
    changes: Mapping[str, int],
    data_changes: Mapping[str, int],
    lost_in_place: List[str],
) -> None:
    """The data-loss gate: refuse a plan that destroys a data-bearing resource or
    changes existing data in place, unless ``--allow-data-loss``; audit the override.

    ``tofu`` has no CTAS/CLONE data snapshot (see AUTOGEN_SPIKE.md, risk R1), so a
    destructive plan fails closed.
    """
    allow_data_loss = bool(getattr(args, "allow_data_loss", False))
    destroys = int(data_changes.get("remove", 0))
    if not allow_data_loss and (_data_loss_blocked(data_changes, False) or lost_in_place):
        losses = [f"plan destroys {destroys} resource(s)"] if destroys > 0 else []
        if lost_in_place:
            losses.append("plan changes existing data in place (" + "; ".join(lost_in_place) + ")")
        raise CLIError(
            1,
            "opentofu_data_loss_gate",
            {
                "error": "; ".join(losses) + "; `tofu` does not snapshot data — re-run with "
                "--allow-data-loss to proceed"
            },
        )
    # Past the gate, only an override with a loss is left to audit.
    if not allow_data_loss:
        return
    if not (destroys > 0 or lost_in_place):
        return
    # Audit-trail: every destructive apply through the override is logged at
    # WARNING so CI log-scrapers + operators have a paper-trail. Matches the same
    # posture as the native engine's _verify_plan_binding bypass warning.
    if destroys > 0:
        cprint(
            f"\n  ⚠️  --allow-data-loss: {destroys} resource(s) will be "
            "DESTROYED and no pre-replace snapshot is taken — this engine has "
            "no CTAS/CLONE step, so `fluid rollback` will have no restore point."
        )
        logger.warning(
            "--allow-data-loss: bypassing the data-loss gate; %d resource(s) "
            "will be destroyed by `tofu apply` with NO pre-replace snapshot "
            "(`fluid rollback` has no restore point). Provider: %s. Plan changes: "
            "+%d ~%d -%d.",
            destroys,
            provider,
            changes["add"],
            changes["change"],
            changes["remove"],
        )
    if lost_in_place:
        # The lines quote live objects (a table's column types): data, not markup.
        cprint_literal(
            "\n  ⚠️  --allow-data-loss: existing data is changed in place with no snapshot: "
            + "; ".join(lost_in_place),
            soft_wrap=True,
        )
        logger.warning(
            "--allow-data-loss: bypassing the data-loss gate for in-place data changes "
            "(%s). Provider: %s.",
            "; ".join(lost_in_place),
            provider,
        )
    warn(
        logger,
        "opentofu_destructive_gate_override",
        provider=provider,
        resources_to_destroy=destroys,
        **({"in_place_data_loss": list(lost_in_place)} if lost_in_place else {}),
        **changes,
    )


def _explained_failure(
    plugin: Any,
    contract: Mapping[str, Any],
    output: str,
    workdir: str,
    env: Mapping[str, str],
) -> Optional[CLIError]:
    """A plugin's fix-it for a failed ``tofu`` run, or ``None`` when it has none.

    Optional plugin capability ``explain_plan_failure(output, contract, state)``
    turns a provider error the operator cannot act on (Databricks: "changing the
    'type' of an existing column is not supported", naming no column) into one
    they can. The event is ``opentofu_plan_failed`` for log parsers.
    """
    explain = getattr(plugin, "explain_plan_failure", None)
    if not callable(explain):
        return None
    try:
        refusal = explain(output, contract, runner.tofu_state_resources(workdir, env=env))
    except Exception as exc:  # noqa: BLE001 — advisory; the raw error still surfaces
        logging.getLogger(__name__).debug("plan-failure explanation failed: %s", exc)
        return None
    if refusal is None:
        return None
    err = CLIError(
        1,
        "opentofu_plan_failed",
        {"kind": refusal.kind, "error": str(refusal), "remediation": list(refusal.remediation)},
    )
    err.suggestions = list(refusal.remediation)
    return err


def _plan_failure(
    plugin: Any,
    contract: Mapping[str, Any],
    plan: Any,
    workdir: str,
    env: Mapping[str, str],
) -> CLIError:
    """The error for a failed ``tofu plan``: the plugin's fix-it, else the raw tail."""
    output = (plan.stdout or "") + (plan.stderr or "")
    encrypted = _encryption_failure(output)
    if encrypted is not None:
        return encrypted
    explained = _explained_failure(plugin, contract, output, workdir, env)
    if explained is not None:
        return explained
    err = CLIError(
        1,
        "opentofu_plan_failed",
        {"error": _tail(redact_secret_text(plan.stderr or plan.stdout or ""))},
    )
    if _mentions_state_lock(output):
        err.suggestions = list(err.suggestions) + [
            "The state is locked: wait for the other run, or clear a stale lock with "
            f"'tofu force-unlock <id>' in {shlex.quote(workdir)}"
        ]
    return err


def _guard_region_move(
    plugin: Any, contract: Mapping[str, Any], workdir: str, env: Mapping[str, str]
) -> None:
    """Fail closed when state holds this contract's resources in another region.

    The provider now takes the region the bindings name, where it used to take
    the environment's. A contract first applied from a shell in us-east-1 has
    its resources there; re-applied with the region pinned to eu-west-1, the
    refresh finds nothing in eu-west-1, drops the old resources from state as
    drift and plans them as creates. No destroy is planned, so the data-loss
    gate cannot fire, and the originals are left behind unmanaged. Reproduced
    against an emulator before this guard existed.
    """
    from fluid_build.iac import provider_config

    pinned = (provider_config(plugin, contract) or {}).get("region")
    recorded_regions = getattr(plugin, "recorded_regions", None)
    if not pinned or not callable(recorded_regions):
        return
    elsewhere = sorted(recorded_regions(runner.tofu_state_resources(workdir, env=env)) - {pinned})
    if elsewhere:
        raise CLIError(
            1,
            "opentofu_region_moved",
            {
                "error": f"state holds this contract's resources in {', '.join(elsewhere)}, "
                f"but its bindings name {pinned}. Applying would create them again in "
                f"{pinned} and leave the originals unmanaged",
                "state": workdir,
            },
        )


def _guard_packaging_transitions(
    contract: Mapping[str, Any],
    workdir: str,
    env: Mapping[str, str],
    args,
    logger: logging.Logger,
    *,
    plugin: Any = None,
    actions: Any = (),
) -> None:
    """Fail closed when a container's ownership would flip under existing state.

    Thin CLI adapter over ``iac.transition.guard_ownership_transitions``:
    the detection + remediation text live in ``iac/`` (no ``cli`` imports
    there), and this side owns the ``CLIError`` translation and the
    structured audit events the run record carries.

    A no-op for every contract without a ``packaging`` block (the LEGACY
    sentinel can never transition) and for a fresh workdir with no state.

    ``plugin``/``actions`` supply the import candidates
    (:meth:`IacProviderPlugin.discover_imports`) — the exact addresses
    :func:`_adopt_existing` is about to ``tofu import`` moments later. The
    guard needs them because a provider that emits no ``data`` sub-tree for
    a shared pool (Snowflake) leaves the REFERENCED container invisible in
    state, so the state diff alone cannot see the adoption coming.
    """
    from fluid_build.iac.transition import (
        PackagingTransitionError,
        guard_ownership_transitions,
    )

    state = runner.tofu_state_list(workdir, env=env)
    if not state:
        return
    import_candidates: List[str] = []
    if plugin is not None:
        try:
            import_candidates = [
                str(block.to) for block in plugin.discover_imports(contract, actions)
            ]
        except Exception as exc:  # pragma: no cover - defensive; guard must not crash apply
            logger.debug("opentofu: import-candidate probe failed: %s", exc)
    try:
        adoptions = guard_ownership_transitions(
            contract,
            state,
            workdir=workdir,
            adopt_shared_container=bool(getattr(args, "adopt_shared_container", False)),
            import_candidates=import_candidates,
            logger=logger,
        )
    except PackagingTransitionError as exc:
        # Structured audit event BEFORE the raise, so the run record shows
        # the blocked transition even though the apply never proceeded.
        info(logger, "packaging_transition_blocked", **exc.event_fields())
        raise CLIError(
            1,
            "packaging_transition_blocked",
            {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
        )
    if adoptions:
        # WARNING-level audit trail for the override — same discipline as
        # ``opentofu_destructive_gate_override``.
        warn(
            logger,
            "packaging_adoption_override",
            containers=[t.as_event() for t in adoptions],
            count=len(adoptions),
        )


def _guard_catalog_moves(
    plugin: Any,
    contract: Mapping[str, Any],
    provider: str,
    workdir: str,
    env: Mapping[str, str],
    logger: logging.Logger,
    *,
    actions: Any = (),
) -> None:
    """Fail closed when the plan would destroy what an older forge-cli created for
    an Iceberg table that now lives in another catalog (Glue resources on AWS, an
    EXTERNAL VOLUME on Snowflake).

    Thin CLI adapter over ``iac.catalog_moves.guard_catalog_moves``, the shape
    of :func:`_guard_packaging_transitions`: detection and remediation text live
    in ``iac/``, and this side owns the ``CLIError`` and the audit event.

    A no-op for a provider whose emitter moved nothing
    (``catalog_moves.catalog_move_spec``), for every contract without a moved
    expose (checked before the state is even listed), and for a fresh workdir.
    A probe that cannot run is skipped rather than failing the apply, since
    the data-loss gate still stands behind it, but never silently: that gate's
    remedy is ``--allow-data-loss``, the one thing an operator must not reach
    for here, so the skip is a WARNING that says so.
    """
    from fluid_build.iac.catalog_moves import (
        CatalogMoveError,
        catalog_move_spec,
        guard_catalog_moves,
        moved_iceberg_exposes,
    )

    spec = catalog_move_spec(plugin, provider)
    if spec is None:
        return
    moved = moved_iceberg_exposes(contract, spec=spec)
    if not moved:
        return

    def _skipped(reason: str) -> None:
        warn(
            logger,
            "iceberg_catalog_move_probe_skipped",
            provider=provider,
            reason=reason,
            exposes=[{"expose": e, "catalog": k} for e, k in moved],
            remedy=(
                "if the plan below destroys resources an older forge-cli created for these "
                "exposes, do NOT pass --allow-data-loss: release them with `tofu state rm` "
                "and re-run apply"
            ),
        )

    state = runner.tofu_state_list(workdir, env=env)
    if not state:
        # ``tofu state list`` answers [] for a fresh workdir and for a failed
        # listing alike; ``state pull`` tells the two apart.
        try:
            doc = read_state(Path(workdir), env)
        except Exception as exc:  # StateMigrationError, or no ``tofu`` at all
            _skipped(f"the state could not be read: {exc}")
            return
        if doc.resources:
            _skipped(
                f"`tofu state list` listed nothing, but the state holds "
                f"{len(doc.resources)} resource(s)"
            )
        return
    try:
        guard_catalog_moves(plugin, contract, state, workdir=workdir, actions=actions, spec=spec)
    except CatalogMoveError as exc:
        # Structured audit event BEFORE the raise, as for a packaging transition.
        info(logger, "iceberg_catalog_move_blocked", **exc.event_fields())
        raise CLIError(
            1,
            "iceberg_catalog_move_blocked",
            {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
        )
    except Exception as exc:  # defensive: a failed probe must not fail the apply
        _skipped(f"the probe failed: {type(exc).__name__}: {exc}")


def _report_suppressed_drift(
    plugin: Any,
    contract: Mapping[str, Any],
    workdir: str,
    env: Mapping[str, str],
    args,
    logger: logging.Logger,
) -> None:
    """Surface drift the emitted module's ``ignore_changes`` suppresses.

    Optional plugin capability: a cloud plugin that pins
    ``lifecycle.ignore_changes`` on an attribute derived from the contract
    implements ``suppressed_drift(contract, prior_resources)`` and returns
    one record per drifted object. Plugins without it are silently skipped,
    so this stays a no-op for every other cloud.

    Reporting, not gating. ``ignore_changes`` on Snowflake columns is a
    deliberate design decision (the build engine owns the real column types,
    and Snowflake rejects most in-place scale changes) — the defect was that
    the apply said nothing about it. ``fluid verify --strict`` remains the
    gate; this makes sure the operator knows to run it.
    """
    detect = getattr(plugin, "suppressed_drift", None)
    if not callable(detect):
        return
    try:
        prior = runner.tofu_prior_state_resources(workdir, env=env)
        drift = detect(contract, prior)
    except Exception as exc:  # noqa: BLE001 — advisory only, never fails an apply
        logger.debug("suppressed-drift report unavailable: %s", type(exc).__name__)
        return
    if not drift:
        return

    mode = str(getattr(args, "mode", None) or "amend")
    for record in drift:
        details = []
        for item in record.get("type_mismatches") or []:
            details.append(f"{item['column']}: contract={item['declared']} live={item['live']}")
        for name in record.get("missing") or []:
            details.append(f"{name}: declared but absent from the live table")
        for name in record.get("extra") or []:
            details.append(f"{name}: present in the live table but not declared")
        cprint(
            f"\n  ⚠️  column drift NOT reconciled on {record['table']} "
            f"(mode={mode}): " + "; ".join(details)
        )
        logger.warning(
            "opentofu_suppressed_column_drift table=%s mode=%s details=%s",
            record["table"],
            mode,
            "; ".join(details),
        )
    cprint(
        '      The emitted module pins lifecycle.ignore_changes=["column"], so '
        "`tofu` will not alter these columns — the build engine owns the "
        "materialized types. Run `fluid verify --strict` to gate on it."
    )
    warn(logger, "opentofu_suppressed_drift_reported", tables=[r["table"] for r in drift])


#: Resource types whose destroy revokes access and deletes no data: a member grant,
#: a Lake Formation permission, a policy tag or its taxonomy. Removing one is how a
#: contract revokes a reader or lifts a column restriction, so the data-loss gate
#: does not count it (it did, and a revocation needed --allow-data-loss, the flag
#: that also lets the same plan drop tables). The same split policy-as-code gates
#: over ``tofu show -json`` make by resource type (OPA's Terraform tutorial weighs
#: deletes per type). A key's IAM grant is not here: without it BigQuery cannot
#: decrypt the table. ``databricks_grant`` is authoritative for one principal on one
#: securable, so removing it revokes that principal's privileges and nothing else. A
#: ``databricks_entity_tag_assignment`` is one governance tag on a schema or table:
#: removing a label from the contract deletes the tag, and the table keeps its data.
ACCESS_ONLY_RESOURCE_TYPES = frozenset(
    {
        "google_bigquery_dataset_iam_member",
        "google_bigquery_table_iam_member",
        "google_storage_bucket_iam_member",
        "google_data_catalog_policy_tag_iam_member",
        "google_data_catalog_policy_tag",
        "google_data_catalog_taxonomy",
        "aws_lakeformation_permissions",
        "databricks_grant",
        "databricks_entity_tag_assignment",
    }
)


#: Resource types whose destroy deletes a definition the contract re-creates, and
#: no table data: a Databricks job (its run history goes with it) and a workspace
#: file of a dbt project uploaded from the build's repository. Removing a build
#: from the contract removes them, ungated, with a line saying so (founder
#: decision for the Databricks provider, 2026-10-29). A schema's data quality
#: monitor is one too: deleting it deletes its hidden scan job and what it has
#: learned, never a row (``iac/providers/databricks_quality.py``). So is a Glue table
#: optimizer, which schedules an Iceberg table's maintenance (``fluid_build/maintenance``).
RUN_DEFINITION_RESOURCE_TYPES = frozenset(
    {
        "databricks_job",
        "databricks_workspace_file",
        "databricks_directory",
        "databricks_data_quality_monitor",
        # A Glue Data Catalog table optimizer (``exposes[].iceberg.maintenance``, backend
        # glue): a schedule of compaction, retention or orphan deletion. Deleting it stops
        # that upkeep and deletes no file and no row.
        "aws_glue_catalog_table_optimizer",
    }
)


def _data_bearing_changes(
    changes: Mapping[str, int], removals: List[Tuple[str, str]], plugin: Any = None
) -> Tuple[Dict[str, int], List[str]]:
    """``(changes with only data-bearing removals, addresses of access-only removals)``.

    Neither access-only removals, run definitions (:func:`_retired_definitions`) nor what a
    build ran with (:func:`_build_definitions`) count. Fails closed: when the plan's
    per-resource events do not account for every removal in its summary (an older
    ``tofu``, a truncated stream), every removal counts, as before.
    """
    counted = {key: int(changes.get(key, 0)) for key in ("add", "change", "remove")}
    if len(removals) != counted["remove"]:
        return counted, []
    revoked = [addr for addr, kind in removals if kind in ACCESS_ONLY_RESOURCE_TYPES]
    counted["remove"] -= (
        len(revoked)
        + len(_retired_definitions(changes, removals))
        + len(_build_definitions(plugin, changes, removals))
    )
    return counted, revoked


def _build_definitions(
    plugin: Any, changes: Mapping[str, int], removals: List[Tuple[str, str]]
) -> List[str]:
    """Addresses of removed resources the provider plugin says hold no data: its optional
    ``retired_definitions(removals)`` (on AWS, what a forge Spark build ran with: its
    artifacts, role, log group and job). Only addresses the plan removes, none the other
    exemptions already count, and ``[]`` when the plan's events do not account for every
    removal (fail closed)."""
    hook = getattr(plugin, "retired_definitions", None)
    if not callable(hook) or len(removals) != int(changes.get("remove", 0)):
        return []
    exempt = ACCESS_ONLY_RESOURCE_TYPES | RUN_DEFINITION_RESOURCE_TYPES
    removed = {addr for addr, kind in removals if kind not in exempt}
    named = [str(addr) for addr in hook(list(removals))]
    return [addr for addr in dict.fromkeys(named) if addr in removed]


def _retired_definitions(changes: Mapping[str, int], removals: List[Tuple[str, str]]) -> List[str]:
    """Addresses of removed run definitions (:data:`RUN_DEFINITION_RESOURCE_TYPES`),
    ``[]`` when the plan's events do not account for every removal (fail closed)."""
    if len(removals) != int(changes.get("remove", 0)):
        return []
    return [addr for addr, kind in removals if kind in RUN_DEFINITION_RESOURCE_TYPES]


def _report_plan_notices(plugin: Any, contract: Mapping[str, Any], logger: logging.Logger) -> None:
    """Print what the plugin says the apply cannot do itself (``plan_notices``), such as
    a Unity Catalog privilege on an object the product does not own. Advisory only."""
    notices = getattr(plugin, "plan_notices", None)
    if not callable(notices):
        return
    try:
        for line in notices(contract):
            # A notice can quote live objects, so it is data, not markup.
            cprint_literal(f"  note: {line}")
    except Exception as exc:  # noqa: BLE001 — advisory only, never fails an apply
        logger.debug("plan notices unavailable: %s", type(exc).__name__)


def _reconcile_with_state(
    plugin: Any,
    module_path: Path,
    workdir: str,
    env: Mapping[str, str],
    logger: logging.Logger,
    *,
    announce: bool = True,
) -> List[Dict[str, Any]]:
    """Let the plugin patch the written module against the current state.

    Optional plugin capability ``reconcile_state(module, state_resources)``: the GCP
    plugin uses it once per dataset an older forge-cli applied with an authoritative
    access list, so a grant the contract no longer makes is revoked rather than left
    unmanaged (``iac/providers/gcp.py::reconcile_legacy_dataset_access``). A no-op for
    a fresh workdir and for plugins without it. ``fluid diff`` calls it too
    (``announce=False``), so its plan shows the revocation the apply will make.
    """
    reconcile = getattr(plugin, "reconcile_state", None)
    if not callable(reconcile):
        return []
    state = runner.tofu_state_resources(workdir, env=env)
    if not state:
        return []
    module = json.loads(module_path.read_text(encoding="utf-8"))
    reports = reconcile(module, state)
    if not reports:
        return []
    blocked = [r for r in reports if r.get("blocked")]
    if blocked:
        raise CLIError(
            1,
            "opentofu_dataset_access_unreconciled",
            {
                "error": "state holds dataset(s) an older forge-cli applied with an "
                "authoritative access list, and every entry of it is a grant the contract "
                "no longer makes, so it cannot be narrowed in place: "
                + "; ".join(f"{r['dataset']}: {', '.join(r['revoked'])}" for r in blocked),
                "remediation": [
                    "Revoke those entries on the dataset (bq update or the console), or "
                    "keep one of them in accessPolicy for this apply, then re-run."
                ],
            },
        )
    module_path.write_text(json.dumps(module, indent=2), encoding="utf-8")
    if not announce:
        return reports
    for report in reports:
        cprint(
            f"\n  dataset {report['dataset']}: its access list was written by an older "
            "forge-cli; this apply revokes the entries no grant of the contract covers: "
            + ", ".join(report["revoked"])
        )
    warn(logger, "gcp_dataset_access_reconciled", datasets=reports)
    return reports


def _data_loss_blocked(changes: Mapping[str, int], allow_data_loss: bool) -> bool:
    """A plan that removes resources is blocked unless ``--allow-data-loss`` is set."""
    return int(changes.get("remove", 0)) > 0 and not allow_data_loss


def _tail(text: str, limit: int = 800) -> str:
    return (text or "")[-limit:]


def _diagnostics(events: Any) -> List[Mapping[str, Any]]:
    """The error diagnostics in a ``tofu -json`` stream, in the order printed."""
    found: List[Mapping[str, Any]] = []
    for event in events or []:
        diag = event.get("diagnostic") if isinstance(event, Mapping) else None
        if isinstance(diag, Mapping) and diag.get("severity", "error") == "error":
            found.append(diag)
    return found


def _mentions_state_lock(text: str) -> bool:
    """True when tofu says the state lock is the problem.

    Matched on the phrases tofu prints ("Error acquiring the state lock",
    "Error locking state"), never on a bare "lock", which "block" contains.
    """
    low = text.lower()
    return "state lock" in low or "locking state" in low


def _apply_failure(result: Any, workdir: str, plugin: Any = None) -> CLIError:
    """The error for a failed ``tofu apply``: what failed, on which resource, and the way on.

    ``-json`` puts every diagnostic on stdout as one JSON line, so the raw tail
    was a run of ``{"@level":...}`` objects. The diagnostics are read back into
    a summary, the resource address and the provider's detail; output that holds
    none (a crash, a timeout) falls back to the first ``Error:``, else the exit
    code. Tofu records state as it goes, so what was created before the failure
    is normally tracked and a re-run converges: the suggestions say so. A plugin's
    optional ``apply_failure_hints(address, detail)`` adds its way on for the resource that
    failed, ahead of the engine's own ``tofu state list`` hint.
    """
    diagnostics = _diagnostics(getattr(result, "events", None))
    context: Dict[str, Any] = {}
    locked = False
    address = detail = ""
    if diagnostics:
        first = diagnostics[0]
        # Redacted before it is cut: a credential split at the limit (a URL that lost
        # its ``@``) matches no pattern.
        address = redact_secret_text(str(first.get("address") or "").strip())
        detail = redact_secret_text(str(first.get("detail") or "").strip())
        summary = redact_secret_text(str(first.get("summary") or "").strip()) or "tofu apply failed"
        context["error"] = summary + (f" ({address})" if address else "")
        if address:
            context["resource"] = address
        if detail:
            context["detail"] = detail[:800]
        if len(diagnostics) > 1:
            context["more_errors"] = len(diagnostics) - 1
        locked = _mentions_state_lock(summary + " " + detail)
    else:
        output = result.stderr or result.stdout or ""
        context["error"] = (
            _first_error(redact_secret_text(output), 800)
            if "Error:" in output
            else f"tofu apply exited {getattr(result, 'returncode', 1)}"
        )
        locked = _mentions_state_lock(output)
    err = CLIError(1, "opentofu_apply_failed", context)
    hints = [f"To see what exists, run 'tofu state list' in {shlex.quote(workdir)}"]
    plugin_hints = getattr(plugin, "apply_failure_hints", None)
    if address and callable(plugin_hints):
        hints = list(plugin_hints(address, detail)) + hints
    if locked:
        hints.append("The state is locked: wait for the other run, or 'tofu force-unlock <id>'")
    err.suggestions = list(err.suggestions) + hints
    return err


def _first_error(text: str, limit: int = 400) -> str:
    """``tofu`` output from its first ``Error:`` on, where the cause is stated.

    The tail of a failed import is the data-source context printed after the
    error, which cut the cause itself off mid-sentence.
    """
    start = (text or "").find("Error:")
    return text[start : start + limit] if start >= 0 else _tail(text, limit)
