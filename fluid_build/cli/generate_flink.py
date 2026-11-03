"""Generate reviewable, deterministic Flink SQL and operator resources."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml


def register_subcommand(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("flink", help="Compile managed Flink SQL and operator resources")
    parser.add_argument("contract")
    parser.add_argument("--out", "-o", default="runtime/flink")
    parser.add_argument("--env")
    parser.set_defaults(generate_sub="flink", func=run)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build._contract_loader import CLIError, load_contract_with_overlay
    from fluid_build.cli.console import cprint
    from fluid_build.cli.plan import _gate_contract_for_plan_or_apply
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.streaming.compiler import compile_contract

    source = validate_cli_path(args.contract, mode="read", file_type="contract")
    output = validate_cli_path(args.out, mode="write", must_exist=False, file_type="output")
    contract = load_contract_with_overlay(str(source), getattr(args, "env", None), logger)
    _gate_contract_for_plan_or_apply(contract, logger, command="plan")
    artifacts = compile_contract(contract)
    if not artifacts:
        raise CLIError(
            1,
            "generate_flink_external",
            {
                "error": "No managed Flink build; opt in with properties.parameters.flink",
            },
        )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for artifact in artifacts:
        # Resource names are validated DNS labels, unlike arbitrary build IDs.
        metadata = artifact.resources[1]["metadata"]
        (output / metadata["namespace"]).mkdir(exist_ok=True)
        name = f"{metadata['namespace']}/{metadata['name']}"
        (output / f"{name}.sql").write_text(artifact.sql, encoding="utf-8")
        (output / f"{name}.yaml").write_text(
            yaml.safe_dump_all(artifact.resources, sort_keys=False),
            encoding="utf-8",
        )
        manifest.append(
            {
                "build": artifact.build_id,
                "digest": artifact.digest,
                "sql": f"{name}.sql",
                "resources": f"{name}.yaml",
            }
        )
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    cprint(f"Generated {len(artifacts)} managed Flink application(s) in {output}", markup=False)
    return 0
