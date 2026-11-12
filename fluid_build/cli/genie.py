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
from typing import Any, Dict, List, Mapping, Sequence, Tuple

COMMAND = "genie"
#: The file names a folder argument is searched for.
CONTRACT_SUFFIXES = (".fluid.yaml", ".fluid.yml", ".fluid.json")
_GLOB_CHARS = frozenset("*?[")

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
        help="contract files, glob patterns, or folders (searched for *.fluid.yaml)",
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
    from fluid_build.cli._common import load_contract_with_overlay
    from fluid_build.cli._common import resolve_env_templates_in_contract as resolve
    from fluid_build.cli._logging import info
    from fluid_build.cli.console import cprint_literal
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.providers.databricks import genie_estate
    from fluid_build.iac.providers.databricks_genie_estate import converge_estate

    contracts: List[Tuple[str, Mapping[str, Any]]] = [
        (label, resolve(load_contract_with_overlay(str(path), args.env, logger)))
        for label, path in contract_paths(args.contracts)
    ]
    estate = genie_estate(args.estate, contracts, title=args.title)
    say = cprint_literal
    say(
        f"Genie estate {args.estate}: {len(contracts)} contract(s), "
        f"{len(estate.owners)} table(s), {estate.joins} join(s)"
    )
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


def contract_paths(items: Sequence[str]) -> List[Tuple[str, Path]]:
    """``(label, path)`` per contract: each item a file, a glob pattern (``**`` too) or a
    folder searched for :data:`CONTRACT_SUFFIXES`, in that order, each file once. A folder or
    a glob reaches no symlink and no ignored or build-output folder (``.git``, ``node_modules``,
    ``.venv``, ``target``, ...) below where it starts; a file named outright is read as named.
    Every path passes the CLI's path validator (``cli/security.py``) before it is read."""
    from glob import glob

    from fluid_build.cli._common import CLIError
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.util.upstream_discovery import BUILD_OUTPUT_DIRS, IGNORED_DIRS

    skip = IGNORED_DIRS | BUILD_OUTPUT_DIRS
    found: List[Path] = []
    for item in items:
        if _GLOB_CHARS & set(item):
            base = _glob_base(item)
            matches = [
                m for m in sorted(glob(item, recursive=True)) if _reachable(Path(m), base, skip)
            ]
        else:
            matches = [item]
        if not matches:
            raise CLIError(1, "contract_file_not_found", {"path": item})
        for match in matches:
            path = Path(match)
            if path.is_dir():
                inside = _contracts_in(path, skip)
                if not inside:
                    raise CLIError(
                        1,
                        "genie_estate_no_contracts",
                        {
                            "path": match,
                            "error": f"{match} holds no {' / '.join(CONTRACT_SUFFIXES)} file",
                        },
                    )
                found += inside
            else:
                found.append(path)
    missing = [str(p) for p in found if not p.is_file()]
    if missing:
        raise CLIError(1, "contract_file_not_found", {"path": ", ".join(missing)})
    unique: Dict[Path, Path] = {}
    for path in found:
        unique.setdefault(path.resolve(), path)
    return [
        (str(given), validate_cli_path(given, mode="read", file_type="contract"))
        for given in unique.values()
    ]


def _glob_base(pattern: str) -> Path:
    """The folder a glob pattern starts from: its parts before the first with a wildcard."""
    plain: List[str] = []
    for part in Path(pattern).parts:
        if _GLOB_CHARS & set(part):
            break
        plain.append(part)
    return Path(*plain) if plain else Path(".")


def _reachable(path: Path, base: Path, skip: frozenset) -> bool:
    """Whether a glob's match is one a folder walk from ``base`` reaches: no symlink on the way
    and no ignored folder below ``base``."""
    try:
        below = path.relative_to(base).parts
    except ValueError:
        below = path.parts
    current = base
    for part in below:
        current = current / part
        if current.is_symlink():
            return False
    return not any(part in skip for part in below[:-1])


def _contracts_in(folder: Path, skip: frozenset) -> List[Path]:
    """The contract files under ``folder``, in name order, past no symlink and into no
    ignored or build-output folder."""
    out: List[Path] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name not in skip:
                out += _contracts_in(entry, skip)
        elif entry.is_file() and entry.name.endswith(CONTRACT_SUFFIXES):
            out.append(entry)
    return out


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
