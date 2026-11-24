# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --privacy-unit --follow``: a unit that follows its child rows, end to end."""

from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest
import yaml

from fluid_build.cli import main
from fluid_build.cli._profile_release import HELP
from fluid_build.profiler import _stats, document
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _follow_data as fd
from tests.profiler.dp._privacy import need

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

TABLES = ("account", "invoice", "line", "ticket", "product")


def _workspace(tmp_path, monkeypatch, *, rich=False):
    monkeypatch.chdir(tmp_path)
    text = yaml.safe_dump(fd.CONTRACT, sort_keys=False)
    (tmp_path / "billing.fluid.yaml").write_text(text, encoding="utf-8")
    digest = FakeBackend().contract_digest(text, name="billing.fluid.yaml")
    (tmp_path / "billing.profile-request.yml").write_text(
        yaml.safe_dump(fd.request_document(digest=digest, rich=rich), sort_keys=False),
        encoding="utf-8",
    )
    fd.write_csvs(tmp_path, fd.tables())
    return tmp_path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    return _workspace(tmp_path, monkeypatch)


@pytest.fixture
def rich_workspace(tmp_path, monkeypatch):
    return _workspace(tmp_path, monkeypatch, rich=True)


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


def _sources(tables=TABLES):
    return [part for table in tables for part in ("--source", f"{table}={table}.csv")]


def _argv(*more, record="rec.json", rho="1", follows=fd.FOLLOWS):
    flags = [flag for text in follows for flag in ("--follow", text)]
    return [
        "profile",
        "billing.fluid.yaml",
        "--request",
        "billing.profile-request.yml",
        *_sources(),
        "--privacy-unit",
        "account",
        *flags,
        "--rho",
        rho,
        "--release-record",
        record,
        *more,
    ]


def _again(*more, record="rec.json"):
    return [
        "profile",
        "billing.fluid.yaml",
        "--request",
        "billing.profile-request.yml",
        *_sources(),
        "--release-record",
        record,
        "--rederive",
        *more,
    ]


def _out(capsys):
    seen = capsys.readouterr()
    return " ".join((seen.out + seen.err).split())


def _blocks(value):
    """Every ``noise`` block and every ``cliqueSelection`` selection share of a document."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "noise":
                found.append(item)
            elif key == "selection" and "privacyLoss" in item:
                found.append(item)
            else:
                found.extend(_blocks(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_blocks(item))
    return found


def _read(profile_bytes):
    from flux_stats.profile import load_profile_text

    profile, parsed = load_profile_text(profile_bytes.decode("utf-8"))
    return profile, parsed


# ---------------------------------------------------------------------------
# What is written
# ---------------------------------------------------------------------------


def test_a_follows_release_is_accepted_by_the_reader_and_every_noise_block_is_in_its_account(
    workspace, registry, capsys
):
    assert main(_argv()) == 0
    out = _out(capsys)
    written = (workspace / "account.profile.json").read_bytes()
    profile, body = _read(written)  # the real reader: the tree, every charge and the budget
    assert body["_schemaURL"] == document.SCHEMA_URL_1_4_0
    unit = body["privacy"]["privacyUnit"]
    assert unit["expose"] == "account"
    assert unit["follows"] == [
        {
            "parent": {"table": "account", "columns": ["id"]},
            "child": {"table": "invoice", "columns": ["account_id"]},
            "maxChildren": 3,
        },
        {
            "parent": {"table": "invoice", "columns": ["invoice_id"]},
            "child": {"table": "line", "columns": ["invoice_id"]},
            "maxChildren": 2,
        },
    ]
    assert body["privacy"]["publicStructure"] is True
    assert profile.accounting is not None
    assert dict(profile.accounting.reached) == {"account": 1, "invoice": 3, "line": 6}
    # every noise block is in the reader's account exactly once, and they add up to at most the total
    assert len(profile.parts) == len(_blocks(body)) > 10
    assert len({where for where, _ in profile.parts}) == len(profile.parts)
    assert sum(Fraction(each.loss.rho) for _, each in profile.parts) <= Fraction(1)
    assert "variance" not in written.decode("utf-8")
    # the followed relationships are entries of `relationships`, and so are the others released
    held = [
        (each["parent"]["table"], each["child"]["table"], len(each["statistics"]))
        for each in body["relationships"]
    ]
    assert ("account", "invoice", 3) in held and ("invoice", "line", 0) in held
    assert ("product", "invoice", 2) in held and ("account", "ticket", 2) in held
    # the report: what the unit is, what it reaches, what it treats as fixed, what it warns about
    assert "account (m = 1), invoice (m = 3), line (m = 6)" in out
    assert "does not reach, which the guarantee treats as fixed" in out and "product, ticket" in out
    assert "product.category carries the sensitivity label pii" in out and "P1" in out
    assert "squared L2 sensitivity 18 for one unit" in out


def test_a_follows_release_names_an_ask_fluid_profile_does_not_form(workspace, registry, capsys):
    asked = yaml.safe_load((workspace / "billing.profile-request.yml").read_text("utf-8"))
    asked["queries"] = [
        {
            "name": "large_quantities",
            "sql": "SELECT qty FROM invoice",
            "unit": {"table": "invoice"},
        }
    ]
    (workspace / "billing.profile-request.yml").write_text(yaml.safe_dump(asked), encoding="utf-8")
    assert main(_argv()) == 0
    out = _out(capsys)
    assert "Not released (fluid profile runs no consumer query): query large_quantities." in out
    assert "SELECT" not in out


def test_without_follow_the_release_names_no_follows_and_keeps_its_bytes(workspace, registry):
    # a release with no --follow is the release it was: the format and the unit do not change
    asked = yaml.safe_load((workspace / "billing.profile-request.yml").read_text("utf-8"))
    for name in ("invoice", "line", "ticket", "product"):
        asked["tables"].pop(name)
    asked["relationships"] = []
    (workspace / "one.profile-request.yml").write_text(yaml.safe_dump(asked), encoding="utf-8")
    argv = [
        "profile",
        "billing.fluid.yaml",
        "--request",
        "one.profile-request.yml",
        "--source",
        "account=account.csv",
        "--privacy-unit",
        "account",
        "--rho",
        "1",
        "--release-record",
        "one.json",
    ]
    assert main(argv) == 0
    body = json.loads((workspace / "account.profile.json").read_bytes())
    assert (
        body["_schemaURL"]
        == "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"
    )
    assert body["privacy"]["privacyUnit"] == {"expose": "account"}
    assert "relationships" not in body


def test_the_reader_refuses_a_follow_edited_into_a_non_tree_as_the_profiler_does(
    workspace, registry
):
    from flux_stats.errors import FluxError

    from fluid_build.profiler.dp import release
    from fluid_build.profiler.dp.follows import plan_follows

    assert main(_argv()) == 0
    _, body = _read((workspace / "account.profile.json").read_bytes())
    edits = {
        "two paths into one table": {
            "parent": {"table": "product", "columns": ["product_id"]},
            "child": {"table": "invoice", "columns": ["product_id"]},
            "maxChildren": 1,
        },
    }
    for name, extra in edits.items():
        edited = copy.deepcopy(body)
        edited["privacy"]["privacyUnit"]["follows"].append(extra)
        with pytest.raises(FluxError) as refused:
            _read(json.dumps(edited).encode("utf-8"))
        assert "FLX-5036" in str(refused.value), name
    # the profiler refuses the same set of entries before it reads a record
    with pytest.raises(release.ReleaseRefused):
        plan_follows(
            fd.CONTRACT,
            "account",
            fd.specs(*fd.FOLLOWS, "product.product_id=invoice.product_id:1"),
            FakeBackend(),
        )
    # a parent the unit does not reach
    edited = copy.deepcopy(body)
    del edited["privacy"]["privacyUnit"]["follows"][0]
    with pytest.raises(FluxError):
        _read(json.dumps(edited).encode("utf-8"))
    # and the cap of a followed relationship is a bound on its children: a bin above it is refused
    edited = copy.deepcopy(body)
    edited["privacy"]["privacyUnit"]["follows"][0]["maxChildren"] = 1
    with pytest.raises(FluxError):
        _read(json.dumps(edited).encode("utf-8"))


def test_a_follows_release_is_drawn_again_to_the_same_bytes(workspace, registry, capsys):
    assert main(_argv()) == 0
    first = (workspace / "account.profile.json").read_bytes()
    record = json.loads((workspace / "rec.json").read_text("utf-8"))
    assert record["unit"]["mode"] == "follows"
    assert [each["maxChildren"] for each in record["unit"]["follows"]] == [3, 2]
    kinds = {each.get("kind", "count") for each in record["tables"]}
    assert kinds == {"count", "relationship", "link"}
    assert all("table" in each for each in record["tables"])
    (workspace / "account.profile.json").unlink()
    capsys.readouterr()
    assert main(_again()) == 0
    assert (workspace / "account.profile.json").read_bytes() == first
    assert "Drawn again from the release record" in _out(capsys)
    # the recorded follows are the release's own: another one is refused
    other = _again("--follow", "account.id=invoice.account_id:2")
    assert main(other) == 1
    assert (workspace / "account.profile.json").read_bytes() == first


def test_a_record_whose_follows_or_parts_were_edited_is_refused(workspace, registry, capsys):
    assert main(_argv()) == 0
    capsys.readouterr()
    path = workspace / "rec.json"
    original = json.loads(path.read_text("utf-8"))
    for edit in (
        lambda body: body["unit"]["follows"][0].update({"maxChildren": 2}),
        lambda body: body["unit"]["follows"].pop(),
        lambda body: body["tables"].pop(),
        lambda body: body["tables"][0].update({"scale": "1"}),
        lambda body: body["unit"].update({"mode": "keyed"}),
    ):
        edited = copy.deepcopy(original)
        edit(edited)
        path.write_text(json.dumps(edited), encoding="utf-8")
        assert main(_again()) == 1
        assert not (workspace / "never.json").exists()


def test_follow_is_exclusive_with_the_other_ways_of_naming_a_unit_and_refuses_before_a_read(
    workspace, registry, capsys
):
    for flag, value in (
        ("--release-table", "invoice"),
        ("--unit-key", "id"),
        ("--max-rows-per-unit", "2"),
        ("--max-rows-per-cell", "1"),
    ):
        assert main(_argv(flag, value)) == 1
        out = _out(capsys)
        assert "profile_release_follow_conflict" in out and flag in out
    # a set that is not a tree is refused naming the entry, and no record was read: the source is
    # a file that does not exist
    argv = _argv(follows=("invoice.invoice_id=line.invoice_id:2",))
    argv[argv.index("account=account.csv")] = "account=missing.csv"
    assert main(argv) == 1
    out = _out(capsys)
    assert "profile_release_follow_refused" in out
    assert "--follow invoice.invoice_id=line.invoice_id:2" in out
    assert "nothing: no record was read" in out
    assert not (workspace / "rec.json").exists()


def test_a_malformed_follow_is_refused_by_the_parser(workspace, registry, capsys):
    with pytest.raises(SystemExit):
        main(_argv(follows=("account.id=invoice.account_id",)))
    assert "MAX" in capsys.readouterr().err


def test_every_table_the_release_reads_needs_its_source(workspace, registry, capsys):
    argv = _argv()
    at = argv.index("product=product.csv")
    del argv[at - 1 : at + 1]
    assert main(argv) == 1
    assert "profile_source_missing" in _out(capsys)
    assert not (workspace / "rec.json").exists()


def test_a_repeated_parent_key_refuses_the_release_naming_the_table_and_no_value(
    workspace, registry, capsys
):
    rows = fd.tables()
    rows["account"].append(dict(rows["account"][0]))
    fd.write_csvs(workspace, rows)
    assert main(_argv()) == 1
    out = _out(capsys)
    assert "profile_release_repeated_key" in out and "'account'" in out
    assert not (workspace / "rec.json").exists()
    assert not (workspace / "account.profile.json").exists()


def test_a_record_outside_a_declared_domain_refuses_naming_table_column_and_row(
    workspace, registry, capsys
):
    rows = fd.tables()
    rows["invoice"][2]["status"] = "void"
    fd.write_csvs(workspace, rows)
    assert main(_argv()) == 1
    out = _out(capsys)
    assert "profile_record_outside_declared_domain" in out
    assert "'status' row 2" in out and "void" not in out


def test_the_help_says_what_follow_does_and_guarantees():
    assert "--follow PARENT.COL[,COL]=CHILD.COL[,COL]:MAX" in HELP
    assert "one row of the --privacy-unit expose together with the rows" in HELP
    assert "treated as fixed" in HELP and "P1" in HELP
    assert "cannot be used with --release-table, --unit-key or" in HELP


def test_fluid_profile_help_lists_the_flag_and_an_example(capsys):
    try:
        code = main(["profile", "--help"])
    except SystemExit as exited:
        code = exited.code
    assert code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "--follow" in text and "PARENT.COL" in text
    assert "a unit that follows its child rows" in text


# ---------------------------------------------------------------------------
# Rank correlations and cliques of a reached table, with the real selection
# ---------------------------------------------------------------------------


def test_cliques_and_ranks_of_a_reached_table_are_released_and_drawn_again(
    rich_workspace, registry, capsys
):
    for module in ("opendp", "mbi", "polars"):
        need(module)
    assert main(_argv("--row-floor", "50", "--clique-method", "mst")) == 0
    out = _out(capsys)
    written = (rich_workspace / "account.profile.json").read_bytes()
    profile, body = _read(written)
    invoice = body["tables"]["invoice"]
    assert invoice["cliqueSelection"]["method"] == "mst"
    assert {each["columns"][0] for each in invoice["cliques"]} == {"status"}
    assert all(
        statistic["rowFloor"] == 50
        for pair in invoice["columnPairs"]
        for statistic in pair["statistics"]
    )
    assert "cliques" not in body["tables"]["account"]
    assert sum(Fraction(each.loss.rho) for _, each in profile.parts) <= Fraction(1)
    assert len(profile.parts) + 1 == len(_blocks(body)) + 1  # the selection share is one part
    record = json.loads((rich_workspace / "rec.json").read_text("utf-8"))
    assert record["options"] == {"cliqueMethod": "mst", "rowFloor": 50}
    assert set(record["selection"]["tables"]) == {"invoice"}
    assert "Cliques chosen with MST: invoice: status, channel" in out
    (rich_workspace / "account.profile.json").unlink()
    assert main(_again()) == 0
    assert (rich_workspace / "account.profile.json").read_bytes() == written
