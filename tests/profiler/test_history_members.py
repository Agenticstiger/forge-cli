# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A history's members under the output checks: coded ladders, declared zeros, linked cells.

The members a table's history publishes (PROFILE-SPEC format 1-3-0) are checked a cell at a
time: their ladders are trimmed to the bins that hold records and coded at the ends, the zeros
the contract states are written as zeros, and the first starts are checked beside the start and
end columns. The records are a library's memberships, one row per version of a member's card.

The probes read the document as a reader would, with linear algebra over every count it
publishes (the start and end columns' quantiles, histograms and extremes, the first starts and
the totals), and look for a small cell that the first starts let a reader form.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler import dates, disclosure, history
from fluid_build.profiler._stats import load_backend
from fluid_build.profiler.runner import ExposeSource, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

RULE = disclosure.ACRO_DEFAULT
TABLE = "membership_version"
TIERS = ("basic", "closed", "plus")
DAY = 86_400_000


def _contract(*, lifecycle: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entity: Dict[str, Any] = {"name": "member", "type": "natural", "expr": "member_id"}
    if lifecycle is not None:
        entity["lifecycle"] = lifecycle
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "library",
        "exposes": [
            {
                "exposeId": TABLE,
                "kind": "table",
                "semantics": {
                    "entities": [entity],
                    "dimensions": [
                        {
                            "name": "valid_from",
                            "type": "time",
                            "typeParams": {
                                "timeGranularity": "day",
                                "validityParams": {"isStart": True},
                            },
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
                "contract": {
                    "schema": [
                        {"name": "member_id", "type": "integer"},
                        {"name": "valid_from", "type": "date"},
                        {"name": "valid_to", "type": "date"},
                        {"name": "tier", "type": "enum('basic','closed','plus')"},
                        {"name": "is_current", "type": "enum('N','Y')"},
                    ]
                },
            }
        ],
    }


def _cards(
    by_versions: Dict[int, int],
    *,
    gaps: Sequence[int] = (30, 60, 90, 120),
    spread: int = 4,
    closed_last: int = 0,
    closed_every: int = 0,
    closed_before: int = 10**6,
    mark_last: bool = False,
    start: date = date(2019, 1, 1),
) -> List[Dict[str, Any]]:
    """Each member's versions, chained end to next start; the last open unless *closed_last*."""
    rows: List[Dict[str, Any]] = []
    number = 0
    for versions, members in by_versions.items():
        for _ in range(members):
            begins = start + timedelta(days=number * spread)
            for k in range(versions):
                gap = gaps[(number + k) % len(gaps)]
                last = k == versions - 1
                closed = (
                    not last
                    or number < closed_last
                    or (closed_every > 0 and number % closed_every == 0 and number < closed_before)
                )
                tier = TIERS[2] if k % 2 else TIERS[0]
                if mark_last and last:
                    tier = "closed"
                rows.append(
                    {
                        "member_id": number,
                        "valid_from": begins.isoformat(),
                        "valid_to": (begins + timedelta(days=gap)).isoformat() if closed else None,
                        "tier": tier,
                        "is_current": "Y" if last else "N",
                    }
                )
                begins += timedelta(days=gap)
            number += 1
    return rows


def _profile(tmp_path: Path, contract: Dict[str, Any], rows: Sequence[Dict[str, Any]]) -> Any:
    path = tmp_path / "cards.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return profile_exposes(contract, [ExposeSource(TABLE, str(path))], backend=load_backend())


def _table(result: Any) -> Dict[str, Any]:
    found: Dict[str, Any] = result.document["tables"][TABLE]
    return found


def _group(entry: Dict[str, Any], key: str = "") -> Dict[str, Any]:
    found: Dict[str, Any] = next(group for group in entry["groups"] if group["group"] == key)
    return found


def _shown(group: Dict[str, Any], field: str) -> List[Optional[int]]:
    statuses = group.get("cellStatus", ["F"] * len(group[field]))
    return [c if s == "F" else None for c, s in zip(group[field], statuses, strict=True)]


def _ms(text: str) -> int:
    return (date.fromisoformat(text) - date(1970, 1, 1)).days * DAY


# -- the coded ladder ---------------------------------------------------------------------------


def test_a_coded_span_trims_the_empty_ends_and_folds_a_small_end_bin_inward():
    assert dates.coded_span([0, 0, 40, 30, 0, 0], 10) == [(2, 2), (3, 3)]
    # a small end bin is folded inward, and the coded end reaches the ladder's public end
    assert dates.coded_span([0, 3, 40, 0, 30, 2, 0], 10) == [(0, 2), (3, 3), (4, 6)]
    # or the public bound the caller gives
    assert dates.coded_span([0, 3, 40, 30, 2, 0, 0], 10, 1, 5) == [(1, 2), (3, 5)]
    # an interior zero stays a cell of its own, for the check to see
    assert dates.coded_span([12, 0, 12], 10) == [(0, 0), (1, 1), (2, 2)]
    # a small end bin is folded until the end holds the least cell
    assert dates.coded_span([12, 4, 3, 3], 10) == [(0, 0), (1, 3)]
    assert dates.coded_span([12, 4, 3, 2], 10) == [(0, 3)]
    assert dates.coded_span([4, 3, 3, 12], 10) == [(0, 2), (3, 3)]
    assert dates.coded_span([5, 4], 10) is None
    assert dates.coded_span([0, 0], 10) is None
    assert dates.coded_span([6, 4], 10) == [(0, 1)]
    # each end is coded by its own outermost bin, even when one fold takes every bin
    assert dates.coded_span([9, 0, 0, 1, 0, 0], 10) == [(0, 5)]
    assert dates.coded_span([0, 9, 0, 0, 1, 0], 10, 0, 5) == [(0, 5)]
    assert dates.coded_span([0, 40, 0, 0, 1, 0], 10) == [(1, 5)]
    # the least cell is the bar: an end bin of exactly that many stands, one fewer folds
    assert dates.coded_span([10, 40], 10) == [(0, 0), (1, 1)]
    assert dates.coded_span([9, 40], 10) == [(0, 1)]


def test_coded_edges_and_cells_follow_the_runs():
    ladder = [0, 1, 2, 8, 31, 91]
    runs = dates.coded_span([0, 3, 40, 30, 2], 10)
    assert runs == [(0, 2), (3, 4)]
    assert dates.coded_edges(ladder, runs) == [0, 8, 91]
    assert dates.coded_cells([0, 3, 40, 30, 2], runs) == [43, 32]


# -- versions per entity and version lengths ----------------------------------------------------


def test_versions_per_entity_is_published_on_the_bins_that_hold_members(tmp_path):
    # Three members with a dozen versions: too few for a top bin, so they join the one below it.
    result = _profile(tmp_path, _contract(), _cards({1: 120, 2: 90, 3: 60, 5: 40, 12: 3}))
    entry = _table(result)["history"]["versionsPerEntity"]
    # and that coded top bin reaches the ladder's own top, so no member's count places it
    assert entry["bins"] == ["1", "2", "3", "5", str(disclosure.MAX_COUNT)]
    group = _group(entry)
    assert group["status"] == "F" and group["k"] == disclosure.round_count(313, RULE)
    assert _shown(group, "binCounts") == [120, 90, 60, disclosure.round_count(43, RULE)]


def test_a_single_zero_cell_withholds_that_cell_not_the_member(tmp_path):
    # No member holds three or four versions: that cell is withheld (and the cells the check needs
    # beside it), and the group with its entities stays published.
    result = _profile(tmp_path, _contract(), _cards({1: 120, 2: 90, 5: 60, 10: 40}))
    entry = _table(result)["history"]["versionsPerEntity"]
    assert entry["bins"] == ["1", "2", "3", "5", "10", "20"]
    group = _group(entry)
    assert group["status"] == "F" and "k" in group
    assert group["cellStatus"][2] == "C"
    assert group["cellStatus"].count("F") >= 2


def test_version_lengths_with_small_end_bins_are_coded_and_published(tmp_path):
    # A few one-day versions and a few of three years sit in end bins of their own: they are
    # folded inward, and the histogram is published rather than withheld whole.
    rows = _cards({3: 200}, gaps=(40, 40, 100, 200, 40, 100, 100, 200) * 12 + (1, 1100))
    result = _profile(tmp_path, _contract(), rows)
    entry = _table(result)["history"]["versionLength"]
    assert _group(entry)["status"] == "F"
    edges = [int(each) for each in entry["bins"]]
    # the coded ends reach day 0 and a public top: past the published starts' spread, or the
    # ladder's own top when the start column publishes no bound
    assert edges[0] == 0 and edges[-1] in (3653, dates.DAY_LADDER[-1])
    assert set(edges) <= set(dates.DAY_LADDER)


@pytest.mark.parametrize("extra", [{7: 1}, {7: 3}, {15: 2}])
def test_one_extreme_entity_does_not_move_an_outer_edge(tmp_path, extra):
    plain = _table(_profile(tmp_path, _contract(), _cards({2: 100, 3: 50})))["history"]
    more = _table(_profile(tmp_path, _contract(), _cards({2: 100, 3: 50, **extra})))["history"]
    edges = more["versionsPerEntity"]["bins"]
    assert edges[0] == plain["versionsPerEntity"]["bins"][0]
    assert edges[-1] in (plain["versionsPerEntity"]["bins"][-1], str(disclosure.MAX_COUNT))


def test_one_extreme_version_length_does_not_set_an_outer_edge(tmp_path):
    rows = _cards({3: 200}, gaps=(40, 100, 200))
    lone = _cards({3: 200}, gaps=(40, 100, 200) * 133 + (1100,))
    edges = _table(_profile(tmp_path, _contract(), lone))["history"]["versionLength"]["bins"]
    plain = _table(_profile(tmp_path, _contract(), rows))["history"]["versionLength"]["bins"]
    assert edges[:-1] == plain[:-1]
    assert int(edges[-1]) not in (731, 1461)


# -- zeros the contract states ------------------------------------------------------------------


def test_an_end_marker_publishes_no_closed_last_version_as_zero(tmp_path):
    rows = _cards({1: 60, 2: 60, 3: 40}, mark_last=True)
    marked = _contract(lifecycle={"endMarker": {"column": "tier", "value": "closed"}})
    result = _profile(tmp_path, marked, rows)
    group = _group(_table(result)["history"]["lastVersion"])
    assert group["status"] == "F"
    assert _shown(group, "levelCounts") == [0, disclosure.round_count(160, RULE)]
    plain = _group(_table(_profile(tmp_path, _contract(), rows))["history"]["lastVersion"])
    assert "F" not in plain.get("cellStatus", ["C", "C"]) or plain["status"] != "F"


def test_an_end_marker_the_records_break_is_checked_like_any_cell(tmp_path):
    rows = _cards({1: 60, 2: 60, 3: 40}, mark_last=True, closed_last=3)
    marked = _contract(lifecycle={"endMarker": {"column": "tier", "value": "closed"}})
    group = _group(_table(_profile(tmp_path, marked, rows))["history"]["lastVersion"])
    assert group.get("cellStatus", ["F", "F"])[0] == "C" or group["status"] != "F"


def test_the_marked_value_has_no_transitions_and_publishes_them_as_zeros(tmp_path):
    rows = _cards({1: 60, 2: 60, 3: 40}, mark_last=True)
    marked = _contract(lifecycle={"endMarker": {"column": "tier", "value": "closed"}})
    transitions = _table(_profile(tmp_path, marked, rows))["history"]["columns"]["tier"][
        "transitions"
    ]
    group = _group(transitions, "closed")
    assert group["status"] == "F"
    assert _shown(group, "levelCounts") == [0, 0, 0]


def test_a_current_flag_publishes_the_flagged_value_s_transitions_as_zeros(tmp_path):
    rows = _cards({1: 60, 2: 60, 3: 40})
    flagged = _contract(lifecycle={"currentFlag": {"column": "is_current", "value": "Y"}})
    transitions = _table(_profile(tmp_path, flagged, rows))["history"]["columns"]["is_current"][
        "transitions"
    ]
    assert _shown(_group(transitions, "Y"), "levelCounts") == [0, 0]
    assert _group(transitions, "N")["status"] == "F"
    plain = _table(_profile(tmp_path, _contract(), rows))["history"]["columns"]["is_current"]
    assert all(group["status"] != "F" for group in plain["transitions"]["groups"])


def test_a_few_rows_with_no_member_and_no_end_withhold_the_open_last_versions(tmp_path):
    # The end column's NULLs less the open last versions would count those few rows.
    rows = _cards({1: 60, 2: 60, 3: 40}, closed_every=4)
    whole = _group(_table(_profile(tmp_path, _contract(), rows))["history"]["lastVersion"])
    assert whole["status"] == "F" and "cellStatus" not in whole
    rows += [
        {
            "member_id": None,
            "valid_from": "2020-01-01",
            "valid_to": None,
            "tier": "basic",
            "is_current": "Y",
        }
        for _ in range(3)
    ]
    group = _group(_table(_profile(tmp_path, _contract(), rows))["history"]["lastVersion"])
    assert group["status"] != "F" or "F" not in group.get("cellStatus", ["F"])
    assert (
        _group(_table(_profile(tmp_path, _contract(), rows))["history"]["versionsPerEntity"])[
            "status"
        ]
        == "F"
    )


def test_a_marked_value_the_records_move_from_is_checked_like_any_group(tmp_path):
    # Some members hold the marker before their last version: its changes are counted.
    rows = _cards({1: 60, 2: 60, 3: 40}, mark_last=True)
    for row in rows:
        if 120 <= row["member_id"] < 160 and row["tier"] == "basic":
            row["tier"] = "closed"
    marked = _contract(lifecycle={"endMarker": {"column": "tier", "value": "closed"}})
    transitions = _table(_profile(tmp_path, marked, rows))["history"]["columns"]["tier"][
        "transitions"
    ]
    group = _group(transitions, "closed")
    assert group["status"] != "F" or _shown(group, "levelCounts") != [0, 0, 0]


# -- first starts beside the start and end columns ----------------------------------------------


def test_first_starts_are_published_on_the_years_that_hold_them(tmp_path):
    result = _profile(tmp_path, _contract(), _cards({1: 100, 2: 100, 3: 100, 4: 60}))
    entry = _table(result)["history"]["firstStart"]
    assert _group(entry)["status"] == "F"
    edges = [int(each) for each in entry["bins"]]
    assert edges[0] == _ms("2019-01-01") and len(edges) > 2
    loaded, _ = load_profile_text(result.canonical.decode("utf-8"))
    assert loaded.tables[TABLE].history is not None


def _rank(rows: List[List[Fraction]]) -> Tuple[List[List[Fraction]], List[int]]:
    """Rows in reduced echelon form, and their pivots."""
    reduced: List[List[Fraction]] = []
    pivots: List[int] = []
    for row in rows:
        row = _reduce(row, reduced, pivots)
        lead = next((at for at, value in enumerate(row) if value), None)
        if lead is None:
            continue
        row = [value / row[lead] for value in row]
        for other in reduced:
            if other[lead]:
                factor = other[lead]
                other[:] = [a - factor * b for a, b in zip(other, row, strict=True)]
        reduced.append(row)
        pivots.append(lead)
    return reduced, pivots


def _reduce(
    row: List[Fraction], reduced: List[List[Fraction]], pivots: List[int]
) -> List[Fraction]:
    row = list(row)
    for basis, lead in zip(reduced, pivots, strict=True):
        if row[lead]:
            factor = row[lead]
            row = [a - factor * b for a, b in zip(row, basis, strict=True)]
    return row


def _column_edges(entry: Dict[str, Any]) -> Tuple[set, Optional[int], Optional[int]]:
    edges: set = set()
    for statistic in entry.get("statistics") or ():
        bins = [int(each) for each in statistic.get("bins") or ()]
        for group in statistic["groups"]:
            if group["status"] != "F":
                continue
            if statistic["definition"] == "quantile/2":
                edges |= {bins[group["quantileBin"]], bins[group["quantileBin"] + 1]}
            else:
                statuses = group.get("cellStatus", ["F"] * (len(bins) - 1))
                for at, status in enumerate(statuses):
                    if status == "F":
                        edges |= {bins[at], bins[at + 1]}
    metrics = entry["metrics"]
    low = int(metrics["min"]["value"]) if metrics["min"]["status"] == "F" else None
    high = int(metrics["max"]["value"]) if metrics["max"]["status"] == "F" else None
    return edges, low, high


def _small_cells_the_first_starts_open(result: Any, rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Every small cell a reader forms with the first starts and cannot form without them.

    The series are the start column S, the end column N, the first starts F, the later versions'
    starts L = S - F and the closed last ends C = N - S + F.
    """
    table = _table(result)
    first = table["history"]["firstStart"]
    f_edges = [int(each) for each in first["bins"]]
    if f_edges == history._year_ladder():
        return []  # the public ladder: its outer edges say nothing of the records
    published = _group(first)["status"] == "F"
    statuses = _group(first).get("cellStatus", ["F" if published else "C"] * (len(f_edges) - 1))
    s_edges, s_low, s_high = _column_edges(table["columns"]["valid_from"])
    n_edges, n_low, n_high = _column_edges(table["columns"]["valid_to"])
    places: List[Optional[int]] = [None, *sorted(s_edges | n_edges | set(f_edges)), None]
    starts = [_ms(r["valid_from"]) for r in rows if r["valid_from"]]
    ends = [_ms(r["valid_to"]) for r in rows if r["valid_to"]]
    firsts: Dict[Any, int] = {}
    for r in rows:
        if r["member_id"] is not None:
            at = _ms(r["valid_from"])
            firsts[r["member_id"]] = min(firsts.get(r["member_id"], at), at)

    def below(values: Sequence[int], at: int) -> int:
        edge = places[at]
        if edge is None:
            return 0 if at == 0 else len(values)
        return sum(1 for value in values if value < edge)

    width = len(places)
    value = {
        "S": [below(starts, at) for at in range(width)],
        "N": [below(ends, at) for at in range(width)],
        "F": [below(list(firsts.values()), at) for at in range(width)],
    }
    value["L"] = [s - f for s, f in zip(value["S"], value["F"], strict=True)]
    value["C"] = [n - s + f for n, s, f in zip(value["N"], value["S"], value["F"], strict=True)]
    offset = {"S": 0, "N": width, "F": 2 * width}

    def unit(name: str, at: int) -> List[Fraction]:
        row = [Fraction(0)] * (3 * width)
        if name == "C":
            row[offset["N"] + at] += 1
            row[offset["S"] + at] -= 1
            row[offset["F"] + at] += 1
        elif name == "L":
            row[offset["S"] + at] += 1
            row[offset["F"] + at] -= 1
        else:
            row[offset[name] + at] += 1
        return row

    def knows(edges: set, low: Optional[int], high: Optional[int], at: int) -> bool:
        edge = places[at]
        return (
            edge is None
            or edge in edges
            or (low is not None and edge <= low)
            or (high is not None and edge > high)
        )

    base: List[List[Fraction]] = []
    if not any(value["C"]):  # no closed last version ends: the chain gives C everywhere
        base.extend(unit("C", at) for at in range(width))
    versions = table["history"]["versionsPerEntity"]
    shown = [int(each) for each in versions["bins"]]
    if not any(value["L"]) and _group(versions)["status"] == "F" and shown[-1] <= 2:
        base.extend(unit("L", at) for at in range(width))  # every entity shows one version
    for at in range(width):
        if knows(s_edges, s_low, s_high, at):
            base.append(unit("S", at))
            if value["S"][at] in (0, value["S"][-1]):  # a first start is a start
                base.append(unit("F", at))
        if knows(n_edges, n_low, n_high, at):
            base.append(unit("N", at))
        if at in (0, width - 1):
            base.append(unit("F", at))
    with_first = list(base)
    for at in range(width):
        edge = places[at]
        if edge is None or edge <= f_edges[0] or edge >= f_edges[-1]:
            with_first.append(unit("F", at))
    # the first starts' own cells, each a fact between two of its edges
    for cell, status in enumerate(statuses):
        if status == "F":
            low, high = places.index(f_edges[cell]), places.index(f_edges[cell + 1])
            row = unit("F", high)
            row = [a - b for a, b in zip(row, unit("F", low), strict=True)]
            with_first.append(row)
    known, pivots = _rank(with_first)
    plain, plain_pivots = _rank(base)
    found: List[str] = []
    for name in "SNFLC":
        for low in range(width):
            for high in range(low + 1, width):
                target = [a - b for a, b in zip(unit(name, high), unit(name, low), strict=True)]
                if any(_reduce(target, known, pivots)):
                    continue  # not formed
                if not any(_reduce(target, plain, plain_pivots)):
                    continue  # formed without the first starts
                lo_edge, hi_edge = places[low], places[high]
                if name == "F" and (
                    (hi_edge is not None and hi_edge <= f_edges[0])
                    or (lo_edge is not None and lo_edge >= f_edges[-1])
                    or (lo_edge in f_edges and hi_edge in f_edges)
                ):
                    continue  # their own cells and the trim's zeros, which the check saw
                count = value[name][high] - value[name][low]
                if count < 0 or not disclosure.check_count(count, RULE).published:
                    found.append(f"{name}[{lo_edge}, {hi_edge}) = {count}")
    return found


def test_no_small_cell_can_be_formed_with_the_first_starts_beside_the_columns(tmp_path):
    # A quarter of the members have closed their card, in every year the first starts cover.
    rows = _cards({1: 100, 2: 100, 3: 100, 4: 60}, closed_every=4)
    result = _profile(tmp_path, _contract(), rows)
    assert _group(_table(result)["history"]["firstStart"])["status"] == "F"
    assert _small_cells_the_first_starts_open(result, rows) == []


@pytest.mark.parametrize("closed", [0, 3, 12, 25, -4, -9])
def test_no_small_cell_is_formed_against_the_strongest_reader_of_the_columns(
    tmp_path, monkeypatch, closed
):
    # A reader told every year edge of the start and end columns: the first starts withhold the
    # cells beside each edge that would set a small cell of closed last ends apart.
    years = frozenset(_ms(f"{year}-01-01") for year in range(2018, 2030))
    monkeypatch.setattr(
        history, "_published", lambda context, name: history._Published(years, None, None)
    )
    spread = {"closed_every": -closed} if closed < 0 else {"closed_last": closed}
    rows = _cards({1: 100, 2: 100, 3: 100, 4: 60}, spread=5, **spread)
    result = _profile(tmp_path, _contract(), rows)
    table = _table(result)

    def everything(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "statistics": [
                {
                    "definition": "marginalHistogram/1",
                    "bins": [str(each) for each in sorted(years)],
                    "groups": [{"group": "", "status": "F"}],
                }
            ],
            "metrics": {"min": {"status": "C"}, "max": {"status": "C"}},
        }

    strongest = {
        **table,
        "columns": {
            **table["columns"],
            "valid_from": everything(table["columns"]["valid_from"]),
            "valid_to": everything(table["columns"]["valid_to"]),
        },
    }

    class Shown:
        document = {"tables": {TABLE: strongest}}

    assert _small_cells_the_first_starts_open(Shown(), rows) == []
    load_profile_text(result.canonical.decode("utf-8"))


def test_the_versions_and_the_first_starts_sum_to_one_published_count_of_members(tmp_path):
    # Across members the totals agree, so no member gives another's withheld total back.
    result = _profile(tmp_path, _contract(), _cards({1: 100, 2: 100, 3: 100, 4: 60}))
    published = _table(result)["history"]
    assert _group(published["firstStart"])["k"] == _group(published["versionsPerEntity"])["k"]


def test_first_starts_that_would_show_years_with_no_closed_card_are_withheld(tmp_path):
    # Every closed card is a first-year member's: with the columns' yearly quantiles, first
    # starts by year would show the later years hold no closed last version, a zero cell.
    rows = _cards({1: 100, 2: 100, 3: 100, 4: 60}, closed_last=40)
    result = _profile(tmp_path, _contract(), rows)
    first = _table(result)["history"]["firstStart"]
    assert _group(first)["status"] != "F" or "C" in _group(first).get("cellStatus", ())
    assert _small_cells_the_first_starts_open(result, rows) == []


def test_first_starts_set_apart_only_by_their_outer_edge_are_withheld_whole(tmp_path):
    # Closed cards stop before the last year of first starts: the year after them would hold
    # few closed last ends, set apart by the records' own top edge, which no cell can hide.
    rows = _cards({1: 100, 2: 100, 3: 100, 4: 60}, closed_every=4, closed_before=300)
    result = _profile(tmp_path, _contract(), rows)
    first = _table(result)["history"]["firstStart"]
    assert [int(each) for each in first["bins"]] == history._year_ladder()
    assert _group(first)["status"] == "C"
    assert _small_cells_the_first_starts_open(result, rows) == []


def _yearly_cohorts(later: Dict[int, int]) -> List[Dict[str, Any]]:
    """Fifty members join in each of 2019 to 2022; *later* of them renew in that year's cohort."""
    rows: List[Dict[str, Any]] = []
    number = 0
    for year, closed in ((2019, 20), (2020, 15), (2021, 15), (2022, 15)):
        for i in range(50):
            begins = date(year, 1, 1) + timedelta(days=(i * 3) % 150)
            if i < closed:
                rows.append(_row(number, begins.isoformat(), date(year, 12, 20).isoformat()))
            elif i < closed + later[year]:
                renewed = begins + timedelta(days=60 + i)
                rows.append(_row(number, begins.isoformat(), renewed.isoformat()))
                rows.append(_row(number, renewed.isoformat(), None))
            else:
                rows.append(_row(number, begins.isoformat(), None))
            number += 1
    return rows


@pytest.mark.parametrize("renewed_in_2021", [3, 0])
def test_a_small_cell_of_later_versions_starts_is_not_formed_from_the_first_starts(
    tmp_path, renewed_in_2021
):
    # Fifty first starts in each year; later versions start 20, 20, N and 20 times in them. The
    # start column's published year cells less the first starts' give the later versions' starts
    # in a year: a few (or none) in 2021, a cell no table may publish or let a reader form.
    rows = _yearly_cohorts({2019: 20, 2020: 20, 2021: renewed_in_2021, 2022: 20})
    result = _profile(tmp_path, _contract(), rows)
    table = _table(result)
    start_edges, _, _ = _column_edges(table["columns"]["valid_from"])
    assert _ms("2021-01-01") in start_edges and _ms("2022-01-01") in start_edges
    first = table["history"]["firstStart"]
    assert _group(first)["status"] != "F" or "C" in _group(first).get("cellStatus", ())
    if _group(first)["status"] == "F":  # the first starts of 2021 are not published beside it
        at = [int(each) for each in first["bins"]].index(_ms("2021-01-01"))
        assert _group(first)["cellStatus"][at] != "F"
    assert _small_cells_the_first_starts_open(result, rows) == []


def test_the_later_versions_starts_are_a_series_the_first_starts_link():
    # S = F + L at an edge: the start column's and the first starts' counts there give L's cells.
    places: List[Optional[int]] = [None, 10, 20, None]
    series = {
        "S": [0, 40, 70, 100],
        "N": [0, 30, 50, 90],
        "F": [0, 35, 60, 80],
    }
    series["L"] = [s - f for s, f in zip(series["S"], series["F"], strict=True)]
    series["C"] = [n - s + f for n, s, f in zip(series["N"], series["S"], series["F"], strict=True)]
    known = {
        "S": [True, True, True, True],
        "N": [True, True, True, True],
        "F": [True, True, True, True],
        "L": [True, False, False, True],
        "C": [True, False, False, True],
    }
    formed = history._formed_cells(places, series, known)
    assert formed[("L", 0, 1)] == 5 and formed[("L", 1, 2)] == 5 and formed[("L", 2, 3)] == 10
    # N and L at an edge give C there, as N = L + C
    assert [formed[("C", at, at + 1)] for at in range(3)] == [25, 15, 30]
    fewer = history._formed_cells(places, series, {**known, "F": [True, False, False, True]})
    assert ("L", 0, 1) not in fewer and ("L", 0, 3) in fewer


def test_first_starts_stay_published_where_every_entity_has_one_version(tmp_path, monkeypatch):
    # With one version each, no later version starts: L is 0 at every edge, which the versions per
    # entity already show, so a reader forms no cell of it that the first starts reveal.
    rows = _cards({1: 300}, closed_every=3)
    result = _profile(tmp_path, _contract(), rows)
    table = _table(result)
    assert table["history"]["versionsPerEntity"]["bins"] == ["1", "2"]
    first = _group(table["history"]["firstStart"])
    assert first["status"] == "F" and set(first.get("cellStatus", "F")) == {"F"}
    assert _small_cells_the_first_starts_open(result, rows) == []
    # were the one version each not known, L's zeros would withhold some of the first starts
    monkeypatch.setattr(history, "_shows_one_version_each", lambda entry: False)
    guarded = _group(_table(_profile(tmp_path, _contract(), rows))["history"]["firstStart"])
    assert guarded["status"] != "F" or "C" in guarded.get("cellStatus", ())


def test_rows_with_no_entity_make_a_negative_link_and_withhold_the_first_starts(tmp_path):
    # Rows that start and have no entity are starts no first start or later version counts, so
    # N - S + F goes negative. A negative count is never a cell a reader may form: it is treated
    # as small, and the first starts beside its edge are withheld.
    rows = _cards({1: 100, 2: 100, 3: 100, 4: 60})
    plain = _group(_table(_profile(tmp_path, _contract(), rows))["history"]["firstStart"])
    assert plain["status"] == "F" and set(plain.get("cellStatus", "F")) == {"F"}
    for k in range(90):
        day = (date(2020, 1, 1) + timedelta(days=k * 3)).isoformat()
        rows.append(_row(None, day, None))  # type: ignore[arg-type]
    result = _profile(tmp_path, _contract(), rows)
    first = _group(_table(result)["history"]["firstStart"])
    assert first["status"] != "F" or "C" in first.get("cellStatus", ())
    assert _small_cells_the_first_starts_open(result, rows) == []


def _versions_decision(statuses: Sequence[str]) -> Any:
    from fluid_build.profiler._stats import EdgeDecision

    return EdgeDecision(
        group_status={"": "F"},
        cell_status={"": tuple(statuses)},
        group_reason={"": ""},
        cell_reason={"": tuple("" if each == "F" else "small" for each in statuses)},
        rounded={},
    )


def test_the_audit_withholds_beside_a_cell_the_versions_pin():
    # With too few versions for any entity to hold twenty, the withheld top cell is pinned at
    # zero: the audit withholds the published cell of the highest lower edge beside it.
    backend = load_backend()
    edges = [1, 2, 3, 5, 10, 20, disclosure.MAX_COUNT]
    group = backend.count_cells([20, 50, 12, 15, 10, 0], edges, 16)
    decided = _versions_decision(["F", "C", "F", "F", "F", "C"])
    audited = history._audited(decided, group, 332, RULE, edges)
    assert audited.cell_status[""] == ("F", "C", "F", "F", "C", "C")
    assert history._audited(decided, group, 360, RULE, edges).cell_status[""] == (
        decided.cell_status[""]
    )


def test_the_audit_reads_a_trimmed_top_edge():
    # More versions than every entity at the top of its bin holds: on a trimmed top edge no
    # table holds, so the audit withholds; on the ladder's open top any table may.
    backend = load_backend()
    group = backend.count_cells([20, 50, 12, 15, 10], [1, 2, 3, 5, 10, 20], 16)
    decided = _versions_decision(["F", "F", "F", "C", "F"])
    trimmed = history._audited(decided, group, 494, RULE, [1, 2, 3, 5, 10, 20])
    assert trimmed.cell_status[""] != decided.cell_status[""]
    group = backend.count_cells([20, 50, 12, 15, 10], [1, 2, 3, 5, 10, disclosure.MAX_COUNT], 16)
    edges = [1, 2, 3, 5, 10, disclosure.MAX_COUNT]
    assert history._audited(decided, group, 494, RULE, edges).cell_status[""] == (
        decided.cell_status[""]
    )


def test_versions_per_entity_goes_through_the_audit(tmp_path, monkeypatch):
    seen: List[Sequence[int]] = []

    def audit(decision: Any, group: Any, versions: int, rule: Any, edges: Sequence[int]) -> Any:
        seen.append(list(edges))
        return _versions_decision(["C"] * len(group.cells))

    monkeypatch.setattr(history, "_audited", audit)
    result = _profile(tmp_path, _contract(), _cards({1: 120, 2: 90, 3: 60, 5: 40}))
    entry = _table(result)["history"]["versionsPerEntity"]
    assert seen == [[1, 2, 3, 5, 10]]
    group = _group(entry)
    assert group["status"] == "C" or set(group["cellStatus"]) == {"C"}


def test_a_coded_member_with_no_group_published_is_written_on_the_whole_ladder():
    written: List[Any] = []

    class Context:
        def withheld(self, definition: str, shape: Dict[str, Any], keys: Sequence[str]) -> Any:
            written.append(shape["bins"])
            return {"groups": [{"group": "", "status": "C"}]}

    from fluid_build.profiler._stats import EdgeDecision

    withheld = EdgeDecision(
        group_status={"": "C"}, cell_status={}, group_reason={"": "small"}, cell_reason={},
        rounded={},
    )  # fmt: skip
    coded = history._Coded([3, 5], object(), object(), withheld)  # type: ignore[arg-type]
    history._written(Context(), coded, [0, 1, 3, 5, 9])  # type: ignore[arg-type]
    assert written == [["0", "1", "3", "5", "9"]]


def test_a_column_s_published_quantiles_place_its_records_at_their_bin_s_edges():
    class Stage:
        tables_doc = {
            TABLE: {
                "columns": {
                    "valid_from": {
                        "statistics": [
                            {
                                "definition": "quantile/2",
                                "bins": ["0", "10", "20", "30"],
                                "groups": [{"group": "", "status": "F", "quantileBin": 1}],
                            },
                            {
                                "definition": "quantile/2",
                                "bins": ["0", "10", "20", "30"],
                                "groups": [{"group": "", "status": "C"}],
                            },
                        ],
                        "metrics": {"min": {"status": "F", "value": "5"}, "max": {"status": "C"}},
                    }
                }
            }
        }

    class Context:
        stage = Stage()
        expose_id = TABLE

    found = history._published(Context(), "valid_from")  # type: ignore[arg-type]
    assert found.edges == frozenset({10, 20})
    assert found.knows(5) and found.knows(10) and not found.knows(30) and found.knows(None)


def _row(member: int, start: str, end: Optional[str]) -> Dict[str, Any]:
    return {
        "member_id": member,
        "valid_from": start,
        "valid_to": end,
        "tier": "basic",
        "is_current": "Y" if end is None else "N",
    }


def test_coded_ends_reach_the_bounds_the_start_column_publishes(tmp_path):
    # Three hundred members join from 2019 and renew a year or so later; three renew only in
    # 2025, beside nine who join that year, so the start column publishes its extremes. The long
    # renewals and the nine newcomers are too few for end bins of their own: they are coded to
    # the ladder edge past the published extremes, not to the ladder's far end.
    rows: List[Dict[str, Any]] = []
    for member in range(300):
        joined = date(2019, 1, 1) + timedelta(days=member)
        renewed = (joined + timedelta(days=400)).isoformat()
        rows += [_row(member, joined.isoformat(), renewed), _row(member, renewed, None)]
    for member in range(300, 303):
        rows += [_row(member, "2019-01-05", "2025-03-01"), _row(member, "2025-03-01", None)]
    rows += [_row(member, "2025-03-01", None) for member in range(303, 312)]
    table = _table(_profile(tmp_path, _contract(), rows))
    metrics = table["columns"]["valid_from"]["metrics"]
    assert metrics["min"]["status"] == "F" and metrics["max"]["status"] == "F"
    lengths = [int(each) for each in table["history"]["versionLength"]["bins"]]
    assert lengths[-1] == 3653
    firsts = [int(each) for each in table["history"]["firstStart"]["bins"]]
    assert firsts[-1] == _ms("2026-01-01")


def test_one_entity_in_a_small_table_does_not_place_the_top_edge(tmp_path):
    edges = [
        _table(_profile(tmp_path, _contract(), _cards({1: 9, n: 1})))["history"][
            "versionsPerEntity"
        ]["bins"][-1]
        for n in (2, 7)
    ]
    assert edges == [str(disclosure.MAX_COUNT)] * 2
