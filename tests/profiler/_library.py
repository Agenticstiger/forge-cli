# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A library's branches and its members' cards: fixtures for reference tables and tracked keys.

``branch`` is a small table a contract may mark public. ``card`` is a table with a validity
window, one row per version of a member's card, and a foreign key ``branch_id`` to ``branch``.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from fluid_build.profiler._stats import load_backend
from fluid_build.profiler.runner import ExposeSource, profile_exposes

BRANCH = "branch"
CARD = "card"


def contract(
    *,
    key_type: str = "string",
    classification: Optional[str] = "Public",
    policy: Optional[Dict[str, Any]] = None,
    branch_columns: Sequence[Dict[str, Any]] = (),
    related: bool = True,
    composite: bool = False,
    versioned: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    expose: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    schema: List[Dict[str, Any]] = [
        {"name": "branch_id", "type": key_type},
        {"name": "city", "type": "string"},
        *copy.deepcopy(list(branch_columns)),
    ]
    branch: Dict[str, Any] = {"exposeId": BRANCH, "kind": "table", "contract": {"schema": schema}}
    expose_policy: Dict[str, Any] = dict(policy or {})
    if classification is not None:
        expose_policy["classification"] = classification
    if expose_policy:
        branch["policy"] = expose_policy
    branch.update(copy.deepcopy(expose or {}))
    if composite:
        schema.append({"name": "region", "type": "string"})
    if versioned:
        schema.extend(
            [{"name": "valid_from", "type": "date"}, {"name": "valid_to", "type": "date"}]
        )
        branch["semantics"] = {
            "entities": [{"name": "branch", "type": "natural", "expr": "branch_id"}],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {
                        "timeGranularity": "day",
                        "validityParams": {"isEnd": True, "chain": "nextStart"},
                    },
                },
            ],
        }
    columns = ["branch_id", "region"] if composite else ["branch_id"]
    card_schema = [
        {"name": "member_id", "type": "integer"},
        {"name": "valid_from", "type": "date"},
        {"name": "valid_to", "type": "date"},
        {"name": "branch_id", "type": key_type},
    ]
    if composite:
        card_schema.append({"name": "region", "type": "string"})
    card: Dict[str, Any] = {
        "exposeId": CARD,
        "kind": "table",
        "semantics": {
            "entities": [{"name": "member", "type": "natural", "expr": "member_id"}],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {
                        "timeGranularity": "day",
                        "validityParams": {"isEnd": True, "chain": "nextStart"},
                    },
                },
            ],
        },
        "contract": {"schema": card_schema},
    }
    if related:
        card["semantics"]["relationships"] = [
            {
                "name": "home_branch",
                "to": BRANCH,
                "fromColumns": columns,
                "toColumns": columns,
                "cardinality": "many_to_one",
            }
        ]
    built: Dict[str, Any] = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "library",
        "exposes": [branch, card],
    }
    if metadata is not None:
        built["metadata"] = metadata
    return built


def keys(count: int, *, kind: str = "string") -> List[Any]:
    """*count* branch keys: text, whole numbers or uuids."""
    if kind == "integer":
        return list(range(1, count + 1))
    if kind == "uuid":
        return [f"00000000-0000-4000-8000-{n:012x}" for n in range(1, count + 1)]
    return [f"b{n:02d}" for n in range(1, count + 1)]


def branches(branch_keys: Sequence[Any]) -> List[Dict[str, Any]]:
    return [{"branch_id": key, "city": f"town {n}"} for n, key in enumerate(branch_keys)]


def cards(members: Sequence[Sequence[Any]], region: Optional[str] = None) -> List[Dict[str, Any]]:
    """Each member's versions, each holding the branch it names; chained, the last open."""
    rows: List[Dict[str, Any]] = []
    for member, branch_sequence in enumerate(members):
        begins = date(2020, 1, 1) + timedelta(days=member % 90)
        for position, branch_key in enumerate(branch_sequence):
            last = position == len(branch_sequence) - 1
            row: Dict[str, Any] = {
                "member_id": member,
                "valid_from": begins.isoformat(),
                "valid_to": None if last else (begins + timedelta(days=30)).isoformat(),
                "branch_id": branch_key,
            }
            if region is not None:
                row["region"] = region
            rows.append(row)
            begins += timedelta(days=30)
    return rows


def write(path: Path, rows: Sequence[Dict[str, Any]]) -> str:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def profile(
    tmp_path: Path,
    built: Dict[str, Any],
    branch_rows: Sequence[Dict[str, Any]],
    card_rows: Sequence[Dict[str, Any]],
) -> Any:
    return profile_exposes(
        built,
        [
            ExposeSource(BRANCH, write(tmp_path / "branch.ndjson", branch_rows)),
            ExposeSource(CARD, write(tmp_path / "card.ndjson", card_rows)),
        ],
        backend=load_backend(),
    )


def spread(branch_keys: Sequence[Any], per_branch: Sequence[int]) -> List[List[Any]]:
    """Members with one version each: ``per_branch[n]`` of them at branch ``n``."""
    return [[key] for key, count in zip(branch_keys, per_branch, strict=True) for _ in range(count)]
