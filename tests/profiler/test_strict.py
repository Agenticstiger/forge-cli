# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --request R --strict``: refuse, before any record is read, what the contract rules out.

An ask the contract and the request alone show cannot be formed (a date pair with a column that
is not a DATE, asks of a relationship the contract does not declare, a statistic a column's type
rules out) is refused before a record is read. An ask only the records leave unanswered (a small
cell, a column with no published span, a value that is not a whole day) is never refused: the
profile runs and names it under Not measured, as without ``--strict``.

``test_every_refused_ask_is_named_and_every_contract_ask_is_refused`` is the check that holds
the two sides together: over a set of requests, each ask the refusal names is named under Not
measured by a run without ``--strict`` with the same reason, and each ask that run names for a
reason the records do not decide is one the refusal names.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from fluid_build.cli import main
from fluid_build.profiler import query_rates, sources
from fluid_build.profiler import request as asked_for
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestDateLink,
    RequestDatePair,
    RequestEnd,
    RequestExposure,
    RequestFact,
    RequestLinked,
    RequestQuery,
    RequestRankCorrelation,
    RequestRelationship,
    RequestTable,
    RequestWorkload,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, ProfileError, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")

MEMBERS = 300
LOANS = 900
DAY = timedelta(days=1)


def _contract(*, with_branches: bool = True) -> Dict[str, Any]:
    exposes: List[Dict[str, Any]] = [
        {
            "exposeId": "members",
            "kind": "table",
            "contract": {
                "schema": [
                    {"name": "member_id", "type": "int"},
                    {"name": "joined", "type": "date"},
                    {"name": "lapsed", "type": "date"},
                    {"name": "visited", "type": "timestamp"},
                    {"name": "tier", "type": "enum('basic','plus')"},
                    {
                        "name": "score",
                        "type": "int",
                        "validationRules": [{"type": "range", "constraint": ">= 0 and <= 100"}],
                    },
                    {"name": "credit", "type": "decimal(10,2)"},
                    {"name": "name", "type": "string"},
                    {"name": "extras", "type": "json"},
                ],
                "invariants": [
                    {"id": "order", "check": "lapsed IS NULL OR lapsed >= joined"},
                ],
            },
        },
        {
            "exposeId": "loans",
            "kind": "table",
            "contract": {
                "schema": [
                    {"name": "loan_id", "type": "int"},
                    {"name": "member_id", "type": "int"},
                    {"name": "branch_id", "type": "int"},
                    {"name": "opened", "type": "date"},
                    {"name": "due", "type": "date"},
                    {"name": "stamped", "type": "timestamp"},
                    {"name": "pages", "type": "int"},
                    {"name": "fee", "type": "decimal(10,2)"},
                ],
                "invariants": [
                    {"id": "after", "check": "opened >= borrower.joined"},
                    {"id": "later", "check": "due >= opened"},
                ],
            },
            "semantics": {
                "relationships": [
                    {
                        "name": "borrower",
                        "to": "members",
                        "fromColumns": ["member_id"],
                        "toColumns": ["member_id"],
                        "cardinality": "many_to_one",
                    },
                    *(
                        [
                            {
                                "name": "home",
                                "to": "branches",
                                "fromColumns": ["branch_id"],
                                "toColumns": ["branch_id"],
                                "cardinality": "many_to_one",
                            }
                        ]
                        if with_branches
                        else []
                    ),
                ]
            },
        },
    ]
    if with_branches:
        exposes.append(
            {
                "exposeId": "branches",
                "kind": "table",
                "contract": {"schema": [{"name": "branch_id", "type": "int"}]},
            }
        )
    return {"fluidVersion": "0.7.7", "kind": "DataProduct", "id": "library", "exposes": exposes}


def _members() -> List[Dict[str, Any]]:
    return [
        {
            "member_id": n,
            "joined": (date(2023, 1, 1) + (n % 360) * DAY).isoformat(),
            "lapsed": (date(2024, 6, 1) + (n % 200) * DAY).isoformat() if n % 3 else None,
            "visited": f"2024-03-{1 + n % 20:02d}T10:00:00Z",
            "tier": ("basic", "plus")[n % 2],
            "score": n % 100,
            "credit": f"{n % 20}.50",
            "name": f"m{n}",
            "extras": None,
        }
        for n in range(MEMBERS)
    ]


def _loans() -> List[Dict[str, Any]]:
    rows = []
    for n in range(LOANS):
        opened = date(2024, 1, 1) + (n % 300) * DAY
        rows.append(
            {
                "loan_id": n,
                "member_id": n % MEMBERS,
                "branch_id": n % 3,
                "opened": opened.isoformat(),
                "due": (opened + (7 + n % 21) * DAY).isoformat(),
                "stamped": opened.isoformat() + "T09:00:00Z",
                "pages": n % 400,
                "fee": f"{n % 9}.25",
            }
        )
    return rows


def _write(path, rows) -> str:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def _sources(tmp_path, tables: Sequence[str]) -> List[ExposeSource]:
    rows = {"members": _members, "loans": _loans, "branches": lambda: [{"branch_id": 0}]}
    return [
        ExposeSource(name, _write(tmp_path / f"{name}.ndjson", rows[name]())) for name in tables
    ]


def _request(*tables: RequestTable, relationships: Sequence[RequestRelationship] = (), **more: Any):
    return Request(
        contract_digest=None,
        fractional_bits=16,
        tables=tables,
        relationships=tuple(relationships),
        **more,
    )


def _no_read(monkeypatch):
    def refuse(*args: Any, **kwargs: Any):
        raise AssertionError("a record was read")

    monkeypatch.setattr(sources, "read_columns", refuse)


def _dated_loans() -> RequestTable:
    return RequestTable(
        "loans",
        False,
        (
            RequestColumn("opened", "timestamp"),
            RequestColumn("due", "timestamp"),
            RequestColumn("stamped", "timestamp"),
        ),
        date_pairs=(RequestDatePair("opened", "due"), RequestDatePair("opened", "stamped")),
    )


# -- a date pair of a DATE and a timestamp column ---------------------------------------------


def test_a_date_pair_with_a_timestamp_column_is_refused_before_a_read(tmp_path, monkeypatch):
    request = _request(_dated_loans())
    contract = _contract(with_branches=False)
    exposes = _sources(tmp_path, ["loans"])
    named = profile_exposes(contract, exposes, backend=load_backend(), request=request)
    reason = dict(named.not_measured)["date pair opened to stamped of loans"]
    assert reason.startswith("its order is on the millisecond")
    assert "date pair opened to due of loans" not in dict(named.not_measured)

    _no_read(monkeypatch)
    with pytest.raises(ProfileError) as refused:
        profile_exposes(contract, exposes, backend=load_backend(), request=request, strict=True)
    assert refused.value.event == "profile_request_not_formed"
    assert "1 item" in str(refused.value)
    assert refused.value.context[reason] == "date pair opened to stamped of loans"
    assert refused.value.context["read"] == "nothing: no record was read"


def test_the_command_refuses_a_date_pair_with_a_timestamp_column(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    import yaml

    (tmp_path / "library.fluid.yaml").write_text(
        yaml.safe_dump(_contract(with_branches=False)), encoding="utf-8"
    )
    _write(tmp_path / "loans.ndjson", _loans())
    (tmp_path / "ask.yml").write_text(
        '"_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile-request/'
        '1-1-0/FluxProfileRequest.json"\n'
        '"encoding": {"fractionalBits": 16, "rounding": "floor"}\n'
        '"tables":\n'
        '  "loans":\n'
        '    "columns":\n'
        '      "opened": {"domain": "timestamp", "metrics": ["count"]}\n'
        '      "stamped": {"domain": "timestamp", "metrics": ["count"]}\n'
        '    "datePairs": [{"start": "opened", "end": "stamped"}]\n',
        encoding="utf-8",
    )
    argv = [
        "profile",
        "library.fluid.yaml",
        "--request",
        "ask.yml",
        "--source",
        "loans=loans.ndjson",
        "--out",
        "p.json",
    ]
    assert main([*argv, "--strict"]) == 1
    out = capsys.readouterr().out
    assert "profile_request_not_formed" in out and "date pair opened to stamped of loans" in out
    assert not (tmp_path / "p.json").exists()
    assert main(argv) == 0
    assert "Not measured: date pair opened to stamped of loans" in capsys.readouterr().out


# -- a relationship the contract does not declare ---------------------------------------------


def _undeclared() -> RequestRelationship:
    return RequestRelationship(
        RequestEnd("members", ("member_id",)),
        RequestEnd("loans", ("loan_id",)),
        ("marginalHistogram/1",),
        dates=(RequestDateLink("joined", "opened"),),
        exposure=(RequestExposure("joined", "lapsed"),),
    )


def _members_and_loans() -> Tuple[RequestTable, RequestTable]:
    return (
        RequestTable(
            "members",
            False,
            (
                RequestColumn("member_id", "number"),
                RequestColumn("joined", "timestamp", (), ("marginalHistogram/1",)),
                RequestColumn("lapsed", "timestamp"),
            ),
        ),
        RequestTable(
            "loans",
            False,
            (RequestColumn("loan_id", "number"), RequestColumn("opened", "timestamp")),
        ),
    )


def test_asks_of_an_undeclared_relationship_are_refused_each_by_name(tmp_path, monkeypatch):
    request = _request(*_members_and_loans(), relationships=[_undeclared()])
    contract = _contract(with_branches=False)
    exposes = _sources(tmp_path, ["members", "loans"])
    link = "loans(loan_id) to members(member_id)"
    wanted = {
        f"children per parent of {link}",
        f"dates members.joined to loans.opened of {link}",
        f"exposure members.joined to lapsed of {link}",
    }
    named = profile_exposes(contract, exposes, backend=load_backend(), request=request)
    undeclared = "the contract declares no such relationship"
    assert wanted <= {what for what, reason in named.not_measured if reason == undeclared}

    _no_read(monkeypatch)
    with pytest.raises(ProfileError) as refused:
        profile_exposes(contract, exposes, backend=load_backend(), request=request, strict=True)
    assert f"{len(wanted)} items" in str(refused.value)
    assert set(refused.value.context[undeclared].split("; ")) == wanted


# -- an ask only the records leave unanswered is never refused ---------------------------------


def test_what_only_the_records_decide_is_named_and_never_refused(tmp_path):
    # Twelve members join on the one day and no other day holds ten: the column has no
    # published span and the contract declares no range, so its exposure has no window; and
    # the last day bin of the pair of due dates holds three loans, fewer than the least cell.
    members = _members()
    for row in members:
        row["joined"] = (date(2023, 1, 1) + (int(row["member_id"]) * 37 % 3000) * DAY).isoformat()
    loans = _loans()
    for row in loans[:3]:
        row["due"] = (date.fromisoformat(row["opened"]) - DAY).isoformat()
    request = _request(
        RequestTable(
            "members",
            False,
            (
                RequestColumn("member_id", "number"),
                RequestColumn("joined", "timestamp", (), ("marginalHistogram/1",)),
            ),
        ),
        RequestTable(
            "loans",
            False,
            (
                RequestColumn("member_id", "number"),
                RequestColumn("opened", "timestamp"),
                RequestColumn("due", "timestamp"),
            ),
            date_pairs=(RequestDatePair("opened", "due"),),
        ),
        relationships=[
            RequestRelationship(
                RequestEnd("members", ("member_id",)),
                RequestEnd("loans", ("member_id",)),
                (),
                exposure=(RequestExposure("joined"),),
            )
        ],
    )
    exposes = [
        ExposeSource("members", _write(tmp_path / "m.ndjson", members)),
        ExposeSource("loans", _write(tmp_path / "l.ndjson", loans)),
    ]
    contract = _contract(with_branches=False)
    results = []
    for strict in (False, True):
        try:
            results.append(
                profile_exposes(
                    contract, exposes, backend=load_backend(), request=request, strict=strict
                )
            )
        except ProfileError as exc:
            pytest.fail(f"{exc} {exc.context}")
    for result in results:
        said = dict(result.not_measured)
        assert said["days of date pair opened to due of loans"].startswith("its first or last")
        what = next(each for each in said if each.startswith("exposure members.joined of"))
        assert said[what].startswith("an exposure states its window")
    assert results[0].canonical == results[1].canonical


# -- the two sides hold together -----------------------------------------------------------------


def _every_kind_of_ask() -> Request:
    metrics = ("count", "nullCount", "distinctCount", "sum", "min", "max", "quantiles", "median")
    statistics = (
        "mean/1",
        "marginalHistogram/1",
        "quantile/2",
        "quantile/1",
        "support/1",
        "levelHistogram/1",
        "kurtosis/1",
    )
    members = RequestTable(
        "members",
        True,
        (
            RequestColumn("member_id", "number", metrics, statistics),
            RequestColumn("joined", "timestamp", ("count", "sum"), statistics),
            RequestColumn("lapsed", "timestamp", ("count",), ("marginalHistogram/1",)),
            RequestColumn("visited", "timestamp", ("count",), statistics),
            RequestColumn("tier", "category", metrics, statistics),
            RequestColumn("score", "number", metrics, statistics),
            RequestColumn("credit", "number", metrics, statistics),
            RequestColumn("name", "category", ("count", "sum"), statistics),
            RequestColumn("extras", "category", ("count",), ("levelHistogram/1",)),
        ),
        rank_correlations=(
            RequestRankCorrelation("kendallTau/1", ("score", "credit", "tier", "member_id")),
        ),
        clique_columns=("tier", "score", "joined", "name", "credit"),
        date_pairs=(
            RequestDatePair("joined", "lapsed"),
            RequestDatePair("joined", "visited"),
            RequestDatePair("joined", "missing"),
            RequestDatePair("member_id", "joined"),
        ),
        calendar=("visited", "joined"),
        workload=(RequestWorkload(("tier", "name"), 2),),
    )
    loans = RequestTable(
        "loans",
        False,
        (
            RequestColumn("loan_id", "number", ("count",), statistics),
            RequestColumn("member_id", "number", ("count", "distinctCount"), statistics),
            RequestColumn("branch_id", "number", ("count",), ("mean/1",)),
            RequestColumn("opened", "timestamp", ("count",), ("marginalHistogram/1",)),
            RequestColumn("due", "timestamp", ("count",), ("quantile/2",)),
            RequestColumn("stamped", "timestamp", ("count",), ("marginalHistogram/1",)),
            RequestColumn("pages", "number", ("count", "sum"), statistics),
            RequestColumn("fee", "number", ("count",), ("mean/1", "marginalHistogram/1")),
        ),
        date_pairs=(RequestDatePair("opened", "due"), RequestDatePair("opened", "stamped")),
        calendar=("stamped", "opened"),
    )
    return _request(
        members,
        loans,
        relationships=[
            RequestRelationship(
                RequestEnd("members", ("member_id",)),
                RequestEnd("loans", ("member_id",)),
                ("marginalHistogram/1", "mean/1"),
                group_by=("tier", "name", "missing"),
                linked=(RequestLinked("fee", "tier"), RequestLinked("pages", "tier")),
                dates=(
                    RequestDateLink("joined", "opened"),
                    RequestDateLink("joined", "stamped"),
                    RequestDateLink("visited", "due"),
                    RequestDateLink("joined", "missing"),
                ),
                exposure=(
                    RequestExposure("joined", "lapsed"),
                    RequestExposure("joined"),
                    RequestExposure("visited"),
                ),
            ),
            _undeclared(),
            RequestRelationship(
                RequestEnd("branches", ("branch_id",)),
                RequestEnd("loans", ("branch_id",)),
                ("marginalHistogram/1",),
                group_by=("branch_id",),
                dates=(RequestDateLink("branch_id", "opened"),),
                exposure=(RequestExposure("branch_id"),),
            ),
        ],
        facts=(
            RequestFact("members.order", "members", "order"),
            RequestFact("loans.after", "loans", "after"),
        ),
        queries=(
            RequestQuery("recent", "SELECT count(*) AS n FROM loans", table="loans"),
            RequestQuery("files", "SELECT * FROM read_csv('/etc/hosts')", table="loans"),
        ),
    )


def _small_requests() -> List[Request]:
    members, loans = _members_and_loans()
    return [
        _request(_dated_loans()),
        _request(members, loans, relationships=[_undeclared()]),
        _request(members, loans),
    ]


def _captured(monkeypatch) -> List[List[Tuple[str, str]]]:
    asked: List[List[Tuple[str, str]]] = []
    original = asked_for.refuse_asks

    def spy(found: Sequence[Tuple[str, str]]) -> ProfileError:
        asked.append(list(found))
        return original(found)

    monkeypatch.setattr(asked_for, "refuse_asks", spy)
    return asked


def _sides_hold(contract, exposes, request, monkeypatch) -> List[Tuple[str, str]]:
    """Check the refusal against the profile without ``--strict``; the asks the refusal names."""
    named = profile_exposes(contract, exposes, backend=load_backend(), request=request)
    asked = _captured(monkeypatch)
    try:
        profile_exposes(contract, exposes, backend=load_backend(), request=request, strict=True)
    except ProfileError as exc:
        assert exc.event == "profile_request_not_formed"
    before = asked[0] if asked else []

    after = list(named.not_measured)
    not_read = {
        (table.plan.expose_id, name): why
        for table in named.tables
        for name, why in table.plan.skipped
    }
    left_out = {why for _, _, why in named.relationships_left_out}
    for what, reason in before:
        if (what, reason) in after:
            continue
        # A column the profile does not read has no line of its own among the asks left out: the
        # report names it as not profiled with its reason. So has a declared relationship it
        # does not read, under its own line.
        table, _, column = what.partition(" of ")[2].partition(".")
        assert not_read.get((table, column)) == reason or (
            what.startswith("children per parent of ") and reason in left_out
        ), (what, reason)
    for what, reason in after:
        assert asked_for.decided_by_records(reason) or (what, reason) in before, (what, reason)
    return before


@pytest.mark.parametrize("which", range(4))
def test_every_refused_ask_is_named_and_every_contract_ask_is_refused(tmp_path, monkeypatch, which):
    request = ([*_small_requests(), _every_kind_of_ask()])[which]
    names = [each.expose_id for each in request.tables]
    _sides_hold(_contract(), _sources(tmp_path, names), request, monkeypatch)


@pytest.mark.parametrize("with_asks", [False, True])
def test_the_same_holds_for_the_shop_request(tmp_path, monkeypatch, with_asks):
    import yaml

    from tests.profiler import test_request as shop

    text = shop._with_asks(shop.REQUEST) if with_asks else shop.REQUEST
    request = load_backend().read_request(text, name="shop.profile-request.yml")
    customers, orders = shop._rows()
    exposes = [
        ExposeSource("customers", _write(tmp_path / "customers.ndjson", customers)),
        ExposeSource("orders", _write(tmp_path / "orders.ndjson", orders)),
    ]
    before = _sides_hold(yaml.safe_load(shop.CONTRACT), exposes, request, monkeypatch)
    assert before  # the request asks a statistic of a key column, which the contract rules out


def test_the_matrix_holds_each_reason_the_contract_decides(tmp_path, monkeypatch):
    request = _every_kind_of_ask()
    asked = _captured(monkeypatch)
    with pytest.raises(ProfileError):
        profile_exposes(
            _contract(),
            _sources(tmp_path, ["members", "loans"]),
            backend=load_backend(),
            request=request,
            strict=True,
        )
    reasons = {reason for _, reason in asked[0]}
    assert not any(asked_for.decided_by_records(reason) for reason in reasons)
    for needle in (
        "its order is on the millisecond",
        "is not a column fluid profile reads",
        "a key of a counted relationship",
        "the contract declares no such relationship",
        "fluid profile has no output check for it",
        "sum is not formed for a category column",
        "a mean is formed for a number column only",
        "is not a number or timestamp column",
        "has no public levels or bins",
        "the relationship is not one whose two tables and keys the profile reads",
        "its target is not profiled in this call",
        "an exposure is formed over DATE columns",
        "fluid profile answers a query that keeps some of its unit's records: ",
        "the check refuses the query: ",
    ):
        assert any(needle in reason for reason in reasons), needle


def test_the_records_reasons_are_the_ones_decided_by_records():
    for reason in (
        "no non-null record to form it over",
        "no record forms it",
        "the output check withholds it",
        "a value of loans.due is not a whole day",
        "a key of members names more than one parent, so a child has no one parent",
        "an exposure states its window, and the parent's start has no published span or "
        "declared range",
        *query_rates.RECORD_REASONS,
    ):
        assert asked_for.decided_by_records(reason), reason
    for reason in (
        "a key of a counted relationship, which its children per parent describe",
        "the contract declares no such relationship",
        "a mean is formed for a number column only",
        "fluid profile answers a query that keeps some of its unit's records: it joins",
    ):
        assert not asked_for.decided_by_records(reason), reason


def test_strict_leaves_a_request_with_nothing_the_contract_rules_out(tmp_path):
    members, loans = _members_and_loans()
    request = _request(members, loans)
    result = profile_exposes(
        _contract(with_branches=False),
        _sources(tmp_path, ["members", "loans"]),
        backend=load_backend(),
        request=request,
        strict=True,
    )
    assert result.document["tables"]["members"]
