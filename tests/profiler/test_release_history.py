# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A private release of a table's noisy history: the document, the record and the report.

The engine's own reader is the oracle: every document here is read back by it (a noisy history
member names format 1-5-0, sits in a table the unit follows into, and states a share of the budget
that covers the charge of ``m_T``). The counts behind each member are in
``tests/profiler/dp/test_history_release.py``; the gym is the one of ``tests/profiler/dp/_gym_data``.
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
from fluid_build.profiler import _stats, document
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import release
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _gym_data as gym
from tests.profiler.dp._gym_data import TABLE

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

F = Fraction
SEED = gym.SEED


def _plan(**asks: Any) -> fr.FollowReleasePlan:
    return gym.follow_plan(gym.request(history={}, **asks))


def _loaded(drawn: fr.FollowRelease) -> Any:
    from flux_stats.profile import load_profile_text

    loaded, _ = load_profile_text(drawn.canonical.decode("utf-8"))
    return loaded.tables[TABLE].history


# ---- the document -------------------------------------------------------------------------------


def test_the_history_is_written_whole_with_a_noise_block_on_every_member():
    drawn, _ = gym.draw(_plan())
    held = gym.history_of(drawn)
    assert held["entity"] == ["card_no"]
    assert (held["start"], held["end"]) == ("valid_from", "valid_to")
    members = [held[name] for name in ("versionsPerEntity", "firstStart", "versionLength")]
    members += [held["chain"], held["lastVersion"], held["reasons"]]
    for column in held["columns"].values():
        members += [column["initial"], column["transitions"]]
    assert len(members) == 10
    by_label = {each.label: each for each in drawn.tables}
    for member in members:
        assert member["disclosure"] == "noisy" and member["sampleSize"] == {"min": 0}
        assert set(member["noise"]) == {"mechanism", "scale", "privacyLoss"}
        assert member["noise"]["mechanism"] == "discreteGaussian"
        for group in member["groups"]:
            assert group["status"] == "F" and "noisyCounts" in group
            assert "k" not in group and "binCounts" not in group
    assert held["versionsPerEntity"]["noise"]["scale"] == (
        by_label[f"{TABLE}/history/versionsPerEntity"].scale
    )
    assert held["columns"]["tier"]["transitions"]["noise"]["privacyLoss"] == {
        "rho": by_label[f"{TABLE}/history/transitions/tier"].share
    }


def test_the_bins_and_levels_are_the_public_ones_and_every_group_is_published():
    from fluid_build.profiler import dates, history

    held = gym.history_of(gym.draw(_plan())[0])
    assert held["versionsPerEntity"]["bins"] == [str(e) for e in history._version_edges()]
    assert held["firstStart"]["bins"] == [str(e) for e in history._year_ladder()]
    assert held["versionLength"]["bins"] == [
        str(e) for e in dates.DAY_LADDER[dates.DAY_LADDER.index(0) :]
    ]
    assert held["chain"]["levels"] == ["dayBefore", "gap", "nextStart", "overlap"]
    assert held["lastVersion"]["levels"] == ["closed", "open"]
    assert held["reasons"]["levels"] == ["access", "several", "tier"]
    for member in ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"):
        (group,) = held[member]["groups"]
        assert group["group"] == ""
        width = len(held[member].get("bins", held[member].get("levels", []))) - (
            1 if "bins" in held[member] else 0
        )
        assert len(group["noisyCounts"]) == width
    transitions = held["columns"]["tier"]["transitions"]
    assert transitions["groupBy"] == "tier" and transitions["levels"] == ["basic", "plus"]
    assert [group["group"] for group in transitions["groups"]] == ["basic", "plus"]
    assert all(len(group["noisyCounts"]) == 2 for group in transitions["groups"])


def test_the_table_states_the_columns_the_history_reads_by_domain():
    drawn, _ = gym.draw(_plan())
    columns = drawn.document["tables"][TABLE]["columns"]
    assert columns["valid_from"]["domain"] == "timestamp"
    assert columns["valid_to"]["domain"] == "timestamp"
    assert columns["card_no"]["domain"] == "number"
    assert columns["access"] == {"domain": "category"}  # read for the history, nothing else
    assert "statistics" not in columns["valid_from"]


def test_the_document_names_format_1_5_0_and_the_engine_reads_every_noisy_member():
    drawn, _ = gym.draw(_plan())
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_5_0
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))
    held = _loaded(drawn)
    assert held is not None and held.entity == ("card_no",)
    written = gym.history_of(drawn)
    for read, name in (
        (held.versions_per_entity, "versionsPerEntity"),
        (held.first_start, "firstStart"),
        (held.version_length, "versionLength"),
        (held.chain, "chain"),
        (held.last_version, "lastVersion"),
        (held.reasons, "reasons"),
    ):
        assert read is not None and read.noise is not None, name
        (group,) = read.groups
        assert list(group.noisy) == [F(t) for t in written[name]["groups"][0]["noisyCounts"]]
    assert set(held.columns) == {"tier", "access"}
    transitions = held.columns["tier"].transitions
    assert transitions is not None and transitions.noise is not None
    assert [group.key for group in transitions.groups] == ["basic", "plus"]


def test_a_history_one_tracked_column_has_no_reasons_and_the_engine_reads_it():
    drawn, _ = gym.draw(gym.follow_plan(gym.request(history={"columns": ["tier"]})))
    assert "reasons" not in gym.history_of(drawn)
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))


def test_a_history_with_no_tracked_column_is_written_with_the_five_members():
    contract = gym.contract()
    schema = contract["exposes"][1]["contract"]["schema"]
    contract["exposes"][1]["contract"]["schema"] = [
        each for each in schema if each["name"] not in ("tier", "access")
    ]
    asked = gym.request(history={})
    del asked["tables"][TABLE]["columns"]["tier"]
    del asked["tables"][TABLE]["columns"]["access"]
    plan = gym.follow_plan(asked, contract_=contract)
    drawn, _ = gym.draw(plan)
    held = gym.history_of(drawn)
    assert "columns" not in held and "reasons" not in held
    assert {"versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"} <= set(held)
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))


def test_a_document_that_mixes_a_history_null_counts_and_ordinary_parts_passes_the_reader():
    asked = gym.request(history={}, null_counts=("valid_to", "tier"))
    plan = gym.follow_plan(asked)
    drawn, _ = gym.draw(plan)
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))
    columns = drawn.document["tables"][TABLE]["columns"]
    assert [each["definition"] for each in columns["tier"]["statistics"]] == [
        "levelHistogram/1",
        "nullCount/2",
    ]
    assert [each["definition"] for each in columns["valid_to"]["statistics"]] == ["nullCount/2"]
    assert "history" in drawn.document["tables"][TABLE]
    assert drawn.document["tables"]["member"]["columns"]["branch"]["statistics"]
    # the history, the null counts and the histograms are all parts of one budget
    spent = sum((F(each.share) for each in drawn.tables), F(0))
    assert spent <= plan.rho
    assert len({each.label for each in drawn.tables}) == len(drawn.tables)


def test_the_history_of_a_second_followed_table_beside_a_table_with_no_window_is_read():
    asked = gym.request(history={}, visits_history=True)
    plan = gym.follow_plan(asked, follows=(gym.FOLLOW, gym.FOLLOW_VISITS))
    drawn, _ = gym.draw(plan)
    assert "history" not in drawn.document["tables"]["visit"]
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))


def test_the_same_seed_draws_the_same_bytes_and_another_seed_other_noise():
    plan = _plan()
    one, _ = gym.draw(plan)
    two, _ = gym.draw(plan)
    assert one.canonical == two.canonical and one.digest == two.digest
    other, _ = gym.draw(plan, seed=bytes(range(50, 82)))
    assert other.canonical != one.canonical


# ---- the budget -------------------------------------------------------------------------------


def test_the_history_parts_add_to_the_budget_and_each_takes_an_equal_share():
    plain = gym.follow_plan(gym.request())
    plan = _plan()
    base, _ = gym.draw(plain)
    drawn, _ = gym.draw(plan)
    assert len(drawn.tables) == len(base.tables) + 10
    assert drawn.share == release.split_budget(plan.rho, False, len(drawn.tables))[1]
    assert drawn.share < base.share
    assert len({each.share for each in drawn.tables}) == 1
    assert sum((F(each.share) for each in drawn.tables), F(0)) <= plan.rho


# ---- the release without the ask is the release it was ------------------------------------------


def test_a_release_without_a_history_ask_is_the_release_it_was():
    from tests.profiler.test_release_cells import FOLLOW_DIGEST
    from tests.profiler.test_release_null_counts import _follow_draw, _follow_plan_of

    drawn, _ = _follow_draw(_follow_plan_of({}))
    assert drawn.digest == FOLLOW_DIGEST
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_4_0
    assert "history" not in drawn.canonical.decode("utf-8")


def test_a_gym_request_with_no_history_writes_none_and_the_format_it_wrote():
    plan = gym.follow_plan(gym.request())
    drawn, _ = gym.draw(plan)
    assert "history" not in drawn.document["tables"][TABLE]
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_4_0
    assert not any(each.kind == release.HISTORY for each in drawn.tables)
    assert plan.entity_units == ()
    # the history columns are not read for a release that does not ask for it
    assert "access" not in {each.name for each in dict(plan.reads)[TABLE]}


def test_an_ask_that_is_not_held_changes_no_byte_of_the_release():
    base, _ = gym.draw(gym.follow_plan(gym.request()))
    plan = gym.follow_plan(gym.request(member_history=True))
    assert dict(plan.not_released)["history of member"]
    drawn, _ = gym.draw(plan)
    assert drawn.canonical == base.canonical


# ---- the record -------------------------------------------------------------------------------


def test_the_record_carries_the_kind_and_draws_the_same_document_again():
    plan = _plan()
    drawn, rows = gym.draw(plan)
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    stated = [each for each in record.tables if each.get("kind") == "history"]
    assert [each["label"] for each in stated] == [
        f"{TABLE}/history/{name}"
        for name in (
            "versionsPerEntity",
            "firstStart",
            "versionLength",
            "chain",
            "lastVersion",
            "initial/tier",
            "transitions/tier",
            "initial/access",
            "transitions/access",
            "reasons",
        )
    ]
    assert {each["table"] for each in stated} == {TABLE}
    assert [each["columns"] for each in stated if each["label"].endswith("/tier")] == [
        ["tier"],
        ["tier"],
    ]
    assert fr.check_follow_record(plan, record) == {}
    again = fr.draw_follow(plan, rows, SEED, FakeBackend(), cliques={}, producer=gym.PRODUCER)
    assert again.canonical == drawn.canonical


def test_a_record_that_does_not_state_the_history_of_the_plan_is_refused():
    plan = _plan()
    drawn, _ = gym.draw(plan)
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    for other in (
        gym.follow_plan(gym.request()),
        gym.follow_plan(gym.request(history={"columns": ["tier"]})),
    ):
        with pytest.raises(release.ReleaseRefused) as refused:
            fr.check_follow_record(other, record)
        assert refused.value.event == "profile_release_record_mismatch"


# ---- how the report words it ------------------------------------------------------------------


def test_the_report_names_a_history_member_by_its_label_as_it_names_the_other_parts():
    from fluid_build.cli import _profile_follow

    assert _profile_follow.part_words(f"{TABLE}/history/chain") == f"history chain of {TABLE}"
    assert (
        _profile_follow.part_words(f"{TABLE}/history/transitions/tier")
        == f"history transitions of tier of {TABLE}"
    )
    left = [(f"{TABLE}/history/initial/access", "why"), (f"{TABLE}/history/reasons", "why")]
    assert _profile_follow.left_out_words(left) == [
        (f"history initial of access of {TABLE}", "why"),
        (f"history reasons of {TABLE}", "why"),
    ]


def test_the_notes_say_what_a_history_counts_and_name_no_count():
    from fluid_build.cli import _profile_follow

    plan = _plan()
    notes = " ".join(_profile_follow.unknown_values_notes(plan.table(TABLE).plan, TABLE))
    assert "history of membership_version" in notes and "public ladders" in notes
    assert not any(ch.isdigit() for ch in notes)
    assert "history" not in " ".join(
        _profile_follow.unknown_values_notes(plan.table("member").plan, "member")
    )


# ---- the command line -------------------------------------------------------------------------


def _write_csv(path: Path, names: Sequence[str], rows: Sequence[Mapping[str, Optional[str]]]):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(names)
        for row in rows:
            writer.writerow(["" if row[name] is None else row[name] for name in names])


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = yaml.safe_dump(gym.CONTRACT, sort_keys=False)
    (tmp_path / "gym.fluid.yaml").write_text(text, encoding="utf-8")
    request = gym.request(history={}, with_visits=True)
    request["tables"]["visit"]["history"] = {}
    request["tables"]["member"]["history"] = {}
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


def test_the_report_names_each_member_and_the_asks_left_out_and_prints_no_count(
    workspace, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    assert main(_argv()) == 0
    seen = capsys.readouterr()
    text = seen.out + seen.err + caplog.text
    flat = " ".join(text.split())
    for name in ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"):
        assert f"{TABLE}/history/{name}" in flat, name
    assert f"{TABLE}/history/transitions/tier" in flat and f"{TABLE}/history/reasons" in flat
    # the asks the release does not hold are named with why, never with a count
    assert "history of member" in flat
    assert "history of visit" in flat
    for place in {str(workspace), str(workspace.resolve())}:
        text = text.replace(place, "<workspace>")
    shown = set(re.findall(r"(?<![\w.])\d+(?![\w.])", text))
    assert "11" not in shown  # the versions of the gym
    written = json.loads((workspace / "member.profile.json").read_text(encoding="utf-8"))
    assert written["_schemaURL"] == document.SCHEMA_URL_1_5_0
    assert "history" in written["tables"][TABLE]
    record = json.loads((workspace / "rec.json").read_text(encoding="utf-8"))
    assert any(each.get("kind") == "history" for each in record["tables"])


def test_the_report_names_the_format_the_document_names(workspace, capsys):
    assert main(_argv()) == 0
    seen = capsys.readouterr()
    flat = " ".join((seen.out + seen.err).split())
    written = json.loads((workspace / "member.profile.json").read_text(encoding="utf-8"))
    assert written["_schemaURL"] == document.SCHEMA_URL_1_5_0
    assert "its privacy unit (format 1-5-0, PROFILE-SPEC section 3a)" in flat
    assert "format 1-4-0" not in flat


def test_rederive_draws_the_same_release_again_with_a_history(workspace, capsys):
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


def test_a_release_whose_records_put_an_entity_under_two_parents_is_refused_and_writes_nothing(
    workspace, capsys
):
    rows = gym.versions()
    rows[1] = dict(rows[1], member_id="2")
    _write_csv(workspace / f"{TABLE}.csv", gym.VERSION_COLUMNS, rows)
    assert main(_argv()) != 0
    assert not (workspace / "member.profile.json").exists()
    seen = capsys.readouterr()
    assert "profile_release_entity_units" in seen.out + seen.err


def test_a_noisy_history_beside_a_clique_selection_names_format_1_5_0_and_the_engine_reads_it():
    # Selection itself needs OpenDP; a re-derivation hands the cliques in, which is what the
    # document is written from. The document holds both forms and the reader must accept it.
    asked = gym.request(history={})
    asked["tables"][TABLE]["columns"]["access"]["statistics"] = ["levelHistogram/1"]
    plan = gym.follow_plan(asked)
    assert plan.selects
    backend = FakeBackend()
    rows = fr.encode_follow_rows(plan, gym.read_of(plan), backend)
    drawn = fr.draw_follow(
        plan,
        rows,
        SEED,
        backend,
        cliques={TABLE: (("tier", "access"),)},
        producer=gym.PRODUCER,
        selection_version="1.0.0",
    )
    table = drawn.document["tables"][TABLE]
    assert "cliqueSelection" in table and "history" in table
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_5_0
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))


def test_a_tracked_column_named_several_leaves_reasons_out_and_the_engine_reads_the_rest():
    contract = gym.contract()
    for each in contract["exposes"][1]["contract"]["schema"]:
        if each["name"] == "access":
            each["name"] = "several"
    asked = gym.request(history={})
    asked["tables"][TABLE]["columns"]["several"] = asked["tables"][TABLE]["columns"].pop("access")
    plan = gym.follow_plan(asked, contract_=contract)
    held = gym.tables()
    held[TABLE] = [
        {("several" if name == "access" else name): value for name, value in row.items()}
        for row in held[TABLE]
    ]
    drawn, _ = gym.draw(plan, held)
    written = gym.history_of(drawn)
    assert set(written["columns"]) == {"tier", "several"}
    assert "reasons" not in written
    assert any(
        what == f"history of {TABLE} reasons" and "several" in why
        for what, why in plan.not_released
    )
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))


def test_the_history_note_says_a_truncated_entity_is_described_by_what_is_kept():
    from fluid_build.cli import _profile_follow

    notes = " ".join(_profile_follow.unknown_values_notes(_plan().table(TABLE).plan, TABLE))
    assert "more versions than the cap under one parent row is described by the versions kept" in (
        notes
    )
