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
from fluid_build.iac.credentials import build_tofu_env, credential_report
from fluid_build.iac.naming import safe_ident

from ._common import CLIError, load_contract_with_overlay, resolve_env_templates_in_contract
from ._logging import info
from .generate_iac import _resolve_provider, native_actions


def apply_via_opentofu(args, logger: logging.Logger) -> int:
    """Run ``fluid apply`` through the OpenTofu engine. Returns an exit code."""
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
        raise CLIError(
            1,
            "opentofu_engine_no_tofu",
            {
                "error": "the `tofu` binary is required to provision cloud "
                "infrastructure — install it from "
                "https://opentofu.org/docs/intro/install/"
            },
        )

    backend = parse_backend(getattr(args, "state_backend", None))
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
    module_path.write_text(
        build_module(plugin, contract, actions=actions, backend=backend), encoding="utf-8"
    )

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

    _adopt_existing(plugin, contract, actions, str(workdir), env, logger)

    plan = runner.tofu_plan(str(workdir), env=env)
    if not plan.ok:
        raise CLIError(1, "opentofu_plan_failed", {"error": _tail(plan.stderr or plan.stdout)})
    changes = runner.change_summary(plan)
    cprint(f"\n  tofu plan: +{changes['add']} ~{changes['change']} -{changes['remove']}")

    # Data-loss gate — `tofu` has no CTAS/CLONE data snapshot (see
    # AUTOGEN_SPIKE.md, risk R1), so a destructive plan fails closed.
    if _data_loss_blocked(changes, bool(getattr(args, "allow_data_loss", False))):
        raise CLIError(
            1,
            "opentofu_data_loss_gate",
            {
                "error": f"plan destroys {changes['remove']} resource(s); `tofu` does not "
                "snapshot data — re-run with --allow-data-loss to proceed"
            },
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


def _data_loss_blocked(changes: Mapping[str, int], allow_data_loss: bool) -> bool:
    """A plan that removes resources is blocked unless ``--allow-data-loss`` is set."""
    return int(changes.get("remove", 0)) > 0 and not allow_data_loss


def _tail(text: str, limit: int = 800) -> str:
    return (text or "")[-limit:]
