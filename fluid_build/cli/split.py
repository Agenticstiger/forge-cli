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

"""fluid split — split a flat contract into composable fragments.

This is the inverse of ``fluid bundle``.  Major sections (sovereignty,
accessPolicy, builds, exposes) become separate YAML files under
``fragments/``, referenced via ``$ref`` pointers from the root contract.

Usage:
    fluid split contract.fluid.yaml
    fluid split contract.fluid.yaml --dry-run
    fluid split contract.fluid.yaml --out ./output
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

COMMAND = "split"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Split a flat contract into composable fragments with $ref pointers",
        description=(
            "Split a monolithic FLUID contract into a fragment-first layout.\n"
            "Major sections (sovereignty, accessPolicy, builds, exposes) become\n"
            "separate YAML files under fragments/, referenced via $ref.\n\n"
            "This is the inverse of 'fluid bundle'."
        ),
        epilog=(
            "Examples:\n"
            "  fluid split contract.fluid.yaml              # split in place\n"
            "  fluid split contract.fluid.yaml --dry-run    # preview changes\n"
            "  fluid split contract.fluid.yaml --out ./out  # different target\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", help="Path to the flat FLUID contract file")
    p.add_argument(
        "--out",
        "-o",
        default=None,
        help="Output directory (default: same directory as the contract)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing any files",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def _load_contract(path: str) -> Dict[str, Any]:
    """Load a YAML or JSON contract file."""
    import json as _json

    contract_path = Path(path)
    if not contract_path.exists():
        raise FileNotFoundError(f"Contract file not found: {path}")

    text = contract_path.read_text(encoding="utf-8")
    if contract_path.suffix.lower() == ".json":
        return _json.loads(text)  # type: ignore[no-any-return]
    if yaml is None:
        raise RuntimeError("YAML support requires PyYAML. Install with: pip install pyyaml")
    return yaml.safe_load(text) or {}  # type: ignore[no-any-return]


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.cli.forge_contract_fragments import (
        describe_fragment_layout,
        split_contract_to_fragments,
    )

    contract_path = Path(args.contract)
    dry_run = args.dry_run
    out_dir = Path(args.out) if args.out else contract_path.parent

    try:
        contract = _load_contract(str(contract_path))
    except FileNotFoundError as e:
        sys.stderr.write(f"\u274c {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"\u274c Failed to load contract: {e}\n")
        return 1

    # Check if there's anything to split.
    preview = describe_fragment_layout(contract)
    if not preview:
        sys.stderr.write(
            "\u2139 Nothing to split \u2014 contract has no sections that benefit from fragments.\n"
        )
        return 0

    if dry_run:
        sys.stderr.write(f"Would create {len(preview)} fragments:\n")
        for p in preview:
            sys.stderr.write(f"   {p}\n")
        return 0

    # Perform the split.
    root_contract, fragment_files = split_contract_to_fragments(contract)

    # Write fragment files.
    for rel_path, content in fragment_files.items():
        fpath = out_dir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    # Write the updated root contract (with $ref pointers).
    root_out = out_dir / contract_path.name
    if yaml is None:
        raise RuntimeError("YAML support requires PyYAML. Install with: pip install pyyaml")
    root_out.write_text(
        yaml.dump(root_contract, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Tell the user what happened.
    sys.stderr.write(f"\u2705 Split into {len(fragment_files)} fragments:\n")
    for rel_path in sorted(fragment_files):
        sys.stderr.write(f"   {rel_path}\n")
    sys.stderr.write(f"\n   Root contract updated with $ref pointers: {root_out}\n")
    sys.stderr.write("   Run fluid bundle to see the resolved output.\n")

    logger.info(
        "split_complete",
        extra={"fragments": len(fragment_files), "out_dir": str(out_dir)},
    )
    return 0
