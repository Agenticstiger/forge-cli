# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A gym's members and their membership cards, for the tests of a private release of a history.

``member`` is the privacy unit. A ``membership_version`` is one version of a card (the natural
entity ``card_no``, the window ``valid_from`` to ``valid_to``, a ``tier`` and an ``access`` level that
a version may change), and a card belongs to one member. A ``visit`` follows a member and declares no
window, and a ``guest_pass`` declares one but no relationship: the unit never reaches it.

``CARDS`` holds six cards of eleven versions in all, built so that every member of a history has a
cell a reader can work out by hand (see ``test_history_release``).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from fluid_build.profiler import sources
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import release
from fluid_build.profiler.dp.follows import parse_follow
from tests.profiler._fake_backend import FakeBackend

SEED = bytes(range(11, 43))
PRODUCER = "https://example.test/producer"
REQUEST_URL_1_3_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-3-0/FluxProfileRequest.json"
)
TABLE = "membership_version"
FOLLOW = f"member.member_id={TABLE}.member_id:4"
FOLLOW_VISITS = "member.member_id=visit.member_id:2"

CONTRACT: Dict[str, Any] = yaml.safe_load("""
fluidVersion: "0.7.7"
kind: DataProduct
id: gym.memberships
name: Gym memberships
metadata:
  owner:
    team: gym-team
exposes:
  - exposeId: member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
        - { name: branch, type: "enum('city','harbor')", required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
  - exposeId: membership_version
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/membership_version.parquet }
    contract:
      schema:
        - { name: card_no, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
        - { name: tier, type: "enum('basic','plus')", required: true }
        - { name: access, type: "enum('day','full')", required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE }
    semantics:
      entities:
        - { name: card, type: natural, expr: card_no }
      dimensions:
        - name: valid_from
          type: time
          typeParams: { validityParams: { isStart: true } }
        - name: valid_to
          type: time
          typeParams: { validityParams: { isEnd: true, chain: nextStart } }
      relationships:
        - name: held_by
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
  - exposeId: visit
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/visit.parquet }
    contract:
      schema:
        - { name: visit_id, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
        - { name: lane, type: "enum('pool','floor')", required: true }
    semantics:
      entities:
        - { name: visit, type: primary, expr: visit_id }
      relationships:
        - name: made_by
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
  - exposeId: guest_pass
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/guest_pass.parquet }
    contract:
      schema:
        - { name: pass_no, type: INT, required: true }
        - { name: tier, type: "enum('basic','plus')", required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE }
    semantics:
      entities:
        - { name: pass, type: natural, expr: pass_no }
      dimensions:
        - name: valid_from
          type: time
          typeParams: { validityParams: { isStart: true } }
        - name: valid_to
          type: time
          typeParams: { validityParams: { isEnd: true } }
""")

MEMBER_COLUMNS = ("member_id", "branch")
VERSION_COLUMNS = ("card_no", "member_id", "tier", "access", "valid_from", "valid_to")
VISIT_COLUMNS = ("visit_id", "member_id", "lane")
PASS_COLUMNS = ("pass_no", "tier", "valid_from", "valid_to")

# card, member, tier, access, valid_from, valid_to
_CARDS: Sequence[Tuple[str, str, str, str, str, Optional[str]]] = (
    ("101", "1", "basic", "day", "2026-01-01", "2026-02-01"),
    ("101", "1", "plus", "full", "2026-02-01", None),
    ("102", "1", "plus", "full", "2026-03-10", "2026-04-09"),
    ("103", "2", "basic", "day", "2025-12-01", "2025-12-31"),
    ("103", "2", "basic", "full", "2026-01-02", "2026-02-01"),
    ("103", "2", "plus", "full", "2026-02-01", None),
    ("104", "3", "plus", "full", "2026-01-01", "2026-01-10"),
    ("104", "3", "basic", "day", "2026-01-11", None),
    ("105", "4", "basic", "day", "2026-01-01", None),
    ("105", "4", "basic", "full", "2026-01-15", None),
    ("106", "5", "plus", "day", "2025-06-01", "2025-07-01"),
)


def members() -> List[Dict[str, Optional[str]]]:
    return [{"member_id": str(n), "branch": ("city", "harbor")[n % 2]} for n in range(1, 7)]


def versions() -> List[Dict[str, Optional[str]]]:
    return [dict(zip(VERSION_COLUMNS, row, strict=True)) for row in _CARDS]


def visits() -> List[Dict[str, Optional[str]]]:
    return [
        {"visit_id": str(n), "member_id": str(1 + n % 6), "lane": ("pool", "floor")[n % 2]}
        for n in range(12)
    ]


def passes() -> List[Dict[str, Optional[str]]]:
    return [{"pass_no": "1", "tier": "basic", "valid_from": "2026-01-01", "valid_to": None}]


def tables(
    version_rows: Optional[Sequence[Mapping[str, Optional[str]]]] = None,
) -> Dict[str, Sequence[Mapping[str, Optional[str]]]]:
    return {
        "member": members(),
        TABLE: versions() if version_rows is None else version_rows,
        "visit": visits(),
        "guest_pass": passes(),
    }


def contract(*, entity: str = "card_no") -> Dict[str, Any]:
    """The gym contract; with ``entity="member_id"`` a member is the natural entity of a version."""
    found = copy.deepcopy(CONTRACT)
    if entity != "card_no":
        (natural,) = found["exposes"][1]["semantics"]["entities"]
        natural["expr"] = entity
    return found


def request(
    *,
    history: Optional[Mapping[str, Any]] = None,
    null_counts: Sequence[str] = (),
    visits_history: bool = False,
    pass_history: bool = False,
    member_history: bool = False,
    with_visits: bool = False,
) -> Dict[str, Any]:
    """A request of format 1-3-0; *history* is the ask of ``membership_version`` (``{}``: defaults)."""
    version: Dict[str, Any] = {
        "columns": {
            "card_no": {"domain": "number"},
            "member_id": {"domain": "number"},
            "tier": {"domain": "category", "statistics": ["levelHistogram/1"]},
            "access": {"domain": "category"},
            "valid_from": {"domain": "timestamp"},
            "valid_to": {"domain": "timestamp"},
        }
    }
    for name in null_counts:
        asked = version["columns"][name]
        asked["statistics"] = [*asked.get("statistics", ()), "nullCount/2"]
    if history is not None:
        version["history"] = dict(history)
    tables_: Dict[str, Any] = {
        "member": {
            "columns": {
                "member_id": {"domain": "number"},
                "branch": {"domain": "category", "statistics": ["levelHistogram/1"]},
            },
            **({"history": {}} if member_history else {}),
        },
        TABLE: version,
    }
    if with_visits or visits_history:
        tables_["visit"] = {
            "columns": {
                "visit_id": {"domain": "number"},
                "member_id": {"domain": "number"},
                "lane": {"domain": "category", "statistics": ["levelHistogram/1"]},
            },
            **({"history": {}} if visits_history else {}),
        }
    if pass_history:
        tables_["guest_pass"] = {
            "columns": {
                "pass_no": {"domain": "number"},
                "tier": {"domain": "category", "statistics": ["levelHistogram/1"]},
                "valid_from": {"domain": "timestamp"},
                "valid_to": {"domain": "timestamp"},
            },
            "history": {},
        }
    return {
        "_schemaURL": REQUEST_URL_1_3_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "tables": tables_,
    }


def follow_plan(
    asked: Optional[Mapping[str, Any]] = None,
    *,
    contract_: Optional[Mapping[str, Any]] = None,
    follows: Sequence[str] = (FOLLOW,),
    rho: str = "1",
) -> fr.FollowReleasePlan:
    backend = FakeBackend()
    built = backend.read_request(
        json.dumps(request(history={}) if asked is None else asked),
        name="gym.profile-request.yml",
    )
    return fr.plan_follow_release(
        CONTRACT if contract_ is None else contract_,
        built,
        backend,
        unit="member",
        rho=release.parse_rho(rho),
        follows=tuple(parse_follow(text) for text in follows),
    )


def read_of(
    plan: fr.FollowReleasePlan,
    held: Optional[Mapping[str, Sequence[Mapping[str, Optional[str]]]]] = None,
) -> Dict[str, sources.SourceColumns]:
    values = tables() if held is None else held
    return {
        table: sources.SourceColumns(
            description="rows",
            row_count=len(values[table]),
            values={
                name: [row[name] for row in values[table]]
                for name in dict.fromkeys(
                    [
                        *(column.name for column in columns),
                        *(values[table][0] if values[table] else ()),
                    ]
                )
            },
        )
        for table, columns in plan.reads
    }


def encode(
    plan: fr.FollowReleasePlan,
    held: Optional[Mapping[str, Sequence[Mapping[str, Optional[str]]]]] = None,
) -> fr.FollowRows:
    return fr.encode_follow_rows(plan, read_of(plan, held), FakeBackend())


def draw(
    plan: fr.FollowReleasePlan,
    held: Optional[Mapping[str, Sequence[Mapping[str, Optional[str]]]]] = None,
    seed: bytes = SEED,
) -> Tuple[fr.FollowRelease, fr.FollowRows]:
    backend = FakeBackend()
    rows = fr.encode_follow_rows(plan, read_of(plan, held), backend)
    drawn = fr.draw_follow(plan, rows, seed, backend, cliques={}, producer=PRODUCER)
    return drawn, rows


def history_of(drawn: fr.FollowRelease) -> Dict[str, Any]:
    found: Dict[str, Any] = drawn.document["tables"][TABLE]["history"]
    return found
