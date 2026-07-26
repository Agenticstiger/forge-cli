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

"""``fluid product-add`` — append a source / exposure / dq rule to a contract.

Items are written to their **canonical** homes in the FLUID schema (the
contract root is closed, ``additionalProperties: false``), so the contract
still passes ``fluid validate`` afterward:

* ``source``   -> a ``consumes[]`` entry (``{productId, exposeId}``) — an
  upstream product expose this contract reads.
* ``exposure`` -> an ``exposes[]`` entry (``{exposeId, kind, binding,
  contract}``) — a data interface this product publishes.
* ``dq``       -> a rule under ``exposes[].contract.dq.rules[]``
  (``{id, type, severity}``) on the targeted expose (``--expose``, else the
  first expose).

The result is written back **in place, to the file the user named**, in that
file's own serialisation (YAML in -> YAML out, JSON in -> JSON out). Writing a
sibling ``.json`` for YAML input silently broke accumulation: every invocation
re-read the untouched YAML, so ``product-add`` twice kept only the second item.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from ._common import CLIError
from ._io import atomic_write, dump_json
from ._logging import info
from .console import cprint

COMMAND = "product-add"

# Closed enums from the bundled FLUID schema ($defs.expose.kind, $defs.dqRule.type).
_EXPOSE_KINDS = {
    "table",
    "view",
    "api",
    "file",
    "stream",
    "topic",
    "feature_store",
    "model",
    "vector",
    "graph",
    "time_series",
    "other",
}
_DQ_TYPES = {
    "freshness",
    "completeness",
    "uniqueness",
    "valid_values",
    "accuracy",
    "schema",
    "anomaly_detection",
    "drift_detection",
}
_DQ_SEVERITIES = {"info", "warn", "error", "critical"}
# Sensible default binding.format per platform (binding requires platform/format/location).
_PLATFORM_FORMAT = {
    "local": "parquet",
    "gcp": "bigquery_table",
    "aws": "s3_file",
    "azure": "delta_table",
    "snowflake": "snowflake_table",
    "databricks": "delta_table",
    "kafka": "kafka_topic",
    "confluent": "kafka_topic",
    "postgres": "native",
}


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help="Add a source/exposure/dq rule to an existing contract",
        description=(
            "Append a source (consumes[]), exposure (exposes[]), or data-quality "
            "rule (exposes[].contract.dq.rules[]) to an existing FLUID contract. "
            "Output stays schema-valid."
        ),
    )
    p.add_argument("contract", help="contract.fluid.(json|yaml)")
    p.add_argument("what", choices=["source", "exposure", "dq"], help="What to add")
    p.add_argument(
        "--id",
        required=True,
        help=("Identifier: exposeId (exposure), upstream productId (source), " "or rule id (dq)"),
    )
    p.add_argument("--description", help="Description / purpose of the item")
    p.add_argument(
        "--type",
        help=(
            "exposure: expose kind (table/view/file/stream/topic/...; default table). "
            "dq: rule type (completeness/freshness/uniqueness/valid_values/accuracy/"
            "schema/...; default completeness)."
        ),
    )
    p.add_argument(
        "--location",
        help="exposure: binding location path. source: upstream exposeId.",
    )
    p.add_argument(
        "--platform",
        help="exposure: binding platform (local/gcp/aws/snowflake/...; default local)",
    )
    p.add_argument(
        "--expose",
        help="dq: target exposeId to attach the rule to (default: first expose)",
    )
    p.add_argument(
        "--severity",
        choices=sorted(_DQ_SEVERITIES),
        help="dq: rule severity (default warn)",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        contract_path = Path(args.contract)
        if not contract_path.exists():
            raise CLIError(2, "contract_not_found", {"path": args.contract})

        info(logger, "product_add_loading", contract=args.contract)
        from fluid_build.loader import _parse_file

        contract = _parse_file(contract_path)

        if args.what == "source":
            added, total = _add_source(contract, args)
        elif args.what == "exposure":
            added, total = _add_exposure(contract, args)
        else:  # dq
            added, total = _add_dq_check(contract, args)

        # Write atomically, back to the file the user named and in its own
        # format. Anything else makes repeated calls non-cumulative: the next
        # invocation re-reads the (untouched) input and starts over.
        _dump_contract(contract_path, contract)

        info(
            logger,
            "product_add_success",
            what=args.what,
            added=added,
            total=total,
            output=str(contract_path),
        )
        if added == 0:
            cprint(f"= {args.what} '{args.id}' already present in {contract_path} " f"({total} total) — nothing to do")
            info(logger, "product_add_duplicate", id=args.id)
        else:
            cprint(f"✅ Added {args.what} '{args.id}' to {contract_path} ({total} total)")

        return 0

    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "product_add_failed", {"error": str(e)})


def _dump_contract(path: Path, contract: Dict[str, Any]) -> None:
    """Serialise ``contract`` back over ``path`` using that path's own format.

    ``.yaml`` / ``.yml`` round-trip through ``yaml.safe_dump`` (the repo-wide
    idiom: ``sort_keys=False`` so authored key order survives); everything else
    is JSON. PyYAML is not a round-trip loader, so YAML comments do not survive
    — say so rather than dropping them silently.
    """
    if path.suffix.lower() not in (".yaml", ".yml"):
        dump_json(str(path), contract)
        return

    import yaml

    original = path.read_text(encoding="utf-8")
    atomic_write(
        str(path),
        yaml.safe_dump(contract, sort_keys=False, default_flow_style=False, allow_unicode=True),
    )
    if any(line.lstrip().startswith("#") for line in original.splitlines()):
        cprint(f"⚠  YAML comments in {path} were not preserved (contract rewritten from parsed data)")


def _add_source(contract: Dict[str, Any], args) -> Tuple[int, int]:
    """Add an upstream dependency as a canonical ``consumes[]`` entry."""
    consumes: List[Dict[str, Any]] = contract.setdefault("consumes", [])
    before = len(consumes)

    consume: Dict[str, Any] = {
        "productId": args.id,
        # consumeRef requires exposeId; use --location as the upstream expose,
        # falling back to the productId when unspecified.
        "exposeId": args.location or args.id,
    }
    if args.description:
        consume["purpose"] = args.description

    consumes.append(consume)
    contract["consumes"] = _deduplicate_by(
        consumes, lambda c: (c.get("productId"), c.get("exposeId"))
    )
    total = len(contract["consumes"])
    return total - before, total


def _add_exposure(contract: Dict[str, Any], args) -> Tuple[int, int]:
    """Add a data interface as a canonical ``exposes[]`` entry."""
    exposes: List[Dict[str, Any]] = contract.setdefault("exposes", [])
    before = len(exposes)

    platform = args.platform or "local"
    kind = args.type if args.type in _EXPOSE_KINDS else "table"
    expose: Dict[str, Any] = {
        "exposeId": args.id,
        "kind": kind,
        "binding": {
            "platform": platform,
            "format": _PLATFORM_FORMAT.get(platform, "parquet"),
            "location": {"path": args.location or f"output/{args.id}.parquet"},
        },
        # Empty schema is valid across all bundled schema versions; avoid
        # version-specific optional keys (e.g. schemaPolicy) so the output stays
        # valid regardless of the target contract's fluidVersion.
        "contract": {"schema": []},
    }
    if args.description:
        expose["description"] = args.description

    exposes.append(expose)
    contract["exposes"] = _deduplicate(exposes, "exposeId")
    total = len(contract["exposes"])
    return total - before, total


def _add_dq_check(contract: Dict[str, Any], args) -> Tuple[int, int]:
    """Add a data-quality rule under the target expose's ``contract.dq.rules``."""
    exposes: List[Dict[str, Any]] = contract.get("exposes", [])
    if not exposes:
        raise CLIError(
            2,
            "product_add_no_expose",
            {
                "hint": (
                    "dq rules attach to an expose; add an exposure first "
                    "(`fluid product-add <contract> exposure --id ...`)."
                )
            },
        )

    if args.expose:
        target = next((e for e in exposes if e.get("exposeId") == args.expose), None)
        if target is None:
            raise CLIError(2, "product_add_expose_not_found", {"expose": args.expose})
    else:
        target = exposes[0]

    expose_contract: Dict[str, Any] = target.setdefault("contract", {})
    rules: List[Dict[str, Any]] = expose_contract.setdefault("dq", {}).setdefault("rules", [])
    before = len(rules)

    rule: Dict[str, Any] = {
        "id": args.id,
        "type": args.type if args.type in _DQ_TYPES else "completeness",
        "severity": args.severity or "warn",
    }
    if args.description:
        rule["description"] = args.description

    rules.append(rule)
    expose_contract["dq"]["rules"] = _deduplicate(rules, "id")
    total = len(expose_contract["dq"]["rules"])
    return total - before, total


def _deduplicate(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """Deduplicate dicts by a single key, keeping the last occurrence."""
    seen: Dict[Any, Dict[str, Any]] = {}
    passthrough: List[Dict[str, Any]] = []
    for item in items:
        if key in item:
            seen[item[key]] = item
        else:
            passthrough.append(item)
    return passthrough + list(seen.values())


def _deduplicate_by(
    items: List[Dict[str, Any]], keyfn: Callable[[Dict[str, Any]], Any]
) -> List[Dict[str, Any]]:
    """Deduplicate dicts by a composite key function, keeping the last occurrence."""
    seen: Dict[Any, Dict[str, Any]] = {}
    for item in items:
        seen[keyfn(item)] = item
    return list(seen.values())
