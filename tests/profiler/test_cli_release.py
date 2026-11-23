# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --privacy-unit``: a release under differential privacy, as an operator runs it."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import stat
import sys
from collections import Counter
from pathlib import Path

import pytest

from fluid_build.cli import _profile_release, main
from fluid_build.profiler import _stats
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _release_data as data
from tests.profiler.dp import _release_run as run
from tests.profiler.dp._privacy import need

NULL_POOL_SENTENCE = "Rows with no key value (a null in a key column) are taken together as one entity, bounded the same way: the stated loss holds for that entity as a whole and for each of its rows. An entity that holds only some of those rows, or rows both with and without a key value, is covered at a larger loss: by group privacy at most 4 times rho, or 9 times rho when it holds rows of both kinds."

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("orders.fluid.yaml", "orders.profile-request.yml"):
        shutil.copy(data.FIXTURES / name, tmp_path / name)
    shutil.copy(data.FIXTURES / "shop.fluid.yaml", tmp_path / "shop.fluid.yaml")
    shutil.copy(data.FIXTURES / "shop.profile-request.yml", tmp_path / "shop.profile-request.yml")
    data.write_orders(tmp_path / "orders.csv", data.orders())
    return tmp_path


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


@pytest.fixture
def selected(monkeypatch):
    return run.stub_selection(monkeypatch)


def _argv(*more, record="rec.json", rho="1", c="3"):
    return [
        "profile",
        "orders.fluid.yaml",
        "--request",
        "orders.profile-request.yml",
        "--source",
        "orders=orders.csv",
        "--privacy-unit",
        "orders",
        "--unit-key",
        "customer_id",
        "--max-rows-per-unit",
        c,
        "--rho",
        rho,
        "--release-record",
        record,
        *more,
    ]


def _again(*more, record="rec.json"):
    return [
        "profile",
        "orders.fluid.yaml",
        "--request",
        "orders.profile-request.yml",
        "--source",
        "orders=orders.csv",
        "--release-record",
        record,
        "--rederive",
        *more,
    ]


def _out(capsys):
    seen = capsys.readouterr()
    return seen.out + seen.err


def _seed(workspace) -> str:
    return json.loads((workspace / "rec.json").read_text(encoding="utf-8"))["seed"]


def _numbers(text, workspace):
    """The whole numbers a text holds on their own: not part of a decimal, a word or a path."""
    for place in {str(workspace), str(workspace.resolve())}:
        text = text.replace(place, "<workspace>")
    return set(re.findall(r"(?<![\w.])\d+(?![\w.])", text))


# ---- a release ------------------------------------------------------------------------------


def test_a_release_writes_its_record_then_its_profile_and_prints_the_pin(
    workspace, registry, selected, capsys
):
    assert main(_argv()) == 0
    out = " ".join(_out(capsys).split())
    profile, record = workspace / "orders.profile.json", workspace / "rec.json"
    data_ = profile.read_bytes()
    document = json.loads(data_)
    stored = json.loads(record.read_text(encoding="utf-8"))
    digest = "sha256:" + hashlib.sha256(data_).hexdigest()
    assert stored["profile"]["digest"] == digest
    assert f"profile: {{digest: {digest}}}" in out
    assert registry.checked == [data_.decode("utf-8")]
    assert document["privacy"] == {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "1"},
        "privacyUnit": {"expose": "orders"},
        "publicStructure": True,
    }
    if sys.platform != "win32":
        assert stat.S_IMODE(profile.stat().st_mode) == 0o600
        assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stored["selection"] == {"rho": "0.1", "cliques": [["status", "channel"]]}
    # what the report says
    assert "each entity contributes at most 3 rows of exposes[orders]" in out
    assert "so the stated loss holds for each entity and for each row of it" in out
    assert (
        "rho 1 in all (zero-concentrated): 0.1 choosing the cliques, 0.9 measuring 2 tables" in out
    )
    assert "epsilon 8.434 at delta 1e-6" in out
    assert "clique of status, channel: noise scale 3.162278, share rho 0.45" in out
    assert "histogram of amount: noise scale 3.162278, share rho 0.45" in out
    assert "Cliques chosen with AIM: status, channel." in out
    assert "Not released (a key column: its values name an entity)" in out
    assert "rowCount of orders" in out and "format 1-2-0 holds no noisy form of it" in out
    assert "Keep the release record inside the data owner's boundary" in out
    assert "the noise can be taken off" in out
    assert "cannot ship on flux-engine" not in out
    assert " ".join(NULL_POOL_SENTENCE.split()) in " ".join(out.split())
    assert (
        "The noise is pseudorandom, drawn from HMAC-SHA256 keyed by the recorded seed, so the "
        "guarantee holds as long as HMAC-SHA256 behaves as a pseudorandom function." in out
    )
    assert (
        "Choosing the cliques uses OpenDP's AIM, which OpenDP ships behind its contrib flag." in out
    )
    assert (
        "The guarantee covers data that meets the contract (required columns hold a value, "
        "values lie in their declared levels and ranges, and in rows mode the primary key is "
        "unique); data that does not is refused, releases nothing, and the refusal is for the "
        "data owner (it names a column and a row): a refusal is not covered by the guarantee."
        in out
    )


def test_the_profile_the_release_writes_reads_back_through_flux_stats(
    workspace, registry, selected
):
    from flux_stats.profile import load_profile_text

    assert main(_argv()) == 0
    load_profile_text((workspace / "orders.profile.json").read_text(encoding="utf-8"))


def test_the_default_out_is_named_after_the_release_table_and_out_names_another(
    workspace, registry, selected
):
    assert main(_argv("--out", "p/r.json")) == 0
    assert (workspace / "p" / "r.json").is_file()
    assert not (workspace / "orders.profile.json").exists()


def test_a_release_of_a_child_table_lists_the_other_table_and_says_it_cannot_ship_yet(
    workspace, registry, selected, capsys
):
    argv = [
        "profile",
        "shop.fluid.yaml",
        "--request",
        "shop.profile-request.yml",
        "--source",
        "orders=orders.csv",
        "--privacy-unit",
        "customers",
        "--release-table",
        "orders",
        "--max-rows-per-unit",
        "2",
        "--rho",
        "0.5",
        "--release-record",
        "rec.json",
    ]
    assert main(argv) == 0
    out = " ".join(_out(capsys).split())
    document = json.loads((workspace / "orders.profile.json").read_text(encoding="utf-8"))
    assert set(document["tables"]) == {"customers", "orders"}
    assert set(document["tables"]["customers"]) == {"columns"}
    assert "an entity is a row of exposes[customers]" in out and "through placed_by" in out
    assert (
        "cannot ship on flux-engine 0.9 until it skips a table the release holds no statistic"
        in out
    )
    assert "table customers" in out


def test_a_release_of_each_row_needs_no_bound(workspace, registry, selected, capsys):
    argv = [
        "profile",
        "orders.fluid.yaml",
        "--request",
        "orders.profile-request.yml",
        "--source",
        "orders.csv",
        "--privacy-unit",
        "orders",
        "--rho",
        "1",
        "--release-record",
        "rec.json",
    ]
    assert main(argv) == 0
    assert "each row of exposes[orders] is one entity" in _out(capsys)
    assert json.loads((workspace / "rec.json").read_text())["unit"]["mode"] == "rows"


def test_a_rho_above_one_is_warned_of_once_and_the_release_still_runs(
    workspace, registry, selected, capsys
):
    assert main(_argv(rho="1.5")) == 0
    out = " ".join(_out(capsys).split())
    assert out.count("rho 1.5 is above 1, a large privacy loss") == 1
    assert "The release goes ahead." in out
    assert (workspace / "rec.json").is_file()


def test_a_rho_of_one_is_not_warned_of(workspace, registry, selected, capsys):
    assert main(_argv(rho="1")) == 0
    assert "a large privacy loss" not in _out(capsys)


# ---- nothing data-dependent leaves outside the noise ---------------------------------------------


def test_no_true_count_appears_in_what_a_release_prints_logs_or_writes_beyond_the_noise(
    workspace, registry, selected, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    rows = data.orders()
    per_entity = Counter(row[1] for row in rows)
    kept_at_most = 3
    secrets = {
        len(rows),
        len(per_entity),
        sum(max(0, count - kept_at_most) for count in per_entity.values()),
        max(per_entity.values()),
        len({row[0] for row in rows}),
    }
    cells = Counter((row[2], row[3]) for row in rows)
    secrets |= {count for count in cells.values() if count >= 10}
    secrets |= {count for count in Counter(row[2] for row in rows).values() if count >= 10}
    assert main(_argv()) == 0
    text = _out(capsys) + caplog.text
    seed = _seed(workspace)
    assert seed not in text
    assert seed not in (workspace / "orders.profile.json").read_text(encoding="utf-8")
    shown = _numbers(text, workspace)
    for secret in secrets:
        assert str(secret) not in shown, secret
    # the record names the plan and the tables, and no count either
    record = (workspace / "rec.json").read_text(encoding="utf-8")
    stored = json.loads(record)
    stored.pop("seed")
    rest = json.dumps(stored)
    for secret in secrets:
        assert not re.search(rf"(?<![\w.]){secret}(?![\w.])", rest), secret


def test_a_refusal_names_the_column_and_the_row_and_never_a_value_or_a_count(
    workspace, registry, selected, capsys
):
    rows = data.orders()
    text = (workspace / "orders.csv").read_text(encoding="utf-8").splitlines()
    text[4] = text[4].replace("online", "kiosk").replace("store", "kiosk")  # data row 3
    (workspace / "orders.csv").write_text("\n".join(text) + "\n", encoding="utf-8")
    assert main(_argv()) == 1
    out = " ".join(_out(capsys).split())
    assert "profile_record_outside_declared_domain" in out
    assert "column 'channel' row 3" in out
    assert "kiosk" not in out
    assert str(len(rows)) not in _numbers(out, workspace)
    assert (
        not (workspace / "orders.profile.json").exists() and not (workspace / "rec.json").exists()
    )


# ---- re-derivation ------------------------------------------------------------------------------


def test_the_release_is_drawn_again_from_its_record_to_the_same_bytes(
    workspace, registry, selected, capsys
):
    assert main(_argv()) == 0
    first = (workspace / "orders.profile.json").read_bytes()
    record = (workspace / "rec.json").read_bytes()
    (workspace / "orders.profile.json").unlink()
    capsys.readouterr()
    selected.clear()
    assert main(_again()) == 0
    out = _out(capsys)
    assert (workspace / "orders.profile.json").read_bytes() == first
    assert (workspace / "rec.json").read_bytes() == record
    assert selected == []  # no selection ran: the record's cliques are used
    assert "Drawn again from the release record" in out and "Cliques from the release record" in out
    assert "Wrote the release record" not in out
    assert _seed(workspace) not in out
    # flags that agree with the record are accepted
    assert main(_again("--privacy-unit", "orders", "--rho", "1.0", "--max-rows-per-unit", "3")) == 0


@pytest.mark.parametrize(
    "flags",
    [
        ["--rho", "2"],
        ["--privacy-unit", "customers"],
        ["--max-rows-per-unit", "2"],
        ["--max-rows-per-cell", "1"],
        ["--unit-key", "order_id"],
        ["--release-table", "customers"],
    ],
)
def test_a_flag_that_disagrees_with_the_record_is_refused(
    workspace, registry, selected, capsys, flags
):
    assert main(_argv()) == 0
    (workspace / "orders.profile.json").unlink()
    capsys.readouterr()
    assert main(_again(*flags)) == 1
    assert "profile_release_rederive_disagrees" in _out(capsys)
    assert not (workspace / "orders.profile.json").exists()


def test_changed_records_refuse_and_write_nothing(workspace, registry, selected, capsys):
    assert main(_argv()) == 0
    (workspace / "orders.profile.json").unlink()
    record = (workspace / "rec.json").read_bytes()
    text = (workspace / "orders.csv").read_text(encoding="utf-8")
    (workspace / "orders.csv").write_text(text.replace("paid", "placed", 1), encoding="utf-8")
    capsys.readouterr()
    assert main(_again()) == 1
    out = _out(capsys)
    assert "profile_release_rederive_mismatch" in out
    assert not (workspace / "orders.profile.json").exists()
    assert (workspace / "rec.json").read_bytes() == record
    assert _seed(workspace) not in out


def test_a_changed_request_refuses_and_writes_nothing(workspace, registry, selected, capsys):
    assert main(_argv()) == 0
    (workspace / "orders.profile.json").unlink()
    request = workspace / "orders.profile-request.yml"
    request.write_text("# edited\n" + request.read_text(encoding="utf-8"), encoding="utf-8")
    capsys.readouterr()
    assert main(_again()) == 1
    assert "profile_release_record_mismatch" in _out(capsys)
    assert not (workspace / "orders.profile.json").exists()


def test_a_record_that_states_other_than_the_plan_computes_refuses_and_writes_nothing(
    workspace, registry, selected, capsys
):
    assert main(_argv()) == 0
    (workspace / "orders.profile.json").unlink()
    path = workspace / "rec.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["tables"][0]["scale"] = "1.5"
    path.write_text(json.dumps(stored), encoding="utf-8")
    capsys.readouterr()
    assert main(_again()) == 1
    assert "profile_release_record_mismatch" in _out(capsys)
    assert not (workspace / "orders.profile.json").exists()


def test_another_seed_is_another_release_and_does_not_re_derive(
    workspace, registry, selected, capsys
):
    assert main(_argv()) == 0
    (workspace / "orders.profile.json").unlink()
    path = workspace / "rec.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["seed"] = "00" * 32
    path.write_text(json.dumps(stored), encoding="utf-8")
    capsys.readouterr()
    assert main(_again()) == 1
    out = _out(capsys)
    assert "profile_release_rederive_mismatch" in out and "0" * 64 not in out


def test_the_record_to_re_derive_from_must_exist(workspace, registry, selected, capsys):
    assert main(_again()) == 1
    assert "profile_release_record_missing" in _out(capsys)


# ---- the record's path --------------------------------------------------------------------------


def test_an_existing_record_is_refused_for_a_new_release_and_left_as_it_was(
    workspace, registry, selected, capsys
):
    (workspace / "rec.json").write_text("mine", encoding="utf-8")
    assert main(_argv()) == 1
    assert "profile_record_exists" in _out(capsys)
    assert (workspace / "rec.json").read_text(encoding="utf-8") == "mine"
    assert not (workspace / "orders.profile.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_a_record_that_is_a_link_is_refused(workspace, registry, selected, capsys):
    (workspace / "elsewhere").write_text("x", encoding="utf-8")
    (workspace / "rec.json").symlink_to(workspace / "elsewhere")
    assert main(_argv()) == 1
    assert "profile_release_record_is_a_link" in _out(capsys)
    assert (workspace / "elsewhere").read_text(encoding="utf-8") == "x"


def test_a_record_that_is_the_profile_or_a_source_is_refused(workspace, registry, selected, capsys):
    assert main(_argv(record="orders.profile.json")) == 1
    assert "profile_release_record_is_the_profile" in _out(capsys)
    assert main(_argv(record="orders.csv")) == 1
    assert "profile_release_record_is_the_source" in _out(capsys)
    assert not (workspace / "orders.profile.json").exists()
    assert (workspace / "orders.csv").read_text(encoding="utf-8").startswith("order_id")


# ---- every refusal ---------------------------------------------------------------------------------


def _swap(argv, flag, value):
    argv = list(argv)
    argv[argv.index(flag) + 1] = value
    return argv


def _without(argv, flag, count=2):
    argv = list(argv)
    place = argv.index(flag)
    del argv[place : place + count]
    return argv


@pytest.mark.parametrize(
    "build, event",
    [
        (lambda a: _without(a, "--request"), "profile_release_needs_request"),
        (lambda a: _without(a, "--release-record"), "profile_release_needs_record"),
        (lambda a: _without(a, "--rho"), "profile_release_needs_rho"),
        (lambda a: _without(a, "--privacy-unit"), "profile_release_needs_unit"),
        (lambda a: _swap(a, "--rho", "0"), "profile_release_rho_invalid"),
        (lambda a: _swap(a, "--rho", "1e3"), "profile_release_rho_invalid"),
        (lambda a: _swap(a, "--rho", "-1"), "profile_release_rho_invalid"),
        (lambda a: _swap(a, "--rho", "9" * 101), "profile_release_rho_invalid"),
        (lambda a: _swap(a, "--rho", "0.000000000002"), "profile_release_rho_too_small"),
        (lambda a: _swap(a, "--privacy-unit", "nowhere"), "unit_expose_unknown"),
        (lambda a: _swap(a, "--unit-key", "nope"), "unit_key_column_unknown"),
        (lambda a: _swap(a, "--max-rows-per-unit", "0"), "unit_bound_out_of_range"),
        (lambda a: _swap(a, "--max-rows-per-unit", "5000"), "unit_bound_out_of_range"),
        (lambda a: [*a, "--max-rows-per-cell", "4"], "unit_bound_out_of_range"),
        (lambda a: [*a, "--release-table", "customers"], "unit_expose_unknown"),
        (
            lambda a: _without(a, "--unit-key", 2) + ["--max-rows-per-unit", "2"],
            "unit_bound_in_rows_mode",
        ),
        (lambda a: [*a, "--bundle", "somewhere"], "profile_request_with_bundle"),
        (lambda a: [*a, "--env", "prod"], "profile_request_with_env"),
        (lambda a: [*a, "--expose", "orders"], "profile_request_with_expose"),
        (lambda a: [*a, "--fractional-bits", "8"], "profile_request_fractional_bits_conflict"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_refusal_comes_before_a_record_is_read_and_writes_nothing(
    workspace, registry, selected, capsys, build, event
):
    (workspace / "orders.csv").unlink()  # a read would be a different refusal
    assert main(build(_argv())) == 1
    out = _out(capsys)
    assert event in out, out
    assert (
        not (workspace / "orders.profile.json").exists() and not (workspace / "rec.json").exists()
    )
    assert selected == []


def test_an_unreadable_request_or_one_that_lists_nothing_releasable_is_refused(
    workspace, registry, selected, capsys
):
    request = workspace / "orders.profile-request.yml"
    text = "".join(
        line
        for line in request.read_text(encoding="utf-8").splitlines(keepends=True)
        if "levelHistogram/1" not in line and "marginalHistogram/1" not in line
    )
    request.write_text(text, encoding="utf-8")
    # the request's contract digest still names the contract, so the request is read
    assert main(_argv()) == 1
    assert "profile_release_nothing_to_release" in _out(capsys)
    assert main(_swap(_argv(), "--request", "missing.yml")) != 0


def test_a_source_for_another_table_is_refused_since_only_the_release_table_is_read(
    workspace, registry, selected, capsys
):
    argv = [
        "profile",
        "shop.fluid.yaml",
        "--request",
        "shop.profile-request.yml",
        "--source",
        "customers=customers.csv",
        "--source",
        "orders=orders.csv",
        "--privacy-unit",
        "customers",
        "--release-table",
        "orders",
        "--rho",
        "1",
        "--release-record",
        "rec.json",
    ]
    assert main(argv) == 1
    assert "profile_release_source_unused" in _out(capsys)
    assert not (workspace / "rec.json").exists()


def test_a_request_made_for_another_contract_is_refused(workspace, registry, selected, capsys):
    argv = [
        "profile",
        "shop.fluid.yaml",
        "--request",
        "orders.profile-request.yml",
        "--source",
        "orders.csv",
        "--privacy-unit",
        "customers",
        "--release-table",
        "orders",
        "--rho",
        "1",
        "--release-record",
        "rec.json",
    ]
    assert main(argv) == 1
    assert "profile_request_digest_mismatch" in _out(capsys)


def _csv(workspace, edit):
    lines = (workspace / "orders.csv").read_text(encoding="utf-8").splitlines()
    edit(lines)
    (workspace / "orders.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _field(line, place, value):
    parts = line.split(",")
    parts[place] = value
    return ",".join(parts)


@pytest.mark.parametrize(
    "edit, event, where",
    [
        (
            lambda lines: lines.__setitem__(6, _field(lines[6], 2, "")),
            "profile_release_null",
            "column 'status' row 5",
        ),
        (
            lambda lines: lines.__setitem__(3, _field(lines[3], 4, "1000.01")),
            "profile_record_outside_declared_domain",
            "column 'amount' row 2",
        ),
        (
            lambda lines: lines.__setitem__(3, _field(lines[3], 4, "-1")),
            "profile_record_outside_declared_domain",
            "column 'amount' row 2",
        ),
        (
            lambda lines: lines.__setitem__(9, _field(lines[9], 3, "x")),
            "profile_record_outside_declared_domain",
            "column 'channel' row 8",
        ),
        (
            lambda lines: lines.__setitem__(9, _field(lines[9], 2, "refunded")),
            "profile_record_outside_declared_domain",
            "column 'status' row 8",
        ),
    ],
)
def test_a_record_a_release_cannot_hold_is_refused_before_anything_is_written(
    workspace, registry, selected, capsys, edit, event, where
):
    _csv(workspace, edit)
    assert main(_argv()) == 1
    out = " ".join(_out(capsys).split())
    assert event in out and where in out
    assert "refunded" not in out and "1000.01" not in out
    assert (
        not (workspace / "orders.profile.json").exists() and not (workspace / "rec.json").exists()
    )


def test_a_repeated_primary_key_refuses_a_release_of_each_row(
    workspace, registry, selected, capsys
):
    _csv(workspace, lambda lines: lines.__setitem__(5, "1" + lines[5][lines[5].index(",") :]))
    argv = _without(_argv(), "--unit-key")
    argv = _without(argv, "--max-rows-per-unit")
    assert main(argv) == 1
    assert "profile_release_repeated_key" in _out(capsys)
    assert not (workspace / "rec.json").exists()


def test_cliques_that_hold_too_many_cells_refuse_after_the_selection_and_write_nothing(
    workspace, registry, selected, capsys, monkeypatch
):
    from fluid_build.profiler.dp import selection

    def too_big(*a, **k):
        raise ValueError("the maximal cliques hold more cells than allowed")

    monkeypatch.setattr(selection, "release_structure", too_big)
    assert main(_argv()) == 1
    out = " ".join(_out(capsys).split())
    assert "profile_release_structure_too_large" in out
    assert "more cells than a release may measure" in out
    assert "spent the selection share of this release's budget, rho 0.1, on these records" in out
    assert "list fewer categorical columns" in out
    assert not (workspace / "orders.profile.json").exists()
    assert not (workspace / "rec.json").exists()


def test_a_null_key_row_is_bounded_with_the_others_and_the_report_says_so(
    workspace, registry, selected, capsys
):
    _csv(
        workspace,
        lambda lines: [lines.__setitem__(n, _field(lines[n], 1, "")) for n in range(2, 12)],
    )
    assert main(_argv()) == 0
    out = " ".join(_out(capsys).split())
    assert "are taken together as one entity" in out


def test_the_extra_is_needed_to_choose_cliques_and_nothing_is_read_without_it(
    workspace, registry, capsys, monkeypatch
):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "opendp" else real(name, *a)
    )
    (workspace / "orders.csv").unlink()
    assert main(_argv()) == 1
    out = " ".join(_out(capsys).split())
    assert "profile_release_selection_unavailable" in out
    assert "pip install 'data-product-forge[privacy]'" in out
    assert not (workspace / "rec.json").exists()


# ---- the flags ------------------------------------------------------------------------------------


def test_a_call_without_a_release_flag_is_not_a_release():
    import argparse

    assert not _profile_release.wanted(argparse.Namespace())
    assert not _profile_release.wanted(argparse.Namespace(rederive=False, rho=None))
    assert _profile_release.wanted(argparse.Namespace(rho="1"))
    assert _profile_release.wanted(argparse.Namespace(max_rows_per_unit=0))
    assert _profile_release.wanted(argparse.Namespace(rederive=True))


def _profile_help():
    import argparse

    from fluid_build.cli import build_parser

    parser = build_parser(["profile"])
    (actions,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return " ".join(actions.choices["profile"].format_help().split())


def test_the_help_has_a_section_on_the_private_release():
    text = _profile_help()
    for flag in (
        "--privacy-unit",
        "--release-table",
        "--unit-key",
        "--rho",
        "--max-rows-per-unit",
        "--max-rows-per-cell",
        "--release-record",
        "--rederive",
    ):
        assert flag in text
    assert "private release (differential privacy)" in text
    assert "What it guarantees" in text and "What it does not do" in text
    assert "for which unit" in text.lower()
    assert "one entity is one row of the unit's expose" in text
    assert "recorded inside the data owner's boundary" in text
    assert "with it and the profile the noise can be taken off" in text
    assert " ".join(NULL_POOL_SENTENCE.split()) in " ".join(text.split())
    assert "What the guarantee rests on" in text
    assert (
        "the noise is pseudorandom, drawn from HMAC-SHA256 keyed by the recorded seed, so the "
        "guarantee holds as long as HMAC-SHA256 behaves as a pseudorandom function" in text
    )
    assert (
        "Choosing the cliques uses OpenDP's AIM, which OpenDP ships behind its contrib flag" in text
    )
    assert (
        "The guarantee covers data that meets the contract (required columns hold a value, "
        "values lie in their declared levels and ranges, and in rows mode the primary key is "
        "unique); data that does not is refused, releases nothing, and the refusal is for the "
        "data owner (it names a column and a row): a refusal is not covered by the guarantee"
        in " ".join(text.split())
    )


def test_the_text_of_the_release_holds_no_en_or_em_dash():
    here = Path(__file__).resolve()
    root = here.parents[2]
    files = [
        root / "fluid_build" / "cli" / "_profile_release.py",
        root / "fluid_build" / "profiler" / "dp" / "release.py",
        root / "fluid_build" / "profiler" / "dp" / "bounding.py",
        here,
        here.with_name("test_release_seam.py"),
        here.parent / "dp" / "test_release.py",
        here.parent / "dp" / "test_bounding.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert chr(0x2013) not in text and chr(0x2014) not in text, path


# ---- with the real selection ---------------------------------------------------------------------


def test_one_release_with_the_real_selection(workspace, registry, capsys):
    need("opendp")
    need("mbi")
    need("polars")
    assert main(_argv("--out", "real.profile.json")) == 0
    out = _out(capsys)
    stored = json.loads((workspace / "rec.json").read_text(encoding="utf-8"))
    assert stored["selection"]["rho"] == "0.1"
    assert all(len(clique) >= 2 for clique in stored["selection"]["cliques"])
    assert stored["versions"]["opendp"]
    assert "Cliques chosen with AIM" in out
    first = (workspace / "real.profile.json").read_bytes()
    (workspace / "real.profile.json").unlink()
    assert main(_again("--out", "real.profile.json")) == 0
    assert (workspace / "real.profile.json").read_bytes() == first


def test_the_command_help_lists_the_flags_and_an_example(capsys):
    try:
        rc = main(["profile", "--help"])
    except SystemExit as exited:
        rc = exited.code
    assert rc == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "private release (differential privacy)" in text and "--rederive" in text
    assert "a release under differential privacy" in text
