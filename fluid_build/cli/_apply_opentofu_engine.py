# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The OpenTofu apply engine behind ``fluid apply``.

Compiles the contract to a ``.tf.json`` module and delegates to ``tofu``
(init → plan → apply). Engine selection is automatic and per-provider —
the cloud providers route here, ``local`` keeps its native apply; there
is no user-facing engine switch.

Flow: load contract → emit ``.tf.json`` (+ optional remote backend) →
``tofu init`` → ``tofu plan`` (the review point; ``--mode dry-run`` stops
here) → data-loss gate → ``tofu apply``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Mapping

from fluid_build.cli.console import cprint
from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.backend import parse_backend
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.credentials import build_tofu_env, credential_report
from fluid_build.iac.naming import safe_ident

from ._common import CLIError, load_contract_with_overlay, resolve_env_templates_in_contract
from ._logging import info, warn
from .generate_iac import _resolve_provider, native_actions


def apply_via_opentofu(args, logger: logging.Logger) -> int:
    """Run ``fluid apply`` through the OpenTofu engine. Returns an exit code."""
    # Plan-binding verification — must run BEFORE any tofu apply so a
    # tampered plan.json is rejected before infra changes. The native
    # apply path (cli/apply.py::_verify_plan_digests) had this gate; the
    # OpenTofu cutover initially bypassed it, which silently disabled
    # plan-binding for every cloud provider that cut over (aws / gcp /
    # snowflake). This is the same gate, replicated here so the
    # OpenTofu path matches the native path's stage-7 guarantee.
    _verify_plan_binding_for_opentofu(args, logger)

    contract = _load_contract(args, logger)
    provider = _resolve_provider(contract, getattr(args, "provider", None) or "auto")

    plugin = get_iac_plugin(provider)
    if plugin is None:
        raise CLIError(
            1,
            "opentofu_engine_unsupported_provider",
            {"error": f"no OpenTofu plugin for provider {provider!r}"},
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

    # ``contract`` selects the default state key: packaging-bearing contracts
    # get a per-contract key so two products sharing one state bucket cannot
    # clobber each other; legacy contracts keep the shared key (RFC file 7).
    backend = parse_backend(getattr(args, "state_backend", None), contract)
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
    workdir.mkdir(parents=True, exist_ok=True)
    module_path = workdir / "main.tf.json"
    actions = native_actions(contract, logger)
    try:
        module = build_module(plugin, contract, actions=actions, backend=backend)
    except UnsupportedBindingError as exc:
        # The emitter refused to substitute a different resource kind for the
        # declared binding. Surface it as a typed CLI error rather than a
        # traceback — and, critically, before anything reaches the warehouse.
        raise CLIError(
            1,
            "unsupported_binding",
            {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
        )
    module_path.write_text(module, encoding="utf-8")

    env = build_tofu_env()
    env.update(plugin.credential_env(env))
    present, _absent = credential_report(plugin, env)

    cprint(f"\nOpenTofu engine — provider: {provider}")
    cprint(f"  module:      {module_path}")
    cprint(f"  state:       {('remote: ' + next(iter(backend))) if backend else 'local'}")
    cprint(f"  credentials: {', '.join(present) if present else 'none detected in environment'}")

    init = runner.tofu_init(str(workdir), backend=backend is not None, env=env)
    if not init.ok:
        raise CLIError(1, "opentofu_init_failed", {"error": _tail(init.stderr or init.stdout)})

    # Pre-plan ownership-transition guard (RFC-packaging-modes.md file 10).
    # Runs BEFORE _adopt_existing — brownfield adoption is precisely the
    # mechanism that would re-own a shared pool — and before `tofu plan`,
    # which is where an ownership flip would otherwise first surface as a
    # destroy.
    _guard_packaging_transitions(contract, str(workdir), env, args, logger)

    _adopt_existing(plugin, contract, actions, str(workdir), env, logger)

    plan = runner.tofu_plan(str(workdir), env=env)
    if not plan.ok:
        raise CLIError(1, "opentofu_plan_failed", {"error": _tail(plan.stderr or plan.stdout)})
    changes = runner.change_summary(plan)
    cprint(f"\n  tofu plan: +{changes['add']} ~{changes['change']} -{changes['remove']}")

    # Data-loss gate — `tofu` has no CTAS/CLONE data snapshot (see
    # AUTOGEN_SPIKE.md, risk R1), so a destructive plan fails closed.
    allow_data_loss = bool(getattr(args, "allow_data_loss", False))
    if _data_loss_blocked(changes, allow_data_loss):
        raise CLIError(
            1,
            "opentofu_data_loss_gate",
            {
                "error": f"plan destroys {changes['remove']} resource(s); `tofu` does not "
                "snapshot data — re-run with --allow-data-loss to proceed"
            },
        )
    if allow_data_loss and int(changes.get("remove", 0)) > 0:
        # Audit-trail: every destructive apply through the override is
        # logged at WARNING so CI log-scrapers + operators have a
        # paper-trail. Matches the same posture as the native engine's
        # _verify_plan_binding bypass warning.
        logger.warning(
            "--allow-data-loss: bypassing the data-loss gate; %d resource(s) "
            "will be destroyed by `tofu apply`. Provider: %s. Plan changes: "
            "+%d ~%d -%d.",
            int(changes.get("remove", 0)),
            provider,
            changes["add"],
            changes["change"],
            changes["remove"],
        )
        warn(
            logger,
            "opentofu_destructive_gate_override",
            provider=provider,
            resources_to_destroy=int(changes.get("remove", 0)),
            **changes,
        )

    if bool(getattr(args, "dry_run", False)):
        cprint("\ndry-run: plan only — not applying.")
        info(logger, "opentofu_apply_dry_run", provider=provider, **changes)
        return 0

    apply_result = runner.tofu_apply(str(workdir), env=env)
    if not apply_result.ok:
        raise CLIError(
            1,
            "opentofu_apply_failed",
            {"error": _tail(apply_result.stderr or apply_result.stdout)},
        )
    applied = runner.change_summary(apply_result)
    cprint(f"\n  tofu apply complete: +{applied['add']} ~{applied['change']} -{applied['remove']}")

    info(logger, "opentofu_apply_ok", provider=provider, **applied)
    return 0


def resolve_apply_engine(args, logger: logging.Logger) -> str:
    """Resolve the apply engine for ``fluid apply`` — automatic, per-provider.

    There is no user-facing engine switch: the cloud providers (all cut
    over) compile the contract to ``.tf.json`` and run ``tofu``; ``local``
    keeps its native apply. The per-provider mapping lives in
    ``iac.cutover``. Any failure to classify the contract falls back to
    ``native`` — the safe default.
    """
    from fluid_build.iac.cutover import resolve_engine

    try:
        contract = _load_contract(args, logger)
        provider = _resolve_provider(contract, getattr(args, "provider", None) or "auto")
    except Exception:  # noqa: BLE001 — cannot classify the contract → safe default
        return "native"
    return resolve_engine(None, provider)


def _verify_plan_binding_for_opentofu(args, logger: logging.Logger) -> None:
    """Stage-7 plan-binding gate, OpenTofu-engine edition.

    Mirrors :func:`fluid_build.cli.apply._verify_plan_digests` — same
    semantics, replicated here to avoid a circular import (this module
    is imported by ``cli/apply.py``). When the apply input is a
    ``plan.json``, recompute ``planDigest`` over the plan body and
    re-verify the ``bundleDigest`` if present.

    Honours ``--no-verify-plan-binding`` (logged at WARNING). Plans
    without a ``planDigest`` are treated as tamper signals.
    """
    src = str(getattr(args, "contract", "") or "")
    if not src.endswith(".json"):
        return  # raw contract input — no plan to verify
    if getattr(args, "no_verify_plan_binding", False):
        logger.warning(
            "--no-verify-plan-binding: plan-binding verification was SKIPPED. "
            "This is an emergency escape hatch; the apply may be running "
            "against a tampered or stale plan. Make sure this is recorded "
            "in the change log."
        )
        return
    try:
        with open(src, encoding="utf-8") as handle:
            plan_data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(
            1,
            "opentofu_engine_plan_unreadable",
            {"error": f"{src}: {exc}"},
        )
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


def _load_contract(args, logger: logging.Logger) -> Dict[str, Any]:
    """Load the contract dict from a ``.fluid.yaml`` file or a ``.json`` plan.

    ``{{ env.* }}`` placeholders are resolved before the contract reaches the
    emitter — the OpenTofu data-plane is emitted straight from the contract's
    ``exposes[]``, so an unresolved template would otherwise land verbatim in
    the ``.tf.json``.
    """
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
) -> None:
    """Brownfield adoption — ``tofu import`` each declared resource that
    already exists in the cloud, so ``tofu apply`` reconciles it instead of
    failing "already exists" against pre-provisioned infrastructure.

    Best-effort: a candidate already in state is skipped; one that does not
    exist in the cloud fails to import and is left for ``tofu apply`` to
    create. Genuine apply-time errors still surface from ``tofu apply``.
    """
    candidates = plugin.discover_imports(contract, actions)
    if not candidates:
        return
    in_state = set(runner.tofu_state_list(workdir, env=env))
    adopted = 0
    for block in candidates:
        if block.to in in_state:
            continue
        result = runner.tofu_import(workdir, block.to, block.id, env=env)
        if result.ok:
            adopted += 1
        else:
            logger.debug(
                "opentofu: import skipped %s (id=%s): %s",
                block.to,
                block.id,
                _tail(result.stderr or result.stdout, 200),
            )
    if adopted:
        cprint(f"  brownfield:  adopted {adopted} pre-existing resource(s) into state")


def _guard_packaging_transitions(
    contract: Mapping[str, Any],
    workdir: str,
    env: Mapping[str, str],
    args,
    logger: logging.Logger,
) -> None:
    """Fail closed when a container's ownership would flip under existing state.

    Thin CLI adapter over ``iac.transition.guard_ownership_transitions``:
    the detection + remediation text live in ``iac/`` (no ``cli`` imports
    there), and this side owns the ``CLIError`` translation and the
    structured audit events the run record carries.

    A no-op for every contract without a ``packaging`` block (the LEGACY
    sentinel can never transition) and for a fresh workdir with no state.
    """
    from fluid_build.iac.transition import (
        PackagingTransitionError,
        guard_ownership_transitions,
    )

    state = runner.tofu_state_list(workdir, env=env)
    if not state:
        return
    try:
        adoptions = guard_ownership_transitions(
            contract,
            state,
            workdir=workdir,
            adopt_shared_container=bool(getattr(args, "adopt_shared_container", False)),
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


def _data_loss_blocked(changes: Mapping[str, int], allow_data_loss: bool) -> bool:
    """A plan that removes resources is blocked unless ``--allow-data-loss`` is set."""
    return int(changes.get("remove", 0)) > 0 and not allow_data_loss


def _tail(text: str, limit: int = 800) -> str:
    return (text or "")[-limit:]
