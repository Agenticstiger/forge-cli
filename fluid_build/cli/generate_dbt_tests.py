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

"""``fluid generate dbt-tests`` — emit dbt schema.yml tests from contract quality block.

This is a thin CLI wrapper over :func:`fluid_build.exporters.dbt_tests.render_dbt_tests`.
Lives under ``fluid generate <subcommand>`` to match the existing layout
(``generate ci``, ``generate standard``, ``generate schedule``, ...).
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

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
            "packages — a matching packages.yml is emitted alongside when needed."
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
    p.set_defaults(generate_sub=SUBCOMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Render the contract quality block as a dbt schema.yml."""
    from ..exporters.dbt_tests import MANAGED_BY_SENTINEL, render_dbt_tests

    # Reuse `generate transformation`'s resolver rather than re-implementing
    # detection: both commands emit dbt YAML for the same project, so a
    # second implementation would be a second dialect to keep in sync.
    from .generate_speed_transformation import _resolve_dbt_tests_key

    contract_path = Path(args.contract)
    if not contract_path.exists():
        raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

    contract = load_contract_with_overlay(str(contract_path), getattr(args, "env", None), logger)
    yaml_text = render_dbt_tests(contract, tests_key=_resolve_dbt_tests_key(args, logger))

    out_path = Path(args.out)
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
    _emit_packages_yml(out_path, yaml_text, logger)
    return 0


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
