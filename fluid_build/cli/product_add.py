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
from pathlib import Path
from typing import Any, Dict, List

from ._common import CLIError
from ._io import dump_json
from ._logging import info

COMMAND = "product-add"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help="Add source/exposure/dq to an existing contract",
        description="Append a new source, exposure, or data quality check to an existing FLUID contract.",
    )
    p.add_argument("contract", help="contract.fluid.(json|yaml)")
    p.add_argument("what", choices=["source", "exposure", "dq"], help="What to add")
    p.add_argument("--id", required=True, help="Identifier to add")
    p.add_argument("--description", help="Description of the item")
    p.add_argument(
        "--type", help="Type (for sources: table/view/file; for dq: freshness/schema/quality)"
    )
    p.add_argument("--location", help="Location/path (for sources and exposures)")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        contract_path = Path(args.contract)
        if not contract_path.exists():
            raise CLIError(2, "contract_not_found", {"path": args.contract})

        # Load contract
        info(logger, "product_add_loading", contract=args.contract)
        from fluid_build.loader import _parse_file

        contract = _parse_file(contract_path)

        # Get current values for diff
        section_key = _get_section_key(args.what)
        before_count = len(contract.get(section_key, []))

        # Add new item based on type
        if args.what == "source":
            _add_source(contract, args)
        elif args.what == "exposure":
            _add_exposure(contract, args)
        elif args.what == "dq":
            _add_dq_check(contract, args)

        # Deduplicate
        if section_key in contract:
            contract[section_key] = _deduplicate(contract[section_key], "id")

        after_count = len(contract.get(section_key, []))

        # Write atomically (use JSON for safety; user can convert to YAML if needed)
        output_path = (
            contract_path.with_suffix(".json")
            if contract_path.suffix in (".yaml", ".yml")
            else contract_path
        )
        dump_json(str(output_path), contract)

        # Log summary
        added = after_count - before_count
        info(
            logger,
            "product_add_success",
            what=args.what,
            added=added,
            total=after_count,
            output=str(output_path),
        )

        if added == 0:
            info(logger, "product_add_duplicate", id=args.id)

        return 0

    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "product_add_failed", {"error": str(e)})


def _get_section_key(what: str) -> str:
    """Map 'what' to contract section key."""
    return {"source": "sources", "exposure": "exposures", "dq": "dataQuality"}[what]


def _add_source(contract: Dict[str, Any], args) -> None:
    """Add a source to the contract."""
    if "sources" not in contract:
        contract["sources"] = []

    source = {
        "id": args.id,
        "type": args.type or "table",
    }

    if args.description:
        source["description"] = args.description
    if args.location:
        source["location"] = args.location

    contract["sources"].append(source)


def _add_exposure(contract: Dict[str, Any], args) -> None:
    """Add an exposure to the contract."""
    if "exposures" not in contract:
        contract["exposures"] = []

    exposure = {
        "id": args.id,
        "type": args.type or "dashboard",
    }

    if args.description:
        exposure["description"] = args.description
    if args.location:
        exposure["url"] = args.location

    contract["exposures"].append(exposure)


def _add_dq_check(contract: Dict[str, Any], args) -> None:
    """Add a data quality check to the contract."""
    if "dataQuality" not in contract:
        contract["dataQuality"] = []

    dq = {
        "id": args.id,
        "type": args.type or "quality",
    }

    if args.description:
        dq["description"] = args.description

    contract["dataQuality"].append(dq)


def _deduplicate(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """Deduplicate list of dicts by key, keeping last occurrence."""
    seen = {}
    for item in items:
        if key in item:
            seen[item[key]] = item
    return list(seen.values())
