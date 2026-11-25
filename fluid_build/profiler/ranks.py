# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Rank correlations of column pairs: the ``columnPairs`` a request's ``rankCorrelations`` asks for.

A request (format 1-2-0) lists, for a table, a rank correlation definition and the number or
timestamp columns it is asked of. The profile holds one ``columnPairs`` entry for each unordered
pair of them, the column listed first first, and in it ``kendallTau/1`` (the sum ``S`` of the
products of the signs of the two differences over every pair of records) and ``spearmanRho/1``
(``A4``, four times the sum of the products of the centred mid-ranks), as PROFILE-SPEC
section 5 defines them. Both read the records whose two values are known, after each value is
floored onto the profile's grid, so a tie is a tie on the grid. Neither is the Pearson
correlation of the mid-ranks, which has no bound on what one added record can change.

The registry forms both (:meth:`~fluid_build.profiler._stats.StatisticsBackend.rank_group`);
this module chooses the pairs, reads and floors the values, and applies the disclosure rule.

The rule is the group size rule of section 3, which a group of a ``correlation/1`` follows too:
a group of fewer than ``minimumCell`` records is withheld, and so is a group of fewer than two,
which no profile may publish. A published group states its ``k`` exactly, since ``S`` and
``A4`` are bounded by it and the value is formed from it, so a rule that rounds counts does not
round this one: the ``sampleSize`` band is the band of the rounded count, as for every other
statistic. A ``rowFloor``, given with ``--row-floor``, is written as the statistic's own and the
value is formed with it. A key column of a counted relationship takes no rank correlation, as it
takes no other statistic of its own.

Nothing here names a value: a reason names a column and a statistic.
"""

from __future__ import annotations

from collections import Counter
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from fluid_build.profiler import disclosure, sources
from fluid_build.profiler._stats import RequestTable, StatisticsBackend

__all__ = ["MOST_ROW_FLOOR", "column_pairs", "contract_unformed"]

#: The largest ``rowFloor`` a profile holds: a count, so at most 2^53 - 1.
MOST_ROW_FLOOR = disclosure.MAX_COUNT

#: The order a pair's statistics are written in.
_ORDER = ("kendallTau/1", "spearmanRho/1")

_KEY = "a key of a counted relationship, which its children per parent describe"


def column_pairs(
    table: RequestTable,
    domains: Mapping[str, str],
    read: sources.SourceColumns,
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    fractional_bits: int,
    *,
    row_floor: Optional[int] = None,
    held: FrozenSet[str] = frozenset(),
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """The ``columnPairs`` of *table*, and each pair asked that the profile does not hold.

    *domains* are the domains the profile reads the table's columns in, *read* its records
    and *held* the key columns of its counted relationships. *tally* counts each statistic by
    the status it is written with.
    """
    entries: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    left_out: List[Tuple[str, str]] = []
    encoded: Dict[str, List[Optional[int]]] = {}
    encode = backend.encoder(fractional_bits)

    def grid_points(name: str) -> List[Optional[int]]:
        if name not in encoded:
            values = [
                backend.read_value(domains[name], value, column=name, row_index=index)
                for index, value in enumerate(read.values[name])
            ]
            encoded[name] = [None if value is None else encode(Fraction(value)) for value in values]
        return encoded[name]

    for ask in table.rank_correlations:
        for position, first in enumerate(ask.columns):
            for second in ask.columns[position + 1 :]:
                reason = _unavailable(first, second, domains, held)
                if reason is not None:
                    left_out.append((_what(ask.definition, table.expose_id, first, second), reason))
                    continue
                pairs = [
                    (x, y)
                    for x, y in zip(grid_points(first), grid_points(second), strict=True)
                    if x is not None and y is not None
                ]
                entries.setdefault((first, second), []).append(
                    _statistic(ask.definition, pairs, backend, rule, tally, row_floor)
                )
    found = [
        {
            "columns": [first, second],
            "statistics": sorted(statistics, key=lambda each: _ORDER.index(each["definition"])),
        }
        for (first, second), statistics in entries.items()
    ]
    return found, left_out


def _what(definition: str, expose_id: str, first: str, second: str) -> str:
    return f"{definition} of {expose_id}.{first} and {second}"


def contract_unformed(
    table: RequestTable, domains: Mapping[str, str], held: FrozenSet[str]
) -> List[Tuple[str, str]]:
    """Each rank correlation of *table* the contract rules out, as :func:`column_pairs` names it."""
    found: List[Tuple[str, str]] = []
    for ask in table.rank_correlations:
        for position, first in enumerate(ask.columns):
            for second in ask.columns[position + 1 :]:
                reason = _unavailable(first, second, domains, held)
                if reason is not None:
                    found.append((_what(ask.definition, table.expose_id, first, second), reason))
    return found


def _unavailable(
    first: str, second: str, domains: Mapping[str, str], held: FrozenSet[str]
) -> Optional[str]:
    """Why a pair has no rank correlation here, or ``None``."""
    for name in (first, second):
        if name not in domains:
            return f"{name} is a column fluid profile does not read"
        if domains[name] not in ("number", "timestamp"):
            return f"{name} is not a number or timestamp column"
        if name in held:
            return _KEY
    return None


def _statistic(
    definition: str,
    pairs: Sequence[Tuple[int, int]],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    row_floor: Optional[int],
) -> Dict[str, Any]:
    """One rank correlation of a pair, its one group published or withheld."""
    records = len(pairs)
    verdict = disclosure.check_count(records, rule)
    if verdict.published and records < 2:
        verdict = disclosure.Verdict(disclosure.SMALL_COUNT, "fewer than two records")
    if verdict.published:
        group = {**backend.rank_group(definition, pairs), "status": disclosure.FREE}
    else:
        group = {"group": "", "status": verdict.status}
    tally[verdict.status] += 1
    statistic: Dict[str, Any] = {
        "definition": definition,
        "sampleSize": disclosure.sample_size(records, rule),
        "disclosure": rule.name,
        "groups": [group],
    }
    if row_floor is not None:
        statistic["rowFloor"] = row_floor
    return statistic
