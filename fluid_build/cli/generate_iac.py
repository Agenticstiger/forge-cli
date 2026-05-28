# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid generate iac`` subcommand.

Compiles a FLUID contract into an OpenTofu ``.tf.json`` module — the
autogenerator path. The module is emitted for review; this command never
provisions anything (apply it yourself, or run ``fluid apply``, which
provisions cloud contracts through the OpenTofu engine). Pass ``--validate``
to additionally run ``tofu validate`` on the emitted module.
"""

from __future__ import annotations

import argparse
import logging
import os

from fluid_build.cli.console import cprint
from fluid_build.iac import IAC_PLUGINS, assemble_tofu_document, get_iac_plugin, render_tofu_json

from ._common import CLIError, load_contract_with_overlay, resolve_env_templates_in_contract
from ._logging import info

_PROVIDER_CHOICES = ["auto", *sorted(IAC_PLUGINS)]


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "iac",
        help="Compile a contract to an OpenTofu .tf.json module",
        description=(
            "Compile a FLUID contract into an OpenTofu `.tf.json` module.\n\n"
            "The module is emitted for review — apply it with `tofu`.\n"
            "Supported clouds: " + ", ".join(sorted(IAC_PLUGINS)) + "."
        ),
        epilog="""Examples:
  fluid generate iac contract.fluid.yaml
  fluid generate iac contract.fluid.yaml --provider gcp --out infra/
  fluid generate iac contract.fluid.yaml --validate
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", help="contract.fluid.yaml")
    p.add_argument(
        "--provider",
        choices=_PROVIDER_CHOICES,
        default="auto",
        help="Target cloud (default: auto-detect from the contract)",
    )
    p.add_argument("--out", "-o", default="runtime/iac", help="Output directory")
    p.add_argument("--env", help="Environment overlay")
    p.add_argument(
        "--shadow",
        action="store_true",
        help="After emitting, shadow-compare resource parity against the native planner",
    )
    p.add_argument(
        "--validate",
        action="store_true",
        help="After emitting, run `tofu validate` on the module (needs `tofu` on PATH)",
    )
    p.set_defaults(generate_sub="iac", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate iac``."""
    return run(args, logger)


def run(args, logger: logging.Logger) -> int:
    contract_path = getattr(args, "contract", None)
    if not contract_path:
        cprint("Error: contract path is required.")
        return 1

    try:
        contract = load_contract_with_overlay(contract_path, getattr(args, "env", None), logger)
        contract = resolve_env_templates_in_contract(contract)
        provider = _resolve_provider(contract, getattr(args, "provider", "auto"))
        plugin = get_iac_plugin(provider)
        actions = native_actions(contract, logger)
        resources = plugin.emit(contract, actions)
        count = sum(len(items) for items in resources.values())
        provider_cfg = plugin.provider_block()
        document = assemble_tofu_document(
            required_providers=plugin.required_providers,
            resources=resources,
            data=plugin.emit_data(contract, actions),
            # `.tf.json` keys the provider block by the provider's local name.
            provider={plugin.name: provider_cfg} if provider_cfg else None,
        )
        out_dir = getattr(args, "out", None) or "runtime/iac"
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "main.tf.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render_tofu_json(document))
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "generate_iac_failed", {"error": str(e)})

    info(logger, "generate_iac_ok", provider=provider, resources=count, out=out_path)
    if count == 0:
        cprint(
            f"\nWarning: no {provider} resources found in the contract — emitted an empty module."
        )
    cprint(f"\nWrote OpenTofu module: {out_path}  (provider: {provider}, {count} resources)")

    if getattr(args, "validate", False):
        _validate_with_tofu(out_dir)

    cprint("\nReview and apply with OpenTofu:")
    cprint(f"  tofu -chdir={out_dir} init")
    cprint(f"  tofu -chdir={out_dir} plan")

    if getattr(args, "shadow", False):
        _print_shadow_report(contract, plugin, logger)
    return 0


def _validate_with_tofu(out_dir: str) -> None:
    """Run ``tofu init -backend=false`` + ``tofu validate`` on the emitted module.

    ``tofu validate`` needs the provider schemas, so ``init`` (provider
    download, no backend) runs first — registry network access is required
    on the first run; later runs reuse the cached ``.terraform`` directory.
    Raises ``CLIError`` when ``tofu`` is absent or either step fails, so
    ``--validate`` is a usable CI gate.
    """
    from fluid_build.iac import runner

    if runner.tofu_path() is None:
        raise CLIError(
            1,
            "generate_iac_no_tofu",
            {
                "error": (
                    "--validate needs the `tofu` (OpenTofu) binary on PATH — install "
                    "it from https://opentofu.org/docs/intro/install/"
                )
            },
        )
    cprint("\nValidating with OpenTofu (running tofu init + validate)...")
    init = runner.tofu_init(out_dir, backend=False)
    if not init.ok:
        raise CLIError(
            1,
            "generate_iac_validate_failed",
            {"error": f"`tofu init` failed:\n{init.stderr or init.stdout}"},
        )
    result = runner.tofu_validate(out_dir)
    if not result.ok:
        raise CLIError(
            1,
            "generate_iac_validate_failed",
            {"error": f"`tofu validate` failed:\n{result.stderr or result.stdout}"},
        )
    cprint("OpenTofu validation passed.")


def _resolve_provider(contract, requested: str) -> str:
    """Return the IaC plugin name for the contract, or raise ``CLIError``."""
    if requested and requested != "auto":
        return requested

    found = []
    for exposure in contract.get("exposes") or []:
        platform = str((exposure.get("binding") or {}).get("platform") or "").lower()
        if platform in IAC_PLUGINS and platform not in found:
            found.append(platform)

    if len(found) == 1:
        return found[0]
    supported = "/".join(sorted(IAC_PLUGINS))
    if not found:
        raise CLIError(
            1,
            "generate_iac_no_provider",
            {"error": f"could not detect a supported cloud — pass --provider ({supported})"},
        )
    raise CLIError(
        1,
        "generate_iac_ambiguous_provider",
        {"error": f"contract spans multiple clouds {found} — pass --provider explicitly"},
    )


def native_actions(contract, logger: logging.Logger) -> list:
    """Best-effort native ``provider.plan()`` actions for the contract.

    The OpenTofu emitter consumes these to translate the schedule /
    orchestration ops the planner interprets (see ``iac.base``); shadow-
    compare diffs them against the emitter's output. Returns ``[]`` when
    the native provider cannot be constructed (e.g. no credentials) — the
    emitter then falls back to the ``exposes[]`` data-plane only.
    """
    try:
        from ._common import build_provider, resolve_provider_from_contract

        name, loc = resolve_provider_from_contract(contract)
        native = build_provider(name, loc.get("project"), loc.get("region"), logger)
        if hasattr(native, "plan"):
            return list(native.plan(contract))
    except Exception as exc:  # noqa: BLE001 — native planner is best-effort
        logger.debug("shadow: native planner unavailable: %s", exc)
    return []


def _print_shadow_report(contract, plugin, logger: logging.Logger) -> None:
    """Run shadow-compare and print the native↔OpenTofu parity report."""
    from fluid_build.iac import shadow_compare

    actions = native_actions(contract, logger)
    if not actions:
        cprint(
            "\nShadow-compare: native planner produced no actions "
            "(no credentials, or provider unavailable) — emitted OpenTofu only."
        )
        return
    report = shadow_compare(contract, plugin=plugin, native_actions=actions)
    cprint(f"\nShadow-compare — {report.summary()}")
    for res in report.native_only:
        cprint(f"  native-only (OpenTofu gap): {res.kind} {res.identity}")
    for res in report.opentofu_only:
        cprint(f"  opentofu-only: {res.kind} {res.identity}")
