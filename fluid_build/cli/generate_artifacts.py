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

"""``fluid generate artifacts`` — pipeline stage 3.

Fanout wrapper that takes a stage-1 bundle (.tgz) and emits catalog-ready
artifacts (ODCS, ODPS-Bitol, ODPS v4.1 LF/ODPI, schedule DAGs, policy
bindings) into a single directory with a unified MANIFEST.json. The legacy
``opds`` emit key is a deprecated letter-swap alias of ``odps`` (same
target spec). Delegates all emission to existing per-format commands;
orchestration lives in ``fluid_build.forge.core.artifact_fanout``.

Registered as a subcommand of ``fluid generate``:

    fluid generate artifacts <bundle.tgz> [--out dir] [--emit csv] [--manifest path]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from fluid_build.cli._common import CLIError
from fluid_build.cli.console import cprint
from fluid_build.observability.tracing import traced_stage as _traced_stage


def register_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "artifacts",
        help="Fanout bundle → catalog artifacts (ODCS, ODPS LF/ODPI, ODPS-Bitol, schedule, policies)",
        description=(
            "Stage-3 of the 11-stage pipeline. Reads a Phase-2 bundle and emits "
            "ODCS per-port, ODPS-Bitol, ODPS v4.1 (LF/ODPI), schedule DAGs, and "
            "compiled policy bindings into <out>/, with a unified MANIFEST.json "
            "hashed over every emitted file. The legacy ``opds`` emit key is a "
            "deprecated letter-swap alias of ``odps``."
        ),
        epilog=(
            "Examples:\n"
            "  fluid generate artifacts dist/product.fluid.bundle.tgz \\\n"
            "      --out dist/artifacts/\n"
            "  fluid generate artifacts bundle.tgz --emit odps-bitol,odcs\n"
            "  fluid generate artifacts contract.fluid.yaml --out /tmp/art  # dev shortcut\n\n"
            "Note: --emit dbt is NOT supported. dbt projects are execution artifacts;\n"
            "use `fluid generate speed-transformation` instead.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "bundle",
        help=(
            "Path to a Phase-2 bundle (.tgz) OR a raw resolved contract "
            "(.yaml/.yml). Bundle input is the CI path (MANIFEST re-verified); "
            "contract input is a local-dev shortcut."
        ),
    )
    p.add_argument(
        "--out",
        default="dist/artifacts",
        help="Output directory for emitted artifacts. Default: dist/artifacts",
    )
    p.add_argument(
        "--emit",
        default=None,
        help=(
            "Comma-separated emit selector. Valid: odps, odps-bitol, odcs, schedule, "
            "policies. ``opds`` is also accepted as a deprecated letter-swap alias "
            "of ``odps`` (same LF/ODPI ODPS v4.1 target). Default: all six. ``dbt`` "
            "is NOT a valid emit key — dbt projects are execution artifacts (see "
            "`fluid generate speed-transformation`)."
        ),
    )
    p.add_argument(
        "--manifest",
        default=None,
        help=(
            "Path to write MANIFEST.json. Default: <out>/MANIFEST.json. "
            "The manifest carries SHA-256 per emitted file plus a merkle root "
            "that stage-4 ``fluid validate artifacts`` re-verifies."
        ),
    )
    p.set_defaults(generate_sub="artifacts", func=_run_from_generate)


def _run_from_generate(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate artifacts``."""
    return run(args, logger)


@_traced_stage("generate_artifacts")
def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.forge.core.artifact_fanout import FanoutError, run_fanout

    bundle_path = Path(args.bundle)
    out_dir = Path(args.out)
    manifest_path = Path(args.manifest) if args.manifest else None

    if not bundle_path.exists():
        raise CLIError(2, "generate_artifacts_input_missing", {"path": str(bundle_path)})

    try:
        manifest = run_fanout(
            bundle_path,
            out_dir,
            emit_raw=args.emit,
            manifest_path=manifest_path,
            logger=logger,
        )
    except FanoutError as exc:
        # Surface emit-key context so the operator knows which generator failed.
        meta = {"error": str(exc)}
        if exc.key:
            meta["emit_key"] = exc.key
        raise CLIError(1, "generate_artifacts_failed", meta)

    cprint(f"✅ Artifacts written to {out_dir}")
    cprint(f"   MANIFEST digest: {manifest['digest']}")
    cprint(f"   files: {len(manifest.get('files', {}))}")
    return 0
