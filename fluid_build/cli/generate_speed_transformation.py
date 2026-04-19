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

"""``fluid generate speed-transformation`` subcommand.

Generates transformation engine artifacts (dbt project, SQL scripts, etc.)
from a FLUID contract.  The engine is auto-detected from ``builds[].engine``.

Usage:
    fluid generate speed-transformation                              # discover contract + engine
    fluid generate speed-transformation contract.fluid.yaml          # specify contract
    fluid generate speed-transformation contract.fluid.yaml -o ./out # specify output dir
    fluid generate speed-transformation --list                       # show available engines
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli.console import cprint

from ._common import CLIError, load_contract_with_overlay
from ._logging import error, info

SUBCOMMAND = "speed-transformation"


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register the speed-transformation subcommand under ``fluid generate``."""
    p = subparsers.add_parser(
        SUBCOMMAND,
        help="Generate transformation artifacts (dbt, SQL, etc.) from FLUID contract",
        description="""
        Generate transformation engine artifacts (dbt project, SQL scripts, etc.)
        from a FLUID contract. The engine is auto-detected from builds[].engine.

        Works best when a data model is supplied (upstream contracts or an
        explicit modeling technique from ``fluid forge``).

        Supports dbt (generates full project), sql (generates SQL scripts),
        and is extensible to new engines.
        """,
        epilog="""
Examples:
  # Auto-discover contract and engine
  fluid generate speed-transformation

  # Specify contract path
  fluid generate speed-transformation contract.fluid.yaml

  # Specify output directory
  fluid generate speed-transformation contract.fluid.yaml -o ./dbt_project

  # List available engines
  fluid generate speed-transformation --list
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        "contract",
        nargs="?",
        default=None,
        help="Path to FLUID contract file (default: discover contract.fluid.yaml in CWD)",
    )
    p.add_argument(
        "--output",
        "-o",
        help="Output directory (default: from builds[].repository or ./<engine>_project)",
    )
    p.add_argument(
        "--build-index", type=int, default=0, help="Which build to generate for (default: 0)"
    )
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing output directory")
    p.add_argument("--env", help="Environment overlay to apply (dev/test/prod)")
    p.add_argument(
        "--list", dest="list_engines", action="store_true", help="List available engines and exit"
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    p.set_defaults(generate_sub=SUBCOMMAND, func=run)


def run(args: Any, logger: logging.Logger) -> int:
    """Execute speed-transformation artifact generation."""
    try:
        # --- List mode ---
        if getattr(args, "list_engines", False):
            return _list_engines()

        # --- Load contract ---
        contract_path = _resolve_contract_path(args)
        if args.verbose:
            info(logger, f"Loading contract from {contract_path}")

        contract = load_contract_with_overlay(
            str(contract_path), getattr(args, "env", None), logger
        )

        # --- Speed-transformation banner ---
        # Reads the modeling technique stamped by ``fluid forge`` and
        # tells the user whether the LLM / engine has the grounding it
        # needs to avoid skeleton-only output.
        _print_speed_transformation_banner(contract)

        # --- Resolve build ---
        from fluid_build.util.contract import get_build_engine, get_builds

        builds = get_builds(contract)
        build_index = getattr(args, "build_index", 0)

        if not builds:
            raise CLIError(1, "no_builds", {"path": str(contract_path)})

        if build_index >= len(builds):
            raise CLIError(
                1,
                "build_index_out_of_range",
                {
                    "index": build_index,
                    "count": len(builds),
                },
            )

        build = builds[build_index]
        engine_name = get_build_engine(build)

        if not engine_name:
            engine_name = "dbt"  # default engine

        # --- Look up engine ---
        from fluid_build.engines import get_engine, has_engine, list_engines

        if not has_engine(engine_name):
            # Map provider-specific engine names to base engines
            engine_map = {"dbt-bigquery": "dbt", "dbt-duckdb": "dbt"}
            engine_name = engine_map.get(engine_name, engine_name)

        engine = get_engine(engine_name)
        if engine is None:
            cprint(
                f"No generator available for engine '{engine_name}'.\n"
                f"Available engines: {', '.join(list_engines())}\n\n"
                f"The contract will still work with 'fluid apply' — you'll need\n"
                f"to write transformation code manually in the repository path."
            )
            return 1

        if args.verbose:
            info(logger, f"Using engine: {engine_name}")

        # --- Validate ---
        issues = engine.validate(contract, build)
        errors = [i for i in issues if i.severity.value == "error"]
        if errors:
            for issue in errors:
                error(logger, str(issue))
            return 1

        for issue in issues:
            if issue.severity.value == "warning":
                cprint(f"Warning: {issue}")

        # --- Generate ---
        # ``contract_path.parent`` is the workspace anchor used to locate
        # upstream ``contract.fluid.yaml`` files referenced by ``consumes[]``.
        files = engine.generate(
            contract,
            build,
            build_index=build_index,
            workspace_root=contract_path.parent,
        )

        if not files:
            cprint("No files generated.")
            return 0

        # --- Resolve output directory ---
        output_dir = _resolve_output_dir(args, build, engine_name)

        if output_dir.exists() and not getattr(args, "overwrite", False):
            # Check if directory is non-empty
            if any(output_dir.iterdir()):
                cprint(
                    f"Output directory '{output_dir}' already exists and is not empty.\n"
                    f"Use --overwrite to replace existing files."
                )
                return 1

        # --- Write files ---
        output_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in sorted(files.items()):
            file_path = output_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        # --- Summary ---
        _print_summary(output_dir, files, engine_name)
        return 0

    except CLIError as e:
        error(logger, f"{e.event}: {e.context}")
        return e.exit_code
    except Exception as e:
        error(logger, f"Error generating artifacts: {e}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1


def _print_speed_transformation_banner(contract: Dict[str, Any]) -> None:
    """Surface the modeling technique + user-data-model signal to the user.

    ``fluid forge`` stamps the chosen technique onto
    ``contract["labels"]["dataModelingTechnique"]``. FLUID 0.7.2's
    ``metadata`` block is closed to additional properties; ``labels``
    is the open string map where such annotations live.  The banner
    has three modes:

    * technique + user-supplied model → green (best case)
    * technique only → yellow (LLM works, but without a concrete data
      model it falls back to heuristic joins)
    * nothing to say → silent
    """
    labels = contract.get("labels") or {}
    metadata = contract.get("metadata") or {}
    technique = labels.get("dataModelingTechnique") or metadata.get("dataModelingTechnique")
    if not technique:
        return

    has_data_model = bool(
        metadata.get("user_data_model") or metadata.get("userDataModel") or labels.get("dataModel")
    )

    display = _display_technique(technique)
    if has_data_model:
        cprint(
            f"[green]Generating {display} speed-transformation grounded on your "
            f"supplied data model.[/green]"
        )
    else:
        cprint(
            f"[yellow]{display} speed-transformation works best when a data model "
            f"is supplied. Continuing without one — consider adding one via "
            f"`fluid forge` or labels.dataModel.[/yellow]"
        )


def _display_technique(technique: str) -> str:
    return {
        "data_vault_2": "Data Vault 2.0",
        "dimensional": "dimensional (Kimball)",
    }.get(technique, technique)


def _resolve_contract_path(args: Any) -> Path:
    """Find the contract file."""
    if args.contract:
        path = Path(args.contract)
        if not path.exists():
            raise CLIError(1, "contract_not_found", {"path": str(path)})
        return path

    # Auto-discover in CWD
    for name in ("contract.fluid.yaml", "contract.fluid.json"):
        candidate = Path.cwd() / name
        if candidate.exists():
            return candidate

    raise CLIError(
        1,
        "no_contract_found",
        {
            "cwd": str(Path.cwd()),
            "hint": "Specify a contract path or run from a directory with contract.fluid.yaml",
        },
    )


def _resolve_output_dir(args: Any, build: Dict[str, Any], engine_name: str) -> Path:
    """Determine the output directory."""
    if getattr(args, "output", None):
        return Path(args.output)

    # Use builds[].repository if set
    repository = build.get("repository")
    if repository:
        return Path(repository)

    # Default: ./<engine>_project
    return Path.cwd() / f"{engine_name}_project"


def _list_engines() -> int:
    """List available transformation engines."""
    from fluid_build.engines import list_engines

    engines = list_engines()
    if not engines:
        cprint("No transformation engines registered.")
        return 0

    cprint("Available transformation engines:\n")
    for name in engines:
        from fluid_build.engines import get_engine

        engine = get_engine(name)
        patterns = ", ".join(engine.supported_patterns) if engine else ""
        cprint(f"  {name:12s} patterns: {patterns}")

    cprint("\nUsage: fluid generate speed-transformation [contract.fluid.yaml]")
    cprint("Engine is auto-detected from builds[].engine in the contract.")
    return 0


def _print_summary(output_dir: Path, files: Dict[str, str], engine_name: str) -> None:
    """Print a clean summary of generated files."""
    cprint(f"\nGenerated {len(files)} files ({engine_name} engine):\n")

    # Group by directory
    dirs: Dict[str, list] = {}
    for rel_path in sorted(files.keys()):
        parts = rel_path.split("/")
        if len(parts) > 1:
            dir_name = "/".join(parts[:-1])
        else:
            dir_name = "."
        dirs.setdefault(dir_name, []).append(parts[-1])

    for dir_name, file_names in sorted(dirs.items()):
        if dir_name == ".":
            for f in file_names:
                cprint(f"  {output_dir / f}")
        else:
            cprint(f"  {output_dir / dir_name}/")
            for f in file_names:
                cprint(f"    {f}")

    cprint("\nTip: To regenerate after editing the contract: fluid generate speed-transformation")
