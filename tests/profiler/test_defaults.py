# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The statistics a profile carries without ``--bundle``, declared from the contract alone."""

from __future__ import annotations

import json
import tracemalloc
from fractions import Fraction
from pathlib import Path

import pytest

from fluid_build.profiler import defaults
from fluid_build.profiler.runner import ProfileError, profile_expose
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.fidelity")


def _contract(columns, dimensions=()):
    expose = {
        "exposeId": "t",
        "contract": {"schema": [{"name": n, "type": t, **extra} for n, t, extra in columns]},
    }
    if dimensions:
        expose["semantics"] = {
            "dimensions": [{"name": name, "type": "categorical"} for name in dimensions]
        }
    return {"fluidVersion": "0.7.7", "kind": "DataProduct", "id": "p", "exposes": [expose]}


def _profile(tmp_path: Path, contract, rows, **options):
    path = tmp_path / "t.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return profile_expose(contract, "t", source=str(path), backend=FakeBackend(), **options)


def _statistics(result, column):
    return result.document["tables"]["t"]["columns"][column].get("statistics", [])


def _one(result, column, definition, **shape):
    found = [
        each
        for each in _statistics(result, column)
        if each["definition"] == definition
        and all(each.get(key) == value for key, value in shape.items())
    ]
    assert len(found) == 1, (column, definition, shape, _statistics(result, column))
    return found[0]


STATUS = ("status", "enum('active', 'closed', 'suspended', 'unused')", {})


def test_every_declared_level_is_a_cell_and_one_no_record_holds_is_withheld(tmp_path):
    rows = [{"status": level} for level in ["active"] * 70 + ["closed"] * 25 + ["suspended"] * 5]
    result = _profile(tmp_path, _contract([STATUS]), rows)
    histogram = _one(result, "status", "levelHistogram/1")
    assert histogram["levels"] == ["active", "closed", "suspended", "unused"]
    (group,) = histogram["groups"]
    # 'unused' holds no record and 'suspended' 5: both are small, and so together (5); the
    # published total would form them from 'closed', so 'closed' is withheld beside them.
    assert group["levelCounts"] == [70, None, None, None]
    assert group["cellStatus"] == ["F", "C", "C", "C"]


def test_the_levels_listed_never_depend_on_the_records(tmp_path):
    some = _profile(tmp_path, _contract([STATUS]), [{"status": "active"}] * 40)
    other = _profile(tmp_path, _contract([STATUS]), [{"status": "closed"}] * 3)
    assert (
        _one(some, "status", "levelHistogram/1")["levels"]
        == _one(other, "status", "levelHistogram/1")["levels"]
    )


def test_a_level_the_type_does_not_list_refuses_the_profile_without_showing_it(tmp_path):
    rows = [{"status": "active"}] * 20 + [{"status": "secret-level"}]
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, _contract([STATUS]), rows)
    assert refused.value.event == "profile_record_outside_declared_domain"
    assert "secret-level" not in str(refused.value)
    assert "row 20" in str(refused.value)


def test_a_declared_range_gets_a_histogram_on_bins_of_one_width(tmp_path):
    score = (
        "score",
        "int",
        {"validationRules": [{"type": "range", "constraint": ">= 0 and <= 100"}]},
    )
    rows = [{"score": str(n % 101)} for n in range(400)]
    result = _profile(tmp_path, _contract([score]), rows)
    bins = [str(edge) for edge in range(0, 101, 10)]  # the last bin ends at the maximum, 100
    assert _one(result, "score", "marginalHistogram/1")["bins"] == bins
    # The histogram alone: checked beside quantiles on the same bins, the two let a reader
    # who knows the check tell worlds apart (tests/profiler/test_disclosure_oracle.py).
    definitions = [s["definition"] for s in _statistics(result, "score")]
    assert definitions == ["marginalHistogram/1"]
    (group,) = _one(result, "score", "marginalHistogram/1")["groups"]
    # 0 to 96 are held 4 times each and 97 to 100 three times: the nine bins of ten values
    # hold 40 each, and the last bin, 90 to 100 closed at the top, 7 * 4 + 4 * 3 = 40 too.
    assert group["status"] == "F"
    assert group["binCounts"] == [40] * 10
    assert "cellStatus" not in group
    # Its sum, distinct count and quantiles are withheld, and it has no mean/1: each would
    # give a withheld bin back from the published ones.
    metrics = result.document["tables"]["t"]["columns"]["score"]["metrics"]
    for name in ("sum", "distinctCount", "quantiles"):
        assert metrics[name]["status"] == "D" and "value" not in metrics[name]
        assert "values" not in metrics[name]
    assert metrics["count"]["status"] == "F"


@pytest.mark.parametrize("bits", [16, 0])  # each value is checked against the range itself
def test_a_value_outside_the_declared_range_refuses_the_profile(tmp_path, bits):
    score = (
        "score",
        "int",
        {"validationRules": [{"type": "range", "constraint": "between 1 and 5"}]},
    )
    rows = [{"score": "3"}] * 20 + [{"score": "6"}]
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, _contract([score]), rows, fractional_bits=bits)
    assert refused.value.event == "profile_record_outside_declared_domain"
    assert "'6'" not in str(refused.value)
    assert "column type" in refused.value.context["next"]


@pytest.mark.parametrize(
    "declared, bits, outside",
    [
        ("decimal(10,6)", 16, "0.099995"),  # the grid's step, 2^-16, is coarser than 10^-6
        ("decimal(10,6)", 16, "0.900005"),
        ("decimal(5,2)", 0, "0.05"),
    ],
)
def test_a_value_just_past_a_bound_is_refused_where_the_grid_is_coarser(
    tmp_path, declared, bits, outside
):
    # The bins are rounded outward to the grid, so an end bin reaches past the bound: each
    # value is checked against the declared range itself.
    rule = {"validationRules": [{"type": "range", "constraint": "between 0.1 and 0.9"}]}
    contract = _contract([("share", declared, rule)])
    inside = [{"share": value} for value in ("0.1", "0.5", "0.9")] * 10
    _profile(tmp_path, contract, inside, fractional_bits=bits)  # the bounds themselves are in
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, contract, inside + [{"share": outside}], fractional_bits=bits)
    assert refused.value.event == "profile_record_outside_declared_domain"
    assert outside not in str(refused.value) and "row 30" in str(refused.value)


def test_a_strict_bound_is_not_reached(tmp_path):
    rule = {"validationRules": [{"type": "range", "constraint": "> 0 and <= 10"}]}
    contract = _contract([("score", "int", rule)])
    rows = [{"score": str(1 + n % 10)} for n in range(40)]
    _profile(tmp_path, contract, rows)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, contract, rows + [{"score": "0"}])
    assert refused.value.event == "profile_record_outside_declared_domain"


@pytest.mark.parametrize(
    "declared, constraint",
    [
        ("int", ">= 0"),
        ("decimal(10,2)", ">= 0"),
        ("int", "<= 100"),
        ("int", "between 0 and 1" + "0" * 30),  # a top past the type's own bound
    ],
)
def test_a_range_not_closed_inside_its_type_takes_the_types_ladder(tmp_path, declared, constraint):
    # Ten bins over the rest of the type would hold every record in one of them and publish
    # none: no histogram, so the column keeps its sum, its mean/1 and quantiles.
    rule = {"validationRules": [{"type": "range", "constraint": constraint}]}
    result = _profile(
        tmp_path,
        _contract([("amount", declared, rule)]),
        [{"amount": str(n % 90)} for n in range(200)],
    )
    assert {s["definition"] for s in _statistics(result, "amount")} == {"mean/1", "quantile/2"}
    metrics = result.document["tables"]["t"]["columns"]["amount"]["metrics"]
    assert metrics["sum"]["status"] == "F"
    assert result.tables[0].uncovered == ()


@pytest.mark.parametrize("rules", [7, True, "between 0 and 5", {"type": "range"}])
def test_validation_rules_that_are_not_a_list_declare_no_range(tmp_path, rules):
    contract = _contract([("n", "int", {"validationRules": rules})])
    result = _profile(tmp_path, contract, [{"n": str(n)} for n in range(30)])
    assert {s["definition"] for s in _statistics(result, "n")} == {"mean/1", "quantile/2"}


def test_without_a_declared_range_a_number_gets_quantiles_on_its_types_ladder(tmp_path):
    result = _profile(
        tmp_path, _contract([("age", "int", {})]), [{"age": str(18 + n % 60)} for n in range(500)]
    )
    assert not [s for s in _statistics(result, "age") if s["definition"] == "marginalHistogram/1"]
    quantile = _one(result, "age", "quantile/2", level="0.5")
    assert quantile["bins"][0] == str(-(2**63)) and quantile["bins"][-1] == str(2**63 - 1)
    assert {"0", "1", "2", "5", "10", "20", "50", "100"} <= set(quantile["bins"])
    (group,) = quantile["groups"]
    # The median of 18..77 lies in [20, 50), with records below, in and above that bin.
    assert group["status"] == "F"
    assert quantile["bins"][group["quantileBin"]] == "20"


def test_the_bins_never_depend_on_the_records(tmp_path):
    contract = _contract([("age", "int", {}), ("joined", "date", {})])
    young = _profile(tmp_path, contract, [{"age": "3", "joined": "1999-05-01"}] * 40)
    old = _profile(tmp_path, contract, [{"age": "90", "joined": "2026-11-19"}] * 3)
    for column in ("age", "joined"):
        assert [s.get("bins") for s in _statistics(young, column)] == [
            s.get("bins") for s in _statistics(old, column)
        ]


def test_a_date_gets_quantiles_on_a_ladder_of_years(tmp_path):
    rows = [{"joined": f"20{10 + n % 15}-0{1 + n % 9}-1{n % 10}"} for n in range(300)]
    result = _profile(tmp_path, _contract([("joined", "date", {})]), rows)
    quantile = _one(result, "joined", "quantile/2", level="0.5")
    assert len(quantile["bins"]) == 2 + 201  # the range's two ends and 1900 to 2100
    (group,) = quantile["groups"]
    assert group["status"] == "F"


def _stamp(text: str) -> int:
    return defaults._read_instant(text)  # type: ignore[return-value]


@pytest.mark.parametrize(
    "constraint, low, high",
    [
        (">= '2024-01-01' and <= '2024-12-31'", "2024-01-01", "2024-12-31"),
        ("between '2024-01-01T06:00:00Z' and '2024-01-02T18:30:00Z'", "2024-01-01T06:00:00Z", "2024-01-02T18:30:00Z"),
        (">= '2020-02-29' and < '2030-01-01T00:00:00+02:00'", "2020-02-29", "2029-12-31T22:00:00Z"),
        (">= '2000-01-01' and <= '2001-01-01' and <= '2000-06-30'", "2000-01-01", "2000-06-30"),
    ],
)  # fmt: skip
def test_timestamp_bins_follow_the_declared_range_and_no_edge_leaves_it(constraint, low, high):
    column = {"validationRules": [{"type": "range", "constraint": constraint}]}
    assert defaults.declared_instants(column) == (_stamp(low), _stamp(high))
    bins = defaults.timestamp_bins(*defaults.declared_instants(column))
    edges = [_stamp(each) for each in bins]
    assert edges[0] == _stamp(low) and edges[-1] == _stamp(high)
    assert edges == sorted(set(edges))
    assert all(_stamp(low) <= each <= _stamp(high) for each in edges)
    assert 2 <= len(bins) <= defaults.RANGE_BINS + 1  # N + 1 edges cut N bins


@pytest.mark.parametrize(
    "low, high, expected",
    [
        (1704067200, 1704067290, [
            "2024-01-01T00:00:00Z", "2024-01-01T00:00:10Z", "2024-01-01T00:00:20Z",
            "2024-01-01T00:00:30Z", "2024-01-01T00:00:40Z", "2024-01-01T00:00:50Z",
            "2024-01-01T00:01:00Z", "2024-01-01T00:01:10Z", "2024-01-01T00:01:20Z",
            "2024-01-01T00:01:30Z",
        ]),
        (1704067200, 1704153600, [
            "2024-01-01T00:00:00Z", "2024-01-01T03:00:00Z", "2024-01-01T06:00:00Z",
            "2024-01-01T09:00:00Z", "2024-01-01T12:00:00Z", "2024-01-01T15:00:00Z",
            "2024-01-01T18:00:00Z", "2024-01-01T21:00:00Z", "2024-01-02T00:00:00Z",
        ]),
        (1704067200, 1735689600, [
            "2024-01-01T00:00:00Z", "2024-01-18T00:00:00Z", "2024-03-18T00:00:00Z",
            "2024-05-17T00:00:00Z", "2024-07-16T00:00:00Z", "2024-09-14T00:00:00Z",
            "2024-11-13T00:00:00Z", "2025-01-01T00:00:00Z",
        ]),
        (0, 1, ["1970-01-01T00:00:00Z", "1970-01-01T00:00:01Z"]),
    ],
)  # fmt: skip
def test_timestamp_bins_of_a_short_range_are_the_same_edges_as_ever(low, high, expected):
    assert list(defaults.timestamp_bins(low, high)) == expected


@pytest.mark.parametrize(
    "low, high",
    [
        ("2020-01-01", "2030-01-01"),  # a decade is 3 * 10**8 seconds
        ("2000-01-01", "2030-01-01"),
        ("0001-01-01", "9999-12-31"),  # the widest range a contract can declare
    ],
)
def test_timestamp_bins_of_a_wide_range_are_counted_not_listed(low, high):
    # Listing every second of a decade, to count the edges, takes some 12 GB: the bins are
    # worked out from the count alone, and only the edges kept are ever held.
    tracemalloc.start()
    try:
        bins = defaults.timestamp_bins(_stamp(low), _stamp(high))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000
    assert 2 <= len(bins) <= defaults.RANGE_BINS + 1
    assert bins[0] == f"{low}T00:00:00Z"
    assert [_stamp(each) for each in bins] == sorted({_stamp(each) for each in bins})


@pytest.mark.parametrize(
    "low, high, years",
    [
        ("2020-01-01", "2030-01-01", 2),
        ("2000-01-01", "2030-01-01", 5),
        ("1950-01-01", "2030-01-01", 10),
        ("1900-01-01", "2100-01-01", 50),
        ("0001-01-01", "9999-12-31", 2000),  # 1000-year rungs cut it into 11 bins
    ],
)
def test_the_year_ladder_steps_by_2_5_10_and_skips_no_rung(low, high, years):
    # Each rung is at most 2.5 times the one before, so a range the rung before cut into
    # more than RANGE_BINS bins is cut into at least four by the next one.
    bins = defaults.timestamp_bins(_stamp(low), _stamp(high))
    inner = [_stamp(each) for each in bins[1:-1]]
    steps = {b - a for a, b in zip(inner, inner[1:], strict=False)}
    assert steps == {years * defaults._YEAR_SECONDS}
    assert 4 <= len(bins) - 1 <= defaults.RANGE_BINS


@pytest.mark.parametrize(
    "text",
    [
        "0001-01-01T00:00:00Z",
        "0999-12-31T23:59:59Z",
        "1970-01-01T00:00:00Z",
        "9999-12-31T00:00:00Z",
    ],
)
def test_an_edge_is_spelled_with_a_four_digit_year_on_every_platform(text):
    assert defaults._spell(_stamp(text)) == text


@pytest.mark.parametrize(
    "text, seconds",
    [
        ("0001-01-01", -62_135_596_800),
        ("9999-12-31T23:59:59Z", 253_402_300_799),
        ("9999-12-31T09:59:59-14:00", 253_402_300_799),
        ("9999-12-31T23:59:59-14:00", None),  # 10000-01-01 in UTC
        ("0001-01-01T00:00:00+14:00", None),  # before the year 1 in UTC
    ],
)
def test_an_instant_an_offset_carries_past_the_years_1_to_9999_is_not_read(text, seconds):
    assert defaults._read_instant(text) == seconds


def test_a_range_an_offset_carries_past_the_years_keeps_the_ladder():
    constraint = "between '2024-01-01' and '9999-12-31T23:59:59-14:00'"
    column = {"validationRules": [{"type": "range", "constraint": constraint}]}
    assert defaults.declared_instants(column) is None


@pytest.mark.parametrize(
    "constraint",
    [
        ">= '2024-01-01'",  # one side
        "<= '2024-12-31'",
        ">= '2024-12-31' and <= '2024-01-01'",  # inverted
        ">= '2024-01-01T00:00:00.5Z' and <= '2024-12-31'",  # a fraction of a second
        ">= 'last year' and <= 'today'",  # not instants
        ">= 0 and <= 100",  # a number range
    ],
)
def test_a_range_that_is_not_closed_over_whole_instants_declares_none(constraint):
    column = {"validationRules": [{"type": "range", "constraint": constraint}]}
    assert defaults.declared_instants(column) is None


def test_a_timestamp_with_a_declared_range_is_binned_on_it_not_on_the_ladder(tmp_path):
    rule = {
        "validationRules": [{"type": "range", "constraint": ">= '2024-01-01' and <= '2024-12-31'"}]
    }
    contract = _contract([("placed_at", "timestamp", rule), ("joined", "date", {})])
    rows = [
        {"placed_at": f"2024-0{1 + n % 9}-1{n % 10}T10:00:00Z", "joined": "2021-03-04"}
        for n in range(300)
    ]
    result = _profile(tmp_path, contract, rows)
    low, high = _stamp("2024-01-01"), _stamp("2024-12-31")
    for level in defaults.QUANTILE_LEVELS:
        bins = _one(result, "placed_at", "quantile/2", level=level)["bins"]
        edges = [int(each) // 1000 for each in bins]  # the document spells an instant in ms
        assert edges[0] == low and edges[-1] == high
        assert all(low <= each <= high for each in edges)
        assert edges == sorted(set(edges))
        assert len(bins) <= defaults.RANGE_BINS + 1
    # a date with no declared range keeps the ladder
    assert len(_one(result, "joined", "quantile/2", level="0.5")["bins"]) == 2 + 201


def test_the_timestamp_bins_never_depend_on_the_records(tmp_path):
    rule = {
        "validationRules": [
            {"type": "range", "constraint": "between '2024-01-01' and '2024-12-31'"}
        ]
    }
    contract = _contract([("placed_at", "timestamp", rule)])
    early = _profile(tmp_path, contract, [{"placed_at": "2024-01-02T00:00:00Z"}] * 40)
    late = _profile(tmp_path, contract, [{"placed_at": "2024-12-30T00:00:00Z"}] * 3)
    assert [s.get("bins") for s in _statistics(early, "placed_at")] == [
        s.get("bins") for s in _statistics(late, "placed_at")
    ]


def test_an_unbounded_number_is_named_and_given_no_statistic(tmp_path):
    result = _profile(tmp_path, _contract([("price", "decimal", {})]), [{"price": "1.5"}] * 20)
    assert [s["definition"] for s in _statistics(result, "price")] == ["mean/1"]
    (table,) = result.tables
    assert table.uncovered == (
        ("price", "neither its type nor a validationRules range bounds it within 10^38"),
    )


@pytest.mark.parametrize(
    "declared, rules, reason",
    [
        (
            "decimal(60,0)",
            [],
            "neither its type nor a validationRules range bounds it within 10^38",
        ),
        (
            "decimal",
            [{"type": "range", "constraint": "between 0 and " + "9" * 40}],
            "wider than 10^38",
        ),
    ],
)
def test_a_number_too_wide_for_an_edge_gets_no_statistic_and_the_profile_stands(
    tmp_path, declared, rules, reason
):
    # Its edges would pass the 1,000 digits flux-stats writes out, refusing the profile.
    contract = _contract([("v", declared, {"validationRules": rules})])
    result = _profile(tmp_path, contract, [{"v": "5"}] * 12)
    assert [s["definition"] for s in _statistics(result, "v")] == ["mean/1"]
    ((name, why),) = result.tables[0].uncovered
    assert name == "v" and reason in why


def test_a_declared_grouping_lists_every_declared_level_as_a_group(tmp_path):
    region = ("region", "enum('north', 'south', 'west')", {})
    rows = [
        {"status": status, "region": region}
        for status, region in [("active", "north")] * 30 + [("closed", "south")] * 30
    ]
    result = _profile(tmp_path, _contract([STATUS, region], dimensions=["region"]), rows)
    grouped = _one(result, "status", "levelHistogram/1", groupBy="region")
    # 'west' holds no record and is listed all the same; a group of none withholds its siblings.
    assert [group["group"] for group in grouped["groups"]] == ["north", "south", "west"]
    assert {group["status"] for group in grouped["groups"]} == {"C"}
    # The grouping's own column is not grouped by itself.
    assert not [s for s in _statistics(result, "region") if s.get("groupBy") == "region"]


def test_a_full_grouping_publishes_its_groups(tmp_path):
    # Beside the column's own histogram a grouping publishes only when every cell of every
    # group does (ADR 0012), so every level of both columns is full here.
    region = ("region", "enum('north', 'south')", {})
    rows = [
        {"status": status, "region": region}
        for status, region in [("active", "north")] * 40
        + [("closed", "north")] * 20
        + [("active", "south")] * 30
        + [("closed", "south")] * 30
    ]
    status = ("status", "enum('active', 'closed')", {})
    result = _profile(tmp_path, _contract([status, region], dimensions=["region"]), rows)
    grouped = _one(result, "status", "levelHistogram/1", groupBy="region")
    assert [group["status"] for group in grouped["groups"]] == ["F", "F"]
    assert [group["levelCounts"] for group in grouped["groups"]] == [[40, 20], [30, 30]]


def test_a_grouped_statistic_bands_its_columns_non_null_records(tmp_path):
    # A record whose key is null is in no group. The band counts it all the same, as the
    # column's own figures do: the records its groups hold, beside those, would say how
    # many records hold a status and no region.
    region = ("region", "enum('north', 'south')", {})
    status = ("status", "enum('active', 'closed')", {})
    rows = [{"status": "active", "region": "north"}] * 40 + [
        {"status": "closed", "region": None}
    ] * 3
    result = _profile(tmp_path, _contract([status, region], dimensions=["region"]), rows)
    grouped = _one(result, "status", "levelHistogram/1", groupBy="region")
    ungrouped = _one(result, "status", "levelHistogram/1", groupBy=None)
    assert grouped["sampleSize"] == ungrouped["sampleSize"]
    held = sum(group["k"] for group in grouped["groups"] if group.get("k") is not None)
    assert held <= 40


def test_groupings_past_the_cell_budget_are_left_out_and_named(tmp_path):
    # 101 levels by 100 keys is 10,100 cells: checking them together costs about the cube
    # of the levels, so the grouping is left out, as the declared levels alone decide.
    values = ", ".join(f"'v{n:03d}'" for n in range(101))
    keys = ", ".join(f"'k{n:03d}'" for n in range(100))
    columns = [("value", f"enum({values})", {}), ("key", f"enum({keys})", {})]
    result = _profile(
        tmp_path, _contract(columns, dimensions=["key"]), [{"value": "v000", "key": "k000"}] * 20
    )
    assert not [s for s in _statistics(result, "value") if s.get("groupBy") == "key"]
    assert _one(result, "value", "levelHistogram/1", groupBy=None)
    ((name, why),) = result.tables[0].uncovered
    assert name == "value" and "10,000 cells" in why


def test_with_a_bundle_no_default_statistic_is_declared(tmp_path):
    block = {
        "encoding": {"fractionalBits": 16},
        "records": {"min": 1, "max": 10},
        "budget": {"maxChecksPerState": 1},
        "columns": {"status": {"support": "declared", "maxCardinality": 4}},
        "properties": [
            {"property": "support", "column": "status", "tolerance": {"type": "count", "max": 0}}
        ],
    }
    from fluid_build.profiler.bundle import BundleBlock

    result = _profile(
        tmp_path,
        _contract([STATUS, ("age", "int", {})]),
        [{"status": "active", "age": "30"}] * 20,
        bundle=BundleBlock(raw=block, simulation="s.flux.yml", expose_id="t"),
    )
    assert [s["definition"] for s in _statistics(result, "status")] == ["support/1"]
    assert [s["definition"] for s in _statistics(result, "age")] == ["mean/1"]


# -- a boolean column ---------------------------------------------------------------------

PAID = ("paid", "boolean", {})


def _profile_file(tmp_path: Path, contract, name: str, write, **options):
    path = tmp_path / name
    write(path)
    return profile_expose(contract, "t", source=str(path), backend=FakeBackend(), **options)


def _boolean_rows(trues=60, falses=40):
    return [{"paid": True}] * trues + [{"paid": False}] * falses


def _write_csv(path: Path):
    path.write_text("paid\n" + "true\n" * 60 + "false\n" * 40, encoding="utf-8")


def _write_parquet(path: Path):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT (range % 5 < 3) AS paid FROM range(100)) TO '"
        + str(path)
        + "' (FORMAT parquet)"
    )
    con.close()


def _write_ndjson(path: Path):
    path.write_text("".join(json.dumps(row) + "\n" for row in _boolean_rows()), encoding="utf-8")


@pytest.mark.parametrize(
    "name, write",
    [("t.csv", _write_csv), ("t.ndjson", _write_ndjson), ("t.parquet", _write_parquet)],
)
def test_a_boolean_column_has_its_counts_and_a_histogram_over_false_and_true(tmp_path, name, write):
    result = _profile_file(tmp_path, _contract([PAID]), name, write)
    column = result.document["tables"]["t"]["columns"]["paid"]
    assert column["domain"] == "category"
    assert column["metrics"]["count"]["value"] == 100
    assert column["metrics"]["nullCount"]["status"] == "A"  # no null: a zero is withheld
    assert "distinctCount" not in column["metrics"] or (
        column["metrics"]["distinctCount"].get("status") == "D"
    )
    histogram = _one(result, "paid", "levelHistogram/1")
    assert histogram["levels"] == ["false", "true"]
    (group,) = histogram["groups"]
    assert group["levelCounts"] == [40, 60]


def test_a_constant_boolean_column_withholds_its_empty_cell(tmp_path):
    result = _profile(tmp_path, _contract([PAID]), _boolean_rows(trues=100, falses=0))
    histogram = _one(result, "paid", "levelHistogram/1")
    assert histogram["levels"] == ["false", "true"]
    # 'false' holds no record: a cell of none is withheld, and the group with it, as for an enum.
    assert histogram["groups"] == [{"group": "", "status": "C"}]


def test_a_boolean_column_is_grouped_by_an_enum_dimension_and_groups_one(tmp_path):
    region = ("region", "enum('north', 'south')", {})
    rows = (
        [{"paid": True, "region": "north"}] * 40
        + [{"paid": False, "region": "north"}] * 20
        + [{"paid": True, "region": "south"}] * 30
        + [{"paid": False, "region": "south"}] * 30
    )
    result = _profile(tmp_path, _contract([PAID, region], dimensions=["region"]), rows)
    grouped = _one(result, "paid", "levelHistogram/1", groupBy="region")
    assert [group["levelCounts"] for group in grouped["groups"]] == [[20, 40], [30, 30]]
    # And as the dimension itself: the enum column grouped by the boolean.
    by_paid = _profile(tmp_path, _contract([PAID, region], dimensions=["paid"]), rows)
    by_flag = _one(by_paid, "region", "levelHistogram/1", groupBy="paid")
    assert [group["group"] for group in by_flag["groups"]] == ["false", "true"]
    assert [group["levelCounts"] for group in by_flag["groups"]] == [[20, 30], [40, 30]]


def test_a_boolean_arriving_as_text_reads_as_the_same_level(tmp_path):
    rows = [{"paid": "true"}] * 30 + [{"paid": False}] * 40 + [{"paid": "false"}] * 30
    result = _profile(tmp_path, _contract([PAID]), rows)
    (group,) = _one(result, "paid", "levelHistogram/1")["groups"]
    assert group["levelCounts"] == [70, 30]


@pytest.mark.parametrize("value", ["True", "yes", "TRUE", "SECRETVALUE"])
def test_a_value_outside_false_and_true_refuses_the_profile_without_showing_it(tmp_path, value):
    rows = _boolean_rows() + [{"paid": value}]
    with pytest.raises(ProfileError) as caught:
        _profile(tmp_path, _contract([PAID]), rows)
    assert caught.value.event == "profile_record_outside_declared_domain"
    assert str(value) not in str(caught.value) and "paid" in str(caught.value)


def test_a_simulation_may_declare_a_property_on_a_boolean_column(tmp_path):
    from fluid_build.profiler.bundle import BundleBlock

    block = {
        "encoding": {"fractionalBits": 16},
        "records": {"min": 1, "max": 1000},
        "budget": {"maxChecksPerState": 1},
        "columns": {"paid": {"support": {"values": ["false", "true"]}, "maxCardinality": 2}},
        "properties": [
            {
                "property": "levelHistogram",
                "column": "paid",
                "tolerance": {"type": "distance", "measure": "totalVariation", "max": "1"},
            },
            {"property": "support", "column": "paid", "tolerance": {"type": "count", "max": 0}},
        ],
    }
    result = _profile(
        tmp_path,
        _contract([PAID]),
        _boolean_rows(),
        bundle=BundleBlock(raw=block, simulation="s.flux.yml", expose_id="t"),
    )
    (group,) = _one(result, "paid", "levelHistogram/1")["groups"]
    assert group["levelCounts"] == [40, 60]
    assert _one(result, "paid", "support/1")


# -- the pure parts -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "constraint, bounds",
    [
        (">= 0 and <= 100", (Fraction(0), Fraction(100))),
        ("between -1.5 and 2.25", (Fraction(-3, 2), Fraction(9, 4))),
        ("> 0", (Fraction(0), None)),
        ("< 10", (None, Fraction(10))),
        ("about 5", (None, None)),
    ],
)
def test_a_declared_range_is_read_exactly(constraint, bounds):
    column = {"validationRules": [{"type": "range", "constraint": constraint}]}
    assert defaults.declared_range(column) == bounds


def test_several_range_rules_keep_the_tightest_bounds():
    column = {
        "validationRules": [
            {"type": "range", "constraint": ">= 0"},
            {"type": "range", "constraint": ">= 5 and <= 50"},
            {"type": "regex", "constraint": ">= 1000"},
        ]
    }
    assert defaults.declared_range(column) == (Fraction(5), Fraction(50))


@pytest.mark.parametrize(
    "low, high, bits, edges",
    [
        (0, 100, 16, [Fraction(n) for n in range(0, 101, 10)]),
        (Fraction(1, 2), Fraction(5, 2), 16, [Fraction(n, 4) for n in range(2, 11)]),
        (0, 1, 16, [Fraction(n, 8) for n in range(9)]),
        (3, 97, 16, [Fraction(3), *(Fraction(n) for n in range(10, 91, 10)), Fraction(97)]),
        (0, 1, 0, [Fraction(0), Fraction(1)]),  # no step below the grid's
    ],
)
def test_a_declared_range_is_cut_into_about_ten_bins_on_the_grid(low, high, bits, edges):
    assert list(defaults.range_bins(Fraction(low), Fraction(high), bits)) == edges


@pytest.mark.parametrize(
    "low, high, edges",
    [
        (1, 5, [1, 2, 3, 4, 5]),  # the last bin, closed at the top, holds 4 and 5
        (0, 100, [*range(0, 101, 10)]),
        (-3, 3, [-3, -2, -1, 0, 1, 2, 3]),
        (0, 7, [0, 1, 2, 3, 4, 5, 6, 7]),
    ],
)
def test_an_integral_range_gets_whole_bins_and_ends_at_its_maximum(low, high, edges):
    got = defaults.range_bins(Fraction(low), Fraction(high), 16, integral=True)
    assert list(got) == [Fraction(edge) for edge in edges]
    # The top edge is the declared maximum: flux-engine refuses bins that leave the range.
    assert got[-1] == Fraction(high)


def test_on_a_grid_of_whole_steps_an_integral_range_ends_at_its_maximum_too():
    got = defaults.range_bins(Fraction(1), Fraction(5), 0, integral=True)
    assert list(got) == [Fraction(edge) for edge in (1, 2, 3, 4, 5)]


@pytest.mark.parametrize("integral", [True, False])
@pytest.mark.parametrize("low, high", [(0, 100), (-50, 3), (1, 2), (0, 10**12)])
def test_no_edge_leaves_the_declared_range_on_the_grid(low, high, integral):
    got = defaults.range_bins(Fraction(low), Fraction(high), 16, integral=integral)
    assert got[0] == Fraction(low) and got[-1] == Fraction(high)
    assert all(a < b for a, b in zip(got, got[1:], strict=False))


def test_a_range_off_the_grid_is_rounded_outward_to_it():
    got = defaults.range_bins(Fraction("0.01"), Fraction("0.99"), 16)
    assert got[0] <= Fraction("0.01") < got[1] and got[-2] < Fraction("0.99") <= got[-1]
    assert all((edge * 2**16).denominator == 1 for edge in got)


def test_the_number_ladder_steps_1_2_5_and_halves_below_one_for_a_decimal():
    ladder = defaults.number_ladder(Fraction(-10), Fraction(10), sub_unit=True, fractional_bits=3)
    assert ladder == tuple(
        Fraction(n)
        for n in ("-10", "-5", "-2", "-1", "-1/2", "-1/4", "-1/8", "0", "1/8", "1/4", "1/2")
    ) + tuple(Fraction(n) for n in ("1", "2", "5", "10"))
