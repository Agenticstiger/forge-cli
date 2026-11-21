# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate pyspark-schema`` subcommand.

Writes one PySpark ``StructType`` module for each output port of a contract that declares
``contract.schema`` columns, so a Spark job imports the port's schema instead of inferring or
retyping it. The command never imports pyspark and never connects anywhere: the modules are
text, and the one that imports pyspark is the job that imports them.

Mirrors ``fluid generate vector`` and ``fluid generate iac`` (contract in, files out, no side
effects), and adds ``--check``, which writes nothing and fails when a module is missing or
differs from what the contract generates, for a CI job that keeps committed modules current.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from fluid_build.cli.console import cprint, cprint_literal

from ._common import CLIError, load_contract_with_overlay
from ._logging import info


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "pyspark-schema",
        help="Generate a PySpark StructType module for each output port",
        description=(
            "Generate PySpark StructType modules from a FLUID contract.\n\n"
            "Writes <port>_schema.py for each output port that declares\n"
            "`contract.schema` columns. Each module defines SCHEMA, a StructType\n"
            "with the port's column names, types and nullability (`required: true` is\n"
            "nullable=False), for a Spark job to import. forge-cli never imports\n"
            "pyspark: only the module does, when the job imports it.\n\n"
            "A column type Spark has no form for is written as a string and warned\n"
            "about (json, variant, time, interval); one it cannot hold at all (a\n"
            "spatial type, a decimal past 38 digits, a struct with no declared fields)\n"
            "is refused and nothing is written. The command replaces only files it\n"
            "wrote itself, and never writes an __init__.py or deletes a file."
        ),
        epilog="""Examples:
  fluid generate pyspark-schema contract.fluid.yaml
  fluid generate pyspark-schema contract.fluid.yaml --out jobs/schemas
  # In CI: fail when a committed module is missing, out of date or no longer in the contract
  fluid generate pyspark-schema contract.fluid.yaml --out jobs/schemas --check

The default --out is under runtime/, which `fluid init` git-ignores: pass the directory your
jobs import from to commit the modules.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", help="contract.fluid.yaml")
    p.add_argument(
        "--out",
        "-o",
        default="runtime/pyspark_schema",
        help="Output directory (default: runtime/pyspark_schema)",
    )
    p.add_argument("--env", help="Environment overlay")
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Write nothing; exit 1 when a module is missing or differs from what the "
            "contract generates"
        ),
    )
    p.set_defaults(generate_sub="pyspark-schema", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate pyspark-schema``."""
    return run(args, logger)


def run(args, logger: logging.Logger) -> int:
    contract_path = getattr(args, "contract", None)
    if not contract_path:
        cprint("Error: contract path is required.")
        return 1

    # Deferred: the emitter, which `--help` does not need.
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.spark import pyspark_schema

    out_dir = Path(getattr(args, "out", None) or "runtime/pyspark_schema")
    check = bool(getattr(args, "check", False))
    try:
        contract = load_contract_with_overlay(contract_path, getattr(args, "env", None), logger)
        rendered = pyspark_schema.render_contract(contract)
        planned = pyspark_schema.plan(rendered.modules, out_dir)
        orphaned = pyspark_schema.orphans(out_dir, rendered.modules, rendered.product)
        foreign = [str(p.path) for p in planned if p.status == "foreign"]
        if foreign:
            raise CLIError(1, "generate_pyspark_schema_foreign_file", {"files": ", ".join(foreign)})
        # Another product's module in a shared --out: refused whole, nothing written, so a run for
        # one product never replaces what a run for another wrote (and --check says the same).
        other = [p for p in planned if p.status == "other_product"]
        if other:
            raise CLIError(
                1,
                "generate_pyspark_schema_other_product",
                {
                    "files": ", ".join(
                        f"{p.path} (generated for {p.owner or 'no named product'})" for p in other
                    )
                },
            )
    except CLIError:
        raise
    except UnsupportedBindingError as exc:
        # A column Spark cannot hold, or a contract that is not shaped like one: the remediation
        # is in the contract, not the tooling.
        raise CLIError(
            1,
            "unsupported_binding",
            {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
        )
    except Exception as exc:
        raise CLIError(1, "generate_pyspark_schema_failed", {"error": str(exc)})

    # Said before anything is written, so a run that stops partway has already said it all.
    for module in rendered.modules:
        for warning in module.warnings:
            cprint_literal(f"  warning: {warning}")
    if rendered.skipped:
        cprint_literal(
            "  note: no module for the output port(s) without contract.schema columns: "
            + ", ".join(rendered.skipped)
        )
    if orphaned and not check:
        cprint_literal(
            "  note: not generated from this contract any more (this command never deletes; "
            "remove them if their ports are gone): " + ", ".join(orphaned)
        )

    if not rendered.modules and not (check and orphaned):
        cprint("\nWarning: no output port declares contract.schema columns, nothing to generate.")
        return 0
    if check:
        return _report_check(planned, orphaned, out_dir, logger)

    try:
        pyspark_schema.write(planned)
    except Exception as exc:
        raise CLIError(1, "generate_pyspark_schema_failed", {"error": str(exc)})
    info(
        logger,
        "generate_pyspark_schema_ok",
        modules=len(planned),
        out=str(out_dir),
    )
    cprint_literal(f"\nWrote {len(planned)} PySpark schema module(s) to {out_dir}:")
    for item in planned:
        columns = item.module.columns
        cprint_literal(
            f"  {item.module.filename}  output port {item.module.port!r}, "
            f"{columns} column{'s' if columns != 1 else ''} ({_wrote(item.status)})"
        )
    cprint("\nPut the directory on the job's path (spark-submit --py-files, or PYTHONPATH), then:")
    cprint(f"  from {planned[0].path.stem} import SCHEMA")
    cprint("  df = spark.read.schema(SCHEMA).parquet(path)")
    return 0


def _wrote(status: str) -> str:
    return {"new": "new", "stale": "updated", "unchanged": "unchanged"}.get(status, status)


def _report_check(planned, orphaned, out_dir: Path, logger: logging.Logger) -> int:
    missing = [p.module.filename for p in planned if p.status == "new"]
    stale = [p.module.filename for p in planned if p.status == "stale"]
    if not missing and not stale and not orphaned:
        info(logger, "generate_pyspark_schema_current", modules=len(planned), out=str(out_dir))
        cprint_literal(f"PySpark schema modules in {out_dir} are up to date ({len(planned)}).")
        return 0
    context = {}
    if missing:
        context["missing"] = ", ".join(missing)
    if stale:
        context["differs"] = ", ".join(stale)
    if orphaned:
        context["not in the contract any more"] = ", ".join(orphaned)
    context["out"] = str(out_dir)
    raise CLIError(1, "generate_pyspark_schema_drift", context)
