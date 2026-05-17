# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid generate iac`` subcommand.

Compiles a FLUID contract into an OpenTofu ``.tf.json`` module — the
autogenerator path. The module is emitted for review; this command does
not run ``tofu`` itself (apply it yourself, or use ``fluid apply
--engine opentofu`` once that lands).
"""

from __future__ import annotations

import argparse
import logging
import os

from fluid_build.cli.console import cprint
from fluid_build.iac import IAC_PLUGINS, assemble_tofu_document, get_iac_plugin, render_tofu_json

from ._common import CLIError, load_contract_with_overlay
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
        provider = _resolve_provider(contract, getattr(args, "provider", "auto"))
        plugin = get_iac_plugin(provider)
        resources = plugin.emit(contract)
        count = sum(len(items) for items in resources.values())
        document = assemble_tofu_document(
            required_providers=plugin.required_providers, resources=resources
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
    cprint("\nReview and apply with OpenTofu:")
    cprint(f"  tofu -chdir={out_dir} init")
    cprint(f"  tofu -chdir={out_dir} plan")
    return 0


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
