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

from __future__ import annotations

import argparse
import logging
import os

from fluid_build.cli.console import error as console_error

from ._common import CLIError, load_contract_with_overlay, write_json
from ._logging import info

COMMAND = "export-opds"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help=(
            "[deprecated alias] Export FLUID → LF/ODPI ODPS v4.1 JSON. "
            "Prefer `fluid generate standard --format odps-v4.1`."
        ),
    )
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    # The command name is the deprecated letter-swap; the artifact it leaves on
    # disk must not be. Default to the canonical filename `fluid generate
    # standard --format odps-v4.1` writes, so a repo does not end up with both
    # product.odps-v4.1.json and a byte-identical product.opds.json.
    p.add_argument("--out", default="runtime/exports/product.odps-v4.1.json", help="Output path")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Emit a real LF/ODPI ODPS v4.1 JSON via :class:`OdpsProvider`.

    Bitol ODPS is the center-stage default elsewhere in the CLI; this
    command is the back-compat entry point for the LF/ODPI Open Data Product
    Specification v4.1. It logs a DEPRECATION warning pointing at the
    canonical ``fluid generate standard --format odps-v4.1`` path so audit
    aggregators surface usage during the rename window.
    """
    try:
        logger.warning(
            "export_opds_deprecated",
            extra={"canonical": "fluid generate standard --format odps-v4.1"},
        )
        # Banner to STDERR so users piping the exporter's stdout get clean JSON.
        console_error(
            "WARNING: `fluid export-opds` is the historical letter-swap name for "
            "the LF/ODPI ODPS v4.1 export. Prefer `fluid generate standard "
            "--format odps-v4.1` for new scripts. (The center-stage `--format "
            "odps` emits Bitol ODPS v1.0.0.)"
        )

        from fluid_build.cli._export_env import resolve_for_export
        from fluid_build.providers.opds.opds import OdpsProvider

        c = resolve_for_export(
            load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        )
        rendered = OdpsProvider().render(c)
        # Unwrap the provider's ``{..., artifacts: {...}}`` envelope so the
        # on-disk file is the canonical bare ODPS v4.1 doc.
        payload = rendered.get("artifacts", rendered) if isinstance(rendered, dict) else rendered

        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        write_json(args.out, payload)
        info(logger, "export_opds_ok", out=args.out)
        return 0
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "export_opds_failed", {"error": str(e)})
