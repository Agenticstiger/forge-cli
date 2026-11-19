# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The statistics a profile carries without ``--bundle``, declared from the contract alone."""

from __future__ import annotations

import json
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
    bins = [*(str(edge) for edge in range(0, 101, 10)), "100.5"]  # 100 alone in the last bin
    assert _one(result, "score", "marginalHistogram/1")["bins"] == bins
    # The histogram alone: checked beside quantiles on the same bins, the two let a reader
    # who knows the check tell worlds apart (tests/profiler/test_disclosure_oracle.py).
    definitions = [s["definition"] for s in _statistics(result, "score")]
    assert definitions == ["marginalHistogram/1"]
    (group,) = _one(result, "score", "marginalHistogram/1")["groups"]
    # 0 to 96 are held 4 times each and 97 to 100 three times: every bin of ten publishes,
    # and 100, alone in the last bin with 3 records, is withheld with the bin beside it.
    assert group["status"] == "F"
    assert group["binCounts"][:9] == [40] * 9
    assert group["binCounts"][9:] == [None, None]
    assert group["cellStatus"][9:] == ["C", "C"]
    # Its sum, distinct count and quantiles are withheld, and it has no mean/1: each would
    # give a withheld bin back from the published ones.
    metrics = result.document["tables"]["t"]["columns"]["score"]["metrics"]
    for name in ("sum", "distinctCount", "quantiles"):
        assert metrics[name]["status"] == "D" and "value" not in metrics[name]
        assert "values" not in metrics[name]
    assert metrics["count"]["status"] == "F"


@pytest.mark.parametrize("bits", [16, 0])  # at 0 the last bin, [5, 6], holds 6 too
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
        (1, 5, [1, 2, 3, 4, 5, Fraction(11, 2)]),  # one bin per value, 5 not beside 4
        (0, 100, [*range(0, 101, 10), Fraction(201, 2)]),
        (-3, 3, [-3, -2, -1, 0, 1, 2, 3, Fraction(7, 2)]),
    ],
)
def test_an_integral_range_gets_whole_bins_and_half_a_unit_past_its_top(low, high, edges):
    got = defaults.range_bins(Fraction(low), Fraction(high), 16, integral=True)
    assert list(got) == [Fraction(edge) for edge in edges]


def test_on_a_grid_of_whole_steps_an_integral_range_ends_a_whole_unit_past_its_top():
    # Half a unit is off that grid; a whole one keeps 5 out of 4's bin.
    got = defaults.range_bins(Fraction(1), Fraction(5), 0, integral=True)
    assert list(got) == [Fraction(edge) for edge in (1, 2, 3, 4, 5, 6)]


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
