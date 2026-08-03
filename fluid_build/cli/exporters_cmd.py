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

"""``fluid exporters`` — list the spec exporters (contract → standard).

The discoverable home for the spec EXPORTERS (odps / odcs / odps-bitol). These
serialize a FLUID contract to a data-product / data-contract standard; they are
NOT cloud providers (deliberately absent from ``fluid providers`` /
``--provider``). Use them via ``fluid generate standard --format <x>`` or the
dedicated ``fluid odps`` / ``fluid odcs`` commands.
"""

from __future__ import annotations

import argparse
import json
import logging

from fluid_build.cli.console import cprint

COMMAND = "exporters"


def register(subparsers: argparse._SubParsersAction):
    """Register the ``exporters`` command."""
    p = subparsers.add_parser(
        COMMAND,
        help="List spec exporters (contract → ODPS / ODCS standards)",
    )
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Render the registered spec exporters."""
    from fluid_build.providers._exporters import list_exporters

    exporters = list_exporters()

    if getattr(args, "json", False):
        cprint(
            json.dumps(
                [
                    {
                        "name": e.name,
                        "spec": e.spec,
                        "url": e.url,
                        "formats": list(e.formats),
                        "deprecatedFormats": list(e.deprecated_formats),
                    }
                    for e in exporters
                ],
                indent=2,
            )
        )
        return 0

    cprint("📤 FLUID spec exporters (contract → standard):\n")
    for e in exporters:
        cprint(f"  {e.name:<12} {e.spec}")
        if e.url:
            cprint(f"  {'':<12} {e.url}")
        # Only canonical spellings are advertised as invocations; deprecated
        # aliases are named as such on their own line so this listing and
        # `fluid generate standard --list` cannot disagree about which acronym
        # is current.
        if e.formats:
            cprint(f"  {'':<12} fluid generate standard --format {' | '.join(e.formats)}")
        if e.deprecated_formats:
            cprint(
                f"  {'':<12} deprecated alias(es): "
                f"{', '.join(e.deprecated_formats)} — prefer --format {e.formats[0]}"
            )
        cprint("")
    cprint(
        "Exporters serialize a contract to a SPEC — they are not cloud providers "
        "(see `fluid providers` for deployment targets)."
    )
    return 0
