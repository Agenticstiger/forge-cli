# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs status <product> --release-dir DIR``: which sealed release was in force.

A job Forge does not run leaves no run record; the sealed release ``fluid ship --release-dir``
publishes at each deploy is what it leaves. The records are read like any file someone else may
have written: one level deep, no link followed, bounded, and every text cleaned before it is shown.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from fluid_build.cli import _ship_release as release
from fluid_build.cli.ops import release_status as rs
from fluid_build.cli.ops._cli import register_runs

PRODUCT = "commerce.orders"
LOG = logging.getLogger("test_release_status")
SHA = "4458aaa4" * 5


def seal(
    root: Path,
    name: str,
    *,
    sealed: str = "2026-11-18T09:12:03+00:00",
    product: str = PRODUCT,
    versions: Optional[Dict[str, str]] = None,
    source: Optional[Dict[str, str]] = None,
    run_id: str = "3f9a1c0b7e2d",
    environment: Optional[str] = "prod",
    tamper: bool = False,
    **extra: Any,
) -> Path:
    """A release directory with a ``release.json`` as ``fluid ship`` writes it."""
    identity = {
        "product_id": product,
        "forge_version": "0.37.0",
        "bundle": {"manifest_digest": "sha256:bundle"},
        "expose_versions": versions if versions is not None else {"orders": "1.4.0"},
    }
    if environment is not None:
        identity["environment"] = environment
    index = {
        "schema": release.RELEASE_SCHEMA,
        "identity": identity,
        "identity_digest": release.identity_digest(identity),
        "gates": [],
        "files": {},
        "provenance": {
            "run_id": run_id,
            "started_at": sealed,
            "finished_at": sealed,
            **({"source": source} if source is not None else {}),
        },
        **extra,
    }
    directory = root / name
    directory.mkdir(parents=True)
    if tamper:
        identity["expose_versions"] = {"orders": "9.9.9"}  # after the digest was taken
    (directory / "release.json").write_text(json.dumps(index), encoding="utf-8")
    return directory


@pytest.fixture
def releases(tmp_path: Path) -> Path:
    root = tmp_path / "releases"
    seal(root, "r1", sealed="2026-11-18T09:12:03+00:00", versions={"orders": "1.4.0"},
         source={"revision": SHA, "repository": "https://github.com/org/orders", "ref": "refs/tags/v1.4.0"})  # fmt: skip
    seal(
        root, "r2", sealed="2026-11-20T15:02:11+00:00", versions={"orders": "1.5.0"}, run_id="aaaa"
    )
    seal(
        root, "r3", sealed="2026-11-22T08:00:00+00:00", versions={"orders": "2.0.0"}, run_id="bbbb"
    )
    return root


# --- finding the releases -------------------------------------------------------------


def test_the_releases_of_a_product_are_found_in_the_order_they_were_sealed(releases):
    found = rs.find_releases([str(releases)], PRODUCT)
    assert [r.expose_versions["orders"] for r in found.releases] == ["1.4.0", "1.5.0", "2.0.0"]
    assert found.skipped == {} and all(r.intact for r in found.releases)


def test_a_directory_that_is_itself_a_release_is_one_release(releases):
    found = rs.find_releases([str(releases / "r2")], PRODUCT)
    assert [r.run_id for r in found.releases] == ["aaaa"]


def test_another_products_release_and_what_is_no_release_are_counted_not_listed(releases):
    seal(releases, "other", product="retail.clicks")
    (releases / "empty").mkdir()
    (releases / "note.txt").write_text("x")
    (releases / ".hidden").mkdir()
    (releases / "r4.partial-abc-1234").mkdir()  # a run that did not publish
    found = rs.find_releases([str(releases)], PRODUCT)
    assert len(found.releases) == 3
    assert found.skipped == {
        "a release of another product": 1,
        "a directory with no readable release.json": 1,
    }


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    with pytest.raises(NotADirectoryError, match="--release-dir"):
        rs.find_releases([str(tmp_path / "nope")], PRODUCT)


def test_several_roots_are_read_and_a_release_reached_twice_by_one_path_is_listed_once(releases):
    found = rs.find_releases([str(releases), str(releases / "r1")], PRODUCT)
    ids = [r.release_id for r in found.releases]
    assert len(found.releases) == 3 and len(set(ids)) == 3


def test_a_release_reached_by_two_spellings_of_one_root_is_listed_once(
    releases, tmp_path, monkeypatch
):
    link = tmp_path / "link-to-releases"
    os.symlink(releases, link)
    monkeypatch.chdir(tmp_path)
    roots = [
        str(releases),
        str(link),
        "releases",
        "./releases/r1",
        str(releases / ".." / "releases"),
    ]
    found = rs.find_releases(roots, PRODUCT)
    assert len(found.releases) == 3 and found.skipped == {}


@pytest.mark.parametrize("long_root", [False, True])
def test_the_same_release_sealed_in_one_second_in_two_roots_is_ordered_by_path_not_by_root(
    tmp_path, long_root
):
    top = _deep(tmp_path) if long_root else tmp_path  # a long one is cut where it is shown
    sealed = "2026-11-18T09:12:03+00:00"
    seal(top / "b", "x", sealed=sealed, run_id="from-b")
    seal(top / "a", "y", sealed=sealed, run_id="from-a")
    found = rs.find_releases([str(top / "b"), str(top / "a")], PRODUCT)
    assert [r.run_id for r in found.releases] == ["from-a", "from-b"]  # one identity digest


def test_releases_sealed_in_the_same_second_are_listed_in_the_order_of_their_directories(tmp_path):
    root = tmp_path / "rel"
    for name, version in (("b", "1.1.0"), ("a", "1.0.0"), ("c", "1.2.0")):
        seal(root, name, sealed="2026-11-18T09:12:03+00:00", versions={"orders": version})
    found = rs.find_releases([str(root)], PRODUCT)
    assert [r.expose_versions["orders"] for r in found.releases] == ["1.0.0", "1.1.0", "1.2.0"]


def test_a_directory_holding_more_than_the_limit_is_read_to_the_limit(releases, monkeypatch):
    monkeypatch.setattr(rs, "MAX_ENTRIES", 2)
    found = rs.find_releases([str(releases)], PRODUCT)
    assert len(found.releases) == 2
    # every entry past the limit is counted, not the directory once
    assert found.skipped == {"entries past the first 2 of a directory": 1}
    (releases / "zz-one-more").mkdir()
    again = rs.find_releases([str(releases)], PRODUCT)
    assert again.skipped == {"entries past the first 2 of a directory": 2}


# --- records are not trusted ----------------------------------------------------------


def test_a_link_is_never_followed(releases, tmp_path):
    outside = tmp_path / "outside"
    seal(outside, "inner", versions={"orders": "6.6.6"})
    os.symlink(outside / "inner", releases / "linked-dir")
    (releases / "r2" / "release.json").unlink()
    os.symlink(outside / "inner" / "release.json", releases / "r2" / "release.json")
    found = rs.find_releases([str(releases)], PRODUCT)
    assert [r.expose_versions["orders"] for r in found.releases] == ["1.4.0", "2.0.0"]


@pytest.mark.parametrize(
    "text",
    ["not json", "[1, 2]", "{}", '{"schema": "other/1"}', '{"schema": "fluid-release/1"}'],
)
def test_a_release_json_that_is_not_a_record_is_skipped(releases, text):
    (releases / "r2" / "release.json").write_text(text)
    found = rs.find_releases([str(releases)], PRODUCT)
    assert [r.run_id for r in found.releases] == ["3f9a1c0b7e2d", "bbbb"]
    assert sum(found.skipped.values()) == 1


def test_a_record_with_no_time_it_was_sealed_at_is_skipped(releases):
    index = json.loads((releases / "r2" / "release.json").read_text())
    index["provenance"] = {"run_id": "aaaa", "finished_at": "yesterday-ish"}
    (releases / "r2" / "release.json").write_text(json.dumps(index))
    assert len(rs.find_releases([str(releases)], PRODUCT).releases) == 2


@pytest.mark.parametrize("sealed", ["9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+05:00"])
def test_a_time_no_utc_day_can_hold_skips_that_record_and_not_the_others(releases, sealed, capsys):
    seal(releases, "r9", sealed=sealed)
    found = rs.find_releases([str(releases)], PRODUCT)
    assert len(found.releases) == 3
    assert found.skipped == {"a release.json that is not a fluid-release/1 record": 1}
    assert runs_status("--release-dir", str(releases), "--on", "2026-11-20") == 0
    assert "orders 1.5.0" in capsys.readouterr().out


@pytest.mark.timeout(30)  # a pipe that is opened for reading blocks until a writer comes
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_release_json_that_is_a_named_pipe_is_skipped_without_blocking(releases):
    (releases / "r2" / "release.json").unlink()
    os.mkfifo(releases / "r2" / "release.json")
    found = rs.find_releases([str(releases)], PRODUCT)
    assert [r.run_id for r in found.releases] == ["3f9a1c0b7e2d", "bbbb"]
    assert found.skipped == {"a directory with no readable release.json": 1}


@pytest.mark.timeout(30)
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_that_holds_a_valid_record_is_not_read_as_one(tmp_path):
    directory = seal(tmp_path / "rel", "a")
    record = (directory / "release.json").read_bytes()
    (directory / "release.json").unlink()
    os.mkfifo(directory / "release.json")
    keep = os.open(directory / "release.json", os.O_RDONLY | os.O_NONBLOCK)  # keeps the buffer
    try:
        writer = os.open(directory / "release.json", os.O_WRONLY)
        os.write(writer, record)
        os.close(writer)
        assert rs.find_releases([str(tmp_path / "rel")], PRODUCT).releases == []
    finally:
        os.close(keep)


@pytest.mark.parametrize("value", [["prod"], {"a": 1}, 7, True, [[[[]]]] * 3, ""])
def test_a_field_that_is_not_text_is_left_out_not_turned_into_text(tmp_path, value):
    directory = seal(tmp_path / "rel", "a")
    index = json.loads((directory / "release.json").read_text())
    index["identity"]["environment"] = value
    index["identity"]["forge_version"] = value
    index["identity"]["bundle"]["manifest_digest"] = value
    index["provenance"]["run_id"] = value
    (directory / "release.json").write_text(json.dumps(index))
    (release,) = rs.find_releases([str(tmp_path / "rel")], PRODUCT).releases
    assert (release.environment, release.forge_version, release.run_id) == (None, None, None)
    assert release.bundle_digest is None
    assert "env" not in "\n".join(rs.describe(release))


def test_what_the_digest_does_not_cover_can_change_and_the_answer_says_so(releases):
    index = json.loads((releases / "r3" / "release.json").read_text())
    index["provenance"]["finished_at"] = "2026-11-17T00:00:00Z"  # outside the identity digest
    (releases / "r3" / "release.json").write_text(json.dumps(index))
    found = rs.find_releases([str(releases)], PRODUCT)
    assert all(r.intact for r in found.releases)
    answers = rs.in_force_by_environment(found.releases, None, moment("2026-11-25T00:00:00"))
    text = "\n".join(rs.report(PRODUCT, answers, found, None))
    assert rs.NOT_AUTHENTICATED in text and "does not say who wrote the record" in text


def test_a_record_of_exactly_the_limit_is_read_and_one_byte_more_is_not(tmp_path, monkeypatch):
    directory = seal(tmp_path / "rel", "a")
    text = (directory / "release.json").read_text(encoding="utf-8")
    monkeypatch.setattr(rs, "MAX_INDEX_BYTES", len(text) + 5)
    (directory / "release.json").write_text(text + " " * 5, encoding="utf-8")
    assert len(rs.find_releases([str(tmp_path / "rel")], PRODUCT).releases) == 1
    (directory / "release.json").write_text(text + " " * 6, encoding="utf-8")
    assert rs.find_releases([str(tmp_path / "rel")], PRODUCT).releases == []


def test_a_record_larger_than_the_limit_is_not_read(releases, monkeypatch):
    monkeypatch.setattr(rs, "MAX_INDEX_BYTES", 50)
    found = rs.find_releases([str(releases)], PRODUCT)
    assert found.releases == []
    assert found.skipped == {"a directory with no readable release.json": 3}


def test_a_record_that_was_changed_after_it_was_sealed_is_shown_as_not_intact(releases):
    seal(releases, "r9", sealed="2026-11-19T00:00:00+00:00", tamper=True)
    found = rs.find_releases([str(releases)], PRODUCT)
    changed = next(r for r in found.releases if r.path.endswith("r9"))
    assert changed.intact is False
    text = "\n".join(rs.describe(changed))
    assert "NOT INTACT" in text and "9.9.9" in text


def test_texts_from_a_record_cannot_rewrite_the_terminal(releases):
    esc, bidi = "\x1b[31m", chr(0x202E)
    seal(
        releases,
        "r5",
        sealed="2026-11-21T00:00:00+00:00",
        versions={f"orders{esc}": f"1.0{bidi}0\n"},
        source={"revision": SHA, "repository": f"https://h/{esc}x{bidi}", "ref": "a\nb"},
        run_id=f"run{esc}\r\nforged: ok",
        environment=f"prod{esc}{bidi}",
    )
    found = rs.find_releases([str(releases)], PRODUCT)
    shown = "\n".join(line for r in found.releases for line in rs.describe(r))
    assert esc not in shown and bidi not in shown and "\r" not in shown
    assert "\nforged" not in shown
    assert len(rs.clean("x" * 500)) == 200 and rs.clean("x" * 500).endswith("...")


# --- which release was in force -------------------------------------------------------


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_the_release_in_force_at_the_start_of_a_day_and_the_ones_sealed_during_it(releases):
    found = rs.find_releases([str(releases)], PRODUCT).releases
    on_the_20th = rs.in_force(found, date(2026, 11, 20), moment("2026-11-25T00:00:00"))
    assert on_the_20th.at_start.expose_versions["orders"] == "1.4.0"
    assert [r.expose_versions["orders"] for r in on_the_20th.during] == ["1.5.0"]
    quiet = rs.in_force(found, date(2026, 11, 19), moment("2026-11-25T00:00:00"))
    assert quiet.at_start.expose_versions["orders"] == "1.4.0" and quiet.during == []
    before = rs.in_force(found, date(2026, 11, 17), moment("2026-11-25T00:00:00"))
    assert before.at_start is None and before.during == []


def test_without_a_day_it_is_the_latest_release_sealed_by_now(releases):
    found = rs.find_releases([str(releases)], PRODUCT).releases
    now = rs.in_force(found, None, moment("2026-11-21T12:00:00"))
    assert now.at_start.expose_versions["orders"] == "1.5.0" and now.during == []
    assert rs.in_force(found, None, moment("2026-11-01T00:00:00")).at_start is None
    # a release sealed at this very second is already in force
    exact = rs.in_force(found, None, moment("2026-11-20T15:02:11"))
    assert exact.at_start.expose_versions["orders"] == "1.5.0"


def test_a_time_with_an_offset_or_none_is_read_as_utc(releases):
    seal(releases, "r6", sealed="2026-11-23T01:30:00+02:00", versions={"orders": "3.0.0"})
    seal(releases, "r7", sealed="2026-11-23T10:00:00Z", versions={"orders": "3.1.0"})
    seal(releases, "r8", sealed="2026-11-24T10:00:00", versions={"orders": "3.2.0"})
    found = rs.find_releases([str(releases)], PRODUCT).releases
    times = {r.expose_versions["orders"]: rs._stamp(r.sealed_at) for r in found}
    assert times["3.0.0"] == "2026-11-22T23:30:00Z"
    assert times["3.1.0"] == "2026-11-23T10:00:00Z" and times["3.2.0"] == "2026-11-24T10:00:00Z"


def test_a_release_sealed_at_midnight_was_sealed_during_the_new_day_not_in_force_at_its_start(
    tmp_path,
):
    root = tmp_path / "rel"
    seal(root, "old", sealed="2026-11-19T10:00:00Z", versions={"orders": "1.0.0"})
    seal(root, "mid", sealed="2026-11-20T00:00:00Z", versions={"orders": "1.1.0"})
    seal(root, "next", sealed="2026-11-21T00:00:00Z", versions={"orders": "1.2.0"})
    found = rs.find_releases([str(root)], PRODUCT).releases
    got = rs.in_force(found, date(2026, 11, 20), moment("2026-11-25T00:00:00"))
    assert got.at_start.expose_versions["orders"] == "1.0.0"
    assert [r.expose_versions["orders"] for r in got.during] == ["1.1.0"]  # the next day's is not


def test_the_last_day_there_is_has_an_end(releases):
    found = rs.find_releases([str(releases)], PRODUCT).releases
    got = rs.in_force(found, date(9999, 12, 31), moment("2026-11-25T00:00:00"))
    assert got.at_start.expose_versions["orders"] == "2.0.0" and got.end.year == 9999


def test_each_environment_is_answered_on_its_own(tmp_path):
    root = tmp_path / "envs"
    seal(root, "p1", sealed="2026-11-10T09:00:00Z", versions={"orders": "1.0.0"})
    seal(
        root,
        "d1",
        sealed="2026-11-12T09:00:00Z",
        versions={"orders": "2.0.0-dev"},
        environment="dev",
    )
    seal(root, "p2", sealed="2026-11-14T09:00:00Z", versions={"orders": "1.1.0"})
    found = rs.find_releases([str(root)], PRODUCT)
    day = date(2026, 11, 13)
    answers = rs.in_force_by_environment(found.releases, day, moment("2026-11-25T00:00:00"))
    assert {a.environment: a.at_start.expose_versions["orders"] for a in answers} == {
        "dev": "2.0.0-dev",
        "prod": "1.0.0",  # the dev release sealed after it does not stand in for it
    }
    only = rs.in_force_by_environment(found.releases, day, moment("2026-11-25T00:00:00"), "prod")
    assert [a.environment for a in only] == ["prod"]
    text = "\n".join(rs.report(PRODUCT, answers, found, day))
    assert "environment dev:" in text and "environment prod:" in text


def test_a_release_with_no_environment_is_an_answer_of_its_own_and_comes_first(tmp_path):
    root = tmp_path / "rel"
    seal(root, "a", environment=None, versions={"orders": "1.0.0"})
    seal(root, "b", sealed="2026-11-19T00:00:00Z", versions={"orders": "1.1.0"})
    found = rs.find_releases([str(root)], PRODUCT)
    answers = rs.in_force_by_environment(found.releases, None, moment("2026-11-25T00:00:00"))
    assert [a.environment for a in answers] == [None, "prod"]
    assert "environment (none recorded):" in "\n".join(rs.report(PRODUCT, answers, found, None))


def test_the_text_answer_names_the_version_the_release_the_run_and_the_code(releases):
    found = rs.find_releases([str(releases)], PRODUCT)
    answer = rs.in_force(found.releases, date(2026, 11, 20), moment("2026-11-25T00:00:00"))
    text = "\n".join(rs.report(PRODUCT, [answer], found, date(2026, 11, 20)))
    assert "orders 1.4.0" in text and "orders 1.5.0" in text
    assert f"release  {found.releases[0].release_id}" in text
    assert "run 3f9a1c0b7e2d" in text and "env prod" in text and "forge 0.37.0" in text
    assert f"code     {SHA} https://github.com/org/orders refs/tags/v1.4.0" in text
    assert "(the record names no source)" in text  # the second release names none
    # the bundle a commit's fluid.bundle-digest stamp names, so a snapshot finds its release
    assert text.count("bundle   sha256:bundle") == 2
    assert "At the start" not in text and "In force at the start of 2026-11-20" in text
    assert "Sealed during 2026-11-20:" in text


# --- the command ----------------------------------------------------------------------


def runs_status(*argv: str) -> int:
    parser = argparse.ArgumentParser()
    register_runs(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["runs", "status", PRODUCT, *argv])
    return args.func(args, LOG)


def test_the_text_answer_keeps_each_line_whole_at_any_width(releases, capsys, monkeypatch):
    """Piped (a CI log, ``| grep``), the console is 80 columns wide: a release's digest must
    stay on the line that names it."""
    from rich.console import Console

    import fluid_build._console as shown

    monkeypatch.setattr(shown, "console", Console(width=40))
    assert runs_status("--release-dir", str(releases)) == 0
    out = capsys.readouterr().out
    assert re.search(r"^  release  sha256:[0-9a-f]{64}$", out, re.M), out
    assert "  bundle   sha256:bundle\n" in out


def test_the_command_says_which_release_was_in_force_on_a_day(releases, capsys):
    assert runs_status("--release-dir", str(releases), "--on", "2026-11-20") == 0
    out = capsys.readouterr().out
    assert "orders 1.4.0" in out and "orders 1.5.0" in out and "orders 2.0.0" not in out


def test_json_carries_the_same_answer(releases, capsys):
    assert runs_status("--release-dir", str(releases), "--on", "2026-11-20", "--json") == 0
    got = json.loads(capsys.readouterr().out)
    assert got["on"] == "2026-11-20" and got["releases_read"] == 3 and got["not_read"] == {}
    (answer,) = got["answers"]
    assert answer["environment"] == "prod"
    assert answer["in_force"]["expose_versions"] == {"orders": "1.4.0"}
    assert answer["in_force"]["sealed_at"] == "2026-11-18T09:12:03Z"
    assert answer["in_force"]["source"]["revision"] == SHA
    assert [r["run_id"] for r in answer["during"]] == ["aaaa"]


def test_no_release_by_then_is_exit_1(releases, capsys):
    assert runs_status("--release-dir", str(releases), "--on", "2026-11-01") == 1
    assert "No release of commerce.orders was sealed by the end of 2026-11-01." in (
        capsys.readouterr().out
    )


def test_no_release_at_all_is_exit_1(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert runs_status("--release-dir", str(tmp_path / "empty")) == 1
    assert "No release of commerce.orders has been sealed." in capsys.readouterr().out


def test_the_command_can_answer_for_one_environment(tmp_path, capsys):
    root = tmp_path / "envs"
    seal(root, "p1", sealed="2026-11-10T09:00:00Z", versions={"orders": "1.0.0"})
    seal(
        root,
        "d1",
        sealed="2026-11-12T09:00:00Z",
        versions={"orders": "2.0.0-dev"},
        environment="dev",
    )
    assert runs_status("--release-dir", str(root), "--on", "2026-11-13") == 0
    both = capsys.readouterr().out
    assert "environment dev:" in both and "environment prod:" in both
    assert runs_status("--release-dir", str(root), "--on", "2026-11-13", "--env", "prod") == 0
    one = capsys.readouterr().out
    assert "orders 1.0.0" in one and "2.0.0-dev" not in one and "environment prod:" not in one
    assert runs_status("--release-dir", str(root), "--env", "staging") == 1
    assert "No release of commerce.orders in environment staging has been sealed." in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "day",
    [
        "20261120",
        "2026-W47-5",
        "2026-11-20T00:00",
        chr(0xFF12) + "026-11-20",
        "2026-1-20",
        "0000-01-01",
    ]
    + [" 2026-11-20", "2026-11-20\n", "2026-11-31"],
)
def test_a_day_is_year_month_day_and_nothing_else(releases, day):
    assert runs_status("--release-dir", str(releases), "--on", day) == 2


def test_an_environment_that_is_empty_is_a_usage_error(releases, caplog):
    assert runs_status("--release-dir", str(releases), "--env", "  ") == 2
    assert "--env needs an environment name" in caplog.text


def test_the_last_day_there_is_can_be_asked_about(releases, capsys):
    assert runs_status("--release-dir", str(releases), "--on", "9999-12-31") == 0
    assert "orders 2.0.0" in capsys.readouterr().out


def test_a_day_that_is_not_one_and_a_missing_directory_are_usage_errors(releases, tmp_path, caplog):
    assert runs_status("--release-dir", str(releases), "--on", "yesterday") == 2
    assert runs_status("--release-dir", str(tmp_path / "nope")) == 2
    assert runs_status("--on", "2026-11-20") == 2  # a day answers for a release directory
    assert runs_status("--env", "prod") == 2  # and so does an environment
    assert "pass the directory of releases too" in caplog.text


def test_a_hostile_day_is_not_echoed_whole(releases, caplog):
    assert runs_status("--release-dir", str(releases), "--on", "\x1b[31m" + "x" * 200) == 2
    assert "\x1b" not in caplog.text and "x" * 100 not in caplog.text


def test_without_release_dir_the_status_reads_run_records_as_before(tmp_path, caplog):
    assert runs_status("--state-root", str(tmp_path)) == 1
    assert "no builds found for product commerce.orders" in caplog.text


def test_the_module_imports_only_the_standard_library_when_it_is_loaded():
    import ast

    body = ast.parse(Path(rs.__file__).read_text(encoding="utf-8")).body
    names = {a.name.split(".")[0] for n in body if isinstance(n, ast.Import) for a in n.names}
    names |= {(n.module or "").split(".")[0] for n in body if isinstance(n, ast.ImportFrom)}
    assert names <= {
        "__future__", "os", "re", "stat", "dataclasses", "datetime", "pathlib", "typing",
    }  # fmt: skip


# --- a long root, a changed record, a directory that cannot be read ---------------------


def _deep(tmp_path, length=345):
    """A root ``length`` characters long, as a CI runner's work directory can be."""
    root = tmp_path
    while len(str(root)) < length:
        root = root / ("d" * 40)
    return root


def test_under_a_long_root_each_release_shows_the_end_of_its_path(tmp_path):
    root = _deep(tmp_path)
    for name, version in (("b", "1.1.0"), ("a", "1.0.0")):
        seal(root, name, sealed="2026-11-18T09:12:03+00:00", versions={"orders": version})
    found = rs.find_releases([str(root)], PRODUCT)
    paths = [r.path for r in found.releases]
    assert [r.expose_versions["orders"] for r in found.releases] == ["1.0.0", "1.1.0"]
    assert paths[0].endswith(f"{os.sep}a") and paths[1].endswith(f"{os.sep}b"), paths
    assert all(p.startswith("...") and len(p) <= 200 for p in paths)


def test_a_newer_record_that_is_not_intact_never_wins_over_an_older_intact_one(releases):
    seal(releases, "r4", sealed="2026-11-23T08:00:00+00:00", tamper=True)
    found = rs.find_releases([str(releases)], PRODUCT)
    answer = rs.in_force(found.releases, None, moment("2026-11-25T00:00:00"))
    assert answer.at_start is not None and answer.at_start.expose_versions == {"orders": "2.0.0"}
    assert [r.path[-3:] for r in answer.not_intact] == [f"{os.sep}r4"]
    text = "\n".join(rs.report(PRODUCT, [answer], found, None))
    head, _, warning = text.partition("Not in force:")
    assert "orders 2.0.0" in head and "9.9.9" not in head
    assert "NOT INTACT" in warning and "9.9.9" in warning


def test_on_a_day_a_record_that_is_not_intact_is_never_the_one_in_force(releases):
    seal(releases, "r1b", sealed="2026-11-19T00:00:00+00:00", tamper=True)
    found = rs.find_releases([str(releases)], PRODUCT)
    answer = rs.in_force(found.releases, date(2026, 11, 20), moment("2026-11-25T00:00:00"))
    assert answer.at_start is not None and answer.at_start.expose_versions == {"orders": "1.4.0"}
    assert [r.path[-4:] for r in answer.not_intact] == [f"{os.sep}r1b"]


def test_with_no_intact_record_nothing_is_in_force_and_the_command_says_why(tmp_path, capsys):
    root = tmp_path / "releases"
    seal(root, "r1", sealed="2026-11-18T09:12:03+00:00", tamper=True)
    assert runs_status("--release-dir", str(root)) == 1
    out = capsys.readouterr().out
    assert "No intact release of commerce.orders" in out and "Not in force:" in out
    assert runs_status("--release-dir", str(root), "--json") == 1
    (answer,) = json.loads(capsys.readouterr().out)["answers"]
    assert answer["in_force"] is None and answer["not_intact"][0]["intact"] is False


def test_a_release_dir_that_cannot_be_read_is_a_usage_error(tmp_path, caplog):
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    locked = tmp_path / "locked"
    (locked / "releases").mkdir(parents=True)
    locked.chmod(0)
    try:
        assert runs_status("--release-dir", str(locked / "releases")) == 2
    finally:
        locked.chmod(0o700)
    assert "cannot be read" in caplog.text
