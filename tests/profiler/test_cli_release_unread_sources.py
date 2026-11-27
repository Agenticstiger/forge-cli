# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --privacy-unit``: a ``--source`` for a table the release chooses not to read.

A caller that hands every expose of a contract its records must not be turned away because the
release has no use for one of them: the release goes on, and says which source it left alone and
why. ``--strict`` keeps the refusal, and a ``--source`` that names no expose is still refused.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from fluid_build.cli import main
from fluid_build.profiler import _stats
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _follow_data as fd

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

#: A library branch: no relationship to any other table, and not listed by the request.
BRANCH = yaml.safe_load("""
exposeId: branch
kind: table
binding:
  platform: local
  format: parquet
  location: { path: ./out/branch.parquet }
contract:
  schema:
    - { name: branch_id, type: INT, required: true }
    - { name: city, type: "enum('north','south')", required: true }
semantics:
  entities:
    - { name: branch, type: primary, expr: branch_id }
""")

#: What the branch file holds: no CSV reader would take it, so a release that read it would fail.
UNREAD = "branch_id,city\nnot a number,\n,,,,\n"
HELD_CITY = "zzz-held-city"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    contract = copy.deepcopy(fd.CONTRACT)
    contract["exposes"].append(copy.deepcopy(BRANCH))
    text = yaml.safe_dump(contract, sort_keys=False)
    (tmp_path / "billing.fluid.yaml").write_text(text, encoding="utf-8")
    digest = FakeBackend().contract_digest(text, name="billing.fluid.yaml")
    (tmp_path / "billing.profile-request.yml").write_text(
        yaml.safe_dump(fd.request_document(digest=digest), sort_keys=False), encoding="utf-8"
    )
    fd.write_csvs(tmp_path, fd.tables())
    (tmp_path / "branch.csv").write_text(UNREAD + HELD_CITY + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


def _argv(*more, extra=("--source", "branch=branch.csv")):
    sources = [
        part
        for table in ("account", "invoice", "line", "ticket", "product")
        for part in ("--source", f"{table}={table}.csv")
    ]
    return [
        "profile",
        "billing.fluid.yaml",
        "--request",
        "billing.profile-request.yml",
        *sources,
        *extra,
        "--privacy-unit",
        "account",
        "--follow",
        fd.FOLLOW_INVOICES,
        "--follow",
        fd.FOLLOW_LINES,
        "--rho",
        "1",
        "--release-record",
        "rec.json",
        *more,
    ]


def _out(capsys):
    seen = capsys.readouterr()
    return " ".join((seen.out + seen.err).split())


def test_a_source_the_release_does_not_read_is_noted_and_the_release_goes_on(
    workspace, registry, capsys
):
    assert main(_argv()) == 0
    out = _out(capsys)
    assert (workspace / "account.profile.json").is_file()
    assert (workspace / "rec.json").is_file()
    assert "--source branch" in out and "not read" in out
    assert "no record of it was read" in out
    # the note names the expose, never the location or anything the file holds
    assert "branch.csv" not in out and HELD_CITY not in out
    assert HELD_CITY not in (workspace / "account.profile.json").read_text("utf-8")
    # the release is the release it was without that source
    assert (
        main(_argv(extra=()) + ["--out", "plain.json", "--release-record", "plain-rec.json"]) == 0
    )
    assert "--source branch" not in _out(capsys)


def test_a_release_without_an_unread_source_says_nothing_of_one(workspace, registry, capsys):
    assert main(_argv(extra=())) == 0
    assert "not read" not in _out(capsys)


def test_strict_keeps_the_refusal_of_a_source_the_release_does_not_read(
    workspace, registry, capsys
):
    assert main(_argv("--strict")) == 1
    out = _out(capsys)
    assert "profile_release_source_unused" in out and "exposes[branch]" in out
    assert "no record was read" in out
    assert not (workspace / "rec.json").exists()
    assert not (workspace / "account.profile.json").exists()


def test_a_source_that_names_no_expose_of_the_contract_is_still_refused(
    workspace, registry, capsys
):
    assert main(_argv(extra=("--source", "shelf=branch.csv"))) == 1
    out = _out(capsys)
    assert "profile_source_unpaired" in out and "shelf" in out
    assert not (workspace / "rec.json").exists()


def test_a_source_for_an_unread_table_must_still_name_a_file_that_exists(
    workspace, registry, capsys
):
    assert main(_argv(extra=("--source", "branch=missing.csv"))) == 1
    out = _out(capsys)
    assert "missing.csv" in out and "is not a file" in out
    assert not (workspace / "rec.json").exists()


def test_a_source_for_an_unread_table_with_no_location_is_refused(workspace, registry, capsys):
    assert main(_argv(extra=("--source", "branch="))) == 1
    assert "profile_source_empty" in _out(capsys)
    assert not (workspace / "rec.json").exists()


def test_a_source_for_a_table_the_release_reads_is_still_read(workspace, registry, capsys):
    (workspace / "invoice.csv").write_text("not,a,table\n1,2\n", encoding="utf-8")
    assert main(_argv()) != 0
    assert not (workspace / "rec.json").exists()


def test_the_profile_is_never_written_over_a_source_the_release_does_not_read(
    workspace, registry, capsys
):
    assert main(_argv("--out", "branch.csv")) == 1
    assert "profile_out_is_the_source" in _out(capsys)
    assert (workspace / "branch.csv").read_text("utf-8").startswith("branch_id,city")
    assert not (workspace / "rec.json").exists()


def test_the_release_record_is_never_written_over_a_source_the_release_does_not_read(
    workspace, registry, capsys
):
    argv = _argv()
    argv[argv.index("rec.json")] = "branch.csv"
    assert main(argv) == 1
    assert "profile_release_record" in _out(capsys)
    assert (workspace / "branch.csv").read_text("utf-8").startswith("branch_id,city")


def test_the_table_that_goes_with_an_unread_source_is_set_aside_with_it(
    workspace, registry, capsys
):
    (workspace / "branch.db").write_bytes(b"")
    argv = _argv("--table", "branch=branch", extra=("--source", "branch=sqlite:///branch.db"))
    assert main(argv) == 0
    out = _out(capsys)
    assert "--source branch names exposes[branch]" in out and "branch.db" not in out


def test_strict_refuses_the_source_before_its_table_is_looked_at(workspace, registry, capsys):
    assert main(_argv("--strict", "--table", "branch=branch")) == 1
    assert "profile_release_source_unused" in _out(capsys)


def test_an_unread_source_given_twice_is_refused_as_a_read_one_is(workspace, registry, capsys):
    extra = ("--source", "branch=branch.csv", "--source", "branch=account.csv")
    assert main(_argv(extra=extra)) == 1
    assert "profile_source_repeated" in _out(capsys)
    assert not (workspace / "rec.json").exists()


@pytest.mark.parametrize(
    "location",
    ["s3://bucket/branch.csv", "file://otherhost/branch.csv", ".", "x" * 5000],
)
def test_a_location_the_read_would_refuse_is_refused_for_an_unread_source_too(
    workspace, registry, capsys, location
):
    assert main(_argv(extra=("--source", f"branch={location}"))) == 1
    assert not (workspace / "rec.json").exists()
    assert "Traceback" not in _out(capsys)
