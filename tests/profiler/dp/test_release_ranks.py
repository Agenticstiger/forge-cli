# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Rank correlations and cliques with binned columns in a release under differential privacy."""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction
from typing import Any, List

import pytest

from fluid_build.profiler import document
from fluid_build.profiler.dp import record as record_module
from fluid_build.profiler.dp import release
from fluid_build.profiler.dp.release import ReleaseRefused
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _rank_run as ranked
from tests.profiler.dp import _release_data as data
from tests.profiler.dp import _release_run as run
from tests.profiler.dp._privacy import need

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.rank_correlation")

F = Fraction
MIXED = ranked.MIXED


@pytest.fixture(scope="module")
def mixed():
    plan = ranked.plan()
    return plan, ranked.draw(plan, data.orders())


@pytest.fixture(scope="module")
def chosen():
    """A release whose cliques a selection chose, so its record states the selection."""
    plan = ranked.plan()
    drawn = ranked.draw(
        plan, data.orders(), cliques=None, select=lambda *a, **k: MIXED, structure=run.keep
    )
    return plan, drawn


def _event(call: Any) -> str:
    with pytest.raises(ReleaseRefused) as info:
        call()
    return info.value.event


def _read(drawn: release.Release):
    from flux_stats.profile import load_profile_text

    profile, _ = load_profile_text(drawn.canonical.decode("utf-8"))
    return profile


def _keys(value: Any) -> List[str]:
    if isinstance(value, dict):
        return [*value, *(key for each in value.values() for key in _keys(each))]
    if isinstance(value, list):
        return [key for each in value for key in _keys(each)]
    return []


# ---- the plan ------------------------------------------------------------------------------


def test_the_plan_holds_each_asked_pair_with_the_default_floor_and_the_units_sensitivity():
    plan = ranked.plan(c=3)
    assert plan.rank_pairs == (
        (ranked.KENDALL, "amount", "quantity"),
        (ranked.SPEARMAN, "amount", "quantity"),
    )
    assert plan.row_floor == release.DEFAULT_ROW_FLOOR == 1000
    # one entity changes up to three rows, each moving the value by at most 4/(f+1) or 9/(f+1)
    assert plan.delta(ranked.KENDALL) == F(3 * 4, 1001)
    assert plan.delta(ranked.SPEARMAN) == F(3 * 9, 1001)
    assert [each.name for each in plan.eligible] == ["status", "channel", "amount", "quantity"]
    assert plan.selects and plan.clique_method == "aim"


def test_a_unit_of_one_row_still_moves_a_rank_correlation_as_two_rows_do():
    # replacing the one kept row of a unit is a removal and an addition
    plan = ranked.plan(c=1)
    assert plan.delta(ranked.KENDALL) == F(2 * 4, 1001)
    assert plan.delta(ranked.SPEARMAN) == F(2 * 9, 1001)


def test_a_row_floor_is_taken_and_a_release_of_each_row_has_a_delta_of_one_record():
    plan = ranked.plan(c=None, row_floor=99)
    assert plan.row_floor == 99 and plan.delta(ranked.KENDALL) == F(4, 100)


def test_without_cliqueColumns_only_categorical_columns_join_cliques():
    plan = ranked.plan(joining=())
    assert [each.name for each in plan.eligible] == ["status", "channel"]


def test_a_pair_the_release_cannot_hold_is_reported_not_released():
    plan = ranked.plan(rank_columns=("amount", "status", "quantity"))
    assert [(each[1], each[2]) for each in plan.rank_pairs] == [("amount", "quantity")] * 2
    named = dict(plan.not_released)
    assert "kendallTau/1 of orders.amount and status" in named
    assert "spearmanRho/1 of orders.status and quantity" in named
    assert all(isinstance(reason, str) and reason for reason in named.values())


def test_a_row_floor_with_no_rank_correlation_released_is_refused():
    assert _event(lambda: ranked.plan(ranks=(), row_floor=50)) == "profile_release_row_floor_unused"


def test_an_unknown_clique_method_is_refused():
    assert _event(lambda: ranked.plan(method="pgm")) == "profile_release_clique_method"


# ---- the budget: one list of parts ---------------------------------------------------------


def test_the_parts_are_listed_once_with_each_kind(mixed):
    plan, _ = mixed
    parts = release.release_parts(plan, MIXED)
    assert [(each.kind, each.columns) for each in parts] == [
        ("clique", ("status", "amount")),
        ("clique", ("amount", "quantity")),
        ("levels", ("channel",)),
        ("rank", ("amount", "quantity")),
        ("rank", ("amount", "quantity")),
    ]
    assert [each.definition for each in parts if each.kind == "rank"] == [
        ranked.KENDALL,
        ranked.SPEARMAN,
    ]
    assert len({each.label for each in parts}) == len(parts)


def test_the_document_the_report_and_the_record_hold_the_planners_parts(mixed):
    plan, drawn = mixed
    parts = release.release_parts(plan, MIXED)
    assert [each.label for each in drawn.tables] == [each.label for each in parts]
    record = release.build_record(
        plan, drawn, run.SEED, request_bytes=b"request", contract_digest=None
    )
    assert [each["label"] for each in record.tables] == [each.label for each in parts]
    profile = _read(drawn)
    spent = dict(profile.parts)
    # each noise block once, and the selection share once, in the reader's own account
    assert len(spent) == len(profile.parts) == len(parts) + 1
    shares = sorted(Fraction(each.loss.rho) for each in spent.values())
    expected = sorted([*(Fraction(each.share) for each in drawn.tables), plan.rho / 10])
    assert shares == expected
    assert sum(shares) <= plan.rho
    selection_parts = [where for where in spent if where.endswith("cliqueSelection.selection")]
    assert len(selection_parts) == 1
    assert spent[selection_parts[0]].loss.rho == plan.rho / 10
    assert drawn.measurement_rho == drawn.share * len(parts)


def test_every_part_is_one_equal_share_of_what_the_selection_leaves():
    plan = ranked.plan(rho="0.7")
    parts = release.release_parts(plan, MIXED)
    selection_rho, share = release.split_budget(plan.rho, plan.selects, len(parts))
    assert selection_rho == F(7, 100)
    assert share > 0 and F(63, 100) / len(parts) - share < F(1, 10**12)
    assert selection_rho + share * len(parts) <= plan.rho


def test_a_release_with_no_selection_spends_all_on_the_parts():
    plan = ranked.plan(joining=("status",))
    assert not plan.selects
    drawn = ranked.draw(plan, data.orders(), cliques=())
    profile = _read(drawn)
    assert not any("cliqueSelection" in where for where, _ in profile.parts)
    assert sum(Fraction(each.loss.rho) for _, each in profile.parts) <= plan.rho


# ---- the document --------------------------------------------------------------------------


def test_the_reader_accepts_rank_correlations_and_a_mixed_clique(mixed):
    plan, drawn = mixed
    text = drawn.canonical.decode("utf-8")
    _read(drawn)
    body = json.loads(text)
    assert body["_schemaURL"] == document.SCHEMA_URL_1_4_0
    assert body["privacy"]["publicStructure"] is True
    assert "variance" not in _keys(body)
    table = body["tables"]["orders"]
    first, second = table["cliques"]
    assert first["definition"] == second["definition"] == "cliqueMarginal/2"
    assert first["domains"][0] == {"levels": ["paid", "placed", "returned", "shipped"]}
    assert first["domains"][1]["bins"] == list(plan.column("amount").edge_texts)
    assert second["domains"][1]["bins"] == list(plan.column("quantity").edge_texts)
    (pair,) = table["columnPairs"]
    assert pair["columns"] == ["amount", "quantity"]
    assert [each["definition"] for each in pair["statistics"]] == [ranked.KENDALL, ranked.SPEARMAN]
    for statistic, definition in zip(
        pair["statistics"], (ranked.KENDALL, ranked.SPEARMAN), strict=True
    ):
        assert statistic["rowFloor"] == 1000 and statistic["sampleSize"] == {"min": 0}
        assert statistic["disclosure"] == "noisy"
        (group,) = statistic["groups"]
        assert set(group) == {"group", "status", "noisyValue"}
        assert group["group"] == "" and group["status"] == "F"
        assert statistic["noise"]["mechanism"] == "gaussian"
        delta = plan.delta(definition)
        scale, rho = Fraction(statistic["noise"]["scale"]), Fraction(
            statistic["noise"]["privacyLoss"]["rho"]
        )
        assert rho >= delta * delta / (2 * scale * scale)


def test_the_clique_selection_states_the_method_the_producer_and_the_share(mixed):
    plan, drawn = mixed
    block = drawn.document["tables"]["orders"]["cliqueSelection"]
    assert block["method"] == "aim" and block["published"] == "measured"
    assert block["producer"].startswith("https://pypi.org/project/opendp/")
    assert block["selection"] == {"privacyLoss": {"rho": "0.1"}}
    assert set(block) == {"method", "producer", "selection", "published"}


def test_the_producer_names_the_opendp_version_that_ran(monkeypatch):
    need("opendp")  # the producer names the OpenDP version installed
    plan = ranked.plan()
    drawn = ranked.draw(
        plan, data.orders(), cliques=None, select=lambda *a, **k: MIXED, structure=run.keep
    )
    producer = drawn.document["tables"]["orders"]["cliqueSelection"]["producer"]
    assert producer == f"https://pypi.org/project/opendp/{release._version('opendp')}/"
    again = ranked.draw(plan, data.orders(), selection_version="0.16.0")
    assert (
        again.document["tables"]["orders"]["cliqueSelection"]["producer"]
        == "https://pypi.org/project/opendp/0.16.0/"
    )


def test_a_release_with_no_rank_correlation_and_no_binned_clique_keeps_format_1_2_0():
    plan = ranked.plan(ranks=(), joining=("status", "channel"))
    drawn = ranked.draw(plan, data.orders(), cliques=(("status", "channel"),))
    body = drawn.document
    assert body["_schemaURL"] == release.SCHEMA_URL_1_2_0
    table = body["tables"]["orders"]
    assert [each["definition"] for each in table["cliques"]] == ["cliqueMarginal/1"]
    assert "cliqueSelection" not in table and "columnPairs" not in table
    assert "variance" not in _keys(body)
    _read(drawn)


def test_a_clique_with_all_categorical_columns_stays_a_cliqueMarginal_1_in_a_1_4_0_document(mixed):
    plan = ranked.plan()
    drawn = ranked.draw(
        plan, data.orders(), cliques=(("status", "channel"), ("amount", "quantity"))
    )
    table = drawn.document["tables"]["orders"]
    assert [each["definition"] for each in table["cliques"]] == [
        "cliqueMarginal/1",
        "cliqueMarginal/2",
    ]
    assert "levels" in table["cliques"][0] and "domains" not in table["cliques"][0]
    _read(drawn)


def test_a_clique_the_columns_may_not_join_is_refused():
    plan = ranked.plan(joining=("status", "channel", "amount"))
    assert _event(lambda: ranked.draw(plan, data.orders(), cliques=MIXED)).endswith("mismatch")


def test_a_clique_with_a_binned_column_stays_one_table_with_the_ones_it_shares_columns_with(
    mixed,
):
    _, drawn = mixed
    table = drawn.document["tables"]["orders"]
    # `amount` is in two cliques, which therefore agree on it (the reader checks it, exactly)
    assert [each["columns"] for each in table["cliques"]] == [list(each) for each in MIXED]
    assert "statistics" not in table["columns"]["amount"]


def test_a_seed_gives_the_same_bytes_and_another_seed_other_noise(mixed):
    plan, first = mixed
    again = ranked.draw(plan, data.orders())
    assert again.canonical == first.canonical
    other = ranked.draw(plan, data.orders(), seed=bytes(range(2, 34)))
    assert other.canonical != first.canonical


def test_the_noisy_value_is_the_exact_value_when_the_budget_is_large():
    backend = FakeBackend()
    plan = ranked.plan(rho="1000000000", joining=("status",), row_floor=200)
    rows = data.orders()
    drawn = ranked.draw(plan, rows, cliques=(), backend=backend)
    encoded = release.encode_rows(plan, ranked.columns(rows), backend)
    pair = drawn.document["tables"]["orders"]["columnPairs"][0]["statistics"][0]
    assert pair["rowFloor"] == 200
    # the exact value over the rows the bounding kept: re-formed here from the same rows
    from fluid_build.profiler.dp import bounding

    kept = bounding.bound_rows(
        encoded.units, encoded.contents, run.SEED, plan.unit.max_rows_per_unit
    )
    pairs = [(encoded.grid["amount"][i], encoded.grid["quantity"][i]) for i in kept]
    exact = backend.rank_value(ranked.KENDALL, pairs, 200)
    value = Fraction(pair["groups"][0]["noisyValue"])
    assert abs(value - exact) < Fraction(1, 100)
    assert exact != 0


# ---- choosing the cliques with OpenDP ------------------------------------------------------


@pytest.mark.parametrize("method", ["aim", "mst"])
def test_both_methods_run_and_the_reader_accepts_the_release(method):
    need("opendp")
    need("mbi")
    need("polars")
    plan = ranked.plan(method=method, rho="2")
    drawn = ranked.draw(plan, data.orders(), cliques=None)
    assert drawn.selected is not None
    body = drawn.document["tables"]["orders"]
    assert body["cliqueSelection"]["method"] == method
    assert body["cliqueSelection"]["producer"].startswith("https://pypi.org/project/opendp/0.16")
    assert body["cliqueSelection"]["selection"] == {"privacyLoss": {"rho": "0.2"}}
    profile = _read(drawn)
    assert sum(Fraction(each.loss.rho) for _, each in profile.parts) <= plan.rho
    if method == "mst":
        # a tree of pairs over the four columns
        assert len(drawn.selected) == 3 and all(len(each) == 2 for each in drawn.selected)
    for clique in body.get("cliques", []):
        assert clique["definition"] in ("cliqueMarginal/1", "cliqueMarginal/2")


def test_only_the_columns_cliqueColumns_names_join_a_selection():
    need("opendp")
    need("mbi")
    need("polars")
    plan = ranked.plan(joining=("status", "quantity"), ranks=())
    assert [each.name for each in plan.eligible] == ["status", "quantity"]
    drawn = ranked.draw(plan, data.orders(), cliques=None)
    assert all(set(each) <= {"status", "quantity"} for each in drawn.selected)
    names = {name for each in drawn.cliques for name in each}
    assert names <= {"status", "quantity"}


def test_a_selection_is_given_the_bin_index_of_a_binned_column_and_the_method(monkeypatch):
    plan = ranked.plan(method="mst")
    seen = []

    def choose(rows, names, levels, weights, rho, contributions, **more):
        seen.append((rows, names, levels, weights, rho, more))
        return MIXED

    ranked.draw(plan, data.orders(), cliques=None, select=choose, structure=run.keep)
    rows, names, levels, weights, rho, more = seen[0]
    assert names == ["status", "channel", "amount", "quantity"]
    assert levels["quantity"] == tuple(str(i) for i in range(plan.column("quantity").size))
    assert all(row[3] in levels["quantity"] for row in rows)
    assert rho == F(1, 10) and more == {"method": "mst"}
    assert weights[("status", "amount")] == 1 and weights[("channel", "amount")] == 2


# ---- the record and the re-derivation -----------------------------------------------------


def _record(plan, drawn):
    return release.build_record(
        plan, drawn, run.SEED, request_bytes=b"request", contract_digest="sha256:" + "ab" * 32
    )


def test_a_record_holds_the_rank_parts_the_floor_and_the_method(mixed):
    plan, drawn = mixed
    record = _record(plan, drawn)
    assert dict(record.options) == {"rowFloor": 1000}
    ranks = [each for each in record.tables if each.get("kind") == "rank"]
    assert [each["definition"] for each in ranks] == [ranked.KENDALL, ranked.SPEARMAN]
    assert all(
        each["rowFloor"] == 1000 and each["columns"] == ["amount", "quantity"] for each in ranks
    )
    assert record.selection is None  # the cliques were given, not chosen
    mst = ranked.plan(method="mst")
    assert dict(_record(mst, ranked.draw(mst, data.orders())).options) == {
        "rowFloor": 1000,
        "cliqueMethod": "mst",
    }


def test_drawing_again_from_the_record_gives_the_same_bytes():
    plan = ranked.plan(method="mst", row_floor=250)
    drawn = ranked.draw(
        plan, data.orders(), cliques=None, select=lambda *a, **k: MIXED, structure=run.keep
    )
    record = _record(plan, drawn)
    assert dict(record.selection or {}) == {"rho": "0.1", "cliques": [list(each) for each in MIXED]}
    # through the file's own form, as --rederive reads it
    again_record = record_module.ReleaseRecord.from_document(
        json.loads(json.dumps(record.to_document()))
    )
    assert dict(again_record.options) == {"rowFloor": 250, "cliqueMethod": "mst"}
    replan = ranked.plan(method="mst", row_floor=250)
    cliques = release.check_record(replan, again_record)
    assert cliques == MIXED
    again = ranked.draw(
        replan,
        data.orders(),
        cliques=cliques,
        selection_version=again_record.versions.get("opendp"),
    )
    assert again.canonical == drawn.canonical and again.digest == drawn.digest


def test_a_record_that_states_other_options_or_ranks_is_refused(chosen):
    plan, drawn = chosen
    record = _record(plan, drawn)
    ranks = [i for i, each in enumerate(record.tables) if each.get("kind") == "rank"]
    tampered = [
        dataclasses.replace(record, options={}),
        dataclasses.replace(record, options={"rowFloor": 999}),
        dataclasses.replace(record, options={"rowFloor": 1000, "cliqueMethod": "mst"}),
        dataclasses.replace(
            record,
            tables=tuple(
                {**each, "rowFloor": 5} if i in ranks else each
                for i, each in enumerate(record.tables)
            ),
        ),
        dataclasses.replace(
            record,
            tables=tuple(
                {**each, "scale": "1"} if i in ranks else each
                for i, each in enumerate(record.tables)
            ),
        ),
        dataclasses.replace(
            record,
            tables=tuple(
                {k: v for k, v in each.items() if k != "kind"} if i in ranks else each
                for i, each in enumerate(record.tables)
            ),
        ),
        dataclasses.replace(
            record, tables=tuple(each for i, each in enumerate(record.tables) if i not in ranks)
        ),
    ]
    for each in tampered:
        assert _event(lambda: release.check_record(plan, each)) == "profile_release_record_mismatch"
    assert release.check_record(plan, record) == MIXED


def test_a_record_of_an_old_release_has_no_options_and_is_still_read():
    plan = run.keyed_plan()
    drawn = run.draw(plan, data.orders())
    record = release.build_record(plan, drawn, run.SEED, request_bytes=b"r", contract_digest=None)
    assert dict(record.options) == {}
    assert "options" not in record.to_document()
    again = record_module.ReleaseRecord.from_document(record.to_document())
    assert dict(again.options) == {}


@pytest.mark.parametrize("bad", [{"rowFloor": 1.5}, {"rowFloor": True}, {"x": [1]}])
def test_a_record_option_that_is_not_text_or_a_whole_number_is_refused(mixed, bad):
    plan, drawn = mixed
    record = dataclasses.replace(_record(plan, drawn), options=bad)
    with pytest.raises(record_module.RecordRefused):
        record.to_document()
