# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A private release of a column's noisy null count (``nullCount/2``, own rows or ``--follow``).

A request of format 1-3-0 may list ``nullCount/2`` for a column. The release then holds one more
count table of the column's table: two cells, the rows where the column is known and the rows where
it is NULL, under the sensitivity of any count table of that table (``m_T`` times one row, so a
squared L2 of ``m_T**2``). It has a share of its own, draws its noise from the stream of its label,
joins no clique and no consistency step, and the document that holds one names format 1-5-0.

``loan`` is the release table of the library fixture of ``test_release_dates_nulls``: a ``shelf``
the library does not always record, a ``returned_on`` that is empty while the loan is out and a
``scanned_at`` nobody has filled. A ``member`` is the privacy unit of the ``--follow`` releases.
"""

from __future__ import annotations

import json
import logging
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from fluid_build.cli import main
from fluid_build.profiler import document, sources
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import release, selection
from fluid_build.profiler.dp.discrete_gaussian import sample_discrete_gaussian
from fluid_build.profiler.dp.follows import parse_follow
from fluid_build.profiler.dp.randomness import SeededSource
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.test_release_dates_nulls import (  # noqa: F401  (workspace is a fixture)
    CONTRACT,
    FOLLOW,
    LOAN_COLUMNS,
    SEED,
    _argv,
    _columns,
    _event,
    _numbers,
    _request,
    _secrets,
    loans,
    members,
    workspace,
)

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

F = Fraction
PRODUCER = "https://example.test/producer"
REQUEST_URL_1_3_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-3-0/FluxProfileRequest.json"
)
NULLABLE = ("shelf", "returned_on")


def _asking(
    *columns: str,
    table: str = "loan",
    member: bool = False,
    member_pairs: Sequence[Tuple[str, str]] = (),
) -> Dict[str, Any]:
    """The library request of ``test_release_dates_nulls``, asking ``nullCount/2`` of *columns*."""
    built = _request(member=member, member_pairs=member_pairs)
    built["_schemaURL"] = REQUEST_URL_1_3_0
    for name in columns:
        asked = built["tables"][table]["columns"][name]
        asked["statistics"] = [*asked.get("statistics", ()), "nullCount/2"]
    return built


def _plan(*columns: str, rho: str = "1") -> release.ReleasePlan:
    backend = FakeBackend()
    asked = backend.read_request(json.dumps(_asking(*columns)), name="loans.profile-request.yml")
    return release.plan_release(CONTRACT, asked, backend, unit="loan", rho=release.parse_rho(rho))


def _draw(plan: release.ReleasePlan, rows=None, seed: bytes = SEED):
    backend = FakeBackend()
    encoded = release.encode_rows(plan, _columns(rows or loans(), LOAN_COLUMNS), backend)
    drawn = release.draw(plan, encoded, seed, backend, cliques=(), producer=PRODUCER)
    return drawn, encoded


def _follow_draw(plan: fr.FollowReleasePlan, seed: bytes = SEED):
    backend = FakeBackend()
    wanted = dict(plan.reads)
    values = {"member": members(), "loan": loans()}
    read = {
        table: sources.SourceColumns(
            description="rows",
            row_count=len(values[table]),
            values={
                column.name: [row[column.name] for row in values[table]] for column in wanted[table]
            },
        )
        for table in wanted
    }
    rows = fr.encode_follow_rows(plan, read, backend)
    drawn = fr.draw_follow(plan, rows, seed, backend, cliques={}, producer=PRODUCER)
    return drawn, rows


def _part(drawn: Any, label: str) -> release.ReleasedTable:
    return next(each for each in drawn.tables if each.label == label)


def _null_count(document_: Mapping[str, Any], table: str, column: str) -> Mapping[str, Any]:
    (found,) = [
        each
        for each in document_["tables"][table]["columns"][column]["statistics"]
        if each["definition"] == "nullCount/2"
    ]
    return found  # type: ignore[no-any-return]


def _noise(label: str, scale: str, count: int, seed: bytes = SEED) -> List[int]:
    """What the stream of *label* adds to *count* cells with a discrete Gaussian of *scale*."""
    sigma2 = Fraction(scale) ** 2
    source = SeededSource(seed, label)
    return [sample_discrete_gaussian(sigma2, source) for _ in range(count)]


# ---- own rows: the part -------------------------------------------------------------------------


def test_a_null_count_ask_adds_one_part_for_each_column_with_a_share_of_its_own():
    plain = _plan()
    plan = _plan(*NULLABLE)
    assert plan.null_counts == NULLABLE
    parts = release.release_parts(plan, ())
    added = [each for each in parts if each.kind == release.NULLCOUNT]
    assert [(each.label, each.columns, each.sizes, each.cells) for each in added] == [
        ("nullCount/shelf", ("shelf",), (2,), 2),
        ("nullCount/returned_on", ("returned_on",), (2,), 2),
    ]
    # the others are the parts the release held without the ask, in their place
    assert [each for each in parts if each.kind != release.NULLCOUNT] == list(
        release.release_parts(plain, ())
    )
    drawn, _ = _draw(plan)
    base, _ = _draw(plain)
    assert len(drawn.tables) == len(base.tables) + 2
    assert drawn.share == release.split_budget(plan.rho, False, len(drawn.tables))[1]
    assert drawn.share < base.share
    assert sum((F(each.share) for each in drawn.tables), F(0)) <= plan.rho


def test_a_null_count_is_charged_as_a_count_table_of_its_table():
    plan = _plan("shelf")
    drawn, _ = _draw(plan)
    part = _part(drawn, "nullCount/shelf")
    assert part.kind == release.NULLCOUNT and part.cells == 2
    assert F(part.scale) * 10**6 == release.scale_units(F(part.share), plan.sensitivity)
    assert part.scale == _part(drawn, "noise/kind").scale  # the same sensitivity and share


def test_a_null_count_is_written_as_the_column_statistic_and_names_format_1_5_0():
    plan = _plan(*NULLABLE)
    drawn, _ = _draw(plan)
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_5_0
    for name in NULLABLE:
        part = _part(drawn, f"nullCount/{name}")
        found = _null_count(drawn.document, "loan", name)
        (group,) = found["groups"]
        assert found == {
            "definition": "nullCount/2",
            "levels": ["known", "null"],
            "sampleSize": {"min": 0},
            "disclosure": "noisy",
            "noise": {
                "mechanism": "discreteGaussian",
                "scale": part.scale,
                "privacyLoss": {"rho": part.share},
            },
            "groups": [{"group": "", "status": "F", "noisyCounts": group["noisyCounts"]}],
        }
        assert len(group["noisyCounts"]) == 2
    # the column's own histogram stays beside it
    shelf = drawn.document["tables"]["loan"]["columns"]["shelf"]["statistics"]
    assert [each["definition"] for each in shelf] == ["levelHistogram/1", "nullCount/2"]


def test_the_engine_reads_the_document_and_puts_the_statistic_among_the_column_null_counts():
    from flux_stats.profile import load_profile_text

    drawn, _ = _draw(_plan(*NULLABLE))
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))
    loaded, _ = load_profile_text(drawn.canonical.decode("utf-8"))
    for name in NULLABLE:
        (read,) = loaded.tables["loan"].columns[name].null_counts
        written = _null_count(drawn.document, "loan", name)["groups"][0]["noisyCounts"]
        assert list(read.group.noisy) == [F(text) for text in written]


def test_the_cells_count_the_rows_known_and_null_over_the_rows_kept():
    plan = _plan(*NULLABLE, "scanned_at")
    drawn, encoded = _draw(plan)
    rows = loans()
    kept = list(range(len(rows)))
    exact = release.measure_counts(drawn.plan, encoded, kept, drawn.tables)
    for name in (*NULLABLE, "scanned_at"):
        known = sum(1 for row in rows if row[name] is not None)
        assert exact[f"nullCount/{name}"] == [known, len(rows) - known]
    assert exact["nullCount/scanned_at"][0] == 0
    # a row that is not kept counts in no cell
    assert (
        sum(
            release.measure_counts(drawn.plan, encoded, kept[:100], drawn.tables)["nullCount/shelf"]
        )
        == 100
    )


def test_the_noise_is_the_stream_of_the_label_and_the_part_joins_no_consistency_step():
    plan = _plan("shelf", "kind")
    drawn, encoded = _draw(plan)
    part = _part(drawn, "nullCount/shelf")
    assert not release.agrees(plan, part)
    exact = release.measure_counts(
        drawn.plan, encoded, list(range(len(encoded.contents))), drawn.tables
    )["nullCount/shelf"]
    expected = [
        count + draw for count, draw in zip(exact, _noise(part.label, part.scale, 2), strict=True)
    ]
    written = _null_count(drawn.document, "loan", "shelf")["groups"][0]["noisyCounts"]
    assert [F(text) for text in written] == [F(value) for value in expected]


def test_a_column_of_a_required_value_is_asked_and_released_like_any_other():
    plan = _plan("kind")
    assert plan.null_counts == ("kind",)
    drawn, _ = _draw(plan)
    assert _null_count(drawn.document, "loan", "kind")["levels"] == ["known", "null"]


def test_a_release_without_the_ask_is_the_release_it_was():
    from tests.profiler.test_release_cells import OWN_DIGEST

    backend = FakeBackend()
    asked = backend.read_request(json.dumps(_asking()), name="loans.profile-request.yml")
    plan = release.plan_release(CONTRACT, asked, backend, unit="loan", rho=release.parse_rho("1"))
    assert plan.null_counts == ()
    drawn, _ = _draw(plan)
    assert drawn.digest == OWN_DIGEST
    assert drawn.document["_schemaURL"] != document.SCHEMA_URL_1_5_0
    assert "nullCount" not in drawn.canonical.decode("utf-8")


def test_an_ask_the_release_cannot_hold_names_why_and_changes_no_byte():
    base, _ = _draw(_plan())
    plan = _plan("loan_id", "member_id")  # the primary key, and a key of a relationship
    assert plan.null_counts == ()
    said = dict(plan.not_released)
    for name in ("loan_id", "member_id"):
        assert "key" in said[f"nullCount/2 of loan.{name}"]
        assert "format 1-2-0 holds no noisy form" not in said[f"nullCount/2 of loan.{name}"]
    drawn, _ = _draw(plan)
    assert drawn.canonical == base.canonical


def test_a_null_count_of_a_column_released_nowhere_else_is_still_released():
    # scanned_at lists no histogram: its null count needs no public levels or bins
    plan = _plan("scanned_at")
    assert plan.null_counts == ("scanned_at",)
    assert "scanned_at" not in {each.name for each in plan.columns}
    drawn, _ = _draw(plan)
    assert _null_count(drawn.document, "loan", "scanned_at")["groups"][0]["status"] == "F"


# ---- own rows: the cap, the record --------------------------------------------------------------


def test_the_null_counts_are_added_units_after_the_nullable_columns():
    plan = _plan(*NULLABLE)
    parts = release.release_parts(plan, ())
    pairs, columns, nulls = release.added_units(plan, parts)
    assert [[part.label for part in unit] for unit in pairs] == [
        ["pair/days/borrowed_on/due_on", "pair/order/borrowed_on/due_on"],
        ["pair/days/borrowed_on/returned_on", "pair/order/borrowed_on/returned_on"],
    ]
    assert [[part.label for part in unit] for unit in columns] == [
        ["noise/shelf"],
        ["noise/returned_on"],
    ]
    assert [[part.label for part in unit] for unit in nulls] == [
        ["nullCount/shelf"],
        ["nullCount/returned_on"],
    ]


def test_a_null_count_that_does_not_fit_the_cap_is_left_out_with_the_ones_after_it(monkeypatch):
    plan = _plan(*NULLABLE)
    cells = {part.label: part.cells for part in release.release_parts(plan, ())}
    everything = sum(cells.values())
    cap = everything - cells["nullCount/returned_on"]
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", cap)
    plan = _plan(*NULLABLE)
    drawn, _ = _draw(plan)
    reason = release.left_out_reason()
    assert dict(drawn.left_out) == {"nullCount/returned_on": reason}
    labels = [each.label for each in drawn.tables]
    assert "nullCount/shelf" in labels and "nullCount/returned_on" not in labels
    assert [
        each["definition"]
        for each in drawn.document["tables"]["loan"]["columns"]["returned_on"]["statistics"]
    ] == ["marginalHistogram/1"]
    assert drawn.plan is not None and drawn.plan.null_counts == ("shelf",)
    record = release.build_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    assert release.check_record(plan, record) == ()


def test_the_record_carries_the_kind_and_the_document_is_drawn_again_byte_for_byte():
    plan = _plan(*NULLABLE)
    drawn, encoded = _draw(plan)
    record = release.build_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    stated = [each for each in record.tables if each["label"].startswith("nullCount/")]
    assert [(each["label"], each["columns"], each["kind"]) for each in stated] == [
        ("nullCount/shelf", ["shelf"], "nullCount"),
        ("nullCount/returned_on", ["returned_on"], "nullCount"),
    ]
    assert release.check_record(plan, record) == ()
    again = release.draw(plan, encoded, SEED, FakeBackend(), cliques=(), producer=PRODUCER)
    assert again.canonical == drawn.canonical and again.digest == drawn.digest


def test_a_record_that_does_not_state_the_null_counts_of_the_plan_is_refused():
    plan = _plan(*NULLABLE)
    drawn, _ = _draw(plan)
    record = release.build_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    other = _plan("shelf")
    assert _event(lambda: release.check_record(other, record)) == "profile_release_record_mismatch"
    plain = _plan()
    assert _event(lambda: release.check_record(plain, record)) == "profile_release_record_mismatch"


# ---- a unit that follows its child rows -------------------------------------------------------------


def test_a_followed_table_charges_its_null_counts_the_reach_of_the_table():
    plan = _follow_plan_of({"loan": ("shelf", "returned_on"), "member": ("branch",)})
    assert plan.table("loan").plan.null_counts == ("shelf", "returned_on")
    assert plan.table("member").plan.null_counts == ("branch",)
    drawn, _ = _follow_draw(plan)
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_5_0
    loan = _part(drawn, "loan/nullCount/shelf")
    assert loan.table == "loan" and loan.sensitivity == 9  # m = 3
    assert F(loan.scale) * 10**6 == release.scale_units(F(loan.share), 9)
    member = _part(drawn, "member/nullCount/branch")
    assert member.sensitivity == 1
    assert F(member.scale) * 10**6 == release.scale_units(F(member.share), 1)
    assert member.scale != loan.scale
    entry = _null_count(drawn.document, "loan", "shelf")
    assert entry["noise"]["scale"] == loan.scale and entry["levels"] == ["known", "null"]
    assert _null_count(drawn.document, "member", "branch")["noise"]["scale"] == member.scale
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))
    assert sum((F(each.share) for each in drawn.tables), F(0)) <= plan.rho


def test_the_charge_of_a_followed_null_count_is_that_of_the_cap_of_the_followed_table():
    plan = _follow_plan_of({"loan": ("shelf",)}, follow="member.member_id=loan.member_id:2")
    assert plan.reach == {"member": 1, "loan": 2}
    drawn, _ = _follow_draw(plan)
    part = _part(drawn, "loan/nullCount/shelf")
    assert part.sensitivity == 4
    assert F(part.scale) * 10**6 == release.scale_units(F(part.share), 4)


def test_a_followed_null_count_counts_the_rows_kept_after_the_truncation():
    plan = _follow_plan_of({"loan": ("shelf",)})
    drawn, rows = _follow_draw(plan)
    kept = fr.truncated(plan, rows, SEED)
    _, tables = fr.describe(plan, {})
    exact = fr.measure_counts(plan, rows, kept, tables, FakeBackend())["loan/nullCount/shelf"]
    chosen = [loans()[index] for index in kept["loan"]]
    known = sum(1 for row in chosen if row["shelf"] is not None)
    assert exact == [known, len(chosen) - known]
    assert known + (len(chosen) - known) < len(loans())  # the cap of 3 per member dropped rows
    part = _part(drawn, "loan/nullCount/shelf")
    assert not release.agrees(plan.table("loan").plan, part)


def test_a_followed_null_count_is_an_added_unit_before_the_link_statistics(monkeypatch):
    plan = _follow_plan_of(
        {"loan": ("shelf",), "member": ("branch",)},
        relationships=[
            {
                "parent": {"table": "member", "columns": ["member_id"]},
                "child": {"table": "loan", "columns": ["member_id"]},
                "statistics": ["marginalHistogram/1"],
                "linked": [{"column": "shelf", "parentColumn": "branch"}],
            }
        ],
    )
    (link,) = plan.links
    cells = {each.part.label: each.part.cells for each in fr.follow_parts(plan, {})}
    total = sum(cells.values())
    # the link is the last of the added parts: it is the first left out
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", total - cells[link.label])
    _, left = fr.admit(plan, {})
    assert [label for label, _ in left] == [link.label]
    # then the null counts, the last of them first (the tables in the plan's order)
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", total - cells[link.label] - 2)
    kept, left = fr.admit(plan, {})
    assert [label for label, _ in left] == ["loan/nullCount/shelf", link.label]
    assert kept.table("member").plan.null_counts == ("branch",)
    assert kept.table("loan").plan.null_counts == ()
    drawn, _ = _follow_draw(plan)
    assert [label for label, _ in drawn.left_out] == ["loan/nullCount/shelf", link.label]
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    assert fr.check_follow_record(plan, record) == {}


def test_a_followed_release_records_the_null_counts_and_draws_the_same_again():
    plan = _follow_plan_of({"loan": ("shelf",), "member": ("branch",)})
    drawn, _ = _follow_draw(plan)
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    kinds = {each["label"]: each["kind"] for each in record.tables if "kind" in each}
    assert kinds["loan/nullCount/shelf"] == "nullCount"
    assert fr.check_follow_record(plan, record) == {}
    other = _follow_plan_of({"loan": ("shelf",)})
    with pytest.raises(release.ReleaseRefused) as refused:
        fr.check_follow_record(other, record)
    assert refused.value.event == "profile_release_record_mismatch"
    assert _follow_draw(plan)[0].digest == drawn.digest


def test_a_followed_release_without_the_ask_is_the_release_it_was():
    from tests.profiler.test_release_cells import FOLLOW_DIGEST

    plan = _follow_plan_of({})
    drawn, _ = _follow_draw(plan)
    assert drawn.digest == FOLLOW_DIGEST
    assert drawn.document["_schemaURL"] == document.SCHEMA_URL_1_4_0


def test_a_followed_ask_on_a_key_column_is_named_and_changes_no_byte():
    base, _ = _follow_draw(_follow_plan_of({}))
    plan = _follow_plan_of({"loan": ("member_id",)})
    assert plan.table("loan").plan.null_counts == ()
    assert "key" in dict(plan.not_released)["nullCount/2 of loan.member_id"]
    assert _follow_draw(plan)[0].canonical == base.canonical


def _follow_plan_of(
    asks: Mapping[str, Sequence[str]],
    follow: str = FOLLOW,
    relationships: Optional[Sequence[Mapping[str, Any]]] = None,
) -> fr.FollowReleasePlan:
    backend = FakeBackend()
    request = _request(member=True, member_pairs=(("joined_on", "left_on"),))
    request["_schemaURL"] = REQUEST_URL_1_3_0
    if relationships:
        request["relationships"] = list(relationships)
    for table, names in asks.items():
        for name in names:
            asked = request["tables"][table]["columns"][name]
            asked["statistics"] = [*asked.get("statistics", ()), "nullCount/2"]
    return fr.plan_follow_release(
        CONTRACT,
        backend.read_request(json.dumps(request), name="loans.profile-request.yml"),
        backend,
        unit="member",
        rho=release.parse_rho("1"),
        follows=(parse_follow(follow),),
    )


# ---- how the report words it -------------------------------------------------------------------


def test_the_report_names_a_null_count_part_by_its_label_as_it_names_a_date_pair():
    from fluid_build.cli import _profile_follow

    assert _profile_follow.part_words("nullCount/shelf") == "null count of shelf"
    assert _profile_follow.part_words("loan/nullCount/shelf") == "null count of shelf of loan"
    assert _profile_follow.part_words("pair/days/a/b") == "date pair a to b"
    left = [("loan/nullCount/shelf", "why"), ("loan/noise/shelf", "why")]
    assert _profile_follow.left_out_words(left) == [
        ("null count of shelf of loan", "why"),
        ("shelf of loan", "why"),
    ]


def test_the_notes_say_a_null_count_is_released_only_where_one_is_asked():
    from fluid_build.cli import _profile_follow

    plain = " ".join(_profile_follow.unknown_values_notes(_plan()))
    assert "no null count or share of nulls is released" in plain
    asked = " ".join(_profile_follow.unknown_values_notes(_plan("shelf", "kind")))
    assert "no null count" not in asked
    assert "null count of kind, shelf" in asked and "two cells" in asked


# ---- the command line -------------------------------------------------------------------------------


@pytest.fixture
def asking(workspace):
    """The library workspace with a request that asks the null count of a nullable column."""
    import yaml

    path = workspace / "library.profile-request.yml"
    request = yaml.safe_load(path.read_text(encoding="utf-8"))
    request["_schemaURL"] = REQUEST_URL_1_3_0
    request["tables"]["loan"]["columns"]["shelf"]["statistics"].append("nullCount/2")
    request["tables"]["loan"]["columns"]["returned_on"]["statistics"].append("nullCount/2")
    request["tables"]["member"]["columns"]["branch"]["statistics"].append("nullCount/2")
    path.write_text(yaml.safe_dump(request, sort_keys=False), encoding="utf-8")
    return workspace


@pytest.mark.parametrize("follow", [False, True], ids=["own rows", "follow"])
def test_the_report_names_each_null_count_and_prints_no_count(asking, capsys, caplog, follow):
    caplog.set_level(logging.DEBUG)
    assert main(_argv(follow=follow)) == 0
    seen = capsys.readouterr()
    text = seen.out + seen.err + caplog.text
    flat = " ".join(text.split())
    assert "null count of shelf" in flat and "null count of returned_on" in flat
    assert ("null count of branch" in flat) == follow
    shown = _numbers(text, asking)
    for secret in _secrets():
        assert str(secret) not in shown, secret
    name = "member" if follow else "loan"
    written = json.loads((asking / f"{name}.profile.json").read_text(encoding="utf-8"))
    assert written["_schemaURL"] == document.SCHEMA_URL_1_5_0
    record = json.loads((asking / "rec.json").read_text(encoding="utf-8"))
    record.pop("seed")
    for secret in _secrets():
        assert f" {secret}" not in json.dumps(record).replace('"', " ")


@pytest.mark.parametrize("follow", [False, True], ids=["own rows", "follow"])
def test_rederive_draws_the_same_release_again_with_null_counts(asking, capsys, follow):
    assert main(_argv(follow=follow)) == 0
    capsys.readouterr()
    name = "member" if follow else "loan"
    first = (asking / f"{name}.profile.json").read_bytes()
    (asking / f"{name}.profile.json").unlink()
    argv = _argv(follow=follow, record="rec.json")
    assert main([*argv, "--rederive"]) == 0
    assert (asking / f"{name}.profile.json").read_bytes() == first


def test_the_null_count_ask_is_no_longer_named_as_a_form_the_format_lacks(asking, capsys):
    assert main(_argv()) == 0
    flat = " ".join((lambda s: s.out + s.err)(capsys.readouterr()).split())
    assert "nullCount/2 of loan.shelf" not in flat
    assert "nullCount/2 of loan.returned_on" not in flat
