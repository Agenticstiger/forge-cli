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

"""``fluid generate vector`` subcommand.

Compiles a FLUID contract into a pgvector RAG target — the embeddings-table
+ ANN-index DDL for the product's ``ai-embeddable`` columns, plus a RAG
provenance manifest. The artefacts are emitted for review; this command
never connects to a database (apply the ``embeddings.sql`` yourself, or feed
``vector_manifest.json`` to a retrieval pipeline).

Mirrors ``fluid generate iac`` (contract-in / artefact-out, no side effects).
"""

from __future__ import annotations

import argparse
import json
import logging
import os

from fluid_build.cli.console import cprint

from ._common import CLIError, load_contract_with_overlay, resolve_env_templates_in_contract
from ._logging import info


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "vector",
        help="Compile a contract to a pgvector embeddings target (DDL + RAG manifest)",
        description=(
            "Compile a FLUID contract into a pgvector RAG target.\n\n"
            "Emits `embeddings.sql` (CREATE EXTENSION vector + the embeddings\n"
            "table + ANN index for the ai-embeddable columns) and\n"
            "`vector_manifest.json` (RAG provenance) for review. It consumes the\n"
            "`ai-embeddable` column labels the ai_ready agent stamps."
        ),
        epilog="""Examples:
  fluid generate vector contract.fluid.yaml
  fluid generate vector contract.fluid.yaml --out runtime/vector
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", help="contract.fluid.yaml")
    p.add_argument("--out", "-o", default="runtime/vector", help="Output directory")
    p.add_argument("--env", help="Environment overlay")
    p.set_defaults(generate_sub="vector", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate vector``."""
    return run(args, logger)


def run(args, logger: logging.Logger) -> int:
    contract_path = getattr(args, "contract", None)
    if not contract_path:
        cprint("Error: contract path is required.")
        return 1

    # Heavy import deferred so `fluid --help` / parser build stays cold-path clean.
    from fluid_build.output_ports.vector import compile_vector_port, validate_vector_binding

    try:
        contract = load_contract_with_overlay(contract_path, getattr(args, "env", None), logger)
        contract = resolve_env_templates_in_contract(contract)

        errors, warnings = validate_vector_binding(contract)
        for msg in warnings:
            cprint(f"  warning: {msg}")
        if errors:
            for msg in errors:
                cprint(f"  error: {msg}")
            raise CLIError(1, "generate_vector_invalid_binding", {"errors": errors})

        artifacts = compile_vector_port(contract)
        target_count = len(artifacts.targets)
        if target_count == 0:
            cprint(
                "\nWarning: no pgvector expose found in the contract "
                "(set binding.platform: pgvector) — nothing to emit."
            )
            return 0

        out_dir = getattr(args, "out", None) or "runtime/vector"
        os.makedirs(out_dir, exist_ok=True)
        sql_path = os.path.join(out_dir, "embeddings.sql")
        manifest_path = os.path.join(out_dir, "vector_manifest.json")
        with open(sql_path, "w", encoding="utf-8") as f:
            f.write(artifacts.ddl)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(artifacts.manifest, f, indent=2, sort_keys=True)
            f.write("\n")
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "generate_vector_failed", {"error": str(e)})

    embeddable = sum(len(t.embeddable_columns) for t in artifacts.targets)
    info(
        logger,
        "generate_vector_ok",
        targets=target_count,
        embeddable_columns=embeddable,
        sql=sql_path,
        manifest=manifest_path,
    )
    cprint(
        f"\nWrote pgvector target: {sql_path} + {manifest_path}  "
        f"({target_count} expose(s), {embeddable} embeddable column(s))"
    )
    cprint("\nReview and apply the embeddings table with psql:")
    cprint(f"  psql <dsn> -f {sql_path}")
    return 0
