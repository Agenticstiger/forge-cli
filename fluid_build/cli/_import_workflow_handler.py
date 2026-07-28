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

"""Handler for ``fluid import {meltano,airbyte,dlt,singer,dbt} <source>``.

Dispatches to the matching importer in
:mod:`fluid_build.cli.import_workflow`, writes the resulting contract YAML
to disk, and prints the translation report so the user knows exactly what
mapped 1:1, what got defaulted, and what's unsupported.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

from fluid_build.cli._errors import SchemaValidationError
from fluid_build.cli.console import cprint
from fluid_build.cli.import_workflow import get_importer


def run_import_from_tool(args, logger: logging.Logger, *, tool: str, source: Optional[str]) -> int:
    if not source:
        raise SchemaValidationError(
            what=f"`fluid import {tool}` requires a positional source argument",
            why=(
                f"The {tool} importer needs to know what to convert "
                f"(project dir / workspace id / pipeline name / tap config / "
                f"dbt manifest)."
            ),
            fix=(
                f"`fluid import {tool} <project-dir | workspace-id | "
                f"pipeline-name | tap-config.json | manifest.json>`"
            ),
            doc="https://forge.fluid.dev/ref/import",
            extras={"tool": tool},
        )

    importer = get_importer(tool)
    if importer is None:
        raise SchemaValidationError(
            what=f"unknown importer tool: {tool}",
            why=f"`{tool}` is not in the registered importer set.",
            fix="Use one of: meltano | airbyte | dlt | singer | dbt.",
            doc="https://forge.fluid.dev/ref/import",
            extras={"tool": tool, "supported": ["meltano", "airbyte", "dlt", "singer", "dbt"]},
        )

    options = {"split_by": getattr(args, "split_by", None) or "project"}

    cprint(f"📥 Importing {tool} configuration from {source}…")
    try:
        # Split-capable importers (dbt) expose a plural API; the rest keep the
        # single-contract Protocol shape.
        if hasattr(importer, "import_to_contracts"):
            contracts, report = importer.import_to_contracts(source, options=options)
        else:
            contract, report = importer.import_to_contract(source, options=options)
            contracts = [contract] if contract else []
    except Exception as exc:  # noqa: BLE001
        raise SchemaValidationError(
            what=f"{tool} import failed for {source}",
            why=str(exc),
            fix=(
                "Check the source path/identifier and that the foreign tool's "
                "config is well-formed."
            ),
            doc=f"https://forge.fluid.dev/ref/import#{tool}",
            extras={"tool": tool, "source": source},
        ) from exc

    out_arg = getattr(args, "out_path", None)
    written: list[Path] = []
    if len(contracts) == 1:
        out_path = Path(out_arg or _default_out_path(contracts[0]))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml.safe_dump(contracts[0], sort_keys=False), encoding="utf-8")
        written.append(out_path)
    else:
        # Multi-contract (split) import: --out names the output DIRECTORY.
        out_dir = Path(out_arg) if out_arg else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        for contract in contracts:
            out_path = out_dir / _default_out_path(contract)
            out_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
            written.append(out_path)

    for path in written:
        cprint(f"✓ Wrote {path}")
    _print_report(report)
    if written:
        cprint(
            "\nNext: `fluid validate {0}` and review before applying.".format(
                written[0] if len(written) == 1 else "<each written contract>"
            )
        )
    return 0


def _default_out_path(contract: dict) -> str:
    cid = (contract.get("id") or "imported").replace("/", "_")
    return f"contract.{cid}.fluid.yaml"


def _print_report(report) -> None:
    if report.mapped_one_to_one:
        cprint(f"\n  Mapped 1:1 ({len(report.mapped_one_to_one)}):")
        for x in report.mapped_one_to_one:
            cprint(f"    • {x}")
    if report.required_defaults:
        cprint(f"\n  Used defaults ({len(report.required_defaults)}):")
        for x in report.required_defaults:
            cprint(f"    • {x}")
    if report.unsupported:
        cprint(f"\n  Unsupported (must be re-authored, {len(report.unsupported)}):")
        for x in report.unsupported:
            cprint(f"    ⚠ {x}")
    if report.notes:
        cprint("\n  Notes:")
        for x in report.notes:
            cprint(f"    – {x}")
