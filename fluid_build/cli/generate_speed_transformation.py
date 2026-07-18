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

"""``fluid generate transformation`` subcommand.

Generates transformation engine artifacts (dbt project, SQL scripts, etc.)
from a FLUID contract.  The engine is auto-detected from ``builds[].engine``.

Usage:
    fluid generate transformation                              # discover contract + engine
    fluid generate transformation contract.fluid.yaml          # specify contract
    fluid generate transformation contract.fluid.yaml -o ./out # specify output dir
    fluid generate transformation --list                       # show available engines
"""

from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fluid_build.cli.console import cprint
from fluid_build.cli.forge_banner import print_v2_banner

from ._common import CLIError, load_contract_with_overlay
from ._logging import error, info

SUBCOMMAND = "speed-transformation"
PUBLIC_SUBCOMMAND = "transformation"


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register the speed-transformation subcommand under ``fluid generate``."""
    p = subparsers.add_parser(
        PUBLIC_SUBCOMMAND,
        aliases=[SUBCOMMAND, "dbt"],
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
  fluid generate transformation

  # Specify contract path
  fluid generate transformation contract.fluid.yaml

  # Specify output directory
  fluid generate transformation contract.fluid.yaml -o ./dbt_project

  # List available engines
  fluid generate transformation --list
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the v2-preview banner (also honours $FLUID_QUIET / $FLUID_NONINTERACTIVE).",
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
    p.add_argument("--model", help="Path to a forged logical sidecar (*.model.json)")
    p.add_argument(
        "--all-builds",
        action="store_true",
        help="Generate artifacts for every build in the contract",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max parallel build generations when using --all-builds (default: 4)",
    )
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing output directory")
    p.add_argument("--env", help="Environment overlay to apply (dev/test/prod)")
    p.add_argument(
        "--list", dest="list_engines", action="store_true", help="List available engines and exit"
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    p.add_argument(
        "--mesh-hub",
        default=None,
        metavar="HUB_PROJECT_NAME",
        help=(
            "dbt Mesh hub project this data product participates in. "
            "When set, emits a ``dependencies.yml`` declaring the hub "
            "so ``dbt deps`` pulls it in. Every model in ``exposes[]`` "
            "also gets ``access: public`` on its emitted schema.yml "
            "entry regardless of this flag — the --mesh-hub addition is "
            "only for products that reference a central hub. Requires "
            "dbt-core >= 1.6."
        ),
    )
    p.add_argument(
        "--model-contracts",
        action="store_true",
        help=(
            "Emit dbt model contracts on every expose model: "
            "``config: {contract: {enforced: true}}`` plus per-column "
            "``data_type`` and ``constraints`` derived from "
            "``exposes[].contract.schema[]``. ``dbt build`` then fails in "
            "producer CI whenever the model's output diverges from the "
            "contract schema (dbt-core >= 1.5). Opt-in because enforcement "
            "fails builds for already-drifted user SQL. dbt-only; ignored "
            "with a warning for other engines."
        ),
    )
    p.add_argument(
        "--dbt-validate",
        action="store_true",
        help=(
            "After generating a dbt project, run ``dbt parse`` against it "
            "to catch Jinja / ref / config issues before handing off to a "
            "user. Requires dbt-core on PATH. Silently skipped when the "
            "engine is not dbt."
        ),
    )

    p.set_defaults(generate_sub=SUBCOMMAND, func=run)


def run(args: Any, logger: logging.Logger) -> int:
    """Execute speed-transformation artifact generation."""
    try:
        print_v2_banner("speed_transformation", quiet=getattr(args, "quiet", False))
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
        from fluid_build.util.contract import get_builds

        builds = get_builds(contract)
        build_index = getattr(args, "build_index", 0)
        logical_model = _load_logical_model(args, contract_path, contract)

        if not builds:
            raise CLIError(1, "no_builds", {"path": str(contract_path)})

        if not getattr(args, "all_builds", False) and build_index >= len(builds):
            raise CLIError(
                1,
                "build_index_out_of_range",
                {
                    "index": build_index,
                    "count": len(builds),
                },
            )

        selected_builds = (
            list(enumerate(builds))
            if getattr(args, "all_builds", False)
            else [(build_index, builds[build_index])]
        )
        if getattr(args, "all_builds", False):
            results = _generate_all_builds(
                args,
                logger,
                contract,
                contract_path,
                selected_builds,
                logical_model=logical_model,
            )
            gate_failures = 0
            for output_dir, files, engine_name in results:
                if files:
                    _print_summary(output_dir, files, engine_name)
                    if _should_run_dbt_gate(args, engine_name):
                        if not _run_dbt_parse_gate(output_dir, logger):
                            gate_failures += 1
            if gate_failures:
                return 1
            return 0

        current_index, build = selected_builds[0]
        output_dir, files, engine_name = _generate_single_build(
            args,
            logger,
            contract,
            contract_path,
            build=build,
            build_index=current_index,
            logical_model=logical_model,
        )
        if not files:
            cprint("No files generated.")
            return 0
        _print_summary(output_dir, files, engine_name)
        if _should_run_dbt_gate(args, engine_name):
            if not _run_dbt_parse_gate(output_dir, logger):
                return 1
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

    * technique + model sidecar -> green (best case)
    * technique only -> yellow (the deterministic builder can emit a
      compile-safe skeleton, but it is not grounded on a logical model)
    * nothing to say → silent
    """
    labels = contract.get("labels") or {}
    metadata = contract.get("metadata") or {}
    technique = labels.get("dataModelingTechnique") or metadata.get("dataModelingTechnique")
    if not technique:
        return

    has_data_model = bool(
        metadata.get("user_data_model")
        or metadata.get("userDataModel")
        or labels.get("dataModel")
        or labels.get("modelSidecar")
    )

    display = _display_technique(technique)
    if has_data_model:
        cprint(
            f"[green]Generating {display} transformation artifacts grounded on your "
            f"supplied data model.[/green]"
        )
    else:
        cprint(
            f"[yellow]{display} transformation generation works best when a data model "
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


def _load_logical_model(args: Any, contract_path: Path, contract: Dict[str, Any]):
    model_path = _resolve_model_path(args, contract_path, contract)
    if not model_path:
        return None
    from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
    from fluid_build.forge_datamodel.logical_canonicalizer import canonicalize_logical_draft

    return canonicalize_logical_draft(
        LogicalDraft.model_validate_json(model_path.read_text(encoding="utf-8"))
    )


def _resolve_model_path(args: Any, contract_path: Path, contract: Dict[str, Any]) -> Optional[Path]:
    if getattr(args, "model", None):
        path = Path(args.model)
        if not path.exists():
            raise CLIError(1, "model_not_found", {"path": str(path)})
        return path
    labels = contract.get("labels") or {}
    label_candidate = labels.get("modelSidecar")
    if isinstance(label_candidate, str):
        candidate = contract_path.parent / label_candidate
        if candidate.exists():
            return candidate
    sibling = contract_path.with_name(f"{contract_path.name}.model.json")
    if sibling.exists():
        return sibling
    return None


def _generate_all_builds(
    args: Any,
    logger: logging.Logger,
    contract: Dict[str, Any],
    contract_path: Path,
    selected_builds: List[Tuple[int, Dict[str, Any]]],
    *,
    logical_model: Any = None,
) -> List[Tuple[Path, Dict[str, str], str]]:
    results: List[Tuple[Path, Dict[str, str], str]] = []
    max_workers = max(1, min(getattr(args, "concurrency", 4), len(selected_builds)))
    for wave in _topological_build_waves(selected_builds):
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _generate_single_build,
                    args,
                    logger,
                    contract,
                    contract_path,
                    build=build,
                    build_index=index,
                    logical_model=logical_model,
                    all_builds=True,
                ): (index, build)
                for index, build in wave
            }
            for future in as_completed(futures):
                results.append(future.result())
    return sorted(results, key=lambda item: str(item[0]))


def _topological_build_waves(
    selected_builds: List[Tuple[int, Dict[str, Any]]],
) -> List[List[Tuple[int, Dict[str, Any]]]]:
    remaining = {str(build.get("id") or index): (index, build) for index, build in selected_builds}
    dependency_map: Dict[str, set[str]] = {}
    for index, build in selected_builds:
        build_id = str(build.get("id") or index)
        raw_deps = build.get("dependsOn") or build.get("depends_on") or []
        dependency_map[build_id] = {str(dep) for dep in raw_deps if str(dep)}

    waves: List[List[Tuple[int, Dict[str, Any]]]] = []
    resolved: set[str] = set()
    while remaining:
        ready_ids = [
            build_id
            for build_id, deps in dependency_map.items()
            if build_id in remaining and deps.issubset(resolved)
        ]
        if not ready_ids:
            ready_ids = sorted(remaining.keys())
        wave = [remaining.pop(build_id) for build_id in ready_ids]
        waves.append(wave)
        resolved.update(ready_ids)
    return waves


def _generate_single_build(
    args: Any,
    logger: logging.Logger,
    contract: Dict[str, Any],
    contract_path: Path,
    *,
    build: Dict[str, Any],
    build_index: int,
    logical_model: Any = None,
    all_builds: bool = False,
) -> Tuple[Path, Dict[str, str], str]:
    from fluid_build.copilot.agents.base import StageSession
    from fluid_build.copilot.agents.builder_agent import BuilderAgent
    from fluid_build.copilot.store.factory import resolve_store
    from fluid_build.engines import get_engine, has_engine, list_engines
    from fluid_build.engines.base import TransformationIntent
    from fluid_build.util.contract import get_build_engine

    engine_name = get_build_engine(build) or "dbt"
    if not has_engine(engine_name):
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
        raise CLIError(1, "engine_not_found", {"engine": engine_name})

    if args.verbose:
        info(logger, f"Using engine: {engine_name} for build {build.get('id', build_index)}")

    issues = engine.validate(contract, build)
    errors = [issue for issue in issues if issue.severity.value == "error"]
    if errors:
        for issue in errors:
            error(logger, str(issue))
        raise CLIError(1, "engine_validation_failed", {"build_index": build_index})
    for issue in issues:
        if issue.severity.value == "warning":
            cprint(f"Warning: {issue}")

    # --mesh-hub is a dbt-only dbt-Mesh preservation knob. Warn-and-drop
    # for other engines rather than silently ignoring or hard-erroring.
    engine_kwargs: Dict[str, Any] = {}
    mesh_hub = getattr(args, "mesh_hub", None)
    if mesh_hub:
        if engine_name == "dbt":
            engine_kwargs["mesh_hub"] = mesh_hub
        else:
            cprint(
                f"[yellow]Warning: --mesh-hub is only meaningful for "
                f"engine=dbt; ignoring for engine={engine_name}.[/yellow]"
            )

    # --model-contracts mirrors the --mesh-hub plumbing: dbt-only,
    # warn-and-drop for other engines.
    if getattr(args, "model_contracts", False):
        if engine_name == "dbt":
            engine_kwargs["model_contracts"] = True
        else:
            cprint(
                f"[yellow]Warning: --model-contracts is only meaningful for "
                f"engine=dbt; ignoring for engine={engine_name}.[/yellow]"
            )

    transformation_intent = None
    if logical_model is not None:
        session = StageSession(
            store=resolve_store(workspace_root=contract_path.parent),
            workspace_root=contract_path.parent,
        )
        physical = BuilderAgent().build_physical(
            session,
            logical=logical_model,
            contract=contract,
            engine=engine_name,
        )
        transformation_intent = TransformationIntent(
            stages=[
                {
                    "name": spec.name,
                    "sql": spec.sql,
                    "layer": spec.layer,
                    "depends_on": spec.depends_on,
                    "outputs": spec.outputs,
                }
                for spec in physical.transform_plan.builds
            ],
            user_data_model=logical_model.model_dump(mode="json", by_alias=True),
            data_modeling_technique=logical_model.technique,
        )

    output_dir = _resolve_output_dir(args, build, engine_name)
    if all_builds:
        base_output = (
            Path(args.output)
            if getattr(args, "output", None)
            else (Path.cwd() / "speed_transformation_artifacts")
        )
        build_name = build.get("id") or f"build_{build_index}"
        output_dir = base_output / str(build_name)
    if engine_name == "dbt":
        engine_kwargs["output_dir"] = output_dir

    files = engine.generate(
        contract,
        build,
        build_index=build_index,
        transformation_intent=transformation_intent,
        workspace_root=contract_path.parent,
        **engine_kwargs,
    )
    if engine_name == "dbt" and not _dbt_sql_model_paths(files):
        raise CLIError(
            1,
            "dbt_models_empty",
            {
                "path": str(output_dir),
                "hint": (
                    "dbt generation produced no models/**/*.sql files. "
                    "Check labels.modelSidecar or pass --model with a valid forged sidecar."
                ),
            },
        )

    if output_dir.exists() and not getattr(args, "overwrite", False):
        if any(output_dir.iterdir()):
            raise CLIError(
                1,
                "output_not_empty",
                {
                    "path": str(output_dir),
                    "hint": "Use --overwrite to replace existing files.",
                },
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in sorted(files.items()):
        file_path = output_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    return output_dir, files, engine_name


def _dbt_sql_model_paths(files: Dict[str, str]) -> List[str]:
    """Return dbt SQL model paths, excluding project config and YAML files."""
    return [
        path
        for path in files
        if path.startswith("models/") and path.endswith(".sql") and "/." not in path
    ]


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

    cprint("\nUsage: fluid generate transformation (optional CONTRACT.fluid.yaml)")
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

    cprint("\nTip: To regenerate after editing the contract: fluid generate transformation")


def _should_run_dbt_gate(args: Any, engine_name: str) -> bool:
    """Gate predicate: true when the user opted in *and* engine is dbt.

    Any other engine is silently skipped — ``dbt parse`` is only
    meaningful for dbt projects. This keeps the flag generic enough to
    stay on in multi-build CI pipelines that mix dbt with SQL scripts.
    """
    if not getattr(args, "dbt_validate", False):
        return False
    return engine_name == "dbt"


def _run_dbt_parse_gate(output_dir: Path, logger: logging.Logger) -> bool:
    """Run ``dbt parse`` in ``output_dir`` and surface any failures.

    Returns True on success (parse clean) or when dbt isn't installed —
    we warn in the latter case rather than hard-failing so CI pipelines
    without dbt can opt in without breaking. Returns False only on an
    actual parse error so the caller can bump the exit code.

    Honours the project-local ``profiles.yml`` that the generator just
    emitted by passing ``--profiles-dir <output_dir>`` when that file
    exists. Without this, a fresh user with no ``~/.dbt/profiles.yml``
    would see the gate fail out of the box even though the project it
    just generated is self-contained.

    dbt discovery is delegated to the build runner's resolvers so the
    gate honours ``$DBT_EXECUTABLE`` (path, bare name, or multi-token
    wrapper like ``poetry run dbt``) plus the venv-sibling fallback —
    previously this was a hard ``shutil.which("dbt")``, inconsistent
    with how ``fluid apply`` resolves dbt.
    """
    import subprocess

    from fluid_build.build_runners.dbt.runner import (
        _configured_dbt_command_prefix,
        _resolve_dbt_executable,
    )

    command_prefix = _configured_dbt_command_prefix()
    if command_prefix is None:
        dbt_bin = _resolve_dbt_executable()
        if dbt_bin is None:
            cprint(
                "[yellow]--dbt-validate set but no usable dbt was found "
                "(checked $DBT_EXECUTABLE, PATH, and the active venv). "
                "Install dbt (e.g. `pip install dbt-core dbt-postgres`) "
                "or set DBT_EXECUTABLE to enable this gate. Skipping.[/yellow]"
            )
            return True
        command_prefix = [dbt_bin]

    command = [*command_prefix, "parse", "--project-dir", str(output_dir)]
    # Only inject --profiles-dir when the generator emitted a local
    # profiles.yml. If the project ships without one (e.g. user already
    # manages ~/.dbt/profiles.yml), let dbt use its default resolution.
    if (output_dir / "profiles.yml").exists():
        command.extend(["--profiles-dir", str(output_dir)])

    cprint(f"\n[cyan]Running `dbt parse` against {output_dir}[/cyan]")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        error(logger, f"dbt_parse_gate_failed_to_spawn: {exc}")
        return False

    if completed.returncode == 0:
        cprint("[green]✓ dbt parse succeeded.[/green]")
        return True

    cprint("[red]✗ dbt parse failed — surfacing output:[/red]")
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    if stderr:
        cprint(stderr)
    if stdout:
        cprint(stdout)
    return False
