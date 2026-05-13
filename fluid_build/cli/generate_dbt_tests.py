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
            "Range tests require the dbt-utils package."
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
    p.set_defaults(generate_sub=SUBCOMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Render the contract quality block as a dbt schema.yml."""
    from ..exporters.dbt_tests import MANAGED_BY_SENTINEL, render_dbt_tests

    contract_path = Path(args.contract)
    if not contract_path.exists():
        raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

    contract = load_contract_with_overlay(str(contract_path), getattr(args, "env", None), logger)
    yaml_text = render_dbt_tests(contract)

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
    return 0
