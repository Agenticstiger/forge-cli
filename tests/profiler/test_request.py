# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --request``: measure what a FLUX profile request lists, and nothing else.

The fixture in ``tests/profiler/fixtures/request/`` is a small contract (``shop.fluid.yaml``:
two exposes with a declared relationship, an enum, a ranged and an unranged integer, a decimal,
a timestamp, a boolean, a string, keys and one invariant) and the request the engine writes
for it. The request was made with::

    fluxsim init <dir>/shop --contract tests/profiler/fixtures/request/shop.fluid.yaml

which writes ``<dir>/shop.profile-request.yml``; that file is copied as it was written, so its
contract digest is the one ``fluxsim init`` computed. ``test_forge_takes_the_contract_digest_
fluxsim_init_took`` pins that forge computes the same one.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from fluid_build.cli import main
from fluid_build.profiler import _stats
from fluid_build.profiler import request as asked_for
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestDateLink,
    RequestDatePair,
    RequestEnd,
    RequestExposure,
    RequestRelationship,
    RequestTable,
    RequestWorkload,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, ProfileError, profile_exposes
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.request")

FIXTURES = Path(__file__).parent / "fixtures" / "request"
CONTRACT = (FIXTURES / "shop.fluid.yaml").read_text(encoding="utf-8")
REQUEST = (FIXTURES / "shop.profile-request.yml").read_text(encoding="utf-8")


def _rows():
    customers = [
        {"customer_id": n, "region": ["north", "south", "west"][n % 3]} for n in range(100)
    ]
    orders = [
        {
            "order_id": n,
            "customer_id": n % 80,
            "status": ["open", "paid", "void"][n % 3],
            "quantity": n % 50,
            "units": n % 7,
            "amount": f"{n % 20}.50",
            "placed_at": f"2026-07-{1 + n % 20:02d}T10:00:00.000Z",
            "paid": n % 2 == 0,
            "note": "n",
        }
        for n in range(300)
    ]
    return customers, orders


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shop.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "shop.profile-request.yml").write_text(REQUEST, encoding="utf-8")
    customers, orders = _rows()
    for name, rows in (("customers", customers), ("orders", orders)):
        (tmp_path / f"{name}.ndjson").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    return tmp_path


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


def _argv(*more, request="shop.profile-request.yml"):
    return [
        "profile",
        "shop.fluid.yaml",
        "--request",
        request,
        "--source",
        "customers=customers.ndjson",
        "--source",
        "orders=orders.ndjson",
        *more,
    ]


def _contract(workspace):
    import yaml

    return yaml.safe_load((workspace / "shop.fluid.yaml").read_text(encoding="utf-8"))


def _profile(workspace, request, **options):
    return profile_exposes(
        _contract(workspace),
        [
            ExposeSource(table.expose_id, str(workspace / f"{table.expose_id}.ndjson"))
            for table in request.tables
        ],
        backend=FakeBackend(),
        request=request,
        **options,
    )


def _asked(workspace):
    return FakeBackend().read_request(REQUEST, name="shop.profile-request.yml")


def test_forge_takes_the_contract_digest_fluxsim_init_took(workspace):
    asked = _asked(workspace)
    for backend in (FakeBackend(), load_backend()):
        assert backend.contract_digest(CONTRACT, name="shop.fluid.yaml") == asked.contract_digest


def test_the_profile_answers_exactly_what_the_request_lists(workspace):
    asked = _asked(workspace)
    result = _profile(workspace, asked)
    document = result.document
    assert set(document["tables"]) == {"customers", "orders"}
    for table in asked.tables:
        entry = document["tables"][table.expose_id]
        assert ("rowCount" in entry) == table.row_count
        assert set(entry["columns"]) == {column.name for column in table.columns}
        for column in table.columns:
            shown = entry["columns"][column.name]
            assert set(shown["metrics"]) <= set(column.metrics)
            for statistic in shown.get("statistics", ()):
                # Listed for the column, and ungrouped: a request asks for no grouping.
                assert statistic["definition"] in column.statistics
                assert "groupBy" not in statistic
    # The boolean and the string list no statistic, so none is written.
    for name in ("paid", "note"):
        assert "statistics" not in document["tables"]["orders"]["columns"][name]
    (link,) = document["relationships"]
    assert [each["definition"] for each in link["statistics"]] == ["marginalHistogram/1"]
    assert result.fractional_bits == asked.fractional_bits


def test_every_listed_item_left_out_has_its_reason(workspace):
    result = _profile(workspace, _asked(workspace))
    said = dict(result.not_measured)
    assert "aggregate reads child rows" in said["fact orders.lines_counted"]
    assert "row order" in said["fact orders.customer_fixes_status"]
    assert "fact orders.quantity_not_above_units" not in said
    expected = {
        "marginalHistogram/1 of orders.units": "no declared range",
        "quantile/2 of orders.quantity": "never both on one set of bins",
        "mean/1 of orders.placed_at": "number column only",
        "support/1 of orders.status": "level histogram",
        "quantile/2 of customers.customer_id": "of a counted relationship",
        "mean/1 of orders.customer_id": "of a counted relationship",
        "mean/1 of orders.quantity": "declared range",
    }
    for what, words in expected.items():
        assert words in said[what], (what, said.get(what))
    # Nothing is reported that the profile holds.
    published = {
        (name, each["definition"])
        for name, column in result.document["tables"]["orders"]["columns"].items()
        for each in column.get("statistics", ())
    }
    assert ("quantity", "marginalHistogram/1") in published
    assert not [what for what in said if what == "marginalHistogram/1 of orders.quantity"]
    assert any("children per parent" in what and "mean/1" in what for what in said)


def test_a_narrow_request_measures_nothing_it_does_not_list(workspace):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "orders",
                False,
                (
                    RequestColumn("units", "number", ("count",), ("mean/1",)),
                    RequestColumn("paid", "category", ("nullCount",)),
                ),
            ),
        ),
    )
    result = _profile(workspace, asked)
    table = result.document["tables"]["orders"]
    assert "rowCount" not in table and "relationships" not in result.document
    assert set(table["columns"]) == {"units", "paid"}
    assert set(table["columns"]["units"]["metrics"]) == {"count"}
    assert [s["definition"] for s in table["columns"]["units"]["statistics"]] == ["mean/1"]
    assert set(table["columns"]["paid"]["metrics"]) == {"nullCount"}
    assert not result.not_measured


def test_a_listed_relationship_the_contract_does_not_declare_is_reported_not_refused(workspace):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable("customers", False, (RequestColumn("region", "category"),)),
            RequestTable("orders", False, (RequestColumn("units", "number"),)),
        ),
        relationships=(
            RequestRelationship(
                RequestEnd("customers", ("region",)),
                RequestEnd("orders", ("units",)),
                ("marginalHistogram/1",),
            ),
        ),
    )
    result = _profile(workspace, asked)
    assert "relationships" not in result.document
    ((what, reason),) = result.not_measured
    assert "children per parent" in what and "declares no such relationship" in reason


def _with_asks(text):
    """The fixture request in format 1-1-0, also asking a calendar of orders and one query."""
    text = text.replace("/profile-request/1-0-0/", "/profile-request/1-1-0/")
    marker = '    "rowCount": true\n"relationships":'
    assert text.count(marker) == 1
    text = text.replace(
        marker, '    "rowCount": true\n    "calendar":\n      - "placed_at"\n"relationships":'
    )
    return text + (
        '"queries":\n'
        '  - "name": "paid_orders"\n'
        '    "sql": "SELECT order_id FROM orders WHERE paid"\n'
        '    "unit":\n'
        '      "table": "orders"\n'
    )


def test_the_reader_keeps_every_ask_of_the_request_format(workspace):
    asked = FakeBackend().read_request(_with_asks(REQUEST), name="asks.yml")
    assert asked.table("orders").calendar == ("placed_at",)
    assert asked.table("customers").calendar == ()
    # A query is kept by its name alone: its text is the consumers', and never kept.
    assert asked.queries == ("paid_orders",)
    assert "SELECT" not in repr(asked)


def test_every_ask_fluid_profile_does_not_form_is_named_with_its_reason():
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "orders",
                False,
                (RequestColumn("placed_at", "timestamp"), RequestColumn("status", "category")),
                date_pairs=(RequestDatePair("placed_at", "shipped_at"),),
                calendar=("placed_at",),
                workload=(RequestWorkload(("status",), 3),),
            ),
        ),
        relationships=(
            RequestRelationship(
                RequestEnd("customers", ("customer_id",)),
                RequestEnd("orders", ("customer_id",)),
                ("marginalHistogram/1",),
                dates=(RequestDateLink("joined_at", "placed_at"),),
                exposure=(
                    RequestExposure("joined_at", "left_at"),
                    RequestExposure("joined_at"),
                ),
            ),
        ),
        queries=("paid_orders",),
    )
    link = "orders(customer_id) to customers(customer_id)"
    # A public profile forms date pairs, dates and exposure; what it cannot it names once it has
    # read the records, with the reason it has.
    assert asked_for.unformed(asked) == [
        ("workload status of orders", asked_for.NO_WORKLOAD),
        ("query paid_orders", asked_for.NO_QUERIES),
    ]
    assert asked_for.unformed(asked, private=True) == [
        ("date pair placed_at to shipped_at of orders", asked_for.NO_DATE_PAIRS),
        ("calendar of orders.placed_at", asked_for.NO_CALENDAR),
        ("workload status of orders", asked_for.NO_WORKLOAD),
        (f"dates customers.joined_at to orders.placed_at of {link}", asked_for.NO_DATES),
        (f"exposure customers.joined_at to left_at of {link}", asked_for.NO_EXPOSURE),
        (f"exposure customers.joined_at of {link}", asked_for.NO_EXPOSURE),
        ("query paid_orders", asked_for.NO_QUERIES),
    ]
    assert asked_for.unformed(replace(asked, tables=(), relationships=(), queries=())) == []
    assert (
        asked_for.unformed(replace(asked, tables=(), relationships=(), queries=()), private=True)
        == []
    )


def test_a_profile_forms_the_calendar_a_request_asks_and_names_the_query_it_does_not_run(
    workspace,
):
    asked = FakeBackend().read_request(_with_asks(REQUEST), name="asks.yml")
    result = _profile(workspace, asked)
    said = dict(result.not_measured)
    assert "calendar of orders.placed_at" not in said
    assert said["query paid_orders"] == asked_for.NO_QUERIES
    assert "queries" not in result.document
    orders = dict(result.document["tables"]["orders"])
    (entry,) = orders.pop("calendar")
    assert entry["column"] == "placed_at" and set(entry) == {
        "column",
        "month",
        "dayOfWeek",
        "hourOfWeek",
    }
    # The rest of the request is answered as it was without the asks.
    without = _profile(workspace, _asked(workspace))
    assert {**result.document["tables"], "orders": orders} == without.document["tables"]
    assert [each for each in result.not_measured if each not in without.not_measured] == [
        ("query paid_orders", asked_for.NO_QUERIES),
    ]


def test_the_cli_names_the_query_it_does_not_run_and_reports_the_calendar(
    workspace, registry, capsys
):
    (workspace / "asks.yml").write_text(_with_asks(REQUEST), encoding="utf-8")
    rc = main(_argv("--out", "p.json", request="asks.yml"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Not measured: calendar of orders.placed_at" not in out
    assert "exposes[orders]: Calendar: " in out
    assert "Not measured: query paid_orders: fluid profile runs no consumer query." in out
    assert "SELECT" not in out


def test_strict_refuses_a_request_with_an_ask_it_does_not_form_before_a_read(
    workspace, registry, capsys
):
    (workspace / "asks.yml").write_text(_with_asks(REQUEST), encoding="utf-8")
    (workspace / "orders.ndjson").unlink()  # a read would be a different refusal
    out = _refused(
        capsys,
        _argv("--strict", "--out", "p.json", request="asks.yml"),
        "profile_request_not_formed",
    )
    assert "asks for 1 item fluid profile does not form" in out
    assert "query paid_orders" in out and "calendar of" not in out
    assert "no record was read" in out and "SELECT" not in out
    assert not (workspace / "p.json").exists()


def test_strict_accepts_a_request_that_asks_only_for_what_it_forms(workspace, registry, capsys):
    rc = main(_argv("--strict", "--out", "p.json"))
    assert rc == 0, capsys.readouterr().out
    assert (workspace / "p.json").is_file()


def test_strict_needs_a_request(workspace, registry, capsys):
    argv = ["profile", "shop.fluid.yaml", "--expose", "orders", "--source", "orders.ndjson"]
    _refused(capsys, [*argv, "--strict"], "profile_strict_needs_request")


def test_the_document_reads_back_and_follows_its_content(workspace):
    result = _profile(workspace, _asked(workspace))
    from flux_stats.profile import load_profile_text

    load_profile_text(result.canonical.decode("utf-8"))
    FakeBackend().check_document(result.canonical.decode("utf-8"))
    assert result.digest.startswith("sha256:")
    assert "1-3-0" in result.document["_schemaURL"]  # it holds a statistic counted per entity


def test_the_cli_profile_answers_the_request_and_says_what_it_left_out(workspace, registry, capsys):
    rc = main(_argv("--out", "shop.profile.json"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "The profile answers the request shop.profile-request.yml" in out
    assert "its contract digest is this contract's" in out
    assert "Fact orders.quantity_not_above_units: its share is published" in out
    assert "Not measured: fact orders.lines_counted: an aggregate reads child rows" in out
    assert "Not measured: quantile/2 of orders.quantity:" in out
    assert (workspace / "shop.profile.json").is_file()


def test_a_request_with_no_digest_is_accepted_and_says_so(workspace, registry, capsys):
    text = REQUEST.replace(
        '"contract":\n  "digest": "' + _asked(workspace).contract_digest + '"\n', ""
    )
    assert "digest" not in text
    (workspace / "nodigest.yml").write_text(text, encoding="utf-8")
    rc = main(_argv("--out", "p.json", request="nodigest.yml"))
    out = capsys.readouterr().out
    assert rc == 0 and "it names no contract digest" in out


def _refused(capsys, argv, event):
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 1 and event in out, out
    return out


def test_request_with_bundle_env_or_expose_is_refused(workspace, registry, capsys):
    (workspace / "bundle").mkdir()
    _refused(capsys, _argv("--bundle", "bundle"), "profile_request_with_bundle")
    _refused(capsys, _argv("--env", "prod"), "profile_request_with_env")
    _refused(capsys, _argv("--expose", "orders"), "profile_request_with_expose")
    assert not list(workspace.glob("*.profile.json"))


def test_a_contract_that_is_a_bundle_archive_is_refused(workspace, registry, capsys):
    import tarfile

    with tarfile.open(workspace / "bundle.tgz", "w:gz") as archive:
        archive.add(workspace / "shop.fluid.yaml", arcname="shop.fluid.yaml")
    argv = _argv()
    argv[1] = "bundle.tgz"
    _refused(capsys, argv, "profile_request_contract_is_a_bundle")


@pytest.mark.parametrize(
    "prepare, event",
    [
        (lambda w: (w / "d.yml").mkdir(), "profile_request_unreadable"),
        (lambda w: (w / "d.yml").write_bytes(b"\xff\xfe not utf-8"), "profile_request_unreadable"),
        (
            lambda w: (w / "d.yml").write_text("_schemaURL: other\ntables: {}\n"),
            "profile_request_refused",
        ),
        (lambda w: (w / "d.yml").write_text("- not\n- a mapping\n"), "profile_request_refused"),
    ],
)
def test_a_request_file_that_cannot_be_read_is_refused(workspace, registry, capsys, prepare, event):
    prepare(workspace)
    out = _refused(capsys, _argv(request="d.yml"), event)
    assert "FLX-5038" in out or event == "profile_request_unreadable"


def test_a_request_file_past_the_size_bound_is_refused_before_it_is_read(
    workspace, registry, capsys, monkeypatch
):
    monkeypatch.setattr(asked_for, "MOST_REQUEST_BYTES", 100)
    _refused(capsys, _argv(), "profile_request_unreadable")


def test_a_request_that_is_a_link_is_refused(workspace, registry, capsys):
    (workspace / "link.yml").symlink_to(workspace / "shop.profile-request.yml")
    _refused(capsys, _argv(request="link.yml"), "ERR_SYMLINK_PATH_REJECTED")


def test_a_digest_that_is_not_the_contracts_is_refused_with_the_way_to_fix_it(
    workspace, registry, capsys
):
    (workspace / "shop.fluid.yaml").write_text(
        CONTRACT + "# changed\n" + "x: 1\n", encoding="utf-8"
    )
    out = _refused(capsys, _argv(), "profile_request_digest_mismatch")
    assert "fluxsim init" in out


def _request_with(workspace, old, new):
    assert old in REQUEST
    (workspace / "edited.yml").write_text(REQUEST.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize(
    "old, new, event",
    [
        ('  "customers":\n', '  "clients":\n', "profile_request_"),
        ('      "region":\n', '      "area":\n', "profile_request_"),
        ('        "domain": "timestamp"', '        "domain": "category"', "profile_request_"),
    ],
)
def test_a_table_column_or_domain_the_contract_does_not_have_is_refused(
    workspace, registry, capsys, old, new, event
):
    _request_with(workspace, old, new)
    _refused(capsys, _argv(request="edited.yml"), event)


def test_each_table_column_and_domain_refusal_has_its_own_code(workspace):
    asked = _asked(workspace)
    contract = _contract(workspace)

    def refuse(request):
        with pytest.raises(ProfileError) as caught:
            asked_for.check_tables(request, contract)
        return caught.value.event

    (customers, orders) = asked.tables
    ghost = RequestTable("ghost", False, customers.columns)
    assert refuse(Request(None, 16, (ghost,))) == "profile_request_table_not_an_expose"
    column = RequestColumn("area", "category")
    assert (
        refuse(Request(None, 16, (RequestTable("customers", False, (column,)),)))
        == "profile_request_column_not_in_contract"
    )
    wrong = RequestColumn("region", "number")
    assert (
        refuse(Request(None, 16, (RequestTable("customers", False, (wrong,)),)))
        == "profile_request_column_domain"
    )
    assert asked_for.check_tables(asked, contract) is None


def test_a_listed_column_fluid_profile_does_not_read_is_reported_not_refused(workspace):
    # fluxsim init lists a nested or bytes column as a category; fluid profile reads neither,
    # so the column is named as not profiled, with why, and the rest is measured.
    import yaml

    contract = yaml.safe_load(CONTRACT)
    orders = next(each for each in contract["exposes"] if each["exposeId"] == "orders")
    orders["contract"]["schema"].append({"name": "payload", "type": "json"})
    asked = _asked(workspace)
    customers, listed = asked.tables
    payload = RequestColumn("payload", "category", ("count",))
    request = Request(
        None,
        asked.fractional_bits,
        (customers, RequestTable(listed.expose_id, listed.row_count, (*listed.columns, payload))),
        asked.relationships,
        asked.facts,
    )
    assert asked_for.check_tables(request, contract) is None
    result = profile_exposes(
        contract,
        [
            ExposeSource(table.expose_id, str(workspace / f"{table.expose_id}.ndjson"))
            for table in request.tables
        ],
        backend=FakeBackend(),
        request=request,
    )
    (_, table) = result.tables
    assert ("payload", "a nested value has no profile domain") in table.plan.skipped
    assert "payload" not in result.document["tables"]["orders"]["columns"]


def test_a_grid_other_than_the_requests_is_refused(workspace, registry, capsys):
    out = _refused(
        capsys, _argv("--fractional-bits", "8"), "profile_request_fractional_bits_conflict"
    )
    assert "16" in out
    assert main(_argv("--fractional-bits", "16", "--out", "p.json")) == 0


def test_a_listed_table_needs_its_source_and_an_unlisted_one_takes_none(
    workspace, registry, capsys
):
    base = ["profile", "shop.fluid.yaml", "--request", "shop.profile-request.yml"]
    _refused(capsys, [*base, "--source", "orders=orders.ndjson"], "profile_request_source_missing")
    narrow = Request(
        None, 16, (RequestTable("orders", False, (RequestColumn("units", "number"),)),)
    )
    with pytest.raises(ProfileError) as caught:
        asked_for.check_sources(narrow, ["orders", "customers"])
    assert caught.value.event == "profile_request_source_unused"
    # One listed table takes a bare --source.
    asked_for.check_sources(narrow, ["orders"])
