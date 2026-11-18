# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --bundle`` over the real statistic registry, ``flux-stats``.

A Simulation declares the properties its fidelity check compares; with ``--bundle``
the profile carries the statistic each one is compared with. These tests run the
whole path, from a bundle directory and an NDJSON slice to the document, and check
that:

* flux-stats' own profile reader reads the document back, and finds for each
  declared property the statistic the engine's matcher looks for: the same
  definition, ``groupBy``, ``bins``, ``levels`` and ``level``;
* each disclosure rule fires where the records meet it: a small cell, a rare
  support member, a small group (and the secondary suppression it brings), a
  quantile witness too few records hold;
* a record the declaration refuses stops the profile, naming no value.

The module needs a flux-stats that carries ``flux_stats.fidelity``.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

pytest.importorskip("flux_stats.fidelity")
from flux_stats.fidelity import expose_from_contract, parse_fidelity_block  # noqa: E402
from flux_stats.profile import load_profile_text  # noqa: E402

from fluid_build.cli import main  # noqa: E402
from fluid_build.profiler import _stats, bundle, disclosure, document  # noqa: E402
from fluid_build.profiler._stats import DeclaredStatistic  # noqa: E402
from fluid_build.profiler.runner import ProfileError, profile_expose  # noqa: E402
from tests.profiler.test_flux_stats_backend import _slice  # noqa: E402

pytestmark = pytest.mark.unit

EXPOSE = "payment"
CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "shop.payment",
    "name": "Payment",
    "exposes": [
        {
            "exposeId": EXPOSE,
            "kind": "table",
            "binding": {"platform": "local", "format": "csv", "location": {"path": "p.csv"}},
            "contract": {
                "schema": [
                    {"name": "amount", "type": "decimal(10,2)"},
                    {"name": "market", "type": "string"},
                    {"name": "qty", "type": "integer"},
                ]
            },
        }
    ],
}

_ABSOLUTE = {"type": "absolute", "value": "5"}
_DISTANCE = {"type": "distance", "measure": "totalVariation", "max": "0.1"}
PROPERTIES: List[Dict[str, Any]] = [
    {"property": "mean", "column": "amount", "tolerance": _ABSOLUTE},
    {"property": "mean", "column": "amount", "groupBy": "market", "tolerance": _ABSOLUTE},
    {
        "property": "marginalHistogram",
        "column": "amount",
        "bins": ["0", "50", "100"],
        "tolerance": _DISTANCE,
    },
    {"property": "levelHistogram", "column": "market", "tolerance": _DISTANCE},
    {"property": "support", "column": "market", "tolerance": {"type": "count", "max": 0}},
    {"property": "quantile", "column": "amount", "level": "0.5", "tolerance": _ABSOLUTE},
    {
        "property": "correlation",
        "columns": ["amount", "qty"],
        "minVariance": "0.01",
        "tolerance": {"type": "absolute", "value": "0.1"},
    },
]


def _block(
    properties: List[Dict[str, Any]], bits: Optional[int] = None, market_null: str = "fail"
) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "records": {"min": 4, "max": 100000},
        "columns": {
            "amount": {"range": {"min": "0", "max": "100"}, "null": "exclude"},
            "qty": {"range": {"min": "0", "max": "100"}, "null": "exclude"},
            "market": {
                "support": {"values": ["IE", "GB", "FR", "DE"]},
                "maxCardinality": 8,
                "null": market_null,
            },
        },
        "properties": properties,
        "budget": {"maxChecksPerState": 20},
    }
    if bits is not None:
        block["encoding"] = {"fractionalBits": bits, "rounding": "floor"}
    return block


def _bundle(
    tmp_path: Path,
    properties: List[Dict[str, Any]],
    bits: Optional[int] = None,
    market_null: str = "fail",
) -> Path:
    directory = tmp_path / "payment-bundle"
    directory.mkdir()
    simulation = {
        "fluxVersion": "0.5.0",
        "kind": "Simulation",
        "id": "sim-payment",
        "extensions": {
            "com.agentics.flux/1": {
                "emits": {EXPOSE: {"fidelity": _block(properties, bits, market_null)}}
            }
        },
    }
    (directory / "simulation.flux.yml").write_text(yaml.safe_dump(simulation), encoding="utf-8")
    (directory / "payment.fluid.yml").write_text(yaml.safe_dump(CONTRACT), encoding="utf-8")
    return directory


AMOUNTS = ["0", "20", "40", "60", "80"]
MARKETS = ["IE", "GB", "FR"]


def _rows(count: int = 120) -> List[Dict[str, Any]]:
    return [
        {"amount": AMOUNTS[n % 5], "market": MARKETS[n % 3], "qty": str(n % 7)}
        for n in range(count)
    ]


def _profile(
    tmp_path: Path, rows: List[Dict[str, Any]], properties=None, bits=None, market_null="fail"
):
    chosen = properties if properties is not None else PROPERTIES
    directory = _bundle(tmp_path, chosen, bits, market_null)
    block = bundle.read_bundle_block(directory, EXPOSE, _stats.load_backend())
    return profile_expose(
        CONTRACT,
        EXPOSE,
        source=_slice(tmp_path, rows),
        backend=_stats.load_backend(),
        cwd=tmp_path,
        bundle=block,
    )


def _entries(result, column: str) -> List[Dict[str, Any]]:
    return result.document["tables"][EXPOSE]["columns"][column]["statistics"]


def _entry(result, column: str, definition: str, group_by: Optional[str] = None) -> Dict[str, Any]:
    (found,) = [
        entry
        for entry in _entries(result, column)
        if entry["definition"] == definition and entry.get("groupBy") == group_by
    ]
    return found


def test_the_document_answers_every_declared_property(tmp_path):
    result = _profile(tmp_path, _rows())
    assert result.fractional_bits == 16
    assert [reason for _, reason in result.left_out] == [
        "a correlation's moments need the exact record count, which the output checks round"
    ]
    assert result.left_out[0][0] == "correlation of 'amount' and 'qty'"
    assert not [
        g
        for e in _entries(result, "amount")
        if e["definition"] != "quantile/1"
        for g in e["groups"]
        if g["status"] != "F"
    ]

    amount = _entries(result, "amount")
    assert [(e["definition"], e.get("groupBy")) for e in amount] == [
        ("mean/1", None),
        ("mean/1", "market"),
        ("marginalHistogram/1", None),
        ("quantile/1", None),
    ]
    ungrouped = _entry(result, "amount", "mean/1")
    assert ungrouped["groups"] == [
        {"group": "", "k": 120, "Sx": str(4800 * 2**16 // 1), "status": "F"}
    ]
    by_market = _entry(result, "amount", "mean/1", "market")
    assert [(g["group"], g["k"], g["status"]) for g in by_market["groups"]] == [
        ("FR", 40, "F"),
        ("GB", 40, "F"),
        ("IE", 40, "F"),
    ]
    histogram = _entry(result, "amount", "marginalHistogram/1")
    assert histogram["bins"] == ["0", "50", "100"]
    assert histogram["groups"] == [{"group": "", "k": 120, "binCounts": [70, 50], "status": "F"}]
    # The quantile beside the histogram of its column is withheld (FLUX ADR 0012): its
    # ranks count at its witness, an edge the histogram's bins do not have.
    quantile = _entry(result, "amount", "quantile/1")
    assert quantile["level"] == "0.5"
    assert quantile["groups"] == [{"group": "", "status": "C"}]

    levels = _entry(result, "market", "levelHistogram/1")
    assert levels["levels"] == ["DE", "FR", "GB", "IE"]
    (level_group,) = levels["groups"]
    # DE is declared and no record holds it: a zero cell is withheld, and with the column's
    # count known, the three others would give it back, so FR is withheld with it (a formed
    # zero is protected). A histogram's withheld cell is written C, whatever withheld it;
    # the result keeps why: DE is small, FR is secondary.
    assert level_group["levelCounts"] == [None, None, 40, 40]
    assert level_group["cellStatus"] == ["C", "C", "F", "F"]
    assert result.withheld_cells == {"A": 1, "D": 1}
    assert _entry(result, "market", "support/1")["groups"][0]["support"] == ["FR", "GB", "IE"]


def test_the_engines_matcher_finds_each_declared_property(tmp_path):
    """What ``verifier._reference`` compares: definition, groupBy, bins, levels and level."""
    result = _profile(tmp_path, _rows())
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    expose = expose_from_contract(CONTRACT, EXPOSE)
    parsed = parse_fidelity_block(_block(PROPERTIES), expose=expose)
    from flux_stats.reading import level_cells
    from flux_stats.registry import definition_for_kind

    answered = 0
    for declaration in parsed.properties:
        if declaration.kind.value == "correlation":
            continue
        levels = (
            level_cells(parsed.columns[declaration.columns[0]])
            if declaration.kind.value == "levelHistogram"
            else ()
        )
        found = [
            entry
            for entry in profile.column_statistics(EXPOSE, declaration.columns[0])
            if entry.identifier == definition_for_kind(declaration.kind).identifier
            and entry.group_by == declaration.group_by
            and entry.bins == declaration.bins
            and entry.levels == levels
            and entry.level == declaration.level
        ]
        assert len(found) == 1, declaration.property_id
        answered += 1
    assert answered == 6


def test_a_group_the_rules_publish_reads_back_whole(tmp_path):
    result = _profile(tmp_path, _rows())
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    histogram = next(
        entry
        for entry in profile.column_statistics(EXPOSE, "amount")
        if entry.identifier == "marginalHistogram/1"
    )
    assert histogram.bins == (Fraction(0), Fraction(50), Fraction(100))
    (group,) = histogram.groups
    assert group.published and group.statistic.bin_counts == (70, 50)


def test_a_rare_support_member_withholds_the_group_and_siblings_of_a_grouped_mean(tmp_path):
    rows = _rows()
    rows[0]["market"] = "DE"  # one record: a group of one, and a member one record holds
    result = _profile(tmp_path, rows)

    support = _entry(result, "market", "support/1")
    assert support["groups"] == [{"group": "", "status": "A"}]

    by_market = _entry(result, "amount", "mean/1", "market")
    assert [(g["group"], g["status"]) for g in by_market["groups"]] == [
        ("DE", "A"),
        ("FR", "D"),
        ("GB", "D"),
        ("IE", "D"),
    ]
    assert all(set(g) == {"group", "status"} for g in by_market["groups"])
    # The ungrouped mean needs no secondary suppression.
    assert _entry(result, "amount", "mean/1")["groups"][0]["status"] == "F"
    # Three siblings of the mean, and the quantile beside the histogram of its column.
    assert result.withheld["D"] == 4
    assert ("amount", "mean/1", "market", None) not in result.statistics
    assert ("amount", "mean/1", None, None) in result.statistics
    assert ("market", "support/1", None, None) in result.withheld_statistics
    assert ("amount", "mean/1", "market", None) in result.withheld_statistics
    assert ("amount", "mean/1", None, None) not in result.withheld_statistics
    # The histogram's DE cell is a one: withheld by its cell, the group is published.
    (group,) = _entry(result, "market", "levelHistogram/1")["groups"]
    assert group["cellStatus"][0] == "C" and group["levelCounts"][0] is None
    load_profile_text(result.canonical.decode("utf-8"))


_ONE_CODE = (
    "The document writes C for every withheld group and cell of a histogram or a quantile, "
    "whatever withheld it, so its code says nothing of its size."
)


@pytest.mark.parametrize(
    ("low", "high", "level", "status"),
    [
        # 5 records of 1 below 105 of 50: the 6th of 120 is a 50 that 105 records hold,
        # and its published ranks would say that 5 records lie below it.
        pytest.param(5, 10, "0.05", "C", id="five-below"),
        pytest.param(10, 5, "0.95", "C", id="five-above"),
        pytest.param(10, 10, "0.1", "F", id="ten-below"),
        pytest.param(10, 10, "0.9", "F", id="ten-above"),
        # None below: no record lies under the witness, a zero ACRO's default withholds.
        pytest.param(0, 10, "0.5", "C", id="none-below"),
    ],
)
def test_a_quantile_needs_ten_records_below_and_above_its_witness(
    tmp_path, low, high, level, status
):
    amounts = ["1"] * low + ["50"] * (120 - low - high) + ["99"] * high
    rows = [
        {"amount": amount, "market": MARKETS[n % 3], "qty": "1"} for n, amount in enumerate(amounts)
    ]
    result = _profile(tmp_path, rows, [{**PROPERTIES[5], "level": level}])
    (group,) = _entry(result, "amount", "quantile/1")["groups"]
    assert group["status"] == status
    if status == "F":
        assert group["quantileWitness"] == str(50 * 2**16)
    else:
        assert group == {"group": "", "status": "C"}  # withheld for a small cell, written C


def test_a_quantile_alone_on_its_column_is_published(tmp_path):
    result = _profile(tmp_path, _rows(), [PROPERTIES[5]])
    (group,) = _entry(result, "amount", "quantile/1")["groups"]
    assert group["k"] == 120 and group["status"] == "F"
    assert group["quantileWitness"] == str(40 * 2**16)
    assert group["quantileBelow"] == 50 and group["quantileAtOrBelow"] == 70


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param([PROPERTIES[2], PROPERTIES[5]], id="beside-a-histogram"),
        pytest.param([PROPERTIES[5], {**PROPERTIES[5], "level": "0.25"}], id="beside-a-level"),
        pytest.param(
            [PROPERTIES[5], {**PROPERTIES[5], "groupBy": "market"}], id="beside-a-groupby"
        ),
    ],
)
def test_a_quantile_beside_another_count_at_its_columns_edges_is_withheld(tmp_path, properties):
    result = _profile(tmp_path, _rows(), properties)
    quantiles = [e for e in _entries(result, "amount") if e["definition"] == "quantile/1"]
    assert quantiles and all(g["status"] == "C" for e in quantiles for g in e["groups"])
    histograms = [e for e in _entries(result, "amount") if e["definition"] == "marginalHistogram/1"]
    assert all(g["status"] == "F" for e in histograms for g in e["groups"])
    # The ungrouped median passes its own checks, so it is counted as secondary
    # suppression: withheld so that another count cannot be given back.
    assert result.withheld["D"] >= 1


def test_a_group_withheld_for_its_size_withholds_its_siblings_in_a_grouped_histogram(tmp_path):
    rows = _rows()
    for row in rows[:3]:
        row["market"] = "DE"  # a group of three records: its size is the count less the rest
    by_market = {**PROPERTIES[2], "groupBy": "market"}
    result = _profile(tmp_path, rows, [PROPERTIES[2], by_market])
    grouped = _entry(result, "amount", "marginalHistogram/1", "market")
    assert [(g["group"], g["status"]) for g in grouped["groups"]] == [
        ("DE", "C"),
        ("FR", "C"),
        ("GB", "C"),
        ("IE", "C"),
    ]
    assert result.withheld["D"] == 3
    # The column's own histogram is no group of the groupBy.
    assert _entry(result, "amount", "marginalHistogram/1")["groups"][0]["status"] == "F"


def _three_bands(market: str, count: int) -> List[Dict[str, Any]]:
    # A third of the records each at 10, 50 and 90: the median is a 50 that a third
    # hold, with a third below it and a third above it.
    return [
        {"amount": ("10", "50", "90")[n % 3], "market": market, "qty": "1"} for n in range(count)
    ]


def test_a_group_withheld_for_its_size_withholds_its_siblings_in_a_grouped_quantile(tmp_path):
    rows = [*_three_bands("DE", 3)] + [
        row for market in MARKETS for row in _three_bands(market, 39)
    ]
    result = _profile(tmp_path, rows, [{**PROPERTIES[5], "groupBy": "market"}])
    grouped = _entry(result, "amount", "quantile/1", "market")
    # Every other market's median passes its own checks, and goes with the small one.
    assert {g["status"] for g in grouped["groups"]} == {"C"}
    assert result.withheld["D"] == 3


@pytest.mark.parametrize(
    "grouped",
    [
        pytest.param({**PROPERTIES[2], "groupBy": "market"}, id="marginalHistogram"),
        pytest.param(
            {**PROPERTIES[5], "groupBy": "market", "bins": ["0", "50", "100"]}, id="quantile-2"
        ),
    ],
)
def test_a_small_group_withholds_every_group_of_its_groupby_in_each_statistic(tmp_path, grouped):
    rows = _rows()
    for row in rows[:3]:
        row["market"] = "DE"  # a group of three records
    result = _profile(tmp_path, rows, [grouped])
    (entry,) = [e for e in _entries(result, "amount") if e.get("groupBy") == "market"]
    assert {g["status"] for g in entry["groups"]} == {"C"}
    assert all(set(g) == {"group", "status"} for g in entry["groups"])


def _row(amount: str, market: str = "IE") -> Dict[str, Any]:
    return {"amount": amount, "market": market, "qty": "1"}


def _histogram(bins: List[str], column: str = "amount", **more: Any) -> Dict[str, Any]:
    return {
        "property": "marginalHistogram",
        "column": column,
        "bins": bins,
        "tolerance": _DISTANCE,
        **more,
    }


def _histograms(result, column: str = "amount") -> List[Dict[str, Any]]:
    return [e for e in _entries(result, column) if e["definition"] == "marginalHistogram/1"]


def test_a_histogram_and_a_coarser_one_of_a_column_give_no_withheld_cell_back(tmp_path):
    # Fine cells of 30, 5, 40 and 45 records: the 5 is small, and the coarse cell of 35 beside
    # the 30 would give it back by subtraction. Neither histogram is checked alone.
    rows = [_row("10")] * 30 + [_row("30")] * 5 + [_row("60")] * 40 + [_row("80")] * 45
    fine = _histogram(["0", "25", "50", "75", "100"])
    coarse = _histogram(["0", "50", "100"])
    result = _profile(tmp_path, rows, [fine, coarse])
    fine_group, coarse_group = (histogram["groups"][0] for histogram in _histograms(result))
    fine_cells, coarse_cells = fine_group["binCounts"], coarse_group["binCounts"]
    assert fine_cells[1] is None  # the 5 itself
    for cell, inside in ((0, fine_cells[:2]), (1, fine_cells[2:])):
        if coarse_cells[cell] is not None:
            # A published coarse cell with exactly one withheld cell inside it gives that cell back.
            assert sum(count is None for count in inside) != 1
    published = [count for count in fine_cells + coarse_cells if count is not None]
    assert 5 not in published and 35 not in published
    withheld = [count is None for count in fine_cells]
    assert sum(withheld) >= 2  # a single withheld cell is the total less the published ones
    assert result.withheld_cells["D"] >= 1  # more than the small cell itself was withheld
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_level_histogram_with_one_small_level_withholds_a_neighbour(tmp_path):
    # Levels DE, FR, GB, IE hold 5, 25, 40 and 50 records. The 5 is small, and the column's
    # count less the three published levels is exactly that 5: another level goes with it.
    rows = (
        [_row("10", "IE")] * 50
        + [_row("10", "GB")] * 40
        + [_row("10", "FR")] * 25
        + [_row("10", "DE")] * 5
    )
    result = _profile(tmp_path, rows, [PROPERTIES[3]])
    (group,) = _entry(result, "market", "levelHistogram/1")["groups"]
    assert group["cellStatus"][0] == "C"  # DE, the small level
    assert group["cellStatus"].count("C") == 2
    assert group["levelCounts"][0] is None and None in group["levelCounts"][1:]
    assert result.withheld_cells == {"A": 1, "D": 1}


def test_a_formed_zero_is_protected_under_the_default_rule(tmp_path):
    # Cells of 12, 0 and 12 records: the zero is withheld (ACRO's default holds zeros
    # disclosive), and the column's 24 less the two published 12s would give it back.
    rows = [_row("5")] * 12 + [_row("25")] * 12
    result = _profile(tmp_path, rows, [_histogram(["0", "10", "20", "100"])])
    (group,) = _histograms(result)[0]["groups"]
    assert group["cellStatus"][1] == "C" and group["binCounts"][1] is None
    assert group["cellStatus"].count("C") == 2
    assert group["binCounts"][0] == 10 or group["binCounts"][2] == 10  # the 12 left, rounded
    assert result.withheld_cells == {"A": 1, "D": 1}


def test_a_formed_zero_is_published_where_the_rule_holds_zeros_safe(tmp_path):
    from dataclasses import replace

    rule = replace(disclosure.ACRO_DEFAULT, name="zeros-safe", zeros_disclosive=False)
    directory = _bundle(tmp_path, [_histogram(["0", "10", "20", "100"])])
    block = bundle.read_bundle_block(directory, EXPOSE, _stats.load_backend())
    rows = [_row("5")] * 12 + [_row("25")] * 12
    result = profile_expose(
        CONTRACT,
        EXPOSE,
        source=_slice(tmp_path, rows),
        backend=_stats.load_backend(),
        cwd=tmp_path,
        bundle=block,
        rule=rule,
    )
    (group,) = _histograms(result)[0]["groups"]
    assert group["binCounts"] == [10, 0, 10] and "cellStatus" not in group


def test_a_quantile_with_bins_is_written_as_quantile_2(tmp_path):
    binned = {**PROPERTIES[5], "bins": ["0", "50", "100"]}
    result = _profile(tmp_path, _rows(), [binned])
    entry = _entry(result, "amount", "quantile/2")
    assert entry["level"] == "0.5" and entry["bins"] == ["0", "50", "100"]
    # 120 records, the median is the 60th, a 40: in the first bin, with 72 records through it
    # (rounded to 70), none below it, and 48 above it.
    assert entry["groups"] == [
        {"group": "", "k": 120, "quantileBin": 0, "binBelow": 0, "binThrough": 70, "status": "F"}
    ]
    assert "quantileWitness" not in json.dumps(entry)
    assert result.document["_schemaURL"] == document.SCHEMA_URL_1_1_0
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    (read,) = [
        e for e in profile.column_statistics(EXPOSE, "amount") if e.identifier == "quantile/2"
    ]
    assert read.bins == (Fraction(0), Fraction(50), Fraction(100))
    assert read.level == Fraction(1, 2)
    (group,) = read.groups
    assert group.published and group.statistic.quantile_bin == 0
    assert (group.statistic.bin_below, group.statistic.bin_through) == (0, 70)
    assert result.statistics.count(
        ("amount", "quantile/2", None, "at level 0.5 over bins 0, 50, 100")
    )


def test_the_engines_matcher_finds_a_quantile_with_bins(tmp_path):
    binned = {**PROPERTIES[5], "bins": ["0", "50", "100"]}
    result = _profile(tmp_path, _rows(), [binned])
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    expose = expose_from_contract(CONTRACT, EXPOSE)
    parsed = parse_fidelity_block(_block([binned]), expose=expose)
    from flux_stats.fidelity import definition_for_property

    (declaration,) = parsed.properties
    found = [
        entry
        for entry in profile.column_statistics(EXPOSE, "amount")
        if entry.identifier == definition_for_property(declaration).identifier
        and entry.bins == declaration.bins
        and entry.level == declaration.level
    ]
    assert len(found) == 1


def test_a_profile_without_a_quantile_with_bins_keeps_format_1_0_0(tmp_path):
    result = _profile(tmp_path, _rows())
    assert result.document["_schemaURL"] == document.SCHEMA_URL
    assert "quantile/2" not in result.canonical.decode("utf-8")


def test_a_quantile_with_bins_beside_a_histogram_on_its_partition_is_published(tmp_path):
    # The bins of the quantile are the histogram's: one partition, so it is not withheld
    # for standing beside another count at the column's edges, as a quantile/1 is.
    binned = {**PROPERTIES[5], "bins": ["0", "50", "100"]}
    result = _profile(tmp_path, _rows(), [PROPERTIES[2], binned])
    (group,) = _entry(result, "amount", "quantile/2")["groups"]
    assert group["status"] == "F"
    assert _histograms(result)[0]["groups"][0]["status"] == "F"


def test_rows_no_group_holds_are_one_more_group_the_check_counts_and_never_writes(tmp_path):
    rows = _rows()
    for row in rows[:15]:
        row["market"] = None  # fifteen records in no group, and in the column's count
    by_market = {**PROPERTIES[2], "groupBy": "market"}
    result = _profile(tmp_path, rows, [by_market], market_null="exclude")
    grouped = _entry(result, "amount", "marginalHistogram/1", "market")
    assert [g["group"] for g in grouped["groups"]] == ["FR", "GB", "IE"]
    assert {g["status"] for g in grouped["groups"]} == {"F"}
    assert b"\\u0000" not in result.canonical and b"\x00" not in result.canonical


def test_a_refusal_of_the_counts_by_the_output_check_stops_the_profile_naming_no_value(
    tmp_path, monkeypatch
):
    def refuse(self, statistics, **_kwargs):
        raise ValueError("the groups of 'market' do not add up to the column's total")

    monkeypatch.setattr(_stats._FluxStatsBackend, "check_edge_counts", refuse)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, _rows(), [PROPERTIES[2]])
    assert refused.value.event == "profile_edge_counts_refused"
    assert "column 'amount'" in str(refused.value)


def test_a_quantile_beside_a_histogram_of_another_column_is_published(tmp_path):
    histogram_of_qty = {**PROPERTIES[2], "column": "qty"}
    result = _profile(tmp_path, _rows(), [histogram_of_qty, PROPERTIES[5]])
    (group,) = _entry(result, "amount", "quantile/1")["groups"]
    assert group["status"] == "F"


def test_a_small_group_of_one_column_leaves_another_columns_groups_published(tmp_path):
    rows = _rows()
    for index, row in enumerate(rows[:15]):
        row["market"] = "DE"
        if index < 12:
            row["amount"] = None  # DE holds 15 records, 3 of them with an amount
    by_market = [
        {**PROPERTIES[2], "groupBy": "market"},
        {**PROPERTIES[2], "column": "qty", "groupBy": "market"},
    ]
    result = _profile(tmp_path, rows, by_market)
    amount = _entry(result, "amount", "marginalHistogram/1", "market")
    assert {g["status"] for g in amount["groups"]} == {"C"}
    qty = _entry(result, "qty", "marginalHistogram/1", "market")
    assert {g["status"] for g in qty["groups"]} == {"F"}


def test_rows_no_group_holds_that_the_block_refuses_withhold_a_grouped_histogram(tmp_path):
    # One row with no market holds an amount outside the declared range: the profile
    # cannot read it as the block does, so it cannot vouch for it.
    rows = _rows()
    rows[6]["market"] = None
    rows[6]["amount"] = "500"
    by_market = {**PROPERTIES[2], "groupBy": "market"}
    result = _profile(tmp_path, rows, [by_market], market_null="exclude")
    grouped = _entry(result, "amount", "marginalHistogram/1", "market")
    assert {g["status"] for g in grouped["groups"]} == {"C"}
    assert result.withheld["D"] == len(grouped["groups"])


def test_few_rows_no_group_holds_withhold_a_grouped_histogram(tmp_path):
    # Three rows with no market are in no group, and still the column's count less the groups'.
    by_market = {**PROPERTIES[2], "groupBy": "market"}
    result = _profile(tmp_path, _null_market_rows(3), [by_market], market_null="exclude")
    grouped = _entry(result, "amount", "marginalHistogram/1", "market")
    assert {g["status"] for g in grouped["groups"]} == {"C"}
    assert result.withheld["D"] == len(grouped["groups"])


def test_a_quantile_whose_witness_too_few_records_hold_is_withheld(tmp_path):
    rows = [{"amount": f"{n % 100}.5", "market": MARKETS[n % 3], "qty": "1"} for n in range(120)]
    result = _profile(tmp_path, rows, [PROPERTIES[5]])
    (group,) = _entry(result, "amount", "quantile/1")["groups"]
    # Every amount repeats at most twice: the witness is held by two records. Written C,
    # as every withheld quantile is, and counted as a small count.
    assert group == {"group": "", "status": "C"}
    assert result.withheld["A"] >= 1


def _null_market_rows(count: int) -> List[Dict[str, Any]]:
    rows = _rows()
    for row in rows[:count]:
        row["market"] = None
    return rows


def test_a_record_no_group_holds_withholds_the_groups_of_a_grouped_mean(tmp_path):
    # Under null: exclude a null key leaves the record out of every group, while the
    # column's ungrouped mean and sum still hold it: the groups would give it back.
    rows = _rows()
    rows[6]["market"] = None  # one record, amount 20
    result = _profile(tmp_path, rows, PROPERTIES[:2], market_null="exclude")
    by_market = _entry(result, "amount", "mean/1", "market")
    assert [(g["group"], g["status"]) for g in by_market["groups"]] == [
        ("FR", "D"),
        ("GB", "D"),
        ("IE", "D"),
    ]
    assert result.withheld["D"] == 3
    assert _entry(result, "amount", "mean/1")["groups"][0]["status"] == "F"


def test_records_no_group_holds_that_pass_the_checks_leave_the_groups_published(tmp_path):
    result = _profile(tmp_path, _null_market_rows(15), PROPERTIES[:2], market_null="exclude")
    by_market = _entry(result, "amount", "mean/1", "market")
    assert {g["status"] for g in by_market["groups"]} == {"F"}


def test_records_no_group_holds_that_the_block_refuses_withhold_the_groups(tmp_path):
    # Only the grouped mean reads the amount as the block declares it, so the row in no
    # group is never refused; the profile cannot vouch for it, and withholds the groups.
    rows = _rows()
    rows[6]["market"] = None
    rows[6]["amount"] = "500"  # outside the declared range of 0 to 100
    result = _profile(tmp_path, rows, [PROPERTIES[1]], market_null="exclude")
    by_market = _entry(result, "amount", "mean/1", "market")
    assert {g["status"] for g in by_market["groups"]} == {"D"}


def test_a_null_key_on_a_null_value_hides_nothing(tmp_path):
    rows = _rows()
    rows[6]["market"] = None
    rows[6]["amount"] = None
    result = _profile(tmp_path, rows, PROPERTIES[:2], market_null="exclude")
    by_market = _entry(result, "amount", "mean/1", "market")
    assert {g["status"] for g in by_market["groups"]} == {"F"}


def test_two_quantiles_of_one_column_are_named_apart_by_level(tmp_path):
    quantiles = [{**PROPERTIES[5], "level": "0.5"}, {**PROPERTIES[5], "level": "0.25"}]
    result = _profile(tmp_path, _rows(), quantiles)
    named = result.statistics + result.withheld_statistics
    shapes = {each[3] for each in named if each[1] == "quantile/1"}
    assert shapes == {"at level 0.5", "at level 0.25"}


def test_a_zero_cell_is_withheld_with_the_cell_that_gives_it_back(tmp_path):
    rows = [{"amount": "10", "market": "IE", "qty": "1"} for _ in range(120)]
    properties = [
        {
            "property": "marginalHistogram",
            "column": "amount",
            "bins": ["0", "5", "100"],
            "tolerance": _DISTANCE,
        }
    ]
    result = _profile(tmp_path, rows, properties)
    (group,) = _entry(result, "amount", "marginalHistogram/1")["groups"]
    # The zero cell is withheld, and the 120 beside it would give it back: it goes too.
    assert group["binCounts"] == [None, None] and group["cellStatus"] == ["C", "C"]
    assert result.withheld_cells == {"A": 1, "D": 1}


def test_a_small_table_withholds_the_histogram_whole(tmp_path):
    rows = [{"amount": "10", "market": "IE", "qty": "1"} for _ in range(6)]
    result = _profile(tmp_path, rows, [PROPERTIES[2], PROPERTIES[3]])
    # Withheld for a small count, and written C: the status says nothing of the size.
    assert _entry(result, "amount", "marginalHistogram/1")["groups"] == [
        {"group": "", "status": "C"}
    ]
    assert _entry(result, "market", "levelHistogram/1")["groups"] == [{"group": "", "status": "C"}]
    assert result.withheld["A"] >= 2


def test_a_declared_mean_replaces_the_fixed_one_and_the_block_sets_the_grid(tmp_path):
    result = _profile(tmp_path, _rows(), [PROPERTIES[0]], bits=8)
    assert result.fractional_bits == 8
    assert result.document["encoding"]["fractionalBits"] == 8
    (entry,) = _entries(result, "amount")  # one mean/1, not the fixed one and the declared one
    assert entry["groups"][0]["Sx"] == str(4800 * 2**8)
    # The qty column is a number too, and keeps its fixed mean.
    assert [e["definition"] for e in _entries(result, "qty")] == ["mean/1"]


def test_a_null_under_null_fail_is_refused_naming_no_value(tmp_path):
    rows = _rows()
    rows[7]["market"] = None
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, rows)
    assert refused.value.event == "profile_bundle_record_refused"
    assert "FLX-5005" in str(refused.value) and "null is refused" in str(refused.value)


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param([PROPERTIES[3]], id="level-histogram"),
        # A group key is published, so every grouped property refuses a key the block
        # does not declare: one read from the records would publish a record's value.
        pytest.param([PROPERTIES[1]], id="mean-by-market"),
        pytest.param([{**PROPERTIES[2], "groupBy": "market"}], id="histogram-by-market"),
        pytest.param([{**PROPERTIES[5], "groupBy": "market"}], id="quantile-by-market"),
    ],
)
def test_a_value_outside_the_declared_support_is_refused(tmp_path, properties):
    rows = _rows()
    rows[7]["market"] = "ZZ-secret-market"
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, rows, properties)
    assert refused.value.event == "profile_bundle_record_refused"
    assert "outside the declared support" in str(refused.value)
    assert "ZZ-secret-market" not in str(refused.value)


def test_a_support_member_outside_the_declaration_is_withheld_with_its_group(tmp_path):
    # A support lists what the records hold, as the engine reads it, so an undeclared
    # member is not refused; one record holds it, so the whole support is withheld.
    rows = _rows()
    rows[7]["market"] = "ZZ-secret-market"
    result = _profile(tmp_path, rows, [PROPERTIES[4]])
    (entry,) = _entries(result, "market")
    assert entry["groups"] == [{"group": "", "status": "A"}]
    assert b"ZZ-secret-market" not in result.canonical


def test_a_value_outside_the_declared_range_is_refused(tmp_path):
    rows = _rows()
    rows[3]["amount"] = "5000"
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, rows, [PROPERTIES[0]])
    assert "outside the declared range" in str(refused.value)
    assert "5000" not in str(refused.value)


def test_a_block_the_grammar_refuses_is_refused_before_any_record_is_read(tmp_path):
    properties = [{"property": "mean", "column": "nope", "tolerance": _ABSOLUTE}]
    directory = _bundle(tmp_path, properties)
    block = bundle.read_bundle_block(directory, EXPOSE, _stats.load_backend())
    with pytest.raises(ProfileError) as refused:
        profile_expose(
            CONTRACT,
            EXPOSE,
            source=str(tmp_path / "missing.ndjson"),  # never read
            backend=_stats.load_backend(),
            cwd=tmp_path,
            bundle=block,
        )
    assert refused.value.event == "profile_bundle_block_refused"


def test_a_declaration_over_the_wrong_domain_is_refused(tmp_path):
    contract = json.loads(json.dumps(CONTRACT))
    contract["exposes"][0]["contract"]["schema"][1]["type"] = "integer"  # market as a number
    directory = _bundle(tmp_path, [PROPERTIES[4]])
    block = bundle.read_bundle_block(directory, EXPOSE, _stats.load_backend())
    with pytest.raises(ProfileError) as refused:
        profile_expose(
            contract,
            EXPOSE,
            source=_slice(tmp_path, _rows()),
            backend=_stats.load_backend(),
            cwd=tmp_path,
            bundle=block,
        )
    assert refused.value.event == "profile_bundle_column_domain"


def test_a_declared_column_the_contract_types_unreadably_is_refused(tmp_path):
    contract = json.loads(json.dumps(CONTRACT))
    contract["exposes"][0]["contract"]["schema"][0]["type"] = "double"
    directory = _bundle(tmp_path, [PROPERTIES[0]])
    block = bundle.read_bundle_block(directory, EXPOSE, _stats.load_backend())
    with pytest.raises(ProfileError) as refused:
        profile_expose(
            contract,
            EXPOSE,
            source=_slice(tmp_path, _rows()),
            backend=_stats.load_backend(),
            cwd=tmp_path,
            bundle=block,
        )
    assert refused.value.event == "profile_bundle_column_not_profiled"


def test_the_seam_deduplicates_identical_properties(tmp_path):
    directory = _bundle(tmp_path, [PROPERTIES[0], PROPERTIES[0], PROPERTIES[6]])
    block = bundle.read_bundle_block(directory, EXPOSE, _stats.load_backend())
    declared = _stats.load_backend().read_block(block.raw, CONTRACT, EXPOSE)
    assert [s.description for s in declared.statistics] == ["mean/1 of 'amount'"]
    assert declared.columns == ("amount",)
    assert [name for name, _ in declared.left_out] == ["correlation of 'amount' and 'qty'"]


def test_the_command_writes_the_document_and_names_what_it_answers(tmp_path, monkeypatch, capsys):
    directory = _bundle(tmp_path, PROPERTIES)
    monkeypatch.chdir(tmp_path)
    source = _slice(tmp_path, _rows())
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            source,
            "--bundle",
            str(directory),
            "--out",
            "payment.profile.json",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out
    assert "mean/1 of 'amount'" in out and "mean/1 of 'amount' by 'market'" in out
    assert "levelHistogram/1 of 'market'" in out and "quantile/1 of 'amount'" in out
    assert "Not answered: correlation of 'amount' and 'qty':" in out
    assert "a run that checks it is refused (FLX-5037)." in out
    # The DE level holds no record: its cell is withheld, written C, and counted by its check,
    # and FR goes with it so that the count cannot be given back.
    assert (
        "Withheld histogram cells, by the check (CL_CONF_STATUS code): "
        "A (small count) 1, D (secondary suppression) 1." in out
    )
    assert _ONE_CODE in out
    written = (tmp_path / "payment.profile.json").read_bytes()
    load_profile_text(written.decode("utf-8"))


def test_a_grid_that_differs_from_the_blocks_is_refused_before_any_record_is_read(
    tmp_path, monkeypatch, capsys
):
    directory = _bundle(tmp_path, PROPERTIES, bits=8)
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            "does-not-exist.ndjson",
            "--bundle",
            str(directory),
            "--fractional-bits",
            "16",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 1
    assert "profile_fractional_bits_conflict" in out
    assert not (tmp_path / "payment.profile.json").exists()
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            _slice(tmp_path, _rows()),
            "--bundle",
            str(directory),
            "--fractional-bits",
            "8",
        ]
    )
    assert rc == 0


def test_a_flux_stats_without_the_grammar_refuses_before_reading(tmp_path, monkeypatch, capsys):
    import sys

    directory = _bundle(tmp_path, PROPERTIES)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "flux_stats.fidelity", None)
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            "does-not-exist.ndjson",
            "--bundle",
            str(directory),
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 1
    assert "profile_statistics_unavailable" in out
    assert "reads the fidelity block with flux-stats, and the installed flux-stats" in out
    assert "lacks flux_stats.fidelity. It needs flux-stats[yaml]>=0.7.0,<0.8" in out
    assert "next: pip install 'data-product-forge[profile]'" in out


def test_a_block_written_with_bare_null_keys_and_bare_floats_profiles_as_the_quoted_one(tmp_path):
    (tmp_path / "quoted").mkdir()
    (tmp_path / "bare").mkdir()
    quoted = _profile(tmp_path / "quoted", _rows(), [PROPERTIES[0], PROPERTIES[5]])
    directory = _bundle(tmp_path / "bare", [PROPERTIES[0], PROPERTIES[5]])
    path = directory / "simulation.flux.yml"
    text = path.read_text(encoding="utf-8")
    assert "'null':" in text and "level: '0.5'" in text
    path.write_text(text.replace("'null':", "null:").replace("level: '0.5'", "level: 0.5"))
    block = bundle.read_bundle_block(directory, EXPOSE, _stats.load_backend())
    assert block.raw["columns"]["market"]["null"] == "fail"
    bare = profile_expose(
        CONTRACT,
        EXPOSE,
        source=_slice(tmp_path / "bare", _rows()),
        backend=_stats.load_backend(),
        cwd=tmp_path / "bare",
        bundle=block,
    )
    assert bare.digest == quoted.digest


def test_the_report_names_the_statistics_written_with_every_group_withheld(
    tmp_path, monkeypatch, capsys
):
    rows = _rows()
    rows[0]["market"] = "DE"  # a support member one record holds: support and grouped mean A/D
    directory = _bundle(tmp_path, PROPERTIES)
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            _slice(tmp_path, rows),
            "--bundle",
            str(directory),
            "--out",
            "payment.profile.json",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out
    whole = "Withheld whole, so a run that checks them scores them censored: "
    assert whole in out
    line = out.split(whole)[1].split(". ")[0]
    assert "support/1 of 'market'" in line and "mean/1 of 'amount' by 'market'" in line
    assert "mean/1 of 'amount';" not in line and not line.endswith("mean/1 of 'amount'")
    # A statistic with a published group stays on the published line only.
    published = out.split("Fidelity statistics: ")[1].split(", on the fidelity block's")[0]
    assert "support/1" not in published
    assert "on the fidelity block's fractionalBits 16." in out  # the grid is the block's


def _partly_rows() -> List[Dict[str, Any]]:
    rows = _rows()
    for row in rows[:12]:
        # Twelve records in one group, 8 and 4 in the two bins: the cell that holds the
        # median is small, so that group is withheld, and its size is not, so the other
        # groups are published.
        row["market"] = "DE"
    return rows


_PARTLY_PROPERTIES = [
    {
        "property": "quantile",
        "column": "amount",
        "groupBy": "market",
        "level": "0.5",
        "bins": ["0", "50", "100"],
        "tolerance": _ABSOLUTE,
    }
]


def test_a_statistic_with_some_groups_withheld_is_named_with_its_counts(tmp_path):
    result = _profile(tmp_path, _partly_rows(), _PARTLY_PROPERTIES)
    statuses = [g["status"] for g in _entry(result, "amount", "quantile/2", "market")["groups"]]
    published = statuses.count("F")
    assert 0 < published < len(statuses)
    assert set(statuses) == {"F", "C"}
    named = ("amount", "quantile/2", "market", "at level 0.5 over bins 0, 50, 100")
    assert result.partly_withheld_statistics == ((*named, published, len(statuses)),)
    assert named not in result.statistics
    assert named not in result.withheld_statistics


def test_the_report_names_the_statistics_withheld_in_part(tmp_path, monkeypatch, capsys):
    directory = _bundle(tmp_path, _PARTLY_PROPERTIES)
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            _slice(tmp_path, _partly_rows()),
            "--bundle",
            str(directory),
            "--out",
            "payment.profile.json",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out
    assert (
        "Partly withheld, so a run that checks them scores only the published groups: "
        "quantile/2 of 'amount' at level 0.5 over bins 0, 50, 100 by 'market' "
        "(3 of 4 groups)." in out
    )
    assert _ONE_CODE in out and "Withheld histogram cells" not in out


def test_a_histogram_withheld_whole_is_named_in_the_report(tmp_path, monkeypatch, capsys):
    # Six records: the histogram's one group is withheld for its size, so no cell is
    # counted, and the report still says the document writes C for it.
    rows = [{"amount": "10", "market": "IE", "qty": "1"} for _ in range(6)]
    directory = _bundle(tmp_path, [PROPERTIES[2]])
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            _slice(tmp_path, rows),
            "--bundle",
            str(directory),
            "--out",
            "payment.profile.json",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out
    assert "marginalHistogram/1 of 'amount' over bins 0, 50, 100" in out.split("Withheld whole")[1]
    assert _ONE_CODE in out and "Withheld histogram cells" not in out
    written = json.loads((tmp_path / "payment.profile.json").read_bytes())
    (histogram,) = [
        entry
        for entry in written["tables"][EXPOSE]["columns"]["amount"]["statistics"]
        if entry["definition"] == "marginalHistogram/1"
    ]
    assert histogram["groups"] == [{"group": "", "status": "C"}]


def test_a_report_with_no_withheld_histogram_value_says_nothing_of_c(tmp_path, monkeypatch, capsys):
    rows = [{"amount": AMOUNTS[n % 5], "market": MARKETS[n % 3], "qty": "1"} for n in range(120)]
    directory = _bundle(tmp_path, [PROPERTIES[2]])
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "profile",
            str(directory / "payment.fluid.yml"),
            "--source",
            _slice(tmp_path, rows),
            "--bundle",
            str(directory),
            "--out",
            "payment.profile.json",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out
    assert _ONE_CODE not in out and "Withheld histogram cells" not in out
