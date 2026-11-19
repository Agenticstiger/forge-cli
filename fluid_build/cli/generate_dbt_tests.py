# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate dbt-tests`` — emit dbt schema.yml tests from contract quality block.

This is a thin CLI wrapper over :func:`fluid_build.exporters.dbt_tests.render_project_tests`;
when the contract declares invariants it also writes the ``fluid_rule`` test macro into the
dbt project's macro folder (:func:`_macro_path`).
Lives under ``fluid generate <subcommand>`` to match the existing layout
(``generate ci``, ``generate standard``, ``generate schedule``, ...).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ._common import CLIError, load_contract_with_overlay
from ._logging import info

SUBCOMMAND = "dbt-tests"


def register_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``generate dbt-tests`` subcommand."""
    p = subparsers.add_parser(
        SUBCOMMAND,
        help="Generate dbt schema.yml tests from the contract quality block",
        description=(
            "Reads exposes[].quality.tests[] and emits a dbt schema.yml document "
            "with per-column tests. Drop the output into your dbt project's "
            "models/<schema>/ directory and run `dbt test` to execute. "
            "Range/freshness tests reference the dbt_expectations / dbt_utils "
            "packages — a matching packages.yml is emitted alongside when needed. "
            "On a 0.7.7 contract, each declared invariant becomes a fluid_rule test, "
            "and the fluid_rule macro is written to the dbt project's macro folder "
            "(see --macros-dir); a rule no engine can run is reported and not tested."
        ),
        epilog=(
            "Examples:\n"
            "  fluid generate dbt-tests\n"
            "  fluid generate dbt-tests contract.fluid.yaml -o dbt/models/schema.yml\n"
            "  fluid generate dbt-tests --env prod"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "contract",
        nargs="?",
        default="contract.fluid.yaml",
        help="Contract path (default: contract.fluid.yaml in CWD)",
    )
    p.add_argument(
        "-o",
        "--out",
        default="schema.yml",
        help="Output path for the dbt schema.yml (default: ./schema.yml)",
    )
    p.add_argument("--env", help="Environment overlay (dev/test/prod)")
    # Same flag, same resolution as `fluid generate transformation` — the two
    # dbt-emitting commands must agree on the dialect, or a Fusion user who
    # generated a data_tests: project gets a tests: schema.yml from here that
    # Fusion's strict parser rejects.
    p.add_argument(
        "--dbt-tests-key",
        choices=["auto", "tests", "data_tests"],
        default=None,
        help=(
            "Which YAML key generated dbt data tests attach under. "
            "dbt-core 1.8 renamed ``tests:`` to ``data_tests:`` (both work "
            "on core 1.8+); the Fusion engine strict-parses and requires "
            "``data_tests:``, while dbt-core <1.8 only understands the "
            "legacy ``tests:``. Default ('auto', also via "
            "$FLUID_DBT_TESTS_KEY) detects the dbt binary you would "
            "actually run: Fusion or core>=1.8 emit data_tests:, core<1.8 "
            "or no dbt found emit the legacy tests:. Pass an explicit "
            "value in CI generators that have no local dbt binary."
        ),
    )
    p.add_argument(
        "--macros-dir",
        default=None,
        help=(
            "Macro folder the fluid_rule test macro is written to (as fluid/fluid_rule.sql), "
            "when the contract declares invariants. Default: the first macro-paths entry of "
            "the nearest dbt_project.yml above --out (macros/ when it names none)."
        ),
    )
    _add_coverage_arguments(p)
    p.set_defaults(generate_sub=SUBCOMMAND, func=run)


def _add_coverage_arguments(p: argparse.ArgumentParser) -> None:
    """The coverage report and required-rule flags."""
    p.add_argument(
        "--coverage",
        action="store_true",
        help=(
            "Print which declared rules each engine can run (duckdb, snowflake, bigquery, "
            "databricks, postgres, athena), and why not where it cannot."
        ),
    )
    p.add_argument(
        "--coverage-out",
        default=None,
        metavar="PATH",
        help="Write the same coverage report as JSON to PATH.",
    )
    p.add_argument(
        "--require-rule",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "A declared rule that must run (repeatable): generation fails, writing no tests, "
            "when a selected adapter cannot run it, or when nothing tests it (a placeholder "
            "declaration with no bound)."
        ),
    )
    p.add_argument(
        "--require-all-rules",
        action="store_true",
        help="Every declared rule must run on the selected adapters (as --require-rule for each).",
    )
    p.add_argument(
        "--adapter",
        action="append",
        default=[],
        metavar="TYPE",
        help=(
            "A dbt adapter type the required rules must run on (repeatable): duckdb, "
            "snowflake, bigquery, databricks, spark, postgres, athena or trino. Default: the "
            "adapters of the platforms the contract's exposes bind to, or every adapter when "
            "none of them maps to one."
        ),
    )


def run(args, logger: logging.Logger) -> int:
    """Render the contract quality block as a dbt schema.yml."""
    from ..exporters.dbt_tests import MANAGED_BY_SENTINEL, render_project_tests

    # Reuse `generate transformation`'s resolver rather than re-implementing
    # detection: both commands emit dbt YAML for the same project, so a
    # second implementation would be a second dialect to keep in sync.
    from .generate_speed_transformation import _resolve_dbt_tests_key

    contract_path = Path(args.contract)
    if not contract_path.exists():
        raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

    contract = load_contract_with_overlay(str(contract_path), getattr(args, "env", None), logger)
    _check_coverage(contract, args, logger)
    rendered = render_project_tests(contract, tests_key=_resolve_dbt_tests_key(args, logger))
    yaml_text = rendered.yaml

    out_path = Path(args.out)
    macro_path = (
        _macro_path(out_path, getattr(args, "macros_dir", None))
        if rendered.uses_fluid_rule
        else None
    )
    if macro_path is not None:
        _check_macro_target(macro_path)
    # Refuse to clobber a non-managed file — prevents accidental overwrite
    # of a hand-curated schema.yml. The user can explicitly delete the
    # file (or pass a different --out) to re-emit.
    if out_path.exists():
        existing = out_path.read_text(encoding="utf-8")
        if MANAGED_BY_SENTINEL not in existing:
            raise CLIError(
                1,
                "dbt_tests_refusing_overwrite",
                {
                    "path": str(out_path),
                    "detail": (
                        f"refusing to overwrite {out_path}: file is not "
                        f"managed by fluid (missing '{MANAGED_BY_SENTINEL}' "
                        f"header). Delete it first or pass a different --out."
                    ),
                },
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text, encoding="utf-8")
    info(logger, "dbt_tests_written", out=str(out_path), bytes=len(yaml_text))
    if macro_path is not None:
        from ..data_rules._dbt import MACRO

        macro_path.parent.mkdir(parents=True, exist_ok=True)
        macro_path.write_text(MACRO, encoding="utf-8")
        info(logger, "dbt_tests_macro_written", out=str(macro_path))
    for skipped in rendered.skipped:
        if skipped.adapters:
            logger.warning(
                "declared rule %r on expose %r cannot run on the %s adapter(s): %s. Its "
                "fluid_rule test fails there, naming the rule; it runs on the others.",
                skipped.id,
                skipped.expose,
                ", ".join(skipped.adapters),
                skipped.reason,
            )
            continue
        logger.warning(
            "declared rule %r on expose %r has no dbt test: %s. It is not tested, and nothing "
            "approximates it; run fluid validate on the contract to see what to change.",
            skipped.id,
            skipped.expose,
            skipped.reason,
        )
    _emit_packages_yml(out_path, yaml_text, logger)
    return 0


def _check_coverage(contract: Any, args: Any, logger: logging.Logger) -> None:
    """Print or write the coverage report when asked, and refuse, before anything is
    written, when a required rule cannot run on a selected adapter."""
    show = bool(getattr(args, "coverage", False))
    out = getattr(args, "coverage_out", None)
    required = list(getattr(args, "require_rule", None) or [])
    if getattr(args, "require_all_rules", False):
        required.append("*")
    if not (show or out or required):
        return
    from ..data_rules._coverage import as_json, coverage, render_table, unmet

    rows = coverage(contract)
    if show:
        sys.stdout.write(render_table(rows) + "\n")
    if out:
        import json

        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(as_json(rows), indent=2) + "\n", encoding="utf-8")
        info(logger, "dbt_tests_coverage_written", out=str(out))
    if not required:
        return
    _refuse_inert_rules(contract)
    known = {row.id for row in rows}
    unknown = sorted(r for r in required if r != "*" and r not in known)
    if unknown:
        raise CLIError(
            1,
            "dbt_tests_unknown_required_rule",
            {
                "rules": unknown,
                "detail": (
                    f"--require-rule names {', '.join(unknown)}, which the contract does not "
                    f"declare. Declared: {', '.join(sorted(known)) or 'none'}."
                ),
            },
        )
    adapters = _selected_adapters(contract, getattr(args, "adapter", None) or [])
    missed = unmet(rows, required, adapters)
    if missed:
        lines = [f"{row.id} on {adapter}: {verdict.reason}" for row, adapter, verdict in missed]
        raise CLIError(
            1,
            "dbt_tests_required_rule_unsupported",
            {
                "rules": sorted({row.id for row, _, _ in missed}),
                "detail": (
                    "required rules cannot run on the selected adapter(s): "
                    + "; ".join(lines)
                    + ". Restate them in FLUID SQL/1 the adapter can run, select another "
                    "--adapter, or drop them from --require-rule."
                ),
            },
        )


def _refuse_inert_rules(contract: Any) -> None:
    """A requirement on a contract that declares rules below 0.7.7, where they do not exist
    (and run nowhere), fails rather than pass with nothing tested."""
    from ..data_rules import FIRST_VERSION, _version

    version = _version(contract) if isinstance(contract, dict) else ()
    if version and version >= FIRST_VERSION:
        return
    keys = ("identifierFormat", "placeholders", "sharedAttribute")
    declared = False
    for expose in contract.get("exposes") or [] if isinstance(contract, dict) else []:
        if not isinstance(expose, dict):
            continue
        spec = expose.get("contract") if isinstance(expose.get("contract"), dict) else {}
        columns = [c for c in spec.get("schema") or [] if isinstance(c, dict)]
        semantics = expose.get("semantics") if isinstance(expose.get("semantics"), dict) else {}
        links = [r for r in semantics.get("relationships") or [] if isinstance(r, dict)]
        declared = declared or bool(spec.get("invariants"))
        declared = declared or any(k in c for c in columns for k in keys)
        declared = declared or any("sharedColumns" in r for r in links)
    if declared:
        shown = contract.get("fluidVersion") or "none"
        raise CLIError(
            1,
            "dbt_tests_rules_need_preview",
            {
                "detail": (
                    f"the contract declares rules about valid data, which exist from "
                    f"fluidVersion 0.7.7, and it declares {shown}: none of them runs, so no "
                    "requirement can be met. Declare fluidVersion: 0.7.7."
                )
            },
        )


def _selected_adapters(contract: Any, named: list) -> dict:
    """adapter type -> FLUID SQL/1 dialect: those named, else the adapters of the platforms
    the contract's exposes bind to, else every adapter forge writes SQL for."""
    from ..data_rules._compile import platform_dialect
    from ..data_rules._dbt import ADAPTERS

    if named:
        bad = [a for a in named if a not in ADAPTERS]
        if bad:
            raise CLIError(
                1,
                "dbt_tests_unknown_adapter",
                {
                    "adapters": bad,
                    "detail": (
                        f"--adapter {', '.join(bad)}: forge writes fluid_rule SQL for "
                        f"{', '.join(ADAPTERS)}."
                    ),
                },
            )
        return {a: ADAPTERS[a] for a in named}
    dialects = {
        platform_dialect(e)[0] for e in (contract.get("exposes") or []) if isinstance(e, dict)
    } - {None}
    chosen = {a: d for a, d in ADAPTERS.items() if d in dialects and a not in ("spark", "trino")}
    return chosen or dict(ADAPTERS)


def _macro_path(out_path: Path, macros_dir: str | None) -> Path:
    """Where the ``fluid_rule`` macro goes: ``<macros dir>/fluid/fluid_rule.sql``.

    dbt loads a generic test only from the project's macro paths (a ``.sql`` file under
    ``models/`` would be a model), so without ``--macros-dir`` the macro goes to the first
    ``macro-paths`` entry of the nearest ``dbt_project.yml`` above ``--out``.
    """
    from ..data_rules._dbt import MACRO_FILE

    if macros_dir:
        return Path(macros_dir) / MACRO_FILE
    # absolute(), not resolve(): --out inside the project through a symlinked folder still
    # belongs to the project the path names.
    out_abs = Path(os.path.abspath(out_path))
    for folder in [out_abs.parent, *out_abs.parents]:
        project = folder / "dbt_project.yml"
        if project.is_file():
            return folder / _first_macro_path(project) / MACRO_FILE
    raise CLIError(
        1,
        "dbt_tests_no_macro_dir",
        {
            "path": str(out_path),
            "detail": (
                "the contract declares invariants, which run as the fluid_rule dbt test, and "
                f"no dbt_project.yml was found above {out_path} to put its macro in. Pass "
                "--macros-dir <your project's macro folder>, or write --out inside the project."
            ),
        },
    )


def _first_macro_path(project: Path) -> str:
    """The project's first ``macro-paths`` entry (dbt's default: ``macros``), kept inside
    the project folder."""
    # The one reader of dbt_project.yml (build_runners/dbt/project_files.py), as a bundle uses.
    from ..build_runners.dbt.project_files import DbtProjectFilesError, load_project_config

    try:
        doc = load_project_config(project.parent)
    except DbtProjectFilesError as unreadable:
        raise CLIError(
            1, "dbt_tests_no_macro_dir", {"path": str(project), "detail": str(unreadable)}
        ) from unreadable
    paths = doc.get("macro-paths")
    first = paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], str) else None
    if not first:
        return "macros"
    if Path(first).is_absolute() or ".." in Path(first).parts:
        raise CLIError(
            1,
            "dbt_tests_no_macro_dir",
            {
                "path": str(project),
                "detail": (
                    f"{project}'s first macro-paths entry {first!r} is outside the project, "
                    "so fluid does not write the fluid_rule macro there. Pass --macros-dir "
                    "<a folder dbt loads macros from>."
                ),
            },
        )
    return first


def _check_macro_target(macro_path: Path) -> None:
    """Refuse to overwrite a macro file fluid did not write."""
    from ..data_rules._dbt import MACRO_SENTINEL

    if macro_path.exists():
        existing = macro_path.read_text(encoding="utf-8", errors="replace")
        if not existing.startswith(MACRO_SENTINEL):
            raise CLIError(
                1,
                "dbt_tests_refusing_overwrite",
                {
                    "path": str(macro_path),
                    "detail": (
                        f"refusing to overwrite {macro_path}: it is not managed by fluid "
                        f"(missing the '{MACRO_SENTINEL}' first line). Delete it or pass "
                        "another --macros-dir."
                    ),
                },
            )


def _emit_packages_yml(out_path: Path, yaml_text: str, logger: logging.Logger) -> None:
    """Mirror the engine's packages.yml emission next to the schema.yml.

    The rendered schema.yml may reference package-namespaced tests
    (``dbt_expectations.expect_column_values_to_be_between``,
    ``dbt_utils.recency`` / ``expression_is_true``); without their pins the
    receiving dbt project fails ``dbt parse``. Semantics match the engine
    path (``engines/dbt/packages_yml.py``): emit only when needed, regenerate
    a fluid-managed file, and never touch a user-managed one — just tell the
    user which pins it must carry.
    """
    from ..engines.dbt.packages_yml import (
        MANAGED_BY_SENTINEL as PACKAGES_SENTINEL,
    )
    from ..engines.dbt.packages_yml import (
        PACKAGE_PINS,
        render_packages_yml,
        required_packages,
    )

    needed = required_packages({str(out_path): yaml_text})
    if not needed:
        return

    pkg_path = out_path.parent / "packages.yml"
    if pkg_path.exists() and PACKAGES_SENTINEL not in pkg_path.read_text(encoding="utf-8"):
        info(
            logger,
            "dbt_tests_packages_yml_left_untouched",
            path=str(pkg_path),
            required=[
                {"package": PACKAGE_PINS[p]["package"], "version": PACKAGE_PINS[p]["version"]}
                for p in needed
            ],
            detail=(
                f"{pkg_path} is user-managed (missing '{PACKAGES_SENTINEL}'); "
                "ensure it carries the listed packages, then run `dbt deps`."
            ),
        )
        return

    content = render_packages_yml(needed)
    pkg_path.write_text(content, encoding="utf-8")
    info(logger, "dbt_tests_packages_written", out=str(pkg_path), packages=needed)
