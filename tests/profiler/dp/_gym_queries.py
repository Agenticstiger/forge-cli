# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The consumer queries of the gym, for the tests of a private release that answers them.

The gym is the one of :mod:`tests.profiler.dp._gym_data`: ``member`` is the privacy unit and a
``membership_version`` is one version of a card. Every query below has a count that can be worked
out by hand from the eleven versions (``_gym_data._CARDS``), which no member truncates:

* ``plus_rows``: the versions of the ``plus`` tier, 5 of the 11 (a table unit);
* ``full_plus``: of the 6 versions with ``full`` access (a view unit), those of the ``plus`` tier, 4;
* ``full_by_tier``: the versions with ``full`` access, grouped by tier: ``basic`` 2 of 6, ``plus``
  4 of 5;
* ``members_with_two``: of the 5 members that hold a version, those with 2 or more, 4 (the
  entities are the members, the child column of the relationship the unit follows);
* ``cards_with_two``: of the 6 cards, those with 2 or more versions, 4 (the entities are the
  cards, the natural entity of the table).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from tests.profiler.dp import _gym_data as gym
from tests.profiler.dp._gym_data import TABLE

PLUS = f"SELECT card_no FROM {TABLE} WHERE tier = 'plus'"
FULL_VIEW = f"SELECT * FROM {TABLE} WHERE access = 'full'"
FULL_PLUS = (
    f'WITH "v" AS ({FULL_VIEW}) SELECT "v"."card_no" AS "card_no" FROM "v" AS "v" '
    'WHERE "v"."tier" = \'plus\''
)
FULL_BY_TIER = f"SELECT tier, card_no FROM {TABLE} WHERE access = 'full'"
BY_MEMBER = f"SELECT member_id, count(*) AS n FROM {TABLE} GROUP BY member_id"
BY_CARD = f"SELECT card_no, count(*) AS n FROM {TABLE} GROUP BY card_no"


def over(unit: str, column: str) -> str:
    """A query that keeps the records of an entity unit with two versions or more."""
    return (
        f'WITH "v" AS ({unit}) SELECT "v"."{column}" AS "{column}" FROM "v" AS "v" '
        'WHERE "v"."n" >= 2'
    )


def query(
    name: str,
    sql: str,
    *,
    table: Optional[str] = None,
    unit_sql: Optional[str] = None,
    group_by: Optional[str] = None,
) -> Dict[str, Any]:
    """One entry of a request's ``queries``: a table unit, or a view unit with *unit_sql*."""
    found: Dict[str, Any] = {
        "name": name,
        "sql": sql,
        "unit": {"sql": unit_sql} if unit_sql is not None else {"table": table or TABLE},
    }
    if group_by is not None:
        found["groupBy"] = group_by
    return found


PLUS_ROWS = query("plus_rows", PLUS)
FULL_PLUS_VIEW = query("full_plus", FULL_PLUS, unit_sql=FULL_VIEW)
FULL_BY_TIER_QUERY = query("full_by_tier", FULL_BY_TIER, group_by="tier")
MEMBERS_WITH_TWO = query("members_with_two", over(BY_MEMBER, "member_id"), unit_sql=BY_MEMBER)
CARDS_WITH_TWO = query("cards_with_two", over(BY_CARD, "card_no"), unit_sql=BY_CARD)
EVERY = (PLUS_ROWS, FULL_PLUS_VIEW, FULL_BY_TIER_QUERY, MEMBERS_WITH_TWO, CARDS_WITH_TWO)

#: The cells ``(not returned, returned)`` of each query of :data:`EVERY`, group by group.
CELLS: Mapping[str, Sequence[int]] = {
    "plus_rows": (6, 5),
    "full_plus": (2, 4),
    "full_by_tier": (4, 2, 1, 4),
    "members_with_two": (1, 4),
    "cards_with_two": (2, 4),
}


def asked(
    *queries: Mapping[str, Any],
    history: Optional[Mapping[str, Any]] = None,
    with_visits: bool = False,
    pass_history: bool = False,
) -> Dict[str, Any]:
    """The gym request of :func:`tests.profiler.dp._gym_data.request` with *queries* listed."""
    found = gym.request(history=history, with_visits=with_visits, pass_history=pass_history)
    found["queries"] = [dict(each) for each in queries]
    return found
