# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A private release answers the consumer queries that pass, with noise and a ``unit``.

A query's statistic is a ``levelHistogram/1`` over ``["false", "true"]`` whose two cells per group
are noisy counts of the unit's records the query does not and does return (PROFILE-SPEC section
3a, "A noisy query"). The engine's own reader is the oracle: every document here is read back by
it, and what it reads of the ``unit`` is checked. What is answered, what it is charged and the
exact cells behind it are in ``tests/profiler/dp/test_query_release``; the gym is the one of
``tests/profiler/dp/_gym_data``.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest
import yaml

from fluid_build.cli import main
from fluid_build.profiler import _stats, document, query_rates, sources
from fluid_build.profiler.dp import bounding, query_release, release
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp.discrete_gaussian import sample_discrete_gaussian
from fluid_build.profiler.dp.randomness import SeededSource
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _gym_data as gym
from tests.profiler.dp import _gym_queries as gq
from tests.profiler.dp._gym_data import TABLE

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")
pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")

F = Fraction
SEED = gym.SEED


def _plan(*queries: Mapping[str, Any], history: Optional[Mapping[str, Any]] = None, **more: Any):
    return gym.follow_plan(gq.asked(*queries, history=history, **more))


def _loaded(drawn: Any) -> Any:
    from flux_stats.profile import load_profile_text

    loaded, _ = load_profile_text(drawn.canonical.decode("utf-8"))
    return loaded


def _entries(drawn: Any) -> Dict[str, Dict[str, Any]]:
    return {each["name"]: each for each in drawn.document["queries"]}


# ---- the document -------------------------------------------------------------------------------


def test_each_answered_query_is_an_entry_with_its_unit_and_noisy_groups_in_the_requests_order():
    drawn, _ = gym.draw(_plan(*gq.EVERY, history={}))
    assert [each["name"] for each in drawn.document["queries"]] == [
        each["name"] for each in gq.EVERY
    ]
    entries = _entries(drawn)
    assert entries["plus_rows"]["unit"] == {"table": TABLE}
    assert entries["full_plus"]["unit"] == {"table": TABLE}
    assert entries["members_with_two"]["unit"] == {"table": TABLE, "by": ["member_id"]}
    assert entries["cards_with_two"]["unit"] == {"table": TABLE, "by": ["card_no"]}
    by_label = {each.label: each for each in drawn.tables}
    for name, entry in entries.items():
        (statistic,) = entry["statistics"]
        assert statistic["definition"] == "levelHistogram/1"
        assert statistic["levels"] == ["false", "true"]
        assert statistic["sampleSize"] == {"min": 0} and statistic["disclosure"] == "noisy"
        part = by_label[f"query/{name}"]
        assert statistic["noise"] == {
            "mechanism": "discreteGaussian",
            "scale": part.scale,
            "privacyLoss": {"rho": part.share},
        }
        for group in statistic["groups"]:
            assert group["status"] == "F" and len(group["noisyCounts"]) == 2
            assert "k" not in group and "levelCounts" not in group
    grouped = entries["full_by_tier"]["statistics"][0]
    assert grouped["groupBy"] == "tier"
    assert [each["group"] for each in grouped["groups"]] == ["basic", "plus"]
    assert all(
        "groupBy" not in entries[name]["statistics"][0]
        for name in entries
        if name != "full_by_tier"
    )
    assert [each["group"] for each in entries["plus_rows"]["statistics"][0]["groups"]] == [""]


def test_the_document_names_format_1_5_0_and_the_engine_reads_each_query_and_its_unit():
    drawn, _ = gym.draw(_plan(*gq.EVERY, history={}))
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_5_0
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))
    read = {each.name: each for each in _loaded(drawn).queries}
    assert set(read) == {each["name"] for each in gq.EVERY}
    assert (read["plus_rows"].unit.table, read["plus_rows"].unit.by) == (TABLE, ())
    assert (read["cards_with_two"].unit.table, read["cards_with_two"].unit.by) == (
        TABLE,
        ("card_no",),
    )
    assert read["members_with_two"].unit.by == ("member_id",)
    for each in read.values():
        (statistic,) = each.statistics
        assert statistic.noise is not None
    written = _entries(drawn)["full_by_tier"]["statistics"][0]["groups"]
    got = read["full_by_tier"].statistics[0].groups
    assert [list(each.noisy) for each in got] == [
        [F(text) for text in group["noisyCounts"]] for group in written
    ]


def test_a_query_is_a_count_table_of_its_records_with_noise_drawn_from_the_stream_of_its_label():
    plan = _plan(*gq.EVERY, history={})
    drawn, _ = gym.draw(plan)
    by_label = {each.label: each for each in drawn.tables}
    for name, cells in gq.CELLS.items():
        part = by_label[f"query/{name}"]
        sigma2 = F(part.scale_units**2, 10**12)
        source = SeededSource(SEED, part.label)
        expected = [str(count + sample_discrete_gaussian(sigma2, source)) for count in cells]
        written = [
            text
            for group in _entries(drawn)[name]["statistics"][0]["groups"]
            for text in group["noisyCounts"]
        ]
        assert [F(text) for text in written] == [F(text) for text in expected], name


def test_the_budget_adds_up_with_the_queries_and_each_part_takes_an_equal_share():
    base = gym.follow_plan(gq.asked(history={}))
    plan = _plan(*gq.EVERY, history={})
    base_drawn, _ = gym.draw(base)
    drawn, _ = gym.draw(plan)
    assert len(drawn.tables) == len(base_drawn.tables) + 5
    assert drawn.share == release.split_budget(plan.rho, False, len(drawn.tables))[1]
    assert drawn.share < base_drawn.share
    assert len({each.share for each in drawn.tables}) == 1
    assert sum((F(each.share) for each in drawn.tables), F(0)) <= plan.rho


def test_a_request_with_queries_none_of_which_is_answered_writes_the_release_it_was():
    base, _ = gym.draw(gym.follow_plan(gq.asked(pass_history=True)))
    unreached = gq.query("passes", "SELECT pass_no FROM guest_pass", table="guest_pass")
    drawn, _ = gym.draw(_plan(unreached, pass_history=True))
    assert "queries" not in drawn.document
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_4_0
    assert drawn.canonical == base.canonical and drawn.unanswered == ()


def test_a_release_with_no_queries_in_its_request_keeps_its_bytes():
    from tests.profiler.test_release_cells import FOLLOW_DIGEST
    from tests.profiler.test_release_null_counts import _follow_draw, _follow_plan_of

    drawn, _ = _follow_draw(_follow_plan_of({}))
    assert drawn.digest == FOLLOW_DIGEST
    assert "queries" not in drawn.document


def test_the_same_seed_draws_the_same_bytes_and_another_seed_other_noise():
    plan = _plan(*gq.EVERY, history={})
    one, _ = gym.draw(plan)
    two, _ = gym.draw(plan)
    assert one.canonical == two.canonical
    other, _ = gym.draw(plan, seed=bytes(range(50, 82)))
    assert other.canonical != one.canonical


# ---- an exact profile is as it was ---------------------------------------------------------------


def test_an_exact_profile_writes_its_queries_without_a_unit(tmp_path):
    from tests.profiler import test_query_rates as exact

    result = exact._profile(tmp_path, exact._on_table("north", exact.NORTH))
    (entry,) = result.document["queries"]
    assert set(entry) == {"name", "statistics"}
    (statistic,) = entry["statistics"]
    assert "unit" not in statistic and "noise" not in statistic
    assert statistic["disclosure"] != "noisy"
    assert set(statistic["groups"][0]) >= {"k", "levelCounts"}


# ---- a plain release answers the table and view units of its expose ------------------------------


def _plain_plan(*queries: Mapping[str, Any]) -> release.ReleasePlan:
    backend = FakeBackend()
    built = backend.read_request(json.dumps(gq.asked(*queries)), name="gym.profile-request.yml")
    return release.plan_release(
        gym.CONTRACT, built, backend, unit=TABLE, rho=release.parse_rho("1")
    )


def _plain_read() -> sources.SourceColumns:
    rows = gym.versions()
    return sources.SourceColumns(
        description="rows",
        row_count=len(rows),
        values={name: [row[name] for row in rows] for name in gym.VERSION_COLUMNS},
    )


def _plain_draw(plan: release.ReleasePlan, seed: bytes = SEED):
    backend = FakeBackend()
    rows = release.encode_rows(plan, _plain_read(), backend)
    return release.draw(plan, rows, seed, backend, cliques=(), producer=gym.PRODUCER), rows


PLAIN = (gq.PLUS_ROWS, gq.FULL_PLUS_VIEW, gq.FULL_BY_TIER_QUERY)


def test_a_plain_release_writes_its_queries_and_the_engine_reads_them():
    plan = _plain_plan(*PLAIN)
    drawn, _ = _plain_draw(plan)
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_5_0
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))
    entries = _entries(drawn)
    assert list(entries) == ["plus_rows", "full_plus", "full_by_tier"]
    assert all(each["unit"] == {"table": TABLE} for each in entries.values())
    by_label = {each.label: each for each in drawn.tables}
    tier = by_label["noise/tier"]
    for name in entries:
        assert by_label[f"query/{name}"].scale == tier.scale  # a unit of one row: m = 1
    for read in _loaded(drawn).queries:
        assert read.unit.by == () and read.statistics[0].noise is not None


def test_a_plain_release_counts_its_own_rows_by_hand():
    plan = _plain_plan(*PLAIN)
    backend = FakeBackend()
    rows = release.encode_rows(plan, _plain_read(), backend)
    found = query_release.exact_counts(
        plan.queries, {TABLE: rows.raw}, {TABLE: range(len(rows.contents))}, backend
    )
    assert found == {
        "query/plus_rows": [6, 5],
        "query/full_plus": [2, 4],
        "query/full_by_tier": [4, 2, 1, 4],
    }


def test_a_plain_release_with_no_queries_keeps_its_bytes():
    base, _ = _plain_draw(_plain_plan())
    assert "queries" not in base.document
    assert document.SCHEMA_URL_1_5_0 != base.document["_schemaURL"]
    unreached = gq.query("members", "SELECT member_id FROM member", table="member")
    same, _ = _plain_draw(_plain_plan(unreached))
    assert same.canonical == base.canonical


def test_a_keyed_plain_release_counts_a_unit_at_most_the_cap_in_each_cell():
    from tests.profiler.dp import _release_data as data
    from tests.profiler.dp import _release_run as run

    backend = FakeBackend()
    asked = yaml.safe_load(
        (data.FIXTURES / "orders.profile-request.yml").read_text(encoding="utf-8")
    )
    asked["_schemaURL"] = (
        "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-3-0/"
        "FluxProfileRequest.json"
    )
    asked["queries"] = [
        gq.query("paid", "SELECT order_id FROM orders WHERE status = 'paid'", table="orders")
    ]
    asked.pop("contract", None)
    built = backend.read_request(json.dumps(asked), name="orders.profile-request.yml")
    plan = release.plan_release(
        run.contract("orders"),
        built,
        backend,
        unit="orders",
        rho=release.parse_rho("1"),
        unit_key=("customer_id",),
        max_rows_per_unit=3,
        max_rows_per_cell=1,
    )
    assert [each.plan.name for each in plan.queries.queries] == ["paid"]
    rows = data.orders()
    encoded = release.encode_rows(plan, run.columns(rows), backend)
    kept = bounding.bound_rows(encoded.units, encoded.contents, SEED, 3)
    found = query_release.exact_counts(
        plan.queries, {"orders": encoded.raw}, {"orders": kept}, backend, units=encoded.units, cap=1
    )
    assert found is not None
    paid = {rows[i][1] for i in kept if rows[i][2] == "paid"}
    other = {rows[i][1] for i in kept if rows[i][2] != "paid"}
    assert found["query/paid"] == [len(other), len(paid)]
    drawn = release.draw(plan, encoded, SEED, backend, cliques=run.CLIQUES, producer=gym.PRODUCER)
    by_label = {each.label: each for each in drawn.tables}
    others = {each.scale for each in drawn.tables if each.kind != "query"}
    assert others == {by_label["query/paid"].scale}  # charged as every count table of the release
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))


# ---- the record -------------------------------------------------------------------------------


def test_the_record_carries_the_kind_and_draws_the_same_document_again():
    plan = _plan(*gq.EVERY, history={})
    drawn, rows = gym.draw(plan)
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    stated = [each for each in record.tables if each.get("kind") == "query"]
    assert [each["label"] for each in stated] == [f"query/{each['name']}" for each in gq.EVERY]
    assert {each["table"] for each in stated} == {TABLE}
    assert all(each["columns"] == [] for each in stated)
    assert fr.check_follow_record(plan, record) == {}
    again = fr.draw_follow(plan, rows, SEED, FakeBackend(), cliques={}, producer=gym.PRODUCER)
    assert again.canonical == drawn.canonical


def test_a_record_that_does_not_state_the_queries_of_the_plan_is_refused():
    plan = _plan(*gq.EVERY, history={})
    drawn, _ = gym.draw(plan)
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    for other in (_plan(history={}), _plan(gq.PLUS_ROWS, history={})):
        with pytest.raises(release.ReleaseRefused) as refused:
            fr.check_follow_record(other, record)
        assert refused.value.event == "profile_release_record_mismatch"


def test_a_plain_release_record_carries_the_kind_and_draws_the_same_document_again():
    plan = _plain_plan(*PLAIN)
    drawn, rows = _plain_draw(plan)
    record = release.build_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    assert [each["label"] for each in record.tables if each.get("kind") == "query"] == [
        "query/plus_rows",
        "query/full_plus",
        "query/full_by_tier",
    ]
    assert release.check_record(plan, record) == ()
    again = release.draw(plan, rows, SEED, FakeBackend(), cliques=(), producer=gym.PRODUCER)
    assert again.canonical == drawn.canonical
    with pytest.raises(release.ReleaseRefused):
        release.check_record(_plain_plan(gq.PLUS_ROWS), record)


# ---- the command line -------------------------------------------------------------------------


def _write_csv(path: Path, names: Sequence[str], rows: Sequence[Mapping[str, Optional[str]]]):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(names)
        for row in rows:
            writer.writerow(["" if row[name] is None else row[name] for name in names])


def _setup(
    tmp_path: Path,
    monkeypatch,
    *queries: Mapping[str, Any],
    history: Optional[Mapping[str, Any]] = {},  # noqa: B006  (read, never changed)
    **more: Any,
) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gym.fluid.yaml").write_text(
        yaml.safe_dump(gym.CONTRACT, sort_keys=False), encoding="utf-8"
    )
    request = gq.asked(*queries, history=history, **more)
    (tmp_path / "gym.profile-request.yml").write_text(
        yaml.safe_dump(request, sort_keys=False), encoding="utf-8"
    )
    _write_csv(tmp_path / "member.csv", gym.MEMBER_COLUMNS, gym.members())
    _write_csv(tmp_path / f"{TABLE}.csv", gym.VERSION_COLUMNS, gym.versions())
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return tmp_path


def _argv(*more: str) -> List[str]:
    return [
        "profile",
        "gym.fluid.yaml",
        "--request",
        "gym.profile-request.yml",
        "--source",
        "member=member.csv",
        "--source",
        f"{TABLE}={TABLE}.csv",
        "--privacy-unit",
        "member",
        "--follow",
        gym.FOLLOW,
        "--rho",
        "1",
        "--release-record",
        "rec.json",
        *more,
    ]


def test_the_command_writes_the_queries_names_each_and_prints_no_count(
    tmp_path, monkeypatch, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    workspace = _setup(tmp_path, monkeypatch, *gq.EVERY)
    assert main(_argv()) == 0
    seen = capsys.readouterr()
    text = seen.out + seen.err + caplog.text
    flat = " ".join(text.split())
    for each in gq.EVERY:
        assert f"query/{each['name']}" in flat, each["name"]
    assert "SELECT" not in text
    for place in {str(workspace), str(workspace.resolve())}:
        text = text.replace(place, "<workspace>")
    shown = set(re.findall(r"(?<![\w.])\d+(?![\w.])", text))
    assert "11" not in shown
    written = json.loads((workspace / "member.profile.json").read_text(encoding="utf-8"))
    assert [each["name"] for each in written["queries"]] == [each["name"] for each in gq.EVERY]
    assert written["_schemaURL"] == document.SCHEMA_URL_1_5_0
    record = json.loads((workspace / "rec.json").read_text(encoding="utf-8"))
    assert sum(1 for each in record["tables"] if each.get("kind") == "query") == 5


def test_the_command_names_a_query_it_does_not_answer_with_its_reason_and_no_text(
    tmp_path, monkeypatch, capsys
):
    passes = gq.query("passes", "SELECT pass_no FROM guest_pass", table="guest_pass")
    _setup(tmp_path, monkeypatch, gq.PLUS_ROWS, passes, pass_history=True)
    assert main(_argv()) == 0
    flat = " ".join((capsys.readouterr().out).split())
    assert query_release.NOT_REACHED in flat
    assert "query passes" in flat and "SELECT" not in flat


def test_rederive_draws_the_same_release_again_with_queries(tmp_path, monkeypatch, capsys):
    workspace = _setup(tmp_path, monkeypatch, *gq.EVERY)
    assert main(_argv()) == 0
    capsys.readouterr()
    first = (workspace / "member.profile.json").read_bytes()
    (workspace / "member.profile.json").unlink()
    again = [
        "profile",
        "gym.fluid.yaml",
        "--request",
        "gym.profile-request.yml",
        "--source",
        "member=member.csv",
        "--source",
        f"{TABLE}={TABLE}.csv",
        "--release-record",
        "rec.json",
        "--rederive",
    ]
    assert main(again) == 0
    assert (workspace / "member.profile.json").read_bytes() == first


def test_strict_refuses_a_query_the_contract_and_the_request_rule_out_before_any_read(
    tmp_path, monkeypatch, capsys
):
    passes = gq.query("passes", "SELECT pass_no FROM guest_pass", table="guest_pass")
    workspace = _setup(tmp_path, monkeypatch, gq.PLUS_ROWS, passes, pass_history=True)
    assert main(_argv("--strict")) != 0
    seen = capsys.readouterr()
    assert "profile_request_not_formed" in seen.out + seen.err
    assert "query passes" in seen.out + seen.err
    assert not (workspace / "member.profile.json").exists()
    assert not (workspace / "rec.json").exists()


def test_strict_never_refuses_on_a_reason_only_the_records_give(tmp_path, monkeypatch, capsys):
    workspace = _setup(tmp_path, monkeypatch, gq.PLUS_ROWS, history=None)
    rows = gym.versions()
    rows[3] = dict(rows[3], card_no="not-a-number")
    _write_csv(workspace / f"{TABLE}.csv", gym.VERSION_COLUMNS, rows)
    assert main(_argv("--strict")) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert query_rates.RUN_FAILED in out and "query plus_rows" in out
    written = json.loads((workspace / "member.profile.json").read_text(encoding="utf-8"))
    assert "queries" not in written
    assert (workspace / "rec.json").exists()


def test_the_help_says_a_private_release_answers_the_queries_that_pass():
    from fluid_build.cli import _profile_release, profile_cmd

    flat = " ".join(_profile_release.HELP.split())
    assert "query" in flat and "publishes no query rate" not in flat
    source = Path(profile_cmd.__file__).read_text(encoding="utf-8")
    assert "A private release publishes no query rate" not in source


# ---- the engine does not read what the planner would never write -----------------------------------


def test_a_document_of_the_gym_with_a_query_over_a_table_it_does_not_hold_is_refused_by_the_reader():
    drawn, _ = gym.draw(_plan(*gq.EVERY, history={}))
    broken = json.loads(drawn.canonical.decode("utf-8"))
    broken["queries"][0]["unit"] = {"table": "guest_pass"}
    with pytest.raises(Exception) as refused:
        FakeBackend().check_document(json.dumps(broken))
    assert "guest_pass" in str(refused.value) or "unit" in str(refused.value)
    only = json.loads(drawn.canonical.decode("utf-8"))
    del only["queries"][0]["unit"]
    with pytest.raises(Exception):
        FakeBackend().check_document(json.dumps(only))
