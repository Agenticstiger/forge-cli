# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid genie {apply,remove}``: one Databricks AI/BI Genie space over several data products.

``fluid genie apply <contracts|globs|folders...> --estate <id>`` creates or updates the
estate's space, over the Databricks tables of every contract given, with joins from the
foreign keys they declare (``iac/providers/databricks_genie_estate.py``). ``fluid genie remove
--estate <id>`` trashes it. A product's own space comes from ``fluid apply`` and
``extensions.databricksGenie`` instead (``databricks_genie.py``).

Light at import: only the parser. The provider modules load when a subcommand runs, so the
``fluid --help`` cold path never imports them.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, List, Mapping, Sequence, Tuple

from fluid_build.cli._contract_paths import contract_paths as _expand_contract_paths

COMMAND = "genie"

_DESCRIPTION = """\
One Databricks AI/BI Genie space over several data products (an estate).

The space holds the Databricks tables of every contract given, with what each contract says
of its columns, its example questions and one instruction, and a join for every foreign key
the contracts declare between those tables (a column tagged foreign-key whose
labels.references is <product id>.<column>). Apply the products first (fluid apply): the
space opens on tables that exist. Only the principals that read every table of the estate
are given CAN RUN on it.
"""


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register ``fluid genie {apply,remove}``."""
    p = subparsers.add_parser(
        COMMAND,
        help="Databricks Genie space over several data products (apply · remove)",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="genie_command", required=True)

    apply = sub.add_parser(
        "apply",
        help="Create or update the estate's Genie space",
        description=_DESCRIPTION
        + "\nExample:\n  fluid genie apply tower-management/ --estate tower-management "
        '--title "Tower Management"\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply.add_argument(
        "contracts",
        nargs="+",
        metavar="CONTRACT",
        help="contract files, glob patterns, or folders (searched for *.fluid.yaml at any depth)",
    )
    _estate_argument(apply)
    apply.add_argument("--title", default="", help="the space's title (default: the estate id)")
    apply.add_argument(
        "--env",
        default=None,
        help="the overlay environment to read each contract with, as for fluid apply --env",
    )
    apply.add_argument(
        "--dry-run",
        action="store_true",
        help="read the workspace and print what would change; write nothing",
    )
    apply.set_defaults(func=run_apply)

    remove = sub.add_parser(
        "remove",
        help="Trash the estate's Genie space",
        description="Trash the estate's Genie space: the one an apply with this --estate made "
        "from this identity.",
    )
    _estate_argument(remove)
    remove.add_argument(
        "--dry-run", action="store_true", help="print what would be trashed; trash nothing"
    )
    remove.set_defaults(func=run_remove)


def _estate_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--estate",
        required=True,
        metavar="ID",
        help="the estate's id (letters, digits, '.', '_', '-'): the next run finds the space "
        "by it",
    )


def run_apply(args: argparse.Namespace, logger: logging.Logger) -> int:
    """``fluid genie apply``: compile the estate from the contracts, then converge its space."""
    from fluid_build.cli._logging import info
    from fluid_build.cli.console import cprint_literal
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.providers.databricks import genie_estate
    from fluid_build.iac.providers.databricks_genie_estate import converge_estate

    # The loader says "overlay_applied" for each contract it reads (``fluid.loader``), which for
    # a pack is one line per product; the header names the overlay once instead. A missing
    # overlay still warns, and --log-level DEBUG shows every line.
    loader_log = logging.getLogger("fluid.loader")
    level = loader_log.level
    if logger.getEffectiveLevel() > logging.DEBUG:
        loader_log.setLevel(logging.WARNING)
    try:
        contracts: List[Tuple[str, Mapping[str, Any]]] = [
            (label, _contract(label, path, args.env, logger))
            for label, path in contract_paths(args.contracts)
        ]
    finally:
        loader_log.setLevel(level)
    estate = genie_estate(args.estate, contracts, title=args.title)
    say = cprint_literal
    header = f"Genie estate {args.estate}: {len(contracts)} contract(s)"
    if args.env:
        header += f" (--env {args.env})"
    if estate.space is not None:
        header += f", {len(estate.owners)} table(s), {estate.joins} join(s)"
    say(header)
    for warning in estate.warnings:
        say(f"  note: {warning}")
    for refusal in estate.refusals[1:]:
        say(f"  also refused: {refusal} {' '.join(refusal.remediation)}".rstrip())
    try:
        converge_estate(estate, say, dry_run=args.dry_run)
    except UnsupportedBindingError as exc:
        raise _cli_error(exc) from exc
    info(
        logger,
        "genie_estate_dry_run" if args.dry_run else "genie_estate_applied",
        estate=args.estate,
        products=len(contracts),
        tables=len(estate.owners),
        joins=estate.joins,
    )
    return 0


def run_remove(args: argparse.Namespace, logger: logging.Logger) -> int:
    """``fluid genie remove``: trash the estate's space."""
    from fluid_build.cli._logging import info
    from fluid_build.cli.console import cprint_literal
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.providers.databricks_genie_estate import remove_estate

    try:
        trashed = remove_estate(args.estate, cprint_literal, dry_run=args.dry_run)
    except UnsupportedBindingError as exc:
        raise _cli_error(exc) from exc
    info(
        logger,
        "genie_estate_remove_dry_run" if args.dry_run else "genie_estate_removed",
        estate=args.estate,
        spaces=trashed,
    )
    return 0


def _contract(label: str, path: Path, env: Any, logger: logging.Logger) -> Mapping[str, Any]:
    """One contract, read with its overlay and gated as ``fluid apply`` gates one (a pre-0.7
    ``fluidVersion`` or a schema-invalid document stops the run); a failure names the file."""
    from fluid_build._errors import FluidUserError
    from fluid_build.cli._common import CLIError, load_contract_with_overlay
    from fluid_build.cli._common import resolve_env_templates_in_contract as resolve
    from fluid_build.cli.plan import _gate_contract_for_plan_or_apply

    try:
        contract = resolve(load_contract_with_overlay(str(path), env, logger))
    except (CLIError, FluidUserError):
        raise
    except Exception as exc:  # noqa: BLE001 — a bad file is a typed error, as in fluid apply
        raise CLIError(
            1, "contract_load_failed", {"path": label, "env": env, "error": str(exc)}
        ) from exc
    try:
        _gate_contract_for_plan_or_apply(contract, logger, command="genie")
    except CLIError as exc:
        exc.context.setdefault("path", label)
        raise
    return contract


def contract_paths(items: Sequence[str]) -> List[Tuple[str, Path]]:
    """``(label, path)`` per contract the arguments name (files, globs, folders; see
    :func:`fluid_build.cli._contract_paths.contract_paths`). A folder with none is the
    estate's own ``genie_estate_no_contracts`` error."""
    return _expand_contract_paths(items, empty_event="genie_estate_no_contracts")


def _cli_error(exc: Any) -> Any:
    """A refusal or a Genie API failure as the CLI's typed error, with what to do."""
    from fluid_build.cli._common import CLIError
    from fluid_build.iac.providers.databricks_genie import GenieError

    event = "genie_estate_failed" if isinstance(exc, GenieError) else "genie_estate_refused"
    return CLIError(
        1,
        event,
        {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
    )
