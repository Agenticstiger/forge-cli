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
from fluid_build.profiler import _stats, bundle  # noqa: E402
from fluid_build.profiler.runner import ProfileError, profile_expose  # noqa: E402

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


def _block(properties: List[Dict[str, Any]], bits: Optional[int] = None) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "records": {"min": 4, "max": 100000},
        "columns": {
            "amount": {"range": {"min": "0", "max": "100"}, "null": "exclude"},
            "qty": {"range": {"min": "0", "max": "100"}, "null": "exclude"},
            "market": {
                "support": {"values": ["IE", "GB", "FR", "DE"]},
                "maxCardinality": 8,
                "null": "fail",
            },
        },
        "properties": properties,
        "budget": {"maxChecksPerState": 20},
    }
    if bits is not None:
        block["encoding"] = {"fractionalBits": bits, "rounding": "floor"}
    return block


def _bundle(tmp_path: Path, properties: List[Dict[str, Any]], bits: Optional[int] = None) -> Path:
    directory = tmp_path / "payment-bundle"
    directory.mkdir()
    simulation = {
        "fluxVersion": "0.5.0",
        "kind": "Simulation",
        "id": "sim-payment",
        "extensions": {
            "com.agentics.flux/1": {"emits": {EXPOSE: {"fidelity": _block(properties, bits)}}}
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


def _slice(tmp_path: Path, rows: List[Dict[str, Any]]) -> str:
    path = tmp_path / "payment.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return str(path)


def _profile(tmp_path: Path, rows: List[Dict[str, Any]], properties=None, bits=None):
    directory = _bundle(tmp_path, properties if properties is not None else PROPERTIES, bits)
    block = bundle.read_bundle_block(directory, EXPOSE)
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
    assert not [g for e in _entries(result, "amount") for g in e["groups"] if g["status"] != "F"]

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
    quantile = _entry(result, "amount", "quantile/1")
    assert quantile["level"] == "0.5"
    (group,) = quantile["groups"]
    assert group["k"] == 120 and group["status"] == "F"
    assert group["quantileWitness"] == str(40 * 2**16)
    assert group["quantileBelow"] == 50 and group["quantileAtOrBelow"] == 70

    levels = _entry(result, "market", "levelHistogram/1")
    assert levels["levels"] == ["DE", "FR", "GB", "IE"]
    (level_group,) = levels["groups"]
    # DE is declared and no record holds it: a zero cell is withheld, the rest are published.
    assert level_group["levelCounts"] == [None, 40, 40, 40]
    assert level_group["cellStatus"] == ["A", "F", "F", "F"]
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
    assert result.withheld["D"] == 3
    assert ("amount", "mean/1", "market") not in result.statistics
    assert ("amount", "mean/1", None) in result.statistics
    # The histogram's DE cell is a one: withheld by its cell, the group is published.
    (group,) = _entry(result, "market", "levelHistogram/1")["groups"]
    assert group["cellStatus"][0] == "A" and group["levelCounts"][0] is None
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_quantile_whose_witness_too_few_records_hold_is_withheld(tmp_path):
    rows = [{"amount": f"{n % 100}.5", "market": MARKETS[n % 3], "qty": "1"} for n in range(120)]
    result = _profile(tmp_path, rows)
    (group,) = _entry(result, "amount", "quantile/1")["groups"]
    # Every amount repeats at most twice: the witness is held by two records.
    assert group == {"group": "", "status": "A"}


def test_a_zero_cell_is_withheld_and_the_group_published(tmp_path):
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
    assert group["binCounts"] == [None, 120] and group["cellStatus"] == ["A", "F"]


def test_a_small_table_withholds_the_histogram_whole(tmp_path):
    rows = [{"amount": "10", "market": "IE", "qty": "1"} for _ in range(6)]
    result = _profile(tmp_path, rows, [PROPERTIES[2], PROPERTIES[3]])
    assert _entry(result, "amount", "marginalHistogram/1")["groups"] == [
        {"group": "", "status": "A"}
    ]
    assert _entry(result, "market", "levelHistogram/1")["groups"] == [{"group": "", "status": "A"}]
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


def test_a_value_outside_the_declared_support_is_refused(tmp_path):
    rows = _rows()
    rows[7]["market"] = "ZZ-secret-market"
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, rows, [PROPERTIES[3]])
    assert refused.value.event == "profile_bundle_record_refused"
    assert "outside the declared support" in str(refused.value)
    assert "ZZ-secret-market" not in str(refused.value)


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
    block = bundle.read_bundle_block(directory, EXPOSE)
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
    block = bundle.read_bundle_block(directory, EXPOSE)
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
    block = bundle.read_bundle_block(directory, EXPOSE)
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
    block = bundle.read_bundle_block(directory, EXPOSE)
    declared = _stats.load_backend().read_block(block.raw, CONTRACT, EXPOSE)
    assert [s.description for s in declared.statistics] == ["mean/1 of 'amount'"]
    assert declared.columns == ("amount",)
    assert [name for name, _ in declared.left_out] == ["correlation of 'amount' and 'qty'"]


def test_quantile_ranks_are_proved_by_the_registry(tmp_path):
    backend = _stats.load_backend()
    # 120 records, the median is the 60th: 48 strictly below, 72 at or below proves it.
    assert backend.quantile_ranks_ok(120, Fraction(1, 2), 48, 72)
    assert not backend.quantile_ranks_ok(120, Fraction(1, 2), 60, 70)
    assert not backend.quantile_ranks_ok(120, Fraction(1, 2), 70, 45)  # not a sub-count


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
    assert "needs a flux-stats release that carries flux_stats.fidelity" in out


def test_a_block_written_with_bare_null_keys_and_bare_floats_profiles_as_the_quoted_one(tmp_path):
    (tmp_path / "quoted").mkdir()
    (tmp_path / "bare").mkdir()
    quoted = _profile(tmp_path / "quoted", _rows(), [PROPERTIES[0], PROPERTIES[5]])
    directory = _bundle(tmp_path / "bare", [PROPERTIES[0], PROPERTIES[5]])
    path = directory / "simulation.flux.yml"
    text = path.read_text(encoding="utf-8")
    assert "'null':" in text and "level: '0.5'" in text
    path.write_text(text.replace("'null':", "null:").replace("level: '0.5'", "level: 0.5"))
    block = bundle.read_bundle_block(directory, EXPOSE)
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
